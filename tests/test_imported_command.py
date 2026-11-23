"""Configured CMD without ENTRYPOINT follows Docker semantics (#206)."""
import copy
import hashlib
import json

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.dockerfile_import import import_dockerfile, render_toml
from pycontainer.oci import build_config_json

PLAIN_BASE = {
    "architecture": "amd64",
    "os": "linux",
    "config": {"Env": [], "WorkingDir": "/", "Cmd": ["python3"]},
    "rootfs": {"type": "layers", "diff_ids": []},
}
ENTRYPOINT_BASE = copy.deepcopy(PLAIN_BASE)
ENTRYPOINT_BASE["config"]["Entrypoint"] = ["docker-entrypoint.sh"]

SHELL_CMD = 'CMD echo "hello $NAME" && exec python app.py'
EXEC_CMD = 'CMD ["python", "app.py"]'


def _read_blob(blobs, digest):
    data = (blobs / digest.split(":", 1)[1]).read_bytes()
    assert "sha256:" + hashlib.sha256(data).hexdigest() == digest
    return data


def _built_config(tmp_path, monkeypatch, base, config):
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, layers_dir, os_name, arch: ([], base)
    )
    ImageBuilder(config).build()
    output = tmp_path / "out"
    blobs = output / "blobs" / "sha256"
    manifest_desc = json.loads((output / "index.json").read_bytes())["manifests"][0]
    manifest_bytes = _read_blob(blobs, manifest_desc["digest"])
    assert len(manifest_bytes) == manifest_desc["size"]
    config_desc = json.loads(manifest_bytes)["config"]
    config_bytes = _read_blob(blobs, config_desc["digest"])
    assert len(config_bytes) == config_desc["size"]
    return json.loads(config_bytes)


def _context(tmp_path, framework=False):
    context = tmp_path / "context"
    context.mkdir()
    if framework:
        (context / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    else:
        (context / "app.py").write_text("print('hi')\n")
    return context


def _imported_config(tmp_path, dockerfile, framework=False):
    context = _context(tmp_path, framework)
    result = import_dockerfile(f"FROM python:3.11-slim\n{dockerfile}\n")
    (context / "pyproject.toml").write_text(render_toml(result.config))
    return result, config_from_file(
        context / "pyproject.toml",
        {
            "context_dir": str(context),
            "output_dir": str(tmp_path / "out"),
            "use_cache": False,
        },
    )


@pytest.mark.parametrize("dockerfile", [SHELL_CMD, EXEC_CMD], ids=["shell", "exec"])
@pytest.mark.parametrize("base", [PLAIN_BASE, ENTRYPOINT_BASE], ids=["no-entrypoint", "entrypoint"])
@pytest.mark.parametrize("framework", [False, True], ids=["plain", "fastapi"])
def test_cmd_only_import_inherits_base_entrypoint(tmp_path, monkeypatch, dockerfile, base, framework):
    result, config = _imported_config(tmp_path, dockerfile, framework)
    assert "entrypoint" not in result.config

    image = _built_config(tmp_path, monkeypatch, base, config)["config"]

    assert image.get("Entrypoint") == base["config"].get("Entrypoint")
    assert image["Cmd"] == result.config["cmd"]
    if dockerfile == SHELL_CMD:
        assert image["Cmd"] == ["/bin/sh", "-c", 'echo "hello $NAME" && exec python app.py']


def test_without_configured_cmd_entrypoint_is_still_detected(tmp_path, monkeypatch):
    context = _context(tmp_path)
    config = BuildConfig(context_dir=str(context), output_dir=str(tmp_path / "out"), use_cache=False)
    image = _built_config(tmp_path, monkeypatch, ENTRYPOINT_BASE, config)["config"]
    assert image["Entrypoint"] == ["python", "-m", "app"]
    assert image["Cmd"] == ["python3"]


def test_without_configured_cmd_framework_entrypoint_is_still_applied(tmp_path):
    context = _context(tmp_path, framework=True)
    builder = ImageBuilder(BuildConfig(context_dir=str(context), use_cache=False))
    assert builder.config.entrypoint[0] == "uvicorn"


def test_explicit_entrypoint_with_cmd_is_kept(tmp_path, monkeypatch):
    context = _context(tmp_path, framework=True)
    config = BuildConfig(
        context_dir=str(context),
        output_dir=str(tmp_path / "out"),
        use_cache=False,
        entrypoint=["python", "-m", "svc"],
        cmd=["--port", "80"],
    )
    image = _built_config(tmp_path, monkeypatch, ENTRYPOINT_BASE, config)["config"]
    assert image["Entrypoint"] == ["python", "-m", "svc"]
    assert image["Cmd"] == ["--port", "80"]


def test_explicit_empty_entrypoint_clears_base_and_skips_detection(tmp_path, monkeypatch):
    context = _context(tmp_path, framework=True)
    config = BuildConfig(
        context_dir=str(context),
        output_dir=str(tmp_path / "out"),
        use_cache=False,
        entrypoint=[],
    )
    image = _built_config(tmp_path, monkeypatch, ENTRYPOINT_BASE, config)["config"]
    assert "Entrypoint" not in image
    assert image["Cmd"] == ["python3"]


def test_explicit_empty_cmd_clears_base_cmd():
    image = build_config_json(
        "amd64", "linux", {}, "/app", None, [], cmd=[], base_config=ENTRYPOINT_BASE
    )["config"]
    assert image["Entrypoint"] == ["docker-entrypoint.sh"]
    assert "Cmd" not in image
    assert ENTRYPOINT_BASE["config"]["Cmd"] == ["python3"]


def test_imported_empty_entrypoint_and_cmd_clear_base_values(tmp_path, monkeypatch):
    result, config = _imported_config(tmp_path, "ENTRYPOINT []\nCMD []", framework=True)
    assert (result.config["entrypoint"], result.config["cmd"]) == ([], [])
    image = _built_config(tmp_path, monkeypatch, ENTRYPOINT_BASE, config)["config"]
    assert "Entrypoint" not in image
    assert "Cmd" not in image


@pytest.mark.parametrize(
    ("entrypoint", "cmd", "expected_entrypoint", "expected_cmd"),
    [
        (None, ["python", "app.py"], ["docker-entrypoint.sh"], ["python", "app.py"]),
        ([], [], None, None),
        ([], None, None, ["python3"]),
    ],
)
def test_locked_build_preserves_cmd_inheritance_and_explicit_clears(
    tmp_path, monkeypatch, entrypoint, cmd, expected_entrypoint, expected_cmd
):
    from test_dependency_staging import _write_wheel
    from test_lockfiles import _SimpleIndex, _write_uv_project

    context = tmp_path / "context"
    wheel = _write_wheel(context / "wheelhouse")
    index = _SimpleIndex([wheel])
    _write_uv_project(context, wheel, registry=index.url)
    base = copy.deepcopy(ENTRYPOINT_BASE)
    base["config"]["Env"] = [
        "PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.8", "PYTHON_LIBC=glibc-2.17",
    ]
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "out"), use_cache=False,
        base_image="python:3.11-slim-bookworm", include_deps=True,
        entrypoint=entrypoint, cmd=cmd,
    )

    try:
        image = _built_config(tmp_path, monkeypatch, base, config)["config"]
    finally:
        index.close()

    assert image.get("Entrypoint") == expected_entrypoint
    assert image.get("Cmd") == expected_cmd


