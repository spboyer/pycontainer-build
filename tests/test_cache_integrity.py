"""Regression tests for cache identity and persistence guarantees."""
import hashlib
import logging
import multiprocessing
import os
import tarfile
import time
from pathlib import Path

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.cache import CacheIndexError, CacheIntegrityError, LayerCache
from pycontainer.config import BuildConfig
from pycontainer.fs_utils import file_lock


def _digest(data):
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _store_cache_entry(cache_dir, source_path, relative_path, layer_path, digest, barrier):
    cache = LayerCache(Path(cache_dir))
    barrier.wait(timeout=20)
    cache.store_layer(
        [(Path(source_path), Path(relative_path))],
        digest,
        Path(layer_path),
    )


def _acquire_contended_lock(lock_path, waiting, acquired):
    import msvcrt

    original_locking = msvcrt.locking

    def observe_locking(fd, mode, length):
        try:
            return original_locking(fd, mode, length)
        except OSError:
            if mode == msvcrt.LK_NBLCK:
                waiting.set()
            raise

    msvcrt.locking = observe_locking
    with file_lock(lock_path):
        acquired.set()


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range locking")
def test_empty_windows_lock_reports_nonblocking_contention(tmp_path):
    import msvcrt

    lock_path = tmp_path / "index.lock"
    with lock_path.open("a+b") as holder:
        msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            with pytest.raises(BlockingIOError, match="Lock is already held"):
                with file_lock(lock_path, blocking=False):
                    pytest.fail("contended lock was acquired")
            assert lock_path.stat().st_size == 0
        finally:
            holder.seek(0)
            msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)

    with file_lock(lock_path, blocking=False):
        pass
    assert lock_path.read_bytes() == b"\0"


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range locking")
def test_empty_windows_lock_waits_for_another_process(tmp_path):
    import msvcrt

    lock_path = tmp_path / "index.lock"
    context = multiprocessing.get_context("spawn")
    waiting = context.Event()
    acquired = context.Event()
    process = context.Process(
        target=_acquire_contended_lock, args=(str(lock_path), waiting, acquired)
    )
    with lock_path.open("a+b") as holder:
        msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
        process.start()
        try:
            deadline = time.monotonic() + 30
            while not waiting.wait(timeout=0.1) and process.is_alive():
                if time.monotonic() >= deadline:
                    break
            assert waiting.is_set(), f"lock contention not observed; exit code: {process.exitcode}"
            assert not acquired.is_set()
        finally:
            holder.seek(0)
            msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
                pytest.fail("contended lock process did not finish")

    assert process.exitcode == 0
    assert acquired.is_set()
    assert lock_path.read_bytes() == b"\0"


def test_cache_invalidation_when_content_changes_with_same_size_and_mtime(tmp_path):
    cache = LayerCache(tmp_path / "cache")
    source = tmp_path / "source.py"
    source.write_bytes(b"first")
    fixed_time = 1_700_000_000_000_000_000
    os.utime(source, ns=(fixed_time, fixed_time))
    files = [(source, Path("source.py"))]

    layer_data = b"layer for first"
    layer_path = tmp_path / "layer.tar"
    layer_path.write_bytes(layer_data)
    cache.store_layer(files, _digest(layer_data), layer_path)

    source.write_bytes(b"other")
    os.utime(source, ns=(fixed_time, fixed_time))
    assert cache.get_layer(files) is None


def test_cache_identity_includes_workdir_and_reproducibility(tmp_path):
    cache = LayerCache(tmp_path / "cache")
    source = tmp_path / "source.py"
    source.write_text("content")
    files = [(source, Path("source.py"))]
    layer_path = tmp_path / "layer.tar"
    layer_path.write_bytes(b"layer")
    digest = _digest(layer_path.read_bytes())

    cache.store_layer(files, digest, layer_path, workdir="/app", reproducible=True)

    assert cache.get_layer(files, workdir="/srv", reproducible=True) is None
    assert cache.get_layer(files, workdir="/app", reproducible=False) is None
    assert cache.get_layer(files, workdir="/app", reproducible=True) is not None


