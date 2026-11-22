"""Pull-through registry mirrors: fallback order, isolation, and integrity."""
import gzip
import hashlib
import io
import json
import logging
import socket
import ssl
import sys
import tarfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pycontainer import auth as auth_module
from pycontainer import builder as builder_module
from pycontainer import cli
from pycontainer.auth import RegistryAuth, get_mirror_auth
from pycontainer.blob_store import BlobIntegrityError
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.registry_client import (
    RegistryRateLimitError,
    RegistryRequestError,
    parse_image_reference,
)

SERVER_TIMEOUT_SECONDS = 10
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
UPSTREAM_AUTH = RegistryAuth(username="upstream-user", password="upstream-secret")
UPSTREAM_HEADER = "Basic dXBzdHJlYW0tdXNlcjp1cHN0cmVhbS1zZWNyZXQ="


def _digest(data):
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _base_image():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo("etc/base-marker")
        info.size = 4
        archive.addfile(info, io.BytesIO(b"base"))
    layer = gzip.compress(raw.getvalue(), mtime=0)
    config = json.dumps({
        "os": "linux",
        "architecture": "amd64",
        "config": {},
        "rootfs": {"type": "layers", "diff_ids": [_digest(raw.getvalue())]},
    }).encode()
    manifest = json.dumps({
        "schemaVersion": 2,
        "mediaType": MANIFEST_TYPE,
        "config": {"mediaType": CONFIG_TYPE, "digest": _digest(config), "size": len(config)},
        "layers": [{"mediaType": LAYER_TYPE, "digest": _digest(layer), "size": len(layer)}],
    }).encode()
    return manifest, {_digest(config): config, _digest(layer): layer}


class _Registry:
    def __init__(self):
        self.manifests = {}
        self.blobs = {}
        self.requests = []
        self.status = None
        self.status_paths = {}
        self.redirect_paths = {}
        self.challenge_realm = None

    def serve_image(self, tag="1"):
        manifest, blobs = _base_image()
        self.manifests[tag] = manifest
        self.manifests[_digest(manifest)] = manifest
        self.blobs.update(blobs)
        return manifest, blobs


def _handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, status, body=b"", headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _forced(self):
            state.requests.append(
                (self.command, self.path, self.headers.get("Authorization"))
            )
            if state.challenge_realm and not self.headers.get("Authorization"):
                self._send(401, headers={
                    "Www-Authenticate": f'Bearer realm="{state.challenge_realm}",service="mirror"'
                })
                return True
            for fragment, destination in state.redirect_paths.items():
                if fragment in self.path:
                    self._send(307, headers={"Location": f"{destination}{self.path}"})
                    return True
            for fragment, status in state.status_paths.items():
                if fragment in self.path:
                    self._send(status)
                    return True
            if state.status:
                self._send(state.status)
                return True
            return False

        def do_GET(self):
            if self._forced():
                return
            name = self.path.rsplit("/", 1)[1]
            if "/manifests/" in self.path and name in state.manifests:
                self._send(200, state.manifests[name], {"Content-Type": MANIFEST_TYPE})
            elif "/blobs/" in self.path and name in state.blobs:
                self._send(200, state.blobs[name])
            else:
                self._send(404)

        def do_HEAD(self):
            if not self._forced():
                self._send(200 if self.path.rsplit("/", 1)[1] in state.blobs else 404)

        def do_POST(self):
            if not self._forced():
                self._send(202, headers={"Location": "/v2/upload/session?state=s"})

        def do_PUT(self):
            if self._forced():
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path.startswith("/v2/upload/"):
                state.blobs[_digest(body)] = body
            else:
                state.manifests[self.path.rsplit("/", 1)[1]] = body
            self._send(201)

    return Handler


@contextmanager
def _serve(tls_context=None):
    state = _Registry()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(SERVER_TIMEOUT_SECONDS)


def _closed_port_host():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{sock.getsockname()[1]}"


