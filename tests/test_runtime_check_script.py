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


@pytest.mark.parametrize("matches", [False, True])
def test_index_layout_selects_platform_and_verifies_config_before_start(
    script, monkeypatch, matches
):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="container output\n", stderr="", returncode=0)

    def inspect(command):
        return "linux/arm64" if command[-2] == "{{.Os}}/{{.Architecture}}" else (
            "sha256:expected" if matches else "sha256:wrong"
        )

    monkeypatch.setattr(script.subprocess, "run", run)
    monkeypatch.setattr(script, "command_output", inspect)
    evidence = {}
    kwargs = {
        "source_reference": "runtime.example/check:v1",
        "expected_config_digest": "sha256:expected",
    }
    if matches:
        assert script.run_layout(
            "docker", "skopeo", Path("layout"), "img:tag", 5, "linux/arm64",
            evidence, **kwargs,
        ) == "container output\n"
    else:
        with pytest.raises(RuntimeError, match="config digest"):
            script.run_layout(
                "docker", "skopeo", Path("layout"), "img:tag", 5, "linux/arm64",
                evidence, **kwargs,
            )
    assert calls[0][:6] == ["skopeo", "--override-os", "linux", "--override-arch", "arm64", "copy"]
    assert calls[0][6].endswith(":runtime.example/check:v1")
    assert any(command[:2] == ["docker", "run"] for command in calls) is matches
    assert calls[-1] == ["docker", "image", "rm", "--force", "img:tag"]


def test_shell_and_index_modes_cannot_be_combined(script):
    with pytest.raises(SystemExit) as exc:
        script.main(["--shell-command", "--multi-platform-index"])
    assert exc.value.code == 2
