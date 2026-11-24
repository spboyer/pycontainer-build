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
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pycontainer import cli
from pycontainer.auth import RegistryAuth
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.registry_client import (
    RegistryAuthenticationError,
    RegistryClient,
    RegistryRequestError,
    NoRedirect,
    SafeRedirect,
    _origin,
    _safe_url,
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
        self.token_realm = None
        self.token_response = None


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
            if (
                state.token_realm
                and self.headers.get("Authorization") != "Bearer acquired-token"
            ):
                self._send(
                    401,
                    headers={
                        "Www-Authenticate": f'Bearer realm="{state.token_realm}"'
                    },
                )
            elif state.token_response is not None:
                self._send(200, json.dumps(state.token_response).encode())
            elif state.redirect:
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


@pytest.fixture
def opener_constructions(monkeypatch):
    original = urllib.request.build_opener
    calls = []

    def construct(*handlers):
        calls.append(handlers)
        return original(*handlers)

    def unexpected_urlopen(*_args, **_kwargs):
        pytest.fail("Registry request bypassed its configured opener")

    monkeypatch.setattr(urllib.request, "build_opener", construct)
    monkeypatch.setattr(urllib.request, "urlopen", unexpected_urlopen)
    return calls


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
        "http://127.0.0.1:5000/v2/",
        headers={
            "Authorization": "Bearer secret",
            "Proxy-Authorization": "Basic proxy-secret",
            "Cookie": "session=secret",
        },
    )
    redirected = SafeRedirect(insecure=True).redirect_request(
        request, None, 307, "Temporary Redirect", {}, target
    )
    for header in ("Authorization", "Proxy-Authorization", "Cookie"):
        assert redirected.has_header(header.capitalize()) is keeps_authorization


@pytest.mark.parametrize(
    "entrypoint", ["request", "blob", "realm", "upload", "redirect", "blob-redirect"]
)
@pytest.mark.parametrize(
    "url",
    [
        "https://user:url-password@registry.example/path?token=query-secret",
        "https://user:url-password@[broken/path?token=query-secret",
        "https://registry.example:99999/path?token=query-secret",
        "https://registry.example:0/path?token=query-secret",
        "https://registry.example:/path?token=query-secret",
        "https://registry.example/path#query-secret",
        "https://registry.example/path#",
        "https://registry.example/\tpath?token=query-secret",
        "https:///path?token=query-secret",
        "ftp://registry.example/path?token=query-secret",
        "http://registry.example/path?token=query-secret",
        "https://registry.example/%broken?token=query-secret",
    ],
)
def test_all_transport_entrypoints_reject_unsafe_urls(monkeypatch, entrypoint, url):
    client = RegistryClient("registry.example", "team/app")

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("Unsafe URL reached an opener")

    monkeypatch.setattr(client._opener, "open", unexpected_request)
    monkeypatch.setattr(client, "_get_blob_opener", unexpected_request)
    with pytest.raises(RegistryRequestError) as error:
        if entrypoint == "request":
            client._make_request("GET", url)
        elif entrypoint == "blob":
            client._open_blob_response(url)
        elif entrypoint == "realm":
            client._get_bearer_token({"realm": url})
        elif entrypoint == "blob-redirect":
            client._open_blob_redirect(f"{client.base_url}/blob", url)
        elif entrypoint == "upload":
            monkeypatch.setattr(
                client, "_make_request",
                lambda *_args, **_kwargs: (202, b"", {"Location": url}),
            )
            client.initiate_blob_upload()
        else:
            request = urllib.request.Request(f"{client.base_url}/manifest")
            headers = Message()
            headers["Location"] = url
            SafeRedirect().http_error_302(request, None, 302, "Found", headers)
    assert "url-password" not in str(error.value)
    assert "query-secret" not in str(error.value)


@pytest.mark.parametrize(
    "registry",
    ["user:url-password@registry.example", "[broken", "registry.example:0",
     "registry.example/path", "http://registry.example", "registry.example/"],
)
def test_constructor_rejects_invalid_registry_authorities(registry):
    with pytest.raises(RegistryRequestError) as error:
        RegistryClient(registry, "team/app")
    assert "url-password" not in str(error.value)


def test_origin_uses_actual_port_and_normalizes_default_port():
    assert _origin("https://registry.example") == _origin("https://registry.example:443")
    assert _origin("http://registry.example:0") != _origin("http://registry.example")
    client = RegistryClient("registry.example", "team/app", auth_token="secret")
    assert client._authorization_headers("https://registry.example:443/v2") == {
        "Authorization": "Bearer secret"
    }
    assert client._authorization_headers("https://registry.example:444/v2") == {}


