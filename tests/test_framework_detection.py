import json
import logging
import sys
import tarfile
from pathlib import Path

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer import cli
from pycontainer.config import BuildConfig
from pycontainer.framework import apply_framework_defaults, detect_framework


def write_file(context, name, content):
    path = context / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "source",
    [
        "from celery import Celery\napp = Celery('worker')\n",
        "from celery import Celery as C\napp: C = C('worker')\n",
        "import celery as c\napp = c.Celery('worker')\n",
        "from celery.app import Celery\napp = Celery('worker')\n",
        "import celery.app as c\napp = c.Celery('worker')\n",
        "import celery.app\napp = celery.app.Celery('worker')\n",
    ],
)
def test_celery_requires_a_declared_dependency_and_finds_the_app(tmp_path, source):
    write_file(tmp_path, "worker.py", source)
    assert detect_framework(tmp_path) is None

    write_file(tmp_path, "requirements.txt", "Celery[redis]>=5.4\n")
    assert detect_framework(tmp_path) == (
        "Celery",
        ["celery", "-A", "worker:app", "worker", "--loglevel=INFO"],
        [],
    )


@pytest.mark.parametrize("worker_count", [1, 2])
def test_celery_with_web_app_warns_about_the_separate_worker(tmp_path, caplog, worker_count):
    write_file(tmp_path, "requirements.txt", "celery\n")
    for number in range(worker_count):
        write_file(
            tmp_path, f"worker{number}.py", "from celery import Celery\napp = Celery('x')\n"
        )
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    assert detect_framework(tmp_path)[0] == "FastAPI"
    assert "celery" in caplog.text.lower()
    assert "separate" in caplog.text.lower()
    assert "entrypoint" in caplog.text.lower()


def test_multiple_celery_apps_require_an_explicit_entrypoint(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    for name in ("first.py", "second.py"):
        write_file(tmp_path, name, "from celery import Celery\napp = Celery('x')\n")

    with pytest.raises(ValueError, match="explicit entrypoint"):
        detect_framework(tmp_path)

    config = BuildConfig(entrypoint=["celery", "-A", "first:app", "worker"])
    assert apply_framework_defaults(config, tmp_path).entrypoint == config.entrypoint


def test_celery_ignores_excluded_sources_and_invalid_module_names(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "tests/worker.py", "from celery import Celery\napp = Celery()\n")
    write_file(tmp_path, ".venv/worker.py", "from celery import Celery\napp = Celery()\n")
    assert detect_framework(tmp_path, include_paths=["."]) is None

    write_file(
        tmp_path, "src/not-a-module/worker.py", "from celery import Celery\napp = Celery()\n"
    )
    with pytest.raises(ValueError, match="explicit entrypoint"):
        detect_framework(tmp_path)


@pytest.mark.parametrize(
    ("requirements", "subcommand"),
    [
        ("jupyterlab>=4\n", "lab"),
        ("notebook>=7\n", "notebook"),
        ("notebook\njupyterlab\n", "lab"),
    ],
)
def test_jupyter_requires_notebooks_and_a_declared_dependency(
    tmp_path, requirements, subcommand
):
    write_file(tmp_path, "requirements.txt", requirements)
    assert detect_framework(tmp_path) is None
    write_file(tmp_path, "notebooks/research.ipynb", "{}")

    detected = detect_framework(tmp_path)
    assert detected == (
        "Jupyter",
        ["jupyter", subcommand, "--ip=0.0.0.0", "--port=8888", "--no-browser"],
        [8888],
    )
    assert not any("token" in arg.lower() or "password" in arg.lower() for arg in detected[1])


def test_notebook_without_jupyter_dependency_has_verbose_note(tmp_path, caplog):
    write_file(tmp_path, "requirements.txt", "requests\n")
    write_file(tmp_path, "notebooks/research.ipynb", "{}")

    with caplog.at_level(logging.DEBUG):
        assert detect_framework(tmp_path) is None
    assert "notebook" in caplog.text.lower()
    assert "dependency" in caplog.text.lower()


def test_jupyter_default_selection_includes_notebooks_in_the_app_layer(tmp_path):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "jupyterlab\n")
    write_file(context, "notebooks/research.ipynb", "{}")
    write_file(context, "tests/fixture.ipynb", "{}")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "output"), use_cache=False
    )
    builder = ImageBuilder(config)
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    layer = builder._create_app_layer(layers_dir, builder.context_include_paths)

    with tarfile.open(layer.tar_path) as archive:
        names = archive.getnames()
    assert "app/notebooks/research.ipynb" in names
    assert "app/tests/fixture.ipynb" not in names
    assert config.entrypoint[0:2] == ["jupyter", "lab"]
    assert "JUPYTER_TOKEN" not in config.env


