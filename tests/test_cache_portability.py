"""Offline --cache-dir save/restore: relocation and partial-restore guarantees."""
import hashlib
import json
import logging
import multiprocessing
import os
import shutil
from pathlib import Path

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.cache import CacheIndexError, LayerCache
from pycontainer.config import BuildConfig

_SYNTHETIC_BASE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {"Env": [], "WorkingDir": "/"},
    "rootfs": {"type": "layers", "diff_ids": []},
}


def _synthetic_base(self, layers_dir, os_name, arch):
    return [], json.loads(json.dumps(_SYNTHETIC_BASE_CONFIG))


def _write_context(context, files):
    for relative, content in files.items():
        path = context / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return context


def _build(context, cache_dir, output_dir, *, use_cache=True):
    lookups = []
    builder = ImageBuilder(
        BuildConfig(
            tag="portability:test",
            context_dir=str(context),
            output_dir=str(output_dir),
            cache_dir=str(cache_dir),
            use_cache=use_cache,
            entrypoint=["python", "app.py"],
        )
    )
    builder._pull_base_image = _synthetic_base.__get__(builder)
    if builder.cache is not None:
        original_get_layer = builder.cache.get_layer

        def recording_get_layer(*args, **kwargs):
            result = original_get_layer(*args, **kwargs)
            lookups.append(result is not None)
            return result

        builder.cache.get_layer = recording_get_layer
    builder.build()
    return builder, lookups


def _image_identity(builder, output_dir):
    """Return every immutable descriptor of the image and verify its blobs exist."""
    blobs = Path(output_dir) / "blobs" / "sha256"
    manifest = json.loads((blobs / builder.manifest_digest.split(":", 1)[1]).read_bytes())
    config = json.loads((blobs / builder.config_digest.split(":", 1)[1]).read_bytes())
    for descriptor in [manifest["config"], *manifest["layers"]]:
        blob = (blobs / descriptor["digest"].split(":", 1)[1]).read_bytes()
        assert len(blob) == descriptor["size"]
        assert f"sha256:{hashlib.sha256(blob).hexdigest()}" == descriptor["digest"]
    index = json.loads((Path(output_dir) / "index.json").read_bytes())
    return {
        "index_manifest": index["manifests"][0]["digest"],
        "manifest": builder.manifest_digest,
        "config": builder.config_digest,
        "layers": [layer["digest"] for layer in manifest["layers"]],
        "diff_ids": config["rootfs"]["diff_ids"],
    }


def _cache_blob(cache_dir, digest):
    return Path(cache_dir) / "blobs" / "sha256" / digest.split(":", 1)[1]


@pytest.fixture
def populated(tmp_path):
    context = _write_context(
        tmp_path / "checkout-a" / "app",
        {"app.py": b"print('hello')\n", "pkg/module.py": b"VALUE = 1\n"},
    )
    cache_dir = tmp_path / "cache-a"
    output_dir = tmp_path / "out-a"
    builder, lookups = _build(context, cache_dir, output_dir)
    assert lookups == [False]
    return {
        "context": context,
        "cache_dir": cache_dir,
        "identity": _image_identity(builder, output_dir),
        "app_digest": builder.layers[-1].digest,
    }


def test_relocated_cache_and_checkout_hit_and_build_identical_image(tmp_path, populated):
    restored_cache = tmp_path / "elsewhere" / "restored-cache"
    relocated_context = tmp_path / "checkout-b" / "nested" / "app"
    shutil.copytree(populated["cache_dir"], restored_cache)
    shutil.copytree(populated["context"], relocated_context)
    shutil.rmtree(populated["cache_dir"])
    shutil.rmtree(populated["context"])

    output_dir = tmp_path / "out-b"
    builder, lookups = _build(relocated_context, restored_cache, output_dir)

    assert lookups == [True]
    assert _image_identity(builder, output_dir) == populated["identity"]


def test_restored_blobs_without_index_are_a_clean_miss(tmp_path, populated):
    restored_cache = tmp_path / "restored"
    shutil.copytree(populated["cache_dir"] / "blobs", restored_cache / "blobs")

    builder, lookups = _build(populated["context"], restored_cache, tmp_path / "out-b")
    assert lookups == [False]
    assert _image_identity(builder, tmp_path / "out-b") == populated["identity"]
    assert LayerCache(restored_cache).get_stats()["entries"] == 1

    _, lookups = _build(populated["context"], restored_cache, tmp_path / "out-c")
    assert lookups == [True]


