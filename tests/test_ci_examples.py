"""Offline CLI contracts for the standalone CI examples (not provider execution)."""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

from pycontainer import cli
from pycontainer.auth import get_registry_auth
from pycontainer.registry_client import RegistryClient


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "ci"
COMMAND = re.compile(
    r"(?:python -m pycontainer\.cli|pycontainer|"
    r"python \.ci[/\\]+pycontainer_ci\.py)\s+(build\b.*)"
)


def _commands(text):
    # Support POSIX, batch and PowerShell continuation characters.
    text = re.sub(r"[\\^`]\s*\n\s*", " ", text)
    return [shlex.split(match.group(1)) for match in COMMAND.finditer(text)]


def _parse(monkeypatch, arguments):
    class Parsed(Exception):
        pass

    original = argparse.ArgumentParser.parse_args
    captured = []

    def capture(parser, *args, **kwargs):
        captured.append(original(parser, *args, **kwargs))
        raise Parsed()

    with monkeypatch.context() as patch:
        patch.setattr(sys, "argv", ["pycontainer", *arguments])
        patch.setattr(argparse.ArgumentParser, "parse_args", capture)
        with pytest.raises(Parsed):
            cli.main()
    return captured[0]


@pytest.mark.parametrize("filename,count", [
    ("gitlab-ci.yml", 2),
    ("Jenkinsfile", 2),
    ("circleci-config.yml", 1),
])
def test_ci_build_commands_use_real_parser(monkeypatch, filename, count):
    text = (EXAMPLES / filename).read_text(encoding="utf-8")
    commands = _commands(text)
    assert len(commands) == count, "Every shell branch must contain a validated build"
    assert len(re.findall(r"python \.ci[/\\]+pycontainer_ci\.py\s+build\b", text)) == count, (
        "Every build must use the registry-scoping helper"
    )
    for arguments in commands:
        args = _parse(monkeypatch, arguments)
        assert args.cmd == "build"
        assert args.push
        assert args.tag and len(args.tag) == 1
        assert args.platform == "linux/amd64"
        assert args.base_image == "python:3.11-slim"
        assert args.username is None and args.password is None
        assert args.registry is None, "Use a full tag, not a host-only registry override"
        assert args.cache_dir.startswith(("$CI_PROJECT_DIR/", "$HOME/", "%USERPROFILE%"))
        assert ".ci" in args.exclude_paths
    assert re.search(r"git\+https://github\.com/spboyer/pycontainer-build\.git@[0-9a-f]{40}\"", text)


def test_every_ci_example_is_validated():
    assert {p.name for p in EXAMPLES.iterdir() if p.is_file()} == {
        "gitlab-ci.yml", "Jenkinsfile", "circleci-config.yml", "pycontainer_ci.py"}


def _top_level_keys(text):
    return {m.group(1) for m in re.finditer(r"^([A-Za-z_][\w-]*):", text, re.M)}


@pytest.mark.parametrize("filename", ["gitlab-ci.yml", "circleci-config.yml"])
def test_yaml_examples_are_space_indented(filename):
    text = (EXAMPLES / filename).read_text(encoding="utf-8")
    assert "\t" not in text


def test_gitlab_structure_and_secret_mapping():
    text = (EXAMPLES / "gitlab-ci.yml").read_text(encoding="utf-8")
    assert {"stages", "build-image"} <= _top_level_keys(text)
    assert 'REGISTRY_USERNAME="$CI_REGISTRY_USER"' in text
    assert 'REGISTRY_PASSWORD="$CI_JOB_TOKEN"' in text
    assert "--cache-dir \"$CI_PROJECT_DIR/.pycontainer-cache\"" in text


def test_circleci_structure_context_and_cache():
    text = (EXAMPLES / "circleci-config.yml").read_text(encoding="utf-8")
    assert {"version", "jobs", "workflows"} <= _top_level_keys(text)
    assert "context: registry-push" in text
    assert text.index("restore_cache") < text.index("pycontainer_ci.py build") < text.index("save_cache")
    assert "/home/circleci/.pycontainer/cache" in text


def test_jenkins_structure_and_credentials():
    text = (EXAMPLES / "Jenkinsfile").read_text(encoding="utf-8")
    assert text.count("{") == text.count("}")
    assert text.lstrip().splitlines()[1].startswith("pipeline {")
    assert "usernameVariable: 'REGISTRY_USERNAME'" in text
    assert "passwordVariable: 'REGISTRY_PASSWORD'" in text
    # An explicit checkout after skipDefaultCheckout leaves env.GIT_COMMIT unset.
    assert "skipDefaultCheckout" not in text
    assert '"""' not in text, "Groovy-interpolated strings could leak secrets"


