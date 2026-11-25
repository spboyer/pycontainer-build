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
_INHERITED_ENVIRONMENT = frozenset({"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "TZ"})
_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_ENVELOPES = {"application/cose", "application/jose+json"}
_MAX_MANIFEST = 4 * 1024 * 1024
_MAX_ENVELOPE = 16 * 1024 * 1024
_MAX_ENVELOPE_TOTAL = 64 * 1024 * 1024
_MAX_REFERRERS = 1000
_MAX_CANDIDATES = 16
_MAX_CERTIFICATES = 16
_MAX_CERTIFICATE = 1024 * 1024
_MAX_CBOR_DEPTH = 16
_MAX_CBOR_ITEMS = 1024
_INDEX = "application/vnd.oci.image.index.v1+json"
_COSE_X5CHAIN = 33
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


def _check_jws_envelope(envelope, fingerprint=None):
    """Strictly parse a flattened JWS and return its leaf certificate."""
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
    if not isinstance(chain, list) or not 1 <= len(chain) <= _MAX_CERTIFICATES:
        raise NotationTrustError("Invalid JWS certificate chain")
    certificates = [_decode_jws_base64(cert) for cert in chain]
    if any(len(cert) > _MAX_CERTIFICATE for cert in certificates):
        raise NotationTrustError("JWS certificate exceeds size limit")
    if fingerprint is not None and hashlib.sha256(certificates[0]).hexdigest() != fingerprint:
        raise NotationTrustError("JWS signer fingerprint does not match policy")
    return certificates[0]


class _CborTag:
    __slots__ = ("tag", "value")

    def __init__(self, tag, value):
        self.tag, self.value = tag, value


def _cbor_item(data, pos, depth):
    if depth > _MAX_CBOR_DEPTH or pos >= len(data):
        raise NotationTrustError("Invalid COSE encoding")
    initial = data[pos]
    major, info = initial >> 5, initial & 0x1F
    pos += 1
    if info < 24:
        value = info
    elif info <= 27:
        width = 1 << (info - 24)
        if pos + width > len(data):
            raise NotationTrustError("Truncated COSE encoding")
        value = int.from_bytes(data[pos:pos + width], "big")
        pos += width
    else:
        raise NotationTrustError("Indefinite or reserved COSE encoding")
    if major == 0:
        return value, pos
    if major == 1:
        return -1 - value, pos
    if major in (2, 3):
        end = pos + value
        if end > len(data):
            raise NotationTrustError("Truncated COSE string")
        chunk = data[pos:end]
        if major == 3:
            try:
                chunk = chunk.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise NotationTrustError("Invalid COSE text") from exc
        return chunk, end
    if major in (4, 5):
        if value > _MAX_CBOR_ITEMS:
            raise NotationTrustError("COSE structure exceeds size limit")
        if major == 4:
            items = []
            for _ in range(value):
                item, pos = _cbor_item(data, pos, depth + 1)
                items.append(item)
            return items, pos
        result = {}
        for _ in range(value):
            key, pos = _cbor_item(data, pos, depth + 1)
            if type(key) not in (int, str) or key in result:
                raise NotationTrustError("Invalid or duplicate COSE map key")
            result[key], pos = _cbor_item(data, pos, depth + 1)
        return result, pos
    if major == 6:
        item, pos = _cbor_item(data, pos, depth + 1)
        return _CborTag(value, item), pos
    return _CborTag(-1, value), pos


def _cbor_decode(data):
    item, end = _cbor_item(data, 0, 0)
    if end != len(data):
        raise NotationTrustError("Trailing COSE data")
    return item


def _cose_leaf(envelope):
    """Return the leaf of a single-signer COSE_Sign1 envelope's unprotected x5chain."""
    message = _cbor_decode(envelope)
    if (not isinstance(message, _CborTag) or message.tag != 18
            or not isinstance(message.value, list) or len(message.value) != 4):
        raise NotationTrustError("Only tagged single-signer COSE_Sign1 is supported")
    protected_bytes, unprotected, payload, signature = message.value
    if (not isinstance(protected_bytes, bytes) or not protected_bytes
            or not isinstance(unprotected, dict) or not isinstance(payload, bytes)
            or not isinstance(signature, bytes) or not signature):
        raise NotationTrustError("Invalid COSE_Sign1 structure")
    protected = _cbor_decode(protected_bytes)
    if (not isinstance(protected, dict) or _COSE_X5CHAIN in protected
            or set(protected) & set(unprotected)):
        raise NotationTrustError("Invalid COSE headers")
    chain = unprotected.get(_COSE_X5CHAIN)
    if isinstance(chain, bytes):
        chain = [chain]
    if (not isinstance(chain, list) or not 1 <= len(chain) <= _MAX_CERTIFICATES
            or not all(isinstance(cert, bytes) and 0 < len(cert) <= _MAX_CERTIFICATE
                       for cert in chain)):
        raise NotationTrustError("Invalid COSE certificate chain")
    return chain[0]


def _leaf_certificate(envelope, media_type):
    if media_type == "application/cose":
        return _cose_leaf(envelope)
    return _check_jws_envelope(envelope)


def _referrer_index(client, digest):
    """Discover signature descriptors via the referrers API or its tag fallback."""
    found = client.pull_referrers(digest, _SIGNATURE, _MAX_MANIFEST)
    if found is None:
        tagged = client.pull_optional_manifest_bytes("sha256-" + digest[7:], _MAX_MANIFEST)
        if tagged is None:
            raise NotationTrustError("No Notation signature evidence was found")
        index = tagged[0]
    else:
        index = found[0]
    manifests = index.get("manifests") if isinstance(index, dict) else None
    if (not isinstance(manifests, list) or index.get("schemaVersion") != 2
            or index.get("mediaType") != _INDEX or len(manifests) > _MAX_REFERRERS):
        raise NotationTrustError("Invalid or unbounded Notation referrers index")
    candidates = {}
    for item in manifests:
        if not isinstance(item, dict) or item.get("artifactType") != _SIGNATURE:
            continue
        descriptor = _descriptor(item, {"mediaType": _OCI_MANIFEST})
        if descriptor["size"] > _MAX_MANIFEST:
            raise NotationTrustError("Notation signature manifest exceeds size limit")
        previous = candidates.setdefault(descriptor["digest"], descriptor)
        if previous["size"] != descriptor["size"]:
            raise NotationTrustError("Conflicting Notation signature descriptors")
    if not candidates:
        raise NotationTrustError("No Notation signature evidence was found")
    if len(candidates) > _MAX_CANDIDATES:
        raise NotationTrustError("Too many Notation signatures to evaluate")
    return list(candidates.values())


def _fetch_candidate(client, descriptor, subject, directory, budget):
    signature_digest = descriptor["digest"]
    signature_manifest, actual_digest, raw = client.pull_manifest_bytes(
        signature_digest, expected_digest=signature_digest, expected_size=descriptor["size"])
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
    if envelope["mediaType"] not in _ENVELOPES or envelope["size"] > _MAX_ENVELOPE:
        raise NotationTrustError("Invalid signature envelope")
    if envelope["size"] > budget:
        raise NotationTrustError("Notation signature evidence exceeds size limit")
    blob = directory / envelope["digest"][7:]
    if not client.pull_blob(envelope["digest"], blob, envelope["size"]):
        raise NotationTrustError("Cannot fetch signature envelope")
    if not blob.is_file() or blob.is_symlink() or blob.stat().st_size != envelope["size"]:
        raise NotationTrustError("Invalid signature envelope size")
    envelope_bytes = blob.read_bytes()
    if _digest(envelope_bytes) != envelope["digest"]:
        raise NotationTrustError("Invalid signature envelope digest")
    return envelope, envelope_bytes


def _select_pinned_envelope(client, digest, subject, fingerprint, directory):
    """Return the one discovered envelope whose actual leaf matches the pin."""
    directory.mkdir()
    matches = []
    budget = _MAX_ENVELOPE_TOTAL
    for descriptor in _referrer_index(client, digest):
        envelope, envelope_bytes = _fetch_candidate(
            client, descriptor, subject, directory, budget)
        budget -= envelope["size"]
        leaf = _leaf_certificate(envelope_bytes, envelope["mediaType"])
        if hashlib.sha256(leaf).hexdigest() == fingerprint:
            matches.append((envelope, envelope_bytes))
    if not matches:
        raise NotationTrustError("No Notation signature matches the pinned signer fingerprint")
    if len(matches) != 1:
        raise NotationTrustError("Expected exactly one signature from the pinned signer")
    return matches[0]


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
        # Verification is offline, so nothing beyond OS essentials is inherited;
        # registry credentials in the builder's environment never reach Notation.
        env = {name: value for name, value in os.environ.items()
               if name.upper() in _INHERITED_ENVIRONMENT}
        env.update({"NOTATION_CONFIG": str(config), "DOCKER_CONFIG": str(base / "docker"),
                    "REGISTRY_AUTH_FILE": str(base / "empty-auth.json"),
                    "XDG_CONFIG_HOME": str(base), "XDG_DATA_HOME": str(base),
                    "XDG_CACHE_HOME": str(base), "HOME": str(base), "USERPROFILE": str(base),
                    "APPDATA": str(base), "LOCALAPPDATA": str(base), "PATH": "",
                    "NOTATION_EXPERIMENTAL": "1"})
        envelope, envelope_bytes = _select_pinned_envelope(
            client, digest, subject, fingerprint, base / "candidates")
        if envelope["mediaType"] == "application/jose+json":
            _check_jws_envelope(envelope_bytes, fingerprint)
        _write_blob(layout, envelope_bytes)
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
