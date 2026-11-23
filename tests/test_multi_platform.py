"""Hermetic tests for linux/amd64 + linux/arm64 OCI image-index builds."""
import gzip
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from pycontainer import builder as builder_module
from pycontainer.auth import RegistryAuth
from pycontainer.builder import ImageBuilder, LayerIntegrityError, UnsupportedPlatformError
from pycontainer.config import BuildConfig, normalize_platforms
from pycontainer.registry_client import BlobIntegrityError, RegistryClient, RegistryRequestError

INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
MULTI = "linux/amd64,linux/arm64"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _base_layer(arch: str, libc_version=None):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        payload = f"base for {arch}".encode()
        info = tarfile.TarInfo(f"etc/{arch}")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        if libc_version is not None:
            payload = (
                "Package: libc6\nStatus: install ok installed\n"
                f"Version: {libc_version}-1\n"
                f"Architecture: {'armhf' if arch.startswith('arm-') else arch}\n\n"
            ).encode()
            info = tarfile.TarInfo("var/lib/dpkg/status")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    uncompressed = raw.getvalue()
    return gzip.compress(uncompressed, mtime=0), _digest(uncompressed)


class FakeBaseRegistry:
    """Serves a two-platform base index; records pulls for assertions."""

    def __init__(self, platforms=("amd64", "arm64"), wrong_diff_id_arch=None, config_variants=None,
                 base_env=None, base_env_by_arch=None, base_runtime_by_arch=None,
                 libc_by_arch=None):
        self.blobs = {}
        self.manifests = {}
        descriptors = []
        for entry in platforms:
            arch, _, variant = entry.partition("/")
            layer, diff_id = _base_layer(
                entry.replace("/", "-"), libc_by_arch[arch] if libc_by_arch is not None else None
            )
            if entry == wrong_diff_id_arch:
                diff_id = _digest(b"not the layer content")
            platform = {"os": "linux", "architecture": arch}
            if variant:
                platform["variant"] = variant
            # Real configs may omit the variant; the index descriptor is authoritative.
            config_variant = (config_variants or {}).get(entry)
            env = base_env_by_arch[arch] if base_env_by_arch is not None else base_env
            if env is None:
                env = [
                    "PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.9",
                    "PYTHON_LIBC=glibc-2.17",
                ]
            runtime = {"Env": env, "WorkingDir": "/"}
            if base_runtime_by_arch is not None:
                runtime.update(base_runtime_by_arch[arch])
            config = json.dumps({
                "os": "linux",
                "architecture": arch,
                **({"variant": config_variant} if config_variant else {}),
                "config": runtime,
                "rootfs": {"type": "layers", "diff_ids": [diff_id]},
            }).encode()
            manifest = json.dumps({
                "schemaVersion": 2,
                "mediaType": MANIFEST_TYPE,
                "config": {"mediaType": CONFIG_TYPE, "digest": _digest(config), "size": len(config)},
                "layers": [{
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": _digest(layer),
                    "size": len(layer),
                }],
            }).encode()
            self.blobs[_digest(layer)] = layer
            self.blobs[_digest(config)] = config
            self.manifests[_digest(manifest)] = manifest
            descriptors.append({
                "mediaType": MANIFEST_TYPE,
                "digest": _digest(manifest),
                "size": len(manifest),
                "platform": platform,
            })
        index = json.dumps({"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": descriptors}).encode()
        self.index_digest = _digest(index)
        self.manifests["3.11-slim"] = index
        self.manifests[self.index_digest] = index
        self.manifest_calls = []

    def client(self, *args, **kwargs):
        registry = self

        class Client:
            def pull_manifest(self, reference, **expected):
                registry.manifest_calls.append((reference, expected))
                body = registry.manifests[reference]
                return json.loads(body), _digest(body)

            def pull_blob(self, digest, path, expected_size):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(registry.blobs[digest])

        return Client()


class FakePushClient:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, *args, **kwargs):
        return self

    def _record(self, call):
        if self.fail_on and self.fail_on(call):
            raise RuntimeError(f"simulated failure on {call}")
        self.calls.append(call)

    def push_blob(self, digest, path, check_exists=False):
        assert Path(path).is_file()
        self._record(("blob", digest))
        return True

    def push_manifest(self, reference, data):
        self._record(("manifest", reference, _digest(data)))

    def push_index(self, reference, data):
        self._record(("index", reference, _digest(data)))


def _context(tmp_path, requirements=False):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "app.py").write_text("print('hi')\n")
    (ctx / "pyproject.toml").write_text('[project]\nname="demo"\nversion="0.1"\n')
    if requirements:
        (ctx / "requirements.txt").write_text("demo-wheel==1.0\n")
    return ctx


def _builder(tmp_path, monkeypatch, registry, **overrides):
    monkeypatch.setattr(builder_module, "RegistryClient", registry.client)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    ctx = overrides.pop("ctx", None) or _context(tmp_path)
    options = dict(
        tag="registry.example/team/app:v1",
        base_image="registry.example/library/python:3.11-slim",
        context_dir=str(ctx),
        output_dir=str(tmp_path / "out"),
        platform=MULTI,
        include_deps=False,
        use_cache=False,
        reproducible=True,
    )
    options.update(overrides)
    return ImageBuilder(BuildConfig(**options))


def _blob(out: Path, digest: str) -> bytes:
    return (out / "blobs" / "sha256" / digest.split(":", 1)[1]).read_bytes()


def _record_tag_provenance(monkeypatch):
    seen = []

    def fake_deps_layer(self, layers_dir, **kwargs):
        seen.append((kwargs["arch"], self._base_image_tag_verified))
        return None

    monkeypatch.setattr(ImageBuilder, "_create_deps_layer", fake_deps_layer)
    return seen


@pytest.mark.parametrize("reference, verified", [
    ("registry.example/library/python:3.11-slim", True),
    ("registry.example/library/python:3.11-slim@{digest}", False),
    ("registry.example/library/python@{digest}", False),
])
def test_tag_provenance_survives_digest_pinning_only_for_tag_pulls(
    tmp_path, monkeypatch, reference, verified
):
    registry = FakeBaseRegistry(platforms=("amd64", "arm64", "arm/v6", "arm/v7"))
    seen = _record_tag_provenance(monkeypatch)
    builder = _builder(
        tmp_path, monkeypatch, registry, include_deps=True,
        platform="linux/amd64,linux/arm64,linux/arm/v7",
        base_image=reference.format(digest=registry.index_digest),
    )
    builder.build()

    top_level = [ref for ref, _ in registry.manifest_calls if ref in ("3.11-slim", registry.index_digest)]
    assert len(top_level) == 1
    assert seen == [("amd64", verified), ("arm64", verified), ("arm", verified)]


