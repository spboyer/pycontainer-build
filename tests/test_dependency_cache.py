"""Hash-pinned dependency layer reuse from the local cache (issue #144)."""
import base64
import csv
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import zipfile
from importlib import metadata
from pathlib import Path

import pytest

import pycontainer.builder as builder_module
import pycontainer.dependency_cache as dependency_cache
import pycontainer.dependencies as dependencies
from pycontainer.builder import ImageBuilder
from pycontainer.cache import CacheIndexError, LayerCache
from pycontainer.config import BuildConfig
from pycontainer.dependency_cache import (
    DependencyCacheMiss,
    decode_inventory,
    dependency_cache_key,
    encode_inventory,
    parse_pip_config,
)

from test_dependency_staging import BASE_CONFIG, _write_wheel
from test_lockfiles import _named_wheel, _write_uv_project, indexes  # noqa: F401 - pytest fixture

SECRET = "hunter2-secret"


@pytest.fixture(autouse=True)
def _no_ambient_pip(monkeypatch):
    for name in [name for name in os.environ if name.upper().startswith("PIP_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setattr(dependency_cache, "ambient_pip_settings", lambda: [])


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _project(root, requirements=None):
    context = root / "context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "app.py").write_text("print('app')\n")
    wheel = _write_wheel(context / "wheelhouse")
    (context / "requirements.txt").write_text(
        requirements
        or f"--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0 --hash=sha256:{_sha(wheel)}\n"
    )
    return context


def _builder(tmp_path, monkeypatch, context, name, cache_dir=None, **options):
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, layers_dir, os_name, arch: ([], BASE_CONFIG)
    )
    options.setdefault("include_deps", True)
    options.setdefault("generate_sbom", True)
    return ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / name),
            cache_dir=str(cache_dir or tmp_path / "cache"),
            **options,
        )
    )


def _forbid_install(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("a proven cache hit must not run pip")

    monkeypatch.setattr(builder_module, "install_requirements", fail)
    monkeypatch.setattr(builder_module, "install_locked_requirements", fail)


def _count_installs(monkeypatch):
    calls = []
    original = builder_module.install_requirements

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(builder_module, "install_requirements", counted)
    return calls


def _image(builder):
    output = Path(builder.config.output_dir)
    config = json.loads(
        (output / "blobs" / "sha256" / builder.config_digest.split(":", 1)[1]).read_text()
    )
    sbom = json.loads((output / "sbom.json").read_text())
    return (
        builder.layers[0].digest,
        config["rootfs"]["diff_ids"],
        builder.config_digest,
        [(package["name"], package["versionInfo"]) for package in sbom["packages"]],
    )


def _dependency_keys(cache_dir):
    index_path = Path(cache_dir) / "index.json"
    if not index_path.exists():
        return {}
    index = json.loads(index_path.read_text())
    return {key: value for key, value in index.items() if key.startswith("deps-")}


def test_hash_pinned_requirements_hit_reuses_identical_layer_without_pip(
    tmp_path, monkeypatch, capsys
):
    context = _project(tmp_path)
    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    assert "Dependency layer cache miss: no verified entry" in capsys.readouterr().out

    _forbid_install(monkeypatch)
    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()

    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(second) == _image(first)
    assert _image(second)[3] == [("fixture-dep", "1.0")]
    assert Path(second.layers[0].tar_path).read_bytes() == Path(first.layers[0].tar_path).read_bytes()


def test_locked_build_hit_makes_no_index_requests(tmp_path, monkeypatch, indexes):  # noqa: F811
    context = tmp_path / "context"
    wheel = _write_wheel(context / "wheelhouse")
    recorded = indexes(wheel)
    _write_uv_project(context, wheel, registry=recorded.url)
    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    requests = list(recorded.requests)
    assert requests

    _forbid_install(monkeypatch)
    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()

    assert recorded.requests == requests
    assert _image(second) == _image(first)


def test_hash_pinned_target_markers_reuse_only_the_matching_architecture(
    tmp_path, monkeypatch, capsys
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    wheelhouse = context / "wheelhouse"
    root = _named_wheel(
        wheelhouse,
        "marker-root",
        requires=[
            'marker-amd==1.0; platform_machine == "x86_64"',
            'marker-arm==1.0; platform_machine == "aarch64"',
        ],
    )
    amd = _named_wheel(wheelhouse, "marker-amd")
    arm = _named_wheel(wheelhouse, "marker-arm")
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\n"
        f"marker-root==1.0 --hash=sha256:{_sha(root)}\n"
        f'marker-amd==1.0; platform_machine == "x86_64" --hash=sha256:{_sha(amd)}\n'
        f'marker-arm==1.0; platform_machine == "aarch64" --hash=sha256:{_sha(arm)}\n'
    )

    x86 = _builder(tmp_path, monkeypatch, context, "x86", platform="linux/amd64")
    x86.build()
    aarch64 = _builder(tmp_path, monkeypatch, context, "arm", platform="linux/arm64")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], {**BASE_CONFIG, "architecture": arch}),
    )
    aarch64.build()

    assert set(_image(x86)[3]) == {("marker-root", "1.0"), ("marker-amd", "1.0")}
    assert set(_image(aarch64)[3]) == {("marker-root", "1.0"), ("marker-arm", "1.0")}
    assert x86.layers[0].digest != aarch64.layers[0].digest
    capsys.readouterr()

    _forbid_install(monkeypatch)
    hit = _builder(tmp_path, monkeypatch, context, "arm-hit", platform="linux/arm64")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], {**BASE_CONFIG, "architecture": arch}),
    )
    hit.build()

    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(hit) == _image(aarch64)


