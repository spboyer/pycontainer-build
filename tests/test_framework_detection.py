import json
import logging
import sys
import tarfile
import traceback
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer import cli
from pycontainer.config import BuildConfig
from pycontainer.dependencies import MAX_REQUIREMENTS_BYTES
from pycontainer.framework import apply_framework_defaults, detect_framework
from pycontainer.fs_utils import iter_files
from pycontainer.project import default_include_paths


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


def test_conda_prefix_notebooks_are_neither_detected_nor_shipped(tmp_path):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "jupyterlab\n")
    (context / "env" / "conda-meta").mkdir(parents=True)
    write_file(context, "env/host.ipynb", "{}")
    options = dict(include_paths=["."], exclude_defaults=False)

    assert detect_framework(context, **options) is None

    write_file(context, "notebooks/research.ipynb", "{}")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "output"),
        use_cache=False, **options,
    )
    builder = ImageBuilder(config)
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    layer = builder._create_app_layer(layers_dir, builder.context_include_paths)

    assert config.entrypoint[:2] == ["jupyter", "lab"]
    with tarfile.open(layer.tar_path) as archive:
        names = archive.getnames()
    assert "app/notebooks/research.ipynb" in names
    assert "app/env/host.ipynb" not in names


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
    assert "GRADIO_SERVER_NAME" not in config.env
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


def test_gradio_unlaunched_component_does_not_hide_the_unique_app(tmp_path, caplog):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(tmp_path, "components.py", "import gradio as gr\ncomponent = gr.Interface()\n")
    write_file(tmp_path, "app.py", "import gradio as gr\napp = gr.Blocks()\napp.launch()\n")
    assert detect_framework(tmp_path) == ("Gradio", ["python", "app.py"], [7860])
    assert "no matching launch" in caplog.text


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


def test_fastapi_alias_wins_for_mounted_gradio(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(
        tmp_path, "api.py",
        "from fastapi import FastAPI as API\nimport gradio as gr\n"
        "app = API()\ndemo = gr.Blocks()\n"
        "app = gr.mount_gradio_app(app, demo, path='/ui')\n",
    )
    assert detect_framework(tmp_path)[0] == "FastAPI"


def test_function_local_web_apps_do_not_create_importable_candidates(tmp_path):
    write_file(
        tmp_path, "api.py",
        "from fastapi import FastAPI\napp = FastAPI()\n"
        "def factory():\n    inner = FastAPI()\n    return inner\n",
    )
    framework, entrypoint, _ = detect_framework(tmp_path)
    assert framework == "FastAPI"
    assert entrypoint[1] == "api:app"


def test_fastapi_factory_without_module_binding_requires_explicit_command(tmp_path):
    write_file(
        tmp_path, "api.py",
        "from fastapi import FastAPI\ndef factory():\n    return FastAPI()\n",
    )
    with pytest.raises(ValueError, match="explicit entrypoint"):
        detect_framework(tmp_path)


FLASK_RUN = ["flask", "run", "--host=0.0.0.0", "--port=5000"]


def test_flask_app_factory_uses_flask_run_discovery(tmp_path):
    write_file(
        tmp_path, "app.py",
        "from flask import Flask\ndef create_app():\n"
        "    app = Flask(__name__)\n    return app\n",
    )
    assert detect_framework(tmp_path) == ("Flask", FLASK_RUN, [5000])


def test_multiple_flask_instances_share_one_flask_run_entrypoint(tmp_path):
    write_file(tmp_path, "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    write_file(tmp_path, "other.py", "from flask import Flask\nbp = Flask('x')\n")
    assert detect_framework(tmp_path) == ("Flask", FLASK_RUN, [5000])


def test_mounted_fastapi_sub_application_is_not_a_root_candidate(tmp_path):
    write_file(
        tmp_path, "main.py",
        "from fastapi import FastAPI\napp = FastAPI()\napi = FastAPI()\n"
        "app.mount('/api', api)\n",
    )
    framework, entrypoint, _ = detect_framework(tmp_path)
    assert framework == "FastAPI"
    assert entrypoint[1] == "main:app"


def test_unmounted_fastapi_applications_remain_ambiguous(tmp_path):
    write_file(
        tmp_path, "main.py",
        "from fastapi import FastAPI\napp = FastAPI()\napi = FastAPI()\n",
    )
    with pytest.raises(ValueError, match="Multiple FastAPI"):
        detect_framework(tmp_path)


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


def test_linked_source_directory_outside_context_is_not_detected(tmp_path, caplog):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    source = tmp_path / "external"
    write_file(source, "app.py", "import streamlit\n")
    try:
        (context / "src").symlink_to(source, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlink unavailable: {error}")

    assert detect_framework(context, include_paths=["src"]) is None
    assert "directory symlink resolves outside the build context" in caplog.text


def test_preselected_files_still_respect_explicit_and_default_excludes(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    excluded = write_file(tmp_path, "tests/example.py", "import streamlit\n")
    custom = write_file(tmp_path, "generated/example.py", "import streamlit\n")
    assert detect_framework(tmp_path, files=[excluded, custom], exclude_paths=["generated"]) is None
    assert detect_framework(tmp_path, files=[excluded], exclude_defaults=False)[0] == "Streamlit"


def test_selected_files_accept_an_alias_for_the_context_root(tmp_path, monkeypatch):
    context = tmp_path / "context"
    alias = tmp_path / "context-alias"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    original_resolve = Path.resolve

    def resolve_alias(path, *args, **kwargs):
        if path == alias or alias in path.parents:
            path = context / path.relative_to(alias)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_alias)
    assert detect_framework(alias, files=[alias / "app.py"])[0] == "Streamlit"


def test_selected_files_still_reject_a_resolved_path_outside_context(tmp_path, monkeypatch):
    context = tmp_path / "context"
    source = write_file(context, "app.py", "import streamlit\n")
    outside = write_file(tmp_path, "outside.py", "import streamlit\n")
    original_resolve = Path.resolve

    def resolve_source(path, *args, **kwargs):
        if path == source:
            path = outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_source)
    with pytest.raises(ValueError, match="outside the build context"):
        detect_framework(context, files=[source])


def test_multiple_fastapi_apps_require_explicit_selection(tmp_path):
    source = "from fastapi import FastAPI\napp = FastAPI()\n"
    write_file(tmp_path, "first.py", source)
    write_file(tmp_path, "second.py", source)
    with pytest.raises(ValueError, match="Multiple FastAPI"):
        detect_framework(tmp_path)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('"""\nexample = FastAPI()\n"""\nfrom fastapi import FastAPI\napp = FastAPI()\n', "FastAPI"),
        ('"""\nexample = Flask(__name__)\n"""\nfrom flask import Flask\napp = Flask(__name__)\n', "Flask"),
    ],
)
def test_legacy_web_detection_ignores_app_assignments_in_docstrings(tmp_path, source, expected):
    write_file(tmp_path, "app.py", source)
    assert detect_framework(tmp_path)[0] == expected


