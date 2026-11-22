import io
import logging
import sys

import pytest

from pycontainer import cli, effective_config


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["pycontainer", *argv])
    cli.main()


def _capture_config(monkeypatch, argv):
    configs = []

    class FakeImageBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", FakeImageBuilder)
    _run_cli(monkeypatch, argv)
    return configs[0]


def test_cli_preserves_config_values_when_options_are_omitted(tmp_path, monkeypatch):
    context = tmp_path / "configured-context"
    context.mkdir()
    context_value = context.as_posix()
    config_path = tmp_path / "pycontainer.toml"
    logging_levels = []
    monkeypatch.setattr(
        cli.logging,
        "basicConfig",
        lambda **kwargs: logging_levels.append(kwargs["level"]),
    )
    config_path.write_text(
        f"""
[build]
context-dir = "{context_value}"
platform = "linux/arm64"
include-deps = true
requirements-file = "requirements-prod.txt"
verbose = true
dry-run = true
generate-sbom = true
"""
    )

    config = _capture_config(
        monkeypatch, ["build", "--config", str(config_path)]
    )

    assert config.context_dir == context_value
    assert config.platform == "linux/arm64"
    assert config.include_deps is True
    assert config.requirements_file == "requirements-prod.txt"
    assert config.verbose is True
    assert config.dry_run is True
    assert config.generate_sbom is True
    assert logging_levels == [logging.DEBUG]


def test_cli_options_override_config_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "from-cli").mkdir()
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        """
[build]
context-dir = "from-file"
platform = "linux/arm64"
include-deps = false
requirements-file = "requirements-file.txt"
verbose = false
dry-run = false
generate-sbom = false
use-cache = true
reproducible = true
"""
    )

    config = _capture_config(
        monkeypatch,
        [
            "build",
            "--config",
            str(config_path),
            "--context",
            "from-cli",
            "--platform",
            "linux/amd64",
            "--include-deps",
            "--requirements",
            "requirements-cli.txt",
            "--verbose",
            "--dry-run",
            "--sbom",
            "--no-cache",
            "--no-reproducible",
        ],
    )

    assert config.context_dir == "from-cli"
    assert config.platform == "linux/amd64"
    assert config.include_deps is True
    assert config.requirements_file == "requirements-cli.txt"
    assert config.verbose is True
    assert config.dry_run is True
    assert config.generate_sbom == "spdx"
    assert config.use_cache is False
    assert config.reproducible is False


def test_cli_context_include_and_exclude_options_override_config(tmp_path, monkeypatch):
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        """
[build]
include-paths = ["from-file"]
exclude-paths = ["generated"]
exclude-defaults = true
"""
    )

    config = _capture_config(
        monkeypatch,
        [
            "build",
            "--config",
            str(config_path),
            "--include",
            "app.py",
            "--include",
            "pyproject.toml",
            "--exclude",
            "artifacts/**",
            "--no-default-excludes",
        ],
    )

    assert config.include_paths == ["app.py", "pyproject.toml"]
    assert config.exclude_paths == ["artifacts/**"]
    assert config.exclude_defaults is False


def test_cli_uses_build_config_defaults_without_a_config_file(monkeypatch):
    monkeypatch.setattr(effective_config, "detect_python_version", lambda context: "3.12")

    config = _capture_config(monkeypatch, ["build"])

    assert config.context_dir == "."
    assert config.base_image == "python:3.12-slim"
    assert config.platform == "linux/amd64"
    assert config.include_deps is False
    assert config.requirements_file == "requirements.txt"
    assert config.verbose is False
    assert config.dry_run is False
    assert config.generate_sbom is False


@pytest.mark.parametrize(
    ("arguments", "expected_format"),
    [
        (["--sbom"], "spdx"),
        (["--sbom", "spdx"], "spdx"),
        (["--sbom", "cyclonedx"], "cyclonedx"),
    ],
)
def test_cli_sbom_format_options(monkeypatch, arguments, expected_format):
    config = _capture_config(monkeypatch, ["build", *arguments])

    assert config.generate_sbom == expected_format