@pytest.mark.parametrize("damage", ["missing", "same-size-corrupt"])
def test_restored_index_with_bad_blob_discards_and_rebuilds(tmp_path, populated, damage, caplog):
    restored_cache = tmp_path / "restored"
    shutil.copytree(populated["cache_dir"], restored_cache)
    blob = _cache_blob(restored_cache, populated["app_digest"])
    if damage == "missing":
        blob.unlink()
    else:
        original = blob.read_bytes()
        blob.write_bytes(bytes(byte ^ 0xFF for byte in original))
        assert blob.stat().st_size == len(original)

    with caplog.at_level(logging.WARNING, logger="pycontainer.cache"):
        builder, lookups = _build(populated["context"], restored_cache, tmp_path / "out-b")

    assert lookups == [False]
    if damage == "same-size-corrupt":
        assert "discarded corrupt blob" in caplog.text
    assert _image_identity(builder, tmp_path / "out-b") == populated["identity"]
    data = blob.read_bytes()
    assert f"sha256:{hashlib.sha256(data).hexdigest()}" == populated["app_digest"]

    _, lookups = _build(populated["context"], restored_cache, tmp_path / "out-c")
    assert lookups == [True]


@pytest.mark.parametrize(
    "index_text",
    [
        "{",
        "[]",
        json.dumps({"key": {"digest": "sha256:short"}}),
    ],
)
def test_restored_malformed_index_fails_with_remediation(tmp_path, populated, index_text):
    restored_cache = tmp_path / "restored"
    shutil.copytree(populated["cache_dir"], restored_cache)
    index_file = restored_cache / "index.json"
    index_file.write_text(index_text, encoding="utf-8")

    with pytest.raises(CacheIndexError) as raised:
        _build(populated["context"], restored_cache, tmp_path / "out-b")
    message = str(raised.value)
    assert f"Delete {index_file}" in message
    assert "--no-cache" in message

    builder, _ = _build(populated["context"], restored_cache, tmp_path / "out-c", use_cache=False)
    assert _image_identity(builder, tmp_path / "out-c") == populated["identity"]

    index_file.unlink()
    _, lookups = _build(populated["context"], restored_cache, tmp_path / "out-d")
    assert lookups == [False]


def _concurrent_build(context, cache_dir, output_dir, barrier, results):
    barrier.wait(timeout=30)
    builder, lookups = _build(Path(context), Path(cache_dir), Path(output_dir))
    results.put((str(context), lookups, builder.layers[-1].digest))


def test_concurrent_builds_share_a_restored_cache(tmp_path, populated):
    restored_cache = tmp_path / "restored"
    shutil.copytree(populated["cache_dir"], restored_cache)
    contexts = [
        populated["context"],
        _write_context(tmp_path / "other-1", {"app.py": b"print('one')\n"}),
        _write_context(tmp_path / "other-2", {"app.py": b"print('two')\n"}),
    ]
    spawn = multiprocessing.get_context("spawn")
    barrier = spawn.Barrier(len(contexts))
    results = spawn.Queue()
    processes = [
        spawn.Process(
            target=_concurrent_build,
            args=(str(context), str(restored_cache), str(tmp_path / f"out-{i}"), barrier, results),
        )
        for i, context in enumerate(contexts)
    ]
    for process in processes:
        process.start()
    outcomes = {}
    for _ in processes:
        context, lookups, digest = results.get(timeout=60)
        outcomes[context] = (lookups, digest)
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert outcomes[str(contexts[0])] == ([True], populated["app_digest"])
    cache = LayerCache(restored_cache)
    assert cache.get_stats()["entries"] == 3
    for context in contexts[1:]:
        _, lookups = _build(context, restored_cache, tmp_path / f"verify-{context.name}")
        assert lookups == [True]


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot represent POSIX permission bits")
def test_mode_change_misses_because_layer_records_the_mode(tmp_path, populated):
    restored_cache = tmp_path / "restored"
    relocated_context = tmp_path / "checkout-b"
    shutil.copytree(populated["cache_dir"], restored_cache)
    shutil.copytree(populated["context"], relocated_context)
    (relocated_context / "app.py").chmod(0o755)

    builder, lookups = _build(relocated_context, restored_cache, tmp_path / "out-b")

    assert lookups == [False]
    assert builder.layers[-1].digest != populated["app_digest"]