KEY_DEFAULTS = dict(
    source_kind="requirements",
    os_name="linux",
    arch="amd64",
    python_version="3.11",
    python_full_version="3.11.8",
    base_image="python:3.11-slim",
    base_config=BASE_CONFIG,
    base_image_tag_verified=True,
    workdir="/app",
    reproducible=True,
)


def _key(context, **overrides):
    options = {**KEY_DEFAULTS, "context_dir": context, "requirements": context / "requirements.txt"}
    options.update(overrides)
    return dependency_cache_key(**options).layer


def _nested_project(tmp_path):
    context = _project(tmp_path)
    digest = _sha(next((context / "wheelhouse").iterdir()))
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\n-r nested/base.txt\n-c constraints.txt\n"
    )
    (context / "nested").mkdir()
    (context / "nested" / "base.txt").write_text(f"fixture-dep==1.0 --hash=sha256:{digest}\n")
    (context / "constraints.txt").write_text("fixture-dep==1.0\n")
    return context


def _append(path, text):
    path.write_text(path.read_text() + text)


INPUT_CHANGES = {
    "nested requirements": lambda c, m: _append(
        c / "nested" / "base.txt", "other-dep==2.0 --hash=sha256:" + "b" * 64 + "\n"
    ),
    "constraint file": lambda c, m: (c / "constraints.txt").write_text("fixture-dep===1.0\n"),
    "find-links wheel content": lambda c, m: _append(c / "wheelhouse" / "extra.whl", "x"),
    "pip version": lambda c, m: m.setattr(
        dependency_cache.metadata, "version", lambda name: "0.0-test"
    ),
    "ambient index": lambda c, m: m.setattr(
        dependency_cache,
        "ambient_pip_settings",
        lambda: [[":env:", "index-url", "https://other.example.invalid/simple"]],
    ),
    "installer backend": lambda c, m: m.setattr(dependency_cache, "INSTALLER_BACKEND", "other"),
}


