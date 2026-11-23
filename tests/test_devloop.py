"""Skaffold and Tilt custom-builder contract tests.

The fake tool runners below reproduce how each tool launches a custom build
command (environment, working directory, shell or argv form, output file
handling) and run the commands shipped in ``examples/``. A loopback registry
is the push boundary, explicitly opted into HTTP; a ``sitecustomize`` shim
replaces only the base-image pull.
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
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import trustme

from scripts import check_devloop_tools as real_check
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
        from pycontainer import builder

        def _synthetic_base(self, *_args):
            return [], {"architecture": "amd64", "os": "linux",
                        "config": {"Env": [], "WorkingDir": "/"}}

        builder.ImageBuilder._pull_base_image = _synthetic_base
    '''
)


class FakeRegistry:
    """Minimal Registry v2 push endpoint that records manifests and requests."""

    def __init__(self, root: Path, fail_manifests: bool = False, fail_auth: bool = False):
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
                self._reply(401 if fail_auth else 200 if digest in registry.blobs else 404)

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
    """Launch a buildCommand the way Skaffold's custom builder does."""
    registry = image.split("/", 1)[0]
    env = dict(env, PYCONTAINER_TEST_REGISTRY=registry)
    command = f"{skaffold_build_command(project_dir)} --insecure-registry {registry}"
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
    if tool_env["PYCONTAINER_TEST_REGISTRY"].startswith("127.0.0.1:"):
        argv = [*argv, "--insecure-registry", tool_env["PYCONTAINER_TEST_REGISTRY"]]
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


@pytest.mark.parametrize("tool", ["skaffold", "tilt"])
def test_devloop_auth_failure_is_propagated_without_credentials(project, tool_env, tool):
    workspace = project(tool)
    env = dict(tool_env, REGISTRY_USERNAME="test-user", REGISTRY_PASSWORD="test-secret")
    with FakeRegistry(workspace, fail_auth=True) as registry:
        ref_file = workspace / "dist" / "tilt-image-ref.txt"
        if tool == "skaffold":
            result = run_skaffold(workspace, env, image=f"{registry.host}/demo:x")
        else:
            argv = tilt_command(workspace) + [
                "--image", f"{registry.host}/demo", "--output-ref", str(ref_file),
            ]
            result = run_tilt(workspace, env, argv, output_ref=ref_file)
        assert result.returncode != 0
        assert "HTTP 401" in result.stderr
        assert "test-secret" not in result.stdout + result.stderr
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


@pytest.mark.parametrize("command", ["skaffold-build", "tilt-build"])
def test_devloop_build_arguments_preserve_tool_reference(
    tmp_path, monkeypatch, recording, capsys, command,
):
    config = tmp_path / "pycontainer.toml"
    config.write_text(
        '[build]\nlabels = {version = "${VERSION}"}\nplatform = "${PLATFORM}"\n'
        '[build.build-args]\nVERSION = "default"\nPLATFORM = "linux/amd64"\n'
    )
    reference = "localhost:5000/a:1"
    if command == "skaffold-build":
        monkeypatch.setenv("IMAGE", reference)
        monkeypatch.setenv("BUILD_CONTEXT", str(tmp_path))
        monkeypatch.setenv("PUSH_IMAGE", "true")
        extra = []
    else:
        monkeypatch.setenv("EXPECTED_REF", reference)
        extra = ["--context", str(tmp_path), "--platform", "${PLATFORM}"]

    run_cli(
        monkeypatch, command, "--config", str(config), "--no-progress",
        "--build-arg", "VERSION=private-v2", "--build-arg", "PLATFORM=linux/arm64",
        *extra,
    )
    (builder,) = recording
    assert builder.config.labels == {"version": "private-v2"}
    assert builder.config.env == {}
    assert builder.config.platform == "linux/arm64"
    assert builder.pushes[0][0] == reference
    assert "private-v2" not in capsys.readouterr().out


def test_tilt_output_reference_path_redacts_build_arguments(tmp_path, monkeypatch, recording, capsys):
    config = tmp_path / "pycontainer.toml"
    config.write_text('[build.build-args]\nVALUE = "private-ref"\n')
    output = tmp_path / "private-ref.txt"
    run_cli(
        monkeypatch, "tilt-build", "--config", str(config),
        "--image", "localhost:5000/a", "--output-ref", str(output),
        "--context", str(tmp_path),
    )

    assert output.read_text().strip() == "localhost:5000/a:tilt-" + "a" * 64
    assert "private-ref" not in capsys.readouterr().out
    assert recording[0].pushes[0][0] == output.read_text().strip()


