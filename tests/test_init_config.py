import io
import os
import socket
import sys

import pytest

from pycontainer import builder, cache, cli, init_config, registry_client
from pycontainer.config_loader import load_explicit_file_config
from pycontainer.effective_config import resolve_build_config


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["pycontainer", *argv])
    try:
        cli.main()
    except SystemExit as exit_:
        return exit_.code
    return 0


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """Fail on any build, cache, registry or network use during generation."""

    def forbidden(*args, **kwargs):
        pytest.fail("init must not build, cache or contact a registry or network")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "ImageBuilder", forbidden)
    monkeypatch.setattr(builder, "ImageBuilder", forbidden)
    monkeypatch.setattr(cache, "LayerCache", forbidden)
    monkeypatch.setattr(registry_client, "RegistryClient", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return tmp_path


def _files(root):
    return sorted(p.name for p in root.iterdir())


@pytest.mark.parametrize(
    "template, entry, ports, deps",
    [
        ("fastapi", ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"], [8000], True),
        ("flask", ["python", "-m", "flask", "--app", "app:app", "run", "--host=0.0.0.0", "--port=5000"], [5000], True),
        ("generic", None, [], False),
    ],
)
def test_template_round_trips_through_real_loader(offline, monkeypatch, template, entry, ports, deps):
    (offline / "pyproject.toml").write_text('[project]\nname = "demo"\nrequires-python = ">=3.12"\n')

    assert _run(monkeypatch, ["init", "--template", template]) == 0

    table, settings = load_explicit_file_config(offline / "pycontainer.toml")
    cfg, _ = resolve_build_config(offline / "pycontainer.toml", {})
    assert table == "[build]"
    assert cfg.base_image == "python:3.12-slim"
    assert cfg.tag == f"local/{offline.name.lower().replace('_', '-')}:latest"
    assert cfg.entrypoint == entry
    assert cfg.exposed_ports == ports
    assert cfg.include_deps is deps
    assert _files(offline) == ["pycontainer.toml", "pyproject.toml"]


def test_build_command_consumes_generated_settings(offline, monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, cfg):
            seen["cfg"] = cfg

        def build(self):
            return "built"

    assert _run(monkeypatch, ["init", "-t", "fastapi", "--app", "svc.api:server", "--port", "9000",
                              "--tag", "ghcr.io/me/api:v1", "--platform", "linux/arm64"]) == 0
    monkeypatch.setattr(cli, "ImageBuilder", Recorder)
    assert _run(monkeypatch, ["build", "--dry-run", "--config", "pycontainer.toml"]) == 0
    cfg = seen["cfg"]
    assert cfg.dry_run is True
    assert cfg.tag == "ghcr.io/me/api:v1"
    assert cfg.platform == "linux/arm64"
    assert cfg.entrypoint[3] == "svc.api:server" and cfg.exposed_ports == [9000]


def test_generic_template_options(offline, monkeypatch):
    assert _run(monkeypatch, ["init", "-t", "generic", "--app", "pkg.worker", "--port", "7000",
                              "--include-deps", "--base-image", "python:3.13-slim"]) == 0
    cfg, _ = resolve_build_config(offline / "pycontainer.toml", {})
    assert cfg.entrypoint == ["python", "-m", "pkg.worker"]
    assert cfg.exposed_ports == [7000] and cfg.include_deps and cfg.base_image == "python:3.13-slim"


@pytest.mark.parametrize(
    "argv",
    [
        ["init", "-t", "unknown"],
        ["init"],
        ["init", "-t", "fastapi", "-i"],
        ["init", "-i", "--tag", "a:b"],
        ["init", "-i", "--force"],
        ["init", "--list-templates", "--force"],
        ["init", "-t", "fastapi", "--port", "nope"],
    ],
)
def test_usage_errors_write_nothing(offline, monkeypatch, argv):
    assert _run(monkeypatch, argv) == 2
    assert _files(offline) == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--app", "main"],
        ["--app", "main:app;rm"],
        ["--port", "0"],
        ["--port", "70000"],
        ["--tag", "Bad Tag"],
        ["--platform", "windows/amd64"],
        ["--context", "missing-dir"],
    ],
)
def test_invalid_template_values_write_nothing(offline, monkeypatch, capsys, extra):
    assert _run(monkeypatch, ["init", "-t", "fastapi", *extra]) == 2
    assert "error:" in capsys.readouterr().err
    assert _files(offline) == []


