"""Opt-in verification with real public registries and external verifiers."""
import base64
import json
import os
from pathlib import Path
import tempfile

import pytest

from pycontainer import base_trust
from pycontainer import builder as builder_module
from pycontainer.auth import RegistryAuth
from pycontainer.base_trust import TrustVerificationError
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.registry_client import RegistryClient


pytestmark = pytest.mark.registry_integration

DISTROLESS = (
    "gcr.io/distroless/static-debian13",
    "sha256:1a64f46364620465ef9e9a8f5413757dfdf22695e050bbfd7303eeaf17a59383",
)
QUAY = (
    "quay.io/travier/cosign-example",
    "sha256:54d03addad384bd8c179f82eb861701fdf89b4b3abf7a3d9a3242fa6f8d124cb",
)
MCR = (
    "mcr.microsoft.com/oss/v2/kubernetes-csi/azurelustre-csi",
    "sha256:f3690435712e2a141c8b8f7db3d8480831204151763c40fab3a4b41d9fb67cb7",
)
RATIFY = (
    "ghcr.io/ratify-project/ratify-dev",
    "sha256:e66f7aabca8a79494f91fc9702c90877a1a8046db9302e5782f2c5104cd9d8f5",
)


class _BaseBlobReached(Exception):
    pass


def _require_environment(name):
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"Set {name} to opt in to this real-provider test")
    return value


def _exercise_builder(tmp_path, monkeypatch, image, policy, *, reject=False):
    repository, digest = image
    observed = []
    real_cosign = base_trust._run_cosign
    clean_home = tmp_path / "empty-home"
    clean_home.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                 "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(clean_home))
    for name in tuple(os.environ):
        if name.startswith(("AWS_", "AZURE_", "GOOGLE_")):
            monkeypatch.delenv(name)

    def run_cosign(command, environment):
        output = real_cosign(command, environment)
        observed.append(command[-1])
        return output

    monkeypatch.setattr(base_trust, "_run_cosign", run_cosign)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *_: RegistryAuth())
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "empty-docker"))
    monkeypatch.setenv("REGISTRY_AUTH_FILE", str(tmp_path / "empty-auth.json"))
    original_pull_blob = RegistryClient.pull_blob

    def stop_before_blob(self, blob_digest, destination, expected_size):
        if not observed:
            # Signature evidence blobs are fetched before the verifier runs.
            assert "pycontainer-trust-" in str(destination)
            return original_pull_blob(self, blob_digest, destination, expected_size)
        assert len(observed) == 1 and not observed[0].startswith(repository)
        raise _BaseBlobReached

    monkeypatch.setattr(RegistryClient, "pull_blob", stop_before_blob)
    config = BuildConfig(
        context_dir=str(tmp_path), output_dir=str(tmp_path / "output"),
        base_image=f"{repository}@{digest}", base_image_trust=policy,
        use_cache=False,
    )
    layers = tmp_path / "layers"
    layers.mkdir()
    expected_error = TrustVerificationError if reject else _BaseBlobReached
    with pytest.raises(expected_error):
        ImageBuilder(config)._pull_base_image(layers, "linux", "amd64")
    if reject:
        assert observed == []


def test_real_cosign_keyless_and_wrong_identity(tmp_path, monkeypatch):
    binary = _require_environment("PYCONTAINER_COSIGN_BIN")
    policy = {
        "provider": "cosign", "repository": DISTROLESS[0], "binary": binary,
        "identity": "keyless@distroless.iam.gserviceaccount.com",
        "issuer": "https://accounts.google.com",
    }
    _exercise_builder(tmp_path, monkeypatch, DISTROLESS, policy)


def test_real_cosign_keyless_wrong_identity_fails_before_blobs(tmp_path, monkeypatch):
    binary = _require_environment("PYCONTAINER_COSIGN_BIN")
    policy = {
        "provider": "cosign", "repository": DISTROLESS[0], "binary": binary,
        "identity": "wrong@example.com", "issuer": "https://accounts.google.com",
    }
    _exercise_builder(tmp_path, monkeypatch, DISTROLESS, policy, reject=True)


def test_real_cosign_public_key(tmp_path, monkeypatch):
    binary = _require_environment("PYCONTAINER_COSIGN_BIN")
    key = _require_environment("PYCONTAINER_COSIGN_TEST_KEY")
    policy = {
        "provider": "cosign", "repository": QUAY[0], "binary": binary,
        "key": str(Path(key)),
    }
    _exercise_builder(tmp_path, monkeypatch, QUAY, policy)