def test_devloop_redacts_invalid_interpolated_platform(tmp_path, monkeypatch, recording, capsys):
    config = tmp_path / "pycontainer.toml"
    config.write_text('[build]\nplatform = "${PLATFORM}"\n[build.build-args]\nPLATFORM = {}\n')
    monkeypatch.setenv("EXPECTED_REF", "localhost:5000/a:1")
    value = "linux/amd64, linux/arm64"
    with pytest.raises(SystemExit) as error:
        run_cli(
            monkeypatch, "tilt-build", "--config", str(config),
            "--build-arg", f"PLATFORM={value}",
        )

    assert error.value.code == 2
    diagnostic = capsys.readouterr().err
    assert "multiple platforms" in diagnostic
    assert value not in diagnostic
    assert "<redacted>" in diagnostic
    assert recording == []


@pytest.mark.parametrize("override", [False, True])
def test_skaffold_request_errors_redact_build_arguments(tmp_path, monkeypatch, recording, capsys, override):
    config = tmp_path / "pycontainer.toml"
    default = "fallback" if override else "invalid-private"
    config.write_text(f'[build.build-args]\nVALUE = "{default}"\n')
    monkeypatch.setenv("IMAGE", "localhost:5000/a:1")
    monkeypatch.setenv("BUILD_CONTEXT", str(tmp_path))
    monkeypatch.setenv("PUSH_IMAGE", "invalid-private")
    extra = ["--build-arg", "VALUE=invalid-private"] if override else []
    with pytest.raises(SystemExit) as error:
        run_cli(monkeypatch, "skaffold-build", "--config", str(config), *extra)

    assert error.value.code == 2
    diagnostic = capsys.readouterr().err
    assert "PUSH_IMAGE must" in diagnostic
    assert "invalid-private" not in diagnostic
    assert "<redacted>" in diagnostic
    assert recording == []


@pytest.mark.parametrize("command", ["skaffold-build", "tilt-build"])
def test_devloop_cli_honors_registry_transport_options(
    tmp_path, monkeypatch, recording, command
):
    ca_file = tmp_path / "ca.pem"
    trustme.CA().cert_pem.write_to_path(ca_file)
    if command == "skaffold-build":
        monkeypatch.setenv("IMAGE", "localhost:5000/a:1")
        monkeypatch.setenv("BUILD_CONTEXT", str(tmp_path))
        monkeypatch.setenv("PUSH_IMAGE", "true")
    else:
        monkeypatch.setenv("EXPECTED_REF", "localhost:5000/a:1")

    run_cli(
        monkeypatch, command, "--base-image", "python:3.11-slim",
        "--insecure-registry", "localhost:5000", "--registry-ca-file", str(ca_file),
    )

    (builder,) = recording
    assert builder.config.insecure_registries == ["localhost:5000"]
    assert builder.config.registry_ca_file == str(ca_file)
    assert builder.pushes[0][0] == "localhost:5000/a:1"


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


@pytest.mark.parametrize("command", ["skaffold-build", "tilt-build"])
def test_devloop_build_failure_stops_before_push(tmp_path, monkeypatch, recording, command):
    def fail_build(_self):
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr(RecordingBuilder, "build", fail_build)
    if command == "skaffold-build":
        monkeypatch.setenv("IMAGE", "localhost:5000/a:1")
        monkeypatch.setenv("BUILD_CONTEXT", str(tmp_path))
        monkeypatch.setenv("PUSH_IMAGE", "true")
        argv = [command]
    else:
        argv = [command, "--image", "localhost:5000/a", "--output-ref", str(tmp_path / "ref")]

    with pytest.raises(RuntimeError, match="synthetic build failure"):
        run_cli(monkeypatch, *argv)
    assert recording[0].pushes == []
    assert not (tmp_path / "ref").exists()