def test_cache_identity_tracks_file_order_when_not_reproducible(tmp_path):
    cache = LayerCache(tmp_path / "cache")
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first")
    second.write_text("second")
    files = [(first, Path("first.py")), (second, Path("second.py"))]
    reordered = list(reversed(files))
    layer_path = tmp_path / "layer.tar"
    layer_path.write_bytes(b"layer")
    digest = _digest(layer_path.read_bytes())

    cache.store_layer(files, digest, layer_path, reproducible=False)
    assert cache.get_layer(reordered, reproducible=False) is None

    cache.store_layer(files, digest, layer_path, reproducible=True)
    assert cache.get_layer(reordered, reproducible=True) is not None


def test_cache_deduplicates_shared_blobs_during_eviction(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = LayerCache(cache_dir, max_size_mb=0.001)
    source = tmp_path / "source.py"
    source.write_text("content")
    files = [(source, Path("source.py"))]
    layer_data = b"x" * 600
    layer_path = tmp_path / "layer.tar"
    layer_path.write_bytes(layer_data)
    digest = _digest(layer_data)

    cache.store_layer(files, digest, layer_path, workdir="/app")
    cache.store_layer(files, digest, layer_path, workdir="/srv")

    stats = cache.get_stats()
    assert stats["entries"] == 2
    assert stats["total_size_mb"] == len(layer_data) / (1024 * 1024)
    assert cache.get_layer(files, workdir="/app") is not None
    assert cache.get_layer(files, workdir="/srv") is not None

    second_layer_data = b"y" * 600
    second_layer_path = tmp_path / "second-layer.tar"
    second_layer_path.write_bytes(second_layer_data)
    second_digest = _digest(second_layer_data)
    cache.store_layer(files, second_digest, second_layer_path, workdir="/opt")

    assert cache.get_layer(files, workdir="/app") is None
    assert cache.get_layer(files, workdir="/srv") is None
    assert cache.get_layer(files, workdir="/opt") is not None
    assert cache.get_stats()["total_size_mb"] == len(second_layer_data) / (1024 * 1024)


def test_cache_discards_missing_and_corrupt_blobs(tmp_path, caplog):
    cache_dir = tmp_path / "cache"
    cache = LayerCache(cache_dir)
    source = tmp_path / "source.py"
    source.write_text("content")
    files = [(source, Path("source.py"))]
    layer_data = b"verified layer"
    layer_path = tmp_path / "layer.tar"
    layer_path.write_bytes(layer_data)
    digest = _digest(layer_data)
    cache.store_layer(files, digest, layer_path)
    blob = cache_dir / "blobs" / "sha256" / digest.split(":", 1)[1]

    blob.write_bytes(b"corrupt")
    with caplog.at_level(logging.WARNING, logger="pycontainer.cache"):
        assert cache.get_layer(files) is None
    assert "discarded corrupt blob" in caplog.text
    assert cache.get_stats()["entries"] == 0

    cache.store_layer(files, digest, layer_path)
    blob.unlink()
    assert cache.get_layer(files) is None
    assert cache.get_stats()["entries"] == 0


def test_cache_rejects_malformed_index(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = LayerCache(cache_dir)
    source = tmp_path / "source.py"
    source.write_text("content")
    (cache_dir / "index.json").write_text("{")

    with pytest.raises(CacheIndexError, match="Malformed cache index"):
        cache.get_layer([(source, Path("source.py"))])


def test_cache_rejects_layer_with_wrong_digest(tmp_path):
    cache = LayerCache(tmp_path / "cache")
    source = tmp_path / "source.py"
    source.write_text("content")
    layer_path = tmp_path / "layer.tar"
    layer_path.write_bytes(b"layer")

    with pytest.raises(CacheIntegrityError, match="hashes to"):
        cache.store_layer([(source, Path("source.py"))], _digest(b"wrong"), layer_path)


def test_cache_materializes_only_verified_blobs(tmp_path):
    cache = LayerCache(tmp_path / "cache")
    source = tmp_path / "source.py"
    source.write_text("content")
    files = [(source, Path("source.py"))]
    layer_data = b"complete layer"
    layer_path = tmp_path / "layer.tar"
    layer_path.write_bytes(layer_data)
    digest = _digest(layer_data)
    cached_path = cache.store_layer(files, digest, layer_path)
    destination = tmp_path / "output" / "layer.tar"
    destination.parent.mkdir()
    destination.write_bytes(b"partial")

    cache.copy_verified_blob(cached_path, destination, digest)

    assert destination.read_bytes() == layer_data
    assert not list(destination.parent.glob(".*.tmp"))


def test_cache_index_updates_are_safe_across_processes(tmp_path):
    cache_dir = tmp_path / "cache"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    processes = []
    expected = []

    for index in range(3):
        source = tmp_path / f"source-{index}.py"
        source.write_text(f"source-{index}")
        layer_data = f"layer-{index}".encode()
        layer_path = tmp_path / f"layer-{index}.tar"
        layer_path.write_bytes(layer_data)
        digest = _digest(layer_data)
        relative = Path(f"source-{index}.py")
        expected.append(([(source, relative)], digest))
        process = context.Process(
            target=_store_cache_entry,
            args=(str(cache_dir), str(source), str(relative), str(layer_path), digest, barrier),
        )
        processes.append(process)

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("cache writer process did not finish")
        assert process.exitcode == 0

    cache = LayerCache(cache_dir)
    for files, digest in expected:
        assert cache.get_layer(files) is not None
        assert cache.get_layer(files)[0] == digest
    assert cache.get_stats()["entries"] == 3


def test_builder_defaults_cache_and_respects_disabled_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')")

    builder = ImageBuilder(BuildConfig(context_dir=str(context), output_dir=str(tmp_path / "out")))
    assert builder.cache.enabled
    assert builder.cache.cache_dir == tmp_path / ".pycontainer" / "cache"

    disabled = ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / "disabled"),
            use_cache=False,
        )
    )
    assert disabled.cache is None

    dry_run = ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / "dry-run"),
            dry_run=True,
        )
    )
    assert dry_run.cache is None


