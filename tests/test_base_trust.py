import hashlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from pycontainer import builder as builder_module
from pycontainer import base_trust
from pycontainer import registry_client as registry_module
from pycontainer.auth import RegistryAuth
from pycontainer.base_trust import TrustVerificationError, verify_base_image
from pycontainer.blob_store import BlobIntegrityError
from pycontainer.builder import ImageBuilder, UnsupportedPlatformError
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.registry_client import RegistryClient


INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"
SIMPLESIGNING = "application/vnd.dev.cosign.simplesigning.v1+json"


def _encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def _digest(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _cosign_evidence(digest, repository="registry.example/team/base"):
    return [{"critical": {
        "identity": {"docker-reference": repository},
        "image": {"docker-manifest-digest": digest},
        "type": "cosign container image signature",
    }}]


def _policy(**changes):
    return {
        "provider": "cosign",
        "repository": "registry.example/team/base",
        "identity": "builder@example.com",
        "issuer": "https://issuer.example",
        **changes,
    }


class _Registry:
    registry = "registry.example"
    repository = "team/base"

    def __init__(self, *, index=False, environment=None):
        config = {"os": "linux", "architecture": "amd64", "config": {}}
        if environment is not None:
            config["config"]["Env"] = environment
        config = _encoded(config)
        layer = b"application layer"
        self.blobs = {_digest(config): config, _digest(layer): layer}
        self.manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "config": {"mediaType": CONFIG, "digest": _digest(config), "size": len(config)},
            "layers": [{"mediaType": LAYER, "digest": _digest(layer), "size": len(layer)}],
        }
        manifest_bytes = _encoded(self.manifest)
        self.child_digest = _digest(manifest_bytes)
        self.manifests = {self.child_digest: manifest_bytes}
        if index:
            self.root = {
                "schemaVersion": 2,
                "mediaType": INDEX,
                "manifests": [{
                    "mediaType": MANIFEST,
                    "digest": self.child_digest,
                    "size": len(manifest_bytes),
                    "platform": {"os": "linux", "architecture": "amd64"},
                }],
            }
        else:
            self.root = self.manifest
        self.root_bytes = _encoded(self.root)
        self.root_digest = _digest(self.root_bytes)
        self.manifests[self.root_digest] = self.root_bytes
        self.events = []
        self.tag_requests = 0
        self.verified = False
        signature_config = b'{"rootfs":{"type":"layers","diff_ids":[]}}'
        payload = b'{"critical":{}}'
        self.signature_blobs = {_digest(signature_config): signature_config, _digest(payload): payload}
        self.signatures = {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "config": {"mediaType": CONFIG, "digest": _digest(signature_config),
                       "size": len(signature_config)},
            "layers": [{"mediaType": SIMPLESIGNING, "digest": _digest(payload),
                        "size": len(payload)}],
        }

    def pull_optional_manifest_bytes(self, reference, max_bytes):
        self.events.append(("optional-manifest", reference))
        if self.signatures is None or reference != f"sha256-{self.root_digest[7:]}.sig":
            return None
        body = _encoded(self.signatures)
        assert len(body) <= max_bytes
        return self.signatures, _digest(body), body

    def pull_manifest_bytes(self, reference, expected_digest=None, expected_size=None):
        self.events.append(("manifest", reference))
        if reference == "latest":
            self.tag_requests += 1
            body = self.root_bytes if self.tag_requests == 1 else b'{"retargeted":true}'
        else:
            body = self.manifests[reference]
        digest = _digest(body)
        if expected_digest is not None and digest != expected_digest:
            raise BlobIntegrityError("manifest digest mismatch")
        if expected_size is not None and len(body) != expected_size:
            raise BlobIntegrityError("manifest size mismatch")
        return json.loads(body), digest, body

    def pull_blob(self, digest, destination, expected_size):
        self.events.append(("blob", digest))
        if digest in self.signature_blobs:
            content = self.signature_blobs[digest]
        else:
            assert self.verified, "base blob was fetched before trust verification"
            content = self.blobs[digest]
        assert len(content) == expected_size
        Path(destination).write_bytes(content)
        return True


