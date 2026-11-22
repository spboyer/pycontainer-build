"""Transport security tests: opt-in HTTP registries and custom CA trust."""
import hashlib
import io
import json
import logging
import ssl
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pycontainer import cli
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.registry_client import (
    RegistryAuthenticationError,
    RegistryClient,
    RegistryRequestError,
    SafeRedirect,
    is_loopback_host,
)

trustme = pytest.importorskip("trustme")

SERVER_TIMEOUT_SECONDS = 10
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"


class _Registry:
    """Minimal in-memory Registry v2 state shared with request handlers."""

    def __init__(self):
        self.blobs = {}
        self.manifests = {}
        self.requests = []
        self.redirect = None


def _handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _record(self):
            state.requests.append(
                (self.command, self.path, self.headers.get("Authorization"))
            )

        def _send(self, status, body=b"", headers=None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            self._record()
            if state.redirect:
                self._send(state.redirect[0], headers={"Location": state.redirect[1]})
            elif "/manifests/" in self.path:
                body = state.manifests.get(self.path.rsplit("/", 1)[1])
                if body is None:
                    self._send(404)
                else:
                    digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
                    self._send(
                        200,
                        body,
                        {"Content-Type": MANIFEST_TYPE, "Docker-Content-Digest": digest},
                    )
            elif "/blobs/" in self.path:
                body = state.blobs.get(self.path.rsplit("/", 1)[1])
                self._send(404) if body is None else self._send(200, body)
            else:
                self._send(404)

        def do_HEAD(self):
            self._record()
            digest = self.path.rsplit("/", 1)[1]
            self._send(200 if digest in state.blobs else 404)

        def do_POST(self):
            self._record()
            self._read_body()
            self._send(202, headers={"Location": "/v2/upload/session-1?state=s"})

        def do_PUT(self):
            self._record()
            body = self._read_body()
            if self.path.startswith("/v2/upload/"):
                digest = self.path.split("digest=", 1)[1].replace("%3A", ":")
                assert digest == f"sha256:{hashlib.sha256(body).hexdigest()}"
                state.blobs[digest] = body
            else:
                state.manifests[self.path.rsplit("/", 1)[1]] = body
            self._send(201)

        def _read_body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    return Handler


@contextmanager
def _serve(tls_context=None):
    state = _Registry()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    server.timeout = SERVER_TIMEOUT_SECONDS
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(SERVER_TIMEOUT_SECONDS)


@pytest.fixture
def certificate_authority(tmp_path):
    ca = trustme.CA()
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1", "localhost").configure_cert(server_context)
    ca_file = tmp_path / "registry-ca.pem"
    ca.cert_pem.write_to_path(str(ca_file))
    return server_context, str(ca_file)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("pycontainer.registry_client.time.sleep", lambda _delay: None)


def _http_error(url, code, headers):
    message = Message()
    for key, value in headers.items():
        message[key] = value
    return urllib.error.HTTPError(url, code, "error", message, io.BytesIO(b""))


def test_insecure_loopback_registry_round_trip_with_credentials(tmp_path):
    content = b"layer-bytes"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    blob = tmp_path / "blob"
    blob.write_bytes(content)
    manifest = json.dumps({"schemaVersion": 2, "mediaType": MANIFEST_TYPE}).encode()

    with _serve() as (port, state):
        client = RegistryClient(
            f"127.0.0.1:{port}",
            "team/app",
            username="synthetic-user",
            password="synthetic-password",
            insecure=True,
        )
        assert client.base_url == f"http://127.0.0.1:{port}/v2"
        assert client.push_blob(digest, blob)
        assert client.push_manifest("v1", manifest)
        pulled, pulled_digest = client.pull_manifest("v1")
        destination = tmp_path / "pulled"
        assert client.pull_blob(digest, destination, len(content))

    assert pulled == json.loads(manifest)
    assert pulled_digest == f"sha256:{hashlib.sha256(manifest).hexdigest()}"
    assert destination.read_bytes() == content
    assert [method for method, _, _ in state.requests] == [
        "HEAD", "POST", "PUT", "PUT", "GET", "GET",
    ]
    assert all(auth and auth.startswith("Basic ") for _, _, auth in state.requests)


def test_unlisted_http_registry_keeps_https_and_fails(no_sleep):
    with _serve() as (port, state):
        client = RegistryClient(f"127.0.0.1:{port}", "team/app")
        with pytest.raises(RegistryRequestError, match="failed after 3 attempt"):
            client.pull_manifest("latest")

    assert client.base_url.startswith("https://")
    assert state.requests == []


@pytest.mark.parametrize(
    "credentials",
    [
        {"username": "synthetic-user", "password": "synthetic-password"},
        {"auth_token": "synthetic-token"},
        {"password": "synthetic-token"},
    ],
)
def test_non_loopback_http_registry_refuses_credentials(credentials):
    with pytest.raises(RegistryAuthenticationError, match="plain HTTP"):
        RegistryClient("registry.internal:5000", "team/app", insecure=True, **credentials)


def test_non_loopback_http_registry_refuses_token_challenge():
    client = RegistryClient("registry.internal:5000", "team/app", insecure=True)
    requests = []

    def fake_open(request, timeout):
        requests.append(request.full_url)
        raise _http_error(
            request.full_url,
            401,
            {"Www-Authenticate": 'Bearer realm="https://auth.example/token"'},
        )

    client._opener.open = fake_open
    with pytest.raises(RegistryAuthenticationError, match="plain HTTP"):
        client.pull_manifest("latest")

    assert requests == ["http://registry.internal:5000/v2/team/app/manifests/latest"]


def test_https_registry_rejects_http_token_realm():
    client = RegistryClient(
        "registry.example", "team/app", username="user", password="secret"
    )
    requests = []

    def fake_open(request, timeout):
        requests.append(request.full_url)
        raise _http_error(
            request.full_url,
            401,
            {"Www-Authenticate": 'Bearer realm="http://auth.example/token"'},
        )

    client._opener.open = fake_open
    with pytest.raises(RegistryAuthenticationError, match="HTTPS token realm"):
        client.pull_manifest("latest")

    assert all(url.startswith("https://registry.example/") for url in requests)


def test_https_registry_rejects_http_upload_location(monkeypatch):
    client = RegistryClient("registry.example", "team/app", auth_token="secret")
    monkeypatch.setattr(
        client,
        "_make_request",
        lambda *_args, **_kwargs: (202, b"", {"Location": "http://registry.example/u"}),
    )

    with pytest.raises(RegistryRequestError, match="downgrade"):
        client.initiate_blob_upload()


@pytest.mark.parametrize("path", ["manifests/latest", "blobs/sha256:" + "a" * 64])
def test_https_to_http_redirect_is_blocked(
    tmp_path, certificate_authority, path
):
    server_context, ca_file = certificate_authority
    with _serve() as (http_port, http_state), _serve(server_context) as (
        https_port,
        https_state,
    ):
        https_state.redirect = (302, f"http://127.0.0.1:{http_port}/stolen")
        client = RegistryClient(
            f"127.0.0.1:{https_port}", "team/app", auth_token="secret", ca_file=ca_file
        )
        with pytest.raises(RegistryRequestError, match="downgrade"):
            if path.startswith("manifests"):
                client.pull_manifest("latest")
            else:
                client.pull_blob(path.split("/", 1)[1], tmp_path / "blob", 1)

    assert https_state.requests
    assert http_state.requests == []


def test_cross_origin_redirect_drops_authorization():
    manifest = json.dumps({"schemaVersion": 2}).encode()
    with _serve() as (target_port, target), _serve() as (source_port, source):
        target.manifests["latest"] = manifest
        source.redirect = (307, f"http://127.0.0.1:{target_port}/v2/team/app/manifests/latest")
        client = RegistryClient(
            f"127.0.0.1:{source_port}", "team/app", auth_token="secret", insecure=True
        )
        client.pull_manifest("latest")

    assert source.requests[0][2] == "Bearer secret"
    assert target.requests[0][2] is None


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("https://registry.example:99999/v2/", "malformed"),
        ("ftp://registry.example/v2/", "unsupported scheme"),
        ("http://registry.example/v2/", "downgrade"),
    ],
)
def test_safe_redirect_rejects_unsafe_targets(target, message):
    request = urllib.request.Request(
        "https://registry.example/v2/", headers={"Authorization": "Bearer secret"}
    )
    with pytest.raises(RegistryRequestError, match=message):
        SafeRedirect().redirect_request(request, None, 302, "Found", {}, target)