def test_builder_cache_identity_includes_workdir(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')")
    base_config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": [], "WorkingDir": "/"},
        "rootfs": {"type": "layers", "diff_ids": []},
    }

    def pull_base_image(self, layers_dir, os_name, arch):
        return [], base_config

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", pull_base_image)
    cache_dir = tmp_path / "cache"
    layer_digests = []
    for workdir in ("/app", "/srv"):
        config = BuildConfig(
            tag=f"test:{workdir[1:]}",
            context_dir=str(context),
            output_dir=str(tmp_path / f"output-{workdir[1:]}"),
            workdir=workdir,
            cache_dir=str(cache_dir),
            use_cache=True,
        )
        builder = ImageBuilder(config)
        builder.build()
        layer = builder.layers[-1]
        layer_digests.append(layer.digest)
        with tarfile.open(layer.tar_path) as archive:
            assert archive.getnames() == [f"{workdir[1:]}/app.py"]

    assert layer_digests[0] != layer_digests[1]


def test_cache_miss_reason_is_available_in_debug_logs(tmp_path, caplog):
    cache = LayerCache(tmp_path / "cache")
    source = tmp_path / "source.py"
    source.write_text("content")

    with caplog.at_level(logging.DEBUG, logger="pycontainer.cache"):
        assert cache.get_layer([(source, Path("source.py"))]) is None

    assert "Layer cache miss: no entry" in caplog.text
