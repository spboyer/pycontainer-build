"""Tests for the pre-build preview that compares a plan with an existing image (issue #174)."""
import base64
import contextlib
import http.server
import json
import logging
import os
import shutil
import socket
import stat
import sys
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path

import pytest

from pycontainer import cli, fs_utils, preview, registry_client
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import load_config_file
from pycontainer.framework import detect_framework
from pycontainer.preview import plan_app_layer, preview_image, render_text, run_preview
from pycontainer.registry_client import RegistryClient

from test_layer_inspect import MANIFEST_TYPE, Image, docker_archive, sha, tar

SECRET = "s3cr3t-value"


def context(root: Path, files=None) -> Path:
    files = files or {"src/app.py": "print('hello')\n", "src/util.py": "VALUE = 1\n"}
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    return root


def base_layout(root: Path, content=b"python") -> str:
    Image(root).image([tar(("usr/bin/python3", content))])
    return f"oci:{root}"


def config(tmp_path: Path, **overrides) -> BuildConfig:
    values = dict(tag="demo:v1", base_image=base_layout(tmp_path / "base") if not (tmp_path / "base").exists()
                  else f"oci:{tmp_path / 'base'}",
                  context_dir=str(tmp_path / "ctx"), output_dir=str(tmp_path / "out"), use_cache=False,
                  cache_dir=str(tmp_path / "cache"), env={"API_KEY": SECRET}, labels={"team": "a"})
    values.update(overrides)
    return BuildConfig(**values)


def build(cfg: BuildConfig) -> str:
    ImageBuilder(cfg).build()
    return f"oci:{cfg.output_dir}"


@pytest.fixture
def built(tmp_path):
    context(tmp_path / "ctx")
    cfg = config(tmp_path)
    return cfg, build(cfg)


def changes(result):
    return {change["path"]: change for change in result["files"]}


def setting(result, name):
    return next(item for item in result["settings"] if item["name"] == name)


def test_planned_layer_matches_builder_bytes(built):
    cfg, baseline = built
    result = preview_image(cfg, baseline)
    assert result["provenance"]["level"] == "exact"
    assert result["plan"]["appLayerDiffId"] == result["baseline"]["finalLayerDiffId"]
    assert result["files"] == []
    assert {k: v["status"] for k, v in result["components"].items()} == {
        "base": "identical", "dependencies": "identical", "application": "identical",
    }
    assert all(item["status"] == "identical" for item in result["settings"])


def test_reports_adds_modifications_deletes_and_mode(built, tmp_path):
    cfg, baseline = built
    ctx = tmp_path / "ctx"
    (ctx / "src" / "app.py").write_text("print('changed')\n")
    (ctx / "src" / "util.py").unlink()
    (ctx / "src" / "new.py").write_text("NEW = 1\n")
    os.chmod(ctx / "pyproject.toml", 0o444)
    try:
        result = preview_image(cfg, baseline)
    finally:
        os.chmod(ctx / "pyproject.toml", 0o644)
    assert result["provenance"]["level"] == "positional"
    found = changes(result)
    assert found["/app/src/app.py"]["change"] == "modified"
    assert found["/app/src/app.py"]["reasons"] == ["content"]
    assert found["/app/src/util.py"]["change"] == "deleted"
    assert found["/app/src/new.py"]["change"] == "added"
    assert found["/app/pyproject.toml"]["reasons"] == ["mode"]
    assert result["components"]["application"]["status"] == "changed"
    assert result["components"]["base"]["status"] == "identical"
    text = render_text(result)
    assert "M /app/src/app.py (content)" in text and "D /app/src/util.py" in text and "A /app/src/new.py" in text


def test_config_only_changes_redact_environment_values(built, tmp_path):
    cfg, baseline = built
    planned = config(tmp_path, env={"API_KEY": "rotated-value", "NEW_VAR": "fresh-value"},
                     labels={"team": "b"}, entrypoint=["python", "-m", "demo"], exposed_ports=[8080])
    result = preview_image(planned, baseline)
    assert result["files"] == []
    assert result["components"]["application"]["status"] == "identical"
    assert setting(result, "Env.API_KEY") == {"name": "Env.API_KEY", "status": "changed",
                                               "before": "<redacted>", "after": "<redacted>"}
    assert setting(result, "Env.NEW_VAR")["status"] == "added"
    assert setting(result, "Labels.team") == {"name": "Labels.team", "status": "changed", "before": "a", "after": "b"}
    assert setting(result, "Entrypoint")["after"] == ["python", "-m", "demo"]
    assert setting(result, "ExposedPorts")["status"] == "added"
    for output in (json.dumps(result), render_text(result)):
        assert SECRET not in output and "rotated-value" not in output and "fresh-value" not in output


