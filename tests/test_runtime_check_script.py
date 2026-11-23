import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_runtime_image.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("check_runtime_image", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch(script, monkeypatch, stdout):
    built = []

    class FakeBuilder:
        def __init__(self, config):
            built.append(config)
            self.manifest_digest = "sha256:" + "a" * 64
            self.config_digest = "sha256:" + "b" * 64
            self.layers = [SimpleNamespace(digest="sha256:" + "c" * 64)]

        def build(self):
            pass

    monkeypatch.setattr(script, "ImageBuilder", FakeBuilder)
    monkeypatch.setattr(script, "run_layout", lambda *args: stdout)
    return built


def test_shell_command_check_builds_verbatim_shell_entrypoint(script, monkeypatch):
    built = _patch(script, monkeypatch, script.SHELL_COMMAND_EXPECTED)
    args = SimpleNamespace(
        base_image="python:3.11-slim",
        shell='["/bin/bash", "-c"]',
        timeout=5,
        platform="linux/arm64",
    )
    evidence = {}
    script.shell_command_check(args, "docker", "skopeo", "img:tag", evidence)
    (config,) = built
    assert config.entrypoint == [
        "/bin/bash",
        "-c",
        "printf '%s\\n' \"pycontainer-shell-command-$MARKER\" 'quoted  $NOT_EXPANDED'",
    ]
    assert config.env == {"MARKER": "ok"}
    assert config.base_image == "python:3.11-slim"
    assert config.platform == "linux/arm64"
    assert evidence["image"]["manifest_digest"] == "sha256:" + "a" * 64


def test_shell_command_check_rejects_output_mismatch(script, monkeypatch):
    _patch(script, monkeypatch, "pycontainer-shell-command-\n")
    args = SimpleNamespace(
        base_image="python:3.11-slim", shell=None, timeout=5, platform="linux/amd64"
    )
    with pytest.raises(RuntimeError, match="output mismatch"):
        script.shell_command_check(args, "docker", "skopeo", "img:tag", {})


def test_shell_command_check_rejects_empty_shell(script, monkeypatch):
    _patch(script, monkeypatch, script.SHELL_COMMAND_EXPECTED)
    args = SimpleNamespace(
        base_image="python:3.11-slim", shell="", timeout=5, platform="linux/amd64"
    )
    with pytest.raises(ValueError, match="shell"):
        script.shell_command_check(args, "docker", "skopeo", "img:tag", {})


def test_cmd_only_check_builds_two_imports_without_entrypoint_override(script, monkeypatch):
    built = _patch(script, monkeypatch, "")
    outputs = iter([
        "inherited-entrypoint\nshell=expanded\n",
        "inherited-entrypoint\nexec=$MARKER  literal\n",
    ])
    monkeypatch.setattr(script, "run_layout", lambda *args: next(outputs))
    args = SimpleNamespace(base_image="python:3.11-slim", timeout=5, platform="linux/amd64")
    evidence = {}
    script.cmd_only_check(args, "docker", "skopeo", "img:tag", evidence)

    base, shell, exec_form = built
    assert base.entrypoint[:2] == ["/bin/sh", "-c"]
    assert shell.base_image == exec_form.base_image == f"oci:{base.output_dir}:runtime-check"
    assert shell.entrypoint is exec_form.entrypoint is None
    assert shell.cmd == ["/bin/sh", "-c", 'printf \'shell=%s\\n\' "$MARKER"']
    assert exec_form.cmd == ["printf", "exec=%s\\n", "$MARKER  literal"]
    assert set(evidence["cases"]) == {"shell", "exec"}


@pytest.mark.parametrize("bad_case", ["shell", "exec"])
def test_cmd_only_check_requires_inherited_entrypoint_output(script, monkeypatch, bad_case):
    _patch(script, monkeypatch, "")
    outputs = [
        "inherited-entrypoint\nshell=expanded\n",
        "inherited-entrypoint\nexec=$MARKER  literal\n",
    ]
    index = ["shell", "exec"].index(bad_case)
    outputs[index] = outputs[index].split("\n", 1)[1]
    remaining = iter(outputs)
    monkeypatch.setattr(script, "run_layout", lambda *args: next(remaining))
    args = SimpleNamespace(base_image="python:3.11-slim", timeout=5, platform="linux/amd64")
    with pytest.raises(RuntimeError, match=f"CMD-only {bad_case}-form output mismatch"):
        script.cmd_only_check(args, "docker", "skopeo", "img:tag", {})


def test_run_layout_records_platform_and_output(script, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="container output\n", stderr="", returncode=0)

    monkeypatch.setattr(script.subprocess, "run", run)
    monkeypatch.setattr(script, "command_output", lambda command: "linux/amd64")
    evidence = {}
    stdout = script.run_layout(
        "docker", "skopeo", Path("layout"), "img:tag", 5, "linux/amd64", evidence
    )
    assert stdout == "container output\n"
    assert evidence == {
        "image_platform": "linux/amd64",
        "container_stdout": "container output\n",
    }
    assert calls[-1] == ["docker", "image", "rm", "--force", "img:tag"]


def test_run_layout_refuses_wrong_image_platform_before_start(script, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(script.subprocess, "run", run)
    monkeypatch.setattr(script, "command_output", lambda command: "linux/arm64")
    evidence = {}
    with pytest.raises(RuntimeError, match="imported image platform"):
        script.run_layout(
            "docker", "skopeo", Path("layout"), "img:tag", 5, "linux/amd64", evidence
        )
    assert evidence["image_platform"] == "linux/arm64"
    assert not any(command[:2] == ["docker", "run"] for command in calls)
    assert calls[-1] == ["docker", "image", "rm", "--force", "img:tag"]