def _builder(tmp_path, policy=None, base_image="registry.example/team/base:latest"):
    return ImageBuilder(BuildConfig(
        context_dir=str(tmp_path),
        output_dir=str(tmp_path / "output"),
        base_image=base_image,
        base_image_trust=policy,
        use_cache=False,
    ))


@pytest.mark.parametrize("index", [False, True])
def test_required_trust_resolves_tag_once_and_verifies_before_base_blobs(
    tmp_path, monkeypatch, index
):
    client = _Registry(index=index)
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *_: RegistryAuth())

    def verify(policy, repository, digest, manifest, manifest_bytes, source):
        assert source is client
        assert repository == "registry.example/team/base"
        assert digest == client.root_digest
        assert manifest_bytes == client.root_bytes
        assert manifest == client.root
        assert client.events == [("manifest", "latest")]
        client.events.append(("verify", digest))
        client.verified = True

    monkeypatch.setattr(builder_module, "verify_base_image", verify)
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    layers, config = _builder(tmp_path, _policy())._pull_base_image(
        layers_dir, "linux", "amd64"
    )

    assert client.tag_requests == 1
    assert client.events[:2] == [
        ("manifest", "latest"),
        ("verify", client.root_digest),
    ]
    assert len(layers) == 1
    assert config["architecture"] == "amd64"
    if index:
        assert client.events[2] == ("manifest", client.child_digest)


@pytest.mark.parametrize(
    ("environment", "error"),
    [
        ([], "Python ABI"),
        (["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.8"], "unverified"),
    ],
)
def test_verified_digest_does_not_authenticate_tag_for_dependency_staging(
    tmp_path, monkeypatch, environment, error
):
    client = _Registry(environment=environment)
    client.registry = "registry-1.docker.io"
    client.repository = "library/python"
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *_: RegistryAuth())

    def verify(policy, repository, digest, manifest, manifest_bytes, source):
        assert repository == "docker.io/library/python"
        assert digest == client.root_digest
        source.verified = True

    def unexpected_install(*args, **kwargs):
        pytest.fail("dependency installation must not use an unverified tag")

    monkeypatch.setattr(builder_module, "verify_base_image", verify)
    monkeypatch.setattr(builder_module, "install_requirements", unexpected_install)
    (tmp_path / "requirements.txt").write_text("fixture-dep==1.0\n")
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()
    builder = _builder(
        tmp_path,
        _policy(repository="docker.io/library/python"),
        f"python:3.11-slim@{client.root_digest}",
    )
    _, config = builder._pull_base_image(layers_dir, "linux", "amd64")

    assert client.verified
    with pytest.raises(ValueError, match=error):
        builder._create_deps_layer(
            layers_dir, base_config=config, os_name="linux", arch="amd64"
        )


def test_failed_verification_never_reads_child_or_base_blobs(tmp_path, monkeypatch):
    client = _Registry(index=True)
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *_: RegistryAuth())

    def reject(*args):
        raise RuntimeError("untrusted signature")

    monkeypatch.setattr(builder_module, "verify_base_image", reject)
    layers = tmp_path / "layers"
    layers.mkdir()
    with pytest.raises(RuntimeError, match="untrusted signature"):
        _builder(tmp_path, _policy())._pull_base_image(layers, "linux", "amd64")
    assert client.events == [("manifest", "latest")]