@pytest.mark.parametrize("change", sorted(INPUT_CHANGES))
def test_every_layer_affecting_input_changes_the_key(tmp_path, monkeypatch, change):
    context = _nested_project(tmp_path)
    (context / "wheelhouse" / "extra.whl").write_text("")
    before = _key(context)

    INPUT_CHANGES[change](context, monkeypatch)

    assert _key(context) != before


@pytest.mark.parametrize(
    "overrides",
    [
        {"arch": "arm64"},
        {"python_version": "3.12", "python_full_version": "3.12.1"},
        {"python_full_version": "3.11.9"},
        {"workdir": "/srv"},
        {"base_image": "python:3.11-alpine"},
        {
            "base_image": "example.invalid/custom:1",
            "base_config": {
                "config": {
                    "Env": [
                        "PYTHON_IMPLEMENTATION=CPython",
                        "PYTHON_VERSION=3.11.8",
                        "PYTHON_LIBC=musl-1.2",
                    ]
                }
            },
        },
    ],
)
def test_target_tag_set_python_and_workdir_change_the_key(tmp_path, overrides):
    context = _project(tmp_path)

    assert _key(context, **overrides) != _key(context)


def test_relocated_context_keeps_the_key(tmp_path):
    first = _nested_project(tmp_path / "a")
    second = tmp_path / "b" / "elsewhere"
    shutil.copytree(first, second)

    assert _key(first) == _key(second)


def _lock_key(url, text="fixture-dep==1.0 --hash=sha256:" + "a" * 64):
    return dependency_cache_key(
        **{**KEY_DEFAULTS, "source_kind": "lock"},
        context_dir=Path("."),
        requirement_sets=[(url, f"--index-url {url}\n{text}\n")],
        lock_name="uv.lock",
    ).layer


@pytest.mark.parametrize("source_kind", ["requirements", "lock"])
def test_cache_key_tracks_the_installer_target_marker_environment(
    tmp_path, monkeypatch, source_kind
):
    context = _project(tmp_path)
    key = (
        (lambda: _key(context))
        if source_kind == "requirements"
        else (lambda: _lock_key("https://pypi.org/simple"))
    )
    before = key()
    original = dependencies._target_marker_environment

    def changed(*args, **kwargs):
        return {**original(*args, **kwargs), "implementation_version": "3.11.99"}

    monkeypatch.setattr(dependency_cache, "_target_marker_environment", changed)

    assert key() != before


def test_cache_does_not_reuse_the_pre_target_marker_backend(tmp_path, monkeypatch):
    context = _project(tmp_path)
    current_backend = dependency_cache.INSTALLER_BACKEND
    monkeypatch.setattr(dependency_cache, "INSTALLER_BACKEND", "pip-target-v1")
    legacy_key = _key(context)
    cache = LayerCache(tmp_path / "cache")
    cache.store_bytes(legacy_key, b"legacy dependency layer", ["requirements.txt"])

    monkeypatch.setattr(dependency_cache, "INSTALLER_BACKEND", current_backend)

    assert cache.get_entry(_key(context)) is None


def test_lock_selection_is_recomputed_for_a_different_target(tmp_path, monkeypatch):
    context = _write_uv_project(tmp_path / "context")
    for path in (context / "pyproject.toml", context / "uv.lock"):
        path.write_text(
            path.read_text().replace("sys_platform == 'win32'", "platform_machine == 'aarch64'")
        )
    builder = _builder(tmp_path, monkeypatch, context, "out")

    amd = builder._lock_selection(BASE_CONFIG, "linux", "amd64", base_image_tag_verified=True)
    arm = builder._lock_selection(
        {**BASE_CONFIG, "architecture": "arm64"},
        "linux", "arm64", base_image_tag_verified=True,
    )

    assert {package.name for package in amd.packages} == {"fixture-dep"}
    assert {package.name for package in arm.packages} == {"fixture-dep", "colorama"}


