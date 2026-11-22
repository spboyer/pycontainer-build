"""Skaffold and Tilt custom-builder contract tests.

The fake tool runners below reproduce how each tool launches a custom build
command (environment, working directory, shell or argv form, output file
handling) and run the commands shipped in ``examples/``. A loopback registry
is the push boundary; a ``sitecustomize`` shim replaces only the base-image
pull and points the registry client at plain HTTP.
"""

import ast
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from pycontainer import cli
from pycontainer.devloop import (
    DevLoopError,
    TILT_PLACEHOLDER_TAG,
    single_platform,
    skaffold_request,
    tilt_content_tag,
    tilt_expected_reference,
    write_reference,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"
SUBPROCESS_TIMEOUT_SECONDS = 120
DIGEST = "sha256:" + "a" * 64

SITECUSTOMIZE = textwrap.dedent(
    '''
    import os

    if os.environ.get("PYCONTAINER_TEST_REGISTRY"):
        from pycontainer import builder, registry_client

        def _synthetic_base(self, *_args):
            return [], {"architecture": "amd64", "os": "linux",
                        "config": {"Env": [], "WorkingDir": "/"}}

        builder.ImageBuilder._pull_base_image = _synthetic_base
        _original_init = registry_client.RegistryClient.__init__

        def _loopback_init(self, registry, *args, **kwargs):
            _original_init(self, registry, *args, **kwargs)
            if registry == os.environ["PYCONTAINER_TEST_REGISTRY"]:
                self.base_url = f"http://{registry}/v2"

        registry_client.RegistryClient.__init__ = _loopback_init
    '''
)


class FakeRegistry:
    """Minimal Registry v2 push endpoint that records manifests and requests."""

    def __init__(self, root: Path, fail_manifests: bool = False):
        self.blobs = {}
        self.manifests = {}
        self.requests = []
        registry = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _reply(self, status, headers=None, body=b""):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", "0")))

            def do_HEAD(self):
                registry.requests.append(("HEAD", self.path))
                digest = urlsplit(self.path).path.rsplit("/", 1)[-1]
                self._reply(200 if digest in registry.blobs else 404)

            def do_POST(self):
                registry.requests.append(("POST", self.path))
                self._reply(202, {"Location": f"/v2/uploads/{uuid.uuid4().hex}"})

            def do_PUT(self):
                parsed = urlsplit(self.path)
                registry.requests.append(("PUT", parsed.path))
                body = self._body()
                if "/manifests/" in parsed.path:
                    if fail_manifests:
                        self._reply(500, body=b"manifest rejected")
                        return
                    repo, tag = re.match(r"/v2/(.+)/manifests/(.+)", parsed.path).groups()
                    registry.manifests[(repo, tag)] = body
                    self._reply(201)
                    return
                digest = parse_qs(parsed.query)["digest"][0]
                if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
                    self._reply(400)
                    return
                registry.blobs[digest] = body
                self._reply(201)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def project(tmp_path):
    """Return a factory that copies an example into an isolated workspace."""

    def copy(name):
        destination = tmp_path / name
        shutil.copytree(EXAMPLES / name, destination)
        return destination

    return copy


@pytest.fixture
def tool_env(tmp_path):
    """Environment for the launched command: isolated home, a docker trap."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    trap = tmp_path / "bin"
    trap.mkdir()
    docker_log = tmp_path / "docker-invoked"
    (trap / "docker").write_text(f"#!/bin/sh\necho called > '{docker_log}'\nexit 97\n")
    (trap / "docker").chmod(0o755)
    (trap / "docker.bat").write_text(f"@echo called > \"{docker_log}\"\r\n@exit /b 97\r\n")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("REGISTRY_", "DOCKER_")) and key not in ("GITHUB_TOKEN",)
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONPATH=os.pathsep.join([str(shim), str(REPO_ROOT / "src")]),
        PATH=os.pathsep.join([str(trap), str(Path(sys.executable).parent), env.get("PATH", "")]),
    )
    env["_DOCKER_LOG"] = str(docker_log)
    return env


def skaffold_build_command(project_dir: Path) -> str:
    match = re.search(r"^\s*buildCommand:\s*(.+)$", (project_dir / "skaffold.yaml").read_text(), re.M)
    assert match, "skaffold.yaml must declare a custom buildCommand"
    return match.group(1).strip()


def tilt_command(project_dir: Path) -> list:
    match = re.search(r"^command = (\[.*\])$", (project_dir / "Tiltfile").read_text(), re.M)
    assert match, "Tiltfile must declare the custom_build command as an argv list"
    return ast.literal_eval(match.group(1))


def run_skaffold(project_dir, env, *, image, push="true", platforms=""):
    env = dict(env, PYCONTAINER_TEST_REGISTRY=image.split("/", 1)[0])
    """Launch a buildCommand the way Skaffold's custom builder does."""
    command = skaffold_build_command(project_dir)
    argv = ["cmd.exe", "/C", command] if os.name == "nt" else ["sh", "-c", command]
    repo, _, tag = image.rpartition(":")
    tool_env = dict(env)
    tool_env.update(
        IMAGE=image,
        PUSH_IMAGE=push,
        BUILD_CONTEXT=str(project_dir),
        PLATFORMS=platforms,
        SKIP_TEST="false",
        IMAGE_REPO=repo,
        IMAGE_TAG=tag,
    )
    return subprocess.run(
        argv, cwd=project_dir, env=tool_env, capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def run_tilt(project_dir, env, argv, *, expected_ref=None, registry=None, output_ref=None):
    """Launch a custom_build argv command the way Tilt does (skips_local_docker)."""
    tool_env = dict(env)
    target = expected_ref or (argv[argv.index("--image") + 1] if "--image" in argv else "")
    tool_env["PYCONTAINER_TEST_REGISTRY"] = target.split("/", 1)[0]
    if output_ref is not None:
        Path(output_ref).unlink(missing_ok=True)
    elif expected_ref is not None:
        tool_env["EXPECTED_REF"] = expected_ref
    if registry:
        tool_env.update(REGISTRY_HOST=registry, EXPECTED_REGISTRY=registry)
    return subprocess.run(
        argv, cwd=project_dir, env=tool_env, capture_output=True, text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def read_tilt_output_ref(path: Path) -> str:
    """Mirror Tilt: the file must hold one named, tagged reference."""
    reference = path.read_text(encoding="utf-8").strip()
    assert "\n" not in reference and "@" not in reference
    assert re.fullmatch(r"[^/\s]+/[^:\s]+:[\w][\w.-]{0,127}", reference), reference
    return reference


def assert_pushed(registry, reference, env):
    repo, tag = reference.split("/", 1)[1].rsplit(":", 1)
    manifest = registry.manifests[(repo, tag)]
    layers = json.loads(manifest)["layers"]
    assert layers and all(layer["digest"] in registry.blobs for layer in layers)
    assert not Path(env["_DOCKER_LOG"]).exists(), "Docker must never be invoked"
    return manifest


# --- Skaffold contract ------------------------------------------------------


def test_skaffold_example_pushes_exact_image(project, tool_env):
    workspace = project("skaffold")
    with FakeRegistry(workspace) as registry:
        image = f"{registry.host}/pycontainer-demo:abc123"
        result = run_skaffold(workspace, tool_env, image=image, platforms="linux/amd64")
        assert result.returncode == 0, result.stdout + result.stderr
        assert_pushed(registry, image, tool_env)
        assert set(registry.manifests) == {("pycontainer-demo", "abc123")}


def test_skaffold_rejects_local_daemon_handoff_before_building(project, tool_env):
    workspace = project("skaffold")
    with FakeRegistry(workspace) as registry:
        result = run_skaffold(workspace, tool_env, image=f"{registry.host}/demo:x", push="false")
        assert result.returncode == 2
        assert "PUSH_IMAGE=false" in result.stderr and "build.local.push: true" in result.stderr
        assert registry.requests == []
        assert not (workspace / "dist").exists()


def test_skaffold_rejects_multiple_platforms(project, tool_env):
    workspace = project("skaffold")
    with FakeRegistry(workspace) as registry:
        result = run_skaffold(
            workspace, tool_env, image=f"{registry.host}/demo:x",
            platforms="linux/amd64,linux/arm64",
        )
        assert result.returncode == 2
        assert "issues/29" in result.stderr
        assert registry.requests == []


def test_skaffold_push_failure_propagates(project, tool_env):
    workspace = project("skaffold")
    with FakeRegistry(workspace, fail_manifests=True) as registry:
        result = run_skaffold(workspace, tool_env, image=f"{registry.host}/demo:x")
        assert result.returncode != 0
        assert "Manifest push failed with HTTP 500" in result.stderr


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"BUILD_CONTEXT": "{ctx}", "PUSH_IMAGE": "true"}, "IMAGE is not set"),
        ({"IMAGE": "r.io/a:1", "PUSH_IMAGE": "true"}, "BUILD_CONTEXT is not set"),
        ({"IMAGE": "r.io/a:1", "BUILD_CONTEXT": "relative", "PUSH_IMAGE": "true"}, "absolute path"),
        ({"IMAGE": "r.io/a:1", "BUILD_CONTEXT": "{ctx}"}, "PUSH_IMAGE is not set"),
        ({"IMAGE": "r.io/a:1", "BUILD_CONTEXT": "{ctx}", "PUSH_IMAGE": "yes"}, "'true' or 'false'"),
        ({"IMAGE": "app:1", "BUILD_CONTEXT": "{ctx}", "PUSH_IMAGE": "true"}, "registry explicitly"),
        ({"IMAGE": "r.io/a", "BUILD_CONTEXT": "{ctx}", "PUSH_IMAGE": "true"}, "tagged image reference"),
        ({"IMAGE": f"r.io/a@{DIGEST}", "BUILD_CONTEXT": "{ctx}", "PUSH_IMAGE": "true"}, "without a digest"),
        ({"IMAGE": "r.io/a:1", "BUILD_CONTEXT": "{ctx}", "PUSH_IMAGE": "true", "PLATFORMS": "linux"}, "not a valid platform"),
    ],
)
def test_skaffold_request_rejects_invalid_environment(tmp_path, env, message):
    env = {key: value.format(ctx=tmp_path) for key, value in env.items()}
    with pytest.raises(DevLoopError, match=re.escape(message)):
        skaffold_request(env)