@pytest.mark.parametrize("reference, resolve_tag, verified", [
    ("registry.example/library/python:3.11-slim", True, True),
    ("registry.example/library/python:3.11-slim", False, False),
    ("registry.example/library/python:3.11-slim@{digest}", True, False),
])
def test_staging_abi_checks_receive_per_platform_tag_provenance(
    tmp_path, monkeypatch, reference, resolve_tag, verified
):
    registry = FakeBaseRegistry(platforms=("amd64", "arm64", "arm/v6", "arm/v7"))
    if not resolve_tag:
        # The registry did not vouch for the tag with a digest.
        client_factory = registry.client

        def unresolved_client(*args, **kwargs):
            client = client_factory()
            pull = client.pull_manifest

            def pull_manifest(ref, **expected):
                manifest, digest = pull(ref, **expected)
                return manifest, None if ref == "3.11-slim" else digest

            client.pull_manifest = pull_manifest
            return client

        registry.client = unresolved_client
    seen = []

    def record(name):
        def fake(*args, base_image_tag_verified=False, variant=None, **kwargs):
            seen.append((name, base_image_tag_verified, variant))
            return [] if name == "install" else "3.11"
        return fake

    monkeypatch.setattr(builder_module, "target_python_version", record("version"))
    monkeypatch.setattr(builder_module, "target_wheel_platform", record("wheel"))
    monkeypatch.setattr(builder_module, "install_requirements", record("install"))
    builder = _builder(
        tmp_path, monkeypatch, registry, include_deps=True,
        ctx=_context(tmp_path, requirements=True),
        platform="linux/amd64,linux/arm64,linux/arm/v7",
        base_image=reference.format(digest=registry.index_digest),
    )
    builder.build()

    assert seen == [
        (name, verified, None if name == "version" else variant)
        for variant in (None, None, "v7")
        for name in ("version", "wheel", "install")
    ]


@pytest.mark.parametrize("by_digest", [False, True])
@pytest.mark.parametrize("arm_first", [False, True])
def test_tag_only_base_abi_is_accepted_only_after_verified_tag_pull(
    tmp_path, monkeypatch, by_digest, arm_first
):
    registry = FakeBaseRegistry(
        platforms=("amd64", "arm64", "arm/v6", "arm/v7"),
        base_env=[], libc_by_arch={"amd64": "2.28", "arm64": "2.36", "arm": "2.31"},
    )
    installs = []

    def fake_install(requirements, site_packages, context, os_name, arch, version,
                     base_image, base_config, variant=None, *, base_image_tag_verified):
        installs.append((arch, variant, version, base_image_tag_verified))
        return []

    monkeypatch.setattr(builder_module, "install_requirements", fake_install)
    base_image = "python:3.11-slim"
    if by_digest:
        base_image += "@" + registry.index_digest
    platforms = ["linux/amd64", "linux/arm64", "linux/arm/v7"]
    if arm_first:
        platforms = ["linux/arm/v7", "linux/amd64", "linux/arm64"]
    builder = _builder(
        tmp_path, monkeypatch, registry, include_deps=True,
        ctx=_context(tmp_path, requirements=True), base_image=base_image,
        platform=",".join(platforms),
    )

    if by_digest:
        with pytest.raises(ValueError, match="unverified or custom base image"):
            builder.build()
        assert installs == []
        assert not (tmp_path / "out" / "index.json").exists()
    else:
        builder.build()
        expected = [
            ("amd64", None, "3.11", True),
            ("arm64", None, "3.11", True),
            ("arm", "v7", "3.11", True),
        ]
        if arm_first:
            expected = expected[-1:] + expected[:-1]
        assert installs == expected


@pytest.mark.parametrize("source", ["verified-tag", "unresolved-tag", "digest"])
def test_platform_libc_uses_selected_layers_only_with_verified_provenance(
    tmp_path, monkeypatch, source
):
    registry = FakeBaseRegistry(
        platforms=("amd64", "arm64", "arm/v7"),
        base_env=["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.9"],
        libc_by_arch={"amd64": "2.28", "arm64": "2.36", "arm": "2.31"},
    )
    if source == "unresolved-tag":
        client_factory = registry.client

        def unresolved_client(*args, **kwargs):
            client = client_factory(*args, **kwargs)
            pull = client.pull_manifest

            def pull_manifest(reference, **expected):
                manifest, digest = pull(reference, **expected)
                return manifest, None if reference == "3.11-slim" else digest

            client.pull_manifest = pull_manifest
            return client

        registry.client = unresolved_client
    installs = []

    def fake_install(requirements, site_packages, context, os_name, arch, version,
                     base_image, base_config, *, base_image_tag_verified, variant=None):
        installs.append((arch, base_config["config"]["Env"], base_image_tag_verified))
        return []

    monkeypatch.setattr(builder_module, "install_requirements", fake_install)
    base_image = "python:3.11-slim"
    if source == "digest":
        base_image += "@" + registry.index_digest
    builder = _builder(
        tmp_path, monkeypatch, registry, include_deps=True,
        ctx=_context(tmp_path, requirements=True), base_image=base_image,
        platform="linux/amd64,linux/arm64,linux/arm/v7",
    )

    if source != "verified-tag":
        with pytest.raises(ValueError, match="unverified or custom"):
            builder.build()
        assert installs == []
        assert not (tmp_path / "out" / "index.json").exists()
        return

    builder.build()
    assert [
        (arch, next(item for item in env if item.startswith("PYTHON_LIBC=")), verified)
        for arch, env, verified in installs
    ] == [
        ("amd64", "PYTHON_LIBC=glibc-2.28", True),
        ("arm64", "PYTHON_LIBC=glibc-2.36", True),
        ("arm", "PYTHON_LIBC=glibc-2.31", True),
    ]
    for image in builder.platform_images:
        config = json.loads(_blob(tmp_path / "out", image.config_digest))
        assert not any(item.startswith("PYTHON_LIBC=") for item in config["config"]["Env"])