@pytest.mark.parametrize(
    ("entrypoint", "cmd", "expected_entrypoint", "expected_cmd"),
    [(None, ["python", "app.py"], ["docker-entrypoint.sh"], ["python", "app.py"]),
     ([], [], None, None)],
    ids=["inherit-entrypoint", "clear-entrypoint-and-cmd"],
)
def test_command_semantics_with_metadata(
    tmp_path, monkeypatch, entrypoint, cmd, expected_entrypoint, expected_cmd
):
    base = copy.deepcopy(ENTRYPOINT_BASE)
    base["config"]["Labels"] = {"shared": "base", "base-only": "base"}
    context = _context(tmp_path, framework=True)
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "out"), use_cache=False,
        entrypoint=entrypoint, cmd=cmd, labels={"shared": "explicit"},
        manifest_annotations={"example.manifest": "value"},
        index_annotations={"example.index": "value"},
    )
    image = _built_config(tmp_path, monkeypatch, base, config)["config"]
    assert image.get("Entrypoint") == expected_entrypoint
    assert image.get("Cmd") == expected_cmd
    assert image["Labels"]["shared"] == "explicit"
    assert image["Labels"]["base-only"] == "base"
    assert base["config"]["Labels"]["shared"] == "base"
    output = tmp_path / "out"
    index = json.loads((output / "index.json").read_bytes())
    manifest = json.loads(_read_blob(output / "blobs" / "sha256", index["manifests"][0]["digest"]))
    assert index["annotations"]["example.index"] == "value"
    assert manifest["annotations"]["example.manifest"] == "value"


@pytest.mark.parametrize("cmd", [[], ["argument"]], ids=["clear-cmd", "set-cmd"])
def test_distroless_explicit_shell_entrypoint_with_cmd_is_kept(cmd):
    base = copy.deepcopy(ENTRYPOINT_BASE)
    base["config"]["Labels"] = {"name": "distroless/python3"}
    shell = ["/bin/sh", "-c", "run"]
    image = build_config_json("amd64", "linux", {}, "/app", shell, [], cmd=cmd, base_config=base)
    assert image["config"]["Entrypoint"] == shell
    if cmd:
        assert image["config"]["Cmd"] == cmd
    else:
        assert "Cmd" not in image["config"]


def test_without_base_unset_entrypoint_is_omitted():
    image = build_config_json(
        "amd64", "linux", {}, "/app", None, [],
        labels={"z": "last", "a": "first"}, cmd=["python", "app.py"],
    )
    assert "Entrypoint" not in image["config"]
    assert image["config"]["Cmd"] == ["python", "app.py"]
    assert list(image["config"]["Labels"].items()) == [("a", "first"), ("z", "last")]


def test_dry_run_plan_reports_inherited_entrypoint(tmp_path, capsys):
    context = _context(tmp_path, framework=True)
    ImageBuilder(
        BuildConfig(context_dir=str(context), cmd=["python", "app.py"], dry_run=True)
    ).build()
    out = capsys.readouterr().out
    assert "Entrypoint: <inherited from base image>" in out
    assert "Cmd: python app.py" in out
