"""Local OCI-layout and Docker-save base images (issue #20)."""
import bz2
import gzip
import hashlib
import io
import json
import lzma
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pycontainer.blob_store import BlobIntegrityError
from pycontainer.builder import ImageBuilder, UnsupportedPlatformError
from pycontainer.config import BuildConfig
from pycontainer.local_image import (
    LocalImageError,
    OCI_GZIP_LAYER,
    OCI_TAR_LAYER,
    parse_local_image_reference,
)


def sha(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def layer_tar(files) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def image_config(diff_ids, os_name="linux", arch="amd64", variant=None):
    config = {
        "architecture": arch,
        "os": os_name,
        "config": {"Env": ["PATH=/usr/bin", "BASE=1"], "WorkingDir": "/", "Cmd": ["python3"]},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
    }
    if variant:
        config["variant"] = variant
    return json.dumps(config).encode()


class Layout:
    """Write a minimal OCI image layout."""

    def __init__(self, root: Path):
        self.root = root
        (root / "blobs" / "sha256").mkdir(parents=True)
        (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
        self.descriptors = []

    def blob(self, data: bytes, media_type: str) -> dict:
        digest = sha(data)
        (self.root / "blobs" / "sha256" / digest.split(":", 1)[1]).write_bytes(data)
        return {"mediaType": media_type, "digest": digest, "size": len(data)}

    def image(self, marker: bytes, arch="amd64", variant=None, gzip_layer=False):
        tar = layer_tar({"etc/marker": marker})
        layer_data = gzip.compress(tar, mtime=0) if gzip_layer else tar
        layer = self.blob(layer_data, OCI_GZIP_LAYER if gzip_layer else OCI_TAR_LAYER)
        config = self.blob(
            image_config([sha(tar)], arch=arch, variant=variant),
            "application/vnd.oci.image.config.v1+json",
        )
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config,
            "layers": [layer],
        }
        descriptor = self.blob(
            json.dumps(manifest).encode(), "application/vnd.oci.image.manifest.v1+json"
        )
        platform = {"os": "linux", "architecture": arch}
        if variant:
            platform["variant"] = variant
        return descriptor, platform, layer

    def add(self, descriptor, ref=None, platform=None):
        entry = dict(descriptor)
        if ref:
            entry["annotations"] = {"org.opencontainers.image.ref.name": ref}
        if platform:
            entry["platform"] = platform
        self.descriptors.append(entry)
        self.write_index()

    def write_index(self, annotations=None):
        index = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": self.descriptors,
        }
        if annotations:
            index["annotations"] = annotations
        (self.root / "index.json").write_text(json.dumps(index))


def docker_archive(path: Path, images, *, modern=False, extra_members=None, env=None):
    """Write a docker-save tar. ``images`` is a list of (tags, arch, layer_bytes)."""
    members = {}
    manifest = []
    for tags, arch, layers in images:
        diff_ids = [sha(gzip.decompress(l) if l[:2] == b"\x1f\x8b" else l) for l in layers]
        config = image_config(diff_ids, arch=arch)
        if env:
            config_data = json.loads(config)
            config_data["config"]["Env"].extend(env)
            config = json.dumps(config_data).encode()
        if modern:
            config_name = f"blobs/sha256/{hashlib.sha256(config).hexdigest()}"
            layer_names = [f"blobs/sha256/{hashlib.sha256(l).hexdigest()}" for l in layers]
        else:
            config_name = f"{hashlib.sha256(config).hexdigest()}.json"
            layer_names = [f"{hashlib.sha256(l).hexdigest()[:12]}{i}/layer.tar" for i, l in enumerate(layers)]
        members[config_name] = config
        members.update(zip(layer_names, layers))
        manifest.append({"Config": config_name, "RepoTags": tags, "Layers": layer_names})
    members["manifest.json"] = json.dumps(manifest).encode()
    with tarfile.open(path, "w") as tar:
        for name, data in {**members, **(extra_members or {})}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return manifest


@pytest.fixture
def offline():
    """Fail if the build touches registry credentials or the network client."""
    with patch(
        "pycontainer.builder.RegistryClient", side_effect=AssertionError("registry used")
    ), patch(
        "pycontainer.builder.get_registry_auth", side_effect=AssertionError("auth used")
    ):
        yield


def test_local_base_bypasses_configured_mirrors(tmp_path, offline, monkeypatch):
    layout = Layout(tmp_path / "base")
    descriptor, platform, layer = layout.image(b"local")
    layout.add(descriptor, platform=platform)
    builder = ImageBuilder(BuildConfig(
        base_image=f"oci:{layout.root}",
        context_dir=str(tmp_path / "context"),
        output_dir=str(tmp_path / "out"),
        registry_mirrors={"docker.io": ["mirror.example"]},
    ))
    monkeypatch.setattr(
        builder, "_base_image_client",
        lambda *_args: pytest.fail("local base must not use a registry mirror"),
    )
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()

    layers, config = builder._pull_base_image(layers_dir, "linux", "amd64")

    assert layers[0].digest == layer["digest"]
    assert config["rootfs"]["diff_ids"] == [layer["digest"]]


