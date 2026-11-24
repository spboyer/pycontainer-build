"""Optional real pre-commit integration tests; see pre-commit-requirements.txt."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

pytest.importorskip("pre_commit", reason="install tests/pre-commit-requirements.txt for hook tests")
from pre_commit.clientlib import load_manifest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _run(command, cwd, env):
    return subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, timeout=120,
    )


def _check(command, cwd, env):
    result = _run(command, cwd, env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _snapshot(root):
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
            path.stat().st_mode,
        )
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


@pytest.fixture(scope="module")
def installed_hook(tmp_path_factory):
    root = tmp_path_factory.mktemp("pre-commit hook")
    source = root / "hook source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", ".pre-commit-hooks.yaml"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    home = root / "home"
    home.mkdir()
    guard = root / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import socket\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise RuntimeError('hook execution must be offline and read-only')\n"
        "socket.socket.connect = forbidden\n"
        "socket.create_connection = forbidden\n"
        "import importlib.util\n"
        "if importlib.util.find_spec('pycontainer') is not None:\n"
        "    from pycontainer import cli, builder, cache, registry_client\n"
        "    cli.ImageBuilder = builder.ImageBuilder = forbidden\n"
        "    cache.LayerCache = registry_client.RegistryClient = forbidden\n"
    )
    pip_config = root / "pip.ini"
    pip_config.write_text(
        "[global]\nno-index = true\ndisable-pip-version-check = true\n"
        "[install]\nno-build-isolation = true\nno-deps = true\n"
    )
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    # pip's negative boolean option uses 0 to disable build isolation.
    env.update(
        HOME=str(home), USERPROFILE=str(home), PRE_COMMIT_HOME=str(root / "hook cache"),
        PYTHONPATH=str(guard), PYTHONDONTWRITEBYTECODE="1",
        PIP_CONFIG_FILE=str(pip_config), PIP_NO_INDEX="1", PIP_NO_BUILD_ISOLATION="0",
        VIRTUALENV_SETUPTOOLS="bundle", VIRTUALENV_NO_PERIODIC_UPDATE="1",
        VIRTUALENV_DOWNLOAD="0", GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    _check(["git", "init", "-q", str(source)], root, env)
    _check(["git", "add", "."], source, env)
    _check(
        ["git", "-c", "user.name=Hook Test", "-c", "user.email=hook@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "hook fixture"],
        source, env,
    )
    revision = _check(["git", "rev-parse", "HEAD"], source, env).stdout.strip()
    project = root / "consumer repository"
    project.mkdir()
    _check(["git", "init", "-q", str(project)], root, env)
    config = {
        "repos": [{
            "repo": str(source), "rev": revision,
            "hooks": [{"id": "pycontainer-validate-config", "files": r"\.toml$"}],
        }],
    }
    (project / ".pre-commit-config.yaml").write_text(yaml.safe_dump(config))
    (project / "pycontainer.toml").write_text('[build]\ntag = "warm:1"\n')
    _check(["git", "add", "."], project, env)
    # Install from local source with bundled virtualenv seeds and no package index.
    _check(
        [sys.executable, "-m", "pre_commit", "run", "--all-files"],
        project, env,
    )
    executables = list((root / "hook cache").rglob(
        "pycontainer.exe" if os.name == "nt" else "pycontainer"
    ))
    executable = next(path for path in executables if path.parent.name in ("Scripts", "bin"))
    return project, home, env, executable


def test_manifest_and_default_selection():
    hook, = load_manifest(str(ROOT / ".pre-commit-hooks.yaml"))

    assert hook["language"] == "python"
    assert hook["entry"] == "pycontainer validate config --"
    assert re.search(hook["files"], "service with spaces/pycontainer.toml")
    assert not re.search(hook["files"], "pyproject.toml")


@pytest.mark.parametrize("invalid", [False, True])
def test_real_hook_multiple_paths_and_no_side_effects(installed_hook, invalid):
    project, home, env, executable = installed_hook
    first = project / "first service" / "pycontainer.toml"
    first.parent.mkdir(exist_ok=True)
    first.write_text('[build]\ntag = "first:1"\n')
    second = project / "second service" / "pyproject.toml"
    second.parent.mkdir(exist_ok=True)
    second.write_text(
        '[tool.pycontainer]\nplatform = "bad"\n' if invalid
        else '[tool.pycontainer]\ntag = "second:1"\n'
    )
    dash = project / "-config with spaces.toml"
    dash.write_text('[build\n' if invalid else '[build]\ntag = "dash:1"\n')
    paths = [str(path.relative_to(project)) for path in (first, second, dash)]
    _check(["git", "add", "."], project, env)
    before = _snapshot(project), _snapshot(home)

    direct = _run([str(executable), "validate", "config", "--", *paths], project, env)
    assert direct.returncode == (2 if invalid else 0), direct.stdout + direct.stderr
    result = _run(
        [sys.executable, "-m", "pre_commit", "run", "--verbose", "--files",
         *("./" + path for path in paths)],
        project, env,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (1 if invalid else 0), output
    for path in paths:
        assert path.replace("\\", "/") in output.replace("\\", "/"), output
    if invalid:
        assert "exit code: 2" in output
        assert "[tool.pycontainer].platform: Invalid platform format" in output
        assert "2 of 3 configuration(s) invalid" in output
    assert "Not performed" in output
    assert (_snapshot(project), _snapshot(home)) == before


def test_missing_file_is_not_validated_as_defaults(installed_hook):
    project, home, env, executable = installed_hook
    before = _snapshot(project), _snapshot(home)
    result = _run(
        [str(executable), "validate", "config", "--", "missing config.toml"],
        project, env,
    )
    assert result.returncode == 2
    assert "missing config.toml: Config file not found" in result.stderr
    assert (_snapshot(project), _snapshot(home)) == before


def test_default_selection_skips_unrelated_pyprojects(installed_hook):
    project, home, env, executable = installed_hook
    path = project / "unrelated project" / "pyproject.toml"
    path.parent.mkdir()
    path.write_text('[project]\nname = "unrelated"\n')
    config = yaml.safe_load((project / ".pre-commit-config.yaml").read_text())
    del config["repos"][0]["hooks"][0]["files"]
    selection = project / ".selection-pre-commit.yaml"
    selection.write_text(yaml.safe_dump(config))
    _check(["git", "add", "."], project, env)
    before = _snapshot(project), _snapshot(home)

    result = _run(
        [sys.executable, "-m", "pre_commit", "run", "-c", str(selection),
         "--files", str(path.relative_to(project))],
        project, env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Skipped" in result.stdout
    assert "ok:" not in result.stdout

    selected = _run(
        [sys.executable, "-m", "pre_commit", "run", "--files",
         str(path.relative_to(project))],
        project, env,
    )
    assert selected.returncode == 1, selected.stdout + selected.stderr
    assert "exit code: 2" in selected.stdout + selected.stderr
    assert "defines neither [build] nor [tool.pycontainer]" in selected.stdout + selected.stderr

    explicit = _run(
        [str(executable), "validate", "config", "--", str(path.relative_to(project))],
        project, env,
    )
    assert explicit.returncode == 2
    assert "defines neither [build] nor [tool.pycontainer]" in explicit.stdout + explicit.stderr
    assert (_snapshot(project), _snapshot(home)) == before


def test_missing_cli_fails_clearly(installed_hook):
    project, _, env, executable = installed_hook
    # Remove ambient installs (for example an activated CI venv) from PATH.
    path = os.pathsep.join(
        entry for entry in env.get("PATH", "").split(os.pathsep)
        if entry and shutil.which("pycontainer", path=entry) is None
    )
    env = {**env, "PATH": path}
    env.pop("VIRTUAL_ENV", None)
    assert shutil.which("git", path=path), "git must remain resolvable"
    hidden = executable.with_name(executable.name + ".hidden")
    executable.rename(hidden)
    try:
        assert shutil.which("pycontainer", path=path) is None
        result = _run(
            [sys.executable, "-m", "pre_commit", "run", "--all-files"],
            project, env,
        )
    finally:
        hidden.rename(executable)
    assert result.returncode == 1
    assert "Executable `pycontainer` not found" in result.stdout + result.stderr