@pytest.fixture
def mirror_auth(monkeypatch):
    """Give each mirror host its own synthetic token and the upstream basic auth."""
    resolved = []

    def fake_mirror_auth(host):
        resolved.append(host)
        return RegistryAuth(bearer_token=f"mirror-token-{host}")

    monkeypatch.setattr(builder_module, "get_mirror_auth", fake_mirror_auth)
    monkeypatch.setattr(
        builder_module, "get_registry_auth", lambda *_args, **_kwargs: UPSTREAM_AUTH
    )
    monkeypatch.setattr("pycontainer.registry_client.time.sleep", lambda _delay: None)
    return resolved


@pytest.fixture
def mirror_ca(tmp_path):
    trustme = pytest.importorskip("trustme")
    ca = trustme.CA()
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1").configure_cert(server_context)
    ca_file = tmp_path / "mirror-ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    return server_context, str(ca_file)


def _builder(
    tmp_path, upstream, mirrors, base_ref="team/base:1", insecure_registries=None, **config
):
    (tmp_path / "app.py").write_text("print('hi')\n")
    hosts = [upstream, *mirrors]
    if insecure_registries is None:
        insecure_registries = [host for host in hosts if not host.startswith("docker.io")]
    return ImageBuilder(BuildConfig(
        context_dir=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        base_image=f"{upstream}/{base_ref}",
        insecure_registries=insecure_registries,
        registry_mirrors={upstream: mirrors},
        use_cache=False,
        **config,
    ))


def _pull(builder, tmp_path):
    layers = tmp_path / "layers"
    layers.mkdir(exist_ok=True)
    return builder._pull_base_image(layers, "linux", "amd64")


def test_mirror_serves_pull_without_resolving_or_contacting_upstream(
    tmp_path, mirror_auth, capsys
):
    with _serve() as (upstream, up), _serve() as (mirror, mir):
        mir.serve_image()
        layers, config = _pull(_builder(tmp_path, upstream, [mirror], verbose=True), tmp_path)

    assert config["os"] == "linux" and len(layers) == 1
    assert up.requests == []
    assert mirror_auth == [mirror]
    assert [path for _, path, _ in mir.requests][0] == "/v2/team/base/manifests/1"
    assert {auth for _, _, auth in mir.requests} == {f"Bearer mirror-token-{mirror}"}
    assert f"Served by: {mirror}" in capsys.readouterr().out


@pytest.mark.parametrize("trust_mirror", [True, False])
def test_https_mirror_uses_configured_ca_or_falls_back(
    tmp_path, mirror_auth, mirror_ca, trust_mirror
):
    server_context, ca_file = mirror_ca
    with _serve() as (upstream, up), _serve(server_context) as (mirror, mir):
        up.serve_image()
        mir.serve_image()
        builder = _builder(
            tmp_path, upstream, [mirror],
            insecure_registries=[upstream],
            registry_ca_file=ca_file if trust_mirror else None,
        )
        layers, config = _pull(builder, tmp_path)

    assert config["os"] == "linux" and len(layers) == 1
    if trust_mirror:
        assert mir.requests
        assert {auth for _, _, auth in mir.requests} == {f"Bearer mirror-token-{mirror}"}
        assert up.requests == []
    else:
        assert mir.requests == []
        assert up.requests
        assert {auth for _, _, auth in up.requests} == {UPSTREAM_HEADER}


@pytest.mark.parametrize("redirected_object", ["manifests", "blobs"])
def test_mirror_cross_origin_redirect_never_sends_credentials(
    tmp_path, mirror_auth, redirected_object
):
    with _serve() as (upstream, up), _serve() as (mirror, mir), _serve() as (
        storage, target
    ):
        mir.serve_image()
        target.serve_image()
        mir.redirect_paths[f"/{redirected_object}/"] = f"http://{storage}"
        layers, config = _pull(_builder(tmp_path, upstream, [mirror]), tmp_path)

    assert config["os"] == "linux" and len(layers) == 1
    assert up.requests == []
    assert {auth for _, _, auth in mir.requests} == {f"Bearer mirror-token-{mirror}"}
    assert target.requests
    assert all(auth is None for _, _, auth in target.requests)


