import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def checker(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    return importlib.import_module("check_squash_equivalence")


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _layout(root, *, platform="linux", layer_payloads=(b"layer bytes",)):
    blobs = root / "blobs" / "sha256"
    blobs.mkdir(parents=True)

    def blob(data):
        digest = _digest(data)
        (blobs / digest.split(":", 1)[1]).write_bytes(data)
        return {"digest": digest, "size": len(data)}

    layers = [blob(content) for content in layer_payloads]
    config = blob(json.dumps({
        "os": platform,
        "architecture": "amd64",
        "rootfs": {
            "type": "layers",
            "diff_ids": [_digest(b"uncompressed:" + content) for content in layer_payloads],
        },
    }).encode())
    manifest = blob(json.dumps({"schemaVersion": 2, "config": config, "layers": layers}).encode())
    descriptor = {**manifest, "platform": {"os": platform, "architecture": "amd64"}}
    index = json.dumps({"schemaVersion": 2, "manifests": [descriptor]}).encode()
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (root / "index.json").write_bytes(index)
    return {
        "index_digest": _digest(index),
        "manifest_digest": manifest["digest"],
        "config_digest": config["digest"],
        "layer_digests": [layer["digest"] for layer in layers],
    }


def test_validate_layout_checks_the_transferred_descriptor_graph(checker, tmp_path):
    layout = tmp_path / "layout"
    expected = _layout(layout)

    result = checker.validate_layout(layout, expected)

    assert result["config_digest"] == expected["config_digest"]
    assert result["layer_digests"] == expected["layer_digests"]
    assert result["diff_ids"] == [_digest(b"uncompressed:layer bytes")]

    blob = layout / "blobs" / "sha256" / expected["layer_digests"][0].split(":", 1)[1]
    blob.write_bytes(b"altered data")
    with pytest.raises(RuntimeError, match="blob"):
        checker.validate_layout(layout, expected)


def test_validate_layout_rejects_non_linux_image_and_stale_index(checker, tmp_path):
    layout = tmp_path / "layout"
    expected = _layout(layout, platform="windows")
    with pytest.raises(RuntimeError, match="platform"):
        checker.validate_layout(layout, expected)

    expected["index_digest"] = _digest(b"different index")
    with pytest.raises(RuntimeError, match="index"):
        checker.validate_layout(layout, expected)


def test_build_only_records_both_layouts_without_docker(checker, tmp_path, monkeypatch):
    calls = []

    def fake_build(context, output, base_image, squash):
        assert (context / "wheelhouse" / "runtime_fixture-1.0-py3-none-any.whl").is_file()
        assert "runtime-fixture==1.0" in (context / "requirements.txt").read_text()
        calls.append((base_image, squash))
        record = _layout(output)
        return SimpleNamespace(
            manifest_digest=record["manifest_digest"],
            config_digest=record["config_digest"],
            layers=[SimpleNamespace(digest=value) for value in record["layer_digests"]],
        )

    monkeypatch.setattr(checker, "build_layout", fake_build)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    output = tmp_path / "output"
    checker.main(["--build-only", str(output)])

    metadata = json.loads((output / "metadata.json").read_text())
    assert calls == [("python:3.11-slim", False), ("python:3.11-slim", True)]
    assert metadata["commit"] == "a" * 40
    assert metadata["run_id"] == "123"
    assert metadata["run_attempt"] == "2"
    for name in ("plain", "squashed"):
        checker.validate_layout(output / name, metadata["images"][name])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "b" * 40),
        ("run_id", "456"),
        ("run_attempt", "1"),
        ("builder_os", "Linux"),
    ],
)
def test_validate_provenance_rejects_a_different_run_or_non_windows_builder(
    checker, field, value
):
    metadata = {
        "commit": "a" * 40,
        "run_id": "123",
        "run_attempt": "2",
        "builder_os": "Windows",
    }
    expected = {"commit": "a" * 40, "run_id": "123", "run_attempt": "2"}
    checker.validate_provenance(metadata, expected)
    metadata[field] = value
    with pytest.raises(RuntimeError, match="provenance"):
        checker.validate_provenance(metadata, expected)


