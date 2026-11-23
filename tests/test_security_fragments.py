import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft4Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT4

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.security import (
    KUBERNETES_FRAGMENT,
    OCI_FRAGMENT,
    SECURITY_DIR,
    merge_oci_runtime_fragment,
    render_security_fragments,
    validate_oci_seccomp,
    validate_security,
)

SCHEMA_DIR = Path(__file__).parent / "schemas" / "oci-runtime-spec-v1.3.0"
SCHEMA_BASE = "https://opencontainers.org/runtime-spec/v1.3.0/schema/"
BASE_IMAGE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {"Env": [], "WorkingDir": "/"},
    "rootfs": {"type": "layers", "diff_ids": []},
}
BASE_RUNTIME_CONFIG = {
    "ociVersion": "1.3.0",
    "root": {"path": "rootfs"},
    "process": {
        "cwd": "/",
        "args": ["/bin/sh"],
        "user": {"uid": 0, "gid": 0},
        "capabilities": {
            "bounding": ["CAP_AUDIT_WRITE", "CAP_KILL", "CAP_NET_BIND_SERVICE"],
            "effective": ["CAP_AUDIT_WRITE", "CAP_KILL", "CAP_NET_BIND_SERVICE"],
            "permitted": ["CAP_AUDIT_WRITE", "CAP_KILL", "CAP_NET_BIND_SERVICE"],
        },
    },
    "linux": {
        "namespaces": [{"type": "pid"}, {"type": "mount"}],
        "seccomp": {"defaultAction": "SCMP_ACT_ALLOW", "flags": ["SECCOMP_FILTER_FLAG_LOG"]},
    },
}
SECCOMP_PROFILE = {
    "defaultAction": "SCMP_ACT_ERRNO",
    "defaultErrnoRet": 1,
    "architectures": ["SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64"],
    "syscalls": [
        {"names": ["read", "write", "exit_group"], "action": "SCMP_ACT_ALLOW"},
        {
            "names": ["personality"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": 0, "op": "SCMP_CMP_EQ"}],
        },
    ],
}
OCI_CAPABILITIES = {
    "bounding": ["CAP_NET_BIND_SERVICE", "CAP_KILL"],
    "effective": ["CAP_NET_BIND_SERVICE"],
    "permitted": ["CAP_NET_BIND_SERVICE", "CAP_KILL"],
    "inheritable": ["CAP_NET_BIND_SERVICE"],
    "ambient": ["CAP_NET_BIND_SERVICE"],
}


def _runtime_schema_validator():
    registry = Registry()
    for path in SCHEMA_DIR.glob("*.json"):
        contents = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(
            SCHEMA_BASE + path.name, Resource.from_contents(contents, default_specification=DRAFT4)
        )
    schema = json.loads((SCHEMA_DIR / "config-schema.json").read_text(encoding="utf-8"))
    schema["id"] = SCHEMA_BASE + "config-schema.json"
    return Draft4Validator(schema, registry=registry)


def _context_with_profile(tmp_path, profile=SECCOMP_PROFILE, name="seccomp.json"):
    context = tmp_path / "context"
    (context / "security").mkdir(parents=True, exist_ok=True)
    (context / "app.py").write_text("print('hello')\n")
    (context / "security" / name).write_text(
        profile if isinstance(profile, str) else json.dumps(profile)
    )
    return context


def test_kubernetes_fragment_has_exact_security_context_fields():
    security = validate_security(
        {
            "kubernetes": {
                "capabilities": {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]},
                "seccomp": {"type": "RuntimeDefault"},
                "apparmor": {"type": "Localhost", "localhost-profile": "k8s-apparmor-example"},
                "selinux": {"user": "system_u", "role": "system_r", "type": "container_t", "level": "s0:c123,c456"},
                "read-only-root-filesystem": True,
            }
        },
        "linux/amd64",
    )

    assert render_security_fragments(security, ".") == {
        KUBERNETES_FRAGMENT: {
            "capabilities": {"add": ["NET_BIND_SERVICE"], "drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
            "appArmorProfile": {"type": "Localhost", "localhostProfile": "k8s-apparmor-example"},
            "seLinuxOptions": {"user": "system_u", "role": "system_r", "type": "container_t", "level": "s0:c123,c456"},
            "readOnlyRootFilesystem": True,
        }
    }
    assert validate_security(security, "linux/arm64") == security


def test_kubernetes_localhost_seccomp_and_partial_fields():
    security = validate_security(
        {"kubernetes": {"seccomp": {"type": "Localhost", "localhost_profile": "profiles/audit.json"}, "selinux": {"level": "s0"}}},
        "linux/arm64/v8",
    )

    assert render_security_fragments(security, ".")[KUBERNETES_FRAGMENT] == {
        "seccompProfile": {"type": "Localhost", "localhostProfile": "profiles/audit.json"},
        "seLinuxOptions": {"level": "s0"},
    }


@pytest.mark.parametrize(
    ("kubernetes", "message"),
    [
        ({"seccomp": {"type": "Unconfined"}}, "expected one of RuntimeDefault, Localhost"),
        ({"apparmor": {"type": "Unconfined"}}, "expected one of RuntimeDefault, Localhost"),
        ({"seccomp": {"type": "Localhost"}}, "is required when type is 'Localhost'"),
        ({"seccomp": {"type": "RuntimeDefault", "localhost_profile": "a.json"}}, "only valid when type is 'Localhost'"),
        ({"seccomp": {"type": "Localhost", "localhost_profile": "/var/lib/kubelet/seccomp/a.json"}}, "relative path"),
        ({"seccomp": {"type": "Localhost", "localhost_profile": "../a.json"}}, "relative path"),
        ({"apparmor": {"type": "Localhost", "localhost_profile": "my profile"}}, "whitespace"),
        ({"capabilities": {"add": ["CAP_NET_ADMIN"]}}, "Kubernetes expects 'NET_ADMIN'"),
        ({"capabilities": {"add": ["NET_MAGIC"]}}, "unknown capability 'NET_MAGIC'"),
        ({"capabilities": {"add": ["ALL"]}}, "'ALL' is only supported"),
        ({"capabilities": {"add": ["NET_RAW"], "drop": ["NET_RAW"]}}, "both added and dropped: NET_RAW"),
        ({"capabilities": {"drop": ["KILL", "KILL"]}}, "duplicate capability 'KILL'"),
        ({"capabilities": {"add": [], "drop": []}}, "at least one capability"),
        ({"capabilities": {"keep": ["KILL"]}}, "unknown key(s) 'keep'"),
        ({"selinux": {"user": "system_u:object_r"}}, "may contain only"),
        ({"selinux": {"level": "s0:"}}, "not a valid MLS/MCS level"),
        ({"selinux": {}}, "must not be empty"),
        ({"read_only_root_filesystem": "yes"}, "expected a boolean"),
        ({"read_only_root_filesystem": True, "read-only-root-filesystem": False}, "contains both"),
        ({"privileged": True}, "unknown key(s) 'privileged'"),
        ({}, "must not be empty"),
    ],
)
def test_kubernetes_settings_reject_ambiguous_or_unsupported_values(kubernetes, message):
    with pytest.raises(ValueError) as error:
        validate_security({"kubernetes": kubernetes}, "linux/amd64")
    assert message in str(error.value)


def test_oci_fragment_is_exact_and_valid_after_merging_into_runtime_config(tmp_path):
    context = _context_with_profile(tmp_path)
    security = validate_security(
        {
            "oci": {
                "capabilities": OCI_CAPABILITIES,
                "seccomp_profile": "security/seccomp.json",
                "apparmor_profile": "pycontainer-test",
                "selinux_label": "system_u:system_r:container_t:s0:c1,c2",
                "mount_label": "system_u:object_r:container_file_t:s0:c1,c2",
                "read_only_root": True,
            }
        },
        "linux/amd64",
    )

    fragment = render_security_fragments(security, str(context))[OCI_FRAGMENT]
    assert fragment == {
        "process": {
            "capabilities": OCI_CAPABILITIES,
            "apparmorProfile": "pycontainer-test",
            "selinuxLabel": "system_u:system_r:container_t:s0:c1,c2",
        },
        "linux": {"seccomp": SECCOMP_PROFILE, "mountLabel": "system_u:object_r:container_file_t:s0:c1,c2"},
        "root": {"readonly": True},
    }

    validator = _runtime_schema_validator()
    validator.validate(BASE_RUNTIME_CONFIG)
    merged = merge_oci_runtime_fragment(BASE_RUNTIME_CONFIG, fragment)
    validator.validate(merged)
    assert merged["process"]["capabilities"] == OCI_CAPABILITIES
    assert merged["linux"]["seccomp"] == SECCOMP_PROFILE
    assert merged["linux"]["namespaces"] == BASE_RUNTIME_CONFIG["linux"]["namespaces"]
    assert merged["root"] == {"path": "rootfs", "readonly": True}
    assert merged["process"]["args"] == ["/bin/sh"]


def test_oci_merge_rejects_fields_outside_the_supported_fragment():
    with pytest.raises(ValueError, match="process.args"):
        merge_oci_runtime_fragment(BASE_RUNTIME_CONFIG, {"process": {"args": ["sh"]}})


def _caps(**overrides):
    caps = copy.deepcopy(OCI_CAPABILITIES)
    caps.update(overrides)
    return caps


@pytest.mark.parametrize(
    ("oci", "message"),
    [
        ({"capabilities": {key: value for key, value in OCI_CAPABILITIES.items() if key != "ambient"}}, "missing: ambient"),
        ({"capabilities": _caps(effective=["CAP_NET_BIND_SERVICE", "CAP_CHOWN"])}, "CAP_CHOWN must also be in permitted"),
        ({"capabilities": _caps(bounding=["CAP_NET_BIND_SERVICE"])}, "CAP_KILL must also be in bounding"),
        ({"capabilities": _caps(inheritable=["CAP_NET_BIND_SERVICE", "CAP_CHOWN"])}, "CAP_CHOWN must also be in bounding"),
        ({"capabilities": _caps(inheritable=[])}, "must also be in both permitted and inheritable"),
        ({"capabilities": _caps(ambient=["NET_BIND_SERVICE"])}, "use OCI names"),
        ({"capabilities": _caps(bounding=["ALL"])}, "'ALL' is only supported"),
        ({"capabilities": _caps(add=["CAP_KILL"])}, "unknown key(s) 'add'"),
        ({"seccomp": "RuntimeDefault"}, "unknown key(s) 'seccomp'"),
        ({"seccomp_profile": "/etc/seccomp.json"}, "relative to the build context"),
        ({"seccomp_profile": "../seccomp.json"}, "relative to the build context"),
        ({"selinux_label": "system_u:system_r"}, "user:role:type[:level]"),
        ({"mount_label": "system_u:object_r:container file_t:s0"}, "whitespace"),
        ({"apparmor_profile": ""}, "non-empty string"),
        ({"read_only_root": 1}, "expected a boolean"),
    ],
)
def test_oci_settings_reject_unresolved_or_invalid_values(oci, message):
    with pytest.raises(ValueError) as error:
        validate_security({"oci": oci}, "linux/amd64")
    assert message in str(error.value)


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ({"defaultAction": "SCMP_ACT_ERRNO", "archMap": []}, "unknown OCI seccomp field(s) 'archMap'"),
        ({"defaultAction": "SCMP_ACT_NOTIFY"}, "SCMP_ACT_NOTIFY requires a seccomp agent"),
        ({"defaultAction": "SCMP_ACT_ALLOW", "listenerPath": "/run/agent.sock"}, "user notification"),
        ({"defaultAction": "SCMP_ACT_KILL", "defaultErrnoRet": 1}, "only valid with SCMP_ACT_ERRNO"),
        ({"defaultAction": "SCMP_ACT_ALLOW", "syscalls": [{"names": ["mkdir"], "action": "SCMP_ACT_LOG", "errnoRet": 1}]}, "only valid with SCMP_ACT_ERRNO"),
        ({"defaultAction": "SCMP_ACT_ALLOW", "architectures": ["SCMP_ARCH_SPARC"]}, "unsupported value 'SCMP_ARCH_SPARC'"),
        ({"defaultAction": "SCMP_ACT_ALLOW", "syscalls": [{"names": [], "action": "SCMP_ACT_ALLOW"}]}, "at least one item"),
        ({"defaultAction": "SCMP_ACT_ALLOW", "syscalls": [{"names": ["kill"], "action": "SCMP_ACT_ALLOW", "args": [{"index": 6, "value": 1, "op": "SCMP_CMP_EQ"}]}]}, "between 0 and 5"),
        ({"defaultAction": "SCMP_ACT_ALLOW", "syscalls": [{"names": ["kill"], "action": "SCMP_ACT_ALLOW", "args": [{"index": 0, "value": 1, "op": "EQ"}]}]}, "unsupported value 'EQ'"),
        ({"defaultAction": "SCMP_ACT_ERRNO", "defaultErrnoRet": -1}, "between 0 and 4294967295"),
        ({"syscalls": []}, "missing required field(s) defaultAction"),
    ],
)
def test_oci_seccomp_profiles_are_strictly_validated(profile, message):
    with pytest.raises(ValueError) as error:
        validate_oci_seccomp(profile)
    assert message in str(error.value)


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ('{"defaultAction": "SCMP_ACT_ALLOW", "defaultAction": "SCMP_ACT_KILL"}', "duplicate JSON key"),
        ("{not json", "is not valid JSON"),
    ],
)
def test_oci_seccomp_file_must_be_unambiguous_json(tmp_path, contents, message):
    context = _context_with_profile(tmp_path, contents)
    security = validate_security({"oci": {"seccomp_profile": "security/seccomp.json"}}, "linux/amd64")
    with pytest.raises(ValueError, match=message):
        render_security_fragments(security, str(context))


