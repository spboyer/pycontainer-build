from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from pycontainer.config_loader import config_from_file


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "editor-task-runners"


@pytest.fixture(scope="session")
def python_with_spaces(tmp_path_factory):
    env_dir = tmp_path_factory.mktemp("interpreter") / "python $(echo SYNTHETIC) environment"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(env_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    return env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.fixture
def fake_cli(tmp_path, python_with_spaces):
    project = tmp_path / "project $(echo SYNTHETIC) with spaces"
    nested = project / "nested directory"
    nested.mkdir(parents=True)
    (project / "pycontainer.toml").write_text("[build]\ntag = 'demo:local'\n")
    (project / "pyproject.toml").write_text("[project]\nname = 'synthetic'\nversion = '0.0.0'\n")
    (nested / "app.py").write_text("print('synthetic app')\n")

    package = tmp_path / "fake module" / "pycontainer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        """
import json
import os
from pathlib import Path
import sys

Path(os.environ["FAKE_CLI_LOG"]).write_text(json.dumps({
    "args": sys.argv[1:],
    "cwd": str(Path.cwd()),
}))
print("synthetic CLI output")
sys.exit(int(os.environ.get("FAKE_CLI_EXIT", "0")))
"""
    )
    log = tmp_path / "fake cli log.json"
    env = os.environ.copy()
    env.update(
        PATH=str(python_with_spaces.parent) + os.pathsep + env.get("PATH", ""),
        PYTHONPATH=str(package.parent),
        FAKE_CLI_LOG=str(log),
    )
    return project, nested, python_with_spaces, log, env


def assert_invocation(log, project, dry_run, expected_files=None):
    invocation = json.loads(log.read_text())
    arguments = invocation["args"]
    assert arguments[0:2] == ["build", "--context"]
    assert (project / arguments[2]).resolve() == project
    assert arguments[3] == "--config"
    assert (project / arguments[4]).resolve() == project / "pycontainer.toml"
    assert arguments[5:] == (["--dry-run"] if dry_run else [])
    assert Path(invocation["cwd"]) == project
    if expected_files is None:
        expected_files = {
            Path("pycontainer.toml"),
            Path("pyproject.toml"),
            Path("nested directory") / "app.py",
        }
    assert {path.relative_to(project) for path in project.rglob("*") if path.is_file()} == expected_files
    assert (project / "pycontainer.toml").read_text() == "[build]\ntag = 'demo:local'\n"
    assert (project / "nested directory" / "app.py").read_text() == "print('synthetic app')\n"


def tool_or_skip(name, override):
    tool = os.environ.get(override) or shutil.which(name)
    if not tool:
        pytest.skip(f"{name} is not installed")
    return tool


@pytest.mark.parametrize(
    ("name", "dry_run"),
    [("Pycontainer_Build.run.xml", False), ("Pycontainer_Dry_Run.run.xml", True)],
)
@pytest.mark.parametrize("exit_code", [0, 23])
def test_pycharm_module_configuration_arguments(fake_cli, name, dry_run, exit_code):
    project, _, python, log, env = fake_cli
    configuration = ET.parse(EXAMPLES / "pycharm" / name).getroot().find("configuration")
    assert configuration is not None
    assert configuration.attrib["type"] == "PythonConfigurationType"
    options = {
        option.attrib["name"]: option.attrib.get("value", "")
        for option in configuration.findall("option")
    }
    assert options["MODULE_MODE"] == "true"
    assert options["IS_MODULE_SDK"] == "true"
    assert options["SCRIPT_NAME"] == "pycontainer.cli"
    assert options["WORKING_DIRECTORY"] == "$PROJECT_DIR$"
    parameters = options["PARAMETERS"]
    env["FAKE_CLI_EXIT"] = str(exit_code)
    result = subprocess.run(
        [str(python), "-m", options["SCRIPT_NAME"], *shlex.split(parameters)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == exit_code, result.stderr
    assert_invocation(log, project, dry_run)


@pytest.mark.parametrize(
    ("target", "dry_run"),
    [("pycontainer-build", False), ("pycontainer-dry-run", True)],
)
@pytest.mark.parametrize("exit_code", [0, 23])
def test_makefile_invokes_cli_and_propagates_failure(fake_cli, target, dry_run, exit_code):
    project, _, _, log, env = fake_cli
    make = tool_or_skip("make", "PYCONTAINER_TEST_MAKE")
    env["FAKE_CLI_EXIT"] = str(exit_code)
    result = subprocess.run(
        [
            make,
            "-f",
            str(EXAMPLES / "tasks" / "Makefile.pycontainer"),
            target,
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == (exit_code == 0), result.stdout + result.stderr
    assert_invocation(log, project, dry_run)


@pytest.mark.parametrize(("target", "dry_run"), [("build", False), ("dry-run", True)])
@pytest.mark.parametrize("exit_code", [0, 23])
def test_taskfile_invokes_cli_and_propagates_failure(fake_cli, target, dry_run, exit_code):
    project, _, _, log, env = fake_cli
    task = tool_or_skip("task", "PYCONTAINER_TEST_TASK")
    env["FAKE_CLI_EXIT"] = str(exit_code)
    result = subprocess.run(
        [
            task,
            "--taskfile",
            str(EXAMPLES / "tasks" / "Taskfile.pycontainer.yml"),
            "--dir",
            ".",
            target,
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == (exit_code == 0), result.stdout + result.stderr
    assert_invocation(log, project, dry_run)


@lru_cache
def vim_uses_unix_paths(vim):
    result = subprocess.run(
        [vim, "-Nu", "NONE", "-i", "NONE", "-n", "-es", "-V1", "-c",
         "echo printf('PYCONTAINER_VIM_CAPS=%d,%d', has('win32'), has('job'))",
         "-c", "qall!"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    capabilities = re.search(r"PYCONTAINER_VIM_CAPS=(\d),(\d)", result.stdout + result.stderr)
    if result.returncode or capabilities is None:
        pytest.fail(f"Could not identify Vim's capabilities: {result.stderr}")
    if capabilities.group(2) != "1":
        pytest.skip("Vim was built without +job support")
    return os.name == "nt" and capabilities.group(1) != "1"


def vim_path(value, unix_paths):
    path = Path(value)
    if unix_paths:
        return "/" + path.drive[0].lower() + path.as_posix()[2:]
    return str(path)


def vim_literal(value, unix_paths):
    return "'" + vim_path(value, unix_paths).replace("'", "''") + "'"


@pytest.mark.parametrize(
    ("command", "dry_run"), [("PycontainerBuild", False), ("PycontainerDryRun", True)]
)
@pytest.mark.parametrize("exit_code", [0, 23])
def test_vim_commands_invoke_cli_and_report_failure(
    fake_cli, tmp_path, command, dry_run, exit_code
):
    project, nested, python, log, env = fake_cli
    vim = tool_or_skip("vim", "PYCONTAINER_TEST_VIM")
    unix_paths = vim_uses_unix_paths(vim)
    status = tmp_path / "vim status.txt"
    script = tmp_path / "run vim.vim"
    script.write_text(
        "\n".join(
            [
                f"let g:pycontainer_python = {vim_literal(python, unix_paths)}",
                "execute 'source ' . fnameescape("
                + vim_literal(EXAMPLES / "vim" / "pycontainer.vim", unix_paths)
                + ")",
                "execute 'edit ' . fnameescape("
                + vim_literal(nested / "app.py", unix_paths)
                + ")",
                f"let b:pycontainer_project_root = {vim_literal(project, unix_paths)}",
                "let g:pycontainer_test_error = ''",
                "try",
                f"  {command}",
                "catch /^pycontainer: process exited with status /",
                "  let g:pycontainer_test_error = v:exception",
                "endtry",
                "call writefile([string(g:pycontainer_last_status), g:pycontainer_test_error], "
                + vim_literal(status, unix_paths)
                + ")",
                "if g:pycontainer_last_status != 0",
                "  cquit",
                "endif",
                "qall!",
                "",
            ]
        )
    )
    env["FAKE_CLI_EXIT"] = str(exit_code)
    result = subprocess.run(
        [vim, "-Nu", "NONE", "-i", "NONE", "-n", "-es", "-V1",
         "-S", vim_path(script, unix_paths)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    status_lines = status.read_text().splitlines()
    assert status_lines[0] == str(exit_code), result.stdout + result.stderr
    assert status_lines[1] == (
        f"pycontainer: process exited with status {exit_code}" if exit_code else ""
    )
    assert (result.returncode == 0) == (exit_code == 0), result.stdout + result.stderr
    assert_invocation(log, project, dry_run)


@pytest.mark.parametrize(
    ("command", "dry_run"), [("pycontainer-build", False), ("pycontainer-dry-run", True)]
)
@pytest.mark.parametrize("exit_code", [0, 23])
def test_emacs_commands_invoke_cli_and_report_failure(fake_cli, command, dry_run, exit_code):
    project, nested, python, log, env = fake_cli
    emacs = tool_or_skip("emacs", "PYCONTAINER_TEST_EMACS")
    expression = (
        "(progn "
        f"(setq default-directory (file-name-as-directory {json.dumps(str(nested))})) "
        f"(setq pycontainer-python-executable {json.dumps(str(python))}) "
        f"(setq-local pycontainer-project-root {json.dumps(str(project))}) "
        f"({command}))"
    )
    env["FAKE_CLI_EXIT"] = str(exit_code)
    result = subprocess.run(
        [
            emacs,
            "--quick",
            "--batch",
            "--load",
            str(EXAMPLES / "emacs" / "pycontainer.el"),
            "--eval",
            expression,
        ],
        cwd=nested,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == (exit_code == 0), result.stdout + result.stderr
    if exit_code:
        assert str(exit_code) in result.stderr
    assert_invocation(log, project, dry_run)


@pytest.mark.parametrize("editor", ["vim", "emacs"])
@pytest.mark.parametrize(
    ("boundary", "config_state", "root_setting"),
    [
        ("pyproject.toml", "missing", "project"),
        ("pyproject.toml", "directory", "project"),
        (".git", "missing", "project"),
        (".git", "directory", "project"),
        ("unmarked", "missing", "unset"),
        ("unmarked", "directory", "unset"),
        ("unmarked", "valid", "unset"),
        ("unmarked", "valid", "empty"),
        ("unmarked", "valid", "nonexistent"),
        ("pyproject.toml", "valid", "unset"),
        (".git", "valid", "unset"),
    ],
)
def test_editor_rejects_unscoped_or_invalid_config_without_falling_back(
    fake_cli, tmp_path, editor, boundary, config_state, root_setting
):
    project, nested, python, log, env = fake_cli
    if config_state != "valid":
        (project / "pycontainer.toml").unlink()
    if config_state == "directory":
        (project / "pycontainer.toml").mkdir()
    if boundary in {".git", "unmarked"}:
        (project / "pyproject.toml").unlink()
    if boundary == ".git":
        (project / ".git").mkdir()
    (tmp_path / "pycontainer.toml").write_text("[build]\ntag = 'wrong-project:local'\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'wrong-project'\n")
    if editor == "vim":
        executable = tool_or_skip("vim", "PYCONTAINER_TEST_VIM")
        unix_paths = vim_uses_unix_paths(executable)
        if root_setting in {"project", "nonexistent"}:
            root = project if root_setting == "project" else project / "missing"
            root_assignment = f"let b:pycontainer_project_root = {vim_literal(root, unix_paths)}"
        elif root_setting == "empty":
            root_assignment = "let b:pycontainer_project_root = ''"
        else:
            root_assignment = ""
        script = tmp_path / "missing config.vim"
        script.write_text(
            "\n".join(
                [
                    f"let g:pycontainer_python = {vim_literal(python, unix_paths)}",
                    "execute 'source ' . fnameescape("
                    + vim_literal(EXAMPLES / "vim" / "pycontainer.vim", unix_paths)
                    + ")",
                    "execute 'edit ' . fnameescape("
                    + vim_literal(nested / "app.py", unix_paths)
                    + ")",
                    root_assignment,
                    "try",
                    "  PycontainerDryRun",
                    "catch /^pycontainer:/",
                    "  echomsg v:exception",
                    "  cquit",
                    "endtry",
                    "qall!",
                    "",
                ]
            )
        )
        command = [
            executable, "-Nu", "NONE", "-i", "NONE", "-n", "-es", "-V1",
            "-S", vim_path(script, unix_paths)
        ]
    else:
        executable = tool_or_skip("emacs", "PYCONTAINER_TEST_EMACS")
        if root_setting == "project":
            root = str(project)
        elif root_setting == "nonexistent":
            root = str(project / "missing")
        else:
            root = ""
        root_assignment = (
            f"(setq-local pycontainer-project-root {json.dumps(root)}) "
            if root_setting != "unset" else ""
        )
        expression = (
            "(progn "
            f"(setq default-directory (file-name-as-directory {json.dumps(str(nested))})) "
            f"(setq pycontainer-python-executable {json.dumps(str(python))}) "
            f"{root_assignment}"
            "(pycontainer-dry-run))"
        )
        command = [
            executable,
            "--quick",
            "--batch",
            "--load",
            str(EXAMPLES / "emacs" / "pycontainer.el"),
            "--eval",
            expression,
        ]
    result = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
    )
    diagnostic = log.read_text() if log.exists() else result.stdout + result.stderr
    assert result.returncode != 0, diagnostic
    if root_setting == "project":
        expected_error = "pycontainer.toml"
    elif root_setting == "nonexistent":
        expected_error = "not a directory"
    elif editor == "emacs":
        expected_error = "project-root"
    else:
        expected_error = "project_root"
    assert expected_error in (result.stdout + result.stderr).lower()
    assert not log.exists()


@pytest.mark.parametrize("editor", ["vim", "emacs"])
def test_editor_uses_explicit_buffer_project_root(fake_cli, tmp_path, editor):
    project, nested, python, log, env = fake_cli
    (project / "pyproject.toml").unlink()
    (nested / "pyproject.toml").write_text("[project]\nname = 'nested'\n")
    if editor == "vim":
        executable = tool_or_skip("vim", "PYCONTAINER_TEST_VIM")
        unix_paths = vim_uses_unix_paths(executable)
        script = tmp_path / "explicit project.vim"
        script.write_text(
            "\n".join(
                [
                    f"let g:pycontainer_python = {vim_literal(python, unix_paths)}",
                    "execute 'source ' . fnameescape("
                    + vim_literal(EXAMPLES / "vim" / "pycontainer.vim", unix_paths)
                    + ")",
                    "execute 'edit ' . fnameescape("
                    + vim_literal(nested / "app.py", unix_paths)
                    + ")",
                    f"let b:pycontainer_project_root = {vim_literal(project, unix_paths)}",
                    "try",
                    "  PycontainerDryRun",
                    "catch",
                    "  echomsg v:exception",
                    "  cquit 1",
                    "endtry",
                    "qall!",
                    "",
                ]
            )
        )
        command = [
            executable, "-Nu", "NONE", "-i", "NONE", "-n", "-es", "-V1",
            "-S", vim_path(script, unix_paths)
        ]
    else:
        executable = tool_or_skip("emacs", "PYCONTAINER_TEST_EMACS")
        command = [
            executable,
            "--quick",
            "--batch",
            "--load",
            str(EXAMPLES / "emacs" / "pycontainer.el"),
            "--eval",
            "(progn "
            f"(setq default-directory (file-name-as-directory {json.dumps(str(nested))})) "
            f"(setq pycontainer-python-executable {json.dumps(str(python))}) "
            f"(setq-local pycontainer-project-root {json.dumps(str(project))}) "
            "(pycontainer-dry-run))",
        ]
    result = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert_invocation(
        log,
        project,
        True,
        {
            Path("pycontainer.toml"),
            Path("nested directory") / "app.py",
            Path("nested directory") / "pyproject.toml",
        },
    )


def test_example_config_runs_real_cli_dry_run_without_writes(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    shutil.copyfile(EXAMPLES / "pycontainer.example.toml", project / "pycontainer.toml")
    (project / "app.py").write_text("print('synthetic app')\n")
    assert config_from_file(project / "pycontainer.toml").output_dir == "dist/pycontainer-example"

    def snapshot():
        return {
            path.relative_to(project): path.read_bytes() if path.is_file() else None
            for path in project.rglob("*")
        }

    before = snapshot()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pycontainer.cli",
            "build",
            "--context",
            str(project),
            "--config",
            str(project / "pycontainer.toml"),
            "--dry-run",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "APP_MODE=example" in result.stdout
    assert "example.project=synthetic" in result.stdout
    assert snapshot() == before