@pytest.mark.parametrize(
    "url",
    [
        "https://user:url-password@registry.example/path?token=query-secret",
        "https://user:url-password@[broken?token=query-secret",
        "https://user:url-password@registry.example:bad?token=query-secret",
        "user:url-password@registry.example?token=query-secret",
    ],
)
def test_error_url_redaction_is_safe_for_malformed_urls(url):
    rendered = _safe_url(url)
    assert "url-password" not in rendered
    assert "query-secret" not in rendered


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


@pytest.mark.parametrize("insecure", [False, True])
@pytest.mark.parametrize("redirect", [False, True])
def test_blob_transfers_reuse_ca_aware_openers(
    tmp_path, certificate_authority, opener_constructions, insecure, redirect
):
    server_context, ca_file = certificate_authority
    content = b"reusable-opener-layer"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    source_context = None if insecure else server_context
    with _serve(server_context) as (target_port, target), _serve(
        source_context
    ) as (source_port, source):
        target.blobs[digest] = content
        source.blobs[digest] = content
        if redirect:
            source.redirect = (
                307, f"https://127.0.0.1:{target_port}/v2/team/app/blobs/{digest}"
            )
        client = RegistryClient(
            f"127.0.0.1:{source_port}", "team/app", auth_token="synthetic-token",
            insecure=insecure, ca_file=ca_file,
        )

        assert len(opener_constructions) == 1
        for attempt in range(2):
            destination = tmp_path / f"blob-{attempt}"
            assert client.pull_blob(digest, destination, len(content))
            assert destination.read_bytes() == content

    assert len(source.requests) == 2
    assert all(auth == "Bearer synthetic-token" for _, _, auth in source.requests)
    assert len(target.requests) == (2 if redirect else 0)
    assert all(auth is None for _, _, auth in target.requests)
    assert len(opener_constructions) == 2
    contexts = [
        handler._context for handlers in opener_constructions
        for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler)
    ]
    assert contexts == [client._ssl_context, client._ssl_context]
    assert any(isinstance(handler, NoRedirect) for handler in opener_constructions[1])


def test_concurrent_first_use_reuses_ca_aware_openers(
    tmp_path, certificate_authority, opener_constructions
):
    server_context, ca_file = certificate_authority
    content = b"concurrent-layer"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    with _serve(server_context) as (port, state):
        state.blobs[digest] = content
        state.manifests["latest"] = json.dumps({"schemaVersion": 2}).encode()
        client = RegistryClient(f"127.0.0.1:{port}", "team/app", ca_file=ca_file)
        start = threading.Barrier(4)

        assert len(opener_constructions) == 1

        def transfer(index):
            start.wait(timeout=SERVER_TIMEOUT_SECONDS)
            if index % 2:
                return client.pull_manifest("latest")[0] == {"schemaVersion": 2}
            destination = tmp_path / f"concurrent-{index}"
            return (
                client.pull_blob(digest, destination, len(content))
                and destination.read_bytes() == content
            )

        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(transfer, index) for index in range(4)]
            assert all(future.result(timeout=20) for future in futures)
    assert len(state.requests) == 4
    assert len(opener_constructions) == 2


def test_push_only_client_does_not_construct_blob_opener(tmp_path, opener_constructions):
    content = b"push-only"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    source = tmp_path / "push-source"
    source.write_bytes(content)
    with _serve() as (port, state):
        client = RegistryClient(f"127.0.0.1:{port}", "team/app", insecure=True)
        assert client.push_blob(digest, source)
        assert client.push_manifest("latest", json.dumps({"schemaVersion": 2}).encode())
    assert len(opener_constructions) == 1
    assert state.blobs[digest] == content


def test_concurrent_ca_token_exchange_is_acquired_once(certificate_authority):
    server_context, ca_file = certificate_authority
    with _serve(server_context) as (auth_port, auth), _serve(server_context) as (
        registry_port, registry
    ):
        auth.token_response = {"token": "acquired-token"}
        registry.token_realm = f"https://127.0.0.1:{auth_port}/token"
        registry.manifests["latest"] = json.dumps({"schemaVersion": 2}).encode()
        content = b"authenticated-blob"
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        registry.blobs[digest] = content
        client = RegistryClient(
            f"127.0.0.1:{registry_port}", "team/app",
            username="synthetic-user", password="synthetic-password", ca_file=ca_file,
        )
        start = threading.Barrier(4)

        def pull(index):
            start.wait(timeout=SERVER_TIMEOUT_SECONDS)
            if index % 2:
                with client._open_blob_response(
                    f"{client.base_url}/team/app/blobs/{digest}"
                ) as response:
                    return response.read() == content
            return client.pull_manifest("latest")[0] == {"schemaVersion": 2}

        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(pull, index) for index in range(4)]
            assert all(
                future.result(timeout=20) for future in futures
            )
    assert len(auth.requests) == 1
    assert auth.requests[0][2].startswith("Basic ")