def test_workdir_change_reports_moved_files(built, tmp_path):
    cfg, baseline = built
    result = preview_image(config(tmp_path, workdir="/srv"), baseline)
    found = changes(result)
    assert found["/app/src/app.py"]["change"] == "deleted"
    assert found["/srv/src/app.py"]["change"] == "added"
    assert setting(result, "WorkingDir") == {"name": "WorkingDir", "status": "changed",
                                              "before": "/app", "after": "/srv"}


@pytest.mark.parametrize("use_cache", [False, True])
def test_non_reproducible_build_is_unknown_not_unchanged(tmp_path, use_cache):
    context(tmp_path / "ctx")
    cfg = config(tmp_path, reproducible=False, use_cache=use_cache)
    baseline = build(cfg)
    result = preview_image(cfg, baseline)
    assert result["provenance"]["level"] == "positional"
    assert result["components"]["application"]["status"] == "unknown"
    assert result["plan"]["appLayerDiffId"] is None
    (tmp_path / "ctx" / "src" / "app.py").write_text("print('changed')\n")
    changed = preview_image(cfg, baseline)
    assert changed["provenance"]["level"] == "positional"
    assert changed["components"]["application"]["status"] == "changed"


def test_include_deps_marks_dependencies_and_paths_unknown(built, tmp_path):
    cfg, baseline = built
    (tmp_path / "ctx" / "requirements.txt").write_text("requests\n")
    result = preview_image(config(tmp_path, include_deps=True), baseline)
    assert result["components"]["dependencies"]["status"] == "unknown"
    env = {item["name"]: item for item in result["settings"] if item["name"].startswith("Env.")}
    assert all(env[name]["status"] == "unknown" for name in ("Env.PATH", "Env.PYTHONPATH") if name in env)


def test_changed_base_infers_application_changes(built, tmp_path):
    cfg, baseline = built
    other = base_layout(tmp_path / "other-base", b"different python")
    (tmp_path / "ctx" / "src" / "app.py").write_text("print('changed')\n")
    result = preview_image(config(tmp_path, base_image=other), baseline)
    assert result["components"]["base"]["status"] == "changed"
    assert result["provenance"]["level"] == "inferred"
    assert result["components"]["application"]["status"] == "unknown"
    assert result["components"]["dependencies"]["status"] == "unknown"
    assert changes(result)["/app/src/app.py"]["inferred"] is True


def test_registry_base_with_local_baseline_is_not_resolved(built, tmp_path, monkeypatch):
    cfg, baseline = built
    monkeypatch.setattr(preview, "_registry_client", lambda *a: pytest.fail("registry contacted"))
    result = preview_image(config(tmp_path, base_image="python:3.11-slim"), baseline)
    assert result["components"]["base"]["status"] == "unknown"
    assert result["components"]["dependencies"]["status"] == "unknown"
    assert result["provenance"]["level"] == "exact"
    assert result["components"]["application"]["status"] == "identical"
    assert setting(result, "User")["status"] == "unknown"


def test_unidentifiable_application_layer_is_unknown(tmp_path):
    layout = Image(tmp_path / "img")
    layout.image([tar(("usr/bin/python3", b"python"), ("app", None), ("app/main.py", b"x"))])
    context(tmp_path / "ctx")
    result = preview_image(config(tmp_path), f"oci:{tmp_path / 'img'}")
    assert result["provenance"]["level"] == "none"
    assert result["files"] is None
    assert result["components"]["application"]["status"] == "unknown"
    assert "Application files: unknown" in render_text(result)


@pytest.mark.parametrize("layer", [tar(("app/../../etc/passwd", b"x")), tar(("/abs/path", b"x"))])
def test_unsafe_layer_paths_are_rejected(tmp_path, layer):
    Image(tmp_path / "img").image([layer])
    context(tmp_path / "ctx")
    output, code = run_preview(config(tmp_path), f"oci:{tmp_path / 'img'}")
    assert code == 1 and output.startswith("error:")