@pytest.mark.parametrize("source", ["verified-tag", "unresolved-tag", "digest"])
def test_locked_index_uses_target_markers_and_verified_base_provenance(tmp_path, monkeypatch, source):
    from test_lockfiles import _write_target_marker_uv_project

    ctx = tmp_path / "ctx"
    _write_target_marker_uv_project(ctx)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(ctx / "wheelhouse"))
    registry = FakeBaseRegistry(
        platforms=("amd64", "arm64", "arm/v7"),
        base_env=["PYTHON_VERSION=3.11.9"],
        libc_by_arch={"amd64": "2.28", "arm64": "2.36", "arm": "2.28"},
    )
    if source == "unresolved-tag":
        client_factory = registry.client

        def unresolved_client(*args, **kwargs):
            client = client_factory(*args, **kwargs)
            pull = client.pull_manifest

            def pull_manifest(reference, **expected):
                manifest, digest = pull(reference, **expected)
                return manifest, None if reference == "3.11-slim" else digest

            client.pull_manifest = pull_manifest
            return client

        registry.client = unresolved_client
    installs = []
    real_install = builder_module.install_requirements

    def install(*args, **kwargs):
        installs.append((args[4], kwargs["locked"], kwargs["base_image_tag_verified"]))
        return real_install(*args, **kwargs)

    monkeypatch.setattr(builder_module, "install_requirements", install)
    base_image = "python:3.11-slim"
    if source == "digest":
        base_image += "@" + registry.index_digest
    platforms = (
        "linux/amd64,linux/arm64,linux/arm/v7"
        if source == "verified-tag"
        else "linux/arm/v7,linux/amd64,linux/arm64"
    )
    builder = _builder(
        tmp_path, monkeypatch, registry, ctx=ctx, include_deps=True, base_image=base_image,
        platform=platforms,
        labels={"example.label": "explicit"}, manifest_annotations={"example.manifest": "value"},
        index_annotations={"example.index": "value"},
    )

    if source != "verified-tag":
        with pytest.raises(ValueError, match="unverified or custom base image"):
            builder.build()
        assert installs == []
        assert not (tmp_path / "out" / "index.json").exists()
        return

    builder.build()
    assert installs == [("amd64", True, True), ("arm64", True, True), ("arm", True, True)]
    index = json.loads(_blob(tmp_path / "out", builder.index_digest))
    assert index["annotations"]["example.index"] == "value"
    for image in builder.platform_images:
        arch = image.platform["architecture"]
        suffix = {"amd64": "amd", "arm64": "arm", "arm": "armv7"}[arch]
        manifest = json.loads(_blob(tmp_path / "out", image.manifest_digest))
        config = json.loads(_blob(tmp_path / "out", image.config_digest))
        assert manifest["annotations"]["example.manifest"] == "value"
        assert config["config"]["Labels"]["example.label"] == "explicit"
        assert len(config["rootfs"]["diff_ids"]) == len(manifest["layers"])
        assert config["rootfs"]["diff_ids"][-2:] == [
            layer.digest for layer in image.layers[-2:]
        ]
        with tarfile.open(image.layers[-2].tar_path) as archive:
            names = archive.getnames()
        for prefix in ("marker_root", "marker_trans"):
            assert f"app/.python/site-packages/{prefix}_{suffix}/__init__.py" in names
            for other in {"amd", "arm", "armv7"} - {suffix}:
                assert not any(f"{prefix}_{other}/" in name for name in names)


def test_locked_arm_v7_rejects_incompatible_platform_wheel(tmp_path, monkeypatch):
    from test_dependency_staging import _write_named_wheel
    from test_lockfiles import _write_target_marker_uv_project

    ctx = tmp_path / "ctx"
    _write_target_marker_uv_project(ctx)
    wheelhouse = ctx / "wheelhouse"
    arm_wheel = wheelhouse / "marker_root_armv7-1.0-py3-none-manylinux2014_armv7l.whl"
    _write_named_wheel(wheelhouse, "marker-root-armv7", tag="py3-none-manylinux2014_x86_64")
    foreign_wheel = wheelhouse / "marker_root_armv7-1.0-py3-none-manylinux2014_x86_64.whl"
    lock = ctx / "uv.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            arm_wheel.name, foreign_wheel.name
        ).replace(
            hashlib.sha256(arm_wheel.read_bytes()).hexdigest(),
            hashlib.sha256(foreign_wheel.read_bytes()).hexdigest(),
        ),
        encoding="utf-8",
    )
    arm_wheel.unlink()
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(platforms=("arm/v7",)),
        ctx=ctx, include_deps=True, platform="linux/arm/v7",
    )

    with pytest.raises(RuntimeError, match="linux/arm/v7"):
        builder.build()
    assert not (tmp_path / "out" / "index.json").exists()


def test_normalize_platforms_strips_and_dedupes():
    assert normalize_platforms(" linux/amd64 , linux/arm64,linux/amd64") == [
        "linux/amd64",
        "linux/arm64",
    ]
    with pytest.raises(ValueError):
        normalize_platforms("linux/amd64,,linux/arm64")


def test_scanning_multi_platform_config_is_rejected():
    with pytest.raises(ValueError, match="scanning.*multi-platform"):
        BuildConfig(platform=MULTI, scan=True)


@pytest.mark.parametrize("dry_run", [False, True])
def test_enabling_scanning_after_configuration_rejects_index_build(
    tmp_path, monkeypatch, dry_run
):
    registry = FakeBaseRegistry()
    builder = _builder(tmp_path, monkeypatch, registry, dry_run=dry_run)
    builder.config.scan = True
    with pytest.raises(ValueError, match="scanning.*multi-platform"):
        builder.build()
    assert registry.manifest_calls == []
    assert not (tmp_path / "out" / "index.json").exists()


def test_enabling_scanning_after_index_build_rejects_push_before_auth(
    tmp_path, monkeypatch
):
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry())
    builder.build()
    original_index = (tmp_path / "out" / "index.json").read_bytes()
    builder.config.scan = True
    builder.config.platform = "linux/amd64"
    monkeypatch.setattr(
        builder_module, "get_registry_auth", lambda *args, **kwargs: pytest.fail("auth before gate")
    )
    with pytest.raises(ValueError, match="scanning.*multi-platform"):
        builder.push(show_progress=False)
    assert (tmp_path / "out" / "index.json").read_bytes() == original_index


@pytest.mark.parametrize(
    "entrypoint,cmd,expected_entrypoint,expected_cmd",
    [
        (None, ["/bin/sh", "-c", 'echo "$NAME"'], "inherited", ["/bin/sh", "-c", 'echo "$NAME"']),
        (None, ["python", "app.py"], "inherited", ["python", "app.py"]),
        (None, [], "inherited", None),
        ([], None, None, "inherited"),
        ([], [], None, None),
    ],
    ids=["shell-cmd", "exec-cmd", "clear-cmd", "clear-entrypoint", "clear-both"],
)
def test_platform_commands_inherit_and_clear_independently(
    tmp_path, monkeypatch, entrypoint, cmd, expected_entrypoint, expected_cmd
):
    runtime = {
        arch: {"Entrypoint": [f"{arch}-entrypoint"], "Cmd": [f"{arch}-base-cmd"]}
        for arch in ("amd64", "arm64")
    }
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(base_runtime_by_arch=runtime),
        entrypoint=entrypoint, cmd=cmd,
    )
    builder.build()
    for image in builder.platform_images:
        arch = image.platform["architecture"]
        config = json.loads(_blob(tmp_path / "out", image.config_digest))["config"]
        assert config.get("Entrypoint") == (
            runtime[arch]["Entrypoint"] if expected_entrypoint == "inherited"
            else expected_entrypoint
        )
        assert config.get("Cmd") == (
            runtime[arch]["Cmd"] if expected_cmd == "inherited" else expected_cmd
        )


@pytest.mark.parametrize(
    "platform", ["linux/amd64,linux/arm64/v8", "linux/amd64,linux/arm", "linux/amd64,linux/arm/v6"]
)
def test_multi_platform_rejects_unsupported_targets(platform):
    with pytest.raises(ValueError, match="unsupported target"):
        BuildConfig(tag="app:v1", platform=platform)


def test_multi_platform_rejects_sbom():
    with pytest.raises(ValueError, match="SBOM"):
        BuildConfig(tag="app:v1", platform=MULTI, generate_sbom="spdx")


def test_single_platform_config_is_unchanged():
    assert BuildConfig(tag="app:v1", platform="linux/arm/v7").target_platforms() == ["linux/arm/v7"]