@pytest.mark.parametrize("command", ["skaffold-build", "tilt-build"])
@pytest.mark.parametrize(
    ("configured", "expected"),
    [("linux/amd64, linux/amd64", "linux/amd64"), ("  ", None)],
)
def test_devloop_normalizes_or_rejects_configured_platform(
    tmp_path, monkeypatch, recording, command, configured, expected
):
    config = tmp_path / "pycontainer.toml"
    config.write_text(f'[build]\nplatform = "{configured}"\n')
    if command == "skaffold-build":
        monkeypatch.setenv("IMAGE", "localhost:5000/a:1")
        monkeypatch.setenv("BUILD_CONTEXT", str(tmp_path))
        monkeypatch.setenv("PUSH_IMAGE", "true")
    else:
        monkeypatch.setenv("EXPECTED_REF", "localhost:5000/a:1")

    if expected is None:
        with pytest.raises(SystemExit) as error:
            run_cli(monkeypatch, command, "--config", str(config))
        assert error.value.code == 2
        assert recording == []
    else:
        run_cli(monkeypatch, command, "--config", str(config))
        assert recording[0].config.platform == expected


def test_tilt_example_disables_background_docker_pruning():
    tiltfile = (EXAMPLES / "tilt" / "Tiltfile").read_text()
    assert re.search(r"^docker_prune_settings\(disable\s*=\s*True\)", tiltfile, re.M)


@pytest.mark.parametrize(
    ("deployed", "matches"),
    [
        ("registry.test/team/pycontainer-demo:built", True),
        ("registry.test/team/pycontainer-demo:built@" + DIGEST, True),
        ("registry.test/team/pycontainer-demo-other:built", False),
        ("registry.test/team/pycontainer-demo:different", False),
        ("registry.test/team/pycontainer-demo:built@sha256:" + "b" * 64, False),
    ],
)
def test_real_check_compares_exact_deployed_reference(
    tmp_path, monkeypatch, deployed, matches
):
    monkeypatch.setattr(real_check, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        real_check, "deployed_image", lambda *args, **kwargs: deployed
    )
    kwargs = dict(
        cwd=tmp_path,
        registry="registry.test/team",
        context="test-context",
        namespace="test-namespace",
        reference="registry.test/team/pycontainer-demo:built",
        digest=DIGEST,
    )
    if matches:
        real_check.verify_rollout(**kwargs)
    else:
        with pytest.raises(SystemExit, match="reference|digest"):
            real_check.verify_rollout(**kwargs)


def test_real_check_accepts_docker_hub_familiar_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(real_check, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        real_check, "deployed_image", lambda *args, **kwargs: "team/pycontainer-demo:built"
    )
    real_check.verify_rollout(
        tmp_path, "docker.io/team", "test-context", "test-namespace",
        "docker.io/team/pycontainer-demo:built", DIGEST,
    )


def test_real_check_pins_rollout_and_deployment_to_context_and_namespace(tmp_path, monkeypatch):
    calls = []

    def fake_subprocess_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0, stdout="registry.test/team/pycontainer-demo:built"
        )

    monkeypatch.setattr(real_check.subprocess, "run", fake_subprocess_run)
    real_check.verify_rollout(
        tmp_path, "registry.test/team", "test-context", "test-namespace",
        "registry.test/team/pycontainer-demo:built", DIGEST,
    )
    assert len(calls) == 2
    for command in calls:
        assert command[command.index("--context") + 1] == "test-context"
        assert command[command.index("--namespace") + 1] == "test-namespace"


def test_real_check_rejects_build_outside_requested_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(real_check, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        real_check, "deployed_image", lambda *args, **kwargs: "other.test/team/pycontainer-demo:built"
    )
    with pytest.raises(SystemExit, match="reference"):
        real_check.verify_rollout(
            tmp_path, "registry.test/team", "test-context", "test-namespace",
            "other.test/team/pycontainer-demo:built", DIGEST,
        )


def test_real_check_rejects_wrong_content_tag(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, cwd, env=None):
        calls.append(argv)
        if argv[:2] == ["tilt", "ci"]:
            layout = cwd / "dist" / "image"
            layout.mkdir(parents=True)
            (layout / "index.json").write_text(json.dumps({
                "annotations": {
                    "org.opencontainers.image.ref.name":
                    "registry.test/team/pycontainer-demo:tilt-pending"
                },
                "manifests": [{"digest": DIGEST}],
            }))
            (cwd / "dist" / "tilt-image-ref.txt").write_text(
                "registry.test/team/pycontainer-demo:tilt-" + "b" * 64
            )

    monkeypatch.setattr(real_check, "run", fake_run)
    with pytest.raises(SystemExit, match="content tag|digest"):
        real_check.check_tilt(
            tmp_path, "registry.test/team", "output-ref", "test-context", "test-namespace"
        )
    for action in ("ci", "down"):
        command = next(argv for argv in calls if argv[:2] == ["tilt", action])
        assert command[command.index("--context") + 1] == "test-context"
        assert command[command.index("--namespace") + 1] == "test-namespace"