def test_corrupt_final_layer_is_rejected(tmp_path):
    Image(tmp_path / "img").image([tar(("app/main.py", b"x"))], diff_ids=[sha(b"not the layer")])
    context(tmp_path / "ctx")
    output, code = run_preview(config(tmp_path), f"oci:{tmp_path / 'img'}")
    assert code == 1 and "DiffID" in output


def test_ambiguous_or_mismatched_platform_baselines_are_rejected(tmp_path):
    context(tmp_path / "ctx")
    layout = Image(tmp_path / "dup")
    layout.image([tar(("app/a.py", b"a"))])
    layout.image([tar(("app/b.py", b"b"))])
    output, code = run_preview(config(tmp_path), f"oci:{tmp_path / 'dup'}")
    assert code == 1
    Image(tmp_path / "arm").image([tar(("app/a.py", b"a"))], arch="arm64")
    output, code = run_preview(config(tmp_path), f"oci:{tmp_path / 'arm'}")
    assert code == 1 and "linux/amd64" in output


def test_bare_registry_names_require_explicit_remote_prefix(tmp_path):
    context(tmp_path / "ctx")
    output, code = run_preview(config(tmp_path), "demo:v1")
    assert code == 1 and "docker://" in output


def test_docker_archive_baseline_without_workdir_is_unknown(tmp_path):
    context(tmp_path / "ctx")
    cfg = config(tmp_path)
    archive = docker_archive(tmp_path / "app.tar", [tar(("usr/bin/python3", b"python")),
                                                    tar(("app/src/app.py", b"old"))])
    result = preview_image(cfg, archive)
    assert result["components"]["base"]["status"] == "identical"
    assert result["provenance"]["level"] == "none"
    assert result["files"] is None
    assert result["components"]["application"]["status"] == "unknown"
    assert result["components"]["dependencies"]["status"] == "unknown"


def test_malformed_base_config_is_rejected(built, tmp_path):
    cfg, baseline = built
    layout = Image(tmp_path / "bad-base")
    layer = tar(("usr/bin/python3", b"python"))
    image_config = {"architecture": "amd64", "os": "linux", "config": ["not", "an", "object"],
                    "rootfs": {"type": "layers", "diff_ids": [sha(layer)]}}
    manifest = {"schemaVersion": 2, "mediaType": MANIFEST_TYPE,
                "config": layout.blob(json.dumps(image_config).encode(), "application/vnd.oci.image.config.v1+json"),
                "layers": [layout.blob(layer, "application/vnd.oci.image.layer.v1.tar")]}
    descriptor = layout.blob(json.dumps(manifest).encode(), MANIFEST_TYPE)
    (tmp_path / "bad-base" / "index.json").write_text(json.dumps(
        {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [descriptor]}))
    output, code = run_preview(config(tmp_path, base_image=f"oci:{tmp_path / 'bad-base'}"), baseline)
    assert code == 1 and "malformed" in output


def tree(root: Path):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*")} if root.exists() else {}