def test_builds_verified_index_with_pinned_base(tmp_path, monkeypatch):
    registry = FakeBaseRegistry()
    builder = _builder(tmp_path, monkeypatch, registry, tags=["latest"])
    builder.build()
    out = tmp_path / "out"

    layout = json.loads((out / "index.json").read_text())
    assert [m["mediaType"] for m in layout["manifests"]] == [INDEX_TYPE, INDEX_TYPE]
    assert {m["annotations"]["org.opencontainers.image.ref.name"] for m in layout["manifests"]} == {
        "registry.example/team/app:v1",
        "registry.example/team/app:latest",
    }
    assert {m["digest"] for m in layout["manifests"]} == {builder.index_digest}
    for tag in ("v1", "latest"):
        assert (out / "refs" / "tags" / tag).read_text() == builder.index_digest

    index = json.loads(_blob(out, builder.index_digest))
    assert index["mediaType"] == INDEX_TYPE
    assert [m["platform"] for m in index["manifests"]] == [
        {"architecture": "amd64", "os": "linux"},
        {"architecture": "arm64", "os": "linux"},
    ]
    app_layers = set()
    for descriptor in index["manifests"]:
        manifest = json.loads(_blob(out, descriptor["digest"]))
        config = json.loads(_blob(out, manifest["config"]["digest"]))
        assert config["architecture"] == descriptor["platform"]["architecture"]
        assert len(config["rootfs"]["diff_ids"]) == len(manifest["layers"])
        assert config["rootfs"]["diff_ids"][-1] == manifest["layers"][-1]["digest"]
        app_layers.add(manifest["layers"][-1]["digest"])
    assert len(app_layers) == 1

    # The second platform re-selects from the exact base index snapshot the first
    # one resolved, without another top-level manifest request.
    top_level = [call for call in registry.manifest_calls if call[0] in ("3.11-slim", registry.index_digest)]
    assert top_level == [("3.11-slim", {"expected_digest": None})]


def test_multi_platform_annotations_preserve_refs_digests_and_runtime(
    tmp_path, monkeypatch
):
    bases = {
        arch: {
            "Entrypoint": [f"{arch}-entrypoint"],
            "StopSignal": signal,
            "Volumes": {f"/{arch}": {}},
            "Labels": {"base": arch},
        }
        for arch, signal in (("amd64", "SIGQUIT"), ("arm64", "SIGINT"), ("arm", "SIGTERM"))
    }
    builder = _builder(
        tmp_path, monkeypatch,
        FakeBaseRegistry(platforms=("amd64", "arm64", "arm/v7"), base_runtime_by_arch=bases),
        platform="linux/amd64,linux/arm64,linux/arm/v7",
        tags=["stable"], cmd=["python", "app.py"],
        labels={"example.label": "explicit"},
        manifest_annotations={"example.manifest": "manifest-value"},
        index_annotations={"example.index": "index-value"},
    )
    builder.build()
    out = tmp_path / "out"
    layout = json.loads((out / "index.json").read_bytes())
    assert layout["annotations"] == {
        "example.index": "index-value",
        "org.opencontainers.image.ref.name": "registry.example/team/app:v1",
    }
    assert {
        descriptor["annotations"]["org.opencontainers.image.ref.name"]
        for descriptor in layout["manifests"]
    } == {"registry.example/team/app:v1", "registry.example/team/app:stable"}
    index_bytes = _blob(out, builder.index_digest)
    assert _digest(index_bytes) == builder.index_digest
    for descriptor in layout["manifests"]:
        assert descriptor["digest"] == builder.index_digest
        assert descriptor["size"] == len(index_bytes)
    index = json.loads(index_bytes)
    assert index["annotations"] == {"example.index": "index-value"}
    for descriptor in index["manifests"]:
        arch = descriptor["platform"]["architecture"]
        manifest_bytes = _blob(out, descriptor["digest"])
        assert _digest(manifest_bytes) == descriptor["digest"]
        assert len(manifest_bytes) == descriptor["size"]
        manifest = json.loads(manifest_bytes)
        assert manifest["annotations"] == {"example.manifest": "manifest-value"}
        config_descriptor = manifest["config"]
        config_bytes = _blob(out, config_descriptor["digest"])
        assert _digest(config_bytes) == config_descriptor["digest"]
        assert len(config_bytes) == config_descriptor["size"]
        image_config = json.loads(config_bytes)
        if arch == "arm":
            assert descriptor["platform"]["variant"] == image_config["variant"] == "v7"
        config = image_config["config"]
        assert config["Labels"] == {"base": arch, "example.label": "explicit"}
        assert config["Entrypoint"] == [f"{arch}-entrypoint"]
        assert config["Cmd"] == ["python", "app.py"]
        assert config["StopSignal"] == bases[arch]["StopSignal"]
        assert config["Volumes"] == bases[arch]["Volumes"]

    client = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", client)
    builder.push(show_progress=False)
    assert [call for call in client.calls if call[0] == "index"] == [
        ("index", builder.index_digest, builder.index_digest),
        ("index", "v1", builder.index_digest),
        ("index", "stable", builder.index_digest),
    ]


def test_multi_platform_annotation_changes_recompute_only_dependent_digests(
    tmp_path, monkeypatch
):
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(),
        manifest_annotations={"example.z": "two", "example.manifest": "one"},
        index_annotations={"example.z": "two", "example.index": "one"},
    )
    builder.build()
    original_index = builder.index_digest
    original_manifests = [image.manifest_digest for image in builder.platform_images]
    original_configs = [image.config_digest for image in builder.platform_images]

    builder.config.manifest_annotations = dict(reversed(list(builder.config.manifest_annotations.items())))
    builder.config.index_annotations = dict(reversed(list(builder.config.index_annotations.items())))
    builder.build()
    assert builder.index_digest == original_index
    assert [image.manifest_digest for image in builder.platform_images] == original_manifests

    builder.config.index_annotations["example.index"] = "two"
    builder.build()
    assert builder.index_digest != original_index
    assert [image.manifest_digest for image in builder.platform_images] == original_manifests
    index_with_new_annotation = builder.index_digest

    builder.config.manifest_annotations["example.manifest"] = "two"
    builder.build()
    assert builder.index_digest != index_with_new_annotation
    assert all(
        image.manifest_digest != previous
        for image, previous in zip(builder.platform_images, original_manifests)
    )
    assert [image.config_digest for image in builder.platform_images] == original_configs