@pytest.mark.parametrize("problem", ["digest", "size", "platform", "media_type"])
def test_verified_index_still_requires_exact_child_descriptor(
    tmp_path, monkeypatch, problem
):
    client = _Registry(index=True)
    descriptor = client.root["manifests"][0]
    if problem == "digest":
        bad_digest = "sha256:" + "b" * 64
        client.manifests[bad_digest] = client.manifests[client.child_digest]
        descriptor["digest"] = bad_digest
    elif problem == "size":
        descriptor["size"] += 1
    elif problem == "platform":
        descriptor["platform"]["architecture"] = "arm64"
    else:
        descriptor["mediaType"] = "application/vnd.oci.image.manifest.invalid"
    client.root_bytes = _encoded(client.root)
    client.root_digest = _digest(client.root_bytes)
    monkeypatch.setattr(builder_module, "RegistryClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *_: RegistryAuth())

    def verify(*args):
        client.verified = True

    monkeypatch.setattr(builder_module, "verify_base_image", verify)
    layers = tmp_path / "layers"
    layers.mkdir()
    with pytest.raises((BlobIntegrityError, UnsupportedPlatformError, RuntimeError)):
        _builder(tmp_path, _policy())._pull_base_image(layers, "linux", "amd64")
    assert not any(event[0] == "blob" for event in client.events)


@pytest.mark.parametrize("transport", ["oci", "docker-archive"])
def test_required_registry_trust_rejects_local_sources_before_open(
    tmp_path, monkeypatch, transport
):
    def no_open(*args, **kwargs):
        raise AssertionError("local source was opened before rejecting policy")

    monkeypatch.setattr(builder_module, "open_local_image_source", no_open)
    layers = tmp_path / "layers"
    layers.mkdir()
    builder = _builder(tmp_path, _policy(), f"{transport}:{tmp_path / 'base'}")
    with pytest.raises(RuntimeError, match="local"):
        builder._pull_base_image(layers, "linux", "amd64")


def test_repository_mismatch_fails_before_network(tmp_path, monkeypatch):
    def no_client(*args, **kwargs):
        raise AssertionError("registry contacted before repository policy check")

    monkeypatch.setattr(builder_module, "RegistryClient", no_client)
    layers = tmp_path / "layers"
    layers.mkdir()
    builder = _builder(tmp_path, _policy(repository="registry.example/other/base"))
    with pytest.raises(RuntimeError, match="repository"):
        builder._pull_base_image(layers, "linux", "amd64")


@pytest.mark.parametrize(
    "policy",
    [
        {},
        _policy(repository="registry.example/team/base:latest"),
        _policy(repository="registry.example/team/*"),
        _policy(provider="unknown"),
        _policy(identity="*"),
        _policy(issuer="http://issuer.example"),
        _policy(key="public.pem"),
        _policy(unexpected="value"),
        {"provider": "notation", "repository": "registry.example/team/base",
         "identity": "CN=builder", "trust_root": "root.crt"},
    ],
)
def test_invalid_required_trust_configuration_is_rejected(policy):
    with pytest.raises(ValueError):
        BuildConfig(base_image_trust=policy)


def test_nested_trust_config_is_loaded_without_cli_override(tmp_path):
    config = tmp_path / "pycontainer.toml"
    config.write_text(
        '[build]\nbase_image = "registry.example/team/base:latest"\n'
        '[build.base_image_trust]\nprovider = "cosign"\n'
        'repository = "registry.example/team/base"\n'
        'identity = "builder@example.com"\nissuer = "https://issuer.example"\n'
    )
    assert config_from_file(config).base_image_trust == _policy()


@pytest.mark.parametrize(
    "header",
    ["", "sha512:" + "a" * 128, "sha256:" + "b" * 64, "invalid"],
)
def test_required_manifest_rejects_present_invalid_digest_header(monkeypatch, header):
    client = RegistryClient("registry.example", "team/base")
    body = _encoded({"schemaVersion": 2, "mediaType": MANIFEST})
    monkeypatch.setattr(
        client, "_make_request",
        lambda *args, **kwargs: (200, body, {"Docker-Content-Digest": header}),
    )
    with pytest.raises(BlobIntegrityError):
        client.pull_manifest_bytes("latest")


def test_required_manifest_accepts_absent_digest_header_and_uses_body_hash(monkeypatch):
    client = RegistryClient("registry.example", "team/base")
    body = _encoded({"schemaVersion": 2, "mediaType": MANIFEST})
    monkeypatch.setattr(
        client, "_make_request", lambda *args, **kwargs: (200, body, {})
    )
    _, digest, actual = client.pull_manifest_bytes("latest")
    assert (digest, actual) == (_digest(body), body)


def test_required_manifest_rejects_duplicate_digest_headers(monkeypatch):
    client = RegistryClient("registry.example", "team/base")
    body = _encoded({"schemaVersion": 2, "mediaType": MANIFEST})
    monkeypatch.setattr(
        client, "_make_request",
        lambda *args, **kwargs: (
            200, body, {"Docker-Content-Digest": _digest(body),
                        "docker-content-digest": _digest(body)}
        ),
    )
    with pytest.raises(BlobIntegrityError):
        client.pull_manifest_bytes("latest")


def test_manifest_redirect_does_not_forward_registry_authorization(monkeypatch):
    client = RegistryClient("registry.example", "team/base", auth_token="secret")
    handler = registry_module.SameOriginRedirect()

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size=-1):
            return b"{}"

    def fake_open(request, timeout, strict=False):
        assert request.get_header("Authorization") == "Bearer secret"
        assert not strict
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://storage.example/blob"
        )
        assert not redirected.has_header("Authorization")
        return Response()

    monkeypatch.setattr("pycontainer.registry_client._open_registry_request", fake_open)
    client._make_request("GET", "https://registry.example/v2/team/base/manifests/latest")


