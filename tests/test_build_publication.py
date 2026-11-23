import concurrent.futures
import hashlib
import json
import tarfile
import threading
from pathlib import Path

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig


BASE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {"Env": [], "WorkingDir": "/"},
    "rootfs": {"type": "layers", "diff_ids": []},
}


def _create_builder(tmp_path, monkeypatch, *, use_cache, output_dir=None):
    context = tmp_path / "context"
    context.mkdir(exist_ok=True)
    (context / "app.py").write_text("print('hello')\n")
    (context / "requirements.txt").write_text("# dependencies staged from fixture venv\n")
    site_packages = context / ".venv" / "lib" / "python3.11" / "site-packages"
    (site_packages / "example-1.0.dist-info").mkdir(parents=True, exist_ok=True)
    (site_packages / "example.py").write_text("value = 1\n")
    (site_packages / "example-1.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: example\nVersion: 1.0\n\n"
    )
    (site_packages / "example-1.0.dist-info" / "WHEEL").write_text(
        "Root-Is-Purelib: true\nTag: py3-none-any\n"
    )

    monkeypatch.setattr(
        ImageBuilder,
        "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], BASE_CONFIG),
    )
    return ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(output_dir or tmp_path / "output"),
            include_paths=["app.py", "requirements.txt"],
            include_deps=True,
            base_image="python:3.11-slim-bookworm",
            use_cache=use_cache,
            cache_dir=str(tmp_path / "cache") if use_cache else None,
        )
    )


@pytest.mark.parametrize("use_cache", [False, True])
def test_repeated_builds_reuse_app_and_dependency_blobs(
    tmp_path, monkeypatch, use_cache
):
    builder = _create_builder(tmp_path, monkeypatch, use_cache=use_cache)
    output = Path(builder.config.output_dir)

    builder.build()
    initial_digests = [layer.digest for layer in builder.layers]
    assert len(initial_digests) == 2
    builder.build()

    assert [layer.digest for layer in builder.layers] == initial_digests
    index = json.loads((output / "index.json").read_text())
    assert len(index["manifests"]) == 1
    for layer in builder.layers:
        blob = output / "blobs" / "sha256" / layer.digest.split(":", 1)[1]
        assert hashlib.sha256(blob.read_bytes()).hexdigest() == layer.digest.split(":", 1)[1]
        assert blob.stat().st_size == layer.size
    assert not list((output / "blobs" / "sha256").glob(".*.tmp"))


def test_mismatching_existing_blob_is_not_overwritten(tmp_path, monkeypatch):
    builder = _create_builder(tmp_path, monkeypatch, use_cache=False)
    output = Path(builder.config.output_dir)
    builder.build()
    original_index = (output / "index.json").read_bytes()
    app_layer = builder.layers[-1]
    blob = output / "blobs" / "sha256" / app_layer.digest.split(":", 1)[1]
    blob.write_bytes(b"corrupt existing content")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        builder.build()

    assert blob.read_bytes() == b"corrupt existing content"
    assert (output / "index.json").read_bytes() == original_index
    assert not list((output / "blobs" / "sha256").glob(".*.tmp"))


@pytest.mark.parametrize("layer_kind", ["app", "deps"])
def test_failed_layer_write_cleans_only_its_staging_file(
    tmp_path, monkeypatch, layer_kind
):
    builder = _create_builder(tmp_path, monkeypatch, use_cache=False)
    builder.config.reproducible = False
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    sentinel = layers_dir / f".{layer_kind}-layer-other.tmp"
    sentinel.write_text("owned by another build")

    def interrupt_write(*args, **kwargs):
        raise OSError("simulated interrupted layer write")

    monkeypatch.setattr(tarfile.TarFile, "add", interrupt_write)
    with pytest.raises(OSError, match="simulated interrupted"):
        if layer_kind == "app":
            builder._create_app_layer(layers_dir, ["app.py"])
        else:
            builder._create_deps_layer(layers_dir)

    assert sentinel.read_text() == "owned by another build"
    assert list(layers_dir.glob(f".{layer_kind}-layer-*.tmp")) == [sentinel]


def test_concurrent_build_to_same_output_is_rejected_without_partial_index(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n")
    output = tmp_path / "output"
    entered_base_pull = threading.Event()
    finish_base_pull = threading.Event()

    def pause_base_pull(self, layers_dir, os_name, arch):
        entered_base_pull.set()
        if not finish_base_pull.wait(timeout=10):
            raise TimeoutError("test did not release the first build")
        return [], BASE_CONFIG

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", pause_base_pull)
    config = BuildConfig(
        context_dir=str(context),
        output_dir=str(output),
        include_paths=["app.py"],
        use_cache=False,
    )
    first_builder = ImageBuilder(config)
    second_builder = ImageBuilder(config)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        first_build = executor.submit(first_builder.build)
        try:
            assert entered_base_pull.wait(timeout=10)
            with pytest.raises(RuntimeError, match="concurrent writes"):
                second_builder.build()
            assert not (output / "index.json").exists()
            assert not list((output / "blobs" / "sha256").glob("*.json"))
        finally:
            finish_base_pull.set()
        assert first_build.result(timeout=10) == config.tag
