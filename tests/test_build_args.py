"""Build argument grammar, one-pass expansion and image/cache boundaries."""
import errno
import json
import logging
import sys
import tarfile
import traceback
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pycontainer import cli
from pycontainer import auth as auth_module
from pycontainer import builder as builder_module
from pycontainer import dependencies as dependencies_module
from pycontainer.auth import RegistryAuth
from pycontainer.build_args import resolve_arguments
from pycontainer.builder import ImageBuilder
from pycontainer.cache import LayerCache
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.project import declared_dependency_names
from pycontainer.registry_client import RegistryClient
from pycontainer.system_packages import report_system_packages


def test_direct_config_without_arguments_preserves_runtime_dollars():
    config = BuildConfig(cmd=["/bin/sh", "-c", "echo ${PORT} $$"])
    assert config.cmd == ["/bin/sh", "-c", "echo ${PORT} $$"]


def test_explicit_empty_arguments_reject_undeclared_references():
    with pytest.raises(ValueError, match="Undeclared build argument reference"):
        BuildConfig(build_args={}, cmd=["echo ${PORT}"])


def test_explicit_empty_arguments_expand_escaped_dollars():
    config = BuildConfig(build_args={}, cmd=["echo $$PORT"])
    assert config.cmd == ["echo $PORT"]


def test_cli_without_config_rejects_undeclared_reference(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--tag", "app:${VERSION}"])
    monkeypatch.setattr(cli, "ImageBuilder", lambda config: pytest.fail("builder constructed"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "Undeclared build argument reference" in capsys.readouterr().err


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
def test_defaults_required_values_and_cli_precedence(tmp_path, table):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[{table}]\ntag = "app:${{VERSION}}"\n'
        f'[{table}.build-args]\nVERSION = {{default = "v1"}}\nOPTION = {{}}\n'
    )
    config = config_from_file(
        path, {"tag": "override:${VERSION}"},
        ["VERSION=v2", "OPTION=", "VERSION=v3=extra"],
    )
    assert config.tag == "override:v3=extra"
    assert config.build_args == {"VERSION": "v3=extra", "OPTION": ""}


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
def test_cli_override_replaces_invalid_file_value_before_build_arg_interpolation(tmp_path, table):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[{table}]\ntag = 42\n'
        f'[{table}.build-args]\nVERSION = "default"\n'
    )
    config = config_from_file(
        path, {"tag": "override:${VERSION}"}, ["VERSION=explicit"]
    )
    assert config.tag == "override:explicit"


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("${A}", "${B}$$"),
        ("$${A}", "${A}"),
        ("$$${A}", "$${B}$$"),
        ("$HOME/$$/cost$5", "$HOME/$/cost$5"),
        ("${EMPTY}x${B}", "xdone"),
    ],
)
def test_interpolation_is_one_pass_and_does_not_read_environment(monkeypatch, template, expected):
    monkeypatch.setenv("A", "host-value")
    config = BuildConfig(
        build_args={"A": "${B}$$", "B": "done", "EMPTY": ""},
        labels={"result": template, "${A}": "unchanged-key"},
    )
    assert config.labels == {"result": expected, "${A}": "unchanged-key"}
    assert config.env == {}


@pytest.mark.parametrize("copy_kind", ["replace", "json"])
def test_config_copy_keeps_expanded_dollars_literal_and_expands_new_values(copy_kind):
    original = BuildConfig(
        build_args={"A": "${B}", "B": "expanded"},
        cmd=["echo", "${A}", "$${B}"],
        env={"VALUE": "${A}"},
        labels={"literal": "$${B}"},
    )
    if copy_kind == "replace":
        copied = replace(original, env={**original.env, "NEW": "${B}"})
    else:
        data = json.loads(json.dumps(asdict(original)))
        data["env"]["NEW"] = "${B}"
        copied = BuildConfig(**data)

    assert copied.cmd == ["echo", "${B}", "${B}"]
    assert copied.env == {"VALUE": "${B}", "NEW": "expanded"}
    assert copied.labels == {"literal": "${B}"}
    assert original.env == {"VALUE": "${B}"}
    moved = replace(copied, cmd=["env", *copied.cmd])
    assert moved.cmd == ["env", "echo", "${B}", "${B}"]


def test_config_copy_with_new_arguments_keeps_existing_values_and_redaction():
    original = BuildConfig(build_args={"A": "old-value"}, env={"VALUE": "${A}"})
    copied = replace(original, build_args={"A": "new-value"}, cmd=["${A}"])

    assert copied.env == {"VALUE": "old-value"}
    assert copied.cmd == ["new-value"]
    assert copied.redact("old-value and new-value") == "<redacted> and <redacted>"
    cleared = replace(copied, build_args=None)
    assert cleared.redact("old-value and new-value") == "<redacted> and <redacted>"