def test_unmounted_gradio_with_fastapi_is_not_silently_ignored(tmp_path, caplog):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "demo.py", "import gradio as gr\ndemo = gr.Blocks()\n")

    assert detect_framework(tmp_path)[0] == "FastAPI"
    assert "gradio" in caplog.text.lower()
    assert "launch" in caplog.text.lower()


def test_ambiguous_web_app_still_reports_the_separate_celery_worker(tmp_path, caplog):
    write_file(tmp_path, "requirements.txt", "streamlit\ncelery\n")
    write_file(tmp_path, "first.py", "import streamlit\n")
    write_file(tmp_path, "second.py", "import streamlit\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")

    with pytest.raises(ValueError, match="Multiple Streamlit"):
        detect_framework(tmp_path)
    assert "separate" in caplog.text.lower()


def test_ambiguous_celery_with_explicit_command_clears_default_ports(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    for name in ("first.py", "second.py"):
        write_file(tmp_path, name, "from celery import Celery\napp = Celery()\n")

    config = BuildConfig(entrypoint=["celery", "-A", "first:app", "worker"])
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == ["celery", "-A", "first:app", "worker"]
    assert config.exposed_ports == []
    assert config.labels["framework"] == "celery"


def test_default_ports_remain_mutable_and_mutations_are_overrides(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")

    config = BuildConfig()
    assert config.exposed_ports == []
    config.exposed_ports.append(9000)
    apply_framework_defaults(config, tmp_path)
    assert config.exposed_ports == [9000]

    cleared = BuildConfig()
    cleared.exposed_ports.append(9000)
    cleared.exposed_ports.clear()
    apply_framework_defaults(cleared, tmp_path)
    assert cleared.exposed_ports == []


def test_build_config_serialization_preserves_default_and_explicit_empty_ports():
    default = BuildConfig()
    restored_default = BuildConfig(**json.loads(json.dumps(asdict(default))))
    assert not restored_default.has_exposed_ports()

    cleared = BuildConfig()
    cleared.exposed_ports.clear()
    restored_cleared = BuildConfig(**asdict(cleared))
    assert restored_cleared.has_exposed_ports()
    assert restored_cleared.exposed_ports == []
    restored_json = BuildConfig(**json.loads(json.dumps(asdict(cleared))))
    assert restored_json.has_exposed_ports()
    assert restored_json.exposed_ports == []
    copied = deepcopy(default)
    copied.exposed_ports.clear()
    assert BuildConfig(**asdict(copied)).has_exposed_ports()
    shared = replace(default)
    shared.exposed_ports.clear()
    assert default.has_exposed_ports()
    assert BuildConfig(**asdict(default)).has_exposed_ports()


@pytest.mark.parametrize("ports", [None, []])
def test_security_registry_and_framework_settings_survive_config_roundtrip(tmp_path, ports):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    config = BuildConfig(
        security={"kubernetes": {"read_only_root_filesystem": True}},
        insecure_registries=["LOCALHOST:5000"],
        stop_signal="SIGINT",
        volumes=["/data"],
        exposed_ports=ports,
    )
    apply_framework_defaults(config, tmp_path)

    restored = BuildConfig(**json.loads(json.dumps(asdict(config))))
    apply_framework_defaults(restored, tmp_path)
    assert restored.security == config.security
    assert restored.insecure_registries == ["localhost:5000"]
    assert restored.stop_signal == "SIGINT"
    assert restored.volumes == ["/data"]
    assert restored.labels["framework"] == "streamlit"
    assert restored.entrypoint[0] == "streamlit"
    assert restored.exposed_ports == ([8501] if ports is None else [])


def test_dataclass_replace_preserves_explicit_empty_port_override(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    config = replace(BuildConfig(), exposed_ports=[])

    assert config.has_exposed_ports()
    assert apply_framework_defaults(config, tmp_path).exposed_ports == []


@pytest.mark.parametrize("copy_config", [replace, deepcopy])
def test_config_copies_preserve_generated_defaults_for_redetection(tmp_path, copy_config):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    original = apply_framework_defaults(BuildConfig(), tmp_path)
    copied = copy_config(original)
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    apply_framework_defaults(copied, tmp_path)
    assert copied.entrypoint[0] == "uvicorn"
    assert copied.exposed_ports == [8000]
    assert copied.labels["framework"] == "fastapi"
    assert original.labels["framework"] == "celery"


def test_serialized_generated_defaults_can_be_redetected(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    original = apply_framework_defaults(BuildConfig(), tmp_path)
    restored = BuildConfig(**json.loads(json.dumps(asdict(original))))
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    apply_framework_defaults(restored, tmp_path)
    assert restored.entrypoint[0] == "uvicorn"
    assert restored.exposed_ports == [8000]


def test_explicit_replacement_of_generated_ports_remains_an_override(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    original = apply_framework_defaults(BuildConfig(), tmp_path)
    copied = replace(original, exposed_ports=deepcopy(original.exposed_ports))
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    apply_framework_defaults(copied, tmp_path)
    assert copied.entrypoint[0] == "uvicorn"
    assert copied.exposed_ports == []


def test_direct_replacement_of_generated_ports_remains_an_override(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    config.exposed_ports = deepcopy(config.exposed_ports)
    restored = BuildConfig(**json.loads(json.dumps(asdict(config))))
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    for candidate in (config, restored):
        apply_framework_defaults(candidate, tmp_path)
        assert candidate.entrypoint[0] == "uvicorn"
        assert candidate.exposed_ports == []


def test_constructor_supplied_generated_lists_are_explicit(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    original = apply_framework_defaults(BuildConfig(), tmp_path)
    config = BuildConfig(
        entrypoint=deepcopy(original.entrypoint),
        exposed_ports=deepcopy(original.exposed_ports),
    )
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint[0] == "celery"
    assert config.exposed_ports == []


def test_replacing_default_ports_with_an_explicit_empty_copy(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    default = BuildConfig()
    config = replace(default, exposed_ports=deepcopy(default.exposed_ports))
    apply_framework_defaults(config, tmp_path)
    assert config.exposed_ports == []


@pytest.mark.parametrize("assign_after_construction", [False, True])
def test_copied_untouched_default_ports_are_explicit_when_supplied(
    tmp_path, assign_after_construction
):
    write_file(tmp_path, "app.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    original = BuildConfig()
    copied_ports = deepcopy(original.exposed_ports)
    if assign_after_construction:
        config = BuildConfig()
        config.exposed_ports = copied_ports
    else:
        config = BuildConfig(exposed_ports=copied_ports)
    apply_framework_defaults(config, tmp_path)
    assert config.has_exposed_ports()
    assert config.exposed_ports == []
    assert not original.has_exposed_ports()


def test_deepcopy_of_config_preserves_unspecified_ports():
    original = BuildConfig()
    assert not deepcopy(original).has_exposed_ports()


def test_serialization_preserves_user_mutations_of_generated_defaults(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    config.exposed_ports.clear()
    restored = BuildConfig(**json.loads(json.dumps(asdict(config))))
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    apply_framework_defaults(restored, tmp_path)
    assert restored.entrypoint[0] == "uvicorn"
    assert restored.exposed_ports == []


def test_replacing_generated_entrypoint_restores_inherited_command(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    config.entrypoint = ["custom"]
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == ["custom"]
    assert config.cmd is None


def test_jupyter_checkpoints_are_excluded_unless_default_excludes_are_disabled(tmp_path):
    write_file(tmp_path, "requirements.txt", "jupyterlab\n")
    write_file(tmp_path, "notebooks/research.ipynb", "{}")
    write_file(tmp_path, "notebooks/.ipynb_checkpoints/research-checkpoint.ipynb", "{}")

    defaults = default_include_paths(tmp_path)
    names = {relative.as_posix() for _, relative in iter_files(tmp_path, defaults)}
    assert "notebooks/research.ipynb" in names
    assert "notebooks/.ipynb_checkpoints/research-checkpoint.ipynb" not in names

    defaults = default_include_paths(tmp_path, exclude_defaults=False)
    names = {
        relative.as_posix()
        for _, relative in iter_files(tmp_path, defaults, exclude_defaults=False)
    }
    assert "notebooks/.ipynb_checkpoints/research-checkpoint.ipynb" in names


def test_notebook_selection_preserves_the_existing_context_fallback(tmp_path):
    write_file(tmp_path, "requirements/custom.txt", "jupyterlab\n")
    write_file(tmp_path, "notebooks/research.ipynb", "{}")
    write_file(tmp_path, "data/input.csv", "value\n1\n")

    paths = default_include_paths(tmp_path, requirements_file="requirements/custom.txt")
    names = {relative.as_posix() for _, relative in iter_files(tmp_path, paths)}
    assert "data/input.csv" in names


def test_jupyter_default_allows_root_only_for_root_images(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "jupyterlab\n")
    write_file(context, "analysis.ipynb", "{}")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "root-image"), use_cache=False
    )
    command = image_config_from_build(tmp_path, monkeypatch, config)["Entrypoint"]
    assert "--allow-root" in command
    assert not any("token" in arg.lower() or "password" in arg.lower() for arg in command)

    non_root = BuildConfig(
        context_dir=str(context),
        output_dir=str(tmp_path / "user-image"),
        use_cache=False,
        user="1000",
    )
    command = image_config_from_build(tmp_path, monkeypatch, non_root)["Entrypoint"]
    assert "--allow-root" not in command


def test_src_layout_celery_uses_its_import_root(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "celery\n")
    write_file(context, "src/proj/__init__.py", "")
    write_file(
        context, "src/proj/celery.py",
        "from celery import Celery\napp = Celery('proj', include=['proj.tasks'])\n",
    )
    write_file(context, "src/proj/tasks.py", "")
    assert detect_framework(context)[1][2] == "proj.celery:app"

    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False
    )
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["PYTHONPATH=/base/packages"]
    )
    env = dict(item.split("=", 1) for item in image["Env"])
    assert image["Entrypoint"][2] == "proj.celery:app"
    assert env["PYTHONPATH"] == "/app/src:/base/packages"


def test_environment_requirements_includes_and_vcs_names_are_recognized(tmp_path, monkeypatch):
    write_file(tmp_path, "requirements.txt", "-r ${PROJECT_REQUIREMENTS}\n")
    write_file(tmp_path, "requirements/worker.txt", "git+https://example.invalid/repo.git#egg=celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    monkeypatch.setenv("PROJECT_REQUIREMENTS", "requirements/worker.txt")

    assert detect_framework(tmp_path)[0] == "Celery"


def test_setup_cfg_single_line_semicolon_requirements_are_recognized(tmp_path):
    write_file(tmp_path, "setup.cfg", "[options]\ninstall_requires = gradio>=4,<5; streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    assert detect_framework(tmp_path)[0] == "Streamlit"


def test_setup_cfg_file_requirements_are_recognized(tmp_path):
    write_file(tmp_path, "setup.cfg", "[options]\ninstall_requires = file: deps.txt\n")
    write_file(tmp_path, "deps.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    assert detect_framework(tmp_path)[0] == "Streamlit"


def test_utf8_bom_requirements_are_recognized(tmp_path):
    (tmp_path / "requirements.txt").write_bytes(b"\xef\xbb\xbfstreamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    assert detect_framework(tmp_path)[0] == "Streamlit"


@pytest.mark.parametrize("exclude_paths", [[], ["hidden"]])
def test_unselected_source_symlink_target_is_not_detected(tmp_path, caplog, exclude_paths):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    target = write_file(tmp_path, "hidden/app.py", "import streamlit\n")
    shortcut = tmp_path / "shortcut.py"
    try:
        shortcut.symlink_to(target)
    except OSError as error:
        pytest.skip(f"File symlinks unavailable: {error}")

    assert detect_framework(tmp_path, files=[shortcut], exclude_paths=exclude_paths) is None
    assert "symlink" in caplog.text.lower()


def test_selected_source_symlink_target_can_be_detected(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    target = write_file(tmp_path, "source.txt", "import streamlit\n")
    shortcut = tmp_path / "shortcut.py"
    try:
        shortcut.symlink_to(target.name)
    except OSError as error:
        pytest.skip(f"File symlinks unavailable: {error}")
    assert detect_framework(tmp_path, files=[shortcut, target])[0] == "Streamlit"


def test_source_symlink_requires_every_link_in_the_selected_chain(tmp_path, caplog):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    target = write_file(tmp_path, "source.txt", "import streamlit\n")
    intermediate = tmp_path / "intermediate.txt"
    shortcut = tmp_path / "shortcut.py"
    try:
        intermediate.symlink_to(target.name)
        shortcut.symlink_to(intermediate.name)
    except OSError as error:
        pytest.skip(f"File symlinks unavailable: {error}")
    assert detect_framework(tmp_path, files=[shortcut, target]) is None
    assert "symlink" in caplog.text.lower()
    assert detect_framework(tmp_path, files=[shortcut, intermediate, target])[0] == "Streamlit"


def test_selected_source_symlink_and_target_are_in_the_app_layer(tmp_path):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    target = write_file(context, "source.txt", "import streamlit\n")
    shortcut = context / "shortcut.py"
    try:
        shortcut.symlink_to(target.name)
    except OSError as error:
        pytest.skip(f"File symlinks unavailable: {error}")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False,
        include_paths=["shortcut.py", "source.txt"],
    )
    builder = ImageBuilder(config)
    assert builder.config.entrypoint[2] == "shortcut.py"
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    layer = builder._create_app_layer(layers_dir, builder.context_include_paths)
    with tarfile.open(layer.tar_path) as archive:
        link = archive.getmember("app/shortcut.py")
        assert link.issym()
        assert link.linkname == "source.txt"
        assert archive.getmember("app/source.txt").isfile()


def test_source_symlink_with_absolute_target_is_not_detected(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    target = write_file(tmp_path, "source.txt", "import streamlit\n")
    shortcut = tmp_path / "shortcut.py"
    try:
        shortcut.symlink_to(target)
    except OSError as error:
        pytest.skip(f"File symlinks unavailable: {error}")
    assert detect_framework(tmp_path, files=[shortcut, target]) is None


def test_source_under_symlinked_excluded_directory_is_not_detected(tmp_path, caplog):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    hidden = tmp_path / "hidden"
    write_file(hidden, "app.py", "import streamlit\n")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(hidden, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks unavailable: {error}")
    assert detect_framework(tmp_path, files=[alias / "app.py"], exclude_paths=["hidden"]) is None
    assert "symlink" in caplog.text.lower()


def test_environment_requirement_permission_error_is_redacted(tmp_path, monkeypatch):
    write_file(tmp_path, "requirements.txt", "-r ${PROJECT_REQUIREMENTS}\n")
    target = write_file(tmp_path, "private-credential-filename.txt", "streamlit\n")
    monkeypatch.setenv("PROJECT_REQUIREMENTS", target.name)
    original_is_file = Path.is_file

    def is_file(path):
        if path == target:
            raise PermissionError(f"Access denied: {path}")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)
    with pytest.raises(OSError) as error:
        detect_framework(tmp_path)
    assert target.name not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(
    "content",
    [
        'project = "not a table"\n',
        'tool = "not a table"\n',
        '[tool]\npoetry = "not a table"\n',
    ],
)
def test_invalid_project_metadata_types_are_reported(tmp_path, content):
    write_file(tmp_path, "pyproject.toml", content)
    with pytest.raises(ValueError, match="table"):
        detect_framework(tmp_path)


def test_nested_symlinked_requirements_use_logical_parent_and_excludes(tmp_path):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "-r configs/alias.txt\n")
    target = write_file(context, "shared/actual.txt", "-r nested.txt\n")
    write_file(context, "configs/nested.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    link = context / "configs" / "alias.txt"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"File symlinks unavailable: {error}")

    assert detect_framework(context)[0] == "Streamlit"
    assert detect_framework(context, exclude_paths=["configs/alias.txt"]) is None


def test_repeated_symlink_target_is_inspected_at_each_logical_parent(tmp_path):
    write_file(tmp_path, "requirements.txt", "-r branch/actual.txt\n")
    target = write_file(tmp_path, "branch/actual.txt", "-r sub/alias.txt\n")
    write_file(tmp_path, "branch/sub/sub/alias.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    try:
        (tmp_path / "branch/sub/alias.txt").symlink_to(target)
    except OSError as error:
        pytest.skip(f"File symlinks unavailable: {error}")
    assert detect_framework(tmp_path)[0] == "Streamlit"


def test_recursive_requirements_include_is_reported(tmp_path):
    write_file(tmp_path, "requirements.txt", "-r nested.txt\n")
    write_file(tmp_path, "nested.txt", "-r requirements.txt\n")
    with pytest.raises(ValueError, match="include cycle"):
        detect_framework(tmp_path)


def test_cyclic_environment_requirement_link_is_redacted_during_detection(tmp_path, monkeypatch):
    write_file(tmp_path, "requirements.txt", "-r ${PROJECT_REQUIREMENTS}\n")
    link = tmp_path / "private-credential-filename.txt"
    try:
        link.symlink_to(link)
    except OSError as error:
        pytest.skip(f"File symlinks unavailable: {error}")
    monkeypatch.setenv("PROJECT_REQUIREMENTS", link.name)
    with pytest.raises((OSError, ValueError)) as error:
        detect_framework(tmp_path)
    assert "private-credential-filename" not in "".join(traceback.format_exception(error.value))


def test_unresolved_requirements_include_warns_without_disclosing_values(tmp_path, monkeypatch, caplog):
    write_file(tmp_path, "requirements.txt", "-r ${UNSET_REQUIREMENTS_FILE}\n")
    write_file(tmp_path, "app.py", "print('not a framework')\n")
    monkeypatch.delenv("UNSET_REQUIREMENTS_FILE", raising=False)

    assert detect_framework(tmp_path) is None
    assert "requirements" in caplog.text.lower()


def test_missing_environment_requirements_path_is_not_disclosed(tmp_path, monkeypatch):
    write_file(tmp_path, "requirements.txt", "-r ${PROJECT_REQUIREMENTS}\n")
    monkeypatch.setenv("PROJECT_REQUIREMENTS", "sensitive-private-name.txt")
    with pytest.raises(FileNotFoundError) as error:
        detect_framework(tmp_path)
    assert "sensitive-private-name" not in str(error.value)


def test_framework_dependency_discovery_rejects_oversized_requirements(tmp_path):
    write_file(tmp_path, "requirements.txt", "-r nested.txt\n")
    (tmp_path / "nested.txt").write_bytes(b"#" * MAX_REQUIREMENTS_BYTES)
    with pytest.raises(ValueError, match="combined.*limit"):
        detect_framework(tmp_path)


def test_unparseable_source_does_not_silently_choose_a_fallback(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\nthis is not valid Python !!!\n")

    with pytest.raises(ValueError, match="explicit entrypoint"):
        detect_framework(tmp_path)
    config = BuildConfig(entrypoint=["python", "app.py"])
    assert apply_framework_defaults(config, tmp_path).entrypoint == ["python", "app.py"]


def test_unparseable_higher_priority_source_does_not_lose_to_jupyter(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\njupyterlab\n")
    write_file(tmp_path, "app.py", "import streamlit\nthis is not valid Python !!!\n")
    write_file(tmp_path, "analysis.ipynb", "{}")
    with pytest.raises(ValueError, match="explicit entrypoint"):
        detect_framework(tmp_path)
    config = BuildConfig(entrypoint=["jupyter", "lab"])
    assert apply_framework_defaults(config, tmp_path).entrypoint == ["jupyter", "lab"]


def test_irrelevant_large_sources_are_skipped_but_framework_sources_are_bounded(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "large.py", "# unrelated\n#" + "x" * (1024 * 1024))
    write_file(tmp_path, "app.py", "import streamlit\n")
    assert detect_framework(tmp_path)[0] == "Streamlit"

    write_file(tmp_path, "large.py", "# import streamlit\n#" + "x" * (1024 * 1024))
    with pytest.raises(ValueError, match="explicit entrypoint"):
        detect_framework(tmp_path)


def test_reapplying_defaults_does_not_treat_generated_values_as_user_overrides(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    assert config.labels["framework"] == "celery"
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "fastapi"
    assert config.entrypoint[:2] == ["uvicorn", "api:app"]
    assert config.exposed_ports == [8000]


def test_reapplying_defaults_preserves_changed_generated_values_as_user_overrides(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    config.entrypoint.append("--concurrency=2")
    config.exposed_ports.append(9100)
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "celery"
    assert config.entrypoint[-1] == "--concurrency=2"
    assert config.exposed_ports == [9100]


def test_reapplying_defaults_respects_a_new_user_command(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    config.cmd = ["python", "manual.py"]
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint is None
    assert config.cmd == ["python", "manual.py"]


def test_reapplying_defaults_preserves_an_explicitly_cleared_generated_command(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    config.cmd.clear()
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint is None
    assert config.cmd == []


def test_reapplying_defaults_preserves_explicitly_cleared_generated_ports(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    config.exposed_ports.clear()
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "fastapi"
    assert config.exposed_ports == []


def test_copying_config_does_not_change_generated_port_intent(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    replace(config)
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "fastapi"
    assert config.exposed_ports == [8000]


def test_reapplying_defaults_clears_obsolete_generated_values(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    worker = write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    config = apply_framework_defaults(BuildConfig(), tmp_path)
    worker.unlink()
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint is None
    assert config.cmd is None
    assert not config.has_exposed_ports()
    assert not config.labels.get("framework")


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


def test_cli_reports_build_time_errors_without_a_traceback(tmp_path, monkeypatch, capsys):
    write_file(tmp_path, "app.py", "print('app')\n")
    monkeypatch.setattr(ImageBuilder, "build", lambda self: (_ for _ in ()).throw(OSError("build failed")))
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--context", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "build failed" in stderr
    assert "Traceback" not in stderr


def test_cli_reports_invalid_project_metadata_without_a_traceback(tmp_path, monkeypatch, capsys):
    write_file(tmp_path, "pyproject.toml", 'project = "not a table"\n')
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--dry-run", "--context", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "[project]" in stderr
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


def image_config_from_build(tmp_path, monkeypatch, config, base_env=None):
    base = {
        "architecture": "amd64",
        "os": "linux",
        "config": {
            "Env": base_env or [],
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


def test_gradio_default_preserves_an_inherited_bind_address(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "gradio\n")
    write_file(context, "demo.py", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False
    )
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["GRADIO_SERVER_NAME=127.0.0.1"]
    )
    env = dict(item.split("=", 1) for item in image["Env"])
    assert env["GRADIO_SERVER_NAME"] == "127.0.0.1"


@pytest.mark.parametrize("copy_kind", ["replace", "deepcopy", "json"])
def test_copied_gradio_defaults_preserve_inherited_bind_address(tmp_path, monkeypatch, copy_kind):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "gradio\n")
    write_file(context, "demo.py", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False
    )
    config = apply_framework_defaults(config, context)
    if copy_kind == "json":
        copied = BuildConfig(**json.loads(json.dumps(asdict(config))))
    else:
        copied = (replace if copy_kind == "replace" else deepcopy)(config)
    image = image_config_from_build(
        tmp_path, monkeypatch, copied, base_env=["GRADIO_SERVER_NAME=127.0.0.1"]
    )
    env = dict(item.split("=", 1) for item in image["Env"])
    assert env["GRADIO_SERVER_NAME"] == "127.0.0.1"


@pytest.mark.parametrize("copy_kind", ["direct", "replace", "json"])
def test_replaced_gradio_environment_remains_explicit(tmp_path, monkeypatch, copy_kind):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "gradio\n")
    write_file(context, "demo.py", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False
    )
    config = apply_framework_defaults(config, context)
    explicit_env = {"GRADIO_SERVER_NAME": "0.0.0.0"}
    if copy_kind == "replace":
        config = replace(config, env=explicit_env)
    else:
        config.env = explicit_env
        if copy_kind == "json":
            config = BuildConfig(**json.loads(json.dumps(asdict(config))))
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["GRADIO_SERVER_NAME=127.0.0.1"]
    )
    env = dict(item.split("=", 1) for item in image["Env"])
    assert env["GRADIO_SERVER_NAME"] == "0.0.0.0"


def test_inplace_gradio_environment_override_wins_over_base_image(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "gradio\n")
    write_file(context, "demo.py", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False
    )
    apply_framework_defaults(config, context)
    config.env["GRADIO_SERVER_NAME"] = "0.0.0.0"
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["GRADIO_SERVER_NAME=127.0.0.1"]
    )
    env = dict(item.split("=", 1) for item in image["Env"])
    assert env["GRADIO_SERVER_NAME"] == "0.0.0.0"


def test_explicit_gradio_script_selects_its_defaults_over_fastapi(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "gradio\n")
    write_file(context, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(context, "demo.py", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n")
    config = BuildConfig(
        context_dir=str(context),
        output_dir=str(tmp_path / "image"),
        use_cache=False,
        entrypoint=["python", "demo.py"],
    )
    image = image_config_from_build(tmp_path, monkeypatch, config)
    env = dict(item.split("=", 1) for item in image["Env"])
    assert image["Entrypoint"] == ["python", "demo.py"]
    assert image["ExposedPorts"] == {"7860/tcp": {}}
    assert env["GRADIO_SERVER_NAME"] == "0.0.0.0"


def test_explicit_gradio_script_resolves_ambiguous_candidates(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "gradio\n")
    for name in ("first.py", "second.py"):
        write_file(context, name, "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False,
        entrypoint=["python", "first.py"],
    )
    image = image_config_from_build(tmp_path, monkeypatch, config)
    env = dict(item.split("=", 1) for item in image["Env"])
    assert image["Entrypoint"] == ["python", "first.py"]
    assert image["ExposedPorts"] == {"7860/tcp": {}}
    assert env["GRADIO_SERVER_NAME"] == "0.0.0.0"


def test_unselected_gradio_port_ambiguity_does_not_block_selected_script(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(tmp_path, "first.py", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n")
    write_file(
        tmp_path, "second.py", "import gradio as gr\ndemo = gr.Blocks()\n"
        "demo.launch(server_port=8123)\ndemo.launch(server_port=8124)\n",
    )
    config = BuildConfig(entrypoint=["python", "first.py"])
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == ["python", "first.py"]
    assert config.exposed_ports == [7860]


def test_unrelated_explicit_command_is_not_blocked_by_gradio_port(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(
        tmp_path, "demo.py",
        "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch(server_port=get_port())\n",
    )
    config = BuildConfig(cmd=["python", "manual.py"])
    apply_framework_defaults(config, tmp_path)
    assert config.cmd == ["python", "manual.py"]
    assert not config.has_exposed_ports()


@pytest.mark.parametrize("command", [
    ["python", "-m", "demo"],
    ["python", "-u", "-m", "demo", "api.py"],
])
def test_python_module_command_selects_gradio_over_fastapi(tmp_path, command):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(
        tmp_path, "demo.py",
        "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n",
    )
    config = BuildConfig(entrypoint=command)
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == command
    assert config.labels["framework"] == "gradio"
    assert config.exposed_ports == [7860]


def test_python_command_does_not_treat_script_arguments_as_candidates(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    for name, port in (("a.py", 8123), ("b.py", 8124)):
        write_file(
            tmp_path, name,
            f"import gradio as gr\ndemo = gr.Blocks()\ndemo.launch(server_port={port})\n",
        )
    config = BuildConfig(entrypoint=["python", "b.py", "a.py"])
    apply_framework_defaults(config, tmp_path)
    assert config.exposed_ports == [8124]


@pytest.mark.parametrize(
    ("requirements", "filename", "source", "arguments"),
    [
        ("", "flask_app.py", "from flask import Flask\napp = Flask(__name__)\n", ["flask", "run"]),
        ("", "manage.py", "import django\n", ["manage.py", "runserver"]),
        ("jupyterlab\n", "notes.ipynb", "{}", ["jupyter", "lab"]),
        ("celery\n", "worker.py", "from celery import Celery\napp = Celery()\n",
         ["celery", "-A", "worker:app", "worker"]),
    ],
)
def test_python_script_arguments_are_not_framework_commands(
    tmp_path, requirements, filename, source, arguments
):
    write_file(tmp_path, "requirements.txt", requirements)
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, filename, source)
    config = BuildConfig(entrypoint=["python", "manual.py", *arguments])
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "fastapi"
    assert config.exposed_ports == [8000]


def test_celery_arguments_do_not_select_an_unrelated_fastapi_command(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    command = ["celery", "-A", "worker:app", "worker", "uvicorn", "api:app"]
    config = BuildConfig(entrypoint=command)
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "celery"
    assert config.exposed_ports == []


def test_nonworker_celery_command_does_not_get_worker_defaults(tmp_path):
    write_file(tmp_path, "requirements.txt", "celery\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "worker.py", "from celery import Celery\napp = Celery()\n")
    command = ["celery", "-A", "worker:app", "beat"]
    config = BuildConfig(entrypoint=command)
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == command
    assert not config.has_exposed_ports()


@pytest.mark.parametrize("port_arguments", [["--server.port=9001"], ["--server.port", "9001"]])
def test_streamlit_options_before_script_select_streamlit(tmp_path, port_arguments):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "dashboard.py", "import streamlit\n")
    command = ["streamlit", "run", *port_arguments, "dashboard.py"]
    config = BuildConfig(entrypoint=command)
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "streamlit"


@pytest.mark.parametrize("options", [["--server.headless=true"], ["--server.headless", "true"]])
def test_other_streamlit_options_before_script_select_streamlit(tmp_path, options):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "dashboard.py", "import streamlit\n")
    config = BuildConfig(entrypoint=["streamlit", "run", *options, "dashboard.py"])
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "streamlit"


@pytest.mark.parametrize(
    ("requirements", "filename", "source", "command", "framework"),
    [
        ("", "flask_app.py", "from flask import Flask\napp = Flask(__name__)\n",
         ["python", "-m", "flask", "run"], "flask"),
        ("celery\n", "worker.py", "from celery import Celery\napp = Celery()\n",
         ["python", "-m", "celery", "-A", "worker:app", "worker"], "celery"),
        ("jupyterlab\n", "notes.ipynb", "{}",
         ["python", "-m", "jupyterlab"], "jupyter"),
        ("streamlit\n", "dashboard.py", "import streamlit\n",
         ["python", "-m", "streamlit", "run", "dashboard.py"], "streamlit"),
    ],
)
def test_python_module_selects_the_invoked_framework(
    tmp_path, requirements, filename, source, command, framework
):
    write_file(tmp_path, "requirements.txt", requirements)
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, filename, source)
    config = BuildConfig(entrypoint=command)
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == framework


@pytest.mark.parametrize(
    "command",
    [["django-admin", "runserver"], ["python", "-m", "django", "runserver"]],
)
def test_django_command_variants_select_django(tmp_path, command):
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "manage.py", "import django\n")
    config = BuildConfig(entrypoint=command)
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "django"


@pytest.mark.parametrize(
    ("requirements", "filename", "source", "command", "framework"),
    [
        ("streamlit\n", "dashboard.py", "import streamlit\n",
         ["streamlit", "run", "/workspace/dashboard.py"], "streamlit"),
        ("gradio\n", "demo.py", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n",
         ["python", "/workspace/demo.py"], "gradio"),
        ("streamlit\n", "dashboard.py", "import streamlit\n",
         ["py", "-m", "streamlit", "run", "dashboard.py"], "streamlit"),
        ("streamlit\n", "sub/dashboard.py", "import streamlit\n",
         ["env", "-C", "/workspace/sub", "streamlit", "run", "dashboard.py"], "streamlit"),
        ("gradio\n", "sub/demo.py", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n",
         ["env", "--chdir=sub", "python", "demo.py"], "gradio"),
    ],
)
def test_configured_script_paths_are_resolved_against_image_workdir(
    tmp_path, requirements, filename, source, command, framework
):
    write_file(tmp_path, "requirements.txt", requirements)
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, filename, source)
    config = BuildConfig(entrypoint=command, workdir="/workspace")
    apply_framework_defaults(config, tmp_path, include_paths=["."])
    assert config.labels["framework"] == framework


def test_absolute_script_outside_image_workdir_is_not_associated(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "demo.py", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n")
    config = BuildConfig(entrypoint=["python", "/other/demo.py"], workdir="/workspace")
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "fastapi"


def test_env_chdir_outside_image_workdir_is_not_associated(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "dashboard.py", "import streamlit\n")
    config = BuildConfig(
        entrypoint=["env", "-C", "/other", "streamlit", "run", "dashboard.py"],
        workdir="/workspace",
    )
    apply_framework_defaults(config, tmp_path)
    assert config.labels["framework"] == "fastapi"


def test_explicit_command_disambiguates_frameworks_in_one_script(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\ngradio\n")
    write_file(
        tmp_path, "app.py",
        "import streamlit\nimport gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n",
    )
    config = BuildConfig(entrypoint=["python", "app.py"])
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == ["python", "app.py"]
    assert config.labels["framework"] == "gradio"
    assert config.exposed_ports == [7860]


def test_multiple_gradio_listener_ports_require_explicit_ports(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(
        tmp_path, "app.py",
        "import gradio as gr\ndemo = gr.Blocks()\n"
        "demo.launch(server_port=8123)\ndemo.launch(server_port=8124)\n",
    )
    with pytest.raises(ValueError, match="exposed ports"):
        apply_framework_defaults(BuildConfig(entrypoint=["python", "app.py"]), tmp_path)
    config = BuildConfig(exposed_ports=[8124])
    apply_framework_defaults(config, tmp_path)
    assert config.exposed_ports == [8124]


def test_unknown_gradio_listener_port_requires_explicit_ports(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(
        tmp_path, "app.py",
        "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch(server_port=get_port())\n",
    )
    with pytest.raises(ValueError, match="exposed ports"):
        detect_framework(tmp_path)
    config = BuildConfig(exposed_ports=[8124])
    apply_framework_defaults(config, tmp_path)
    assert config.exposed_ports == [8124]


def test_mixed_default_and_literal_gradio_listeners_require_explicit_ports(tmp_path):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(
        tmp_path, "app.py",
        "import gradio as gr\ndemo = gr.Blocks()\n"
        "demo.launch(server_port=7860)\ndemo.launch()\n",
    )
    with pytest.raises(ValueError, match="exposed ports"):
        detect_framework(tmp_path)


@pytest.mark.parametrize(
    ("source", "command", "framework", "port"),
    [
        ("from flask import Flask\napp = Flask(__name__)\n", ["flask", "run"], "flask", 5000),
        ("import django\n", ["python", "manage.py", "runserver"], "django", 8000),
    ],
)
def test_configured_legacy_command_selects_its_framework(tmp_path, source, command, framework, port):
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "manage.py" if framework == "django" else "flask_app.py", source)
    config = BuildConfig(entrypoint=command)
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == command
    assert config.labels["framework"] == framework
    assert config.exposed_ports == [port]


def test_configured_jupyter_command_selects_jupyter_over_fastapi(tmp_path):
    write_file(tmp_path, "requirements.txt", "jupyterlab\n")
    write_file(tmp_path, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(tmp_path, "analysis.ipynb", "{}")
    config = BuildConfig(entrypoint=["jupyter", "lab"])
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == ["jupyter", "lab"]
    assert config.labels["framework"] == "jupyter"
    assert config.exposed_ports == [8888]


def test_configured_celery_module_selects_worker_over_fastapi(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "celery\n")
    write_file(context, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(context, "src/proj/worker.py", "from celery import Celery\napp = Celery()\n")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False,
        entrypoint=["celery", "-A", "proj.worker", "worker"],
    )
    image = image_config_from_build(tmp_path, monkeypatch, config)
    env = dict(item.split("=", 1) for item in image["Env"])
    assert image["Entrypoint"] == ["celery", "-A", "proj.worker", "worker"]
    assert image["ExposedPorts"] == {}
    assert env["PYTHONPATH"] == "/app/src"


@pytest.mark.parametrize(
    ("requirements", "source", "variable", "expected"),
    [
        ("streamlit", "import streamlit\n", "STREAMLIT_SERVER_PORT", 9100),
        ("gradio", "import gradio as gr\ndemo = gr.Blocks()\ndemo.launch()\n", "GRADIO_SERVER_PORT", 9200),
    ],
)
def test_framework_defaults_honor_inherited_listener_ports(
    tmp_path, monkeypatch, requirements, source, variable, expected
):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", requirements + "\n")
    write_file(context, "app.py", source)
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False
    )
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=[f"{variable}={expected}"]
    )
    assert image["ExposedPorts"] == {f"{expected}/tcp": {}}
    if requirements == "streamlit":
        assert f"--server.port={expected}" in image["Entrypoint"]


@pytest.mark.parametrize("explicit_entrypoint", [False, True])
def test_streamlit_inherited_listener_port_is_independent_of_port_metadata(
    tmp_path, monkeypatch, explicit_entrypoint
):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    kwargs = (
        {"entrypoint": ["streamlit", "run", "app.py"]}
        if explicit_entrypoint else {"exposed_ports": []}
    )
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"),
        use_cache=False, **kwargs,
    )
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["STREAMLIT_SERVER_PORT=9100"]
    )
    if explicit_entrypoint:
        assert image["Entrypoint"] == ["streamlit", "run", "app.py"]
        assert image["ExposedPorts"] == {"9100/tcp": {}}
    else:
        assert "--server.port=9100" in image["Entrypoint"]
        assert image["ExposedPorts"] == {}


@pytest.mark.parametrize("port_arguments", [["--server.port=9200"], ["--server.port", "9200"]])
def test_streamlit_command_port_takes_precedence_over_inherited_listener(
    tmp_path, monkeypatch, port_arguments
):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    command = ["streamlit", "run", "app.py", *port_arguments]
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"),
        use_cache=False, entrypoint=command,
    )
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["STREAMLIT_SERVER_PORT=9100"]
    )
    assert image["Entrypoint"] == command
    assert image["ExposedPorts"] == {"9200/tcp": {}}


def test_streamlit_explicit_listener_port_ignores_invalid_inherited_port(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"),
        use_cache=False, exposed_ports=[9200],
    )
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["STREAMLIT_SERVER_PORT=invalid"]
    )
    assert "--server.port=9200" in image["Entrypoint"]
    assert image["ExposedPorts"] == {"9200/tcp": {}}


def test_streamlit_env_wrapper_delimiter_does_not_hide_listener_port(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    command = ["env", "--", "streamlit", "run", "app.py", "--server.port=9200"]
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"),
        use_cache=False, entrypoint=command,
    )
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["STREAMLIT_SERVER_PORT=9100"]
    )
    assert image["Entrypoint"] == command
    assert image["ExposedPorts"] == {"9200/tcp": {}}


def test_streamlit_script_arguments_do_not_override_listener_port(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "app.py", "import streamlit\n")
    command = ["streamlit", "run", "app.py", "--", "--server.port=9999"]
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"),
        use_cache=False, entrypoint=command,
    )
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["STREAMLIT_SERVER_PORT=9100"]
    )
    assert image["Entrypoint"] == command
    assert image["ExposedPorts"] == {"9100/tcp": {}}


def test_streamlit_listener_option_before_script_is_exposed(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "streamlit\n")
    write_file(context, "api.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write_file(context, "app.py", "import streamlit\n")
    command = ["streamlit", "run", "--server.port=9001", "app.py"]
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"),
        use_cache=False, entrypoint=command,
    )
    image = image_config_from_build(
        tmp_path, monkeypatch, config, base_env=["STREAMLIT_SERVER_PORT=9100"]
    )
    assert image["Entrypoint"] == command
    assert image["ExposedPorts"] == {"9001/tcp": {}}


@pytest.mark.parametrize(
    ("env", "ports", "expected"),
    [
        ({"STREAMLIT_SERVER_PORT": "9000"}, None, 9000),
        ({}, [9001], 9001),
    ],
)
def test_streamlit_generated_command_honors_configured_port(tmp_path, env, ports, expected):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    config = BuildConfig(env=env, **({"exposed_ports": ports} if ports is not None else {}))
    apply_framework_defaults(config, tmp_path)
    assert f"--server.port={expected}" in config.entrypoint
    assert config.exposed_ports == [expected]


def test_streamlit_rejects_conflicting_user_configured_ports(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    config = BuildConfig(env={"STREAMLIT_SERVER_PORT": "9000"}, exposed_ports=[9001])
    with pytest.raises(ValueError, match="conflicts"):
        apply_framework_defaults(config, tmp_path)
    assert config.env["STREAMLIT_SERVER_PORT"] == "9000"
    assert config.exposed_ports == [9001]


def test_streamlit_does_not_change_explicit_command_or_ports_on_conflict(tmp_path):
    write_file(tmp_path, "requirements.txt", "streamlit\n")
    write_file(tmp_path, "app.py", "import streamlit\n")
    config = BuildConfig(
        entrypoint=["custom"], env={"STREAMLIT_SERVER_PORT": "9000"},
        exposed_ports=[9001],
    )
    apply_framework_defaults(config, tmp_path)
    assert config.entrypoint == ["custom"]
    assert config.env["STREAMLIT_SERVER_PORT"] == "9000"
    assert config.exposed_ports == [9001]


@pytest.mark.parametrize(
    ("source", "env", "expected"),
    [
        ("demo.launch(server_port=8123)\n", {}, 8123),
        ("demo.launch()\n", {"GRADIO_SERVER_PORT": "9876"}, 9876),
    ],
)
def test_gradio_configured_listener_port_is_exposed(tmp_path, source, env, expected):
    write_file(tmp_path, "requirements.txt", "gradio\n")
    write_file(tmp_path, "demo.py", "import gradio as gr\ndemo = gr.Blocks()\n" + source)
    config = apply_framework_defaults(BuildConfig(env=env), tmp_path)
    assert config.exposed_ports == [expected]


def test_generated_celery_image_does_not_inherit_base_command_or_ports(tmp_path, monkeypatch):
    context = tmp_path / "context"
    write_file(context, "requirements.txt", "celery\n")
    write_file(context, "worker.py", "from celery import Celery\napp = Celery('x')\n")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "image"), use_cache=False
    )
    image = image_config_from_build(tmp_path, monkeypatch, config)
    assert image["Entrypoint"] == ["celery", "-A", "worker:app", "worker", "--loglevel=INFO"]
    assert "Cmd" not in image
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
    assert "Entrypoint" not in image
    assert image["Cmd"] == ["python3"]
    assert image["ExposedPorts"] == {}