@pytest.mark.parametrize(
    "output",
    [
        "../outside.toml",
        "sub/../../outside.toml",
        "pyproject.toml",
        "PyProject.TOML",
        "config.txt",
        "missing/pycontainer.toml",
        "C:outside.toml",
        "\\rooted.toml",
        "bad name.toml",
        "stream.toml:ads",
        "trailing.toml.",
        "NUL.toml",
        "sub/com1.toml",
        "Con.config.toml",
        "",
    ],
)
def test_unsafe_output_paths_are_rejected(offline, monkeypatch, output):
    assert _run(monkeypatch, ["init", "-t", "generic", "-o", output]) == 2
    assert _files(offline) == []


def test_absolute_output_path_is_rejected(offline, monkeypatch):
    assert _run(monkeypatch, ["init", "-t", "generic", "-o", str(offline / "abs.toml")]) == 2
    assert _files(offline) == []


def test_directory_and_symlink_targets_are_rejected(offline, monkeypatch):
    (offline / "dir.toml").mkdir()
    assert _run(monkeypatch, ["init", "-t", "generic", "-o", "dir.toml", "--force"]) == 2
    real = offline / "real"
    real.mkdir()
    try:
        (offline / "link").symlink_to(real, target_is_directory=True)
        (offline / "link.toml").symlink_to(offline / "victim.toml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    assert _run(monkeypatch, ["init", "-t", "generic", "-o", "link/pycontainer.toml"]) == 2
    assert _run(monkeypatch, ["init", "-t", "generic", "-o", "link.toml", "--force"]) == 2
    assert list(real.iterdir()) == [] and not (offline / "victim.toml").exists()


def test_parent_swapped_for_link_after_validation_is_rejected(offline):
    real = offline / "elsewhere"
    real.mkdir()
    (offline / "sub").mkdir()
    target = init_config.resolve_output_path("sub/pycontainer.toml", offline)
    (offline / "sub").rmdir()
    try:
        (offline / "sub").symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    with pytest.raises(init_config.InitError, match="link"):
        init_config.publish(init_config.render_build_toml({"tag": "a:b"}, "generic"), target)
    assert list(real.iterdir()) == []


def test_existing_file_requires_force(offline, monkeypatch, capsys):
    target = offline / "pycontainer.toml"
    target.write_text("# mine\n")
    assert _run(monkeypatch, ["init", "-t", "flask"]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert target.read_text() == "# mine\n"

    assert _run(monkeypatch, ["init", "-t", "flask", "--force"]) == 0
    assert "[build]" in target.read_text()
    assert _files(offline) == ["pycontainer.toml"]


def test_publish_never_clobbers_file_created_after_precheck(offline, monkeypatch):
    target = offline / "race.toml"
    content = init_config.render_build_toml(init_config.build_settings("generic"), "generic")
    real_resolve = init_config.resolve_build_config

    def racing_resolve(path, overrides):
        target.write_text("# created concurrently\n")
        return real_resolve(path, overrides)

    monkeypatch.setattr(init_config, "resolve_build_config", racing_resolve)
    with pytest.raises(init_config.OutputExistsError):
        init_config.publish(content, target)
    assert target.read_text() == "# created concurrently\n"
    assert _files(offline) == ["race.toml"]


def test_publish_rejects_invalid_content_without_leftovers(offline):
    with pytest.raises(init_config.InitError, match="platform"):
        init_config.publish('[build]\nplatform = "nope"\n', offline / "bad.toml")
    assert _files(offline) == []


def test_interrupted_publish_leaves_no_partial_file(offline, monkeypatch):
    def interrupted(path, overrides):
        raise KeyboardInterrupt

    monkeypatch.setattr(init_config, "resolve_build_config", interrupted)
    with pytest.raises(KeyboardInterrupt):
        init_config.publish("[build]\n", offline / "cfg.toml")
    assert _files(offline) == []


def test_fallback_write_removes_partial_file(offline, monkeypatch):
    target = offline / "cfg.toml"
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        init_config._write_new(target, b"[build]\n")
    assert not target.exists()


def test_existing_pyproject_settings_are_untouched(offline, monkeypatch, capsys):
    pyproject = offline / "pyproject.toml"
    original = '[project]\nname = "demo"\n\n[tool.pycontainer]\ntag = "mine:1"\n'
    pyproject.write_text(original)
    assert _run(monkeypatch, ["init", "-t", "fastapi"]) == 0
    out = capsys.readouterr().out
    assert "[tool.pycontainer] was not changed" in out
    assert "requirements.txt" in out
    assert pyproject.read_text() == original


@pytest.mark.parametrize("lockfile", ["poetry.lock", "Pipfile.lock", "pdm.lock", "uv.lock"])
def test_dependency_advisory_recognizes_lockfiles(offline, monkeypatch, capsys, lockfile):
    (offline / lockfile).write_text("")

    assert _run(monkeypatch, ["init", "-t", "fastapi"]) == 0
    assert "no supported lockfile" not in capsys.readouterr().out


def test_list_templates(offline, monkeypatch, capsys):
    assert _run(monkeypatch, ["init", "--list-templates"]) == 0
    assert [line.split(":")[0] for line in capsys.readouterr().out.splitlines()] == ["fastapi", "flask", "generic"]
    assert _files(offline) == []


# --- interactive wizard -------------------------------------------------------


class Terminal:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _wizard(answers, tty=True):
    term, out, err = Terminal(answers), io.StringIO(), io.StringIO()
    code = init_config.run_wizard(term, out, err, is_tty=lambda: tty)
    return code, term, out.getvalue(), err.getvalue()


# template, context, tag, base, app, port, deps, platform, output, confirm
FASTAPI_ANSWERS = ["fastapi", "", "ghcr.io/me/api:v1", "", "", "", "", "", "", "y"]


@pytest.mark.parametrize("mode", ["template", "wizard"])
def test_init_reports_invalid_project_metadata(offline, monkeypatch, capsys, mode):
    pyproject = offline / "pyproject.toml"
    original = 'project = "invalid"\n'
    pyproject.write_text(original)

    if mode == "template":
        code = _run(monkeypatch, ["init", "--template", "generic"])
        err = capsys.readouterr().err
    else:
        code, _, _, err = _wizard(["generic", "", ""])

    assert code == 2
    assert "cannot detect the Python version" in err
    assert "Traceback" not in err
    assert _files(offline) == ["pyproject.toml"]
    assert pyproject.read_text() == original


def test_wizard_requires_tty(offline):
    code, term, out, err = _wizard(FASTAPI_ANSWERS, tty=False)
    assert code == 2 and term.prompts == [] and "interactive terminal" in err
    assert _files(offline) == []


def test_cli_wizard_without_tty_fails(offline, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("fastapi\n"))
    assert _run(monkeypatch, ["init", "--interactive"]) == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert _files(offline) == []


def test_wizard_writes_validated_config(offline):
    code, term, out, err = _wizard(FASTAPI_ANSWERS)
    assert code == 0, err
    cfg, table = resolve_build_config(offline / "pycontainer.toml", {})
    assert table == "[build]" and cfg.tag == "ghcr.io/me/api:v1"
    assert cfg.entrypoint[3] == "main:app" and cfg.exposed_ports == [8000] and cfg.include_deps
    assert cfg.platform == "linux/amd64"
    assert not any(word in p.lower() for p in term.prompts for word in ("password", "token", "secret", "username"))
    assert "[build]" in out


def test_wizard_reprompts_invalid_answers(offline):
    answers = ["django", "generic", "missing", ".", "Bad Tag", "", "", "1:2", "", "99999", "", "maybe", "",
               "bogus", "", "../x.toml", "out.toml", "y"]
    code, term, out, err = _wizard(answers)
    assert code == 0, err
    assert out.count("invalid:") == 8
    cfg, _ = resolve_build_config(offline / "out.toml", {})
    assert cfg.entrypoint is None and cfg.exposed_ports == []


def test_wizard_aborts_after_repeated_invalid_answers(offline):
    code, term, out, err = _wizard(["x", "y", "z"])
    assert code == 2 and "too many invalid answers" in err
    assert _files(offline) == []


@pytest.mark.parametrize("cut", range(len(FASTAPI_ANSWERS)))
def test_wizard_eof_at_any_prompt_writes_nothing(offline, cut):
    code, term, out, err = _wizard(FASTAPI_ANSWERS[:cut])
    assert code == 1 and "end of input" in err
    assert _files(offline) == []


def test_wizard_ctrl_c_writes_nothing(offline):
    code, term, out, err = _wizard(FASTAPI_ANSWERS[:4] + [KeyboardInterrupt()])
    assert code == 130 and "no file written" in err
    assert _files(offline) == []


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_wizard_decline_writes_nothing(offline, answer):
    code, term, out, err = _wizard(FASTAPI_ANSWERS[:-1] + [answer])
    assert code == 1 and "no file written" in out
    assert _files(offline) == []


def test_wizard_never_overwrites_existing_file(offline):
    existing = offline / "pycontainer.toml"
    existing.write_text("# mine\n")
    code, term, out, err = _wizard(FASTAPI_ANSWERS[:-2] + ["", "other.toml", "y"])
    assert code == 0, err
    assert "never overwrites" in out
    assert existing.read_text() == "# mine\n"
    assert _files(offline) == ["other.toml", "pycontainer.toml"]


def test_noninteractive_commands_unchanged(offline, monkeypatch, capsys):
    assert _run(monkeypatch, ["validate", "config"]) == 0
    assert "ok: defaults" in capsys.readouterr().out