@pytest.mark.parametrize("copy_kind", ["replace", "json"])
def test_config_copy_retains_redaction_after_in_place_argument_clear(copy_kind):
    original = BuildConfig(build_args={"VALUE": "synthetic-copy-marker"})
    original.build_args.clear()

    if copy_kind == "replace":
        copied = replace(original)
    else:
        copied = BuildConfig(**json.loads(json.dumps(asdict(original))))

    assert copied.redact("synthetic-copy-marker") == "<redacted>"


@pytest.mark.parametrize("copy_kind", ["replace", "json"])
def test_snapshot_equal_overrides_remain_literal_when_arguments_change(copy_kind):
    original = BuildConfig(
        build_args={"A": "${B}", "B": "old-value"},
        workdir="/${A}", cmd=["${A}"], env={"VALUE": "${A}"},
    )
    overrides = {
        "build_args": {"B": "new-value"},
        "workdir": "/${B}",
        "cmd": ["${B}"],
        "env": {"VALUE": "${B}"},
    }
    if copy_kind == "replace":
        copied = replace(original, **overrides)
    else:
        data = json.loads(json.dumps(asdict(original)))
        data.update(overrides)
        copied = BuildConfig(**data)

    assert copied.workdir == "/${B}"
    assert copied.cmd == ["${B}"]
    assert copied.env == {"VALUE": "${B}"}
    assert copied.redact("old-value new-value ${B}") == "<redacted> <redacted> <redacted>"
    cleared = replace(copied, build_args=None)
    assert {"old-value", "new-value"} <= set(asdict(cleared)["_build_args_expansion_state"]["redactions"])

    fresh = BuildConfig(**overrides)
    assert (fresh.workdir, fresh.cmd, fresh.env) == (
        "/new-value", ["new-value"], {"VALUE": "new-value"}
    )


def test_config_copy_expands_new_duplicate_list_entries():
    original = BuildConfig(build_args={"A": "${B}", "B": "expanded"}, cmd=["${A}"])

    copied = replace(original, cmd=[*original.cmd, "${B}"])

    assert copied.cmd == ["${B}", "expanded"]


@pytest.mark.parametrize("copy_kind", ["replace", "json"])
def test_config_copy_expands_values_mutated_after_construction(copy_kind):
    removed = "removed-explicit-environment-value"
    original = BuildConfig(
        build_args={"A": "old-value", "B": "${A}"},
        cmd=["echo", "${B}"], env={"KEEP": "${B}", "REMOVE": removed},
        labels={"keep": "${B}"},
    )
    original.cmd.append("${A}")
    original.env["NEW"] = "${A}"
    del original.env["REMOVE"]
    original.labels["new"] = "${A}"
    original.workdir = "/${A}"

    if copy_kind == "replace":
        copied = replace(original, build_args={"A": "new-value", "B": "${A}"})
    else:
        serialized = json.dumps(asdict(original))
        assert removed not in serialized
        data = json.loads(serialized)
        data["build_args"]["A"] = "new-value"
        copied = BuildConfig(**data)

    assert copied.cmd == ["echo", "${A}", "new-value"]
    assert copied.env == {"KEEP": "${A}", "NEW": "new-value"}
    assert copied.labels == {"keep": "${A}", "new": "new-value"}
    assert copied.workdir == "/new-value"

    cleared = replace(original, build_args=None)
    if copy_kind == "replace":
        resumed = replace(cleared, build_args={"A": "new-value", "B": "${A}"})
    else:
        data = json.loads(json.dumps(asdict(cleared)))
        data["build_args"] = {"A": "new-value", "B": "${A}"}
        resumed = BuildConfig(**data)
    assert resumed.cmd == copied.cmd
    assert resumed.env == copied.env
    assert resumed.labels == copied.labels
    assert resumed.workdir == copied.workdir


@pytest.mark.parametrize("copy_kind", ["replace", "json"])
def test_config_copy_preserves_generated_literal_paths_when_commands_change(
    tmp_path, copy_kind,
):
    script = "demo_${UNDECLARED}.py"
    (tmp_path / script).write_text("import streamlit as st\nst.write('hello')\n")
    (tmp_path / "requirements.txt").write_text("streamlit\n")
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=["demo_$${UNDECLARED}.py", "requirements.txt"],
        use_cache=False, build_args={"PORT": "old-value"},
    )
    ImageBuilder(config)
    config.entrypoint.append("${PORT}")

    if copy_kind == "replace":
        copied = replace(config, build_args={"PORT": "new-value"})
    else:
        data = json.loads(json.dumps(asdict(config)))
        data["build_args"]["PORT"] = "new-value"
        copied = BuildConfig(**data)

    assert copied.entrypoint[2] == script
    assert copied.entrypoint[-1] == "new-value"