def test_check_only_rejects_stale_artifact_before_touching_docker(checker, tmp_path, monkeypatch):
    (tmp_path / "metadata.json").write_text(json.dumps({
        "commit": "b" * 40,
        "run_id": "123",
        "run_attempt": "2",
        "builder_os": "Windows",
    }))
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(
        checker, "require_executable",
        lambda name: pytest.fail(f"unexpected executable lookup: {name}"),
    )
    with pytest.raises(RuntimeError, match="provenance"):
        checker.main(["--check-only", str(tmp_path)])


def test_validate_import_checks_config_and_uncompressed_layers(checker, monkeypatch):
    image = {"config_digest": _digest(b"config"), "diff_ids": [_digest(b"layer")]}
    results = iter([image["config_digest"], "linux/amd64", json.dumps(image["diff_ids"])])
    monkeypatch.setattr(checker, "command_output", lambda *args, **kwargs: next(results))
    checker.validate_import("docker", "example:tag", image, 30)

    results = iter([_digest(b"wrong config"), "linux/amd64", json.dumps(image["diff_ids"])])
    monkeypatch.setattr(checker, "command_output", lambda *args, **kwargs: next(results))
    with pytest.raises(RuntimeError, match="config"):
        checker.validate_import("docker", "example:tag", image, 30)

    results = iter([image["config_digest"], "linux/arm64"])
    monkeypatch.setattr(checker, "command_output", lambda *args, **kwargs: next(results))
    with pytest.raises(RuntimeError, match="platform"):
        checker.validate_import("docker", "example:tag", image, 30)

    results = iter([image["config_digest"], "linux/amd64", json.dumps([_digest(b"wrong")])])
    monkeypatch.setattr(checker, "command_output", lambda *args, **kwargs: next(results))
    with pytest.raises(RuntimeError, match="DiffIDs"):
        checker.validate_import("docker", "example:tag", image, 30)


def test_comparison_imports_and_runs_both_transferred_layouts(checker, tmp_path, monkeypatch):
    images = {
        "plain": _layout(tmp_path / "plain", layer_payloads=(b"base", b"deps", b"app")),
        "squashed": _layout(tmp_path / "squashed", layer_payloads=(b"base", b"combined")),
    }
    imported = []
    inspected = []
    observations = []

    def fake_import(docker, skopeo, image, layout, ref, timeout):
        imported.append(layout.name)

    def fake_run(docker, image_ref, timeout, options=(), command=()):
        observations.append((image_ref, options, command))
        return "same listing" if command else "pycontainer-runtime-dependency-ok\n"

    def fake_command(command, timeout):
        if command[0] == "skopeo":
            return "skopeo version 1.0"
        if command[-1] == "{{.Server.Version}}":
            return "Docker 1.0"
        return "linux/amd64"

    monkeypatch.setattr(checker, "import_layout", fake_import)
    monkeypatch.setattr(checker, "validate_import", lambda docker, ref, image, timeout: inspected.append(image))
    monkeypatch.setattr(checker, "run", fake_run)
    monkeypatch.setattr(checker, "command_output", fake_command)
    monkeypatch.setattr(checker.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))

    result = checker.compare_layouts(
        tmp_path, {"images": images}, "docker", "skopeo", None, 30, "/app"
    )

    assert imported == ["plain", "squashed"]
    assert [image["config_digest"] for image in inspected] == [
        images["plain"]["config_digest"], images["squashed"]["config_digest"]
    ]
    assert len(observations) == 4
    assert result["status"] == "passed"
    assert result["listing_sha256"] == hashlib.sha256(b"same listing").hexdigest()


def test_equivalence_requires_matching_output_listing_and_layer_reduction(checker):
    marker = "pycontainer-runtime-dependency-ok\n"
    checker.assert_equivalence((marker, "same listing"), (marker, "same listing"), 6, 5)
    with pytest.raises(RuntimeError, match="filesystem"):
        checker.assert_equivalence((marker, "one"), (marker, "two"), 6, 5)
    with pytest.raises(RuntimeError, match="one fewer layer"):
        checker.assert_equivalence((marker, "same"), (marker, "same"), 6, 6)
    with pytest.raises(RuntimeError, match="dependency"):
        checker.assert_equivalence(("wrong", "same"), ("wrong", "same"), 6, 5)
