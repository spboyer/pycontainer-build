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
from pycontainer.registry_client import RegistryRequestError

INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
MULTI = "linux/amd64,linux/arm64"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _base_layer(arch: str):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        payload = f"base for {arch}".encode()
        info = tarfile.TarInfo(f"etc/{arch}")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    uncompressed = raw.getvalue()
    return gzip.compress(uncompressed, mtime=0), _digest(uncompressed)


class FakeBaseRegistry:
    """Serves a two-platform base index; records pulls for assertions."""

    def __init__(self, platforms=("amd64", "arm64"), wrong_diff_id_arch=None, config_variants=None):
        self.blobs = {}
        self.manifests = {}
        descriptors = []
        for entry in platforms:
            arch, _, variant = entry.partition("/")
            layer, diff_id = _base_layer(entry.replace("/", "-"))
            if entry == wrong_diff_id_arch:
                diff_id = _digest(b"not the layer content")
            platform = {"os": "linux", "architecture": arch}
            if variant:
                platform["variant"] = variant
            # Real configs may omit the variant; the index descriptor is authoritative.
            config_variant = (config_variants or {}).get(entry)
            config = json.dumps({
                "os": "linux",
                "architecture": arch,
                **({"variant": config_variant} if config_variant else {}),
                "config": {"Env": ["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.9", "PYTHON_LIBC=glibc"], "WorkingDir": "/"},
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
    registry = FakeBaseRegistry()
    seen = _record_tag_provenance(monkeypatch)
    builder = _builder(
        tmp_path, monkeypatch, registry, include_deps=True,
        base_image=reference.format(digest=registry.index_digest),
    )
    builder.build()

    top_level = [ref for ref, _ in registry.manifest_calls if ref in ("3.11-slim", registry.index_digest)]
    assert top_level[1] == registry.index_digest
    assert seen == [("amd64", verified), ("arm64", verified)]


def test_normalize_platforms_strips_and_dedupes():
    assert normalize_platforms(" linux/amd64 , linux/arm64,linux/amd64") == [
        "linux/amd64",
        "linux/arm64",
    ]
    with pytest.raises(ValueError):
        normalize_platforms("linux/amd64,,linux/arm64")


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

    # The second platform re-selects from the exact base index the first one resolved.
    top_level = [call for call in registry.manifest_calls if call[0] in ("3.11-slim", registry.index_digest)]
    assert top_level == [
        ("3.11-slim", {"expected_digest": None}),
        (registry.index_digest, {"expected_digest": registry.index_digest}),
    ]


def test_repeated_reproducible_builds_have_same_index_digest(tmp_path, monkeypatch):
    registry = FakeBaseRegistry()
    first = _builder(tmp_path, monkeypatch, registry)
    first.build()
    second = ImageBuilder(first.config)
    second.build()
    assert second.index_digest == first.index_digest


def test_dependency_layers_are_isolated_per_platform(tmp_path, monkeypatch):
    registry = FakeBaseRegistry()
    installs = []

    def fake_install(requirements, site_packages, ctx, os_name, arch, version, base_image, base_config, variant=None):
        installs.append((os_name, arch, base_config["architecture"]))
        package = site_packages / f"native_{arch}"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"ARCH = {arch!r}\n")

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


@pytest.mark.parametrize("platform", [MULTI, "linux/amd64,linux/arm64,linux/arm/v7"])
@pytest.mark.parametrize("requirements", [False, True])
def test_multi_platform_rejects_virtualenv_staging_before_publication(
    tmp_path, monkeypatch, requirements, platform
):
    ctx = _context(tmp_path, requirements=requirements)
    site = ctx / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "demo.py").write_text("X = 1\n")
    staged = []
    monkeypatch.setattr(builder_module, "stage_virtualenv", lambda *a, **k: staged.append(a))
    out = tmp_path / "out"
    (out / "refs" / "tags").mkdir(parents=True)
    (out / "index.json").write_text("previous")
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(platforms=("amd64", "arm64", "arm/v7")),
        include_deps=True, ctx=ctx, platform=platform,
    )

    with pytest.raises(ValueError, match=r"--exclude.*issues/180"):
        builder.build()
    assert staged == []
    assert (out / "index.json").read_text() == "previous"
    assert not (out / "refs" / "tags" / "v1").exists()


def test_multi_platform_uses_requirements_when_virtualenv_is_excluded(tmp_path, monkeypatch):
    ctx = _context(tmp_path, requirements=True)
    site = ctx / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "demo.py").write_text("X = 1\n")
    installs = []

    def fake_install(requirements, site_packages, context, os_name, arch, version, base_image, base_config, variant=None):
        installs.append(arch)

    monkeypatch.setattr(builder_module, "install_requirements", fake_install)
    builder = _builder(
        tmp_path, monkeypatch, FakeBaseRegistry(), include_deps=True,
        ctx=ctx, exclude_paths=[".venv"],
    )
    builder.build()
    assert installs == ["amd64", "arm64"]


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


@pytest.mark.skipif(shutil.which("skopeo") is None, reason="Skopeo is not installed")
def test_skopeo_selects_every_index_layout_tag(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch, FakeBaseRegistry(), tags=["stable"])
    builder.build()
    for reference in builder.config.all_tags():
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

    def fake_install(requirements, site_packages, ctx, os_name, arch, version, base_image, base_config, variant=None):
        installs.append((arch, variant, base_config.get("variant")))
        (site_packages / f"native_{arch}").mkdir(parents=True)

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
    assert top_level == ["3.11-slim", registry.index_digest, registry.index_digest]


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
