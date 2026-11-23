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


_FIXED_NS = 1_700_000_000_000_000_000


def _link_or_skip(source, link):
    try:
        os.link(source, link)
    except (AttributeError, NotImplementedError, OSError) as error:
        pytest.skip(f"Hardlinks are unavailable here: {error}")


def _write_topology(context, linked, names=("a.txt", "b.txt")):
    first = context / names[0]
    for name in names:
        (context / name).unlink(missing_ok=True)
    first.write_text("same content")
    for name in names[1:]:
        if linked:
            _link_or_skip(first, context / name)
        else:
            (context / name).write_text("same content")
    for name in names:
        os.chmod(context / name, 0o644)
        os.utime(context / name, ns=(_FIXED_NS, _FIXED_NS))


def _build_app_layer(context, output, *, reproducible, cache_dir=None, workdir="/app"):
    builder = ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(output),
            workdir=workdir,
            reproducible=reproducible,
            use_cache=cache_dir is not None,
            cache_dir=str(cache_dir) if cache_dir is not None else None,
        )
    )
    layers_dir = output / "layers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    layer = builder._create_app_layer(layers_dir, builder.context_include_paths)
    with tarfile.open(layer.tar_path) as archive:
        entries = [(member.name, member.type, member.linkname) for member in archive.getmembers()]
    return layer.digest, entries


@pytest.mark.parametrize("reproducible", [True, False])
@pytest.mark.parametrize("first_linked", [False, True])
def test_cache_invalidates_when_hardlink_topology_changes(tmp_path, reproducible, first_linked):
    context = tmp_path / "context"
    context.mkdir()
    cache_dir = tmp_path / "cache"
    _write_topology(context, first_linked)
    first_digest, first_entries = _build_app_layer(
        context, tmp_path / "first", reproducible=reproducible, cache_dir=cache_dir
    )

    _write_topology(context, not first_linked)
    cached_digest, cached_entries = _build_app_layer(
        context, tmp_path / "cached", reproducible=reproducible, cache_dir=cache_dir
    )
    fresh_digest, fresh_entries = _build_app_layer(
        context, tmp_path / "fresh", reproducible=reproducible
    )

    linked_entries = [
        ("app/a.txt", tarfile.REGTYPE, ""),
        ("app/b.txt", tarfile.LNKTYPE, "app/a.txt"),
    ]
    independent_entries = [("app/a.txt", tarfile.REGTYPE, ""), ("app/b.txt", tarfile.REGTYPE, "")]
    assert first_entries == (linked_entries if first_linked else independent_entries)
    assert fresh_entries == (independent_entries if first_linked else linked_entries)
    assert cached_entries == fresh_entries
    assert cached_digest == fresh_digest
    assert cached_digest != first_digest

    repeated_digest, repeated_entries = _build_app_layer(
        context, tmp_path / "repeated", reproducible=reproducible, cache_dir=cache_dir
    )
    assert (repeated_digest, repeated_entries) == (fresh_digest, fresh_entries)


def test_cache_identity_for_equivalent_hardlink_topology_is_portable(tmp_path):
    cache = LayerCache(tmp_path / "cache")
    keys = []
    for name in ("one", "two"):
        context = tmp_path / name
        context.mkdir()
        _write_topology(context, True)
        files = [(context / "a.txt", Path("a.txt")), (context / "b.txt", Path("b.txt"))]
        keys.append(cache._compute_files_digest(files))
    assert keys[0] == keys[1]
    assert keys[0].startswith("v3:")


@pytest.mark.parametrize("workdir", ["/app", "/srv/app", "/"])
def test_cache_hardlink_probe_matches_emitted_tar_order(tmp_path, workdir):
    import pycontainer.cache as cache_module

    context = tmp_path / "context"
    context.mkdir()
    _write_topology(context, True, names=("a.txt", "b.txt", "c.txt"))
    cache = LayerCache(tmp_path / "cache")
    keys = set()
    for order in (("c.txt", "a.txt", "b.txt"), ("a.txt", "b.txt", "c.txt")):
        files = [(context / name, Path(name)) for name in order]
        with tarfile.open(tmp_path / "layer.tar", "w") as archive:
            for path, relative in files:
                archive.add(path, arcname=cache_module.app_layer_arcname(workdir, relative))
            emitted = cache_module.emitted_hardlinks(files, archive.getmembers())
        assert emitted == {name: order[0] for name in order[1:]}
        assert LayerCache._probe_hardlinks(files, workdir) == emitted
        keys.add(cache._compute_files_digest(files, workdir, reproducible=False))
    assert len(keys) == 2