@pytest.mark.parametrize(
    ("target", "keeps_authorization"),
    [
        ("http://127.0.0.1:5000/v2/other", True),
        ("https://127.0.0.1:5000/v2/other", False),
        ("http://127.0.0.1:5001/v2/other", False),
        ("http://localhost:5000/v2/other", False),
    ],
)
def test_safe_redirect_keeps_authorization_only_for_same_origin(
    target, keeps_authorization
):
    request = urllib.request.Request(
        "http://127.0.0.1:5000/v2/", headers={"Authorization": "Bearer secret"}
    )
    redirected = SafeRedirect().redirect_request(
        request, None, 307, "Temporary Redirect", {}, target
    )
    assert redirected.has_header("Authorization") is keeps_authorization


def test_custom_ca_is_required_and_trusted(certificate_authority, no_sleep):
    server_context, ca_file = certificate_authority
    manifest = json.dumps({"schemaVersion": 2}).encode()
    with _serve(server_context) as (port, state):
        state.manifests["latest"] = manifest
        default_client = RegistryClient(f"127.0.0.1:{port}", "team/app")
        with pytest.raises(RegistryRequestError, match="URLError"):
            default_client.pull_manifest("latest")
        assert state.requests == []

        trusted = RegistryClient(f"127.0.0.1:{port}", "team/app", ca_file=ca_file)
        pulled, _ = trusted.pull_manifest("latest")

    assert trusted.base_url.startswith("https://")
    assert pulled == {"schemaVersion": 2}