def test_real_skaffold_check_uses_unique_tag_and_scoped_commands(tmp_path, monkeypatch):
    calls = []
    checked = []
    reference = "registry.test/team/pycontainer-demo:test-namespace"

    def fake_run(argv, cwd, env=None):
        calls.append(argv)
        if argv[:2] == ["skaffold", "run"]:
            layout = cwd / "dist" / "image"
            layout.mkdir(parents=True)
            (layout / "index.json").write_text(json.dumps({
                "annotations": {"org.opencontainers.image.ref.name": reference},
                "manifests": [{"digest": DIGEST}],
            }))

    monkeypatch.setattr(real_check, "run", fake_run)
    monkeypatch.setattr(real_check, "verify_rollout", lambda *args: checked.append(args))
    real_check.check_skaffold(
        tmp_path, "registry.test/team", False, "test-context", "test-namespace"
    )

    build = next(argv for argv in calls if argv[:2] == ["skaffold", "run"])
    assert build[build.index("--tag") + 1] == "test-namespace"
    for action in ("run", "delete"):
        command = next(argv for argv in calls if argv[:2] == ["skaffold", action])
        assert command[command.index("--kube-context") + 1] == "test-context"
        assert command[command.index("--namespace") + 1] == "test-namespace"
    assert checked == [
        (tmp_path / "skaffold", "registry.test/team", "test-context",
         "test-namespace", reference, DIGEST)
    ]


def test_real_check_isolates_resources_in_a_new_namespace(monkeypatch):
    calls = []
    projects = []
    monkeypatch.setattr(sys, "argv", [
        "check_devloop_tools", "--registry", "registry.test/team", "--tool", "skaffold"
    ])
    monkeypatch.setattr(real_check.shutil, "which", lambda tool: tool)
    monkeypatch.setattr(real_check, "current_context", lambda: "test-context")
    monkeypatch.setattr(real_check, "run", lambda argv, cwd, env=None: calls.append(argv))
    monkeypatch.setattr(
        real_check, "check_skaffold",
        lambda workdir, registry, insecure, context, namespace: projects.append(
            (registry, context, namespace)
        ),
    )

    real_check.main()

    create = next(argv for argv in calls if "create" in argv)
    delete = next(argv for argv in calls if "delete" in argv)
    namespace = create[-1]
    assert namespace.startswith("pycontainer-devloop-")
    assert delete[delete.index("namespace") + 1] == namespace
    assert projects == [("registry.test/team", "test-context", namespace)]
    assert all("test-context" in argv for argv in (create, delete))


def test_real_check_does_not_delete_namespace_when_creation_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", [
        "check_devloop_tools", "--registry", "registry.test/team", "--tool", "skaffold"
    ])
    monkeypatch.setattr(real_check.shutil, "which", lambda tool: tool)
    monkeypatch.setattr(real_check, "current_context", lambda: "test-context")

    def fake_run(argv, cwd, env=None):
        calls.append(argv)
        if "create" in argv:
            raise SystemExit("namespace already exists")

    monkeypatch.setattr(real_check, "run", fake_run)
    with pytest.raises(SystemExit, match="namespace already exists"):
        real_check.main()
    assert not any("delete" in argv for argv in calls)


def test_real_check_reports_cleanup_failure_without_masking_original(tmp_path, monkeypatch, capsys):
    def fail_cleanup(*args, **kwargs):
        raise SystemExit("cleanup failed")

    monkeypatch.setattr(real_check, "run", fail_cleanup)
    with pytest.raises(ValueError, match="build failed"):
        try:
            raise ValueError("build failed")
        finally:
            real_check.cleanup(["tilt", "down"], tmp_path)
    assert "cleanup failed" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="cleanup failed"):
        real_check.cleanup(["tilt", "down"], tmp_path)
