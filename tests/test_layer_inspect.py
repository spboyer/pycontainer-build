"""Tests for daemon-free local image layer inspection (issue #65)."""
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from pycontainer import local_image
from pycontainer.layer_inspect import inspect_image, render_text, run_inspect
from pycontainer.local_image import OCI_GZIP_LAYER, OCI_TAR_LAYER

CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
REPO_ROOT = Path(__file__).resolve().parents[1]


def sha(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def tar(*entries) -> bytes:
    """Build a layer tar from (name, content) pairs or TarInfo-configuring tuples.

    ``content`` may be bytes (regular file), ``None`` (directory), or a dict of
    TarInfo attributes such as ``{"type": tarfile.SYMTYPE, "linkname": "x"}``.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.mtime = 0
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            elif isinstance(content, dict):
                for key, value in content.items():
                    setattr(info, key, value)
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class Image:
    """Write an OCI layout whose index points at one or more images."""

    def __init__(self, root: Path):
        self.root = root
        (root / "blobs" / "sha256").mkdir(parents=True)
        (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
        self.descriptors = []

    def blob(self, data: bytes, media_type: str) -> dict:
        (self.root / "blobs" / "sha256" / sha(data).split(":")[1]).write_bytes(data)
        return {"mediaType": media_type, "digest": sha(data), "size": len(data)}

    def image(self, layers, *, arch="amd64", compress=True, history=None, diff_ids=None,
              media_type=None, ref=None, platform=True):
        blobs = []
        for layer in layers:
            data = gzip.compress(layer, mtime=0) if compress else layer
            blobs.append(self.blob(data, media_type or (OCI_GZIP_LAYER if compress else OCI_TAR_LAYER)))
        config = {
            "architecture": arch, "os": "linux",
            "config": {"Cmd": ["python3"]},
            "rootfs": {"type": "layers", "diff_ids": diff_ids or [sha(layer) for layer in layers]},
        }
        if history is not None:
            config["history"] = history
        manifest = {
            "schemaVersion": 2, "mediaType": MANIFEST_TYPE,
            "config": self.blob(json.dumps(config).encode(), CONFIG_TYPE), "layers": blobs,
        }
        descriptor = self.blob(json.dumps(manifest).encode(), MANIFEST_TYPE)
        if ref:
            descriptor["annotations"] = {"org.opencontainers.image.ref.name": ref}
        if platform:
            descriptor["platform"] = {"os": "linux", "architecture": arch}
        self.descriptors.append(descriptor)
        (self.root / "index.json").write_text(
            json.dumps({"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": self.descriptors})
        )
        return blobs


def layout(tmp_path: Path, *layers, **options) -> str:
    Image(tmp_path / "img").image(list(layers), **options)
    return f"oci:{tmp_path / 'img'}"


def docker_archive(path: Path, layers, arch="amd64", tag="app:v1") -> str:
    config = json.dumps({
        "architecture": arch, "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [sha(layer) for layer in layers]},
    }).encode()
    members = {f"{hashlib.sha256(config).hexdigest()}.json": config}
    names = []
    for index, layer in enumerate(layers):
        names.append(f"layer{index}/layer.tar")
        members[names[-1]] = layer
    members["manifest.json"] = json.dumps(
        [{"Config": next(iter(members)), "RepoTags": [tag], "Layers": names}]
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return f"docker-archive:{path}"


def error_of(image, **kwargs) -> str:
    output, code = run_inspect(image, as_json=False, **kwargs)
    assert code == 1, output
    assert output.startswith("error: ")
    return output


def changes(layer):
    return {item["path"]: item["change"] for item in layer["files"]}


# --- layer sizes, contents and union semantics ----------------------------


def test_reports_sizes_contents_and_whiteouts(tmp_path):
    base = tar(("app", None), ("app/a.txt", b"a" * 100), ("app/old.pyc", b"p" * 50), ("etc/conf", b"c"))
    top = tar(("app/.wh.old.pyc", b""), ("app/a.txt", b"b" * 200), ("app/new.txt", b"n" * 10))
    result = inspect_image(layout(tmp_path, base, top), files=True)

    first, second = result["layers"]
    assert first["uncompressedSize"] == len(base)
    assert first["compressedSize"] == len(gzip.compress(base, mtime=0))
    assert first["diffId"] == sha(base)
    assert first["contentBytes"] == 151
    assert changes(first) == {"app": "added", "app/a.txt": "added", "app/old.pyc": "added",
                              "etc": "added", "etc/conf": "added"}
    assert (second["added"], second["modified"], second["deleted"]) == (1, 1, 1)
    assert changes(second) == {"app/old.pyc": "deleted", "app/a.txt": "modified", "app/new.txt": "added"}
    assert second["shadowedBytes"] == 150
    assert result["filesystem"] == {"files": 3, "directories": 2, "symlinks": 0, "other": 0, "bytes": 211}
    shadowed = next(o for o in result["observations"] if o["kind"] == "shadowed-files")
    assert shadowed["bytes"] == 150
    assert {p["path"] for p in shadowed["paths"]} == {"app/a.txt", "app/old.pyc"}


def test_opaque_directory_hides_only_lower_layers(tmp_path):
    base = tar(("data", None), ("data/x", b"x" * 5), ("data/sub/y", b"y" * 7))
    top = tar(("data", None), ("data/.wh..wh..opq", b""), ("data/z", b"z"))
    result = inspect_image(layout(tmp_path, base, top), layers=[2])
    layer = result["layers"][1]
    assert changes(layer) == {"data/sub": "deleted", "data/sub/y": "deleted", "data/x": "deleted",
                              "data": "unchanged", "data/z": "added"}
    assert layer["deleted"] == 3  # data/sub, data/sub/y and data/x
    assert layer["shadowedBytes"] == 12
    assert result["filesystem"]["files"] == 1


def test_file_replacing_directory_removes_subtree(tmp_path):
    base = tar(("opt/tool/bin", b"1" * 4), ("opt/tool/lib", b"2" * 6))
    top = tar(("opt/tool", b"file"))
    result = inspect_image(layout(tmp_path, base, top), files=True)
    layer = result["layers"][1]
    assert layer["shadowedBytes"] == 10
    assert result["filesystem"]["files"] == 1
    assert (layer["modified"], layer["deleted"]) == (1, 2)
    assert changes(layer) == {"opt/tool": "modified", "opt/tool/bin": "deleted", "opt/tool/lib": "deleted"}


def test_symlink_replacing_directory_and_directory_replacing_file(tmp_path):
    base = tar(("opt/d/x", b"1"), ("opt/f", b"22"))
    top = tar(("opt/d", {"type": tarfile.SYMTYPE, "linkname": "f"}), ("opt/f/inner", b"3"))
    layer = inspect_image(layout(tmp_path, base, top), files=True)["layers"][1]
    assert changes(layer) == {"opt/d": "modified", "opt/d/x": "deleted", "opt/f": "modified", "opt/f/inner": "added"}
    assert (layer["added"], layer["modified"], layer["deleted"], layer["shadowedBytes"]) == (1, 2, 1, 3)


def test_whiteout_of_missing_path_is_ignored(tmp_path):
    result = inspect_image(layout(tmp_path, tar(("a", b"1")), tar((".wh.missing", b""))))
    assert result["layers"][1]["deleted"] == 0


def test_hardlink_symlink_and_device_entries(tmp_path):
    layer = tar(
        ("bin/python3", b"elf"),
        ("bin/python", {"type": tarfile.LNKTYPE, "linkname": "bin/python3"}),
        ("bin/py", {"type": tarfile.SYMTYPE, "linkname": "python3"}),
        ("dev/null", {"type": tarfile.CHRTYPE, "devmajor": 1, "devminor": 3}),
        ("run/fifo", {"type": tarfile.FIFOTYPE}),
    )
    result = inspect_image(layout(tmp_path, layer), files=True)
    files = {item["path"]: item for item in result["layers"][0]["files"]}
    assert files["bin/python"]["type"] == "hardlink"
    assert files["bin/python"]["digest"] == files["bin/python3"]["digest"]
    assert files["bin/py"]["linkTarget"] == "python3"
    assert files["dev/null"]["linkTarget"] == "1:3"
    assert result["filesystem"]["symlinks"] == 1
    assert result["filesystem"]["other"] == 3


def test_observations_flag_duplicates_and_bloat_without_claiming_savings(tmp_path):
    layer = tar(
        ("app/one.bin", b"d" * 64), ("app/two.bin", b"d" * 64),
        ("app/__pycache__/m.cpython-311.pyc", b"c" * 8),
        ("root/.cache/pip/http/abc", b"p" * 9),
        ("var/lib/apt/lists/deb", b"l" * 3),
        ("src/.git/HEAD", b"ref"),
    )
    result = inspect_image(layout(tmp_path, layer))
    kinds = {o["kind"]: o for o in result["observations"]}
    assert kinds["duplicate-content"]["bytes"] == 64
    assert kinds["duplicate-content"]["paths"][0]["copies"] == 2
    assert kinds["python-bytecode"]["bytes"] == 8
    assert kinds["pip-cache"]["bytes"] == 9
    assert kinds["apt-cache"]["bytes"] == 3
    assert kinds["vcs"]["bytes"] == 3
    assert "shadowed-files" not in kinds
    text = run_inspect(layout(tmp_path / "t", layer))[0]
    assert "not measured savings" in text
    assert "saving" not in text.replace("not measured savings", "").lower()


def test_diff_compares_filesystem_states(tmp_path):
    l1 = tar(("a", b"1"), ("b", b"2"), ("keep", b"k"))
    l2 = tar(("a", b"changed"), (".wh.b", b""), ("c", b"3"))
    l3 = tar(("c", b"3"), ("d", b"4"), (".wh.d", b""))
    image = layout(tmp_path, l1, l2, l3)
    diff = inspect_image(image, diff=(1, 3))["diff"]
    assert {c["path"]: c["change"] for c in diff["changes"]} == {"a": "modified", "b": "deleted", "c": "added", "d": "added"}
    before = next(c for c in diff["changes"] if c["path"] == "a")["before"]
    assert before["size"] == 1 and before["layer"] == 1
    whole = inspect_image(image, diff=(0, 3))["diff"]
    assert {c["path"] for c in whole["changes"]} == {"a", "c", "d", "keep"}
    last = inspect_image(image, diff=(2, 3))["diff"]["changes"]
    assert [(c["change"], c["path"]) for c in last] == [("added", "d")]


def test_history_maps_created_by_to_non_empty_layers(tmp_path):
    history = [
        {"created_by": "ADD rootfs"},
        {"created_by": "ENV A=1", "empty_layer": True},
        {"created_by": "RUN pip install x"},
    ]
    result = inspect_image(layout(tmp_path, tar(("a", b"1")), tar(("b", b"2")), history=history))
    assert [layer["createdBy"] for layer in result["layers"]] == ["ADD rootfs", "RUN pip install x"]
    mismatched = inspect_image(layout(tmp_path / "m", tar(("a", b"1")), history=history))
    assert mismatched["layers"][0]["createdBy"] is None


def test_uncompressed_tar_and_pycontainer_output(tmp_path):
    base = layout(tmp_path, tar(("usr/bin/python3", b"elf")), compress=False)
    assert inspect_image(base)["layers"][0]["mediaType"] == OCI_TAR_LAYER

    context = tmp_path / "ctx"
    (context / "src").mkdir(parents=True)
    (context / "src" / "app.py").write_text("print('hi')\n")
    (context / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    from pycontainer.builder import ImageBuilder
    from pycontainer.config import BuildConfig
    ImageBuilder(BuildConfig(tag="demo:v1", context_dir=str(context), output_dir=str(tmp_path / "out"),
                             base_image=base, use_cache=False)).build()
    report = inspect_image(str(tmp_path / "out"), files=True)
    paths = [item["path"] for layer in report["layers"] for item in layer["files"]]
    assert "usr/bin/python3" in paths
    assert any(path.endswith("src/app.py") for path in paths)


# --- image and platform selection ----------------------------------------


def test_multi_platform_index_requires_explicit_platform(tmp_path):
    image = Image(tmp_path / "img")
    image.image([tar(("arch", b"amd"))], arch="amd64")
    image.image([tar(("arch", b"arm"))], arch="arm64")
    message = error_of(f"oci:{tmp_path / 'img'}")
    assert "--platform" in message and "linux/amd64" in message and "linux/arm64" in message
    result = inspect_image(f"oci:{tmp_path / 'img'}", platform="linux/arm64")
    assert result["platform"] == "linux/arm64"
    assert result["layers"][0]["diffId"] == sha(tar(("arch", b"arm")))
    assert "no exact manifest for linux/s390x" in error_of(f"oci:{tmp_path / 'img'}", platform="linux/s390x")


def test_nested_index_over_entry_limit_fails_without_report(tmp_path, monkeypatch):
    image = Image(tmp_path / "img")
    image.image([tar(("a", b"1"))])
    descriptor = image.descriptors[0]
    nested = {"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": [descriptor, descriptor]}
    nested_descriptor = image.blob(json.dumps(nested).encode(), INDEX_TYPE)
    (image.root / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [nested_descriptor]}))
    monkeypatch.setattr(local_image, "MAX_INDEX_ENTRIES", 1)

    output, code = run_inspect(f"oci:{image.root}", as_json=True)
    assert code == 1
    assert output.startswith("error: ")
    assert "more than 1" in output


def test_platform_must_match_single_image(tmp_path):
    image = layout(tmp_path, tar(("a", b"1")), platform=False)
    assert "does not match" in error_of(image, platform="linux/arm64")
    assert inspect_image(image, platform="linux/amd64")["platform"] == "linux/amd64"


def test_reference_selection_and_non_local_reference(tmp_path):
    image = Image(tmp_path / "img")
    image.image([tar(("v", b"1"))], ref="one")
    image.image([tar(("v", b"2"))], ref="two")
    result = inspect_image(f"oci:{tmp_path / 'img'}:two")
    assert result["layers"][0]["diffId"] == sha(tar(("v", b"2")))
    assert "local images only" in error_of("python:3.11-slim")
    assert "local images only" in error_of(str(tmp_path))


def test_docker_archive_inspection_reports_platform_and_keeps_json_clean(tmp_path, capsys):
    image = docker_archive(tmp_path / "app.tar", [tar(("a", b"1")), tar((".wh.a", b""), ("b", b"22"))])
    output, code = run_inspect(image, as_json=True)
    assert code == 0
    result = json.loads(output)
    assert result["platform"] == "linux/amd64"
    assert [layer["deleted"] for layer in result["layers"]] == [0, 1]
    assert "Loading" not in capsys.readouterr().out
    assert "does not match" in error_of(image, platform="linux/arm64")
    assert inspect_image(image, platform="linux/amd64")["filesystem"]["files"] == 1


def test_oversized_docker_layer_rejected_before_staging(tmp_path, monkeypatch):
    layer = tar(("a", b"1"))
    image = docker_archive(tmp_path / "oversized.tar", [layer])
    monkeypatch.setattr(local_image, "MAX_LAYER_UNCOMPRESSED_BYTES", len(layer) - 1)
    staged = []
    original = local_image._stage_stream

    def track_staging(stream, destination, digest, size, description):
        staged.append(description)
        return original(stream, destination, digest, size, description)

    monkeypatch.setattr(local_image, "_stage_stream", track_staging)
    output, code = run_inspect(image, as_json=True)
    assert code == 1
    assert output.startswith("error: ")
    assert "exceeds" in output
    assert "Docker archive layer 1" not in staged


# --- safe archive handling and explicit errors ---------------------------


@pytest.mark.parametrize("name", ["../escape", "a/../../b"])
def test_rejects_paths_escaping_the_root(tmp_path, name):
    assert "escapes the image root" in error_of(layout(tmp_path, tar((name, b"x"))))


@pytest.mark.parametrize("name", ["/etc/passwd", "//server/share", "/../root"])
def test_rejects_absolute_paths(tmp_path, name):
    assert "absolute path" in error_of(layout(tmp_path, tar((name, b"x"))))


def test_escapes_image_metadata_in_text_output(tmp_path):
    result = inspect_image(layout(tmp_path, tar(("a", b"1"))))
    result["platform"] = "linux/\x1b[2Jamd64"
    result["manifest"]["mediaType"] = "evil\u202etype"
    text = render_text(result)
    assert "\x1b" not in text and "\u202e" not in text


def test_rejects_entries_beneath_symlinks(tmp_path):
    base = tar(("lib", {"type": tarfile.SYMTYPE, "linkname": "/etc"}))
    top = tar(("lib/passwd", b"x"))
    assert "beneath symlink" in error_of(layout(tmp_path, base, top))


def test_rejects_hardlink_to_missing_target_or_directory(tmp_path):
    missing = tar(("a", {"type": tarfile.LNKTYPE, "linkname": "nope"}))
    assert "targets missing path" in error_of(layout(tmp_path / "m", missing))
    directory = tar(("d", None), ("a", {"type": tarfile.LNKTYPE, "linkname": "d"}))
    assert "targets a directory" in error_of(layout(tmp_path / "d", directory))


def test_escapes_control_characters_in_text_output(tmp_path):
    output, code = run_inspect(layout(tmp_path, tar(("evil\x1b[31m\u202ename", b"x"))), files=True)
    assert code == 0
    assert "\x1b" not in output and "\u202e" not in output
    assert "evil\\x1b[31m\\u202ename" in output


def test_rejects_wrong_diff_id(tmp_path):
    image = layout(tmp_path, tar(("a", b"1")), diff_ids=["sha256:" + "0" * 64])
    assert "DiffID" in error_of(image)


def test_rejects_corrupt_and_mislabeled_layers(tmp_path):
    good = tar(("a", b"1" * 1000))
    image = Image(tmp_path / "img")
    blob = image.image([good])[0]
    path = tmp_path / "img" / "blobs" / "sha256" / blob["digest"].split(":")[1]
    data = bytearray(path.read_bytes())
    data[30] ^= 0xFF
    path.write_bytes(bytes(data))
    assert "error:" in error_of(f"oci:{tmp_path / 'img'}")

    mislabeled = layout(tmp_path / "m", good, compress=False, media_type=OCI_GZIP_LAYER)
    assert "uncompressed tar" in error_of(mislabeled)
    unsupported = layout(tmp_path / "u", good, compress=False, media_type="application/x-custom")
    assert "unsupported" in error_of(unsupported).lower()


def test_rejects_data_after_tar_end(tmp_path):
    layer = tar(("a", b"1")) + b"junk" + b"\0" * 508
    assert "after the end" in error_of(layout(tmp_path, layer))


def test_rejects_zstd_without_runtime_support(tmp_path):
    if sys.version_info >= (3, 14):
        pytest.skip("Python 3.14+ decompresses zstd layers")
    image = layout(tmp_path, b"\x28\xb5\x2f\xfd" + b"\0" * 16, compress=False,
                   media_type="application/vnd.oci.image.layer.v1.tar+zstd", diff_ids=["sha256:" + "1" * 64])
    assert "Python 3.14" in error_of(image)


def test_zstd_layers_on_supported_runtime(tmp_path):
    zstd = pytest.importorskip("compression.zstd")
    layer = tar(("z", b"zstd"))
    image = layout(tmp_path, zstd.compress(layer), compress=False,
                   media_type="application/vnd.oci.image.layer.v1.tar+zstd", diff_ids=[sha(layer)])
    assert inspect_image(image)["filesystem"]["files"] == 1


@pytest.mark.parametrize("option, message", [
    ({"layers": [3]}, "--layer 3 is out of range"),
    ({"layers": [0]}, "--layer 0 is out of range"),
    ({"diff": [1, 1]}, "--diff 1 1 is invalid"),
    ({"diff": [0, 3]}, "--diff 0 3 is invalid"),
])
def test_rejects_out_of_range_selections(tmp_path, option, message):
    assert message in error_of(layout(tmp_path, tar(("a", b"1")), tar(("b", b"2"))), **option)


# --- CLI ------------------------------------------------------------------


def test_cli_inspect_text_json_and_error_exit(tmp_path):
    image = layout(tmp_path, tar(("app/main.py", b"print(1)\n")))
    command = [sys.executable, "-m", "pycontainer.cli", "inspect", image]
    env = {"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    text = subprocess.run([*command, "--layer", "1"], capture_output=True, text=True, env=env, timeout=120)
    assert text.returncode == 0, text.stderr
    assert "app/main.py" in text.stdout and "Final filesystem" in text.stdout
    data = subprocess.run([*command, "--json", "--diff", "0", "1"], capture_output=True, text=True, env=env, timeout=120)
    assert json.loads(data.stdout)["diff"]["changes"][0]["path"] == "app"
    bad = subprocess.run([*command, "--layer", "9"], capture_output=True, text=True, env=env, timeout=120)
    assert bad.returncode == 1 and bad.stdout == "" and "out of range" in bad.stderr
