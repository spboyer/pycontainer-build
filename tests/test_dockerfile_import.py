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