def test_oci_seccomp_file_must_exist(tmp_path):
    security = validate_security({"oci": {"seccomp_profile": "missing.json"}}, "linux/amd64")
    with pytest.raises(ValueError, match="Seccomp profile not found"):
        render_security_fragments(security, str(tmp_path))


@pytest.mark.parametrize(
    ("security", "platform", "message"),
    [
        ({"kubernetes": {"read_only_root_filesystem": True}}, "windows/amd64", "Linux targets only"),
        ({}, "linux/amd64", "must not be empty"),
        ({"docker": {}}, "linux/amd64", "unknown key(s) 'docker'"),
        (["kubernetes"], "linux/amd64", "expected a table"),
    ],
)
def test_build_config_rejects_unsupported_security_requests(security, platform, message):
    with pytest.raises(ValueError) as error:
        BuildConfig(security=security, platform=platform)
    assert message in str(error.value)


def _build(
    tmp_path, monkeypatch, output_name, security=None, *, dry_run=False, platform="linux/amd64"
):
    context = _context_with_profile(tmp_path)
    monkeypatch.setattr(
        ImageBuilder,
        "_pull_base_image",
        lambda self, layers_dir, os_name, arch: (
            [], {**BASE_IMAGE_CONFIG, "architecture": arch}
        ),
    )
    builder = ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / output_name),
            include_paths=["app.py"],
            entrypoint=["python", "app.py"],
            use_cache=False,
            security=security,
            dry_run=dry_run,
            platform=platform,
        )
    )
    builder.build()
    return builder


