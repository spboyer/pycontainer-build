"""Verify a single, fingerprint-pinned Notation signature in an isolated layout."""

import base64
import binascii
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .base_trust import TrustVerificationError, resolve_verifier_executable, run_bounded_verifier
from .blob_store import BlobIntegrityError
from .registry_client import RegistryRequestError


class NotationTrustError(RuntimeError):
    """Notation evidence or verification was unavailable or invalid."""


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_REPOSITORY = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)+\Z")
_MEDIA = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_SIGNATURE = "application/vnd.cncf.notary.signature"
_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_ENVELOPES = {"application/cose", "application/jose+json"}
_MAX_MANIFEST = 4 * 1024 * 1024
_MAX_ENVELOPE = 16 * 1024 * 1024
_MAX_OUTPUT = 4 * 1024 * 1024
_TIMEOUT = 120


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _regular_file(path, label):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise NotationTrustError(f"Invalid {label} path")
    file = Path(path).absolute()
    if any(part.is_symlink() for part in (file, *file.parents)) or not file.is_file():
        raise NotationTrustError(f"{label} must be a regular file without symlinks")
    return file


def _certificate(path, label):
    file = _regular_file(path, label)
    with file.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if not data or len(data) > 1024 * 1024:
        raise NotationTrustError(f"Invalid {label} size")
    return data


def _run(args, env):
    try:
        return run_bounded_verifier(
            args, env, _TIMEOUT, _MAX_OUTPUT, "Notation",
        )
    except TrustVerificationError as exc:
        raise NotationTrustError(str(exc)) from exc


def _json(data):
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise NotationTrustError("Invalid Notation JSON") from exc


def _descriptor(value, expected=None):
    if (not isinstance(value, dict) or not isinstance(value.get("digest"), str)
            or not _DIGEST.fullmatch(value["digest"])
            or type(value.get("size")) is not int or value["size"] < 1
            or not isinstance(value.get("mediaType"), str)):
        raise NotationTrustError("Invalid descriptor")
    if expected is not None and any(value.get(k) != v for k, v in expected.items()):
        raise NotationTrustError("Descriptor does not match the immutable subject")
    return value


def _write_blob(layout, data):
    digest = _digest(data)
    (layout / "blobs" / "sha256" / digest[7:]).write_bytes(data)
    return digest