@pytest.mark.parametrize("failure", ["retained-resolution", "current-resolution", "interpolation"])
def test_config_copy_redacts_previous_arguments_from_construction_errors(failure):
    original = BuildConfig(build_args={"SECRET": "private_input"})
    value = "current_input" if failure == "current-resolution" else "private_input"

    with pytest.raises(ValueError) as error:
        if failure == "retained-resolution":
            replace(original, build_args={"private_input": {}})
        elif failure == "current-resolution":
            replace(original, build_args={"current_input": {}, "OTHER": "current_input"})
        else:
            replace(original, build_args={}, cmd=["${private_input}"])

    assert value not in str(error.value)
    assert "<redacted>" in str(error.value)


def test_invalid_serialized_expansion_state_is_rejected():
    with pytest.raises(ValueError, match="Invalid serialized build-argument expansion state"):
        BuildConfig(build_args={}, _build_args_expansion_state={"values": {}})


@pytest.mark.parametrize(
    ("declarations", "overrides", "message"),
    [
        ({"A": {}}, [], "Missing required"),
        ({}, ["A=value"], "Undeclared"),
        ({"A": "ok"}, ["A"], "KEY=VALUE"),
        ({"A": "ok"}, ["A-B=value"], "valid argument name"),
        ({"bad-name": "value"}, [], "Invalid build argument name"),
        ({"A": {"unexpected": "value"}}, [], "only default"),
        ({"A": {"default": 1}}, [], "must be a string"),
        ({"A": "private\nvalue"}, ["A=ok"], "control characters"),
        ({"A": ""}, ["A=private\x7fvalue"], "control characters"),
        ({"A": ""}, ["A=private\x85value"], "control characters"),
    ],
)
def test_invalid_arguments_fail_without_echoing_values(declarations, overrides, message):
    with pytest.raises(ValueError, match=message) as error:
        resolve_arguments(declarations, overrides)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("reference", ["${HOST}", "${A:-x}", "${A", "${}", "${A\n}"])
def test_invalid_references_fail_before_builder(tmp_path, monkeypatch, capsys, reference):
    path = tmp_path / "config.toml"
    path.write_text(f'[build]\nentrypoint = [{json.dumps(reference)}]\n')
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--config", str(path), "--dry-run"])
    monkeypatch.setattr(cli, "ImageBuilder", lambda config: pytest.fail("builder constructed"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    diagnostic = capsys.readouterr().err
    assert "reference" in diagnostic or "control characters" in diagnostic


@pytest.mark.parametrize("arguments", [[], ["--build-arg", "A"], ["--build-arg", "OTHER=x"]])
def test_cli_rejects_missing_malformed_and_undeclared_inputs_before_build(
    tmp_path, monkeypatch, capsys, arguments,
):
    path = tmp_path / "config.toml"
    path.write_text('[build.build-args]\nA = {}\n')
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "build", "--config", str(path), "--dry-run", *arguments,
    ])
    monkeypatch.setattr(cli, "ImageBuilder", lambda config: pytest.fail("builder constructed"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "argument" in capsys.readouterr().err


def test_cli_override_order_and_empty_value(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '[build]\nenv = {OPTION = "${OPTION}"}\nlabels = {version = "${VERSION}"}\n'
        '[build.build-args]\nVERSION = "default"\nOPTION = {}\n'
    )
    configs = []

    class CaptureBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", CaptureBuilder)
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "build", "--config", str(path),
        "--build-arg", "OPTION=", "--build-arg", "VERSION=first",
        "--build-arg", "VERSION=last=with=equals",
    ])
    cli.main()
    assert configs[0].env == {"OPTION": ""}
    assert configs[0].labels == {"version": "last=with=equals"}


@pytest.mark.parametrize("operation", ["build", "push"])
@pytest.mark.parametrize(("category", "exit_code"), [("findings", 3), ("scanner", 4)])
def test_scan_errors_redact_arguments_and_preserve_exit_code(
    tmp_path, monkeypatch, capsys, operation, category, exit_code,
):
    from pycontainer.scanning import ScanError

    value = "private-scan-value"
    path = tmp_path / "config.toml"
    path.write_text(f'[build.build-args]\nVALUE = "{value}"\n')

    class FailingBuilder:
        def __init__(self, config):
            pass

        def build(self):
            if operation == "build":
                raise ScanError(category, f"failure: {value}")
            return "layout"

        def push(self, **kwargs):
            raise ScanError(category, f"failure: {value}")

    monkeypatch.setattr(cli, "ImageBuilder", FailingBuilder)
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--config", str(path), "--push"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    output = capsys.readouterr()
    assert error.value.code == exit_code
    assert value not in output.out + output.err
    assert "<redacted>" in output.err


