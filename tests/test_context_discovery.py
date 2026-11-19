import json
import tarfile
from pathlib import Path

import pytest

from pycontainer.config import BuildConfig
from pycontainer.framework import (
    _find_fastapi_app,
    apply_framework_defaults,
    detect_framework,
)
from pycontainer import fs_utils
from pycontainer.fs_utils import context_exclude_paths, iter_files
from pycontainer.project import default_include_paths, find_dependencies


def test_context_scan_prunes_generated_and_test_paths(tmp_path):
    context = tmp_path / "context"
    (context / "src").mkdir(parents=True)
    (context / "tests" / "fixtures").mkdir(parents=True)
    (context / "fixtures" / "dependency").mkdir(parents=True)
    (context / ".venv" / "lib" / "site-packages").mkdir(parents=True)
    (context / ".git").mkdir()
    (context / "artifacts" / "nested").mkdir(parents=True)
    (context / "cache").mkdir()
    (context / "src" / "main.py").write_text("print('app')")
    (context / "tests" / "fixtures" / "example.py").write_text("fixture")
    (context / "fixtures" / "dependency" / "example.py").write_text("fixture")
    (context / ".venv" / "lib" / "site-packages" / "dependency.py").write_text(
        "dependency"
    )
    (context / ".git" / "config").write_text("git")
    (context / "artifacts" / "nested" / "image.json").write_text("image")
    (context / "cache" / "layer").write_text("cache")

    excludes = context_exclude_paths(
        context,
        output_dir=context / "artifacts",
        cache_dir=context / "cache",
    )
    files = list(iter_files(context, ["."], exclude_paths=excludes))

    assert [relative.as_posix() for _, relative in files] == ["src/main.py"]
    included = list(iter_files(context, ["."], exclude_defaults=False))
    included_paths = {relative.as_posix() for _, relative in included}
    assert "tests/fixtures/example.py" in included_paths
    assert "fixtures/dependency/example.py" in included_paths
    assert ".venv/lib/site-packages/dependency.py" in included_paths
    assert "artifacts/nested/image.json" in included_paths


def test_excluded_directories_are_pruned_during_walk(tmp_path, monkeypatch):
    context = tmp_path / "context"
    (context / ".venv" / "lib").mkdir(parents=True)
    (context / "tests" / "fixtures").mkdir(parents=True)
    (context / "app.py").write_text("print('app')")
    (context / ".venv" / "lib" / "unused.py").write_text("unused")
    (context / "tests" / "fixtures" / "unused.py").write_text("unused")
    visited = []
    original_walk = fs_utils.os.walk

    def tracking_walk(*args, **kwargs):
        for root, directories, filenames in original_walk(*args, **kwargs):
            visited.append(Path(root).relative_to(context).as_posix())
            yield root, directories, filenames

    monkeypatch.setattr(fs_utils.os, "walk", tracking_walk)

    list(iter_files(context, ["."]))

    assert ".venv" not in visited
    assert "tests" not in visited


def test_exclude_glob_matches_and_prunes_subtree(tmp_path, monkeypatch):
    context = tmp_path / "context"
    (context / "fixtures" / "nested").mkdir(parents=True)
    (context / "app.py").write_text("print('app')")
    (context / "fixtures" / "nested" / "sample.py").write_text("fixture")
    visited = []
    original_walk = fs_utils.os.walk

    def tracking_walk(*args, **kwargs):
        for root, directories, filenames in original_walk(*args, **kwargs):
            visited.append(Path(root).relative_to(context).as_posix())
            yield root, directories, filenames

    monkeypatch.setattr(fs_utils.os, "walk", tracking_walk)

    files = list(iter_files(context, ["."], exclude_paths=["fixtures/**"]))

    assert [relative.as_posix() for _, relative in files] == ["app.py"]
    assert "fixtures" not in visited


def test_overlapping_include_paths_are_deduplicated(tmp_path):
    context = tmp_path / "context"
    (context / "src").mkdir(parents=True)
    (context / "src" / "app.py").write_text("print('app')")

    files = list(iter_files(context, [".", "src", "src/app.py"]))

    assert [relative.as_posix() for _, relative in files] == ["src/app.py"]


def test_custom_exclude_paths_are_context_root_relative(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "package" / "src").mkdir(parents=True)
    (tmp_path / "src" / "app.py").write_text("root")
    (tmp_path / "package" / "src" / "app.py").write_text("nested")

    files = list(iter_files(tmp_path, ["."], exclude_paths=["src"]))

    assert [relative.as_posix() for _, relative in files] == [
        "package/src/app.py"
    ]