@pytest.mark.parametrize("release_key", ["example.notes", "framework"])
def test_multi_platform_release_notes_respect_label_precedence(
    tmp_path, monkeypatch, release_key
):
    ctx = _context(tmp_path)
    (ctx / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (ctx / "notes.md").write_bytes(b"release notes\r\n")
    bases = {
        arch: {"Labels": {release_key: f"base-{arch}", "shared": "base"}}
        for arch in ("amd64", "arm64", "arm")
    }
    builder = _builder(
        tmp_path, monkeypatch,
        FakeBaseRegistry(platforms=("amd64", "arm64", "arm/v7"), base_runtime_by_arch=bases),
        ctx=ctx, platform="linux/amd64,linux/arm64,linux/arm/v7",
        release_notes_file="notes.md", release_notes_label=release_key,
        labels={"shared": "explicit"},
        manifest_annotations={"example.manifest": "one"},
        index_annotations={"example.index": "one"},
    )
    builder.build()
    for image in builder.platform_images:
        image_config = json.loads(_blob(tmp_path / "out", image.config_digest))
        if image.platform["architecture"] == "arm":
            assert image.platform["variant"] == image_config["variant"] == "v7"
        config = image_config["config"]
        assert config["Labels"][release_key] == "release notes\n"
        assert config["Labels"]["shared"] == "explicit"


@pytest.mark.parametrize("release_key", ["example.notes", "framework"])
def test_multi_platform_release_notes_reject_explicit_label_collisions_before_pull(
    tmp_path, monkeypatch, release_key
):
    from pycontainer.release_notes import ReleaseNotesError

    ctx = _context(tmp_path)
    (ctx / "notes.md").write_text("notes")
    registry = FakeBaseRegistry()
    with pytest.raises(ReleaseNotesError, match="conflict"):
        _builder(
            tmp_path, monkeypatch, registry, ctx=ctx,
            release_notes_file="notes.md", release_notes_label=release_key,
            labels={release_key: "explicit"},
        )
    assert registry.manifest_calls == []
    assert not (tmp_path / "out" / "index.json").exists()


@pytest.mark.parametrize("override", [False, True])
def test_multi_platform_runtime_config_inherits_and_overrides_per_base(
    tmp_path, monkeypatch, override
):
    bases = {
        "amd64": {"StopSignal": "SIGQUIT", "Volumes": {"/amd64": {}}},
        "arm64": {"StopSignal": "SIGINT", "Volumes": {"/arm64": {}}},
        "arm": {"StopSignal": "SIGTERM", "Volumes": {"/arm": {}}},
    }
    options = {"stop_signal": "SIGTERM", "volumes": ["/shared"]} if override else {}
    builder = _builder(
        tmp_path, monkeypatch,
        FakeBaseRegistry(platforms=("amd64", "arm64", "arm/v7"), base_runtime_by_arch=bases),
        platform="linux/amd64,linux/arm64,linux/arm/v7",
        **options,
    )
    builder.build()
    out = tmp_path / "out"
    index = json.loads(_blob(out, builder.index_digest))

    for descriptor in index["manifests"]:
        arch = descriptor["platform"]["architecture"]
        manifest_bytes = _blob(out, descriptor["digest"])
        assert _digest(manifest_bytes) == descriptor["digest"]
        assert len(manifest_bytes) == descriptor["size"]
        manifest = json.loads(manifest_bytes)
        config_descriptor = manifest["config"]
        config_bytes = _blob(out, config_descriptor["digest"])
        assert _digest(config_bytes) == config_descriptor["digest"]
        assert len(config_bytes) == config_descriptor["size"]
        image_config = json.loads(config_bytes)
        if arch == "arm":
            assert descriptor["platform"]["variant"] == image_config["variant"] == "v7"
        config = image_config["config"]
        assert config["StopSignal"] == ("SIGTERM" if override else bases[arch]["StopSignal"])
        volumes = {f"/{arch}": {}}
        if override:
            volumes["/shared"] = {}
        assert config["Volumes"] == volumes


def test_repeated_reproducible_builds_have_same_index_digest(tmp_path, monkeypatch):
    registry = FakeBaseRegistry()
    first = _builder(tmp_path, monkeypatch, registry)
    first.build()
    second = ImageBuilder(first.config)
    second.build()
    assert second.index_digest == first.index_digest


def test_multi_platform_history_records_each_base_and_one_shared_app(tmp_path, monkeypatch):
    from pycontainer.history import BuildHistory, BuildTiming

    phases = []
    original_phase = BuildTiming.phase

    def record_phase(self, name):
        phases.append(name)
        return original_phase(self, name)

    monkeypatch.setattr(BuildTiming, "phase", record_phase)
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(), record_history=True,
        history_dir=str(tmp_path / "history"),
    )
    phases.clear()
    builder.build()

    assert phases == ["base_image", "application", "base_image"]
    record = BuildHistory(tmp_path / "history").list()[0]
    assert record["status"] == "success"
    assert set(record["phases_ns"]) == {"base_image", "application"}


def test_dependency_layers_are_isolated_per_platform(tmp_path, monkeypatch):
    registry = FakeBaseRegistry()
    installs = []

    def fake_install(requirements, site_packages, ctx, os_name, arch, version, base_image, base_config, **kwargs):
        installs.append((os_name, arch, base_config["architecture"]))
        package = site_packages / f"native_{arch}"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"ARCH = {arch!r}\n")
        return []

    monkeypatch.setattr(builder_module, "install_requirements", fake_install)
    builder = _builder(
        tmp_path, monkeypatch, registry, include_deps=True, ctx=_context(tmp_path, requirements=True)
    )
    builder.build()

    assert installs == [("linux", "amd64", "amd64"), ("linux", "arm64", "arm64")]
    amd64, arm64 = builder.platform_images
    assert amd64.layers[1].digest != arm64.layers[1].digest
    assert amd64.layers[-1].digest == arm64.layers[-1].digest
    for image in builder.platform_images:
        arch = image.platform["architecture"]
        with tarfile.open(image.layers[1].tar_path) as tar:
            names = tar.getnames()
        assert any(f"native_{arch}" in name for name in names)
        assert not any("native_" in name and arch not in name for name in names)


def test_multi_platform_pull_and_push_use_registry_transport_settings(tmp_path, monkeypatch):
    registry = FakeBaseRegistry()
    pull_client = registry.client
    clients = []

    def record_pull(host, repo, **kwargs):
        clients.append((host, repo, kwargs["insecure"], kwargs["ca_file"]))
        return pull_client(host, repo, **kwargs)

    registry.client = record_pull
    monkeypatch.setattr(
        "pycontainer.registry_client.create_registry_ssl_context", lambda _path: None
    )
    builder = _builder(
        tmp_path, monkeypatch, registry,
        insecure_registries=["registry.example"],
        registry_ca_file="synthetic-ca.pem",
    )
    builder.build()
    push_client = FakePushClient()

    def record_push(host, repo, **kwargs):
        clients.append((host, repo, kwargs["insecure"], kwargs["ca_file"]))
        return push_client(host, repo, **kwargs)

    monkeypatch.setattr(builder_module, "RegistryClient", record_push)
    builder.push(show_progress=False)

    assert clients == [
        ("registry.example", "library/python", True, "synthetic-ca.pem"),
        ("registry.example", "library/python", True, "synthetic-ca.pem"),
        ("registry.example", "team/app", True, "synthetic-ca.pem"),
    ]
    assert any(call[0] == "index" and call[1] == "v1" for call in push_client.calls)