def test_skaffold_request_accepts_single_and_duplicate_platform(tmp_path):
    request = skaffold_request(
        {"IMAGE": "localhost:5000/a:1", "BUILD_CONTEXT": str(tmp_path),
         "PUSH_IMAGE": "true", "PLATFORMS": "linux/arm64, linux/arm64"}
    )
    assert request.platform == "linux/arm64"
    assert request.image == "localhost:5000/a:1"


# --- Tilt contract ----------------------------------------------------------


def test_tilt_example_pushes_expected_ref(project, tool_env):
    workspace = project("tilt")
    with FakeRegistry(workspace) as registry:
        expected = f"{registry.host}/pycontainer-demo:tilt-build-1700000000"
        result = run_tilt(
            workspace, tool_env, tilt_command(workspace),
            expected_ref=expected, registry=registry.host,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert_pushed(registry, expected, tool_env)
        assert set(registry.manifests) == {("pycontainer-demo", "tilt-build-1700000000")}


def test_tilt_example_output_ref_mode_reports_content_tag(project, tool_env):
    workspace = project("tilt")
    tiltfile = (workspace / "Tiltfile").read_text()
    assert "outputs_image_ref_to=ref_file" in tiltfile and "skips_local_docker=True" in tiltfile
    with FakeRegistry(workspace) as registry:
        ref_file = workspace / "dist" / "tilt-image-ref.txt"
        ref_file.parent.mkdir()
        ref_file.write_text("stale-from-previous-run\n")
        argv = tilt_command(workspace) + [
            "--image", f"{registry.host}/pycontainer-demo", "--output-ref", str(ref_file),
        ]
        result = run_tilt(workspace, tool_env, argv, output_ref=ref_file)
        assert result.returncode == 0, result.stdout + result.stderr
        reference = read_tilt_output_ref(ref_file)
        manifest = assert_pushed(registry, reference, tool_env)
        assert reference.endswith(":tilt-" + hashlib.sha256(manifest).hexdigest())
        assert TILT_PLACEHOLDER_TAG not in reference

        rerun = run_tilt(workspace, tool_env, argv, output_ref=ref_file)
        assert rerun.returncode == 0, rerun.stderr
        assert read_tilt_output_ref(ref_file) == reference


def test_tilt_push_failure_leaves_no_output_ref(project, tool_env):
    workspace = project("tilt")
    with FakeRegistry(workspace, fail_manifests=True) as registry:
        ref_file = workspace / "out" / "ref.txt"
        argv = tilt_command(workspace) + [
            "--image", f"{registry.host}/pycontainer-demo", "--output-ref", str(ref_file),
        ]
        result = run_tilt(workspace, tool_env, argv, output_ref=ref_file)
        assert result.returncode != 0
        assert not ref_file.exists()


def test_tilt_rejects_registryless_tag_mode_ref(project, tool_env):
    workspace = project("tilt")
    result = run_tilt(workspace, tool_env, tilt_command(workspace), expected_ref="pycontainer-demo:dev")
    assert result.returncode == 2
    assert "custom_build(tag=...)" in result.stderr
    assert not (workspace / "dist").exists()


@pytest.mark.parametrize(
    ("env", "image", "message"),
    [
        ({}, None, "EXPECTED_REF is not set"),
        ({"EXPECTED_REF": "r.io/a:1"}, "r.io/a", "cannot be combined"),
        ({"EXPECTED_REF": "r.io/a"}, None, "tagged image reference"),
        ({}, "app", "registry explicitly"),
        ({}, "r.io/a:1", "without a tag or digest"),
    ],
)
def test_tilt_expected_reference_rejects_invalid_requests(env, image, message):
    with pytest.raises(DevLoopError, match=re.escape(message)):
        tilt_expected_reference(env, image)


def test_tilt_expected_reference_requalifies_docker_hub_familiar_ref():
    env = {"EXPECTED_REF": "myuser/app:tilt-build-1", "EXPECTED_REGISTRY": "docker.io/myuser"}
    assert tilt_expected_reference(env, None) == "docker.io/myuser/app:tilt-build-1"
    with pytest.raises(DevLoopError, match="registry explicitly"):
        tilt_expected_reference({"EXPECTED_REF": "myuser/app:1"}, None)


def test_tilt_content_tag_uses_full_manifest_digest():
    assert tilt_content_tag(DIGEST) == "tilt-" + "a" * 64
    with pytest.raises(DevLoopError):
        tilt_content_tag("sha256:abc")


def test_single_platform_rejects_multiple_after_dedupe():
    assert single_platform("linux/amd64,linux/amd64", "p") == "linux/amd64"
    assert single_platform("", "p") is None
    with pytest.raises(DevLoopError, match="issues/29"):
        single_platform("linux/amd64,linux/arm64", "p")


def test_write_reference_replaces_atomically(tmp_path):
    target = tmp_path / "nested" / "ref.txt"
    write_reference(target, "r.io/a:1")
    write_reference(target, "r.io/a:2")
    assert target.read_bytes() == b"r.io/a:2\n"
    assert list(target.parent.iterdir()) == [target]


# --- CLI wiring (in-process) -------------------------------------------------


class RecordingBuilder:
    instances = []

    def __init__(self, config):
        self.config = config
        self.manifest_digest = DIGEST
        self.pushes = []
        RecordingBuilder.instances.append(self)

    def build(self):
        return "dist/image"

    def push(self, registry_url=None, **kwargs):
        self.pushes.append((registry_url, kwargs))
        return registry_url


@pytest.fixture
def recording(monkeypatch):
    RecordingBuilder.instances = []
    monkeypatch.setattr(cli, "ImageBuilder", RecordingBuilder)
    for key in ("IMAGE", "BUILD_CONTEXT", "PUSH_IMAGE", "PLATFORMS", "EXPECTED_REF"):
        monkeypatch.delenv(key, raising=False)
    return RecordingBuilder.instances


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["pycontainer", *argv])
    cli.main()


