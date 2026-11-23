"""Authenticated signature discovery through the strict, credential-isolated client."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import sys
import tempfile
import threading
import urllib.parse

import pytest
import trustme

from pycontainer import base_trust
from pycontainer import builder as builder_module
from pycontainer import auth as auth_module
from pycontainer import trust_notation as notation
from pycontainer.auth import RegistryAuth
from pycontainer.base_trust import TrustVerificationError, verify_base_image
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.registry_client import RegistryClient, RegistryRequestError


REPOSITORY = "registry.example/team/base"
USERNAME = "synthetic-user"
PASSWORD = "synthetic-registry-secret"
BASIC = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
TOKEN = "synthetic-scoped-token"
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
    """TLS loopback registry with synthetic Basic or Bearer authentication."""

    def __init__(self, credentials=BASIC, repository="team/base", ca=None, bearer=False):
        self.repository = repository
        self.routes = {}
        self.requests = []
        self.credentials = credentials
        self.bearer = bearer
        self.challenge_realm = None
        self.challenge_scope = f"repository:{repository}:pull"
        self.token_queries = []
        self.ca = ca or trustme.CA()
        self.tempdir = tempfile.TemporaryDirectory()
        self.ca_file = self.tempdir.name + "/ca.pem"
        self.ca.cert_pem.write_to_path(self.ca_file)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                path = url.path
                server.requests.append((path, self.headers.get("Authorization")))
                if server.bearer and path == "/token":
                    query = urllib.parse.parse_qs(url.query)
                    server.token_queries.append(query)
                    if (self.headers.get("Authorization") != server.credentials or
                            query != {"service": ["synthetic-registry"],
                                      "scope": [f"repository:{server.repository}:pull"]}):
                        self._reply(403, b"{}")
                    else:
                        self._reply(200, _encoded({"token": TOKEN}))
                    return
                if server.bearer and self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._reply(401, b"{}", {"WWW-Authenticate": (
                        f'Bearer realm="{server.challenge_realm or server.url + "/token"}",'
                        f'service="synthetic-registry",scope="{server.challenge_scope}"')})
                    return
                if server.credentials and self.headers.get("Authorization") != server.credentials:
                    if not server.bearer:
                        self._reply(401, b"{}", {"WWW-Authenticate": 'Basic realm="test"'})
                        return
                status, body, headers = server.routes.get(path, (404, b"{}", {}))
                self._reply(status, body, headers)

            def _reply(self, status, body, headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ca.issue_cert("127.0.0.1").configure_cert(context)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)
        self.tempdir.cleanup()

    def manifest(self, reference, content, media_type):
        self.routes[f"/v2/{self.repository}/manifests/{reference}"] = (
            200, content, {"Content-Type": media_type, "Docker-Content-Digest": _digest(content)})

    def blob(self, content):
        self.routes[f"/v2/{self.repository}/blobs/{_digest(content)}"] = (200, content, {})


def _set_subject(server):
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
    server.blob(config)
    server.blob(layer)


@pytest.fixture
def registry():
    server = _Server()
    _set_subject(server)
    yield server
    server.close()


def _client(server, **options):
    options = {"username": USERNAME, "password": PASSWORD, "ca_file": server.ca_file, **options}
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
    subject = {"mediaType": getattr(server, "subject_media_type", MANIFEST),
               "digest": server.subject_digest, "size": len(server.subject)}
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
    storage = _Server(credentials=None, ca=registry.ca)
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
            kwargs["ca_file"] = registry.ca_file
            super().__init__(*args, **kwargs)
            self.base_url = registry.url + "/v2"

        def pull_blob(self, digest, destination, expected_size):
            if digest in registry.base_blobs:
                events.append("base-blob")
                raise RuntimeError("stop after verification")
            return super().pull_blob(digest, destination, expected_size)

    monkeypatch.setattr(builder_module, "RegistryClient", LoopbackClient)
    monkeypatch.setattr(builder_module, "get_registry_auth",
                        lambda *_, **__: RegistryAuth(username=USERNAME, password=PASSWORD))
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


@pytest.mark.parametrize("provider", ["cosign", "notation"])
@pytest.mark.parametrize("failure", [None, "credentials", "scope", "realm", "ca"])
def test_builder_discovers_signatures_with_scoped_bearer_challenge(
        tmp_path, monkeypatch, provider, failure):
    server = _Server(bearer=True)
    untrusted = _Server(credentials=None, ca=server.ca) if failure == "realm" else None
    try:
        _set_subject(server)
        repository = f"127.0.0.1:{server.httpd.server_address[1]}/team/base"
        if failure == "scope":
            server.challenge_scope = "repository:other/base:pull"
        if untrusted:
            server.challenge_realm = untrusted.url + "/token"
        if provider == "cosign":
            signature_blobs = _serve_cosign(server)
            policy = _cosign_policy()

            def run(args, environment):
                check_verifier(args, environment)
                return _encoded([{"critical": {
                    "identity": {"docker-reference": repository},
                    "image": {"docker-manifest-digest": server.subject_digest},
                    "type": "cosign container image signature"}}])

            monkeypatch.setattr(base_trust, "_run_cosign", run)
        else:
            signature_blobs = {_digest(_serve_notation(server))}
            policy = _notation_policy(tmp_path)

            def run(args, environment):
                check_verifier(args, environment)
                return ("Successfully verified signature for " + args[-1] + "\n").encode()

            monkeypatch.setattr(notation, "_run", run)
        policy["repository"] = repository
        observed = []

        def check_verifier(args, environment):
            _assert_no_secret([*args, *environment.values()])
            assert all(TOKEN not in value for value in [*args, *environment.values()])
            requested_blobs = {
                path.rsplit("/", 1)[1]
                for path, _ in server.requests if "/blobs/" in path
            }
            assert requested_blobs == signature_blobs
            assert not requested_blobs & server.base_blobs
            observed.append(True)

        _credential_environment(monkeypatch)
        monkeypatch.setenv("REGISTRY_TOKEN", TOKEN)
        monkeypatch.setattr(
            builder_module, "get_registry_auth",
            lambda *_, **__: RegistryAuth(
                username=USERNAME,
                password="wrong-password" if failure == "credentials" else PASSWORD,
            ),
        )
        ca_file = server.ca_file
        if failure == "ca":
            ca_file = str(tmp_path / "wrong-ca.pem")
            trustme.CA().cert_pem.write_to_path(ca_file)
        config = BuildConfig(
            context_dir=str(tmp_path), output_dir=str(tmp_path / "output"),
            base_image=f"{repository}:latest", base_image_trust=policy,
            registry_ca_file=ca_file, use_cache=False,
        )
        layers = tmp_path / "layers"
        layers.mkdir()
        if failure:
            expected = {
                "credentials": "HTTP 403",
                "scope": "HTTP 403",
                "realm": "untrusted token realm",
                "ca": "failed after 3 attempt",
            }[failure]
            with pytest.raises(RegistryRequestError, match=expected):
                ImageBuilder(config)._pull_base_image(layers, "linux", "amd64")
            assert not observed
            assert not any("/blobs/" in path for path, _ in server.requests)
            if untrusted:
                assert untrusted.requests == []
            if failure == "ca":
                assert server.requests == []
            elif failure != "realm":
                assert server.token_queries == [{
                    "service": ["synthetic-registry"],
                    "scope": [server.challenge_scope],
                }]
        else:
            pulled_layers, base_config = ImageBuilder(config)._pull_base_image(
                layers, "linux", "amd64")
            assert len(pulled_layers) == 1
            assert base_config["architecture"] == "amd64"
            assert observed == [True]
            assert server.token_queries == [{
                "service": ["synthetic-registry"],
                "scope": ["repository:team/base:pull"],
            }]
            assert server.requests[0] == ("/v2/team/base/manifests/latest", BASIC)
            assert server.requests[1] == ("/token", BASIC)
            assert all(auth == f"Bearer {TOKEN}" for _, auth in server.requests[2:])
            assert [path for path, _ in server.requests].count(
                "/v2/team/base/manifests/latest") == 2
            assert server.base_blobs <= {
                path.rsplit("/", 1)[1]
                for path, _ in server.requests if "/blobs/" in path
            }
    finally:
        if untrusted:
            untrusted.close()
        server.close()


@pytest.mark.parametrize("provider", ["cosign", "notation"])
def test_multi_platform_trust_uses_scoped_base_credentials(
        tmp_path, monkeypatch, provider):
    from test_multi_platform import FakeBaseRegistry

    server = _Server(bearer=True)
    storage = _Server(credentials=None, ca=server.ca)
    try:
        base = FakeBaseRegistry()
        server.subject_digest = base.index_digest
        server.subject = base.manifests[base.index_digest]
        server.subject_media_type = INDEX
        server.base_blobs = set(base.blobs)
        for reference, body in base.manifests.items():
            server.manifest(reference, body, json.loads(body)["mediaType"])
        for body in base.blobs.values():
            server.blob(body)

        repository = f"127.0.0.1:{server.httpd.server_address[1]}/team/base"
        authority = repository.split("/", 1)[0]
        destination_secret = "synthetic-destination-secret"
        monkeypatch.setenv("REGISTRY_USERNAME", "synthetic-destination-user")
        monkeypatch.setenv("REGISTRY_PASSWORD", destination_secret)
        monkeypatch.setenv("REGISTRY_TOKEN", destination_secret)
        monkeypatch.setattr(auth_module.DockerConfigAuthProvider, "_load_config", lambda self: {
            "auths": {authority: {"username": USERNAME, "password": PASSWORD}}
        })

        if provider == "cosign":
            signature_blobs = _serve_cosign(server)
            policy = _cosign_policy()

            def run(args, environment):
                check_verifier(args, environment)
                return _encoded([{"critical": {
                    "identity": {"docker-reference": repository},
                    "image": {"docker-manifest-digest": server.subject_digest},
                    "type": "cosign container image signature"}}])

            monkeypatch.setattr(base_trust, "_run_cosign", run)
        else:
            signature_blobs = {_digest(_serve_notation(server))}
            policy = _notation_policy(tmp_path)

            def run(args, environment):
                check_verifier(args, environment)
                return ("Successfully verified signature for " + args[-1] + "\n").encode()

            monkeypatch.setattr(notation, "_run", run)
        policy["repository"] = repository
        verification_requests = []

        def check_verifier(args, environment):
            _assert_no_secret([*args, *environment.values()])
            assert all(destination_secret not in value and TOKEN not in value
                       for value in [*args, *environment.values()])
            verification_requests.append(len(server.requests))
            server.manifest("3.11-slim", b'{"retargeted":true}', INDEX)

        for digest in signature_blobs:
            path = f"/v2/team/base/blobs/{digest}"
            storage.routes[f"/storage/{digest}"] = server.routes[path]
            server.routes[path] = (307, b"", {"Location": f"{storage.url}/storage/{digest}"})

        context = tmp_path / "context"
        context.mkdir()
        (context / "app.py").write_text("print('hello')\n")
        config = BuildConfig(
            context_dir=str(context), output_dir=str(tmp_path / "output"),
            base_image=f"{repository}:3.11-slim", base_image_trust=policy,
            registry_ca_file=server.ca_file, platform="linux/amd64,linux/arm64",
            use_cache=False,
        )
        ImageBuilder(config).build()

        assert len(verification_requests) == 2
        paths = [path for path, _ in server.requests]
        tag_path = "/v2/team/base/manifests/3.11-slim"
        index_path = f"/v2/team/base/manifests/{server.subject_digest}"
        assert paths.count(tag_path) == 2
        assert paths.count(index_path) == 2
        for position, descriptor in enumerate(json.loads(server.subject)["manifests"]):
            child_path = f"/v2/team/base/manifests/{descriptor['digest']}"
            child_request = paths.index(child_path)
            assert child_request >= verification_requests[position]
            manifest = json.loads(base.manifests[descriptor["digest"]])
            blobs = [manifest["config"], *manifest["layers"]]
            assert all(paths.index(f"/v2/team/base/blobs/{blob['digest']}") > child_request
                       for blob in blobs)
        assert server.token_queries == [
            {"service": ["synthetic-registry"], "scope": ["repository:team/base:pull"]}
        ] * 2
        assert all(auth in (BASIC, f"Bearer {TOKEN}") for _, auth in server.requests)
        assert len(storage.requests) == 2 * len(signature_blobs)
        assert all(auth is None for _, auth in storage.requests)
    finally:
        storage.close()
        server.close()