def test_jupyter_respects_explicit_includes_and_exclusions(tmp_path):
    write_file(tmp_path, "requirements.txt", "jupyterlab\n")
    write_file(tmp_path, "app.py", "print('not a notebook')\n")
    write_file(tmp_path, "notebooks/research.ipynb", "{}")
    write_file(tmp_path, "tests/fixture.ipynb", "{}")

    assert detect_framework(tmp_path, include_paths=["app.py"]) is None
    assert detect_framework(tmp_path, include_paths=["."], exclude_paths=["notebooks"]) is None


@pytest.mark.parametrize(
    "source", ["import streamlit as st\n", "from streamlit import title\n"]
)
def test_streamlit_requires_an_import_and_declared_dependency(tmp_path, source):
    write_file(tmp_path, "src/dashboard.py", source)
    assert detect_framework(tmp_path) is None
    write_file(tmp_path, "requirements.txt", "streamlit>=1\n")

    assert detect_framework(tmp_path) == (
        "Streamlit",
        [
            "streamlit",
            "run",
            "src/dashboard.py",
            "--server.address=0.0.0.0",
            "--server.port=8501",
        ],
        [8501],
    )


def test_streamlit_ignores_comments_strings_and_similar_dependency_names(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit-helper\n")
    write_file(tmp_path, "app.py", "# import streamlit\nexample = 'import streamlit'\n")
    assert detect_framework(tmp_path) is None
    write_file(tmp_path, "app.py", "import streamlit\n")
    assert detect_framework(tmp_path) is None


def test_relative_imports_are_not_framework_imports(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\ngradio\ncelery\n")
    write_file(
        tmp_path, "app.py",
        "from .streamlit import title\n"
        "from .celery import Celery\napp = Celery()\n"
        "from .gradio import Blocks\ndemo = Blocks()\ndemo.launch()\n",
    )
    assert detect_framework(tmp_path) is None


@pytest.mark.parametrize(
    "source",
    [
        "import gradio as gr\ndemo = gr.Interface(fn=None, inputs=[], outputs=[])\ndemo.launch()\n",
        "import gradio as gr\nwith gr.Blocks() as demo:\n    pass\ndemo.launch()\n",
        "from gradio import Interface as UI\nUI(fn=None, inputs=[], outputs=[]).launch()\n",
        "import gradio.blocks as blocks\ndemo = blocks.Blocks()\ndemo.launch()\n",
        "import gradio.blocks\ndemo = gradio.blocks.Blocks()\ndemo.launch()\n",
    ],
)
def test_gradio_finds_launched_apps_and_preserves_user_environment(tmp_path, source):
    write_file(tmp_path, "requirements.txt", "gradio>=4\n")
    write_file(tmp_path, "demo.py", source)

    assert detect_framework(tmp_path) == ("Gradio", ["python", "demo.py"], [7860])
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    assert config.env["GRADIO_SERVER_NAME"] == "0.0.0.0"
    config = apply_framework_defaults(
        BuildConfig(env={"GRADIO_SERVER_NAME": "127.0.0.1"}), tmp_path
    )
    assert config.env["GRADIO_SERVER_NAME"] == "127.0.0.1"


def test_gradio_without_matching_launch_requires_an_explicit_entrypoint(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(
        tmp_path,
        "demo.py",
        "import gradio as gr\ndemo = gr.Blocks()\nsomething_else.launch()\n",
    )
    with pytest.raises(ValueError, match="explicit entrypoint"):
        detect_framework(tmp_path)


def test_gradio_unlaunched_component_does_not_hide_the_unique_app(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(tmp_path, "components.py", "import gradio as gr\ncomponent = gr.Interface()\n")
    write_file(tmp_path, "app.py", "import gradio as gr\napp = gr.Blocks()\napp.launch()\n")
    assert detect_framework(tmp_path) == ("Gradio", ["python", "app.py"], [7860])


@pytest.mark.parametrize(
    ("requirement", "source"),
    [
        ("streamlit", "import streamlit\n"),
        ("gradio", "import gradio as gr\napp = gr.Blocks()\napp.launch()\n"),
    ],
)
def test_multiple_ui_scripts_require_an_explicit_entrypoint(tmp_path, requirement, source):
    write_file(tmp_path, "requirements.txt", requirement + "\n")
    write_file(tmp_path, "first.py", source)
    write_file(tmp_path, "second.py", source)
    with pytest.raises(ValueError, match="explicit entrypoint"):
        detect_framework(tmp_path)

    config = BuildConfig(cmd=["python", "first.py"])
    assert apply_framework_defaults(config, tmp_path).cmd == ["python", "first.py"]


def test_fastapi_wins_for_mounted_gradio_without_a_standalone_launch(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(
        tmp_path,
        "app.py",
        "from fastapi import FastAPI\nimport gradio as gr\n"
        "app = FastAPI()\nwith gr.Blocks() as demo:\n    pass\n"
        "app = gr.mount_gradio_app(app, demo, path='/ui')\n",
    )
    assert detect_framework(tmp_path)[0] == "FastAPI"


def test_framework_precedence_reports_coexisting_candidates(tmp_path, caplog):
    write_file(tmp_path, "requirements.txt", "streamlit\ngradio\njupyterlab\ncelery\n")
    write_file(tmp_path, "a.py", "import streamlit\n")
    write_file(tmp_path, "b.py", "import gradio as gr\napp = gr.Blocks()\napp.launch()\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    write_file(tmp_path, "notebooks/research.ipynb", "{}")

    assert detect_framework(tmp_path)[0] == "Streamlit"
    for name in ("streamlit", "gradio", "jupyter", "celery"):
        assert name in caplog.text.lower()


def test_legacy_web_framework_precedence_is_preserved(tmp_path):
    write_file(tmp_path, "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "manage.py", "import django\n")
    assert detect_framework(tmp_path)[0] == "FastAPI"


def test_declared_dependencies_support_custom_requirements_and_project_metadata(tmp_path):
    write_file(tmp_path, "app.py", "import streamlit\n")
    write_file(tmp_path, "requirements/production.txt", "Streamlit[charts]>=1\n")
    assert detect_framework(tmp_path) is None
    assert detect_framework(tmp_path, requirements_file="requirements/production.txt")[0] == "Streamlit"

    write_file(
        tmp_path,
        "pyproject.toml",
        '[project]\ndependencies = ["streamlit>=1; python_version >= \'3.11\'"]\n',
    )
    assert detect_framework(tmp_path)[0] == "Streamlit"


def test_nested_requirements_stay_within_the_build_context(tmp_path):
    context = tmp_path / "context"
    write_file(context, "app.py", "import streamlit\n")
    write_file(context, "requirements/production.txt", "-r ../shared.txt\n")
    write_file(context, "shared.txt", "streamlit>=1\n")
    assert detect_framework(context, requirements_file="requirements/production.txt")[0] == "Streamlit"

    write_file(context, "requirements/production.txt", "-r ../../outside.txt\n")
    write_file(tmp_path, "outside.txt", "streamlit\n")
    with pytest.raises(ValueError, match="escapes the build context"):
        detect_framework(context, requirements_file="requirements/production.txt")


def test_requirements_line_continuations_are_parsed_like_pip(tmp_path):
    write_file(tmp_path, "app.py", "import streamlit\n")
    write_file(tmp_path, "requirements.txt", "streamlit>=1 \\\n    --hash=sha256:abc123\n")
    assert detect_framework(tmp_path)[0] == "Streamlit"


def test_remote_requirements_include_does_not_log_credentials(tmp_path, caplog):
    write_file(tmp_path, "app.py", "import streamlit\n")
    write_file(
        tmp_path, "requirements.txt", "-r https://user:secret@example.invalid/requirements.txt\n"
    )
    assert detect_framework(tmp_path) is None
    assert "remote requirements" in caplog.text
    assert "secret" not in caplog.text


def test_poetry_and_setup_cfg_runtime_dependencies_are_recognized(tmp_path):
    write_file(tmp_path, "notebooks/research.ipynb", "{}")
    write_file(tmp_path, "pyproject.toml", '[tool.poetry.dependencies]\njupyterlab = "^4"\n')
    assert detect_framework(tmp_path)[0] == "Jupyter"

    (tmp_path / "pyproject.toml").unlink()
    write_file(tmp_path, "setup.cfg", "[options]\ninstall_requires =\n    notebook>=7\n")
    assert detect_framework(tmp_path)[1][1] == "notebook"


def test_detector_accepts_preselected_files_and_never_executes_them(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    marker = tmp_path / "executed"
    chosen = write_file(
        tmp_path,
        "chosen.py",
        f"import streamlit\nopen({str(marker)!r}, 'w').write('executed')\n",
    )
    write_file(tmp_path, "other.py", "import streamlit\n")

    assert detect_framework(tmp_path, files=[chosen])[1][2] == "chosen.py"
    assert not marker.exists()


@pytest.mark.parametrize("ports", [[9000], []])
def test_user_command_entrypoint_and_ports_override_detection(tmp_path, ports):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    config = BuildConfig(
        entrypoint=["custom-entrypoint"], cmd=["custom-arg"], exposed_ports=ports
    )
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == ["custom-entrypoint"]
    assert config.cmd == ["custom-arg"]
    assert config.exposed_ports == ports

    command_only = BuildConfig(cmd=["python", "manual.py"], exposed_ports=ports)
    apply_framework_defaults(command_only, tmp_path)
    assert command_only.entrypoint is None
    assert command_only.cmd == ["python", "manual.py"]
    assert command_only.exposed_ports == ports


def test_cli_reports_ambiguity_without_a_traceback(tmp_path, monkeypatch, capsys):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "first.py", "import streamlit\n")
    write_file(tmp_path, "second.py", "import streamlit\n")
    monkeypatch.setattr(
        sys, "argv", ["pycontainer", "build", "--context", str(tmp_path), "--dry-run"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "explicit entrypoint" in stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize(
    ("requirements", "message"),
    [
        ("-r missing.txt\n", "Requirements file not found"),
        ("-r ../outside.txt\n", "escapes the build context"),
        ("-r\n", "Invalid requirements include"),
    ],
)
def test_cli_reports_invalid_requirement_metadata(tmp_path, monkeypatch, capsys, requirements, message):
    context = tmp_path / "context"
    write_file(context, "app.py", "import streamlit\n")
    write_file(context, "requirements.txt", requirements)
    monkeypatch.setattr(
        sys, "argv", ["pycontainer", "build", "--context", str(context), "--dry-run"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert message in stderr
    assert "Traceback" not in stderr


def test_cli_configured_command_is_shown_without_auto_entrypoint(tmp_path, monkeypatch, capsys):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    config = write_file(
        tmp_path, "pycontainer.toml",
        '[build]\ncmd = ["python", "manual.py"]\nexposed-ports = []\n',
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pycontainer", "build", "--config", str(config),
            "--context", str(context), "--dry-run",
        ],
    )
    cli.main()
    stdout = capsys.readouterr().out
    assert "Entrypoint: <inherited from base>" in stdout
    assert "Command: python manual.py" in stdout
    assert "Exposed Ports: <cleared>" in stdout


def image_config_from_build(tmp_path, monkeypatch, config):
    base = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "Env": [],
            "WorkingDir": "/",
            "Entrypoint": ["base-entrypoint"],
            "Cmd": ["python3"],
            "ExposedPorts": {"9999/tcp": {}},
        },
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    builder = ImageBuilder(config)
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: ([], base))
    builder.build()
    output = Path(config.output_dir)
    index = json.loads((output / "index.json").read_text())

    def blob(digest):
        return json.loads((output / "blobs" / "sha256" / digest.split(":", 1)[1]).read_text())

    manifest = blob(index["manifests"][0]["digest"])
    return blob(manifest["config"]["digest"])["config"]


def test_generated_celery_image_does_not_inherit_base_command_or_ports(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "celery\n")
    write_file(context, "worker.py", "from celery import Celery\napp = Celery('x')\n")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False
    )
    image = image_config_from_build(tmp_path, monkeypatch, config)
    assert image["Entrypoint"] == ["celery", "-A", "worker:app", "worker", "--loglevel=INFO"]
    assert image["Cmd"] == []
    assert image["ExposedPorts"] == {}


def test_explicit_command_and_empty_ports_survive_image_config_merge(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    config = BuildConfig(
        context_dir=str(context),
        output_dir=str(tmp_path / "image"),
        use_cache=False,
        cmd=["python", "manual.py"],
        exposed_ports=[],
    )
    image = image_config_from_build(tmp_path, monkeypatch, config)
    assert image["Entrypoint"] == ["base-entrypoint"]
    assert image["Cmd"] == ["python", "manual.py"]
    assert image["ExposedPorts"] == {}


def test_explicit_empty_entrypoint_clears_the_base_entrypoint(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    config = BuildConfig(
        context_dir=str(context),
        output_dir=str(tmp_path / "image"),
        use_cache=False,
        entrypoint=[],
        exposed_ports=[],
    )
    image = image_config_from_build(tmp_path, monkeypatch, config)
    assert image["Entrypoint"] == []
    assert image["Cmd"] == ["python3"]
    assert image["ExposedPorts"] == {}