def test_real_cosign_wrong_key_fails_before_blobs(tmp_path, monkeypatch):
    binary = _require_environment("PYCONTAINER_COSIGN_BIN")
    key = _require_environment("PYCONTAINER_COSIGN_WRONG_KEY")
    policy = {
        "provider": "cosign", "repository": QUAY[0], "binary": binary,
        "key": str(Path(key)),
    }
    _exercise_builder(tmp_path, monkeypatch, QUAY, policy, reject=True)


def _exercise_notation_builder(tmp_path, monkeypatch, image, policy, *, reject=False):
    state = {"verifying": False, "verified": False, "signature_blobs": 0}
    original_verify = builder_module.verify_base_image
    original_pull_blob = RegistryClient.pull_blob

    def verify(*args):
        state["verifying"] = True
        try:
            original_verify(*args)
            state["verified"] = True
        finally:
            state["verifying"] = False

    def stop_before_base_blob(self, blob_digest, destination, expected_size):
        if state["verifying"]:
            state["signature_blobs"] += 1
            return original_pull_blob(self, blob_digest, destination, expected_size)
        assert state["verified"], "base blob requested before verification"
        raise _BaseBlobReached

    monkeypatch.setattr(builder_module, "verify_base_image", verify)
    monkeypatch.setattr(RegistryClient, "pull_blob", stop_before_base_blob)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *_: RegistryAuth())
    config = BuildConfig(
        context_dir=str(tmp_path), output_dir=str(tmp_path / "output"),
        base_image=f"{image[0]}@{image[1]}", base_image_trust=policy,
        use_cache=False,
    )
    layers = tmp_path / "layers"
    layers.mkdir()
    expected_error = TrustVerificationError if reject else _BaseBlobReached
    with pytest.raises(expected_error):
        ImageBuilder(config)._pull_base_image(layers, "linux", "amd64")
    assert state["verified"] is (not reject)
    return state


@pytest.mark.parametrize("failure", [None, "wrong-timestamp-root", "wrong-fingerprint"])
def test_real_notation_snapshot_through_builder(tmp_path, monkeypatch, failure):
    binary = _require_environment("PYCONTAINER_NOTATION_BIN")
    root = _require_environment("PYCONTAINER_NOTATION_ROOT")
    tsa = _require_environment("PYCONTAINER_NOTATION_TSA")
    policy = {
        "provider": "notation", "repository": MCR[0], "binary": binary,
        "identity": (
            "CN=Microsoft SCD Products RSA Signing,O=Microsoft Corporation,"
            "L=Redmond,ST=Washington,C=US"
        ),
        "signer_fingerprint": "279fe22c1705ff7d5ab8a00f15b4321bc1120a891be5b01b6acde032fa1c8c5e",
        "trust_root": root, "tsa_root": tsa,
    }
    if failure == "wrong-timestamp-root":
        policy["tsa_root"] = root
    elif failure == "wrong-fingerprint":
        policy["signer_fingerprint"] = "0" * 64
    _exercise_notation_builder(tmp_path, monkeypatch, MCR, policy, reject=failure is not None)


def test_real_notation_expired_signer_fails_before_base_blobs(tmp_path, monkeypatch):
    binary = _require_environment("PYCONTAINER_NOTATION_BIN")
    root = _require_environment("PYCONTAINER_NOTATION_EXPIRED_ROOT")
    policy = {
        "provider": "notation", "repository": RATIFY[0], "binary": binary,
        "identity": "CN=ratify.dev,O=ratify-project,L=Seattle,ST=WA,C=US",
        "signer_fingerprint": "f80da6ff59c2ca0db15baf24fac3140c240495beda4c7f193e7c0ce03dde3079",
        "trust_root": root,
    }
    state = _exercise_notation_builder(tmp_path, monkeypatch, RATIFY, policy, reject=True)
    assert state["signature_blobs"] == 1


def _mirror_manifest(source, target, reference, expected=None):
    manifest, digest, body = source.pull_manifest_bytes(reference, expected_digest=expected)
    target.manifest(reference, body, manifest["mediaType"])
    target.manifest(digest, body, manifest["mediaType"])
    return manifest, digest


def _mirror_blobs(source, target, descriptors):
    with tempfile.TemporaryDirectory() as directory:
        for descriptor in descriptors:
            path = Path(directory) / descriptor["digest"].replace(":", "-")
            source.pull_blob(descriptor["digest"], path, descriptor["size"])
            target.blob(path.read_bytes())