@pytest.mark.parametrize(
    "target,authorized",
    [
        ("https://registry.example/v2/team/base/manifest", True),
        ("https://storage.example/v2/team/base/manifest", False),
        ("http://registry.example/v2/team/base/manifest", False),
        ("https://registry.example:444/v2/team/base/manifest", False),
    ],
)
@pytest.mark.parametrize("unredirected", [True, False])
def test_redirect_retains_authorization_only_on_the_same_origin(target, authorized, unredirected):
    request = urllib.request.Request("https://registry.example/v2/team/base")
    if unredirected:
        request.add_unredirected_header("Authorization", "Bearer secret")
    else:
        request.add_header("Authorization", "Bearer secret")
    redirected = registry_module.SameOriginRedirect().redirect_request(
        request, None, 302, "Found", {}, target
    )
    assert redirected.get_header("Authorization") == ("Bearer secret" if authorized else None)


@pytest.mark.parametrize(
    "target",
    ["http://storage.example/blob", "https://user:password@storage.example/blob"],
)
def test_required_blob_redirect_chain_rejects_insecure_targets(target):
    request = urllib.request.Request("https://storage.example/blob")
    with pytest.raises(registry_module.RegistryRequestError, match="insecure"):
        registry_module.SecureRegistryRedirect().redirect_request(
            request, None, 302, "Found", {}, target
        )


def test_required_blob_redirect_chain_strips_cross_origin_authorization():
    request = urllib.request.Request("https://storage.example/blob")
    request.add_header("Authorization", "Bearer secret")
    redirected = registry_module.SecureRegistryRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://other.example/blob"
    )
    assert redirected.get_header("Authorization") is None


def test_required_registry_requests_use_secure_redirect_handler(monkeypatch):
    observed = []

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size=-1):
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            return Response()

    def build_opener(handler):
        observed.append(handler)
        return Opener()

    monkeypatch.setattr(registry_module.urllib.request, "build_opener", build_opener)
    client = RegistryClient("registry.example", "team/base", strict_auth=True)
    client._make_request("GET", "https://registry.example/v2/team/base/manifests/latest")
    assert isinstance(observed[0], registry_module.SecureRegistryRedirect)


def test_required_first_blob_redirect_cannot_downgrade_to_http(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found",
                {"Location": "http://storage.example/blob"}, io.BytesIO(),
            )

    monkeypatch.setattr(registry_module.urllib.request, "build_opener", lambda *_: Opener())
    client = RegistryClient("registry.example", "team/base", strict_auth=True)
    with pytest.raises(registry_module.RegistryRequestError, match="insecure"):
        client._open_blob_response("https://registry.example/v2/team/base/blobs/sha256:abc")