def test_local_preview_is_offline_and_writes_nothing(built, tmp_path, monkeypatch):
    cfg, baseline = built
    home, temp = tmp_path / "home", tmp_path / "tmp"
    home.mkdir(), temp.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(tempfile, "tempdir", str(temp))

    def blocked(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    (tmp_path / "ctx" / "src" / "app.py").write_text("print('changed')\n")
    watched = [tmp_path / name for name in ("ctx", "out", "base", "cache")] + [home]
    before = {str(path): tree(path) for path in watched}
    result = preview_image(cfg, baseline)
    assert changes(result)["/app/src/app.py"]["change"] == "modified"
    assert {str(path): tree(path) for path in watched} == before
    assert not (tmp_path / "cache").exists()
    assert list(temp.iterdir()) == []


class FakeRegistry:
    """Loopback Registry v2 server that serves one OCI layout's blobs and manifests."""

    def __init__(self, layout: Path, repository="team/demo", tag="v1", credentials=None, redirect_to=None,
                 manifest_status=200):
        index = json.loads((layout / "index.json").read_text())
        self.blobs = {f"sha256:{p.name}": p.read_bytes() for p in (layout / "blobs" / "sha256").iterdir()}
        top = index["manifests"][0]
        self.manifests = {tag: (self.blobs[top["digest"]], top["mediaType"])}
        self.repository, self.credentials, self.redirect_to = repository, credentials, redirect_to
        self.manifest_status = manifest_status
        self.seen = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.seen.append((self.path, self.headers.get("Authorization")))
                if owner.credentials and self.headers.get("Authorization") != owner.credentials:
                    self.send_response(401)
                    self.send_header("Www-Authenticate", 'Basic realm="fake"')
                    self.end_headers()
                    return
                prefix = f"/v2/{owner.repository}/"
                kind, _, reference = self.path[len(prefix):].partition("/")
                if owner.redirect_to and kind == "manifests":
                    self.send_response(307)
                    self.send_header("Location", owner.redirect_to + self.path)
                    self.end_headers()
                    return
                if kind == "manifests" and owner.manifest_status != 200:
                    self.send_response(owner.manifest_status)
                    self.end_headers()
                    return
                if kind == "manifests":
                    body, media_type = owner.manifests.get(reference) or (owner.blobs.get(reference), MANIFEST_TYPE)
                elif kind == "blobs":
                    body, media_type = owner.blobs.get(reference), "application/octet-stream"
                else:
                    body = None
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@contextlib.contextmanager
def registry(monkeypatch, layout, **options):
    server = FakeRegistry(layout, **options)
    clients = []

    def factory(host, repository, config):
        authority = server.url.removeprefix("http://")
        client = RegistryClient(authority, repository, username="user", password="pass", insecure=True)
        clients.append(client)
        return client

    monkeypatch.setattr(preview, "_registry_client", factory)
    try:
        yield server
    finally:
        server.close()


BASIC = "Basic " + base64.b64encode(b"user:pass").decode()


def test_remote_baseline_uses_registry_and_resolves_base(built, tmp_path, monkeypatch):
    cfg, _ = built
    (tmp_path / "ctx" / "src" / "new.py").write_text("NEW = 1\n")
    with registry(monkeypatch, tmp_path / "out", credentials=BASIC) as server:
        result = preview_image(cfg, "docker://registry.example/team/demo:v1")
    assert result["baseline"]["source"] == "registry"
    assert result["baseline"]["image"] == "registry.example/team/demo:v1"
    assert result["provenance"]["level"] == "positional"
    assert changes(result)["/app/src/new.py"]["change"] == "added"
    assert any(path.endswith("/manifests/v1") for path, _ in server.seen)


def test_remote_client_uses_build_transport_config(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(registry_client, "create_registry_ssl_context", lambda _: None)
    cfg = config(tmp_path, insecure_registries=["registry.example:5000"], registry_ca_file="ca.pem")
    monkeypatch.setattr(preview, "get_registry_auth", lambda _: SimpleNamespace(
        bearer_token="token", username=None, password=None
    ))
    captured = {}
    monkeypatch.setattr(preview, "RegistryClient", lambda *args, **kwargs: captured.update(
        args=args, kwargs=kwargs
    ))
    preview._registry_client("registry.example:5000", "team/app", cfg)
    assert captured["args"] == ("registry.example:5000", "team/app")
    assert captured["kwargs"]["insecure"] is True
    assert captured["kwargs"]["ca_file"] == "ca.pem"


def test_preview_cli_passes_registry_transport_flags(tmp_path, monkeypatch, capsys):
    captured = []
    monkeypatch.setattr(registry_client, "create_registry_ssl_context", lambda _: None)
    monkeypatch.setattr(preview, "run_preview", lambda cfg, baseline, json_output: (
        captured.append((cfg, baseline)) or "ok", 0
    ))
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "preview", "--baseline", "docker://registry.example:5000/team/app:v1",
        "--context", str(tmp_path), "--base-image", "python:3.11-slim",
        "--insecure-registry", "registry.example:5000", "--registry-ca-file", "ca.pem",
    ])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert captured[0][0].insecure_registries == ["registry.example:5000"]
    assert captured[0][0].registry_ca_file == "ca.pem"
    assert "ok" in capsys.readouterr().out


def test_remote_failures_are_reported_without_partial_output(built, tmp_path, monkeypatch):
    cfg, _ = built
    with registry(monkeypatch, tmp_path / "out", manifest_status=404):
        output, code = run_preview(cfg, "docker://registry.example/team/demo:v1")
    assert code == 1 and "HTTP 404" in output
    with registry(monkeypatch, tmp_path / "out", credentials="Basic wrong"):
        output, code = run_preview(cfg, "docker://registry.example/team/demo:v1")
    assert code == 1 and "401" in output
    monkeypatch.setattr(registry_client, "MAX_MANIFEST_BYTES", 16)
    with registry(monkeypatch, tmp_path / "out"):
        output, code = run_preview(cfg, "docker://registry.example/team/demo:v1")
    assert code == 1 and "exceeds 16 bytes" in output