def test_semantic_validation_redacts_escaped_argument_values(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.toml"
    path.write_text(
        '[build]\ntags = ["${VERSION}"]\n'
        "[build.build-args]\nVERSION = 'private\\value'\n"
    )
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--config", str(path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "private" not in capsys.readouterr().err


def test_real_dry_run_redacts_arguments_and_resolves_cli_fields(tmp_path, monkeypatch, capsys, caplog):
    (tmp_path / "app.py").write_text("print('hello')")
    path = tmp_path / "config.toml"
    path.write_text(
        '[build]\ntag = "app:${VERSION}"\nlabels = {version = "${VERSION}"}\n'
        '[build.build-args]\nVERSION = "private-version"\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "build", "--config", str(path), "--context", str(tmp_path),
        "--tag", "cli:${VERSION}", "--include", "app.py", "--dry-run", "--verbose",
    ])
    with caplog.at_level(logging.DEBUG):
        cli.main()
    output = capsys.readouterr()
    assert "private-version" not in output.out + output.err + caplog.text
    assert "cli:<redacted>" in output.out
    assert not (tmp_path / "dist").exists()


def test_cli_label_precedence_interpolates_and_hides_metadata(tmp_path, monkeypatch, capsys):
    (tmp_path / "app.py").write_text("print('hello')")
    path = tmp_path / "config.toml"
    path.write_text(
        '[build.labels]\nversion = "file:${VERSION}"\nretained = "file"\n'
        '[build.build-args]\nVERSION = "private-version"\n'
    )
    configs = []

    class CaptureBuilder(ImageBuilder):
        def __init__(self, config):
            super().__init__(config)
            configs.append(config)

    monkeypatch.setattr(cli, "ImageBuilder", CaptureBuilder)
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "build", "--config", str(path), "--context", str(tmp_path),
        "--label", "version=cli:${VERSION}", "--manifest-annotation", "example=manifest-value",
        "--index-annotation", "example=index-value", "--dry-run",
    ])
    cli.main()
    assert configs[0].labels == {"version": "cli:private-version", "retained": "file"}
    assert configs[0].manifest_annotations == {"example": "manifest-value"}
    assert configs[0].index_annotations == {"example": "index-value"}
    output = capsys.readouterr()
    for value in ("private-version", "manifest-value", "index-value"):
        assert value not in output.out + output.err


def test_dry_run_redacts_values_in_non_expandable_fields(tmp_path, capsys):
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=[], dry_run=True,
        build_args={"PACKAGE": "libpq5", "PORT": "8000"},
        system_packages=["libpq5"], exposed_ports=[8000],
    )
    ImageBuilder(config).build()
    output = capsys.readouterr().out
    assert "libpq5" not in output
    assert "8000" not in output


def test_security_validation_redacts_interpolated_platform():
    with pytest.raises(ValueError, match="Linux targets only") as error:
        BuildConfig(
            platform="${PLATFORM}",
            build_args={"PLATFORM": "windows/amd64"},
            security={"kubernetes": {"read_only_root_filesystem": True}},
        )
    assert "windows/amd64" not in str(error.value)
    assert "<redacted>" in str(error.value)


def test_security_fragment_dry_run_redacts_build_arguments(tmp_path, capsys):
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=[], dry_run=True,
        build_args={"NAME": "security"},
        security={"kubernetes": {"read_only_root_filesystem": True}},
    )
    ImageBuilder(config).build()
    output = capsys.readouterr().out
    assert "security" not in output
    assert "<redacted>/kubernetes-<redacted>-context.json" in output


def test_security_fragment_output_path_redacts_build_arguments(tmp_path, monkeypatch, caplog):
    value = "private-output-value"
    (tmp_path / "app.py").write_text("print('hello')")
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=["app.py"],
        output_dir=str(tmp_path / "${OUTPUT}"), use_cache=False,
        build_args={"OUTPUT": value},
        security={"kubernetes": {"read_only_root_filesystem": True}},
    )
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda *args: ([], None))
    with caplog.at_level(logging.INFO):
        ImageBuilder(config).build()

    assert (tmp_path / value / "security" / "kubernetes-security-context.json").is_file()
    assert "Security fragment saved" in caplog.text
    assert value not in caplog.text
    assert "<redacted>" in caplog.text


def test_direct_build_redacts_framework_source_warnings_after_arguments_are_cleared(
    tmp_path, caplog
):
    value = "private_module"
    (tmp_path / f"{value}.py").write_text("from fastapi import FastAPI\napp = FastAPI(\n")
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=["${SOURCE}.py"],
        cmd=["python"], use_cache=False, build_args={"SOURCE": value},
    )

    with caplog.at_level(logging.WARNING):
        ImageBuilder(replace(config, build_args=None))

    assert "Cannot inspect" in caplog.text
    assert value not in caplog.text
    assert "<redacted>" in caplog.text