@pytest.mark.parametrize("redirected_object", ["manifests", "blobs"])
def test_https_mirror_refuses_http_redirect_without_fallback(
    tmp_path, mirror_auth, mirror_ca, redirected_object
):
    server_context, ca_file = mirror_ca
    with _serve() as (upstream, up), _serve(server_context) as (mirror, mir), _serve() as (
        storage, target
    ):
        mir.serve_image()
        mir.redirect_paths[f"/{redirected_object}/"] = f"http://{storage}"
        builder = _builder(
            tmp_path, upstream, [mirror],
            insecure_registries=[upstream],
            registry_ca_file=ca_file,
        )
        with pytest.raises(RegistryRequestError, match="downgrade"):
            _pull(builder, tmp_path)

    assert mir.requests
    assert target.requests == []
    assert up.requests == []


def test_fallback_order_connection_404_5xx_then_upstream(tmp_path, mirror_auth, caplog):
    unreachable = _closed_port_host()
    with _serve() as (upstream, up), _serve() as (missing, m404), _serve() as (
        broken,
        m503,
    ):
        up.serve_image()
        m503.status = 503
        builder = _builder(tmp_path, upstream, [unreachable, missing, broken])
        with caplog.at_level(logging.WARNING, logger="pycontainer.registry_mirrors"):
            layers, _ = _pull(builder, tmp_path)

    assert len(layers) == 1
    warnings = [r.getMessage() for r in caplog.records]
    assert any(unreachable in w and "connection failed" in w for w in warnings)
    assert any(missing in w and "HTTP 404" in w for w in warnings)
    assert any(broken in w and "HTTP 503 after retries" in w for w in warnings)
    # 5xx exhausts the client retry budget, then the mirror is skipped for later objects.
    assert len(m503.requests) == 3
    # 404 is per object: the 404 mirror is asked for every object.
    assert len(m404.requests) == 3
    assert {auth for _, _, auth in up.requests} == {UPSTREAM_HEADER}
    assert not any(auth == UPSTREAM_HEADER for _, _, auth in m404.requests + m503.requests)


@pytest.mark.parametrize("status", [401, 403])
def test_mirror_auth_rejection_falls_back_with_warning(
    tmp_path, mirror_auth, caplog, status
):
    with _serve() as (upstream, up), _serve() as (mirror, mir):
        up.serve_image()
        mir.status = status
        with caplog.at_level(logging.WARNING, logger="pycontainer.registry_mirrors"):
            _pull(_builder(tmp_path, upstream, [mirror]), tmp_path)

    assert len(mir.requests) == 1
    assert any(
        f"HTTP {status}" in r.getMessage() and "credentials" in r.getMessage()
        for r in caplog.records
    )
    assert up.requests


def test_mirror_blob_404_falls_back_per_object(tmp_path, mirror_auth):
    with _serve() as (upstream, up), _serve() as (mirror, mir):
        _, blobs = up.serve_image()
        mir.serve_image()
        layer_digest = [d for d, data in blobs.items() if data[:2] == b"\x1f\x8b"][0]
        del mir.blobs[layer_digest]
        _pull(_builder(tmp_path, upstream, [mirror]), tmp_path)

    assert [path for _, path, _ in up.requests] == [f"/v2/team/base/blobs/{layer_digest}"]


def test_tampered_mirror_blob_fails_closed_without_upstream(tmp_path, mirror_auth):
    with _serve() as (upstream, up), _serve() as (mirror, mir):
        up.serve_image()
        _, blobs = mir.serve_image()
        layer_digest = [d for d, data in blobs.items() if data[:2] == b"\x1f\x8b"][0]
        mir.blobs[layer_digest] = b"x" * len(blobs[layer_digest])
        with pytest.raises(BlobIntegrityError, match="hashes to"):
            _pull(_builder(tmp_path, upstream, [mirror]), tmp_path)

    assert up.requests == []
    assert not (tmp_path / "layers" / layer_digest.split(":", 1)[1]).exists()