def test_unknown_full_version_cannot_reuse_known_marker_environment(
    tmp_path, monkeypatch, capsys
):
    context = _project(tmp_path)
    wheel = next((context / "wheelhouse").iterdir())
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\n"
        f'fixture-dep==1.0; python_full_version >= "3.11.1" --hash=sha256:{_sha(wheel)}\n'
    )
    _builder(tmp_path, monkeypatch, context, "known").build()
    existing = _dependency_keys(tmp_path / "cache")
    capsys.readouterr()

    unknown = _builder(tmp_path, monkeypatch, context, "unknown")
    unknown_config = {
        **BASE_CONFIG,
        "config": {**BASE_CONFIG["config"], "Env": ["PYTHON_VERSION=3.11"]},
    }
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], unknown_config),
    )
    with pytest.raises(ValueError, match="python_full_version"):
        unknown.build()

    assert "Dependency layer cache hit" not in capsys.readouterr().out
    assert _dependency_keys(tmp_path / "cache") == existing


def test_unverified_base_cannot_reuse_a_trusted_dependency_layer(
    tmp_path, monkeypatch, capsys
):
    context = _project(tmp_path)
    _builder(tmp_path, monkeypatch, context, "trusted").build()
    capsys.readouterr()
    _forbid_install(monkeypatch)

    unverified = _builder(
        tmp_path, monkeypatch, context, "unverified",
        base_image="python:3.11-slim@sha256:" + "a" * 64,
    )
    with pytest.raises(ValueError, match="Python ABI"):
        unverified.build()

    assert "Dependency layer cache hit" not in capsys.readouterr().out


def test_index_credentials_are_dropped_but_never_alias_query_routing():
    plain = _lock_key("https://pkgs.example.invalid/simple")

    assert _lock_key("https://${INDEX_USER}:${INDEX_TOKEN}@pkgs.example.invalid/simple") == plain
    assert _lock_key(f"https://user:{SECRET}@pkgs.example.invalid/simple") == plain
    assert _lock_key("https://pkgs.example.invalid/other/simple") != plain
    for url in (
        "https://pkgs.example.invalid/simple?",
        "https://pkgs.example.invalid/simple?feed=tenant-a",
        "https://pkgs.example.invalid/simple?feed=tenant-b",
        "https://pkgs.example.invalid/simple?token=${INDEX_TOKEN}",
    ):
        with pytest.raises(DependencyCacheMiss, match="query string") as miss:
            _lock_key(url)
        assert "tenant" not in str(miss.value) and "INDEX_TOKEN" not in str(miss.value)
    with pytest.raises(DependencyCacheMiss, match="placeholder in its host or path"):
        _lock_key("https://${INDEX_HOST}/simple")
    with pytest.raises(DependencyCacheMiss, match="not hash-pinned"):
        _lock_key("https://pkgs.example.invalid/simple", "fixture-dep==1.0")


def test_find_links_url_path_trailing_slashes_do_not_alias(tmp_path):
    context = _project(tmp_path)
    requirements = context / "requirements.txt"
    content = requirements.read_text().replace(
        "./wheelhouse", "https://pkgs.example.invalid/wheels"
    )
    requirements.write_text(content)
    without_slash = _key(context)
    requirements.write_text(content.replace("/wheels", "/wheels/"))

    assert _key(context) != without_slash


def test_utf16_file_used_as_requirement_and_constraint_is_checked_in_both_roles(tmp_path):
    context = _project(tmp_path)
    (context / "requirements.txt").write_text(
        "-r shared.txt\n-c shared.txt\n", encoding="utf-16"
    )
    shared = context / "shared.txt"
    shared.write_text("fixture-dep==1.0\n", encoding="utf-16")
    with pytest.raises(DependencyCacheMiss, match="not fully pinned"):
        _key(context)

    shared.write_text(
        "fixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-16"
    )
    original = _key(context)
    shared.write_text(
        "fixture-dep==1.0 --hash=sha256:" + "b" * 64 + "\n", encoding="utf-16"
    )
    assert _key(context) != original