@pytest.mark.parametrize("location", ["https://[broken", "https://user:secret@localhost/x"])
def test_real_blob_redirect_rejects_malformed_location(location, tmp_path):
    digest = f"sha256:{hashlib.sha256(b'x').hexdigest()}"
    with _serve() as (port, state):
        state.redirect = (307, location)
        client = RegistryClient(f"127.0.0.1:{port}", "team/app", insecure=True)
        with pytest.raises(RegistryRequestError) as error:
            client.pull_blob(digest, tmp_path / "invalid-redirect", 1)
    assert "secret" not in str(error.value)
    assert len(state.requests) == 1
    assert not (tmp_path / "invalid-redirect").exists()


@pytest.mark.parametrize("path", ["manifest", "blob", "token"])
@pytest.mark.parametrize("trusted", [False, True])
def test_redirect_and_token_targets_require_custom_ca(
    tmp_path, certificate_authority, no_sleep, path, trusted
):
    server_context, ca_file = certificate_authority
    content = b"private-ca-layer"
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    with _serve(server_context) as (target_port, target), _serve() as (
        source_port, source
    ):
        target.manifests["latest"] = json.dumps({"schemaVersion": 2}).encode()
        target.blobs[digest] = content
        if path == "token":
            target.token_response = {"token": "acquired-token"}
            source.token_realm = f"https://127.0.0.1:{target_port}/token"
            source.manifests["latest"] = json.dumps({"schemaVersion": 2}).encode()
        else:
            suffix = "manifests/latest" if path == "manifest" else f"blobs/{digest}"
            source.redirect = (
                307, f"https://127.0.0.1:{target_port}/v2/team/app/{suffix}"
            )
        client = RegistryClient(
            f"127.0.0.1:{source_port}", "team/app", insecure=True,
            username="synthetic-user", password="synthetic-password",
            ca_file=ca_file if trusted else None,
        )

        def transfer():
            if path == "blob":
                destination = tmp_path / "ca-blob"
                assert client.pull_blob(digest, destination, len(content))
                assert destination.read_bytes() == content
            else:
                assert client.pull_manifest("latest")[0] == {"schemaVersion": 2}

        if trusted:
            transfer()
        else:
            with pytest.raises(RegistryRequestError):
                transfer()

    if trusted:
        assert target.requests
        if path == "token":
            assert target.requests[0][2].startswith("Basic ")
            assert source.requests[-1][2] == "Bearer acquired-token"
        else:
            assert all(auth is None for _, _, auth in target.requests)
    else:
        assert target.requests == []


@pytest.mark.parametrize(
    ("expected_size", "cap", "allowed"),
    [
        (None, 64, True),
        (None, 8, False),
        ("exact", 1024, True),
        ("short", 1024, False),
        ("exact", 8, False),
    ],
)
def test_manifest_read_is_bounded_through_token_replay(
    monkeypatch, expected_size, cap, allowed
):
    manifest = json.dumps({"schemaVersion": 2}).encode()
    size = {"exact": len(manifest), "short": len(manifest) - 1}.get(expected_size)
    monkeypatch.setattr("pycontainer.registry_client.MAX_MANIFEST_BYTES", cap)
    with _serve() as (auth_port, auth), _serve() as (port, state):
        auth.token_response = {"token": "acquired-token"}
        state.token_realm = f"http://127.0.0.1:{auth_port}/token"
        state.manifests["latest"] = manifest
        client = RegistryClient(
            f"127.0.0.1:{port}", "team/app", insecure=True,
            username="synthetic-user", password="synthetic-password",
        )
        if allowed:
            assert client.pull_manifest("latest", expected_size=size)[0] == {
                "schemaVersion": 2
            }
        else:
            with pytest.raises(RegistryRequestError, match="exceeds"):
                client.pull_manifest("latest", expected_size=size)

    assert len(state.requests) == 2
    assert state.requests[0][2].startswith("Basic ")
    assert state.requests[1][2] == "Bearer acquired-token"