def test_digest_pinned_base_requires_mirror_to_serve_same_digest(tmp_path, mirror_auth):
    with _serve() as (upstream, up), _serve() as (mirror, mir):
        manifest, _ = up.serve_image()
        pinned = _digest(manifest)
        mir.manifests[pinned] = manifest + b"\n"
        builder = _builder(tmp_path, upstream, [mirror], base_ref=f"team/base@{pinned}")
        with pytest.raises(BlobIntegrityError, match=pinned):
            _pull(builder, tmp_path)

    assert mir.requests[0][1] == f"/v2/team/base/manifests/{pinned}"
    assert up.requests == []


@pytest.mark.parametrize("realm_kind", ["unreachable", "503"])
def test_mirror_token_realm_failure_falls_back(
    tmp_path, mirror_auth, monkeypatch, realm_kind
):
    monkeypatch.setattr(builder_module, "get_mirror_auth", lambda _host: RegistryAuth())
    with _serve() as (upstream, up), _serve() as (mirror, mir), _serve() as (
        realm,
        realm_state,
    ):
        up.serve_image()
        mir.serve_image()
        realm_state.status = 503
        realm_host = _closed_port_host() if realm_kind == "unreachable" else realm
        mir.challenge_realm = f"http://{realm_host}/token"
        builder = _builder(tmp_path, upstream, [mirror])
        builder.config.insecure_registries.append(realm_host)
        layers, _ = _pull(builder, tmp_path)

    assert len(layers) == 1
    assert len(mir.requests) == 1
    assert up.requests


def test_rate_limited_mirror_does_not_fall_back(tmp_path, mirror_auth):
    with _serve() as (upstream, up), _serve() as (mirror, mir):
        up.serve_image()
        mir.status = 429
        with pytest.raises(RegistryRateLimitError):
            _pull(_builder(tmp_path, upstream, [mirror]), tmp_path)

    assert up.requests == []


def test_upstream_failure_after_mirrors_is_reported(tmp_path, mirror_auth):
    with _serve() as (upstream, up), _serve() as (mirror, _mir):
        with pytest.raises(RegistryRequestError, match="HTTP 404 from 127.0.0.1"):
            _pull(_builder(tmp_path, upstream, [mirror]), tmp_path)
    assert up.requests


def test_build_pulls_via_mirror_and_push_bypasses_it(tmp_path, mirror_auth):
    with _serve() as (upstream, up), _serve() as (mirror, mir):
        mir.serve_image()
        builder = _builder(tmp_path, upstream, [mirror], tag=f"{upstream}/team/app:v1")
        builder.build()
        mirror_requests = list(mir.requests)
        builder.push(show_progress=False)

    assert mir.requests == mirror_requests
    assert {method for method, _, _ in mir.requests} == {"GET"}
    assert {method for method, _, _ in up.requests} == {"HEAD", "POST", "PUT"}
    assert {auth for _, _, auth in up.requests} == {UPSTREAM_HEADER}
    assert "v1" in up.manifests


def test_docker_hub_mirror_keeps_library_path_and_skips_docker_hub(
    tmp_path, mirror_auth, monkeypatch
):
    opened = []
    monkeypatch.setattr(
        builder_module.ImageBuilder,
        "_registry_client",
        _spy_registry_client(opened),
    )
    with _serve() as (mirror, mir):
        mir.manifests["3.11-slim"], blobs = _base_image()
        mir.blobs.update(blobs)
        (tmp_path / "app.py").write_text("")
        builder = ImageBuilder(BuildConfig(
            context_dir=str(tmp_path),
            base_image="python:3.11-slim",
            insecure_registries=[mirror],
            registry_mirrors={"index.docker.io": [mirror]},
            use_cache=False,
        ))
        _pull(builder, tmp_path)

    assert mir.requests[0][1] == "/v2/library/python/manifests/3.11-slim"
    assert opened == [mirror]


def _spy_registry_client(opened):
    original = builder_module.ImageBuilder._registry_client

    def spy(self, registry, repo, auth):
        opened.append(registry)
        return original(self, registry, repo, auth)

    return spy


