import os
import socket
import sys

import pytest

from pycontainer import builder, cache, cli, effective_config, registry_client


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["pycontainer", *argv])
    try:
        cli.main()
    except SystemExit as exit_:
        return exit_.code
    return 0


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """Fail on any build, cache, registry or network use; isolate HOME and cwd."""

    def forbidden(*args, **kwargs):
        pytest.fail("validate config must not build, cache or contact a registry")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "ImageBuilder", forbidden)
    monkeypatch.setattr(builder, "ImageBuilder", forbidden)
    monkeypatch.setattr(cache, "LayerCache", forbidden)
    monkeypatch.setattr(registry_client, "RegistryClient", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return tmp_path


def _tree(root):
    return sorted(
        (os.path.relpath(os.path.join(base, name), root), os.path.getmtime(os.path.join(base, name)))
        for base, dirs, files in os.walk(root)
        for name in dirs + files
    )


def _write(path, text):
    path.write_text(text)
    return str(path)


def test_valid_layouts_pass_and_report_checks_not_performed(offline, monkeypatch, capsys):
    build = _write(offline / "pycontainer.toml", '[build]\ntag = "app:1"\nplatform = "linux/arm64"\n')
    tool = _write(
        offline / "pyproject.toml",
        '[project]\nname = "demo"\n\n[tool.pycontainer]\nbase-image = "python:3.12-slim"\n'
        '[tool.pycontainer.env]\nMODE = "prod"\n',
    )
    before = _tree(offline)

    assert _run(monkeypatch, ["validate", "config", build, tool]) == 0

    out = capsys.readouterr().out
    assert f"ok: {build} [build]" in out
    assert f"ok: {tool} [tool.pycontainer]" in out
    assert "offline validation does not prove a successful build" in out
    for check in ("registry:", "dependency resolution:", "runtime:"):
        assert check in out
    assert _tree(offline) == before


def test_no_path_validates_build_defaults_with_local_detection(offline, monkeypatch, capsys):
    _write(offline / "pyproject.toml", '[project]\nname = "demo"\nrequires-python = ">=3.13"\n')
    resolved = []
    original = effective_config.resolve_build_config
    monkeypatch.setattr(
        cli, "resolve_build_config",
        lambda path, overrides, build_args=None: resolved.append(original(path, overrides, build_args)) or resolved[-1],
    )

    assert _run(monkeypatch, ["validate", "config"]) == 0

    assert "ok: defaults (no configuration file)" in capsys.readouterr().out
    cfg, table = resolved[0]
    assert cfg.base_image == "python:3.13-slim" and table is None


def test_offline_validation_accepts_signing_config_but_build_requires_push(offline, monkeypatch, capsys):
    path = _write(
        offline / "pycontainer.toml",
        '[build]\nsign = true\nsign-provider = "cosign"\n'
        'sign-certificate-identity = "https://example.com/workflow"\n'
        'sign-certificate-oidc-issuer = "https://example.com"\n',
    )

    assert _run(monkeypatch, ["validate", "config", path]) == 0
    assert f"ok: {path} [build]" in capsys.readouterr().out

    assert _run(monkeypatch, ["build", "--config", path]) == 2
    assert "--sign requires --push" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    ['[project]\nname = "demo"\nrequires-python = 312\n', 'project = "invalid"\n'],
)
def test_invalid_detected_project_metadata_is_a_diagnostic(offline, monkeypatch, capsys, content):
    _write(offline / "pyproject.toml", content)

    assert _run(monkeypatch, ["validate", "config"]) == 2
    assert "[project].requires-python: cannot detect the Python version" in capsys.readouterr().err


def test_cli_overrides_take_precedence_and_are_attributed(offline, monkeypatch, capsys):
    path = _write(offline / "pycontainer.toml", '[build]\nplatform = "linux/arm64"\n')

    assert _run(monkeypatch, ["validate", "config", "-c", path, "--platform", "bad"]) == 2
    err = capsys.readouterr().err
    assert f"error: {path}: CLI option --platform: Invalid platform format: bad" in err

    _write(offline / "pycontainer.toml", '[build]\nplatform = "bad"\n')
    assert _run(monkeypatch, ["validate", "config", "-c", path, "--platform", "linux/amd64"]) == 0


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
def test_cli_overrides_invalid_file_types_before_validation(offline, monkeypatch, capsys, table):
    path = _write(offline / "config.toml", f'[{table}]\nplatform = 123\ninclude-deps = "invalid"\n')

    assert _run(monkeypatch, ["validate", "config", path]) == 2
    assert f"Invalid value for [{table}].platform" in capsys.readouterr().err
    assert _run(
        monkeypatch, ["validate", "config", path, "--platform", "linux/arm64", "--include-deps"]
    ) == 0