def test_multi_platform_rejects_shipped_credentials_before_cache_and_publication(
    tmp_path, monkeypatch
):
    ctx = _context(tmp_path, requirements=True)
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(), ctx=ctx,
        use_cache=True, cache_dir=str(tmp_path / "cache"),
    )
    builder.build()
    out = tmp_path / "out"
    previous_index = (out / "index.json").read_bytes()
    previous_tag = (out / "refs" / "tags" / "v1").read_bytes()
    (ctx / "requirements.txt").write_text(
        "--index-url https://synthetic-user:synthetic-secret@example.invalid/simple\n"
    )

    def unexpected_cache_lookup(*args, **kwargs):
        pytest.fail("credential check must run before cache lookup")

    monkeypatch.setattr(builder.cache, "get_layer", unexpected_cache_lookup)
    with pytest.raises(ValueError, match="literal credentials") as error:
        builder.build()

    assert "synthetic-secret" not in str(error.value)
    assert (out / "index.json").read_bytes() == previous_index
    assert (out / "refs" / "tags" / "v1").read_bytes() == previous_tag


def test_multi_platform_uses_requirements_when_virtualenv_is_excluded(tmp_path, monkeypatch):
    ctx = _context(tmp_path, requirements=True)
    site = ctx / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "demo.py").write_text("X = 1\n")
    installs = []

    def fake_install(requirements, site_packages, context, os_name, arch, version, base_image, base_config, **kwargs):
        installs.append(arch)
        return []

    monkeypatch.setattr(builder_module, "install_requirements", fake_install)
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(), include_deps=True,
        ctx=ctx, exclude_paths=[".venv"],
    )
    builder.build()
    assert installs == ["amd64", "arm64"]


def test_multi_platform_rejects_conda_only_before_pulling_or_publication(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    (ctx / "environment.yml").write_text("name: example\n")
    out = tmp_path / "out"
    refs = out / "refs" / "tags"
    refs.mkdir(parents=True)
    (out / "index.json").write_text("previous index")
    (refs / "v1").write_text("previous tag")
    registry = FakeBaseRegistry()
    builder = _builder(tmp_path, monkeypatch, registry, include_deps=True, ctx=ctx)

    with pytest.raises(ValueError, match="Conda staging is unsupported"):
        builder.build()

    assert registry.manifest_calls == []
    assert (out / "index.json").read_text() == "previous index"
    assert (refs / "v1").read_text() == "previous tag"


def test_multi_platform_conda_with_requirements_stages_each_target(tmp_path, monkeypatch):
    ctx = _context(tmp_path, requirements=True)
    (ctx / "environment.yml").write_text("name: example\n")
    site = ctx / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "demo.py").write_text("X = 1\n")
    installs = []

    def fake_install(requirements, site_packages, context, os_name, arch, version,
                     base_image, base_config, **kwargs):
        installs.append(arch)
        return []

    monkeypatch.setattr(builder_module, "install_requirements", fake_install)
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry(), include_deps=True, ctx=ctx)
    builder.build()
    assert installs == ["amd64", "arm64"]


@pytest.mark.parametrize("include_target_dependency", [False, True])
@pytest.mark.parametrize(
    ("target", "machine"),
    [("arm64", "aarch64"), ("arm/v7", "armv7l")],
)
def test_multi_platform_virtualenv_validates_each_target_base(
    tmp_path, monkeypatch, include_target_dependency, target, machine
):
    ctx = _context(tmp_path)
    site = ctx / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)

    def add_distribution(name, requirement=""):
        module = name.replace("-", "_")
        (site / f"{module}.py").write_text("value = 1\n")
        info = site / f"{module}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
            f"{requirement}\n"
        )
        (info / "WHEEL").write_text(
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )

    add_distribution(
        "app-lib",
        f'Requires-Dist: arm-only; platform_machine == "{machine}" and python_full_version >= "3.11.9"\n',
    )
    if include_target_dependency:
        add_distribution("arm-only")

    envs = {
        arch: ["PYTHON_IMPLEMENTATION=CPython", f"PYTHON_VERSION=3.11.{patch}", "PYTHON_LIBC=glibc-2.17"]
        for arch, patch in (("amd64", 8), (target.split("/")[0], 9))
    }
    out = tmp_path / "out"
    refs = out / "refs" / "tags"
    refs.mkdir(parents=True)
    (out / "index.json").write_text("previous index")
    (refs / "v1").write_text("previous tag")
    builder = _builder(
        tmp_path, monkeypatch,
        FakeBaseRegistry(platforms=("amd64", target), base_env_by_arch=envs),
        include_deps=True, ctx=ctx, platform=f"linux/amd64,linux/{target}",
    )

    if include_target_dependency:
        builder.build()
        assert [image.platform["architecture"] for image in builder.platform_images] == [
            "amd64", target.split("/")[0]
        ]
        if target == "arm/v7":
            assert builder.platform_images[1].platform["variant"] == "v7"
    else:
        with pytest.raises(ValueError, match=rf"linux/{target}.*arm-only.*not installed"):
            builder.build()
        assert (out / "index.json").read_text() == "previous index"
        assert (refs / "v1").read_text() == "previous tag"


def test_missing_platform_in_base_fails_without_replacing_existing_tags(tmp_path, monkeypatch):
    out = tmp_path / "out"
    (out / "refs" / "tags").mkdir(parents=True)
    (out / "index.json").write_text("previous")
    (out / "refs" / "tags" / "v1").write_text("previous")
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry(platforms=("amd64",)))

    with pytest.raises(UnsupportedPlatformError):
        builder.build()
    assert (out / "index.json").read_text() == "previous"
    assert (out / "refs" / "tags" / "v1").read_text() == "previous"
    assert not hasattr(builder, "index_digest")


def test_compressed_base_diff_id_mismatch_fails_before_publication(tmp_path, monkeypatch):
    out = tmp_path / "out"
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry(wrong_diff_id_arch="arm64"))

    with pytest.raises(LayerIntegrityError, match="linux/arm64.*DiffID"):
        builder.build()
    assert not (out / "index.json").exists()
    assert not any(path.is_file() for path in (out / "refs").rglob("*"))
    assert not hasattr(builder, "index_digest")


THREE = "linux/amd64,linux/arm64,linux/arm/v7"
SNAPSHOT_ENV = ["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.9", "PYTHON_LIBC=glibc-2.17"]


def _sha512_header(body):
    return "sha512:" + hashlib.sha512(body).hexdigest()


def _moving_tag_registry(monkeypatch, tag_header=_sha512_header, missing=()):
    """Serve the real RegistryClient a tag that moves from snapshot A to B after one read."""
    first, second = (
        FakeBaseRegistry(platforms=("amd64", "arm64", "arm/v7"), base_env=SNAPSHOT_ENV + [f"SNAPSHOT={name}"])
        for name in ("A", "B")
    )
    requests = []

    def make_request(client, method, url, *args, **kwargs):
        assert method == "GET" and "/manifests/" in url
        reference = url.rsplit("/", 1)[1]
        requests.append(reference)
        if reference == "3.11-slim":
            body = (first if requests.count(reference) == 1 else second).manifests[reference]
            return 200, body, {"Docker-Content-Digest": tag_header(body)}
        body = first.manifests.get(reference) or second.manifests.get(reference)
        if body is None or reference in missing:
            return 404, b"", {}
        return 200, body, {"Docker-Content-Digest": _digest(body)}

    def open_blob(client, url, retry_auth=True):
        data = {**first.blobs, **second.blobs}[url.rsplit("/", 1)[1]]
        response = io.BytesIO(data)
        response.status = 200
        response.headers = {"Content-Length": str(len(data))}
        return response

    monkeypatch.setattr(RegistryClient, "_make_request", make_request)
    monkeypatch.setattr(RegistryClient, "_open_blob_response", open_blob)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *a, **k: RegistryAuth())
    return first, requests