FULL_SECURITY = {
    "kubernetes": {"capabilities": {"drop": ["ALL"]}, "read_only_root_filesystem": True},
    "oci": {"seccomp_profile": "security/seccomp.json", "read_only_root": True},
}


def test_security_fragments_do_not_change_the_image(tmp_path, monkeypatch):
    plain = _build(tmp_path, monkeypatch, "plain")
    secured = _build(tmp_path, monkeypatch, "secured", FULL_SECURITY)

    assert not (tmp_path / "plain" / SECURITY_DIR).exists()
    assert (secured.manifest_digest, secured.config_digest) == (plain.manifest_digest, plain.config_digest)
    for name in ("index.json", "oci-layout"):
        assert (tmp_path / "secured" / name).read_bytes() == (tmp_path / "plain" / name).read_bytes()
    assert sorted(p.name for p in (tmp_path / "secured" / "blobs" / "sha256").iterdir()) == sorted(
        p.name for p in (tmp_path / "plain" / "blobs" / "sha256").iterdir()
    )
    security_dir = tmp_path / "secured" / SECURITY_DIR
    assert json.loads((security_dir / KUBERNETES_FRAGMENT).read_text()) == {
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert json.loads((security_dir / OCI_FRAGMENT).read_text()) == {
        "linux": {"seccomp": SECCOMP_PROFILE},
        "root": {"readonly": True},
    }
    image_config = (tmp_path / "secured" / "blobs" / "sha256" / secured.config_digest.split(":", 1)[1]).read_text()
    assert "seccomp" not in image_config.lower() and "readonly" not in image_config.lower()


def test_rebuild_removes_fragments_that_are_no_longer_requested(tmp_path, monkeypatch):
    _build(tmp_path, monkeypatch, "out", FULL_SECURITY)
    unrelated = tmp_path / "out" / SECURITY_DIR / "notes.txt"
    unrelated.write_text("keep")

    _build(tmp_path, monkeypatch, "out", {"kubernetes": FULL_SECURITY["kubernetes"]})
    assert sorted(p.name for p in (tmp_path / "out" / SECURITY_DIR).iterdir()) == [KUBERNETES_FRAGMENT, "notes.txt"]

    unrelated.unlink()
    _build(tmp_path, monkeypatch, "out")
    assert not (tmp_path / "out" / SECURITY_DIR).exists()


def test_multi_platform_security_fragments_are_written_without_changing_the_index(
    tmp_path, monkeypatch
):
    platform = "linux/amd64,linux/arm64"
    plain = _build(tmp_path, monkeypatch, "plain", platform=platform)
    secured = _build(tmp_path, monkeypatch, "out", FULL_SECURITY, platform=platform)

    assert secured.index_digest == plain.index_digest
    assert [image.manifest_digest for image in secured.platform_images] == [
        image.manifest_digest for image in plain.platform_images
    ]
    security_dir = tmp_path / "out" / SECURITY_DIR
    assert json.loads((security_dir / KUBERNETES_FRAGMENT).read_text()) == {
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert json.loads((security_dir / OCI_FRAGMENT).read_text()) == {
        "linux": {"seccomp": SECCOMP_PROFILE},
        "root": {"readonly": True},
    }

    _build(tmp_path, monkeypatch, "out", platform=platform)
    assert not security_dir.exists()


def test_multi_platform_security_accepts_spaced_platforms(tmp_path, monkeypatch):
    builder = _build(
        tmp_path, monkeypatch, "out", FULL_SECURITY,
        platform=" linux/amd64, linux/arm64 ",
    )

    assert [image.platform["architecture"] for image in builder.platform_images] == [
        "amd64", "arm64"
    ]
    assert (tmp_path / "out" / SECURITY_DIR / OCI_FRAGMENT).is_file()


def test_failed_platform_preserves_security_fragments_and_index(tmp_path, monkeypatch):
    context = _context_with_profile(tmp_path)
    output = tmp_path / "out"
    refs = output / "refs" / "tags"
    security_dir = output / SECURITY_DIR
    refs.mkdir(parents=True)
    security_dir.mkdir()
    (output / "index.json").write_text("previous index")
    (refs / "v1").write_text("previous tag")
    (security_dir / OCI_FRAGMENT).write_text("previous fragment")

    def pull(self, layers_dir, os_name, arch):
        if arch == "arm64":
            raise RuntimeError("arm64 base unavailable")
        return [], BASE_IMAGE_CONFIG

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", pull)
    builder = ImageBuilder(BuildConfig(
        tag="app:v1",
        context_dir=str(context),
        output_dir=str(output),
        include_paths=["app.py"],
        platform="linux/amd64,linux/arm64",
        security=FULL_SECURITY,
        use_cache=False,
    ))

    with pytest.raises(RuntimeError, match="arm64 base unavailable"):
        builder.build()
    assert (output / "index.json").read_text() == "previous index"
    assert (refs / "v1").read_text() == "previous tag"
    assert (security_dir / OCI_FRAGMENT).read_text() == "previous fragment"


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_invalid_seccomp_profile_fails_before_base_image_pull(tmp_path, monkeypatch, platform):
    context = _context_with_profile(tmp_path, {"defaultAction": "SCMP_ACT_NOTIFY"})
    output = tmp_path / "out"
    refs = output / "refs" / "tags"
    refs.mkdir(parents=True)
    (output / "index.json").write_text("previous index")
    (refs / "v1").write_text("previous tag")

    def unexpected_pull(*args):
        raise AssertionError("base image must not be pulled")

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", unexpected_pull)
    builder = ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(output),
            tag="app:v1",
            platform=platform,
            include_paths=["app.py"],
            use_cache=False,
            security={"oci": {"seccomp_profile": "security/seccomp.json"}},
        )
    )
    with pytest.raises(ValueError, match="SCMP_ACT_NOTIFY"):
        builder.build()
    assert not (output / SECURITY_DIR).exists()
    assert (output / "index.json").read_text() == "previous index"
    assert (refs / "v1").read_text() == "previous tag"