def test_validate_build_table_with_unrelated_scalar_tool(offline, monkeypatch):
    path = _write(offline / "config.toml", 'tool = "standalone"\n[build]\n')
    assert _run(monkeypatch, ["validate", "config", path]) == 0


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
def test_build_argument_resolution_matches_build(offline, monkeypatch, table):
    path = _write(
        offline / "config.toml",
        f'[{table}]\ntag = "app:${{VERSION}}"\nplatform = "${{TARGET}}"\n'
        f'[{table}.build-args]\nVERSION = {{}}\nTARGET = "linux/amd64"\n',
    )
    configs = []
    original = effective_config.resolve_build_config

    def resolve(*args):
        result = original(*args)
        configs.append(result[0])
        return result

    class FakeBuilder:
        def __init__(self, config):
            pass

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "resolve_build_config", resolve)
    monkeypatch.setattr(cli, "ImageBuilder", FakeBuilder)
    overrides = ["--build-arg", "VERSION=old", "--build-arg", "VERSION=new",
                 "--build-arg", "TARGET=linux/arm64"]
    assert _run(monkeypatch, ["validate", "config", path, *overrides]) == 0
    assert _run(monkeypatch, ["build", "--config", path, *overrides]) == 0
    assert [(cfg.tag, cfg.platform) for cfg in configs] == [
        ("app:new", "linux/arm64"), ("app:new", "linux/arm64"),
    ]


@pytest.mark.parametrize("extra", [[], ["--build-arg", "MISSING=value"]])
def test_build_argument_errors_have_source_and_no_side_effects(offline, monkeypatch, capsys, extra):
    path = _write(offline / "config.toml", "[build]\n[build.build-args]\nREQUIRED = {}\n")
    for command in (["validate", "config", path], ["build", "--config", path]):
        assert _run(monkeypatch, [*command, *extra]) == 2
        err = capsys.readouterr().err
        assert ("CLI option --build-arg" if extra else "[build].build_args") in err


def test_interpolated_diagnostics_are_redacted(offline, monkeypatch, capsys):
    path = _write(
        offline / "config.toml",
        '[build]\ncontext-dir = "${PRIVATE}"\n[build.build-args]\nPRIVATE = "private-path"\n',
    )
    for command in (["validate", "config", path], ["build", "--config", path]):
        assert _run(monkeypatch, command) == 2
        err = capsys.readouterr().err
        assert "private-path" not in err
        assert "<redacted>" in err


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("override", [False, True])
def test_source_labels_redact_resolved_arguments(offline, monkeypatch, capsys, valid, override):
    value = "supplied-value" if override else "declared-value"
    source = offline / value
    source.mkdir()
    path = _write(
        source / "config.toml",
        '[build]\n' + ('' if valid else 'context-dir = "missing"\n') +
        '[build.build-args]\nVALUE = "declared-value"\n',
    )
    extra = ["--build-arg", f"VALUE={value}"] if override else []
    assert _run(monkeypatch, ["validate", "config", path, *extra]) == (0 if valid else 2)
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert value not in output
    assert "<redacted>" in output
    assert "config.toml" in output
    assert ("ok:" if valid else "error:") in output


@pytest.mark.parametrize("override", [False, True])
def test_missing_argument_redacts_other_known_values_in_source(offline, monkeypatch, capsys, override):
    value = "supplied-value" if override else "declared-value"
    source = offline / value
    source.mkdir()
    path = _write(
        source / "config.toml",
        '[build]\n[build.build-args]\nVALUE = "declared-value"\nREQUIRED = {}\n',
    )
    extra = ["--build-arg", f"VALUE={value}"] if override else []
    assert _run(monkeypatch, ["validate", "config", path, *extra]) == 2
    output = capsys.readouterr().err
    assert value not in output
    assert "<redacted>" in output
    assert "config.toml" in output
    assert "Missing required build argument(s): REQUIRED." in output


def test_validation_applies_security_after_platform_override_and_expansion(offline, monkeypatch, capsys):
    path = _write(
        offline / "config.toml",
        '[build]\nplatform = "windows/amd64"\n'
        '[build.build-args]\nTARGET = "linux/arm64"\n'
        '[build.security.kubernetes]\nread-only-root-filesystem = true\n',
    )
    before = _tree(offline)
    assert _run(monkeypatch, ["validate", "config", path, "--platform", "${TARGET}"]) == 0
    capsys.readouterr()
    assert _run(
        monkeypatch, ["validate", "config", path, "--platform", "${TARGET}",
                      "--build-arg", "TARGET=private-platform"],
    ) == 2
    err = capsys.readouterr().err
    assert "private-platform" not in err
    assert "<redacted>" in err
    assert _tree(offline) == before


@pytest.mark.parametrize("base", ["oci:missing-layout:latest", "docker-archive:missing.tar"])
def test_validation_accepts_local_reference_syntax_without_reading_images(offline, monkeypatch, base):
    assert _run(
        monkeypatch, ["validate", "config", "--base-image", base,
                      "--platform", "linux/amd64,linux/arm64"],
    ) == 0


@pytest.mark.parametrize(
    ("content", "extra", "origin"),
    [
        ('[build]\ntag = "app:1"\ntags = ["bad tag"]\n', [], "[build].tags"),
        ('[tool.pycontainer]\ntag = "app:1"\n', ["--semver-tags"], "CLI option --semver-tags"),
    ],
)
def test_tag_errors_identify_the_setting_that_failed(
    offline, monkeypatch, capsys, content, extra, origin
):
    path = _write(offline / "config.toml", content)

    for command in (["validate", "config", path], ["build", "--config", path]):
        assert _run(monkeypatch, [*command, *extra]) == 2
        assert f"{origin}:" in capsys.readouterr().err