@pytest.mark.parametrize("include_paths", [None, ["app.py"]], ids=["default", "explicit"])
def test_direct_build_redacts_requirements_discovery_warnings_after_arguments_are_cleared(
    tmp_path, monkeypatch, caplog, include_paths
):
    value = "private_requirements"
    (tmp_path / "app.py").write_text("print('hello')\n")
    (tmp_path / f"{value}.txt").write_text("-r ${UNSET_REQUIREMENTS_FILE}\n")
    monkeypatch.delenv("UNSET_REQUIREMENTS_FILE", raising=False)
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=include_paths,
        requirements_file="${SOURCE}.txt", use_cache=False,
        build_args={"SOURCE": value},
    )

    with caplog.at_level(logging.WARNING):
        ImageBuilder(replace(config, build_args=None))

    assert "unresolved requirements include" in caplog.text
    assert value not in caplog.text
    assert "<redacted>" in caplog.text


def test_direct_build_redacts_framework_errors_after_arguments_are_cleared(tmp_path):
    value = "private_module"
    (tmp_path / f"{value}.py").write_text(
        "from fastapi import FastAPI\ndef create():\n    return FastAPI()\n"
    )
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=["${SOURCE}.py"],
        use_cache=False, build_args={"SOURCE": value},
    )

    with pytest.raises(ValueError, match="Cannot determine an importable FastAPI") as error:
        ImageBuilder(replace(config, build_args=None))

    assert value not in str(error.value)
    assert "<redacted>" in str(error.value)


def test_direct_build_redacts_requirements_install_warning_after_arguments_are_cleared(
    tmp_path, monkeypatch, caplog, capsys
):
    value = "private_requirements"
    (tmp_path / "app.py").write_text("print('hello')\n")
    (tmp_path / f"{value}.txt").write_text("fixture-dep\n")
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=["app.py"],
        requirements_file="${SOURCE}.txt", use_cache=False, include_deps=True,
        build_args={"SOURCE": value, "PIP_SETTING": "PIP_TARGET"},
    )
    builder = ImageBuilder(replace(config, build_args=None))
    base_config = {
        "os": "linux",
        "architecture": "amd64",
        "config": {"Env": [
            "PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.8",
            "PYTHON_LIBC=glibc-2.17",
        ]},
    }

    def fail_before_pip(*args, **kwargs):
        raise OSError("pip start blocked by test")

    monkeypatch.setenv("PIP_TARGET", str(tmp_path / "ignored"))
    monkeypatch.setattr(dependencies_module.subprocess, "Popen", fail_before_pip)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="Unable to start pip"):
        builder._create_deps_layer(tmp_path, base_config=base_config)

    assert "not fully pinned" in caplog.text
    assert value not in caplog.text
    assert "<redacted>" in caplog.text
    output = capsys.readouterr().out
    assert "Ignoring host pip settings" in output
    assert "PIP_TARGET" not in output
    assert "<redacted>" in output


def test_direct_build_redacts_missing_requirements_after_arguments_are_cleared(tmp_path):
    value = "private_requirements"
    (tmp_path / "app.py").write_text("print('hello')\n")
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=["app.py"], use_cache=False,
        requirements_file="${SOURCE}.txt", include_deps=True,
        build_args={"SOURCE": value},
    )
    builder = ImageBuilder(replace(config, build_args=None))

    with pytest.raises(FileNotFoundError) as error:
        builder._create_deps_layer(tmp_path)

    assert value not in "".join(traceback.format_exception(error.value))
    assert "<redacted>" in str(error.value)


def test_requirements_install_redacts_missing_include_after_arguments_are_cleared(tmp_path):
    value = "private_requirements"
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"-r {value}.txt\n")
    config = replace(BuildConfig(build_args={"SOURCE": value}), build_args=None)
    base_config = {"config": {"Env": ["PYTHON_LIBC=glibc-2.17"]}}

    with pytest.raises(FileNotFoundError) as error:
        dependencies_module.install_requirements(
            requirements, tmp_path / "site-packages", tmp_path, "linux", "amd64",
            "3.11", "python:3.11-slim", base_config,
            base_image_tag_verified=True, log_redact=config.redact,
        )

    assert error.value.errno == errno.ENOENT
    assert value not in "".join(traceback.format_exception(error.value))
    assert "<redacted>" in str(error.value)


@pytest.mark.parametrize("operation", ["install", "discovery"])
def test_requirements_read_failure_preserves_errno_and_redacts_retained_arguments(
    tmp_path, monkeypatch, operation
):
    value = "private_requirements"
    requirements = tmp_path / f"{value}.txt"
    requirements.write_text("example==1.0\n")
    config = replace(BuildConfig(build_args={"SOURCE": value}), build_args=None)
    original_open = Path.open

    def deny_requirements(path, *args, **kwargs):
        if path == requirements:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_requirements)
    with pytest.raises(PermissionError) as error:
        if operation == "install":
            dependencies_module._scan_requirements(
                requirements, tmp_path, log_redact=config.redact
            )
        else:
            declared_dependency_names(
                tmp_path, requirements.name, log_redact=config.redact
            )

    assert error.value.errno == errno.EACCES
    assert value not in "".join(traceback.format_exception(error.value))
    assert "<redacted>" in str(error.value)