@pytest.mark.parametrize(
    "requirements, reason",
    [
        ("fixture-dep==1.0\n", "not fully pinned"),
        ("fixture-dep>=1.0 --hash=sha256:" + "a" * 64 + "\n", "not fully pinned"),
        ("-e ./local\n", "not fully pinned"),
        ("--no-deps\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n", "--no-deps"),
        (
            "--index-url https://pkgs.example.invalid/simple?feed=a\n"
            "fixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "query string",
        ),
        (
            "--find-links https://pkgs.example.invalid/wheels?feed=a\n"
            "fixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "query string",
        ),
        (
            "--find-links file:///wheels\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "file: URL find-links",
        ),
        (
            "--index-url https://${HOST}/simple\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n",
            "placeholder in its host or path",
        ),
    ],
)
def test_unprovable_requirements_are_explicit_misses(tmp_path, requirements, reason):
    context = _project(tmp_path, requirements)

    with pytest.raises(DependencyCacheMiss, match=reason):
        _key(context)


def test_direct_url_constraints_are_misses(tmp_path):
    context = _project(tmp_path, "-c constraints.txt\nfixture-dep==1.0 --hash=sha256:" + "a" * 64 + "\n")
    (context / "constraints.txt").write_text("fixture-dep @ https://example.invalid/f.whl\n")

    with pytest.raises(DependencyCacheMiss, match="direct URL"):
        _key(context)


@pytest.mark.parametrize(
    "options, reason",
    [
        ({"reproducible": False}, "non-reproducible"),
        ({"source_kind": "venv"}, "virtualenv"),
    ],
)
def test_venv_and_non_reproducible_sources_are_misses(tmp_path, options, reason):
    with pytest.raises(DependencyCacheMiss, match=reason):
        _key(_project(tmp_path), **options)


def test_builder_reports_miss_reason_and_stores_nothing_for_unpinned(tmp_path, monkeypatch, capsys):
    context = _project(tmp_path, "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n")

    _builder(tmp_path, monkeypatch, context, "out").build()

    assert "Dependency layer cache miss: requirements are not fully pinned" in capsys.readouterr().out
    assert _dependency_keys(tmp_path / "cache") == {}