def test_cosign_keyless_checks_claims_transparency_and_exact_identity(monkeypatch):
    client = _Registry()
    commands = []
    monkeypatch.setenv("COSIGN_REPOSITORY", "untrusted.example/other")
    monkeypatch.setenv("SIGSTORE_ROOT_FILE", "untrusted.json")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "secret")
    monkeypatch.setenv("REGISTRY_AUTH_FILE", "untrusted-auth.json")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")

    def run(command, environment):
        layout = Path(command[-1])
        index = json.loads((layout / "index.json").read_bytes())
        blobs = layout / "blobs" / "sha256"
        assert [entry["annotations"]["kind"] for entry in index["manifests"]] == [
            "dev.cosignproject.cosign/image", "dev.cosignproject.cosign/sigs"]
        assert index["manifests"][0]["digest"] == client.root_digest
        assert (blobs / client.root_digest[7:]).read_bytes() == client.root_bytes
        assert sorted(path.name for path in blobs.iterdir()) == sorted(
            [client.root_digest[7:], index["manifests"][1]["digest"][7:]]
            + [digest[7:] for digest in client.signature_blobs])
        commands.append((command, environment))
        return _encoded(_cosign_evidence(client.root_digest))

    monkeypatch.setattr(base_trust, "_run_cosign", run)
    verify_base_image(
        _policy(), "registry.example/team/base", client.root_digest,
        client.root, client.root_bytes, client,
    )
    command, environment = commands[0]
    assert command[1:3] == ["verify", "--local-image"]
    assert "--check-claims=true" in command
    assert "--insecure-ignore-tlog=false" in command
    assert "--insecure-ignore-sct=false" in command
    assert "--certificate-identity=builder@example.com" in command
    assert "--certificate-oidc-issuer=https://issuer.example" in command
    assert not any("registry.example" in argument for argument in command)
    assert not any(event[0] == "blob" and event[1] in client.blobs for event in client.events)
    assert "COSIGN_REPOSITORY" not in environment
    assert "SIGSTORE_ROOT_FILE" not in environment
    assert "DOCKER_AUTH_CONFIG" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert environment["REGISTRY_AUTH_FILE"] != "untrusted-auth.json"
    assert environment["DOCKER_CONFIG"] != os.environ.get("DOCKER_CONFIG")


def test_cosign_public_key_is_snapshotted_before_verification(tmp_path, monkeypatch):
    client = _Registry()
    key = tmp_path / "trusted.pub"
    key.write_bytes(b"synthetic public key")
    copied = []

    def run(command, environment):
        key_argument = next(value for value in command if value.startswith("--key="))
        snapshot = Path(key_argument.removeprefix("--key="))
        assert snapshot != key
        assert snapshot.read_bytes() == key.read_bytes()
        copied.append(snapshot)
        return _encoded(_cosign_evidence(client.root_digest))

    monkeypatch.setattr(base_trust, "_run_cosign", run)
    policy = {
        "provider": "cosign", "repository": "registry.example/team/base",
        "key": str(key),
    }
    verify_base_image(
        policy, "registry.example/team/base", client.root_digest,
        client.root, client.root_bytes, client,
    )
    assert not copied[0].exists()


@pytest.mark.parametrize(
    "output",
    [b"not json", b"[]", b"{}", b'[{"critical":{"image":{"docker-manifest-digest":"sha256:wrong"}}}]'],
)
def test_cosign_rejects_missing_or_mismatched_verification_evidence(monkeypatch, output):
    client = _Registry()
    monkeypatch.setattr(base_trust, "_run_cosign", lambda *args: output)
    with pytest.raises(TrustVerificationError):
        verify_base_image(
            _policy(), "registry.example/team/base", client.root_digest,
            client.root, client.root_bytes, client,
        )


@pytest.mark.parametrize("problem", [
    "missing", "schema", "media", "no-layers", "too-many-layers", "layer-media",
    "layer-size", "config-size", "subject", "bad-digest",
])
def test_cosign_signature_discovery_fails_closed_before_verifier(monkeypatch, problem):
    client = _Registry(index=True)
    signatures = client.signatures
    layer = signatures["layers"][0]
    if problem == "missing":
        client.signatures = None
    elif problem == "schema":
        signatures["schemaVersion"] = 1
    elif problem == "media":
        signatures["mediaType"] = INDEX
    elif problem == "no-layers":
        signatures["layers"] = []
    elif problem == "too-many-layers":
        signatures["layers"] = [layer] * (base_trust.MAX_COSIGN_SIGNATURES + 1)
    elif problem == "layer-media":
        layer["mediaType"] = "application/vnd.dev.sigstore.bundle.v0.3+json"
    elif problem == "layer-size":
        layer["size"] = base_trust.MAX_COSIGN_SIGNATURE_BLOB_BYTES + 1
    elif problem == "config-size":
        signatures["config"]["size"] = 0
    elif problem == "subject":
        signatures["subject"] = {"mediaType": INDEX, "digest": client.child_digest, "size": 1}
    else:
        layer["digest"] = "sha256:ABC"
    called = []
    monkeypatch.setattr(base_trust, "_run_cosign", lambda *args: called.append(args))
    with pytest.raises(TrustVerificationError, match="Cosign"):
        verify_base_image(
            _policy(), "registry.example/team/base", client.root_digest,
            client.root, client.root_bytes, client,
        )
    assert called == []
    assert not any(event[0] == "blob" and event[1] in client.blobs for event in client.events)