def test_dry_run_validates_and_reports_fragments_without_writing(tmp_path, monkeypatch, capsys):
    _build(tmp_path, monkeypatch, "out", FULL_SECURITY, dry_run=True)

    output = capsys.readouterr().out
    assert f"{SECURITY_DIR}/{KUBERNETES_FRAGMENT}" in output
    assert f"{SECURITY_DIR}/{OCI_FRAGMENT}" in output
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
def test_security_tables_load_from_config_file(tmp_path, table):
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        f"[{table}]\ntag = \"demo:1\"\n\n"
        f"[{table}.security.kubernetes]\n"
        "read-only-root-filesystem = true\n"
        "seccomp = { type = \"RuntimeDefault\" }\n"
        "capabilities = { drop = [\"ALL\"], add = [\"NET_BIND_SERVICE\"] }\n"
    )

    config = config_from_file(config_path)
    assert render_security_fragments(config.security, ".")[KUBERNETES_FRAGMENT] == {
        "capabilities": {"add": ["NET_BIND_SERVICE"], "drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
        "readOnlyRootFilesystem": True,
    }


def test_security_config_file_errors_are_explicit(tmp_path):
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text("[build]\nsecurity = \"strict\"\n")
    with pytest.raises(ValueError, match=r"\[build\]\.security: expected dict or None, got str"):
        config_from_file(config_path)

    config_path.write_text("[build.security.kubernetes]\nseccomp = { type = \"Unconfined\" }\n")
    with pytest.raises(ValueError, match="security.kubernetes.seccomp.type"):
        config_from_file(config_path)