@pytest.mark.parametrize("include_path", ["../outside", "C:\\outside"])
def test_context_include_paths_cannot_escape_the_context(tmp_path, include_path):
    with pytest.raises(ValueError, match="must be relative and stay inside"):
        list(iter_files(tmp_path, [include_path]))


def test_flat_app_module_is_included_with_pyproject(tmp_path):
    (tmp_path / "app.py").write_text("print('app')")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'flat-app'\n")
    (tmp_path / "test_app.py").write_text("from fastapi import FastAPI")

    assert default_include_paths(tmp_path) == ["app.py", "pyproject.toml"]


def test_default_paths_skip_explicitly_excluded_source_directory(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('src')")
    (tmp_path / "app.py").write_text("print('app')")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n")

    assert default_include_paths(tmp_path, exclude_paths=["src"]) == [
        "app.py",
        "pyproject.toml",
    ]


def test_framework_detection_ignores_test_fixtures(tmp_path):
    (tmp_path / "app.py").write_text("print('app')")
    fixture_dir = tmp_path / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "fastapi_example.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()"
    )

    assert detect_framework(tmp_path) is None


def test_configured_entrypoint_takes_precedence_over_detected_framework(tmp_path):
    (tmp_path / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()"
    )
    config = BuildConfig(entrypoint=["python", "-m", "app"])
    result = apply_framework_defaults(config, tmp_path)

    assert result.entrypoint == ["python", "-m", "app"]
    assert result.exposed_ports == [8000]
    assert result.labels["framework"] == "fastapi"


def test_framework_detector_source_literals_are_not_applications(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'builder'\n")
    (tmp_path / "framework.py").write_text(
        'if "from fastapi import FastAPI" in content:\n'
        '    return "from flask import Flask"\n'
    )

    assert detect_framework(tmp_path) is None


def test_fastapi_module_name_uses_portable_unicode_path_parts(tmp_path):
    source = tmp_path / "src" / "über" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text("from fastapi import FastAPI\napp = FastAPI()")

    assert _find_fastapi_app(source, tmp_path) == "src.über.api:app"
    assert detect_framework(tmp_path)[1][1] == "src.über.api:app"


def test_build_output_and_cache_are_excluded_but_requested_venv_deps_are_staged(
    tmp_path,
):
    context = tmp_path / "context"
    (context / ".venv" / "lib" / "python3.11" / "site-packages").mkdir(
        parents=True
    )
    (context / "artifacts").mkdir()
    (context / "cache").mkdir()
    (context / "app.py").write_text("print('app')")
    (context / ".venv" / "lib" / "python3.11" / "site-packages" / "pkg.py").write_text(
        "package"
    )
    (context / "artifacts" / "old-image").write_text("old")
    (context / "cache" / "old-layer").write_text("old")

    config = BuildConfig(
        context_dir=str(context),
        output_dir=str(context / "artifacts"),
        cache_dir=str(context / "cache"),
        use_cache=True,
        include_deps=True,
    )
    from pycontainer.builder import ImageBuilder

    builder = ImageBuilder(config)
    files = list(
        iter_files(
            context,
            builder.context_include_paths,
            exclude_paths=builder.context_excludes,
        )
    )
    dependencies = find_dependencies(
        context,
        config.requirements_file,
        exclude_paths=builder.dependency_excludes,
    )
    layer_dir = tmp_path / "layers"
    layer_dir.mkdir()
    dependency_layer = builder._create_deps_layer(layer_dir)
    layer = builder._create_app_layer(layer_dir, builder.context_include_paths)

    with tarfile.open(dependency_layer.tar_path) as archive:
        dependency_names = archive.getnames()
    with tarfile.open(layer.tar_path) as archive:
        names = archive.getnames()

    assert [relative.as_posix() for _, relative in files] == ["app.py"]
    assert [relative.as_posix() for _, relative in dependencies] == [
        ".venv/lib/python3.11/site-packages/pkg.py"
    ]
    assert dependency_names == ["app/.python/site-packages/pkg.py"]
    assert names == ["app/app.py"]
    cache_entries = json.loads((context / "cache" / "index.json").read_text())
    assert [entry["source_files"] for entry in cache_entries.values()] == [["app.py"]]
    assert [
        relative.as_posix()
        for _, relative in iter_files(
            context,
            builder.context_include_paths,
            exclude_paths=builder.context_excludes,
        )
    ] == ["app.py"]


def test_dependency_requirements_path_cannot_escape_context(tmp_path):
    with pytest.raises(ValueError, match="must be relative and stay inside"):
        find_dependencies(tmp_path, "../requirements.txt")