def test_environment_proxies_are_not_used(monkeypatch):
    manifest = json.dumps({"schemaVersion": 2}).encode()
    with _serve() as (proxy_port, proxy), _serve() as (port, state):
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.setenv(name, f"http://proxy-user:proxy-secret@127.0.0.1:{proxy_port}")
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")
        state.manifests["latest"] = manifest
        client = RegistryClient(f"127.0.0.1:{port}", "team/app", insecure=True)
        assert client.pull_manifest("latest")[0] == {"schemaVersion": 2}
    assert proxy.requests == []
    assert state.requests[0][2] is None


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


def test_cli_transport_options_preserve_and_override_toml(
    tmp_path, monkeypatch, certificate_authority
):
    _, ca_file = certificate_authority
    toml = tmp_path / "pycontainer.toml"
    toml.write_text(
        "[build]\n"
        'insecure-registries = ["registry.example:5000"]\n'
        f"registry-ca-file = {json.dumps(ca_file)}\n"
        "[build.security.kubernetes]\n"
        "read-only-root-filesystem = true\n"
    )

    preserved = _capture_cli_config(monkeypatch, ["build", "--config", str(toml)])
    overridden = _capture_cli_config(
        monkeypatch,
        ["build", "--config", str(toml), "--insecure-registry", "localhost:5000"],
    )

    assert preserved.insecure_registries == ["registry.example:5000"]
    assert overridden.insecure_registries == ["localhost:5000"]
    assert preserved.registry_ca_file == overridden.registry_ca_file == ca_file
    expected_security = {"kubernetes": {"read_only_root_filesystem": True}}
    assert preserved.security == overridden.security == expected_security


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


def test_insecure_registry_warning_redacts_build_argument(tmp_path, caplog):
    registry = "private-registry.example:5000"
    config = BuildConfig(
        context_dir=str(tmp_path), insecure_registries=[registry],
        build_args={"REGISTRY": "private-registry"},
    )
    builder = ImageBuilder(config)
    auth = RegistryAuth()

    with caplog.at_level(logging.WARNING, logger="pycontainer.builder"):
        client = builder._registry_client(registry, "app", auth)

    assert client.base_url == f"http://{registry}/v2"
    assert "private-registry" not in caplog.text
    assert "<redacted>.example:5000" in caplog.text


@pytest.mark.parametrize("with_build_args", [False, True])
@pytest.mark.parametrize("transport", ["http", "custom-ca-https"])
def test_builder_pull_and_push_honor_registry_transport(
    tmp_path, monkeypatch, certificate_authority, transport, with_build_args
):
    server_context, ca_file = certificate_authority
    tls_context = server_context if transport == "custom-ca-https" else None
    monkeypatch.setattr(
        "pycontainer.builder.get_registry_auth",
        lambda *_args, **_kwargs: RegistryAuth(),
    )

    image_config = {"architecture": "amd64", "os": "linux"}
    config_bytes = json.dumps(image_config).encode()
    config_digest = f"sha256:{hashlib.sha256(config_bytes).hexdigest()}"
    manifest_bytes = json.dumps({
        "schemaVersion": 2,
        "mediaType": MANIFEST_TYPE,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [],
    }).encode()
    manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"

    with _serve(tls_context) as (port, state):
        registry = f"127.0.0.1:{port}"
        state.manifests["latest"] = manifest_bytes
        state.blobs[config_digest] = config_bytes
        output = tmp_path / "image"
        builder = ImageBuilder(BuildConfig(
            base_image="${REGISTRY}/team/base:latest" if with_build_args else f"{registry}/team/base:latest",
            tag="${REGISTRY}/team/app:v1" if with_build_args else f"{registry}/team/app:v1",
            context_dir=str(tmp_path),
            output_dir=str(output),
            insecure_registries=[registry] if transport == "http" else [],
            registry_ca_file=ca_file if tls_context else None,
            build_args={"REGISTRY": registry} if with_build_args else None,
        ))

        layers, pulled_config = builder._pull_base_image(
            tmp_path / "base-blobs", "linux", "amd64"
        )
        assert layers == []
        assert pulled_config == image_config

        blobs = output / "blobs" / "sha256"
        blobs.mkdir(parents=True)
        (blobs / config_digest.split(":", 1)[1]).write_bytes(config_bytes)
        (blobs / manifest_digest.split(":", 1)[1]).write_bytes(manifest_bytes)
        builder.config_digest = config_digest
        builder.manifest_digest = manifest_digest
        builder.layers = []
        state.blobs.clear()

        assert builder.push(show_progress=False) == f"{registry}/team/app:v1"
        assert state.blobs[config_digest] == config_bytes
        assert state.manifests["v1"] == manifest_bytes