@pytest.mark.parametrize("reproducible", [True, False])
def test_cache_ignores_hardlinks_to_unselected_files(tmp_path, reproducible):
    context = tmp_path / "context"
    context.mkdir()
    cache_dir = tmp_path / "cache"
    _write_topology(context, False, names=("a.txt",))
    first = _build_app_layer(context, tmp_path / "first", reproducible=reproducible, cache_dir=cache_dir)

    _link_or_skip(context / "a.txt", tmp_path / "outside.txt")
    cached = _build_app_layer(context, tmp_path / "cached", reproducible=reproducible, cache_dir=cache_dir)
    fresh = _build_app_layer(context, tmp_path / "fresh", reproducible=reproducible)

    assert cached == fresh == first
    assert fresh[1] == [("app/a.txt", tarfile.REGTYPE, "")]


def test_cache_matches_tar_when_platform_reports_no_inodes(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    cache_dir = tmp_path / "cache"
    _write_topology(context, False)
    _build_app_layer(context, tmp_path / "first", reproducible=True, cache_dir=cache_dir)
    _write_topology(context, True)

    real_lstat = os.lstat
    context_prefix = str(context)

    class _NoInode:
        def __init__(self, result):
            self._result = result

        def __getattr__(self, name):
            return 0 if name == "st_ino" else getattr(self._result, name)

    def lstat_without_inodes(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        return _NoInode(result) if os.fspath(path).startswith(context_prefix) else result

    monkeypatch.setattr(os, "lstat", lstat_without_inodes)
    cached = _build_app_layer(context, tmp_path / "cached", reproducible=True, cache_dir=cache_dir)
    fresh = _build_app_layer(context, tmp_path / "fresh", reproducible=True)

    assert cached == fresh
    assert [entry[1] for entry in fresh[1]] == [tarfile.REGTYPE, tarfile.REGTYPE]


def test_cache_keeps_symlinks_beside_hardlinks(tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    cache_dir = tmp_path / "cache"
    _write_topology(context, True)
    try:
        os.symlink("a.txt", context / "s.txt")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"Symlinks are unavailable here: {error}")

    first = _build_app_layer(context, tmp_path / "first", reproducible=True, cache_dir=cache_dir)
    cached = _build_app_layer(context, tmp_path / "cached", reproducible=True, cache_dir=cache_dir)
    fresh = _build_app_layer(context, tmp_path / "fresh", reproducible=True)

    assert first == cached == fresh
    assert fresh[1] == [
        ("app/a.txt", tarfile.REGTYPE, ""),
        ("app/b.txt", tarfile.LNKTYPE, "app/a.txt"),
        ("app/s.txt", tarfile.SYMTYPE, "a.txt"),
    ]


def test_hardlinked_build_layers_match_rootfs_diff_ids(tmp_path, monkeypatch):
    import json

    context = tmp_path / "context"
    context.mkdir()
    _write_topology(context, False)
    base_config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": [], "WorkingDir": "/"},
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, layers_dir, os_name, arch: ([], base_config)
    )

    def build(name):
        output = tmp_path / name
        ImageBuilder(
            BuildConfig(
                tag="test:links",
                context_dir=str(context),
                output_dir=str(output),
                cache_dir=str(tmp_path / "cache"),
            )
        ).build()
        blobs = output / "blobs" / "sha256"
        index = json.loads((output / "index.json").read_text())
        manifest = json.loads((blobs / index["manifests"][0]["digest"].split(":")[1]).read_text())
        config = json.loads((blobs / manifest["config"]["digest"].split(":")[1]).read_text())
        layers = manifest["layers"]
        assert [layer["digest"] for layer in layers] == config["rootfs"]["diff_ids"]
        for layer in layers:
            blob = blobs / layer["digest"].split(":")[1]
            assert blob.stat().st_size == layer["size"]
            assert _digest(blob.read_bytes()) == layer["digest"]
        with tarfile.open(blobs / layers[-1]["digest"].split(":")[1]) as archive:
            return layers[-1]["digest"], [(m.name, m.type) for m in archive.getmembers()]

    independent = build("independent")
    _write_topology(context, True)
    linked = build("linked")
    assert linked[0] != independent[0]
    assert linked[1] == [("app/a.txt", tarfile.REGTYPE), ("app/b.txt", tarfile.LNKTYPE)]