def test_invalid_paths_are_aggregated_with_source_and_field(offline, monkeypatch, capsys):
    good = _write(offline / "good.toml", '[build]\ntag = "app:1"\n')
    unknown = _write(offline / "unknown.toml", "[tool.pycontainer]\nworkdr = \"/x\"\n")
    wrong_type = _write(offline / "type.toml", "[build]\nexposed-ports = [\"80\"]\n")
    conflict = _write(offline / "both.toml", '[build]\ntag = "a:1"\n[tool.pycontainer]\ntag = "b:1"\n')
    semantic = _write(offline / "semantic.toml", '[tool.pycontainer]\nplatform = "linux"\ninclude-paths = ["../x"]\n')
    missing = str(offline / "missing.toml")

    code = _run(monkeypatch, ["validate", "config", good, unknown, wrong_type, conflict, semantic, missing])

    captured = capsys.readouterr()
    assert code == 2
    assert f"ok: {good} [build]" in captured.out
    assert f"error: {unknown}: Unknown [tool.pycontainer] setting(s): 'workdr'" in captured.err
    assert f"error: {wrong_type}: Invalid value for [build].exposed_ports" in captured.err
    assert f"error: {conflict}: " in captured.err and "both [build] and [tool.pycontainer]" in captured.err
    assert f"error: {semantic}: [tool.pycontainer].platform: Invalid platform format" in captured.err
    assert f"error: {semantic}: [tool.pycontainer].include_paths: Context paths must be relative" in captured.err
    assert f"error: {missing}: Config file not found" in captured.err
    assert "5 of 6 configuration(s) invalid." in captured.err
    assert "Not performed" in captured.out


@pytest.mark.parametrize(
    "argv",
    [
        ["validate"],
        ["validate", "config", "a.toml", "-c", "b.toml"],
        ["validate", "config", "-c", "a.toml", "-c", "b.toml"],
        ["validate", "config", "--push"],
        ["validate", "config", "--password", "secret"],
        ["validate", "config", "--dry-run"],
    ],
)
def test_usage_errors_exit_2(offline, monkeypatch, capsys, argv):
    assert _run(monkeypatch, argv) == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("content", "extra", "message"),
    [
        ("[build]\nunexpected = true\n", [], "Unknown [build] setting"),
        ('[tool.pycontainer]\ninclude-deps = "yes"\n', [], "Invalid value for [tool.pycontainer].include_deps"),
        ('[build]\ngenerate-sbom = "cyclonedx-json"\n', [], "generate_sbom must be a boolean"),
        ('[build]\ncontext-dir = "absent"\n', [], "[build].context_dir: build context directory not found"),
        ("[build]\ninclude-deps = true\n", ["--platform", "linux/s390x"], "CLI option --platform: Dependency staging does not support"),
        ("[build]\n", ["--base-image", "Bad Image"], "CLI option --base-image:"),
        ("[build]\n", ["--tag", "app@sha256:" + "a" * 64], "CLI option --tag: Tag 'app@sha256:"),
        ("[build]\n", ["--system-package", "bad package"], "CLI option --system-package:"),
        ('[build]\nsystem-packages = ["bad package"]\ngenerate-sbom = "x"\n', [], "[build].generate_sbom:"),
    ],
)
def test_build_and_validate_share_checks(offline, monkeypatch, capsys, content, extra, message):
    path = _write(offline / "pycontainer.toml", content)

    assert _run(monkeypatch, ["validate", "config", path, *extra]) == 2
    assert message in capsys.readouterr().err
    assert _run(monkeypatch, ["build", "--config", path, *extra]) == 2
    assert message in capsys.readouterr().err


def test_directory_config_path_reports_an_error_in_build_and_validate(offline, monkeypatch, capsys):
    path = str(offline)

    for command in (["validate", "config", path], ["build", "--config", path]):
        assert _run(monkeypatch, command) == 2
        err = capsys.readouterr().err
        assert path in err or repr(path) in err
        assert "Traceback" not in err


@pytest.mark.parametrize(
    "content",
    ['[project]\nname = "demo"\n', "[biuld]\ntag = \"app:1\"\n", '[tool.pycontiner]\ntag = "app:1"\n', ""],
)
def test_explicit_file_without_a_supported_table_fails_in_build_and_validate(offline, monkeypatch, capsys, content):
    path = _write(offline / "pycontainer.toml", content)
    message = "defines neither [build] nor [tool.pycontainer]"

    assert _run(monkeypatch, ["validate", "config", path]) == 2
    assert f"error: {path}: " in (err := capsys.readouterr().err) and message in err
    assert _run(monkeypatch, ["validate", "config", "-c", path]) == 2
    assert message in capsys.readouterr().err
    assert _run(monkeypatch, ["build", "--config", path]) == 2
    assert message in capsys.readouterr().err