def test_requirements_read_failure_redacts_error_details_not_present_in_path(
    tmp_path, monkeypatch
):
    value = "private_failure"
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n")
    config = replace(BuildConfig(build_args={"VALUE": value}), build_args=None)
    original_open = Path.open

    def deny_requirements(path, *args, **kwargs):
        if path == requirements:
            raise PermissionError(errno.EACCES, f"Permission denied: {value}", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_requirements)
    with pytest.raises(PermissionError) as error:
        dependencies_module._scan_requirements(
            requirements, tmp_path, log_redact=config.redact
        )

    assert error.value.errno == errno.EACCES
    assert value not in "".join(traceback.format_exception(error.value))
    assert "<redacted>" in str(error.value)


@pytest.mark.parametrize("operation", ["install", "discovery"])
def test_environment_requirements_read_failure_hides_the_entire_path(
    tmp_path, monkeypatch, operation
):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-r ${NESTED_REQUIREMENTS}\n")
    nested = tmp_path / "private-environment-path.txt"
    nested.write_text("example==1.0\n")
    monkeypatch.setenv("NESTED_REQUIREMENTS", nested.name)
    original_open = Path.open

    def deny_nested(path, *args, **kwargs):
        if path == nested:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_nested)
    with pytest.raises(PermissionError) as error:
        if operation == "install":
            dependencies_module._scan_requirements(requirements, tmp_path)
        else:
            declared_dependency_names(tmp_path)

    assert error.value.errno == errno.EACCES
    assert nested.name not in "".join(traceback.format_exception(error.value))
    assert "<environment-based requirements file>" in str(error.value)


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("failure", ["exit", "spawn"])
def test_pip_failure_redacts_retained_build_arguments(tmp_path, monkeypatch, locked, failure):
    value = "private_failure"
    config = replace(BuildConfig(build_args={"VALUE": value}), build_args=None)
    base_config = {"config": {"Env": ["PYTHON_LIBC=glibc-2.17"]}}
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n")

    class FailedPip:
        def wait(self, timeout):
            return 1

    def fake_pip(*args, stderr, **kwargs):
        if failure == "spawn":
            raise OSError(f"Cannot start {value}")
        stderr.write(f"ERROR: Cannot install {value}\n".encode())
        stderr.flush()
        return FailedPip()

    monkeypatch.setattr(dependencies_module.subprocess, "Popen", fake_pip)
    with pytest.raises(RuntimeError) as error:
        if locked:
            dependencies_module.install_locked_requirements(
                [("https://example.test/simple", "example==1.0\n")],
                tmp_path / "stage" / "site-packages", tmp_path, "linux", "amd64",
                "3.11", "python:3.11-slim", base_config,
                base_image_tag_verified=True, log_redact=config.redact,
            )
        else:
            dependencies_module.install_requirements(
                requirements, tmp_path / "stage" / "site-packages", tmp_path,
                "linux", "amd64", "3.11", "python:3.11-slim", base_config,
                base_image_tag_verified=True, log_redact=config.redact,
            )

    assert value not in "".join(traceback.format_exception(error.value))
    if failure == "exit":
        assert "<redacted>" in str(error.value)
    else:
        assert "Unable to start pip" in str(error.value)


@pytest.mark.parametrize("operation", ["pull", "push"])
@pytest.mark.parametrize("copy_args", ["original", "empty", "none", "cleared", "json"])
def test_direct_build_redacts_registry_rate_limit_logs(
    tmp_path, monkeypatch, caplog, capsys, operation, copy_args
):
    value = "private-registry"
    config = BuildConfig(
        base_image="${REGISTRY}.example/team/base:v1",
        tag="${REGISTRY}.example/team/app:v1",
        build_args={"REGISTRY": value},
        context_dir=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        use_cache=False,
    )
    if copy_args in {"empty", "none"}:
        config = replace(config, build_args={} if copy_args == "empty" else None)
    elif copy_args in {"cleared", "json"}:
        config.build_args = None
        if copy_args == "json":
            config = BuildConfig(**json.loads(json.dumps(asdict(config))))
    builder = ImageBuilder(config)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *args, **kwargs: RegistryAuth())

    def observe_and_stop(client, *args, **kwargs):
        client._observe_rate_limit(
            {"ratelimit-limit": "100", "ratelimit-remaining": "0"}, client.base_url
        )
        raise RuntimeError("stopped after rate-limit response")

    monkeypatch.setattr(RegistryClient, "pull_manifest", observe_and_stop)
    monkeypatch.setattr(RegistryClient, "push_blob", observe_and_stop)
    builder.manifest_digest = "sha256:" + "a" * 64
    builder.config_digest = "sha256:" + "b" * 64
    builder.layers = []
    manifest = tmp_path / "out" / "blobs" / "sha256" / ("a" * 64)
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"{}")

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="stopped"):
        if operation == "pull":
            builder._pull_base_image(tmp_path / "layers", "linux", "amd64")
        else:
            builder.push(show_progress=False)

    assert "rate limit" in caplog.text
    assert value not in caplog.text
    assert value not in capsys.readouterr().out