def build(tmp_path: Path, base_image: str, platform="linux/amd64"):
    context = tmp_path / "context"
    context.mkdir(exist_ok=True)
    (context / "app.py").write_text("print('hello')\n")
    output = tmp_path / "out"
    config = BuildConfig(
        tag="app:v1",
        base_image=base_image,
        context_dir=str(context),
        output_dir=str(output),
        platform=platform,
        use_cache=False,
    )
    builder = ImageBuilder(config)
    builder.build()
    index = json.loads((output / "index.json").read_text())
    blobs = output / "blobs" / "sha256"
    manifest = json.loads((blobs / index["manifests"][0]["digest"].split(":")[1]).read_bytes())
    image = json.loads((blobs / manifest["config"]["digest"].split(":")[1]).read_bytes())
    return builder, manifest, image


# --- reference parsing ---------------------------------------------------


@pytest.mark.parametrize(
    "value, transport, path, reference, digest",
    [
        ("oci:/tmp/base", "oci", "/tmp/base", None, None),
        ("oci:///tmp/base", "oci", "/tmp/base", None, None),
        ("oci://dist/image", "oci", "dist/image", None, None),
        ("oci:/tmp/base:3.11-slim", "oci", "/tmp/base", "3.11-slim", None),
        ("oci:C:\\layouts\\py:3.11", "oci", "C:\\layouts\\py", "3.11", None),
        ("oci:C:/layouts/py", "oci", "C:/layouts/py", None, None),
        ("oci://C:/layouts/py:v1", "oci", "C:/layouts/py", "v1", None),
        ("oci:/tmp/base@sha256:" + "a" * 64, "oci", "/tmp/base", None, "sha256:" + "a" * 64),
        (
            "docker-archive:/tmp/py.tar:docker.io/library/python:3.11-slim",
            "docker-archive", "/tmp/py.tar", "docker.io/library/python:3.11-slim", None,
        ),
        ("docker-archive:D:\\images\\py.tar", "docker-archive", "D:\\images\\py.tar", None, None),
    ],
)
def test_parse_local_image_reference(value, transport, path, reference, digest):
    parsed = parse_local_image_reference(value)
    assert (parsed.transport, parsed.path, parsed.reference, parsed.digest) == (
        transport, Path(path), reference, digest,
    )


@pytest.mark.parametrize("value", ["python:3.11-slim", "ghcr.io/o/r:v1", "localhost:5000/x"])
def test_registry_references_are_not_local(value):
    assert parse_local_image_reference(value) is None


@pytest.mark.parametrize(
    "value",
    ["oci:", "oci:/tmp/x:", "oci:/tmp/x@sha256:ABC", "oci:/tmp/x:a b", "docker-archive:"],
)
def test_parse_rejects_malformed_local_references(value):
    with pytest.raises(ValueError):
        parse_local_image_reference(value)


# --- OCI layouts ---------------------------------------------------------