def test_cli_accepts_sbom_format_from_config_and_cli_overrides_it(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text('[build]\ngenerate-sbom = "cyclonedx"\n')

    config = _capture_config(monkeypatch, ["build", "--config", str(config_path)])
    assert config.generate_sbom == "cyclonedx"

    config = _capture_config(
        monkeypatch, ["build", "--config", str(config_path), "--sbom", "spdx"]
    )
    assert config.generate_sbom == "spdx"


def test_cli_forwards_basic_credentials_without_logging_them(monkeypatch, capsys):
    pushed = []

    class FakeImageBuilder:
        def __init__(self, config):
            pass

        def build(self):
            return "layout"

        def push(self, **kwargs):
            pushed.append(kwargs)

    monkeypatch.setattr(cli, "ImageBuilder", FakeImageBuilder)
    monkeypatch.setattr(effective_config, "detect_python_version", lambda context: "3.11")
    _run_cli(
        monkeypatch,
        [
            "build",
            "--push",
            "--username",
            "registry-user",
            "--password",
            "registry-password",
        ],
    )

    assert pushed == [
        {
            "registry_url": None,
            "username": "registry-user",
            "password": "registry-password",
            "show_progress": True,
        }
    ]
    assert "registry-password" not in capsys.readouterr().out


def test_cli_rejects_username_without_password_or_password_environment(
    monkeypatch, capsys
):
    monkeypatch.delenv("REGISTRY_PASSWORD", raising=False)

    with pytest.raises(SystemExit) as error:
        _run_cli(monkeypatch, ["build", "--push", "--username", "registry-user"])

    assert error.value.code == 2
    assert "--username requires --password or REGISTRY_PASSWORD" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("unexpected-setting", "true", "Unknown [build] setting"),
        ("include_deps", '"yes"', "Invalid value for [build].include_deps"),
        ("generate-sbom", '"cyclonedx-json"', "generate_sbom must be a boolean"),
    ],
)
def test_cli_reports_invalid_config(tmp_path, monkeypatch, capsys, setting, value, message):
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(f"[build]\n{setting} = {value}\n")

    class FakeImageBuilder:
        def __init__(self, config):
            pass

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", FakeImageBuilder)

    with pytest.raises(SystemExit) as error:
        _run_cli(monkeypatch, ["build", "--config", str(config_path)])

    assert error.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('[build]\ntag = "a:1"\n\n[tool.pycontainer]\ntag = "b:1"\n', "both [build] and [tool.pycontainer]"),
        ('tool = "bad"\n', "[tool] configuration must be a TOML table"),
        ('[tool]\npycontainer = "bad"\n', "[tool.pycontainer] configuration must be a TOML table"),
        ("[tool.pycontainer]\nworkdr = \"/x\"\n", "Unknown [tool.pycontainer] setting(s): 'workdr'"),
        ("[tool.pycontainer]\nexposed_ports = [\"80\"]\n", "Invalid value for [tool.pycontainer].exposed_ports"),
        ("[tool.pycontainer]\npush = true\n", "use 'pycontainer build --push'"),
    ],
)
def test_cli_reports_invalid_tool_pycontainer_config(tmp_path, monkeypatch, capsys, content, message):
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(content)
    monkeypatch.setattr(cli, "ImageBuilder", lambda config: pytest.fail("must not build"))

    with pytest.raises(SystemExit) as error:
        _run_cli(monkeypatch, ["build", "--config", str(config_path), "--tag", "app:1"])

    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_reads_tool_pycontainer_table_and_cli_overrides_it(tmp_path, monkeypatch):
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        '[project]\nname = "demo"\n\n[tool.ruff]\nline-length = 99\n\n'
        '[tool.pycontainer]\nbase-image = "python:3.12-alpine"\nworkdir = "/srv"\nplatform = "linux/amd64"\n\n'
        '[tool.pycontainer.env]\nMODE = "production"\n'
    )

    config = _capture_config(
        monkeypatch, ["build", "--config", str(config_path), "--platform", "linux/arm64"]
    )

    assert config.base_image == "python:3.12-alpine"
    assert config.workdir == "/srv"
    assert config.env == {"MODE": "production"}
    assert config.platform == "linux/arm64"


def test_cli_status_output_survives_legacy_redirected_encoding(monkeypatch):
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")

    class FakeImageBuilder:
        def __init__(self, config):
            pass

        def build(self):
            print("\u2713 Dependency layer created")
            return "layout"

    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(cli, "ImageBuilder", FakeImageBuilder)
    _run_cli(monkeypatch, ["build", "--tag", "app:test"])

    stdout.flush()
    assert b"? Dependency layer created" in stdout.buffer.getvalue()