@pytest.mark.parametrize("operation", ["pull", "mirror", "push"])
@pytest.mark.parametrize("cleared", [False, True], ids=["configured", "cleared"])
def test_direct_build_redacts_azure_auth_logs(
    tmp_path, monkeypatch, caplog, operation, cleared
):
    value = "privatehost"
    registry = f"{value}.azurecr.io"
    config = BuildConfig(
        base_image=(
            "base.example/team/base:v1" if operation == "mirror"
            else "${REGISTRY}.azurecr.io/team/base:v1"
        ),
        tag="${REGISTRY}.azurecr.io/team/app:v1",
        registry_mirrors={"base.example": [registry]} if operation == "mirror" else {},
        build_args={"REGISTRY": value},
        context_dir=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        use_cache=False,
    )
    if cleared:
        config.build_args = None
    for provider in (auth_module.EnvironmentAuthProvider, auth_module.DockerConfigAuthProvider):
        monkeypatch.setattr(provider, "get_token", lambda self, registry: None)
        monkeypatch.setattr(provider, "get_credentials", lambda self, registry: None)
    monkeypatch.setattr(
        auth_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1)
    )
    monkeypatch.setattr(
        RegistryClient, "pull_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stopped after auth")),
    )
    monkeypatch.setattr(
        RegistryClient, "push_blob",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stopped after auth")),
    )
    builder = ImageBuilder(config)
    builder.manifest_digest = "sha256:" + "a" * 64
    builder.config_digest = "sha256:" + "b" * 64
    builder.layers = []
    manifest = tmp_path / "out" / "blobs" / "sha256" / ("a" * 64)
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"{}")

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="stopped"):
        if operation == "push":
            builder.push(show_progress=False)
        else:
            builder._pull_base_image(tmp_path / "layers", "linux", "amd64")

    assert "Azure CLI authentication failed" in caplog.text
    assert value not in caplog.text
    assert "<redacted>" in caplog.text


def test_direct_build_redacts_system_package_logs(tmp_path, monkeypatch, caplog):
    value = "libpq5"
    (tmp_path / "app.py").write_text("print('hello')")
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=["app.py"],
        output_dir=str(tmp_path / "out"), use_cache=False,
        build_args={"PACKAGE": value}, system_packages=[value],
        detect_system_packages=False,
    )
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda *args: ([], None))

    with caplog.at_level(logging.WARNING):
        ImageBuilder(config).build()

    assert "Could not verify system packages" in caplog.text
    assert value not in caplog.text


def test_direct_build_redacts_conda_warning_paths(tmp_path, caplog):
    value = "private-requirements"
    (tmp_path / "environment.yml").write_text("name: example\n")
    (tmp_path / f"{value}.txt").write_text("")
    config = BuildConfig(
        context_dir=str(tmp_path), include_deps=True, dry_run=True,
        requirements_file="${NAME}.txt", build_args={"NAME": value},
    )

    with caplog.at_level(logging.WARNING):
        ImageBuilder(config).build()

    assert "Conda inputs ignored" in caplog.text
    assert "<redacted>.txt" in caplog.text
    assert value not in caplog.text


def test_unreadable_base_layer_diagnostic_redacts_build_arguments(tmp_path, caplog):
    value = "private-path"
    config = BuildConfig(
        build_args={"DIRECTORY": value}, system_packages=["libpq5"],
        detect_system_packages=False,
    )
    layer = SimpleNamespace(tar_path=tmp_path / value / "missing.tar")

    with caplog.at_level(logging.DEBUG):
        report_system_packages(config, [layer], redact=config.redact)

    assert "could not read base layer" in caplog.text
    assert value not in caplog.text