def _wheel_requiring(directory, dependency):
    path = directory / "needs_more-1.0-py3-none-any.whl"
    files = {
        "needs_more/__init__.py": b"",
        "needs_more-1.0.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: needs-more\nVersion: 1.0\nRequires-Dist: {dependency}\n\n"
        ).encode(),
        "needs_more-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow((name, f"sha256={digest}", len(content)))
    writer.writerow(("needs_more-1.0.dist-info/RECORD", "", ""))
    files["needs_more-1.0.dist-info/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_transitively_unpinned_closure_fails_without_storing(tmp_path, monkeypatch):
    context = _project(tmp_path)
    wheel = _wheel_requiring(context / "wheelhouse", "fixture-dep")
    (context / "requirements.txt").write_text(
        f"--no-index\n--find-links ./wheelhouse\nneeds-more==1.0 --hash=sha256:{_sha(wheel)}\n"
    )

    with pytest.raises(RuntimeError):
        _builder(tmp_path, monkeypatch, context, "out").build()

    assert _dependency_keys(tmp_path / "cache") == {}


def _layer_blob(cache_dir):
    key = next(key for key in _dependency_keys(cache_dir) if key.startswith("deps-v"))
    digest = json.loads((Path(cache_dir) / "index.json").read_text())[key]["digest"]
    return Path(cache_dir) / "blobs" / "sha256" / digest.split(":", 1)[1]


def _inventory_key(cache_dir):
    return next(key for key in _dependency_keys(cache_dir) if key.startswith("deps-inventory"))


def test_corrupt_layer_blob_is_discarded_and_rebuilt(tmp_path, monkeypatch, capsys):
    context = _project(tmp_path)
    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    _layer_blob(tmp_path / "cache").write_bytes(b"corrupt")
    installs = _count_installs(monkeypatch)
    capsys.readouterr()

    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()

    assert len(installs) == 1
    assert "no verified entry" in capsys.readouterr().out
    assert _image(second) == _image(first)


@pytest.mark.parametrize("damage", ["missing", "other-layer", "garbage"])
def test_missing_or_mismatched_inventory_is_a_miss(tmp_path, monkeypatch, damage):
    context = _project(tmp_path)
    _builder(tmp_path, monkeypatch, context, "first").build()
    cache = LayerCache(tmp_path / "cache")
    key = _inventory_key(tmp_path / "cache")
    if damage == "missing":
        index_path = tmp_path / "cache" / "index.json"
        index = json.loads(index_path.read_text())
        del index[key]
        index_path.write_text(json.dumps(index))
    else:
        content = (
            encode_inventory("sha256:" + "0" * 64, [("fixture-dep", "1.0")])
            if damage == "other-layer"
            else b"not json"
        )
        cache.store_bytes(key, content, [])
    installs = _count_installs(monkeypatch)

    _builder(tmp_path, monkeypatch, context, "second").build()

    assert len(installs) == 1


def test_decode_inventory_rejects_wrong_shapes():
    layer = "sha256:" + "1" * 64
    assert decode_inventory(encode_inventory(layer, [("a", "1")]), layer) == [("a", "1")]
    assert decode_inventory(b'{"version":1,"layer":"' + layer.encode() + b'","packages":[["a"]]}', layer) is None
    assert decode_inventory(b"\xff", layer) is None


def test_malformed_cache_index_fails_explicitly(tmp_path, monkeypatch):
    context = _project(tmp_path)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "index.json").write_text("{not json")

    with pytest.raises(CacheIndexError):
        _builder(tmp_path, monkeypatch, context, "out").build()