def test_oci_layout_build_preserves_base_offline(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    descriptor, platform, layer = layout.image(b"one", gzip_layer=True)
    layout.add(descriptor, ref="3.11-slim", platform=platform)

    builder, manifest, image = build(tmp_path, f"oci:{layout.root}:3.11-slim")

    assert manifest["layers"][0] == layer
    assert len(manifest["layers"]) == 2
    base_config = json.loads(
        (layout.root / "blobs/sha256" / json.loads(
            (layout.root / "blobs/sha256" / descriptor["digest"].split(":")[1]).read_bytes()
        )["config"]["digest"].split(":")[1]).read_bytes()
    )
    assert image["rootfs"]["diff_ids"][0] == base_config["rootfs"]["diff_ids"][0]
    assert len(image["rootfs"]["diff_ids"]) == len(manifest["layers"])
    assert "BASE=1" in image["config"]["Env"]
    assert image["config"]["Cmd"] == ["python3"]
    assert builder._base_image_name == "3.11-slim"


def test_oci_layout_selects_reference_among_many(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    first, platform, _ = layout.image(b"first")
    second, _, second_layer = layout.image(b"second")
    layout.add(first, ref="one", platform=platform)
    layout.add(second, ref="two", platform=platform)

    _, manifest, _ = build(tmp_path, f"oci:{layout.root}:two")
    assert manifest["layers"][0] == second_layer

    with pytest.raises(LocalImageError, match="multiple images.*one, two"):
        build(tmp_path, f"oci:{layout.root}")
    with pytest.raises(LocalImageError, match="no image matching 'three'"):
        build(tmp_path, f"oci:{layout.root}:three")


def test_oci_layout_selects_each_tag_for_shared_manifest(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    descriptor, _, layer = layout.image(b"shared")
    for tag in ("app:v1", "app:latest"):
        layout.add(descriptor, ref=tag)

    for tag in ("app:v1", "app:latest"):
        builder, manifest, _ = build(tmp_path, f"oci:{layout.root}:{tag}")
        assert manifest["layers"][0] == layer
        assert builder._base_image_name == tag

    with pytest.raises(LocalImageError, match="multiple images.*app:latest, app:v1"):
        build(tmp_path, f"oci:{layout.root}")


@pytest.mark.parametrize("with_platform", [False, True])
def test_oci_layout_digest_selects_manifest_shared_by_tags(tmp_path, offline, with_platform):
    layout = Layout(tmp_path / "base")
    descriptor, platform, layer = layout.image(b"shared")
    for tag in ("app:v1", "app:latest"):
        layout.add(descriptor, ref=tag, platform=platform if with_platform else None)

    builder, manifest, _ = build(tmp_path, f"oci:{layout.root}@{descriptor['digest']}")
    assert manifest["layers"][0] == layer
    assert builder._base_image_name not in ("app:v1", "app:latest")


def test_oci_layout_ref_selects_manifest_shared_by_aliases(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    descriptor, _, layer = layout.image(b"shared")
    layout.descriptors.append(
        {**descriptor, "annotations": {"org.opencontainers.image.ref.name": "app:v1"}}
    )
    layout.descriptors.append(
        {**descriptor, "annotations": {"io.containerd.image.name": "app:v1"}}
    )
    layout.write_index()

    builder, manifest, _ = build(tmp_path, f"oci:{layout.root}:app:v1")
    assert manifest["layers"][0] == layer
    assert builder._base_image_name == "app:v1"


def test_oci_layout_rejects_conflicting_duplicate_digest_descriptors(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    descriptor, _, _ = layout.image(b"shared")
    layout.add(descriptor, ref="one")
    layout.add({**descriptor, "size": descriptor["size"] + 1}, ref="two")

    with pytest.raises(LocalImageError, match="conflicting descriptors"):
        build(tmp_path, f"oci:{layout.root}@{descriptor['digest']}")


def test_oci_layout_selects_by_containerd_image_name_alias(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    tagged, _, tagged_layer = layout.image(b"tagged")
    named, _, named_layer = layout.image(b"named")
    layout.descriptors.append(
        {
            **tagged,
            "annotations": {
                "org.opencontainers.image.ref.name": "3.11",
                "io.containerd.image.name": "docker.io/library/python:3.11",
            },
        }
    )
    layout.descriptors.append(
        {**named, "annotations": {"io.containerd.image.name": "example.com/app:v2"}}
    )
    layout.write_index()

    for selector, layer in (
        ("3.11", tagged_layer),
        ("docker.io/library/python:3.11", tagged_layer),
        ("example.com/app:v2", named_layer),
    ):
        _, manifest, _ = build(tmp_path, f"oci:{layout.root}:{selector}")
        assert manifest["layers"][0] == layer

    with pytest.raises(
        LocalImageError,
        match=r"multiple images.*3\.11, docker\.io/library/python:3\.11, example\.com/app:v2",
    ):
        build(tmp_path, f"oci:{layout.root}")


@pytest.mark.parametrize("reference", ["v1", "example.com/app:v1"])
def test_oci_layout_selects_single_entry_by_either_index_annotation(
    tmp_path, offline, reference
):
    layout = Layout(tmp_path / "base")
    descriptor, _, layer = layout.image(b"index")
    layout.add(descriptor)
    layout.write_index({
        "org.opencontainers.image.ref.name": "v1",
        "io.containerd.image.name": "example.com/app:v1",
    })

    _, manifest, _ = build(tmp_path, f"oci:{layout.root}:{reference}")
    assert manifest["layers"][0] == layer


def test_oci_layout_does_not_select_multi_entry_by_index_annotation(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    first, _, _ = layout.image(b"first")
    second, _, _ = layout.image(b"second")
    layout.add(first, ref="one")
    layout.add(second, ref="two")
    layout.write_index({"org.opencontainers.image.ref.name": "index-name"})

    with pytest.raises(LocalImageError, match="has no image matching"):
        build(tmp_path, f"oci:{layout.root}:index-name")


def test_oci_layout_selects_by_digest(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    first, platform, first_layer = layout.image(b"first")
    second, _, _ = layout.image(b"second")
    layout.add(first, ref="one", platform=platform)
    layout.add(second, ref="two", platform=platform)

    _, manifest, _ = build(tmp_path, f"oci:{layout.root}@{first['digest']}")
    assert manifest["layers"][0] == first_layer


@pytest.mark.parametrize(
    "platform, marker_arch", [("linux/arm64", "arm64"), ("linux/arm/v7", "arm")]
)
def test_oci_layout_nested_index_platform_selection(tmp_path, offline, platform, marker_arch):
    layout = Layout(tmp_path / "base")
    entries = []
    for arch, variant in (("amd64", None), ("arm64", None), ("arm", "v7")):
        descriptor, descriptor_platform, layer = layout.image(arch.encode(), arch=arch, variant=variant)
        entries.append(({**descriptor, "platform": descriptor_platform}, layer, arch))
    nested = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [entry for entry, _, _ in entries],
    }
    layout.add(layout.blob(json.dumps(nested).encode(), nested["mediaType"]), ref="multi")

    _, manifest, image = build(tmp_path, f"oci:{layout.root}:multi", platform=platform)
    expected = next(layer for _, layer, arch in entries if arch == marker_arch)
    assert manifest["layers"][0] == expected
    assert image["architecture"] == marker_arch

    with pytest.raises(UnsupportedPlatformError, match="no exact manifest"):
        build(tmp_path, f"oci:{layout.root}:multi", platform="linux/s390x")


def test_oci_layout_top_level_platform_set_without_reference(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    for arch in ("amd64", "arm64"):
        descriptor, platform, _ = layout.image(arch.encode(), arch=arch)
        layout.add(descriptor, platform=platform)
    _, _, image = build(tmp_path, f"oci:{layout.root}", platform="linux/arm64")
    assert image["architecture"] == "arm64"

    layout.descriptors[1].pop("platform")
    layout.write_index()
    with pytest.raises(LocalImageError, match="without platform metadata"):
        build(tmp_path, f"oci:{layout.root}", platform="linux/arm64")


def test_oci_layout_rejects_platform_mismatch(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    descriptor, platform, _ = layout.image(b"x", arch="arm64")
    layout.add(descriptor, platform=platform)
    with pytest.raises(UnsupportedPlatformError):
        build(tmp_path, f"oci:{layout.root}", platform="linux/amd64")


@pytest.mark.parametrize("damage", ["truncate", "corrupt", "missing", "symlink"])
def test_oci_layout_rejects_damaged_layer_blob(tmp_path, offline, damage):
    layout = Layout(tmp_path / "base")
    descriptor, platform, layer = layout.image(b"x")
    layout.add(descriptor, platform=platform)
    blob = layout.root / "blobs" / "sha256" / layer["digest"].split(":")[1]
    data = blob.read_bytes()
    if damage == "truncate":
        blob.write_bytes(data[:-10])
    elif damage == "corrupt":
        blob.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    elif damage == "missing":
        blob.unlink()
    else:
        outside = tmp_path / "outside"
        outside.write_bytes(data)
        blob.unlink()
        try:
            blob.symlink_to(outside)
        except OSError:
            pytest.skip("symbolic links are unavailable")
    with pytest.raises((BlobIntegrityError, LocalImageError)):
        build(tmp_path, f"oci:{layout.root}")
    assert not (tmp_path / "out" / "index.json").exists()


def test_oci_layout_rejects_wrong_diff_id(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    tar = layer_tar({"a": b"a"})
    layer = layout.blob(gzip.compress(tar), OCI_GZIP_LAYER)
    config = layout.blob(image_config([sha(b"other")]), "application/vnd.oci.image.config.v1+json")
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config, "layers": [layer]}
    layout.add(layout.blob(json.dumps(manifest).encode(), manifest["mediaType"]))
    with pytest.raises(BlobIntegrityError, match="DiffID"):
        build(tmp_path, f"oci:{layout.root}")


NOT_PLAIN_TAR = {
    "gzip": lambda data: gzip.compress(data, mtime=0),
    "zstd": lambda data: b"\x28\xb5\x2f\xfd" + data,
    "bzip2": bz2.compress,
    "xz": lzma.compress,
    "garbage": lambda data: b"not a tar header".ljust(1024, b"\x01"),
    "truncated": lambda data: data[:600],
    "corrupt-later-header": lambda data: data[:1024] + b"\x01" * 512 + data[1536:],
}


@pytest.mark.parametrize("encoding", sorted(NOT_PLAIN_TAR))
def test_oci_plain_tar_layer_rejects_non_tar_bytes_matching_diff_id(
    tmp_path, offline, encoding
):
    layout = Layout(tmp_path / "base")
    data = NOT_PLAIN_TAR[encoding](layer_tar({"a": b"a"}))
    layer = layout.blob(data, OCI_TAR_LAYER)
    config = layout.blob(image_config([sha(data)]), "application/vnd.oci.image.config.v1+json")
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config, "layers": [layer]}
    layout.add(layout.blob(json.dumps(manifest).encode(), manifest["mediaType"]))
    with pytest.raises(BlobIntegrityError, match="uncompressed tar"):
        build(tmp_path, f"oci:{layout.root}")
    assert not (tmp_path / "out" / "index.json").exists()


@pytest.mark.parametrize("encoding", ["bzip2", "xz"])
def test_docker_archive_rejects_unsupported_compressed_layer(tmp_path, offline, encoding):
    archive = tmp_path / "base.tar"
    data = NOT_PLAIN_TAR[encoding](layer_tar({"a": b"a"}))
    docker_archive(archive, [(["base:v1"], "amd64", [data])], modern=True)
    with pytest.raises(BlobIntegrityError, match="uncompressed tar"):
        build(tmp_path, f"docker-archive:{archive}")


def test_oci_plain_tar_layer_accepts_empty_tar(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    empty = layer_tar({})
    layer = layout.blob(empty, OCI_TAR_LAYER)
    config = layout.blob(image_config([sha(empty)]), "application/vnd.oci.image.config.v1+json")
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config, "layers": [layer]}
    layout.add(layout.blob(json.dumps(manifest).encode(), manifest["mediaType"]))
    build(tmp_path, f"oci:{layout.root}")
    assert (tmp_path / "out" / "index.json").exists()


def test_oci_layout_rejects_bad_layout_marker_and_corrupt_manifest(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    descriptor, platform, _ = layout.image(b"x")
    layout.add(descriptor, platform=platform)
    (layout.root / "oci-layout").write_text('{"imageLayoutVersion":"2.0.0"}')
    with pytest.raises(LocalImageError, match="imageLayoutVersion"):
        build(tmp_path, f"oci:{layout.root}")

    (layout.root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    layout.descriptors[0]["size"] += 1
    layout.write_index()
    with pytest.raises(BlobIntegrityError, match="size"):
        build(tmp_path, f"oci:{layout.root}")


def test_pycontainer_output_layout_is_reusable_as_base(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    descriptor, platform, layer = layout.image(b"x")
    layout.add(descriptor, platform=platform)
    first = tmp_path / "first"
    first.mkdir()
    build(first, f"oci:{layout.root}")

    _, manifest, image = build(tmp_path, f"oci:{first / 'out'}:app:v1")
    assert manifest["layers"][0] == layer
    assert len(manifest["layers"]) == 3
    assert len(image["rootfs"]["diff_ids"]) == 3


def test_local_base_image_must_not_overlap_output(tmp_path, offline):
    layout = Layout(tmp_path / "out")
    descriptor, platform, _ = layout.image(b"x")
    layout.add(descriptor, platform=platform)
    with pytest.raises(LocalImageError, match="overlaps the build output"):
        build(tmp_path, f"oci:{layout.root}")


# --- Docker-save archives ------------------------------------------------


@pytest.mark.parametrize("modern", [False, True])
def test_docker_archive_build_preserves_base_offline(tmp_path, offline, modern):
    plain = layer_tar({"etc/one": b"1"})
    compressed = gzip.compress(layer_tar({"etc/two": b"2"}), mtime=0)
    archive = tmp_path / "python.tar"
    entries = docker_archive(
        archive, [(["python:3.11-slim"], "amd64", [plain, compressed])], modern=modern
    )

    builder, manifest, image = build(tmp_path, f"docker-archive:{archive}")

    assert [layer["mediaType"] for layer in manifest["layers"][:2]] == [OCI_TAR_LAYER, OCI_GZIP_LAYER]
    assert [layer["digest"] for layer in manifest["layers"][:2]] == [sha(plain), sha(compressed)]
    assert image["rootfs"]["diff_ids"][:2] == [sha(plain), sha(gzip.decompress(compressed))]
    assert "BASE=1" in image["config"]["Env"]
    assert builder._base_image_name == "python:3.11-slim"
    assert entries


def test_docker_archive_selects_tag_among_many(tmp_path, offline):
    archive = tmp_path / "images.tar"
    one, two = layer_tar({"one": b"1"}), layer_tar({"two": b"2"})
    docker_archive(archive, [(["python:3.11-slim"], "amd64", [one]), (["ghcr.io/o/base:v2"], "amd64", [two])])

    _, manifest, _ = build(tmp_path, f"docker-archive:{archive}:docker.io/library/python:3.11-slim")
    assert manifest["layers"][0]["digest"] == sha(one)
    _, manifest, _ = build(tmp_path, f"docker-archive:{archive}:ghcr.io/o/base:v2")
    assert manifest["layers"][0]["digest"] == sha(two)

    with pytest.raises(LocalImageError, match="contains 2 images"):
        build(tmp_path, f"docker-archive:{archive}")
    with pytest.raises(LocalImageError, match="no image tagged 'python:3.12'"):
        build(tmp_path, f"docker-archive:{archive}:python:3.12")


def test_docker_archive_rejects_platform_mismatch_before_copying(tmp_path, offline):
    archive = tmp_path / "arm.tar"
    layer = layer_tar({"x": b"x"})
    docker_archive(archive, [(["base:arm"], "arm64", [layer])])
    with pytest.raises(LocalImageError, match="does not match requested linux/amd64"):
        build(tmp_path, f"docker-archive:{archive}")
    assert not (tmp_path / "out" / "blobs" / "sha256" / sha(layer).split(":")[1]).exists()


def test_docker_archive_rejects_corrupt_layer_and_config(tmp_path, offline):
    archive = tmp_path / "bad.tar"
    good = layer_tar({"x": b"x"})
    manifest = docker_archive(archive, [(["base:v1"], "amd64", [good])], modern=True)
    corrupt = good[:-1] + b"\x01"
    docker_archive(
        archive, [(["base:v1"], "amd64", [good])], modern=True,
        extra_members={manifest[0]["Layers"][0]: corrupt},
    )
    with pytest.raises(BlobIntegrityError, match="hashes to"):
        build(tmp_path, f"docker-archive:{archive}")

    legacy = tmp_path / "legacy.tar"
    entries = docker_archive(legacy, [(["base:v1"], "amd64", [good])])
    with tarfile.open(legacy) as source:
        members = {m.name: source.extractfile(m).read() for m in source.getmembers()}
    members[entries[0]["Layers"][0]] = corrupt
    with tarfile.open(legacy, "w") as target:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            target.addfile(info, io.BytesIO(data))
    with pytest.raises(BlobIntegrityError, match="DiffID"):
        build(tmp_path, f"docker-archive:{legacy}")


def test_docker_archive_rejects_truncated_and_compressed_archives(tmp_path, offline):
    archive = tmp_path / "base.tar"
    docker_archive(archive, [(["base:v1"], "amd64", [layer_tar({"x": b"x" * 4096})])])
    data = archive.read_bytes()

    truncated = tmp_path / "truncated.tar"
    truncated.write_bytes(data[: len(data) // 2])
    with pytest.raises((LocalImageError, BlobIntegrityError)):
        build(tmp_path, f"docker-archive:{truncated}")

    compressed = tmp_path / "base.tar.gz"
    compressed.write_bytes(gzip.compress(data))
    with pytest.raises(LocalImageError, match="is compressed"):
        build(tmp_path, f"docker-archive:{compressed}")


@pytest.mark.parametrize("unsafe", ["../escape/layer.tar", "/abs/layer.tar"])
def test_docker_archive_rejects_unsafe_paths(tmp_path, offline, unsafe):
    archive = tmp_path / "unsafe.tar"
    layer = layer_tar({"x": b"x"})
    config = image_config([sha(layer)])
    with tarfile.open(archive, "w") as tar:
        for name, data in {
            "config.json": config,
            "manifest.json": json.dumps(
                [{"Config": "config.json", "RepoTags": ["b:1"], "Layers": [unsafe]}]
            ).encode(),
            unsafe: layer,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(LocalImageError, match="unsafe path|escapes"):
        build(tmp_path, f"docker-archive:{archive}")


def test_docker_archive_symlinked_layers_are_resolved_inside_archive(tmp_path, offline):
    layer = layer_tar({"x": b"x"})
    config = image_config([sha(layer), sha(layer)])

    def write(link_target):
        archive = tmp_path / "links.tar"
        with tarfile.open(archive, "w") as tar:
            for name, data in {
                "config.json": config,
                "manifest.json": json.dumps([{
                    "Config": "config.json", "RepoTags": ["b:1"],
                    "Layers": ["a/layer.tar", "b/layer.tar"],
                }]).encode(),
                "a/layer.tar": layer,
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("b/layer.tar")
            link.type = tarfile.SYMTYPE
            link.linkname = link_target
            tar.addfile(link)
        return archive

    _, manifest, _ = build(tmp_path, f"docker-archive:{write('../a/layer.tar')}")
    assert [layer_entry["digest"] for layer_entry in manifest["layers"][:2]] == [sha(layer)] * 2

    with pytest.raises(LocalImageError, match="escapes"):
        build(tmp_path, f"docker-archive:{write('../../outside/layer.tar')}")


def test_dry_run_does_not_read_local_base_image(tmp_path, offline):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('x')\n")
    config = BuildConfig(
        tag="app:v1", base_image=f"oci:{tmp_path / 'missing'}", context_dir=str(context),
        output_dir=str(tmp_path / "out"), dry_run=True, use_cache=False,
    )
    assert ImageBuilder(config).build() == "app:v1"

def test_single_descriptor_platform_supplies_variant(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    descriptor, platform, layer = layout.image(b"arm", arch="arm")
    layout.add(descriptor, platform={**platform, "variant": "v7"})
    _, manifest, image = build(tmp_path, f"oci:{layout.root}", platform="linux/arm/v7")
    assert manifest["layers"][0] == layer
    with pytest.raises(UnsupportedPlatformError):
        build(tmp_path, f"oci:{layout.root}", platform="linux/arm/v6")


def test_nested_manifest_media_type_comes_from_descriptor(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    tar = layer_tar({"a": b"a"})
    layer = layout.blob(tar, OCI_TAR_LAYER)
    config = layout.blob(image_config([sha(tar)]), "application/vnd.oci.image.config.v1+json")
    manifest = layout.blob(
        json.dumps({"schemaVersion": 2, "config": config, "layers": [layer]}).encode(),
        "application/vnd.oci.image.manifest.v1+json",
    )
    nested = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
              "manifests": [{**manifest, "platform": {"os": "linux", "architecture": "amd64"}}]}
    layout.add(layout.blob(json.dumps(nested).encode(), nested["mediaType"]))
    _, built, _ = build(tmp_path, f"oci:{layout.root}")
    assert built["layers"][0] == layer


def test_existing_output_blob_does_not_skip_source_verification(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    descriptor, platform, layer = layout.image(b"x")
    layout.add(descriptor, platform=platform)
    build(tmp_path, f"oci:{layout.root}")
    (layout.root / "blobs" / "sha256" / layer["digest"].split(":")[1]).unlink()
    with pytest.raises(LocalImageError, match="missing blob"):
        build(tmp_path, f"oci:{layout.root}")


def test_oci_layout_rejects_oversized_config_descriptor(tmp_path, offline):
    layout = Layout(tmp_path / "base")
    tar = layer_tar({"a": b"a"})
    layer = layout.blob(tar, OCI_TAR_LAYER)
    config = {"mediaType": "application/vnd.oci.image.config.v1+json",
              "digest": sha(b"big"), "size": 64 * 1024 * 1024}
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config, "layers": [layer]}
    layout.add(layout.blob(json.dumps(manifest).encode(), manifest["mediaType"]))
    with pytest.raises(LocalImageError, match="config exceeds"):
        build(tmp_path, f"oci:{layout.root}")


def test_docker_archive_multi_tag_image_uses_first_tag_name(tmp_path, offline):
    archive = tmp_path / "python.tar"
    docker_archive(archive, [(["python:3.11-slim", "python:3.11"], "amd64", [layer_tar({"x": b"x"})])])
    builder, _, _ = build(tmp_path, f"docker-archive:{archive}")
    assert builder._base_image_name == "python:3.11-slim"


def test_docker_archive_rejects_oversized_extended_header(tmp_path, offline):
    archive = tmp_path / "pax.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = 2
        info.pax_headers = {"comment": "x" * (2 * 1024 * 1024)}
        tar.addfile(info, io.BytesIO(b"[]"))
    with pytest.raises(LocalImageError, match="extended header"):
        build(tmp_path, f"docker-archive:{archive}")


def test_docker_archive_unspecified_target_exposes_platform(tmp_path):
    from pycontainer.local_image import open_local_image_source

    archive = tmp_path / "arm.tar"
    docker_archive(archive, [(["base:arm"], "arm64", [layer_tar({"x": b"x"})])])
    reference = parse_local_image_reference(f"docker-archive:{archive}")
    layers = tmp_path / "layers"
    with open_local_image_source(reference, layers, tmp_path / "out", None, None) as source:
        assert source.platform == {"os": "linux", "architecture": "arm64"}
    with pytest.raises(LocalImageError, match="does not match requested linux/amd64"):
        open_local_image_source(reference, layers, tmp_path / "out", "linux", "amd64")


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("size_delta", [-1, 0, 1])
def test_docker_layer_size_checked_before_any_staging(tmp_path, monkeypatch, modern, size_delta):
    from pycontainer import local_image

    archive = tmp_path / "base.tar"
    first = layer_tar({"first": b"x"})
    second = layer_tar({"second": b"x" * (12 * 1024)})
    docker_archive(archive, [(["base:v1"], "amd64", [first, second])], modern=modern)
    monkeypatch.setattr(
        local_image, "MAX_LAYER_UNCOMPRESSED_BYTES", len(second) - size_delta
    )
    reference = parse_local_image_reference(f"docker-archive:{archive}")
    layers = tmp_path / "layers"
    with patch("pycontainer.local_image._stage_stream", wraps=local_image._stage_stream) as stage:
        if size_delta > 0:
            with pytest.raises(LocalImageError, match="layer 2 exceeds"):
                local_image.open_local_image_source(
                    reference, layers, tmp_path / "out", "linux", "amd64"
                )
            stage.assert_not_called()
            assert not layers.exists()
        else:
            with local_image.open_local_image_source(
                reference, layers, tmp_path / "out", "linux", "amd64"
            ) as source:
                manifest, _ = source.pull_manifest(None)
                assert [layer["size"] for layer in manifest["layers"]] == [
                    len(first), len(second)
                ]


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("count", [999, 1000, 1001])
def test_nested_oci_index_entry_limit(tmp_path, depth, count):
    from pycontainer.local_image import OCILayoutSource

    layout = Layout(tmp_path / "base")
    descriptor, platform, _ = layout.image(b"selected")
    other = {**descriptor, "platform": {"os": "linux", "architecture": "arm64"}}
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {**descriptor, "platform": platform},
            *[other for _ in range(count - 1)],
        ],
    }
    nested = layout.blob(json.dumps(index).encode(), index["mediaType"])
    for _ in range(depth - 1):
        index["manifests"] = [{**nested, "platform": platform}]
        nested = layout.blob(json.dumps(index).encode(), index["mediaType"])
    layout.add(nested)

    reference = parse_local_image_reference(f"oci:{layout.root}")
    with OCILayoutSource(reference) as source:
        def read_nested_index():
            manifest, _ = source.pull_manifest(None)
            for _ in range(depth - 1):
                child = manifest["manifests"][0]
                manifest, _ = source.pull_manifest(
                    child["digest"], child["digest"], child["size"]
                )
            return manifest

        if count > 1000:
            with pytest.raises(LocalImageError, match="more than 1000 manifests"):
                read_nested_index()
        else:
            assert len(read_nested_index()["manifests"]) == count


def local_source_with_env(tmp_path, transport, name, env, arch="amd64"):
    tar = layer_tar({"etc/marker": b"musl"})
    if transport == "oci":
        layout = Layout(tmp_path / "base")
        layer = layout.blob(tar, OCI_TAR_LAYER)
        config = json.loads(image_config([sha(tar)], arch=arch))
        config["config"]["Env"].extend(env)
        config_descriptor = layout.blob(
            json.dumps(config).encode(), "application/vnd.oci.image.config.v1+json"
        )
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": config_descriptor,
            "layers": [layer],
        }
        descriptor = layout.blob(json.dumps(manifest).encode(), manifest["mediaType"])
        layout.descriptors.append(
            {**descriptor, "annotations": {"io.containerd.image.name": name}}
        )
        layout.write_index()
        return f"oci:{layout.root}"
    archive = tmp_path / "base.tar"
    docker_archive(archive, [([name], arch, [tar])], env=env)
    return f"docker-archive:{archive}"


@pytest.mark.parametrize("transport,name", [
    ("oci", "docker.io/library/python@sha256:" + "b" * 64),
    ("oci", "docker.io/library/python:3.11-slim"),
    ("docker", "docker.io/library/python:3.11-slim"),
])
@pytest.mark.parametrize("libc,platform", [
    ("musl-1.2", "musllinux_1_2_x86_64"),
    ("glibc-2.17", "manylinux_2_17_x86_64"),
    (None, None),
    ("unsupported", None),
])
@pytest.mark.parametrize("deps_source", ["requirements", "venv"])
def test_local_source_name_does_not_determine_libc(
    tmp_path, offline, transport, name, libc, platform, deps_source
):
    env = ["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.9"]
    if libc:
        env.append(f"PYTHON_LIBC={libc}")
    base_image = local_source_with_env(tmp_path, transport, name, env)

    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n")
    if deps_source == "requirements":
        (context / "requirements.txt").write_text("fixture-dep==1.0\n")
    else:
        site_packages = context / ".venv" / "lib" / "python3.11" / "site-packages"
        site_packages.mkdir(parents=True)
        (site_packages / "fixture_dep.py").write_text("value = 1\n")
    builder = ImageBuilder(
        BuildConfig(
            tag="app:v1",
            base_image=base_image,
            context_dir=str(context),
            output_dir=str(tmp_path / "out"),
            include_deps=True,
            use_cache=False,
        )
    )
    with patch("pycontainer.dependencies.subprocess.Popen") as pip:
        pip.return_value.wait.return_value = 0
        if platform is None:
            with pytest.raises(ValueError, match="(?i)libc|digest-only"):
                builder.build()
            pip.assert_not_called()
        else:
            builder.build()
            if deps_source == "requirements":
                command = pip.call_args.args[0]
                assert command[command.index("--platform") + 1] == platform
            else:
                pip.assert_not_called()
                assert builder._dependency_site_packages


@pytest.mark.parametrize(("arch", "machine"), [("amd64", "x86_64"), ("arm64", "aarch64")])
def test_local_source_verifies_virtualenv_closure_for_target(tmp_path, offline, arch, machine):
    base_image = local_source_with_env(
        tmp_path,
        "oci",
        "docker.io/library/python:3.11-slim",
        ["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.9", "PYTHON_LIBC=glibc-2.17"],
        arch=arch,
    )
    context = tmp_path / "context"
    site_packages = context / ".venv" / "lib" / "python3.11" / "site-packages"
    site_packages.mkdir(parents=True)
    (context / "app.py").write_text("print('hello')\n")

    def write_dist(name, version, requirements=""):
        dist = site_packages / f"{name.replace('-', '_')}-{version}.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{requirements}\n"
        )
        (dist / "WHEEL").write_text(
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )

    write_dist(
        "app-lib",
        "1.0",
        f'Requires-Dist: target-only>=2; platform_machine == "{machine}"\n'
        'Requires-Dist: windows-only; sys_platform == "win32"\n',
    )

    def builder(output):
        return ImageBuilder(
            BuildConfig(
                tag="app:v1",
                base_image=base_image,
                platform=f"linux/{arch}",
                context_dir=str(context),
                output_dir=str(tmp_path / output),
                include_deps=True,
                use_cache=False,
            )
        )

    with pytest.raises(ValueError, match="target-only.*not installed"):
        builder("missing").build()

    write_dist("target-only", "2.0")
    image = builder("complete")
    image.build()
    assert {name for name, _ in image._staged_packages} == {"app-lib", "target-only"}


@pytest.mark.parametrize("transport", ["oci", "docker"])
@pytest.mark.parametrize("env,expected", [
    (["PYTHON_VERSION=3.11.9"], "PYTHON_IMPLEMENTATION"),
    (["PYTHON_IMPLEMENTATION=PyPy", "PYTHON_VERSION=3.11.9"], "CPython"),
    (["PYTHON_IMPLEMENTATION=CPython"], "PYTHON_VERSION"),
    (["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=invalid"], "PYTHON_VERSION"),
])
def test_local_source_name_does_not_establish_python_abi(
    tmp_path, offline, transport, env, expected
):
    base_image = local_source_with_env(
        tmp_path, transport, "docker.io/library/python:3.11-slim",
        [*env, "PYTHON_LIBC=glibc-2.17"],
    )
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n")
    (context / "requirements.txt").write_text("fixture-dep==1.0\n")
    builder = ImageBuilder(
        BuildConfig(
            tag="app:v1",
            base_image=base_image,
            context_dir=str(context),
            output_dir=str(tmp_path / "out"),
            include_deps=True,
            use_cache=False,
        )
    )
    with patch("pycontainer.dependencies.subprocess.Popen") as pip:
        pip.return_value.wait.return_value = 0
        with pytest.raises(ValueError, match=expected):
            builder.build()
        pip.assert_not_called()
