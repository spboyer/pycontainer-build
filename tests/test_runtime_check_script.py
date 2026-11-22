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

        def build(self):
            pass

    monkeypatch.setattr(script, "ImageBuilder", FakeBuilder)
    monkeypatch.setattr(script, "run_layout", lambda *args: stdout)
    return built


def test_shell_command_check_builds_verbatim_shell_entrypoint(script, monkeypatch):
    built = _patch(script, monkeypatch, script.SHELL_COMMAND_EXPECTED)
    args = SimpleNamespace(base_image="python:3.11-slim", shell='["/bin/bash", "-c"]', timeout=5)
    script.shell_command_check(args, "docker", "skopeo", "img:tag")
    (config,) = built
    assert config.entrypoint == [
        "/bin/bash",
        "-c",
        "printf '%s\\n' \"pycontainer-shell-command-$MARKER\" 'quoted  $NOT_EXPANDED'",
    ]
    assert config.env == {"MARKER": "ok"}
    assert config.base_image == "python:3.11-slim"


def test_shell_command_check_rejects_output_mismatch(script, monkeypatch):
    _patch(script, monkeypatch, "pycontainer-shell-command-\n")
    args = SimpleNamespace(base_image="python:3.11-slim", shell=None, timeout=5)
    with pytest.raises(RuntimeError, match="output mismatch"):
        script.shell_command_check(args, "docker", "skopeo", "img:tag")