def _real_client_builder(tmp_path):
    return ImageBuilder(BuildConfig(
        tag="registry.example/team/app:v1",
        base_image="registry.example/library/python:3.11-slim",
        context_dir=str(_context(tmp_path)), output_dir=str(tmp_path / "out"),
        platform=THREE, include_deps=False, use_cache=False, reproducible=True,
    ))


@pytest.mark.parametrize(("tag_header", "verified"), [
    (_sha512_header, True),
    (lambda body: "blake3:" + "0" * 64, False),
])
def test_moving_tag_with_non_sha256_digest_binds_every_platform_to_first_snapshot(
    tmp_path, monkeypatch, tag_header, verified
):
    first, requests = _moving_tag_registry(monkeypatch, tag_header)
    builder = _real_client_builder(tmp_path)
    builder.build()

    assert requests.count("3.11-slim") == 1
    index = json.loads(_blob(tmp_path / "out", builder.index_digest))
    base_manifests = json.loads(first.manifests["3.11-slim"])["manifests"]
    assert [d["platform"] for d in index["manifests"]] == [d["platform"] for d in base_manifests]
    for descriptor, base in zip(index["manifests"], base_manifests):
        manifest = json.loads(_blob(tmp_path / "out", descriptor["digest"]))
        config = json.loads(_blob(tmp_path / "out", manifest["config"]["digest"]))
        assert "SNAPSHOT=A" in config["config"]["Env"]
        assert manifest["layers"][:-1] == json.loads(first.manifests[base["digest"]])["layers"]
    assert builder._resolved_base_from_tag is verified
    assert builder._base_image_tag_verified is verified


def test_forged_sha512_tag_digest_fails_before_publication(tmp_path, monkeypatch):
    _, requests = _moving_tag_registry(
        monkeypatch, lambda body: _sha512_header(body + b" ")
    )
    builder = _real_client_builder(tmp_path)

    with pytest.raises(BlobIntegrityError, match="digest does not match"):
        builder.build()
    out = tmp_path / "out"
    assert requests == ["3.11-slim"]
    assert not (out / "index.json").exists() and not (out / "oci-layout").exists()
    assert not any(path.is_file() for path in (out / "refs").rglob("*"))


def test_unavailable_pinned_platform_manifest_fails_without_rereading_tag(tmp_path, monkeypatch):
    first = FakeBaseRegistry(platforms=("amd64", "arm64", "arm/v7"), base_env=SNAPSHOT_ENV + ["SNAPSHOT=A"])
    arm64 = json.loads(first.manifests["3.11-slim"])["manifests"][1]["digest"]
    _, requests = _moving_tag_registry(monkeypatch, missing=(arm64,))
    builder = _real_client_builder(tmp_path)

    with pytest.raises(RegistryRequestError, match="404"):
        builder.build()
    out = tmp_path / "out"
    assert requests.count("3.11-slim") == 1 and requests[-1] == arm64
    assert not (out / "index.json").exists() and not (out / "oci-layout").exists()
    assert not any(path.is_file() for path in (out / "refs").rglob("*"))


def _select_layout_reference(layout, reference):
    """Resolve a reference the way OCI image-layout tools do: exact ref.name match."""
    matches = [
        m for m in layout["manifests"]
        if m.get("annotations", {}).get("org.opencontainers.image.ref.name") == reference
    ]
    assert len(matches) == 1, f"{reference!r} matched {len(matches)} descriptors"
    return matches[0]


def test_every_configured_tag_is_selectable_in_index_layout(tmp_path, monkeypatch):
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(),
        tags=["stable", "registry.example/team/app:prod"],
    )
    builder.build()
    out = tmp_path / "out"
    layout = json.loads((out / "index.json").read_text())
    references = builder.config.all_tags()

    assert len(references) == 3
    assert [
        m["annotations"]["org.opencontainers.image.ref.name"] for m in layout["manifests"]
    ] == references
    for reference in references:
        descriptor = _select_layout_reference(layout, reference)
        assert descriptor["mediaType"] == INDEX_TYPE
        assert descriptor["digest"] == builder.index_digest
        assert descriptor["size"] == builder.index_size
        tag = reference.rsplit(":", 1)[1]
        assert (out / "refs" / "tags" / tag).read_text() == builder.index_digest


def test_multi_platform_uses_collision_safe_local_reference_names(tmp_path, monkeypatch):
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(), tag="myapp:latest",
        tags=["_dev", "v1_", "v1..2"],
    )
    builder.build()
    out = tmp_path / "out"
    layout = json.loads((out / "index.json").read_text())
    names = ["myapp:latest"] + [
        "pycontainer-" + f"myapp:{tag}".encode().hex()
        for tag in ("_dev", "v1_", "v1..2")
    ]
    assert [item["annotations"]["org.opencontainers.image.ref.name"] for item in layout["manifests"]] == names
    for name in names:
        assert _select_layout_reference(layout, name)["digest"] == builder.index_digest

    client = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", client)
    builder.push(show_progress=False)
    assert builder.pushed_refs == builder.config.all_tags()


@pytest.mark.skipif(shutil.which("skopeo") is None, reason="Skopeo is not installed")
def test_skopeo_selects_every_index_layout_tag(tmp_path, monkeypatch):
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(), tag="myapp:latest",
        tags=["stable", "_dev", "v1_", "v1..2"],
    )
    builder.build()
    layout = json.loads((tmp_path / "out" / "index.json").read_text())
    for descriptor in layout["manifests"]:
        reference = descriptor["annotations"]["org.opencontainers.image.ref.name"]
        result = subprocess.run(
            ["skopeo", "inspect", "--raw", f"oci:{tmp_path / 'out'}:{reference}"],
            check=True, capture_output=True, timeout=30,
        )
        assert _digest(result.stdout) == builder.index_digest


def test_push_order_is_blobs_manifests_index_then_tags(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry(), tags=["latest"])
    builder.build()
    client = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", client)

    builder.push(show_progress=False)

    kinds = [call[0] for call in client.calls]
    blob_count = kinds.count("blob")
    assert kinds == ["blob"] * blob_count + ["manifest", "manifest", "index", "index", "index"]
    assert [call[1] for call in client.calls if call[0] == "manifest"] == [
        image.manifest_digest for image in builder.platform_images
    ]
    assert [call[1] for call in client.calls if call[0] == "index"] == [builder.index_digest, "v1", "latest"]
    assert all(call[2] == builder.index_digest for call in client.calls if call[0] == "index")
    assert builder.pushed_refs == ["registry.example/team/app:v1", "registry.example/team/app:latest"]


def test_failed_index_push_publishes_no_tags(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry())
    builder.build()
    client = FakePushClient(fail_on=lambda call: call[0] == "index")
    monkeypatch.setattr(builder_module, "RegistryClient", client)

    with pytest.raises(RuntimeError, match="simulated"):
        builder.push(show_progress=False)
    assert not any(call[0] == "index" for call in client.calls)
    assert builder.pushed_refs == []