def test_build_cache_identity_and_no_implicit_runtime_leakage(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("print('hello')")
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda *args: ([], None))
    cache_dir = tmp_path / "cache"

    def build(version, unused="not-shipped", platform="linux/amd64"):
        config = BuildConfig(
            context_dir=str(tmp_path), include_paths=["app.py"],
            output_dir=str(tmp_path / "image"), cache_dir=str(cache_dir),
            build_args={"VERSION": version, "UNUSED": unused},
            workdir="/srv/${VERSION}", labels={"version": "${VERSION}"}, platform=platform,
        )
        builder = ImageBuilder(config)
        builder.build()
        image = json.loads(
            (tmp_path / "image" / "blobs" / "sha256" / builder.config_digest.split(":")[1]).read_bytes()
        )
        return config, builder, image

    first, builder, image = build("release-one")
    metadata = json.dumps(image)
    assert image["config"]["WorkingDir"] == "/srv/release-one"
    assert image["config"]["Labels"]["version"] == "release-one"
    assert "VERSION=" not in metadata and "UNUSED" not in metadata and "not-shipped" not in metadata
    assert image["rootfs"]["diff_ids"] == [layer.digest for layer in builder.layers]
    with tarfile.open(builder.layers[-1].tar_path) as archive:
        assert archive.getnames() == ["srv/release-one/app.py"]
    assert len(json.loads((cache_dir / "index.json").read_text())) == 1
    repeat, _, _ = build("release-one", unused="also-not-shipped")
    assert repeat.cache_identity() == first.cache_identity()
    assert len(json.loads((cache_dir / "index.json").read_text())) == 1
    build("release-two")
    build("release-two", platform="linux/arm64")
    entries = json.loads((cache_dir / "index.json").read_text())
    assert len(entries) == 3
    assert all(entry["source_files"] == [] for entry in entries.values())
    assert "release-one" not in json.dumps(entries) and "release-two" not in json.dumps(entries)
    assert first.cache_identity("other-backend") != first.cache_identity()
    cache = LayerCache(cache_dir)
    assert cache.get_layer(
        [(tmp_path / "app.py", Path("app.py"))],
        workdir=first.workdir, namespace=first.cache_identity("other-backend"),
    ) is None


@pytest.mark.parametrize("arguments", [{}, None])
def test_cache_hides_source_paths_after_arguments_are_cleared(tmp_path, monkeypatch, arguments):
    value = "private-source"
    (tmp_path / f"{value}.py").write_text("print('hello')")
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda *args: ([], None))
    cache_dir = tmp_path / "cache"
    original = BuildConfig(
        context_dir=str(tmp_path), include_paths=["${SOURCE}.py"],
        output_dir=str(tmp_path / "image"), cache_dir=str(cache_dir),
        build_args={"SOURCE": value},
    )

    ImageBuilder(replace(original, build_args=arguments)).build()

    entries = json.loads((cache_dir / "index.json").read_text())
    assert entries
    assert all(entry["source_files"] == [] for entry in entries.values())
    assert value not in json.dumps(entries)


@pytest.mark.parametrize("copy_kind", ["replace", "json"])
@pytest.mark.parametrize("persist_explicit", [False, True])
def test_retained_arguments_do_not_implicitly_persist_in_build_outputs(
    tmp_path, monkeypatch, copy_kind, persist_explicit
):
    previous, current = "private-previous-value", "private-current-value"
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n")
    output, cache, history = (tmp_path / name for name in ("image", "cache", "history"))
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda *args: ([], None))
    original = BuildConfig(
        context_dir=str(context), include_paths=["app.py"], output_dir=str(output),
        cache_dir=str(cache), record_history=True, history_dir=str(history),
        env={"EXPLICIT": "${VALUE}"} if persist_explicit else {},
        build_args={"VALUE": previous},
    )
    changed = replace(original, build_args={"VALUE": current})
    if copy_kind == "replace":
        cleared = replace(changed, build_args=None)
    else:
        data = json.loads(json.dumps(asdict(changed)))
        data["build_args"] = None
        cleared = BuildConfig(**data)

    builder = ImageBuilder(cleared)
    builder.build()

    metadata = json.loads(
        (output / "blobs" / "sha256" / builder.config_digest.split(":")[1]).read_bytes()
    )
    assert (f"EXPLICIT={previous}" in metadata["config"].get("Env", [])) is persist_explicit
    for directory in (output, cache, history):
        files = [path for path in directory.rglob("*") if path.is_file()]
        assert files
        contents = b"".join(path.read_bytes() for path in files)
        assert current.encode() not in contents
        assert (previous.encode() in contents) is (persist_explicit and directory == output)
    assert json.loads((history / "history.json").read_text())["records"][-1]["status"] == "success"
    entries = json.loads((cache / "index.json").read_text())
    assert entries and all(entry["source_files"] == [] for entry in entries.values())


def test_storage_locations_do_not_invalidate_equal_source_layer(tmp_path):
    import hashlib

    source = tmp_path / "app.py"
    source.write_text("print('hello')")
    first = BuildConfig(context_dir="first", output_dir="out-one", cache_dir="cache-one")
    second = BuildConfig(context_dir="second", output_dir="out-two", cache_dir="cache-two")
    cache = LayerCache(tmp_path / "cache")
    files = [(source, Path("app.py"))]
    layer = tmp_path / "layer.tar"
    layer.write_bytes(b"layer")
    digest = "sha256:" + hashlib.sha256(layer.read_bytes()).hexdigest()
    cache.store_layer(files, digest, layer, namespace=first.cache_identity())
    assert cache.get_layer(files, namespace=second.cache_identity()) is not None
