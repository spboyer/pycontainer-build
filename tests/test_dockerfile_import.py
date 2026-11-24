import json
import math
import tomllib

import pytest

from pycontainer import cli
from pycontainer.dockerfile_import import (
    import_dockerfile,
    merge_into_pyproject,
    parse_dockerfile,
    render_toml,
)

SAMPLE = """\
# syntax=docker/dockerfile:1
ARG PY=3.12
FROM python:${PY}-slim AS builder
RUN pip install build

FROM python:${PY}-slim
ARG APP_PORT=8000
ENV APP_ENV=prod \\
    # comment inside continuation
    GREETING="hello world"
WORKDIR /srv
WORKDIR app
LABEL org.opencontainers.image.title="demo" owner=me
EXPOSE ${APP_PORT} 9000/tcp 53/udp
COPY --from=builder /dist /dist
COPY . .
RUN pip install -r requirements.txt
HEALTHCHECK CMD curl -f http://localhost/
USER 1000
ENTRYPOINT ["python", "-m", "app"]
CMD uvicorn app:app
"""


def test_parser_handles_continuations_directives_and_stages():
    parsed = parse_dockerfile(SAMPLE)
    assert parsed.directives == {"syntax": "docker/dockerfile:1"}
    assert [s.name for s in parsed.stages] == ["builder", None]
    env = next(i for i in parsed.stages[1].instructions if i.name == "ENV")
    assert "GREETING" in env.args and "comment" not in env.args


def test_parser_custom_escape_and_heredoc():
    parsed = parse_dockerfile("# escape=`\nFROM x\nENV A=1 `\n B=2\nRUN <<EOF\nFROM fake\nEOF\nUSER u\n")
    assert len(parsed.stages) == 1
    names = [i.name for i in parsed.stages[0].instructions]
    assert names == ["ENV", "RUN", "USER"]


def test_mapping_uses_real_buildconfig_keys():
    result = import_dockerfile(SAMPLE)
    assert result.config == {
        "base_image": "python:3.12-slim",
        "workdir": "/srv/app",
        "env": {"APP_ENV": "prod", "GREETING": "hello world"},
        "labels": {"org.opencontainers.image.title": "demo", "owner": "me"},
        "exposed_ports": [8000, 9000],
        "user": "1000",
        "entrypoint": ["python", "-m", "app"],
        "cmd": ["/bin/sh", "-c", "uvicorn app:app"],
    }


def test_unsupported_instructions_warn_explicitly():
    text = "\n".join(import_dockerfile(SAMPLE).warnings)
    for expected in ("Multi-stage", "COPY unsupported", "another stage", "RUN unsupported",
                     "HEALTHCHECK unsupported", "UDP"):
        assert expected in text


def test_build_arg_override_and_undefined_arg_warning():
    result = import_dockerfile("ARG V\nFROM img:${V}\nENV X=$MISSING\n", {"V": "9"})
    assert result.config["base_image"] == "img:9"
    assert any("MISSING" in w for w in result.warnings)


def test_scratch_and_missing_from():
    assert any("scratch" in w for w in import_dockerfile("FROM scratch\n").warnings)
    with pytest.raises(ValueError):
        parse_dockerfile("RUN x\n")


def test_rendered_toml_round_trips_and_validates():
    config = import_dockerfile(SAMPLE).config
    data = tomllib.loads(render_toml(config))["tool"]["pycontainer"]
    assert data == config
    from dataclasses import fields
    from pycontainer.config import BuildConfig
    assert set(data) <= {f.name for f in fields(BuildConfig)}