def _private_mirror(image, provider):
    """Copy real public signature evidence into a Basic-auth loopback registry."""
    from test_trust_private_registry import _Server
    from pycontainer import trust_notation

    registry, repository = image[0].split("/", 1)
    source = RegistryClient(registry, repository)
    server = _Server(repository=repository)
    manifest, digest = _mirror_manifest(source, server, image[1], image[1])
    for child in manifest.get("manifests", []):
        _mirror_manifest(source, server, child["digest"], child["digest"])
    if provider == "cosign":
        signatures, _ = _mirror_manifest(source, server, f"sha256-{digest[7:]}.sig")
        _mirror_blobs(source, server, [signatures["config"], *signatures["layers"]])
    else:
        index, body = source.pull_referrers(digest, trust_notation._SIGNATURE, 4 << 20)
        server.routes[f"/v2/{repository}/referrers/{digest}"] = (
            200, body, {"Content-Type": index["mediaType"]})
        for descriptor in index["manifests"]:
            signature, _ = _mirror_manifest(source, server, descriptor["digest"], descriptor["digest"])
            _mirror_blobs(source, server, signature["layers"])
    server.requests.clear()
    return server


@pytest.mark.parametrize("provider", ["cosign", "notation"])
def test_real_verifier_with_authenticated_private_registry(tmp_path, monkeypatch, provider):
    from test_trust_private_registry import BASIC, PASSWORD, USERNAME
    from pycontainer import trust_notation

    if provider == "cosign":
        image = QUAY
        policy = {"provider": "cosign", "repository": QUAY[0],
                  "binary": _require_environment("PYCONTAINER_COSIGN_BIN"),
                  "key": _require_environment("PYCONTAINER_COSIGN_TEST_KEY")}
        module, name = base_trust, "_run_cosign"
    else:
        image = MCR
        policy = {
            "provider": "notation", "repository": MCR[0],
            "binary": _require_environment("PYCONTAINER_NOTATION_BIN"),
            "identity": (
                "CN=Microsoft SCD Products RSA Signing,O=Microsoft Corporation,"
                "L=Redmond,ST=Washington,C=US"
            ),
            "signer_fingerprint": (
                "279fe22c1705ff7d5ab8a00f15b4321bc1120a891be5b01b6acde032fa1c8c5e"),
            "trust_root": _require_environment("PYCONTAINER_NOTATION_ROOT"),
            "tsa_root": _require_environment("PYCONTAINER_NOTATION_TSA"),
        }
        module, name = trust_notation, "_run"
    server = _private_mirror(image, provider)
    secret = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    verifier_calls = []
    real_run = getattr(module, name)

    def run(args, environment):
        for value in [*args, *environment.values()]:
            assert PASSWORD not in value and secret not in value
        verifier_calls.append(args)
        return real_run(args, environment)

    class LoopbackClient(RegistryClient):
        def __init__(self, *args, **kwargs):
            assert kwargs["strict_auth"] is True and kwargs["password"] == PASSWORD
            super().__init__(*args, **kwargs)
            self.base_url = server.url + "/v2"

        def pull_blob(self, blob_digest, destination, expected_size):
            if not verifier_calls:
                assert any(prefix in str(destination)
                           for prefix in ("pycontainer-trust-", ".notation-"))
                return super().pull_blob(blob_digest, destination, expected_size)
            raise _BaseBlobReached

    monkeypatch.setattr(module, name, run)
    monkeypatch.setattr(builder_module, "RegistryClient", LoopbackClient)
    monkeypatch.setattr(builder_module, "get_registry_auth",
                        lambda *_: RegistryAuth(username=USERNAME, password=PASSWORD))
    monkeypatch.setenv("REGISTRY_PASSWORD", PASSWORD)
    config = BuildConfig(
        context_dir=str(tmp_path), output_dir=str(tmp_path / "output"),
        base_image=f"{image[0]}@{image[1]}", base_image_trust=policy, use_cache=False,
    )
    layers = tmp_path / "layers"
    layers.mkdir()
    try:
        with pytest.raises(_BaseBlobReached):
            ImageBuilder(config)._pull_base_image(layers, "linux", "amd64")
        assert len(verifier_calls) == 1
        assert server.requests and all(auth == BASIC for _, auth in server.requests)
        unauthenticated = RegistryClient(image[0].split("/", 1)[0], server.repository,
                                         strict_auth=True)
        unauthenticated.base_url = server.url + "/v2"
        with pytest.raises(Exception):
            unauthenticated.pull_manifest_bytes(image[1])
    finally:
        server.close()