@pytest.mark.parametrize("contents", [None, b"not a certificate"])
def test_missing_or_invalid_ca_file_is_config_error(tmp_path, contents):
    ca_file = tmp_path / "ca.pem"
    if contents is not None:
        ca_file.write_bytes(contents)

    with pytest.raises(ValueError, match="Registry CA file"):
        BuildConfig(registry_ca_file=str(ca_file))
    with pytest.raises(ValueError, match="Registry CA file"):
        RegistryClient("registry.example", "team/app", ca_file=str(ca_file))


def test_insecure_registry_entries_are_normalized_and_validated():
    config = BuildConfig(
        insecure_registries=["Localhost:5000", "localhost:5000", "[::1]:5000", "reg"]
    )
    assert config.insecure_registries == ["localhost:5000", "[::1]:5000", "reg"]

    for entry in [
        "http://reg:5000", "reg/path", "user@reg", "reg:0", "reg:99999",
        "", "reg :5000", "::1", "reg:5000?x",
    ]:
        with pytest.raises(ValueError, match="insecure registry"):
            BuildConfig(insecure_registries=[entry])


@pytest.mark.parametrize(
    "host", ["localhost", "127.0.0.1", "127.8.9.10", "[::1]", "::ffff:127.0.0.1"]
)
def test_loopback_detection_accepts_loopback_literals(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["registry.local", "10.0.0.1", "localhost.evil", ""])
def test_loopback_detection_rejects_other_hosts(host):
    assert not is_loopback_host(host)


def _capture_cli_config(monkeypatch, argv):
    configs = []

    class FakeImageBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", FakeImageBuilder)
    monkeypatch.setattr(sys, "argv", ["pycontainer", *argv])
    cli.main()
    return configs[0]


def test_cli_and_toml_accept_transport_options(tmp_path, monkeypatch, certificate_authority):
    _, ca_file = certificate_authority
    config = _capture_cli_config(
        monkeypatch,
        [
            "build", "--base-image", "python:3.11-slim",
            "--insecure-registry", "localhost:5000",
            "--insecure-registry", "Registry.Lan:5000",
            "--registry-ca-file", ca_file,
        ],
    )
    assert config.insecure_registries == ["localhost:5000", "registry.lan:5000"]
    assert config.registry_ca_file == ca_file

    toml = tmp_path / "pycontainer.toml"
    toml.write_text(
        "[build]\n"
        'insecure-registries = ["localhost:5000"]\n'
        f"registry_ca_file = {json.dumps(ca_file)}\n"
    )
    loaded = config_from_file(toml)
    assert loaded.insecure_registries == ["localhost:5000"]
    assert loaded.registry_ca_file == ca_file


@pytest.mark.parametrize(
    "argv",
    [
        ["--insecure-registry", "http://localhost:5000"],
        ["--registry-ca-file", "missing-ca.pem"],
    ],
)
def test_cli_rejects_invalid_transport_options(monkeypatch, capsys, tmp_path, argv):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        _capture_cli_config(
            monkeypatch, ["build", "--base-image", "python:3.11-slim", *argv]
        )
    assert error.value.code == 2
    assert "registry" in capsys.readouterr().err.lower()


def test_builder_warns_once_per_insecure_host(tmp_path, caplog):
    config = BuildConfig(
        context_dir=str(tmp_path), insecure_registries=["localhost:5000"]
    )
    builder = ImageBuilder(config)
    auth = type("Auth", (), {"bearer_token": None, "username": None, "password": None})

    with caplog.at_level(logging.WARNING, logger="pycontainer.builder"):
        first = builder._registry_client("localhost:5000", "base", auth)
        second = builder._registry_client("LOCALHOST:5000", "app", auth)
        secure = builder._registry_client("registry.example", "app", auth)

    assert first.base_url.startswith("http://")
    assert second.base_url.startswith("http://")
    assert secure.base_url.startswith("https://")
    warnings = [r for r in caplog.records if "insecure registry" in r.getMessage()]
    assert len(warnings) == 1