def test_remote_redirects_do_not_forward_authorization(built, tmp_path, monkeypatch):
    cfg, _ = built
    target = FakeRegistry(tmp_path / "out")
    try:
        with registry(monkeypatch, tmp_path / "out", redirect_to=target.url):
            output, code = run_preview(cfg, "docker://registry.example/team/demo:v1")
    finally:
        target.close()
    assert code == 0, output
    assert target.seen and all(auth is None for _, auth in target.seen)


def test_invalid_remote_reference_is_rejected(tmp_path):
    context(tmp_path / "ctx")
    output, code = run_preview(config(tmp_path), "docker://registry.example/demo?x=1")
    assert code == 1 and "Invalid remote baseline" in output


def test_plan_app_layer_is_deterministic(tmp_path):
    ctx = context(tmp_path / "ctx")
    files = [(ctx / "src" / "app.py", Path("src/app.py")), (ctx / "pyproject.toml", Path("pyproject.toml"))]
    first = plan_app_layer(files, "/app", True)
    assert first == plan_app_layer(list(reversed(files)), "/app", True)
    assert set(first[1]) == {"app/src/app.py", "app/pyproject.toml"}


def test_oversized_project_metadata_is_rejected(built, tmp_path, monkeypatch):
    cfg, baseline = built
    (Path(cfg.context_dir) / "pyproject.toml").write_text('[project]\nname = "demo"\n' + "#" * 256 + "\n")
    monkeypatch.setattr(fs_utils, "MAX_PROJECT_FILE_BYTES", 128)
    output, code = run_preview(cfg, baseline)
    assert code == 1 and "pyproject.toml is larger than 128 bytes" in output and "Baseline:" not in output


def test_planned_application_layer_is_bounded(built, monkeypatch):
    cfg, baseline = built
    monkeypatch.setattr(preview, "MAX_LAYER_UNCOMPRESSED_BYTES", 1024)
    output, code = run_preview(cfg, baseline)
    assert code == 1 and "planned application layer is larger than 1024 bytes" in output


def test_bounded_reads_accept_the_limit_and_reject_more(tmp_path):
    path = tmp_path / "pycontainer.toml"
    path.write_bytes(b"[build]\r\ntag = 'a'\r\n")
    size = path.stat().st_size
    assert fs_utils.read_bytes_limited(path, size) == path.read_bytes()
    assert fs_utils.read_text_limited(path, size) == path.read_text()
    with pytest.raises(fs_utils.FileTooLargeError):
        fs_utils.read_bytes_limited(path, size - 1)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fs_utils, "MAX_PROJECT_FILE_BYTES", size - 1)
        with pytest.raises(ValueError, match="larger than"):
            load_config_file(path)


def test_cli_preview_json(built, tmp_path, monkeypatch, capsys):
    cfg, baseline = built
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "preview", "--baseline", baseline, "--json", "--context", cfg.context_dir,
        "--base-image", cfg.base_image, "--tag", "demo:v1", "--cache-dir", cfg.cache_dir,
    ])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["provenance"]["level"] in ("exact", "positional")
    assert result["components"]["base"]["status"] == "identical"

def test_oversized_sources_fail_framework_detection_explicitly(tmp_path, monkeypatch):
    ctx = context(tmp_path / "ctx", {"main.py": "from flask import Flask\napp = Flask(__name__)\n" + "#" * 256})
    assert detect_framework(ctx)[0] == "Flask"
    monkeypatch.setattr(fs_utils, "MAX_PROJECT_FILE_BYTES", 128)
    with pytest.raises(fs_utils.FileTooLargeError, match="main.py is larger than 128 bytes"):
        detect_framework(ctx)


def test_oversized_docker_credentials_fail_explicitly(tmp_path, monkeypatch):
    from pycontainer.auth import DockerConfigAuthProvider

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"auths": {"registry.example": {"auth": "dXNlcjpwYXNz"}}, "pad": "x" * 256}))
    provider = DockerConfigAuthProvider(path)
    assert provider.get_credentials("registry.example") == ("user", "pass")
    monkeypatch.setattr(fs_utils, "MAX_PROJECT_FILE_BYTES", 128)
    with pytest.raises(fs_utils.FileTooLargeError, match="Docker credential file"):
        provider.get_credentials("registry.example")


