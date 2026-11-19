"""Hatch plugin tests."""

from importlib.metadata import entry_points
from pathlib import Path
import shutil
import subprocess
import os
import sys
from types import SimpleNamespace

import pycontainer.builder
from hatch_pycontainer.hooks import ContainerBuildHook, hatch_register_build_hook


def test_hatch_build_hook_entry_point_is_registered():
    registered = entry_points(group="hatch")

    assert any(
        entry.name == "pycontainer"
        and entry.value == "hatch_pycontainer.hooks"
        for entry in registered
    )
    assert hatch_register_build_hook() is ContainerBuildHook


def test_hatch_cli_loads_plugin_and_builds_fixture(tmp_path):
    fixture_source = Path(__file__).parent / "fixtures" / "hatch-project"
    fixture = tmp_path / "hatch-project"
    shutil.copytree(fixture_source, fixture)
    plugin_root = Path(__file__).resolve().parents[1]
    core_root = plugin_root.parents[1]
    pyproject_path = fixture / "pyproject.toml"
    pyproject = pyproject_path.read_text(encoding="utf-8")
    pyproject = pyproject.replace(
        'requires = ["hatchling"]',
        "requires = ["
        f'"hatchling", "pycontainer-build @ {core_root.as_uri()}", '
        f'"hatch-pycontainer @ {plugin_root.as_uri()}"'
        "]",
    )
    pyproject_path.write_text(pyproject, encoding="utf-8")
    hatch = shutil.which("hatch") or str(
        Path(sys.executable).parent / ("hatch.exe" if os.name == "nt" else "hatch")
    )

    result = subprocess.run(
        [hatch, "build", "--target", "wheel"],
        cwd=fixture,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Build Plan for sample-app:1.2.3" in result.stdout


def test_hook_maps_build_and_push_options(monkeypatch, tmp_path):
    builders = []

    class FakeBuilder:
        def __init__(self, config):
            self.config = config
            self.push_registry = None
            builders.append(self)

        def build(self):
            return "dist/image"

        def push(self, registry_url=None):
            self.push_registry = registry_url

    monkeypatch.setattr(pycontainer.builder, "ImageBuilder", FakeBuilder)
    labels = {"custom.label": "kept"}
    config = {
        "tag": "sample-app:1.2.3",
        "base-image": "python:3.12-slim",
        "registry": "ghcr.io/example/sample-app:stable",
        "push": True,
        "include-deps": False,
        "sbom": "spdx",
        "dry-run": True,
        "no-cache": True,
        "env": {"APP_MODE": "test"},
        "labels": labels,
    }
    build_data = {}

    ContainerBuildHook(
        str(tmp_path),
        config,
        None,
        SimpleNamespace(name="sample-app", version="1.2.3"),
        str(tmp_path),
        "wheel",
    ).initialize("1.2.3", build_data)

    builder = builders[0]
    assert builder.config.tag == "sample-app:1.2.3"
    assert builder.config.context_dir == str(tmp_path)
    assert builder.config.base_image == "python:3.12-slim"
    assert builder.config.include_deps is False
    assert builder.config.generate_sbom == "spdx"
    assert builder.config.dry_run is True
    assert builder.config.use_cache is False
    assert builder.config.env == {"APP_MODE": "test"}
    assert builder.config.labels["custom.label"] == "kept"
    assert builder.push_registry == "ghcr.io/example/sample-app:stable"
    assert labels == {"custom.label": "kept"}
    assert build_data["container_image"] == {
        "tag": "sample-app:1.2.3",
        "base_image": "python:3.12-slim",
        "pushed": True,
    }


def test_skip_does_not_create_an_image(monkeypatch, tmp_path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("builder should not be created for a skipped hook")

    monkeypatch.setattr(pycontainer.builder, "ImageBuilder", fail_if_called)

    ContainerBuildHook(
        str(tmp_path),
        {"skip": True},
        None,
        None,
        str(tmp_path),
        "wheel",
    ).initialize("1.0", {})