def test_interruption_between_layer_and_inventory_is_a_later_miss(tmp_path, monkeypatch):
    context = _project(tmp_path)

    def interrupted(self, key, content, source_files):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(LayerCache, "store_bytes", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _builder(tmp_path, monkeypatch, context, "first").build()
    installs = _count_installs(monkeypatch)

    second = _builder(tmp_path, monkeypatch, context, "second")
    second.build()
    _forbid_install(monkeypatch)
    third = _builder(tmp_path, monkeypatch, context, "third")
    third.build()

    assert len(installs) == 1
    assert _image(third) == _image(second)


def _store_dependency_entry(cache_dir, layer_path, digest, barrier):
    cache = LayerCache(Path(cache_dir))
    barrier.wait(timeout=20)
    cache.store_entry("deps-v1:sha256:" + "c" * 64, digest, Path(layer_path), ["requirements.txt"])
    cache.store_bytes(
        "deps-inventory-v1:sha256:" + "c" * 64,
        encode_inventory(digest, [("fixture-dep", "1.0")]),
        ["requirements.txt"],
    )


def test_concurrent_writers_publish_one_verified_entry(tmp_path):
    layer = tmp_path / "layer.tar"
    layer.write_bytes(b"dependency layer" * 1024)
    digest = f"sha256:{_sha(layer)}"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    processes = [
        context.Process(
            target=_store_dependency_entry, args=(str(tmp_path / "cache"), str(layer), digest, barrier)
        )
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0

    cache = LayerCache(tmp_path / "cache")
    assert cache.get_entry("deps-v1:sha256:" + "c" * 64)[0] == digest
    content = cache.get_bytes("deps-inventory-v1:sha256:" + "c" * 64, 1024)
    assert decode_inventory(content, digest) == [("fixture-dep", "1.0")]
    assert not list((tmp_path / "cache").glob(".entry-*"))


def test_disabled_cache_is_silent_and_relocated_cache_hits(tmp_path, monkeypatch, capsys):
    context = _project(tmp_path)
    _builder(tmp_path, monkeypatch, context, "off", use_cache=False).build()
    assert "Dependency layer cache" not in capsys.readouterr().out

    first = _builder(tmp_path, monkeypatch, context, "first")
    first.build()
    relocated_context = tmp_path / "moved" / "context"
    shutil.copytree(context, relocated_context)
    shutil.copytree(tmp_path / "cache", tmp_path / "moved-cache")
    _forbid_install(monkeypatch)
    capsys.readouterr()

    second = _builder(
        tmp_path, monkeypatch, relocated_context, "second", cache_dir=tmp_path / "moved-cache"
    )
    second.build()

    assert "Dependency layer cache hit" in capsys.readouterr().out
    assert _image(second) == _image(first)


def test_cache_metadata_and_output_never_contain_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INDEX_TOKEN", SECRET)
    monkeypatch.setattr(
        dependency_cache,
        "ambient_pip_settings",
        lambda: parse_pip_config(
            f":env:.extra-index-url='https://user:{SECRET}@ambient.example.invalid/simple'\n"
        ),
    )
    wheel_digest = _sha(next((_project(tmp_path) / "wheelhouse").iterdir()))
    context = _project(
        tmp_path,
        "--no-index\n--find-links ./wheelhouse\n"
        f"--extra-index-url https://user:{SECRET}@pkgs.example.invalid/simple\n"
        "--extra-index-url https://${INDEX_USER}:${INDEX_TOKEN}@pkgs.example.invalid/simple\n"
        f"fixture-dep==1.0 --hash=sha256:{wheel_digest}\n",
    )

    _builder(tmp_path, monkeypatch, context, "first").build()
    _forbid_install(monkeypatch)
    _builder(tmp_path, monkeypatch, context, "second").build()

    output = capsys.readouterr().out
    assert "Dependency layer cache hit" in output
    cache_dir = tmp_path / "cache"
    inventory = LayerCache(cache_dir).get_bytes(_inventory_key(cache_dir), 1024 * 1024)
    cache_text = (cache_dir / "index.json").read_text() + inventory.decode()
    for text in (output, cache_text):
        assert SECRET not in text and "INDEX_TOKEN" not in text and "user:" not in text


def test_parse_pip_config_keys_sanitized_settings_and_rejects_unprovable_ones():
    rows = parse_pip_config(
        f":env:.index-url='https://u:{SECRET}@Pkgs.Example.invalid/simple/'\n"
        "global.timeout='60'\n"
        ":env:.config-file='/etc/pip.conf'\n"
        "install.only-binary=':all:'\n"
        "freeze.all='true'\n"
    )
    assert rows == [
        [":env:", "index-url", "https://pkgs.example.invalid/simple/"],
        ["install", "only-binary", ":all:"],
    ]
    for line, reason in [
        (":env:.constraint='/tmp/c.txt'", "constraint is not provably keyed"),
        ("global.find-links='/wheels'", "local files"),
        ("global.index-url='https://example.invalid/simple?feed=a'", "query string"),
        ("global.only-binary='${PKGS}'", "cannot be keyed"),
        ("not a config line", "could not be parsed"),
        ("global.index-url=unquoted", "could not be parsed"),
    ]:
        with pytest.raises(DependencyCacheMiss, match=reason) as miss:
            parse_pip_config(line + "\n")
        assert "/tmp" not in str(miss.value) and "feed" not in str(miss.value)


def test_real_ambient_pip_config_is_read_and_sanitized(monkeypatch):
    monkeypatch.undo()
    for name in [name for name in os.environ if name.upper().startswith("PIP_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("PIP_INDEX_URL", f"https://u:{SECRET}@pkgs.example.invalid/simple")

    rows = dependency_cache.ambient_pip_settings()

    assert [":env:", "index-url", "https://pkgs.example.invalid/simple"] in rows
    assert SECRET not in json.dumps(rows)
    assert metadata.version("pip")