def _encode(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _unique_json(data):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise NotationTrustError("Duplicate JWS JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    try:
        text = data.decode("utf-8")
        if text.startswith("\ufeff"):
            raise ValueError("JSON byte order mark")
        return json.loads(
            text, object_pairs_hook=object_pairs, parse_constant=reject_constant,
        )
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise NotationTrustError("Invalid JWS JSON") from exc


def _decode_jws_base64(value, *, url=False):
    if not isinstance(value, str) or not value:
        raise NotationTrustError("Invalid JWS encoding")
    try:
        if url:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise NotationTrustError("Invalid JWS base64url")
            data = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            canonical = base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
        else:
            data = base64.b64decode(value, validate=True)
            canonical = base64.b64encode(data).decode("ascii")
    except (ValueError, binascii.Error) as exc:
        raise NotationTrustError("Invalid JWS encoding") from exc
    if not data or canonical != value:
        raise NotationTrustError("Noncanonical JWS encoding")
    return data


def _check_jws_envelope(envelope, fingerprint):
    obj = _unique_json(envelope)
    if not isinstance(obj, dict) or set(obj) != {"payload", "protected", "header", "signature"}:
        raise NotationTrustError("Only flattened single-signer JWS is supported")
    _decode_jws_base64(obj["payload"], url=True)
    _decode_jws_base64(obj["signature"], url=True)
    protected = _unique_json(_decode_jws_base64(obj["protected"], url=True))
    header = obj["header"]
    prefix = "io.cncf.notary."
    allowed_protected = {"alg", "cty", "crit", prefix + "signingScheme",
                         prefix + "signingTime", prefix + "authenticSigningTime",
                         prefix + "expiry"}
    allowed_header = {"x5c", prefix + "timestampSignature", prefix + "signingAgent"}
    if (not isinstance(protected, dict) or not isinstance(header, dict)
            or set(protected) - allowed_protected or set(header) - allowed_header
            or set(protected) & set(header)
            or not isinstance(protected.get("alg"), str)
            or protected["alg"] not in {"PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
            or protected.get("cty") != "application/vnd.cncf.notary.payload.v1+json"
            or protected.get(prefix + "signingScheme") not in ("notary.x509", "notary.x509.signingAuthority")):
        raise NotationTrustError("Invalid JWS headers")
    critical = protected.get("crit")
    required_critical = {prefix + "signingScheme"}
    optional_critical = {prefix + "authenticSigningTime", prefix + "expiry"}
    if (not isinstance(critical, list) or not all(isinstance(item, str) for item in critical)
            or len(critical) != len(set(critical))
            or not required_critical <= set(critical)
            or set(critical) - (required_critical | optional_critical)
            or any(item not in protected for item in critical)
            or any(item in protected and item not in critical for item in optional_critical)
            or (protected[prefix + "signingScheme"] == "notary.x509.signingAuthority"
                and prefix + "authenticSigningTime" not in critical)):
        raise NotationTrustError("Invalid JWS critical headers")
    signing_time = prefix + "signingTime"
    authentic_time = prefix + "authenticSigningTime"
    if ((protected[prefix + "signingScheme"] == "notary.x509"
         and (signing_time not in protected or authentic_time in protected))
            or (protected[prefix + "signingScheme"] == "notary.x509.signingAuthority"
                and (authentic_time not in protected or signing_time in protected))):
        raise NotationTrustError("Invalid JWS signing time")
    for key in (signing_time, authentic_time, prefix + "expiry"):
        if key in protected:
            value = protected[key]
            if (not isinstance(value, str) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value)):
                raise NotationTrustError("Invalid JWS timestamp")
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise NotationTrustError("Invalid JWS timestamp") from exc
    chain = header.get("x5c")
    if not isinstance(chain, list) or not 1 <= len(chain) <= 16:
        raise NotationTrustError("Invalid JWS certificate chain")
    certificates = [_decode_jws_base64(cert) for cert in chain]
    if any(len(cert) > 1024 * 1024 for cert in certificates):
        raise NotationTrustError("JWS certificate exceeds size limit")
    if hashlib.sha256(certificates[0]).hexdigest() != fingerprint:
        raise NotationTrustError("JWS signer fingerprint does not match policy")


def verify_notation(policy: dict[str, str], repository: str, digest: str,
                    manifest: dict, manifest_bytes: bytes, client) -> None:
    """Verify one selected envelope, never a remote verifier's success alone."""
    try:
        _verify_notation(policy, repository, digest, manifest, manifest_bytes, client)
    except NotationTrustError:
        raise
    except (OSError, ValueError, BlobIntegrityError, RegistryRequestError) as exc:
        raise NotationTrustError(f"Notation verification failed: {exc}") from exc


def _verify_notation(policy, repository, digest, manifest, manifest_bytes, client):
    if (not isinstance(policy, dict) or policy.get("provider") != "notation"
            or set(policy) - {"provider", "repository", "identity", "signer_fingerprint",
                              "trust_root", "tsa_root", "binary", "auth_realm"}):
        raise NotationTrustError("Invalid Notation policy")
    if "auth_realm" in policy:
        realm = policy["auth_realm"]
        if not isinstance(realm, str) or realm != realm.strip():
            raise NotationTrustError("Invalid registry authentication realm")
        parsed = urlsplit(realm)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment):
            raise NotationTrustError("Invalid registry authentication realm")
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository) or policy.get("repository") != repository:
        raise NotationTrustError("Notation repository scope mismatch")
    identity = policy.get("identity")
    fingerprint = policy.get("signer_fingerprint")
    if (not isinstance(identity, str) or not identity or identity != identity.strip()
            or len(identity) > 2048 or any(c in identity for c in "*\r\n\x00")
            or identity.startswith("x509.subject:") or "=" not in identity):
        raise NotationTrustError("Invalid exact Notation identity")
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise NotationTrustError("Invalid signer fingerprint")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise NotationTrustError("Invalid subject digest")
    if (not isinstance(manifest_bytes, bytes) or not 0 < len(manifest_bytes) <= _MAX_MANIFEST
            or _digest(manifest_bytes) != digest or not isinstance(manifest, dict)
            or _json(manifest_bytes) != manifest or manifest.get("schemaVersion") != 2
            or manifest.get("mediaType") not in _MEDIA):
        raise NotationTrustError("Invalid immutable subject manifest")
    subject = {"mediaType": manifest["mediaType"], "digest": digest, "size": len(manifest_bytes)}
    root = _certificate(policy.get("trust_root"), "trust root")
    tsa = _certificate(policy["tsa_root"], "TSA root") if "tsa_root" in policy else None
    binary = policy.get("binary", "notation")
    if not isinstance(binary, str) or not binary or "\x00" in binary:
        raise NotationTrustError("Invalid Notation executable")
    executable = resolve_verifier_executable(binary)
    if not executable:
        raise NotationTrustError("Notation executable not found")
    try:
        executable = str(Path(executable).resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise NotationTrustError("Cannot resolve Notation executable") from exc
    executable = str(_regular_file(executable, "Notation executable"))

    # Keep all generated state private to this operation and independent of user policy/plugins.
    with tempfile.TemporaryDirectory(prefix=".notation-") as temp:
        base = Path(temp)
        config = base / "config"
        layout = base / "layout"
        (layout / "blobs" / "sha256").mkdir(parents=True)
        (config / "truststore" / "x509" / "ca" / "pinned").mkdir(parents=True)
        (config / "truststore" / "x509" / "ca" / "pinned" / "root.crt").write_bytes(root)
        stores = ["ca:pinned"]
        verification = {"level": "strict"}
        if tsa is not None:
            path = config / "truststore" / "x509" / "tsa" / "pinned"
            path.mkdir(parents=True)
            (path / "root.crt").write_bytes(tsa)
            stores.append("tsa:pinned")
            verification["verifyTimestamp"] = "always"
        trust_policy = {"version": "1.0", "trustPolicies": [{
            "name": "pinned", "registryScopes": [repository],
            "signatureVerification": verification, "trustStores": stores,
            "trustedIdentities": ["x509.subject: " + identity],
        }]}
        (config / "trustpolicy.json").write_bytes(_encode(trust_policy))
        env = os.environ.copy()
        for name in list(env):
            if name.startswith("NOTATION_") or name in (
                    "DOCKER_CONFIG", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                    "XDG_CACHE_HOME", "HOME", "USERPROFILE", "APPDATA",
                    "LOCALAPPDATA", "REGISTRY_AUTH_FILE", "DOCKER_AUTH_CONFIG"):
                env.pop(name, None)
        env.update({"NOTATION_CONFIG": str(config), "DOCKER_CONFIG": str(base / "docker"),
                    "REGISTRY_AUTH_FILE": str(base / "empty-auth.json"),
                    "XDG_CONFIG_HOME": str(base), "XDG_DATA_HOME": str(base),
                    "XDG_CACHE_HOME": str(base), "HOME": str(base), "USERPROFILE": str(base),
                    "APPDATA": str(base), "LOCALAPPDATA": str(base), "PATH": "",
                    "NOTATION_EXPERIMENTAL": "1"})
        inspection = _json(_run([executable, "inspect", "--output", "json",
                                 repository + "@" + digest], env))
        signatures = inspection.get("Signatures") if isinstance(inspection, dict) else None
        if not isinstance(signatures, list) or not signatures or len(signatures) > 100:
            raise NotationTrustError("No bounded Notation signature evidence")
        candidates = []
        for signature in signatures:
            if not isinstance(signature, dict):
                continue
            certs = signature.get("certificates")
            if (isinstance(certs, list) and certs and isinstance(certs[0], dict)
                    and certs[0].get("issuedTo") == identity
                    and certs[0].get("SHA256Fingerprint") == fingerprint
                    and signature.get("mediaType") in _ENVELOPES):
                _descriptor(signature.get("signedArtifact"), subject)
                candidate = signature.get("digest")
                if isinstance(candidate, str) and _DIGEST.fullmatch(candidate):
                    candidates.append((candidate, signature["mediaType"]))
        if len(candidates) != 1:
            raise NotationTrustError("Expected exactly one matching signature")
        signature_digest, envelope_media = candidates[0]
        signature_manifest, actual_digest, raw = client.pull_manifest_bytes(
            signature_digest, expected_digest=signature_digest)
        if (not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_MANIFEST
                or actual_digest != signature_digest or _digest(raw) != signature_digest
                or _json(raw) != signature_manifest or not isinstance(signature_manifest, dict)
                or signature_manifest.get("schemaVersion") != 2
                or signature_manifest.get("mediaType") != _OCI_MANIFEST):
            raise NotationTrustError("Invalid signature manifest")
        _descriptor(signature_manifest.get("subject"), subject)
        config_descriptor = signature_manifest.get("config")
        if (signature_manifest.get("artifactType") != _SIGNATURE
                and (not isinstance(config_descriptor, dict) or config_descriptor.get("mediaType") != _SIGNATURE)):
            raise NotationTrustError("Not a Notation signature manifest")
        layers = signature_manifest.get("layers")
        if not isinstance(layers, list) or len(layers) != 1:
            raise NotationTrustError("Signature manifest must contain exactly one envelope")
        envelope = _descriptor(layers[0])
        if envelope["mediaType"] != envelope_media or envelope["size"] > _MAX_ENVELOPE:
            raise NotationTrustError("Invalid signature envelope")
        blob = layout / "blobs" / "sha256" / envelope["digest"][7:]
        if not client.pull_blob(envelope["digest"], blob, envelope["size"]):
            raise NotationTrustError("Cannot fetch signature envelope")
        if not blob.is_file() or blob.is_symlink() or blob.stat().st_size != envelope["size"]:
            raise NotationTrustError("Invalid signature envelope size")
        envelope_bytes = blob.read_bytes()
        if _digest(envelope_bytes) != envelope["digest"]:
            raise NotationTrustError("Invalid signature envelope digest")
        # A tagged COSE_Sign1 has one signer; COSE_Sign may contain multiple.
        if envelope_media == "application/cose" and not envelope_bytes.startswith(b"\xd2\x84"):
            raise NotationTrustError("Only tagged single-signer COSE_Sign1 is supported")
        if envelope_media == "application/jose+json":
            _check_jws_envelope(envelope_bytes, fingerprint)
        _write_blob(layout, manifest_bytes)
        empty_digest = _write_blob(layout, b"{}")
        wrapper = {"schemaVersion": 2, "mediaType": _OCI_MANIFEST,
                   "config": {"mediaType": _SIGNATURE, "digest": empty_digest, "size": 2},
                   "layers": [envelope], "subject": subject}
        wrapper_bytes = _encode(wrapper)
        wrapper_digest = _write_blob(layout, wrapper_bytes)
        (layout / "oci-layout").write_bytes(_encode({"imageLayoutVersion": "1.0.0"}))
        (layout / "index.json").write_bytes(_encode({"schemaVersion": 2, "manifests": [
            subject, {"mediaType": _OCI_MANIFEST, "digest": wrapper_digest, "size": len(wrapper_bytes)}
        ]}))
        reference = str(layout) + "@" + digest
        output = _run([executable, "verify", "--oci-layout", "--scope", repository,
                       reference], env)
        expected = ("Successfully verified signature for " + reference).encode("utf-8")
        if not output.splitlines() or output.splitlines()[0] != expected:
            raise NotationTrustError("Notation did not confirm signature verification for the exact subject")
