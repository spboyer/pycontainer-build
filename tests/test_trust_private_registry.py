"""Authenticated signature discovery through the strict, credential-isolated client."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading

import pytest

from pycontainer import base_trust
from pycontainer import builder as builder_module
from pycontainer import registry_client as registry_module
from pycontainer import trust_notation as notation
from pycontainer.auth import RegistryAuth
from pycontainer.base_trust import TrustVerificationError, verify_base_image
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.registry_client import RegistryClient


REPOSITORY = "registry.example/team/base"
USERNAME = "synthetic-user"
PASSWORD = "synthetic-registry-secret"
BASIC = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
CERT = b"synthetic signer certificate"


def _encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def _digest(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _descriptor(content, media_type):
    return {"mediaType": media_type, "digest": _digest(content), "size": len(content)}


def _cose(cert):
    # COSE_Sign1: tag 18, [protected bstr {1:-37}, {33:[cert]}, payload, signature].
    protected = b"\xa1\x01\x38\x24"
    head = bytes([0x40 | len(protected)]) + protected
    chain = b"\xa1\x18\x21\x81\x58" + bytes([len(cert)]) + cert
    return b"\xd2\x84" + head + chain + b"\x47payload" + b"\x49signature"


class _Server:
    """Loopback registry that requires Basic credentials on every request."""

    def __init__(self, credentials=BASIC, repository="team/base"):
        self.repository = repository
        self.routes = {}
        self.requests = []
        self.credentials = credentials
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                server.requests.append((path, self.headers.get("Authorization")))
                if server.credentials and self.headers.get("Authorization") != server.credentials:
                    self._reply(401, b"{}", {"WWW-Authenticate": 'Basic realm="test"'})
                    return
                status, body, headers = server.routes.get(path, (404, b"{}", {}))
                self._reply(status, body, headers)

            def _reply(self, status, body, headers):
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def manifest(self, reference, content, media_type):
        self.routes[f"/v2/{self.repository}/manifests/{reference}"] = (
            200, content, {"Content-Type": media_type, "Docker-Content-Digest": _digest(content)})

    def blob(self, content):
        self.routes[f"/v2/{self.repository}/blobs/{_digest(content)}"] = (200, content, {})


@pytest.fixture
def registry():
    server = _Server()
    config = _encoded({"os": "linux", "architecture": "amd64", "config": {}})
    layer = b"base layer"
    subject = _encoded({
        "schemaVersion": 2, "mediaType": MANIFEST,
        "config": _descriptor(config, "application/vnd.oci.image.config.v1+json"),
        "layers": [_descriptor(layer, "application/vnd.oci.image.layer.v1.tar")],
    })
    server.subject = subject
    server.subject_digest = _digest(subject)
    server.base_blobs = {_digest(config), _digest(layer)}
    server.manifest("latest", subject, MANIFEST)
    server.manifest(server.subject_digest, subject, MANIFEST)
    yield server
    server.close()


def _client(server, **options):
    options = {"username": USERNAME, "password": PASSWORD, **options}
    client = RegistryClient("registry.example", "team/base", strict_auth=True, **options)
    client.base_url = server.url + "/v2"
    return client


def _serve_cosign(server):
    config = b'{"rootfs":{"type":"layers","diff_ids":[]}}'
    payload = _encoded({"critical": {"image": {"docker-manifest-digest": server.subject_digest}}})
    signatures = _encoded({
        "schemaVersion": 2, "mediaType": MANIFEST,
        "config": _descriptor(config, "application/vnd.oci.image.config.v1+json"),
        "layers": [_descriptor(payload, "application/vnd.dev.cosign.simplesigning.v1+json")],
    })
    server.manifest(f"sha256-{server.subject_digest[7:]}.sig", signatures, MANIFEST)
    server.blob(config)
    server.blob(payload)
    return {_digest(config), _digest(payload)}


def _serve_notation(server, cert=CERT):
    envelope = _cose(cert)
    subject = {"mediaType": MANIFEST, "digest": server.subject_digest, "size": len(server.subject)}
    signature = _encoded({
        "schemaVersion": 2, "mediaType": MANIFEST, "artifactType": notation._SIGNATURE,
        "config": {"mediaType": "application/vnd.oci.empty.v1+json",
                   "digest": _digest(b"{}"), "size": 2},
        "layers": [_descriptor(envelope, "application/cose")], "subject": subject,
    })
    index = _encoded({"schemaVersion": 2, "mediaType": INDEX, "manifests": [
        dict(_descriptor(signature, MANIFEST), artifactType=notation._SIGNATURE)]})
    server.routes[f"/v2/team/base/referrers/{server.subject_digest}"] = (
        200, index, {"Content-Type": INDEX})
    server.manifest(_digest(signature), signature, MANIFEST)
    server.blob(envelope)
    return envelope


def _assert_no_secret(values):
    for value in values:
        assert PASSWORD not in value and BASIC.split()[1] not in value


def _credential_environment(monkeypatch):
    for name in ("REGISTRY_PASSWORD", "REGISTRY_TOKEN", "GITHUB_TOKEN",
                 "DOCKER_PASSWORD", "NOTATION_PASSWORD", "COSIGN_PASSWORD"):
        monkeypatch.setenv(name, PASSWORD)


def _cosign_policy():
    return {"provider": "cosign", "repository": REPOSITORY,
            "identity": "builder@example.com", "issuer": "https://issuer.example"}


def _notation_policy(tmp_path):
    root = tmp_path / "root.crt"
    root.write_bytes(b"synthetic root")
    return {"provider": "notation", "repository": REPOSITORY, "identity": "CN=Exact,O=Example",
            "signer_fingerprint": hashlib.sha256(CERT).hexdigest(),
            "trust_root": str(root), "binary": sys.executable}


def _verify(server, policy, client=None):
    verify_base_image(policy, REPOSITORY, server.subject_digest, json.loads(server.subject),
                      server.subject, client or _client(server))


def test_cosign_private_signatures_use_authenticated_client_only(registry, monkeypatch):
    signature_blobs = _serve_cosign(registry)
    calls = []

    def run(command, environment):
        calls.append(command)
        _assert_no_secret(command)
        _assert_no_secret(environment.values())
        layout = command[-1]
        files = [path for path in __import__("pathlib").Path(layout).rglob("*") if path.is_file()]
        _assert_no_secret(path.read_bytes().decode("latin-1") for path in files)
        return _encoded([{"critical": {
            "identity": {"docker-reference": REPOSITORY},
            "image": {"docker-manifest-digest": registry.subject_digest},
            "type": "cosign container image signature"}}])

    _credential_environment(monkeypatch)
    monkeypatch.setattr(base_trust, "_run_cosign", run)
    _verify(registry, _cosign_policy())
    assert len(calls) == 1
    assert all(auth == BASIC for _, auth in registry.requests)
    fetched = {path.rsplit("/", 1)[1] for path, _ in registry.requests if "/blobs/" in path}
    assert fetched == signature_blobs
    assert not fetched & registry.base_blobs


def test_notation_private_signatures_use_authenticated_client_only(registry, monkeypatch, tmp_path):
    envelope = _serve_notation(registry)
    calls = []

    def run(args, environment):
        calls.append(args)
        _assert_no_secret(args)
        _assert_no_secret(environment.values())
        return ("Successfully verified signature for " + args[-1] + "\n").encode()

    _credential_environment(monkeypatch)
    monkeypatch.setattr(notation, "_run", run)
    _verify(registry, _notation_policy(tmp_path))
    assert len(calls) == 1
    assert all(auth == BASIC for _, auth in registry.requests)
    assert [path for path, _ in registry.requests if "/blobs/" in path] == [
        f"/v2/team/base/blobs/{_digest(envelope)}"]


def test_notation_private_discovery_falls_back_to_tag_schema(registry, monkeypatch, tmp_path):
    _serve_notation(registry)
    status, index, headers = registry.routes.pop(
        f"/v2/team/base/referrers/{registry.subject_digest}")
    registry.manifest(f"sha256-{registry.subject_digest[7:]}", index, INDEX)
    monkeypatch.setattr(notation, "_run", lambda args, env: (
        "Successfully verified signature for " + args[-1] + "\n").encode())
    _verify(registry, _notation_policy(tmp_path))
    paths = [path for path, _ in registry.requests]
    assert paths.index(f"/v2/team/base/referrers/{registry.subject_digest}") < paths.index(
        f"/v2/team/base/manifests/sha256-{registry.subject_digest[7:]}")


@pytest.mark.parametrize("provider", ["cosign", "notation"])
@pytest.mark.parametrize("credentials", ["missing", "wrong"])
def test_private_signatures_without_valid_credentials_fail_closed(
        registry, monkeypatch, tmp_path, provider, credentials):
    _serve_cosign(registry)
    _serve_notation(registry)
    monkeypatch.setattr(base_trust, "_run_cosign", lambda *args: pytest.fail("verifier ran"))
    monkeypatch.setattr(notation, "_run", lambda *args: pytest.fail("verifier ran"))
    options = {"username": None, "password": None} if credentials == "missing" else {
        "password": "wrong-password"}
    policy = _cosign_policy() if provider == "cosign" else _notation_policy(tmp_path)
    with pytest.raises(TrustVerificationError):
        _verify(registry, policy, _client(registry, **options))
    assert not any("/blobs/" in path for path, _ in registry.requests)


@pytest.mark.parametrize("provider", ["cosign", "notation"])
def test_redirected_signature_blob_never_receives_registry_credentials(
        registry, monkeypatch, tmp_path, provider):
    storage = _Server(credentials=None)
    try:
        if provider == "cosign":
            blobs = sorted(_serve_cosign(registry))
            policy = _cosign_policy()
            monkeypatch.setattr(base_trust, "_run_cosign", lambda *args: _encoded([{"critical": {
                "identity": {"docker-reference": REPOSITORY},
                "image": {"docker-manifest-digest": registry.subject_digest},
                "type": "cosign container image signature"}}]))
        else:
            blobs = [_digest(_serve_notation(registry))]
            policy = _notation_policy(tmp_path)
            monkeypatch.setattr(notation, "_run", lambda args, env: (
                "Successfully verified signature for " + args[-1] + "\n").encode())
        for blob in blobs:
            path = f"/v2/team/base/blobs/{blob}"
            storage.routes[f"/storage/{blob}"] = registry.routes[path]
            registry.routes[path] = (307, b"", {"Location": f"{storage.url}/storage/{blob}"})
        # Loopback storage is HTTP; HTTPS-only redirect enforcement has its own tests.
        monkeypatch.setattr(registry_module, "_require_secure_redirect", lambda url: None)
        _verify(registry, policy)
        assert len(storage.requests) == len(blobs)
        assert all(auth is None for _, auth in storage.requests)
    finally:
        storage.close()


@pytest.mark.parametrize("provider", ["cosign", "notation"])
def test_builder_verifies_private_tag_once_before_any_base_blob(
        registry, monkeypatch, tmp_path, provider):
    events = []
    if provider == "cosign":
        _serve_cosign(registry)
        policy = _cosign_policy()

        def run(command, environment):
            events.append("verified")
            return _encoded([{"critical": {
                "identity": {"docker-reference": REPOSITORY},
                "image": {"docker-manifest-digest": registry.subject_digest},
                "type": "cosign container image signature"}}])

        monkeypatch.setattr(base_trust, "_run_cosign", run)
    else:
        _serve_notation(registry)
        policy = _notation_policy(tmp_path)

        def run(args, environment):
            events.append("verified")
            return ("Successfully verified signature for " + args[-1] + "\n").encode()

        monkeypatch.setattr(notation, "_run", run)

    class LoopbackClient(RegistryClient):
        def __init__(self, *args, **kwargs):
            assert kwargs["strict_auth"] is True
            super().__init__(*args, **kwargs)
            self.base_url = registry.url + "/v2"

        def pull_blob(self, digest, destination, expected_size):
            if digest in registry.base_blobs:
                events.append("base-blob")
                raise RuntimeError("stop after verification")
            return super().pull_blob(digest, destination, expected_size)

    monkeypatch.setattr(builder_module, "RegistryClient", LoopbackClient)
    monkeypatch.setattr(builder_module, "get_registry_auth",
                        lambda *_: RegistryAuth(username=USERNAME, password=PASSWORD))
    config = BuildConfig(context_dir=str(tmp_path), output_dir=str(tmp_path / "output"),
                         base_image="registry.example/team/base:latest",
                         base_image_trust=policy, use_cache=False)
    layers = tmp_path / "layers"
    layers.mkdir()
    with pytest.raises(RuntimeError, match="stop after verification"):
        ImageBuilder(config)._pull_base_image(layers, "linux", "amd64")
    assert events[:2] == ["verified", "base-blob"]
    paths = [path for path, _ in registry.requests]
    assert paths.count("/v2/team/base/manifests/latest") == 1
    assert all(auth == BASIC for _, auth in registry.requests)