def built_final_diff_id(output_dir: Path) -> str:
    root = Path(output_dir)

    def blob(descriptor):
        return (root / "blobs" / "sha256" / descriptor["digest"].split(":", 1)[1]).read_bytes()

    manifest = json.loads((root / "index.json").read_bytes())
    while "manifests" in manifest:
        manifest = json.loads(blob(manifest["manifests"][0]))
    layer = manifest["layers"][-1]
    contents = blob(layer)
    config = json.loads(blob(manifest["config"]))
    diff_id = config["rootfs"]["diff_ids"][-1]
    assert layer["mediaType"] == "application/vnd.oci.image.layer.v1.tar"
    assert layer["size"] == len(contents)
    assert layer["digest"] == diff_id == sha(contents)
    return diff_id


@pytest.mark.parametrize(
    "start_linked", [False, True], ids=["independent-to-hardlink", "hardlink-to-independent"]
)
def test_cache_enabled_exact_matches_built_layer_across_hardlink_topology(
    tmp_path, caplog, start_linked
):
    ctx = context(tmp_path / "ctx", {"src/a.py": "SAME = 1\n", "src/b.py": "SAME = 1\n"})
    first, second = ctx / "src" / "a.py", ctx / "src" / "b.py"
    stamp = 1_700_000_000_000_000_000

    def set_topology(linked):
        second.unlink()
        if linked:
            try:
                os.link(first, second)
            except (AttributeError, NotImplementedError, OSError) as error:
                pytest.skip(f"Hardlinks are unavailable here: {error}")
        else:
            shutil.copy2(first, second)
        for path in (first, second):
            os.utime(path, ns=(stamp, stamp))
        if not first.stat().st_ino or not second.stat().st_ino:
            pytest.skip("Filesystem does not expose inodes needed by tarfile")
        assert os.path.samefile(first, second) is linked

    def snapshot():
        return [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
                for path in (first, second)]

    set_topology(start_linked)
    original = snapshot()
    assert original[0] == original[1]

    def cfg(run):
        return config(tmp_path, use_cache=True, output_dir=str(tmp_path / f"out-{run}"))

    baseline = build(cfg("baseline"))
    baseline_diff_id = built_final_diff_id(tmp_path / "out-baseline")

    set_topology(not start_linked)
    assert snapshot() == original
    cache_before = tree(tmp_path / "cache")
    flipped = preview_image(cfg("flipped"), baseline)
    assert tree(tmp_path / "cache") == cache_before
    build(cfg("flipped"))
    assert flipped["plan"]["useCache"] is True
    assert flipped["provenance"]["level"] == "positional"
    assert flipped["components"]["application"]["status"] == "changed"
    assert changes(flipped)["/app/src/b.py"]["reasons"][0] == "type"
    assert (
        flipped["plan"]["appLayerDiffId"]
        == built_final_diff_id(tmp_path / "out-flipped")
        != baseline_diff_id
    )

    set_topology(start_linked)
    assert snapshot() == original
    cache_before = tree(tmp_path / "cache")
    restored = preview_image(cfg("restored"), baseline)
    assert tree(tmp_path / "cache") == cache_before
    with caplog.at_level(logging.DEBUG, logger="pycontainer.cache"):
        build(cfg("restored"))
    assert any("Layer cache hit:" in record.getMessage() for record in caplog.records)
    assert restored["provenance"]["level"] == "exact"
    assert restored["components"]["application"]["status"] == "identical"
    assert restored["files"] == []
    assert (
        restored["plan"]["appLayerDiffId"]
        == built_final_diff_id(tmp_path / "out-restored")
        == baseline_diff_id
    )


def test_cli_preview_no_cache_flag(built, monkeypatch, capsys):
    cfg, baseline = built
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "preview", "--baseline", baseline, "--json", "--no-cache", "--context", cfg.context_dir,
        "--base-image", cfg.base_image, "--tag", "demo:v1",
    ])
    with pytest.raises(SystemExit):
        cli.main()
    assert json.loads(capsys.readouterr().out)["plan"]["useCache"] is False
