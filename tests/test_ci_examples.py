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
