"""Offline CLI contracts for the standalone CI examples (not provider execution)."""
import argparse
from pathlib import Path
import re
import shlex
import sys

import pytest

from pycontainer import cli


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "ci"
COMMAND = re.compile(r"(?:python -m pycontainer\.cli|pycontainer)\s+(build\b.*)")


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
    for arguments in commands:
        args = _parse(monkeypatch, arguments)
        assert args.cmd == "build"
        assert args.push
        assert args.tag and len(args.tag) == 1
        assert args.platform == "linux/amd64"
        assert args.base_image == "python:3.11-slim"
        assert args.username is None and args.password is None
        assert args.registry is None, "Use a full tag, not a host-only registry override"
    assert re.search(r"git\+https://github\.com/spboyer/pycontainer-build\.git@[0-9a-f]{40}\"", text)


def test_every_ci_example_is_validated():
    assert {p.name for p in EXAMPLES.iterdir()} == {
        "gitlab-ci.yml", "Jenkinsfile", "circleci-config.yml"}


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
    assert text.index("restore_cache") < text.index("pycontainer.cli build") < text.index("save_cache")
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