def test_registry_1_docker_io_reference_keeps_library_normalization():
    assert parse_image_reference("registry-1.docker.io/python:3.12") == (
        "registry-1.docker.io",
        "library/python",
        "3.12",
    )


def test_docker_config_hub_credentials_match_registry_1_alias(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"auths": {"docker.io": {"auth": "dTpw"}}}))
    provider = auth_module.DockerConfigAuthProvider(config)
    assert provider.get_credentials("registry-1.docker.io") == ("u", "p")


def test_mirror_auth_ignores_host_agnostic_environment_credentials(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"auths": {"mirror.example:5000": {"auth": "bTp0"}}}))
    monkeypatch.setattr(
        auth_module.DockerConfigAuthProvider, "__init__",
        lambda self, config_path=None: setattr(self, "config_path", config),
    )
    for name, value in {
        "REGISTRY_TOKEN": "env-token",
        "REGISTRY_USERNAME": "env-user",
        "REGISTRY_PASSWORD": "env-password",
        "GITHUB_TOKEN": "gh-token",
    }.items():
        monkeypatch.setenv(name, value)

    assert get_mirror_auth("mirror.example:5000") == RegistryAuth(username="m", password="t")
    assert get_mirror_auth("other.example") == RegistryAuth()
    assert get_mirror_auth("ghcr.io") == RegistryAuth(bearer_token="gh-token")
    assert get_mirror_auth("ghcr.io.evil.example") == RegistryAuth()


def test_mirror_configuration_is_normalized():
    config = BuildConfig(registry_mirrors={
        "Docker.IO": ["Mirror.Example", "mirror.example", "backup.example:5000"],
        "ghcr.io": ["ghcr-mirror.example"],
    })
    assert config.registry_mirrors == {
        "docker.io": ["mirror.example", "backup.example:5000"],
        "ghcr.io": ["ghcr-mirror.example"],
    }


@pytest.mark.parametrize(
    "mirrors",
    [
        {"docker.io": ["https://mirror.example"]},
        {"docker.io": ["mirror.example/path"]},
        {"docker.io": ["user@mirror.example"]},
        {"docker.io": []},
        {"docker.io": "mirror.example"},
        {"docker.io": ["index.docker.io"]},
        {"docker.io": ["a.example"], "index.docker.io": ["b.example"]},
        {"bad host": ["mirror.example"]},
        ["mirror.example"],
    ],
)
def test_invalid_mirror_configuration_is_rejected(mirrors):
    with pytest.raises(ValueError, match="mirror|registry"):
        BuildConfig(registry_mirrors=mirrors)


def _cli_config(monkeypatch, argv):
    configs = []

    class FakeImageBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", FakeImageBuilder)
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", *argv])
    cli.main()
    return configs[0]


def test_cli_and_toml_mirror_options_merge_per_upstream(tmp_path, monkeypatch):
    toml = tmp_path / "pycontainer.toml"
    toml.write_text(
        "[build.registry_mirrors]\n"
        '"index.docker.io" = ["file-mirror.example"]\n'
        '"ghcr.io" = ["ghcr-mirror.example"]\n'
    )
    assert config_from_file(toml).registry_mirrors == {
        "docker.io": ["file-mirror.example"],
        "ghcr.io": ["ghcr-mirror.example"],
    }

    config = _cli_config(monkeypatch, [
        "--config", str(toml), "--base-image", "python:3.11-slim",
        "--registry-mirror", "docker.io=first.example",
        "--registry-mirror", "docker.io=second.example:5000",
    ])
    assert config.registry_mirrors == {
        "ghcr.io": ["ghcr-mirror.example"],
        "docker.io": ["first.example", "second.example:5000"],
    }


@pytest.mark.parametrize(
    "value", ["docker.io", "=mirror.example", "docker.io=", "docker.io=a=b", "docker.io=http://m"]
)
def test_cli_rejects_invalid_mirror_syntax(monkeypatch, capsys, tmp_path, value):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        _cli_config(monkeypatch, ["--base-image", "python:3.11-slim", "--registry-mirror", value])
    assert error.value.code == 2
    assert "mirror" in capsys.readouterr().err.lower()
