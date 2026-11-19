"""Poetry plugin tests."""

from importlib.metadata import entry_points
from pathlib import Path
import shutil
import subprocess
import os
import sys
from types import SimpleNamespace

from poetry_pycontainer.plugin import ContainerBuildCommand, PycontainerPlugin


def test_poetry_application_plugin_entry_point_is_registered():
    registered = entry_points(group="poetry.application.plugin")

    registered_plugin = next(
        entry
        for entry in registered
        if entry.name == "pycontainer"
        and entry.value == "poetry_pycontainer.plugin:PycontainerPlugin"
    )
    assert registered_plugin.load() is PycontainerPlugin


def test_poetry_cli_loads_plugin_and_builds_fixture(tmp_path):
    fixture_source = Path(__file__).parent / "fixtures" / "poetry-project"
    fixture = tmp_path / "poetry-project"
    shutil.copytree(fixture_source, fixture)
    poetry = shutil.which("poetry") or str(
        Path(sys.executable).parent / ("poetry.exe" if os.name == "nt" else "poetry")
    )

    result = subprocess.run(
        [
            poetry,
            "build-container",
            "--dry-run",
            "--tag",
            "sample-app:test",
        ],
        cwd=fixture,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Build Plan for sample-app:test" in result.stdout
    assert "Base Image: python:3.12-slim" in result.stdout


def test_command_maps_build_and_push_options(monkeypatch, tmp_path):
    import pycontainer.builder

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

    package = SimpleNamespace(
        name="sample-app",
        version="1.2.3",
        description="Sample application",
        authors=["Example Author"],
    )
    config_labels = {"custom.label": "kept"}
    pyproject = SimpleNamespace(
        data={
            "tool": {
                "pycontainer": {
                    "registry": "ghcr.io/example/sample-app:stable",
                    "push": True,
                    "base_image": "python:3.12-slim",
                    "include_deps": False,
                    "sbom": "cyclonedx",
                    "dry_run": True,
                    "no_cache": True,
                    "env": {"APP_MODE": "test"},
                    "labels": config_labels,
                }
            }
        }
    )
    poetry = SimpleNamespace(
        package=package,
        pyproject=pyproject,
        file=SimpleNamespace(path=tmp_path / "pyproject.toml"),
    )

    class TestCommand(ContainerBuildCommand):
        def __init__(self):
            self._options = {
                "tag": None,
                "base-image": None,
                "registry": None,
                "push": None,
                "include-deps": True,
                "sbom": None,
                "verbose": None,
                "dry-run": None,
                "no-cache": None,
            }
            self._poetry = poetry
            self._io = SimpleNamespace(
                is_verbose=lambda: False,
                input=SimpleNamespace(has_parameter_option=lambda _: False),
            )
            self.output = []

        @property
        def poetry(self):
            return self._poetry

        @property
        def io(self):
            return self._io

        def option(self, name):
            return self._options[name]

        def line(self, message):
            self.output.append(message)

        def line_error(self, message):
            self.output.append(message)

    command = TestCommand()

    assert command.handle() == 0

    builder = builders[0]
    assert builder.config.tag == "sample-app:1.2.3"
    assert builder.config.context_dir == str(tmp_path)
    assert builder.config.base_image == "python:3.12-slim"
    assert builder.config.include_deps is False
    assert builder.config.generate_sbom == "cyclonedx"
    assert builder.config.dry_run is True
    assert builder.config.use_cache is False
    assert builder.config.env == {"APP_MODE": "test"}
    assert builder.config.labels["custom.label"] == "kept"
    assert builder.push_registry == "ghcr.io/example/sample-app:stable"
    assert config_labels == {"custom.label": "kept"}