def test_cosign_index_subject_is_staged_as_image_index(monkeypatch):
    client = _Registry(index=True)
    client.signatures["subject"] = {
        "mediaType": INDEX, "digest": client.root_digest, "size": len(client.root_bytes)}
    kinds = []

    def run(command, environment):
        index = json.loads((Path(command[-1]) / "index.json").read_bytes())
        kinds.extend(entry["annotations"]["kind"] for entry in index["manifests"])
        return _encoded(_cosign_evidence(client.root_digest))

    monkeypatch.setattr(base_trust, "_run_cosign", run)
    verify_base_image(
        _policy(), "registry.example/team/base", client.root_digest,
        client.root, client.root_bytes, client,
    )
    assert kinds == ["dev.cosignproject.cosign/imageIndex", "dev.cosignproject.cosign/sigs"]


def test_cosign_signature_blob_integrity_error_is_trust_error(monkeypatch):
    client = _Registry()

    def pull_blob(digest, destination, expected_size):
        raise BlobIntegrityError("signature digest mismatch")

    client.pull_blob = pull_blob
    monkeypatch.setattr(base_trust, "_run_cosign", lambda *args: pytest.fail("verifier ran"))
    with pytest.raises(TrustVerificationError, match="digest mismatch"):
        verify_base_image(
            _policy(), "registry.example/team/base", client.root_digest,
            client.root, client.root_bytes, client,
        )


@pytest.mark.parametrize("problem", ["other-repository", "missing-repository", "wrong-type"])
def test_cosign_rejects_replayed_or_incomplete_repository_claim(monkeypatch, problem):
    client = _Registry()
    evidence = _cosign_evidence(client.root_digest)
    if problem == "other-repository":
        evidence[0]["critical"]["identity"]["docker-reference"] = "other.example/team/base"
    elif problem == "missing-repository":
        del evidence[0]["critical"]["identity"]
    else:
        evidence[0]["critical"]["type"] = "unrelated claim"
    monkeypatch.setattr(base_trust, "_run_cosign", lambda *args: _encoded(evidence))
    with pytest.raises(TrustVerificationError, match="repository"):
        verify_base_image(
            _policy(), "registry.example/team/base", client.root_digest,
            client.root, client.root_bytes, client,
        )


@pytest.mark.parametrize("failure", ["missing", "timeout", "exit"])
def test_cosign_tool_failures_are_explicit(tmp_path, monkeypatch, failure):
    if failure == "missing":
        command = [str(tmp_path / "missing-cosign")]
    elif failure == "timeout":
        monkeypatch.setattr(base_trust, "VERIFIER_TIMEOUT_SECONDS", 0.1)
        command = [sys.executable, "-c", "import time; time.sleep(10)"]
    else:
        command = [sys.executable, "-c", "import sys; sys.exit(1)"]
    with pytest.raises(TrustVerificationError, match="Cosign"):
        base_trust._run_cosign(command, os.environ.copy())


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_cosign_output_is_bounded_while_process_runs(monkeypatch, stream):
    monkeypatch.setattr(base_trust, "MAX_VERIFIER_OUTPUT_BYTES", 1024)
    # Leave room for slow child startup; an unbounded reader would still hit this timeout.
    monkeypatch.setattr(base_trust, "VERIFIER_TIMEOUT_SECONDS", 30)
    script = f"import sys, time; sys.{stream}.write('x' * 200000); sys.{stream}.flush(); time.sleep(120)"
    started = time.monotonic()
    with pytest.raises(TrustVerificationError, match="output exceeded"):
        base_trust._run_cosign([sys.executable, "-c", script], os.environ.copy())
    assert time.monotonic() - started < 30