def test_skaffold_cli_ignores_config_tags_and_dry_run(tmp_path, monkeypatch, recording):
    (tmp_path / "pycontainer.toml").write_text(
        '[build]\ntag = "other:1"\ntags = ["extra"]\nlatest_tag = true\ndry_run = true\nplatform = "linux/arm64"\n'
    )
    monkeypatch.setenv("IMAGE", "localhost:5000/a:1")
    monkeypatch.setenv("BUILD_CONTEXT", str(tmp_path))
    monkeypatch.setenv("PUSH_IMAGE", "true")
    monkeypatch.setenv("PLATFORMS", "linux/amd64")
    run_cli(monkeypatch, "skaffold-build", "--config", str(tmp_path / "pycontainer.toml"), "--no-progress")
    (builder,) = recording
    assert builder.config.all_tags() == ["localhost:5000/a:1"]
    assert builder.config.dry_run is False
    assert builder.config.platform == "linux/amd64"
    assert Path(builder.config.context_dir) == tmp_path
    assert builder.pushes[0][0] == "localhost:5000/a:1"
    assert builder.pushes[0][1]["username"] is None


def test_devloop_cli_rejects_multi_platform_from_config(tmp_path, monkeypatch, recording):
    (tmp_path / "pycontainer.toml").write_text('[build]\nplatform = "linux/amd64,linux/arm64"\n')
    monkeypatch.setenv("EXPECTED_REF", "localhost:5000/a:1")
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, "tilt-build", "--config", str(tmp_path / "pycontainer.toml"), "--context", str(tmp_path))
    assert exit_info.value.code == 2
    assert recording == []


def test_tilt_cli_image_requires_output_ref(tmp_path, monkeypatch, recording):
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, "tilt-build", "--image", "localhost:5000/a", "--context", str(tmp_path))
    assert exit_info.value.code == 2
    assert recording == []


def test_devloop_commands_do_not_accept_credentials_or_tags(monkeypatch, recording):
    for argv in (["skaffold-build", "--password", "x"], ["tilt-build", "--tag", "x:1"]):
        with pytest.raises(SystemExit) as exit_info:
            run_cli(monkeypatch, *argv)
        assert exit_info.value.code == 2
    assert recording == []