@pytest.mark.parametrize(
    ("failure_tag", "timed_out"),
    [("v2", False), ("stable", False), ("stable", True), ("latest", False)],
)
def test_partial_index_tag_push_reports_all_outcomes_without_progress(
    tmp_path, monkeypatch, capsys, failure_tag, timed_out
):
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(), tags=["stable", "latest"]
    )
    builder.build()
    capsys.readouterr()

    class PartialPushClient(FakePushClient):
        def push_index(self, reference, data):
            if reference == failure_tag:
                if timed_out:
                    super().push_index(reference, data)
                    raise TimeoutError("response lost after publication")
                raise RegistryRequestError("registry rejected tag")
            super().push_index(reference, data)

    client = PartialPushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", client)

    with pytest.raises(RegistryRequestError) as raised:
        builder.push(registry_url="mirror.example/team/app:v2", show_progress=False)

    tags = ["v2", "stable", "latest"]
    failure_index = tags.index(failure_tag)
    refs = [f"mirror.example/team/app:{tag}" for tag in tags]
    assert builder.pushed_refs == refs[:failure_index]
    message = str(raised.value)
    assert f"Confirmed pushed: {', '.join(refs[:failure_index]) or '(none)'}." in message
    assert f"Failed tag write (outcome unknown): {refs[failure_index]}." in message
    assert f"Not attempted: {', '.join(refs[failure_index + 1:]) or '(none)'}." in message
    assert str(raised.value.__cause__) in message
    assert isinstance(
        raised.value.__cause__, TimeoutError if timed_out else RegistryRequestError
    )
    calls = [call[1] for call in client.calls if call[0] == "index"]
    assert calls == [builder.index_digest] + tags[:failure_index] + (
        [failure_tag] if timed_out else []
    )
    assert not capsys.readouterr().out


def test_index_push_clears_previous_successes_before_a_new_attempt(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry(), tags=["latest"])
    builder.build()
    monkeypatch.setattr(builder_module, "RegistryClient", FakePushClient())
    builder.push(show_progress=False)
    assert len(builder.pushed_refs) == 2

    client = FakePushClient(fail_on=lambda call: call[0] == "index")
    monkeypatch.setattr(builder_module, "RegistryClient", client)
    with pytest.raises(RuntimeError, match="simulated"):
        builder.push(show_progress=False)
    assert builder.pushed_refs == []


def test_push_rejects_layout_mutated_after_build(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry())
    builder.build()
    arm64 = builder.platform_images[1]
    config_path = tmp_path / "out" / "blobs" / "sha256" / arm64.config_digest.split(":", 1)[1]
    config_path.write_bytes(config_path.read_bytes().replace(b'"arm64"', b'"amd64"'))
    client = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", client)

    with pytest.raises(LayerIntegrityError):
        builder.push(show_progress=False)
    assert client.calls == []


ARM_SET = ("amd64", "arm64", "arm/v5", "arm/v6", "arm/v7")
WITH_ARM = "linux/amd64,linux/arm64,linux/arm/v7"


def test_arm_v7_index_selects_exact_variant_and_round_trips_it(tmp_path, monkeypatch):
    registry = FakeBaseRegistry(platforms=ARM_SET)
    installs = []

    def fake_install(requirements, site_packages, ctx, os_name, arch, version, base_image, base_config, **kwargs):
        installs.append((arch, kwargs.get("variant"), base_config.get("variant")))
        (site_packages / f"native_{arch}").mkdir(parents=True)
        return []

    monkeypatch.setattr(builder_module, "install_requirements", fake_install)
    builder = _builder(
        tmp_path, monkeypatch, registry, platform=WITH_ARM, include_deps=True,
        ctx=_context(tmp_path, requirements=True),
    )
    builder.build()
    out = tmp_path / "out"

    assert installs == [("amd64", None, None), ("arm64", None, None), ("arm", "v7", "v7")]
    index = json.loads(_blob(out, builder.index_digest))
    assert [m["platform"] for m in index["manifests"]] == [
        {"architecture": "amd64", "os": "linux"},
        {"architecture": "arm64", "os": "linux"},
        {"architecture": "arm", "os": "linux", "variant": "v7"},
    ]
    arm_layer, _ = _base_layer("arm-v7")
    descriptor = index["manifests"][2]
    manifest = json.loads(_blob(out, descriptor["digest"]))
    config = json.loads(_blob(out, manifest["config"]["digest"]))
    assert (config["os"], config["architecture"], config["variant"]) == ("linux", "arm", "v7")
    assert manifest["layers"][0]["digest"] == _digest(arm_layer)
    assert {m["layers"][-1]["digest"] for m in (
        json.loads(_blob(out, d["digest"])) for d in index["manifests"]
    )} == {builder.platform_images[0].layers[-1].digest}
    top_level = [call[0] for call in registry.manifest_calls if call[0] in ("3.11-slim", registry.index_digest)]
    assert top_level == ["3.11-slim"]


def test_arm_v7_base_config_variant_conflict_fails_before_publication(tmp_path, monkeypatch):
    registry = FakeBaseRegistry(platforms=ARM_SET, config_variants={"arm/v7": "v6"})
    builder = _builder(tmp_path, monkeypatch, registry, platform=WITH_ARM)

    with pytest.raises(UnsupportedPlatformError, match="does not match index variant"):
        builder.build()
    assert not (tmp_path / "out" / "index.json").exists()


def test_arm_v7_missing_from_base_fails_without_fallback(tmp_path, monkeypatch):
    registry = FakeBaseRegistry(platforms=("amd64", "arm64", "arm/v6"))
    builder = _builder(tmp_path, monkeypatch, registry, platform=WITH_ARM)

    with pytest.raises(UnsupportedPlatformError, match="no exact manifest for linux/arm/v7"):
        builder.build()
    assert not (tmp_path / "out" / "index.json").exists()


def test_arm_without_v7_variant_rejects_dependency_staging_before_pull(tmp_path, monkeypatch):
    registry = FakeBaseRegistry(platforms=("arm/v6",))
    builder = _builder(
        tmp_path, monkeypatch, registry, platform="linux/arm/v6", include_deps=True,
        ctx=_context(tmp_path, requirements=True),
    )

    with pytest.raises(ValueError, match="linux/arm/v6; supported targets"):
        builder.build()
    assert registry.manifest_calls == []


def test_push_rejects_arm_v7_config_variant_mutated_after_build(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry(platforms=ARM_SET), platform=WITH_ARM)
    builder.build()
    arm = builder.platform_images[2]
    config_path = tmp_path / "out" / "blobs" / "sha256" / arm.config_digest.split(":", 1)[1]
    config_path.write_bytes(config_path.read_bytes().replace(b'"variant":"v7"', b'"variant":"v6"'))
    client = FakePushClient()
    monkeypatch.setattr(builder_module, "RegistryClient", client)

    with pytest.raises(LayerIntegrityError):
        builder.push(show_progress=False)
    assert client.calls == []