def test_output_limit_takes_precedence_over_racing_timeout(monkeypatch):
    class Process:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO(b"x" * 2048)
            self.stderr = io.BytesIO()
            self.returncode = None
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                deadline = time.monotonic() + 5
                while not self.stdout.closed and time.monotonic() < deadline:
                    time.sleep(0.01)
                raise subprocess.TimeoutExpired("cosign", timeout)
            self.returncode = -9
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(base_trust.subprocess, "Popen", Process)
    with pytest.raises(TrustVerificationError, match="output exceeded"):
        base_trust.run_bounded_verifier(["cosign"], {}, 1, 1024, "Cosign")


@pytest.mark.parametrize("size", [1024, 1025])
def test_cosign_output_limit_boundary(monkeypatch, size):
    monkeypatch.setattr(base_trust, "MAX_VERIFIER_OUTPUT_BYTES", 1024)
    script = f"import sys; sys.stdout.buffer.write(b'x' * {size})"
    command = [sys.executable, "-c", script]
    if size == 1024:
        assert base_trust._run_cosign(command, os.environ.copy()) == b"x" * size
    else:
        with pytest.raises(TrustVerificationError, match="output exceeded"):
            base_trust._run_cosign(command, os.environ.copy())


def test_cosign_output_limit_combines_stdout_and_stderr(monkeypatch):
    monkeypatch.setattr(base_trust, "MAX_VERIFIER_OUTPUT_BYTES", 1024)
    script = "import sys; sys.stdout.write('x' * 600); sys.stderr.write('y' * 600)"
    with pytest.raises(TrustVerificationError, match="output exceeded"):
        base_trust._run_cosign([sys.executable, "-c", script], os.environ.copy())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executable search behavior")
def test_verifier_lookup_does_not_search_working_directory_implicitly(tmp_path, monkeypatch):
    project = tmp_path / "project"
    trusted = tmp_path / "trusted"
    project.mkdir()
    trusted.mkdir()
    (project / "cosign.exe").touch()
    expected = trusted / "cosign.exe"
    expected.touch()
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", str(trusted))
    monkeypatch.setenv("PATHEXT", ".EXE")
    assert Path(base_trust.resolve_verifier_executable("cosign")) == expected


def test_cosign_missing_key_fails_closed(tmp_path):
    client = _Registry()
    policy = {
        "provider": "cosign", "repository": "registry.example/team/base",
        "key": str(tmp_path / "missing.pub"),
    }
    with pytest.raises(TrustVerificationError, match="regular file"):
        verify_base_image(
            policy, "registry.example/team/base", client.root_digest,
            client.root, client.root_bytes, client,
        )


def test_required_auth_rejects_untrusted_external_token_realm(monkeypatch):
    client = RegistryClient(
        "registry.example", "team/base", username="synthetic-user",
        password="synthetic-password", strict_auth=True,
    )

    def no_request(*args, **kwargs):
        raise AssertionError("credentials were sent to an untrusted realm")

    monkeypatch.setattr("pycontainer.registry_client._open_registry_request", no_request)
    with pytest.raises(RuntimeError, match="untrusted token realm"):
        client._get_bearer_token({"realm": "https://evil.example/token"})


def test_required_auth_allows_pinned_realm_without_forwarding_redirected_credentials(
    monkeypatch,
):
    realm = "https://auth.example/token"
    client = RegistryClient(
        "registry.example", "team/base", username="synthetic-user",
        password="synthetic-password", strict_auth=True, trusted_auth_realm=realm,
    )

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size=-1):
            return b'{"token":"synthetic-token"}'

    def open_request(request, timeout, strict=False):
        assert strict
        assert request.get_header("Authorization").startswith("Basic ")
        redirected = registry_module.SameOriginRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://storage.example/token"
        )
        assert not redirected.has_header("Authorization")
        return Response()

    monkeypatch.setattr("pycontainer.registry_client._open_registry_request", open_request)
    assert client._get_bearer_token({"realm": realm}) == "synthetic-token"