def test_import_preserves_runtime_dollars_when_loading_build_config(tmp_path):
    from pycontainer.config_loader import config_from_file

    result = import_dockerfile(
        'FROM python:3.12\nENV PORT=8000\nCMD ["/bin/sh", "-c", "echo ${PORT} $$ $HOME"]\n'
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(render_toml(result.config))
    config = config_from_file(path)
    assert config.cmd == ["/bin/sh", "-c", "echo ${PORT} $$ $HOME"]
    assert config.env == {"PORT": "8000"}


def test_merge_replaces_only_pycontainer_tables():
    existing = '[project]\nname = "x"\n\n[tool.pycontainer]\ntag = "old"\n\n[tool.pycontainer.env]\nA = "1"\n\n[tool.ruff]\nline-length = 99\n'
    merged = merge_into_pyproject(existing, render_toml({"workdir": "/w"}))
    data = tomllib.loads(merged)
    assert data["project"]["name"] == "x"
    assert data["tool"]["ruff"]["line-length"] == 99
    assert data["tool"]["pycontainer"] == {"workdir": "/w"}


def test_force_preserves_unrelated_multiline_text_with_table_like_lines(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12\n")
    out = tmp_path / "pyproject.toml"
    existing = (
        '[project]\nname = "keep"\ndescription = """\n'
        'before\u2028same line\n[tool.pycontainer]\nimportant description\n[tool.ruff]\nafter\n"""\n'
        '\n[tool.ruff]\nline-length = 99\n'
    )
    out.write_text(existing, encoding="utf-8")

    assert _run_cli(monkeypatch, ["import", str(dockerfile), "-o", str(out), "--force"]) == 0

    data = tomllib.loads(out.read_text(encoding="utf-8"))
    assert data["project"] == tomllib.loads(existing)["project"]
    assert data["tool"]["ruff"] == {"line-length": 99}
    assert data["tool"]["pycontainer"] == {"base_image": "python:3.12"}


def test_force_preserves_unrelated_quoted_dotted_table(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12\n")
    out = tmp_path / "pyproject.toml"
    out.write_text('[tool."pycontainer.backup"]\nnote = "keep"\nratio = nan\n')

    assert _run_cli(monkeypatch, ["import", str(dockerfile), "-o", str(out), "--force"]) == 0

    data = tomllib.loads(out.read_text())
    assert data["tool"]["pycontainer.backup"]["note"] == "keep"
    assert math.isnan(data["tool"]["pycontainer.backup"]["ratio"])
    assert data["tool"]["pycontainer"] == {"base_image": "python:3.12"}


def test_force_preserves_unrelated_array_table_context(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12\n")
    out = tmp_path / "pyproject.toml"
    existing = (
        '[[fruit]]\nname = "one"\n'
        '[tool.pycontainer]\nbase_image = "old"\n'
        '[fruit.physical]\ncolor = "red"\n'
        '[[fruit]]\nname = "two"\n'
        '[tool.pycontainer.env]\nMODE = "old"\n'
    )
    out.write_text(existing)

    assert _run_cli(monkeypatch, ["import", str(dockerfile), "-o", str(out), "--force"]) == 0

    data = tomllib.loads(out.read_text())
    assert data["fruit"] == tomllib.loads(existing)["fruit"]
    assert data["tool"]["pycontainer"] == {"base_image": "python:3.12"}


def test_force_replaces_single_quoted_pycontainer_tables(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12\n")
    out = tmp_path / "pyproject.toml"
    out.write_text(
        "[tool.'pycontainer']\nbase_image = \"old\"\n"
        "[tool.'pycontainer'.env]\nMODE = \"old\"\n"
        "# Ruff compatibility settings\n"
        '[tool.ruff]\nline-length = 99\n'
    )

    assert _run_cli(monkeypatch, ["import", str(dockerfile), "-o", str(out), "--force"]) == 0

    data = tomllib.loads(out.read_text())
    assert "# Ruff compatibility settings" in out.read_text()
    assert data["tool"]["ruff"] == {"line-length": 99}
    assert data["tool"]["pycontainer"] == {"base_image": "python:3.12"}


@pytest.mark.parametrize(
    "existing",
    [
        '[tool]\npycontainer = {base_image = "old"}\nother = "keep"\n',
        'tool.pycontainer = {base_image = "old"}\n',
    ],
)
def test_force_refuses_inline_config_without_changing_file(tmp_path, monkeypatch, capsys, existing):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12\n")
    out = tmp_path / "pyproject.toml"
    out.write_text(existing)

    assert _run_cli(monkeypatch, ["import", str(dockerfile), "-o", str(out), "--force"]) == 2
    assert out.read_text() == existing
    assert "Cannot safely merge [tool.pycontainer]" in capsys.readouterr().err


def test_cli_refuses_overwrite_then_merges_with_force(tmp_path, monkeypatch, capsys):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12\nWORKDIR /w\nRUN echo hi\n")
    out = tmp_path / "pyproject.toml"
    out.write_text('[project]\nname = "keep"\n')

    monkeypatch.setattr("sys.argv", ["pycontainer", "import", str(dockerfile), "-o", str(out)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert out.read_text() == '[project]\nname = "keep"\n'
    assert "WARNING" in capsys.readouterr().out

    monkeypatch.setattr("sys.argv", ["pycontainer", "import", str(dockerfile), "-o", str(out), "--force"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    data = tomllib.loads(out.read_text())
    assert data["project"]["name"] == "keep"
    assert data["tool"]["pycontainer"]["workdir"] == "/w"


def test_cli_prints_snippet_without_output(tmp_path, monkeypatch, capsys):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12\n")
    monkeypatch.setattr("sys.argv", ["pycontainer", "import", str(dockerfile)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "[tool.pycontainer]" in capsys.readouterr().out


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["pycontainer", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code


def test_imported_config_is_consumed_by_build_cli(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.12-alpine\nWORKDIR /srv\nENV MODE=production\n"
        'LABEL org.opencontainers.image.title="demo"\nEXPOSE 8080\n'
        'CMD ["python", "app.py"]\n'
    )
    out = tmp_path / "pyproject.toml"
    out.write_text('[project]\nname = "keep"\n\n[tool.ruff]\nline-length = 99\n')
    assert _run_cli(monkeypatch, ["import", str(dockerfile), "-o", str(out), "--force"]) == 0

    configs = []

    class FakeImageBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", FakeImageBuilder)
    monkeypatch.setattr("sys.argv", ["pycontainer", "build", "--config", str(out), "--dry-run"])
    cli.main()

    config = configs[0]
    assert config.base_image == "python:3.12-alpine"
    assert config.workdir == "/srv"
    assert config.env == {"MODE": "production"}
    assert config.labels == {"org.opencontainers.image.title": "demo"}
    assert config.exposed_ports == [8080]
    assert config.cmd == ["python", "app.py"]
    assert tomllib.loads(out.read_text())["tool"]["ruff"]["line-length"] == 99


def test_imported_config_drives_real_build(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        'FROM python:3.12-alpine\nWORKDIR /srv\nENV MODE=production\nLABEL owner=team\n'
        'EXPOSE 8080\nCMD ["python", "app.py"]\n'
    )
    (tmp_path / "app.py").write_text("print('hello')\n")
    out = tmp_path / "pyproject.toml"
    assert _run_cli(monkeypatch, ["import", str(dockerfile), "-o", str(out)]) == 0

    base_images = []

    def pull_base_image(builder, layers_dir, os_name, arch):
        base_images.append((builder.config.base_image, os_name, arch))
        return [], {
            "architecture": arch,
            "os": os_name,
            "config": {"Env": ["BASE=kept"], "Labels": {"base": "kept"}},
            "rootfs": {"type": "layers", "diff_ids": []},
        }

    monkeypatch.setattr(cli.ImageBuilder, "_pull_base_image", pull_base_image)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr("sys.argv", [
        "pycontainer", "build", "--config", str(out), "--context", str(tmp_path),
        "--tag", "app:latest", "--no-cache",
    ])
    cli.main()

    blobs = tmp_path / "dist" / "image" / "blobs" / "sha256"
    index = json.loads((tmp_path / "dist" / "image" / "index.json").read_text())
    manifest_digest = index["manifests"][0]["digest"].split(":", 1)[1]
    manifest = json.loads((blobs / manifest_digest).read_text())
    config_digest = manifest["config"]["digest"].split(":", 1)[1]
    config = json.loads((blobs / config_digest).read_text())

    assert base_images == [("python:3.12-alpine", "linux", "amd64")]
    assert config["config"]["WorkingDir"] == "/srv"
    assert config["config"]["Env"] == ["BASE=kept", "MODE=production"]
    assert config["config"]["Labels"] == {"base": "kept", "owner": "team"}
    assert config["config"]["ExposedPorts"] == {"8080/tcp": {}}
    assert config["config"]["Cmd"] == ["python", "app.py"]
    assert config["rootfs"]["diff_ids"] == [layer["digest"] for layer in manifest["layers"]]


@pytest.mark.parametrize("existing", ['[build]\ntag = "a:1"\n', 'build = { tag = "a:1" }\n'])
def test_force_refuses_to_add_tool_table_beside_build(tmp_path, monkeypatch, capsys, existing):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12\n")
    out = tmp_path / "pycontainer.toml"
    out.write_text(existing)

    assert _run_cli(monkeypatch, ["import", str(dockerfile), "-o", str(out), "--force"]) == 2
    assert out.read_text() == existing
    assert "already defines [build]" in capsys.readouterr().err


def test_shell_form_uses_default_shell_and_preserves_quotes_verbatim():
    result = import_dockerfile("FROM x\nENTRYPOINT echo \"a  b\" 'c $HOME' && exec run\n")
    assert result.config["entrypoint"] == ["/bin/sh", "-c", "echo \"a  b\" 'c $HOME' && exec run"]


def test_explicit_shell_converts_shell_form_but_not_exec_form():
    text = 'FROM x\nENTRYPOINT ["python", "-m", "app"]\nCMD serve --port "8 0"\n'
    result = import_dockerfile(text, shell=["/bin/bash", "-lc"])
    assert result.config["entrypoint"] == ["python", "-m", "app"]
    assert result.config["cmd"] == ["/bin/bash", "-lc", 'serve --port "8 0"']
    assert any("/bin/bash" in n for n in result.notes)


def test_dockerfile_shell_applies_to_later_instructions_and_still_warns():
    text = 'FROM x\nCMD early\nSHELL ["/bin/ash", "-eo", "pipefail", "-c"]\nENTRYPOINT late\n'
    result = import_dockerfile(text, shell=["/bin/bash", "-c"])
    assert result.config["cmd"] == ["/bin/bash", "-c", "early"]
    assert result.config["entrypoint"] == ["/bin/ash", "-eo", "pipefail", "-c", "late"]
    assert any("SHELL" in w and "RUN" in w for w in result.warnings)


@pytest.mark.parametrize("value", ["/bin/bash -c", "[]", '[""]', '["/bin/sh", 1]', '{"a": 1}', '["/bin/sh\\u0000"]'])
def test_malformed_dockerfile_shell_is_ignored_with_warning(value):
    result = import_dockerfile(f"FROM x\nSHELL {value}\nCMD run\n")
    assert result.config["cmd"] == ["/bin/sh", "-c", "run"]
    assert any("SHELL" in w and "ignored" in w for w in result.warnings)


@pytest.mark.parametrize("shell", [[], [""], ["/bin/sh", 1], "/bin/sh -c", ["/bin/sh\x00"]])
def test_malformed_explicit_shell_is_rejected(shell):
    with pytest.raises(ValueError, match="shell"):
        import_dockerfile("FROM x\nCMD run\n", shell=shell)


@pytest.mark.parametrize("value", ["/bin/bash -c", "[]", '["/bin/sh", 1]', "not json"])
def test_cli_shell_option_rejects_malformed_arrays(tmp_path, monkeypatch, capsys, value):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM x\nCMD run\n")
    assert _run_cli(monkeypatch, ["import", str(dockerfile), "--shell", value]) == 2
    assert "shell" in capsys.readouterr().err


def test_cli_shell_option_converts_shell_form(tmp_path, monkeypatch, capsys):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM x\nCMD run it\n")
    assert _run_cli(monkeypatch, ["import", str(dockerfile), "--shell", '["/bin/bash", "-c"]']) == 0
    assert 'cmd = ["/bin/bash", "-c", "run it"]' in capsys.readouterr().out


def test_invalid_exec_json_is_shell_form_with_accurate_note():
    result = import_dockerfile("FROM x\nCMD [python, app.py]\n")
    assert result.config["cmd"] == ["/bin/sh", "-c", "[python, app.py]"]
    assert any("shell form" in n for n in result.notes)


def test_import_never_executes_shell_text(monkeypatch):
    import os
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("importer must not execute commands")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(os, "popen", forbidden)
    result = import_dockerfile("FROM x\nSHELL [\"/bin/bash\", \"-c\"]\nCMD touch /tmp/pwned && rm -rf /\n")
    assert result.config["cmd"] == ["/bin/bash", "-c", "touch /tmp/pwned && rm -rf /"]


def test_stopsignal_maps_docker_spellings_and_warns_on_unknown():
    assert import_dockerfile("FROM x\nSTOPSIGNAL SIGINT\n").config["stop_signal"] == "SIGINT"
    assert import_dockerfile("FROM x\nSTOPSIGNAL term\n").config["stop_signal"] == "SIGTERM"
    assert import_dockerfile("FROM x\nSTOPSIGNAL 9\n").config["stop_signal"] == "SIGKILL"
    assert import_dockerfile("FROM x\nSTOPSIGNAL rtmin+3\n").config["stop_signal"] == "SIGRTMIN+3"
    assert import_dockerfile("FROM x\nARG S=SIGQUIT\nSTOPSIGNAL $S\n").config["stop_signal"] == "SIGQUIT"
    for value in ("99", "SIGFOO"):
        result = import_dockerfile(f"FROM x\nSTOPSIGNAL {value}\n")
        assert "stop_signal" not in result.config
        assert any("STOPSIGNAL" in w for w in result.warnings)


def test_volume_maps_json_and_word_forms_and_warns_on_unsafe_paths():
    text = 'FROM x\nARG D=/data\nVOLUME ["/srv/uploads", "$D"]\nVOLUME /logs /data relative\n'
    result = import_dockerfile(text)
    assert result.config["volumes"] == ["/srv/uploads", "/data", "/logs"]
    assert any("VOLUME" in w and "relative" in w for w in result.warnings)
    assert any("metadata" in n for n in result.notes)
    data = tomllib.loads(render_toml(result.config))["tool"]["pycontainer"]
    assert data["volumes"] == ["/srv/uploads", "/data", "/logs"]


def test_imported_volume_literal_dollar_survives_config_loading(tmp_path):
    from pycontainer.config_loader import config_from_file

    result = import_dockerfile('FROM python:3.12\nVOLUME ["/cost$5"]\n')
    path = tmp_path / "pyproject.toml"
    path.write_text(render_toml(result.config))

    assert result.config["volumes"] == ["/cost$5"]
    assert config_from_file(path).volumes == ["/cost$5"]


def test_existing_import_output_is_unchanged_without_new_instructions():
    assert import_dockerfile(SAMPLE).config["cmd"] == ["/bin/sh", "-c", "uvicorn app:app"]


def test_shell_form_on_distroless_base_warns_missing_shell():
    result = import_dockerfile("FROM gcr.io/distroless/python3\nCMD python app.py\n")
    assert result.config["cmd"] == ["/bin/sh", "-c", "python app.py"]
    assert any("distroless" in w for w in result.warnings)
    exec_result = import_dockerfile('FROM gcr.io/distroless/python3\nCMD ["python", "app.py"]\n')
    assert not any("distroless" in w for w in exec_result.warnings)


@pytest.mark.parametrize("alias,canonical", [("IOT", "SIGABRT"), ("cld", "SIGCHLD"), ("SIGPOLL", "SIGIO")])
def test_stopsignal_alias_maps_to_canonical(alias, canonical):
    result = import_dockerfile(f"FROM python:3.12\nSTOPSIGNAL {alias}\n")
    assert result.config["stop_signal"] == canonical


@pytest.mark.parametrize("args", ["", "[]"])
def test_empty_volume_warns(args):
    result = import_dockerfile(f"FROM python:3.12\nVOLUME {args}\n")
    assert "volumes" not in result.config
    assert any("VOLUME has no paths" in w for w in result.warnings)