def test_example_command_validation_rejects_unknown_flags(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _parse(monkeypatch, _commands(
            'python -m pycontainer.cli build --tag app:v1 --nonexistent-ci-flag'
        )[0])
    assert exc.value.code == 2


def test_example_command_extraction_includes_continued_flags():
    assert _commands(
        'python -m pycontainer.cli build \\\n --tag "app:v1" ^\n --push'
    ) == [["build", "--tag", "app:v1", "--push"]]


def _registry_helper():
    spec = importlib.util.spec_from_file_location(
        "ci_registry_build", EXAMPLES / "pycontainer_ci.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tag,registry", [
    ("registry.example.com:5000/team/app:v1.2.3", "registry.example.com:5000"),
    ("ghcr.io/team/app:v1", "ghcr.io"),
])
def test_ci_credentials_are_scoped_to_push_registry(monkeypatch, tmp_path, tag, registry):
    helper = _registry_helper()
    monkeypatch.setenv("REGISTRY_USERNAME", "synthetic-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "synthetic-password")
    monkeypatch.setenv("REGISTRY_TOKEN", "synthetic-registry-token")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-github-token")
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "existing-azure-state"))
    arguments = [
        "build", "--tag", tag, "--context", str(tmp_path),
        "--cache-dir", str(tmp_path / "cache"), "--push",
    ]
    homes = []

    def run(command, *, env):
        assert command == [sys.executable, "-m", "pycontainer.cli", *arguments]
        assert "synthetic-password" not in command
        assert env["HOME"] == env["USERPROFILE"]
        assert not any(key in env for key in (
            "REGISTRY_USERNAME", "REGISTRY_PASSWORD", "REGISTRY_TOKEN", "GITHUB_TOKEN",
            "AZURE_CONFIG_DIR",
        ))
        homes.append(Path(env["HOME"]))
        config = homes[-1] / ".docker" / "config.json"
        assert config.is_file()
        if os.name != "nt":
            assert config.stat().st_mode & 0o077 == 0
        with monkeypatch.context() as patch:
            patch.setattr(os, "environ", env)
            base_auth = get_registry_auth("docker.io")
            push_auth = get_registry_auth(registry)
        assert base_auth.bearer_token is None
        assert base_auth.username is None and base_auth.password is None
        base_client = RegistryClient(
            "docker.io", "library/python",
            auth_token=base_auth.bearer_token,
            username=base_auth.username, password=base_auth.password,
        )
        assert not base_client._authorization_headers(
            "https://registry-1.docker.io/v2/library/python/manifests/3.11-slim"
        )
        assert push_auth.bearer_token is None
        assert (push_auth.username, push_auth.password) == (
            "synthetic-user", "synthetic-password"
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(helper.subprocess, "run", run)
    assert helper.main(arguments) == 0
    assert len(homes) == 1 and not homes[0].exists()


def test_ci_helper_refuses_credentials_inside_build_context(monkeypatch, tmp_path):
    helper = _registry_helper()
    monkeypatch.setenv("REGISTRY_USERNAME", "synthetic-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "synthetic-password")
    monkeypatch.setattr(helper.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not build"))
    with pytest.raises(SystemExit) as exc:
        helper.main(["build", "--tag", "registry.example.com/team/app:v1",
                     "--context", str(tmp_path), "--cache-dir", str(tmp_path / "cache")])
    assert exc.value.code != 0


def test_ci_helper_refuses_credentials_inside_cache(monkeypatch, tmp_path):
    helper = _registry_helper()
    monkeypatch.setenv("REGISTRY_USERNAME", "synthetic-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "synthetic-password")
    monkeypatch.setattr(helper.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not build"))
    context = tmp_path / "app"
    context.mkdir()
    with pytest.raises(SystemExit) as exc:
        helper.main(["build", "--tag", "registry.example.com/team/app:v1",
                     "--context", str(context), "--cache-dir", str(tmp_path)])
    assert exc.value.code != 0


def test_ci_helper_propagates_failure_and_cleans_credentials(monkeypatch, tmp_path):
    helper = _registry_helper()
    monkeypatch.setenv("REGISTRY_USERNAME", "synthetic-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "synthetic-password")
    homes = []

    def run(command, *, env):
        homes.append(Path(env["HOME"]))
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(helper.subprocess, "run", run)
    assert helper.main(["build", "--tag", "registry.example.com/team/app:v1",
                        "--context", str(tmp_path), "--no-cache"]) == 7
    assert len(homes) == 1 and not homes[0].exists()


@pytest.mark.parametrize("arguments", [
    ["build", "--tag", "registry.example.com/team/app:v1", "--registry", "other.example.com/team/app"],
    ["build", "--tag", "registry.example.com/team/app:v1", "--username", "synthetic-user"],
    ["build", "--tag", "registry.example.com/team/app:v1", "--password", "synthetic-password"],
    ["build", "--tag", "team/app:v1"],
])
def test_ci_helper_rejects_unscoped_or_command_line_credentials(monkeypatch, tmp_path, arguments):
    helper = _registry_helper()
    monkeypatch.setenv("REGISTRY_USERNAME", "synthetic-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "synthetic-password")
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not build"))
    with pytest.raises(SystemExit) as exc:
        helper.main([*arguments, "--context", str(tmp_path), "--no-cache"])
    assert exc.value.code != 0


@pytest.mark.parametrize("missing", ["REGISTRY_USERNAME", "REGISTRY_PASSWORD"])
def test_ci_helper_requires_credentials(monkeypatch, tmp_path, missing):
    helper = _registry_helper()
    monkeypatch.setenv("REGISTRY_USERNAME", "synthetic-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "synthetic-password")
    monkeypatch.delenv(missing)
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not build"))
    with pytest.raises(SystemExit) as exc:
        helper.main(["build", "--tag", "registry.example.com/team/app:v1",
                     "--context", str(tmp_path), "--no-cache"])
    assert exc.value.code != 0


def test_ci_helper_requires_explicit_context(monkeypatch):
    helper = _registry_helper()
    monkeypatch.setenv("REGISTRY_USERNAME", "synthetic-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "synthetic-password")
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not build"))
    with pytest.raises(SystemExit) as exc:
        helper.main(["build", "--tag", "registry.example.com/team/app:v1", "--no-cache"])
    assert exc.value.code != 0


def test_ci_helper_requires_explicit_cache_mode(monkeypatch, tmp_path):
    helper = _registry_helper()
    monkeypatch.setenv("REGISTRY_USERNAME", "synthetic-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "synthetic-password")
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not build"))
    with pytest.raises(SystemExit) as exc:
        helper.main(["build", "--tag", "registry.example.com/team/app:v1",
                     "--context", str(tmp_path)])
    assert exc.value.code != 0
