"""Opt-in verification of immutable registry base-image subjects."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from pathlib import Path
from typing import Dict

from .blob_store import BlobIntegrityError, digest_filename
from .registry_client import RegistryRequestError, parse_image_reference


VERIFIER_TIMEOUT_SECONDS = 180
MAX_VERIFIER_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_TRUST_MATERIAL_BYTES = 4 * 1024 * 1024
MAX_COSIGN_SIGNATURE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_COSIGN_SIGNATURE_BLOB_BYTES = 1024 * 1024
MAX_COSIGN_SIGNATURES = 64
_COSIGN_SIGNATURE_MEDIA = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_INDEX_MEDIA = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
_IMAGE_MEDIA = tuple(sorted(_COSIGN_SIGNATURE_MEDIA))


class TrustVerificationError(RuntimeError):
    """Required base-image trust could not be established."""


def resolve_verifier_executable(binary: str) -> str | None:
    """Resolve a verifier without Windows' implicit current-directory search."""
    if not isinstance(binary, str) or not binary or "\0" in binary:
        return None
    if os.name != "nt" or os.path.dirname(binary):
        executable = shutil.which(binary)
        return os.path.abspath(executable) if executable else None

    extensions = [
        ext.rstrip(".") for ext in
        (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
        if ext
    ]
    names = [binary + extension for extension in extensions]
    if any(binary.upper().endswith(extension.upper()) for extension in extensions):
        names.insert(0, binary)
    for directory in os.get_exec_path():
        if not directory:
            continue
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return os.path.abspath(candidate)
    return None


def canonical_repository(registry: str, repository: str) -> str:
    if registry in {"index.docker.io", "registry-1.docker.io"}:
        registry = "docker.io"
    return f"{registry}/{repository}"


def validate_trust_policy(policy: Dict[str, str]) -> None:
    if not isinstance(policy, dict) or not policy:
        raise ValueError("base_image_trust must be a non-empty table.")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in policy.items()):
        raise ValueError("Every base_image_trust setting must be a string.")
    if any(not value or value != value.strip() or "\0" in value for value in policy.values()):
        raise ValueError("base_image_trust settings must be non-empty strings.")

    provider = policy.get("provider")
    if provider not in {"cosign", "notation"}:
        raise ValueError("base_image_trust.provider must be 'cosign' or 'notation'.")
    allowed = {"provider", "repository", "binary", "auth_realm"}
    allowed |= (
        {"key", "identity", "issuer", "trust_root"}
        if provider == "cosign"
        else {"identity", "signer_fingerprint", "trust_root", "tsa_root"}
    )
    unknown = sorted(set(policy) - allowed)
    if unknown:
        raise ValueError(f"Unknown base_image_trust setting(s): {', '.join(unknown)}.")

    scope = policy.get("repository")
    if not scope:
        raise ValueError("base_image_trust.repository is required.")
    try:
        registry, repository, _ = parse_image_reference(scope)
    except ValueError as error:
        raise ValueError("base_image_trust.repository is invalid.") from error
    component = r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*"
    if (
        scope != canonical_repository(registry, repository)
        or registry != registry.lower()
        or not re.fullmatch(rf"{component}(?:/{component})*", repository)
    ):
        raise ValueError(
            "base_image_trust.repository must be an exact canonical registry/repository "
            "without a tag, digest, or wildcard."
        )

    identity = policy.get("identity")
    if identity and "*" in identity:
        raise ValueError("base_image_trust.identity must be exact, not a wildcard.")
    if provider == "cosign":
        key = policy.get("key")
        issuer = policy.get("issuer")
        if key and (identity or issuer):
            raise ValueError("Cosign key and identity/issuer modes are mutually exclusive.")
        if not key and not (identity and issuer):
            raise ValueError("Cosign requires a key or an exact identity and issuer.")
        if issuer:
            _validate_https_url(issuer, "issuer")
    else:
        if not identity or identity.startswith("x509.subject:"):
            raise ValueError("Notation requires an exact X.509 subject without a prefix.")
        if not re.fullmatch(r"[0-9a-f]{64}", policy.get("signer_fingerprint", "")):
            raise ValueError("Notation requires a lowercase SHA-256 signer_fingerprint.")
        if not policy.get("trust_root"):
            raise ValueError("Notation requires a trust_root certificate.")

    if policy.get("auth_realm"):
        _validate_https_url(policy["auth_realm"], "auth_realm")


def _validate_https_url(value: str, name: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"base_image_trust.{name} must be an HTTPS URL.")


def _copy_material(path: str, destination: Path) -> None:
    source = Path(path).expanduser()
    if source.is_symlink() or not source.is_file():
        raise TrustVerificationError(f"Trust material is not a regular file: {source}")
    try:
        with source.open("rb") as stream:
            data = stream.read(MAX_TRUST_MATERIAL_BYTES + 1)
        if not data or len(data) > MAX_TRUST_MATERIAL_BYTES:
            raise TrustVerificationError(f"Trust material has an invalid size: {source}")
        destination.write_bytes(data)
    except OSError as error:
        raise TrustVerificationError(f"Cannot read trust material: {source}") from error


def run_bounded_verifier(command, environment, timeout, output_limit, label) -> bytes:
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=environment,
        )
    except FileNotFoundError as error:
        raise TrustVerificationError(f"{label} executable was not found.") from error
    except OSError as error:
        raise TrustVerificationError(f"{label} could not be executed.") from error

    output = bytearray()
    total = 0
    lock = threading.Lock()
    exceeded = threading.Event()
    stream_failed = threading.Event()

    def consume(stream, capture):
        nonlocal total
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                with lock:
                    total += len(chunk)
                    if total > output_limit:
                        exceeded.set()
                    elif capture:
                        output.extend(chunk)
                if exceeded.is_set():
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    break
        except (OSError, ValueError):
            stream_failed.set()
        finally:
            try:
                stream.close()
            except OSError:
                stream_failed.set()

    threads = [
        threading.Thread(target=consume, args=(process.stdout, True), daemon=True),
        threading.Thread(target=consume, args=(process.stderr, False), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    finally:
        for thread in threads:
            thread.join(timeout=2)
    # The output limit is the more precise diagnosis when it races the deadline.
    if exceeded.is_set():
        raise TrustVerificationError(f"{label} output exceeded its size limit.")
    if timed_out:
        raise TrustVerificationError(f"{label} verification timed out.")
    if any(thread.is_alive() for thread in threads):
        raise TrustVerificationError(f"{label} output streams did not close.")
    if stream_failed.is_set():
        raise TrustVerificationError(f"Cannot read {label} verification output.")
    if process.returncode:
        raise TrustVerificationError(
            f"{label} verification failed with exit code {process.returncode}."
        )
    return bytes(output)


_COSIGN_INHERITED_ENVIRONMENT = frozenset({
    "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "TZ", "HTTP_PROXY", "HTTPS_PROXY",
    "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
})


def _run_cosign(command, environment) -> bytes:
    executable = resolve_verifier_executable(command[0])
    if executable is None:
        raise TrustVerificationError("Cosign executable was not found.")
    return run_bounded_verifier(
        [executable, *command[1:]], environment, VERIFIER_TIMEOUT_SECONDS,
        MAX_VERIFIER_OUTPUT_BYTES, "Cosign",
    )


def _bounded_descriptor(value, label: str, max_size: int, media_types=None) -> dict:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["digest"])
        or type(value.get("size")) is not int
        or not 0 < value["size"] <= max_size
        or not isinstance(value.get("mediaType"), str)
        or (media_types is not None and value["mediaType"] not in media_types)
    ):
        raise TrustVerificationError(f"Cosign {label} descriptor is invalid or too large.")
    return value


def _stage_cosign_layout(
    client, layout: Path, digest: str, manifest: dict, manifest_bytes: bytes
) -> None:
    """Fetch tag-schema Cosign evidence through the strict registry client."""
    found = client.pull_optional_manifest_bytes(
        f"sha256-{digest[7:]}.sig", MAX_COSIGN_SIGNATURE_MANIFEST_BYTES
    )
    if found is None:
        raise TrustVerificationError(
            "No Cosign signature evidence was found for the base-image digest."
        )
    signatures, signatures_digest, signatures_bytes = found
    media_type = signatures.get("mediaType")
    layers = signatures.get("layers")
    if (
        signatures.get("schemaVersion") != 2
        or media_type not in _COSIGN_SIGNATURE_MEDIA
        or not isinstance(layers, list)
        or not 1 <= len(layers) <= MAX_COSIGN_SIGNATURES
    ):
        raise TrustVerificationError("Cosign signature manifest has an unsupported shape.")
    subject = signatures.get("subject")
    if subject is not None and (not isinstance(subject, dict) or subject.get("digest") != digest):
        raise TrustVerificationError("Cosign signature manifest names another subject.")
    descriptors = [_bounded_descriptor(
        signatures.get("config"), "config", MAX_COSIGN_SIGNATURE_BLOB_BYTES,
    )] + [
        _bounded_descriptor(
            layer, "signature", MAX_COSIGN_SIGNATURE_BLOB_BYTES,
            {"application/vnd.dev.cosign.simplesigning.v1+json"},
        )
        for layer in layers
    ]
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    for descriptor in {item["digest"]: item for item in descriptors}.values():
        if not client.pull_blob(
            descriptor["digest"], blobs / descriptor["digest"][7:], descriptor["size"]
        ):
            raise TrustVerificationError("Cosign signature blob could not be fetched.")
    (blobs / digest[7:]).write_bytes(manifest_bytes)
    (blobs / signatures_digest[7:]).write_bytes(signatures_bytes)
    subject_media = manifest.get("mediaType")
    if subject_media not in _INDEX_MEDIA + _IMAGE_MEDIA:
        raise TrustVerificationError("Base-image subject has no supported media type.")
    subject_kind = (
        "dev.cosignproject.cosign/imageIndex"
        if subject_media in _INDEX_MEDIA
        else "dev.cosignproject.cosign/image"
    )
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": subject_media,
                "size": len(manifest_bytes),
                "digest": digest,
                "annotations": {"kind": subject_kind},
            },
            {
                "mediaType": media_type,
                "size": len(signatures_bytes),
                "digest": signatures_digest,
                "annotations": {"kind": "dev.cosignproject.cosign/sigs"},
            },
        ],
    }
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")
    (layout / "index.json").write_text(json.dumps(index), encoding="utf-8")


def _verify_cosign(
    policy: Dict[str, str], repository: str, digest: str,
    manifest: dict, manifest_bytes: bytes, client,
) -> None:
    with tempfile.TemporaryDirectory(prefix="pycontainer-trust-") as directory:
        root = Path(directory)
        layout = root / "layout"
        _stage_cosign_layout(client, layout, digest, manifest, manifest_bytes)
        command = [
            policy.get("binary", "cosign"),
            "verify",
            "--local-image",
            "--check-claims=true",
            "--insecure-ignore-tlog=false",
            "--insecure-ignore-sct=false",
            "--output=json",
        ]
        if "key" in policy:
            key = root / "key.pem"
            _copy_material(policy["key"], key)
            command.append(f"--key={key}")
        else:
            command.extend([
                f"--certificate-identity={policy['identity']}",
                f"--certificate-oidc-issuer={policy['issuer']}",
            ])
        if "trust_root" in policy:
            trusted_root = root / "trusted-root.json"
            _copy_material(policy["trust_root"], trusted_root)
            command.append(f"--trusted-root={trusted_root}")
        command.append(str(layout))
        # Only OS essentials plus proxy/CA settings (keyless TUF and Rekor access)
        # are inherited, so builder registry credentials never reach Cosign.
        environment = {
            key: value for key, value in os.environ.items()
            if key.upper() in _COSIGN_INHERITED_ENVIRONMENT
        }
        environment.update({
            "HOME": str(root), "USERPROFILE": str(root), "APPDATA": str(root),
            "LOCALAPPDATA": str(root), "XDG_CONFIG_HOME": str(root),
            "XDG_DATA_HOME": str(root), "XDG_CACHE_HOME": str(root),
            "DOCKER_CONFIG": str(root / "docker"),
            "REGISTRY_AUTH_FILE": str(root / "empty-auth.json"),
        })
        output = _run_cosign(command, environment)

    try:
        results = json.loads(output)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise TrustVerificationError("Cosign returned invalid verification evidence.") from error
    if not isinstance(results, list) or not results:
        raise TrustVerificationError("Cosign returned no verified signatures.")
    for result in results:
        critical = result.get("critical") if isinstance(result, dict) else None
        image = critical.get("image") if isinstance(critical, dict) else None
        if not isinstance(image, dict) or image.get("docker-manifest-digest") != digest:
            raise TrustVerificationError("Cosign evidence does not match the base-image digest.")
        identity = critical.get("identity")
        if (
            not isinstance(identity, dict)
            or identity.get("docker-reference") != repository
            or critical.get("type") != "cosign container image signature"
        ):
            raise TrustVerificationError(
                "Cosign evidence does not bind the required repository."
            )


def verify_base_image(
    policy: Dict[str, str],
    repository: str,
    digest: str,
    manifest: dict,
    manifest_bytes: bytes,
    client,
) -> None:
    """Verify one resolved subject before reading any base config or layer."""
    validate_trust_policy(policy)
    if repository != policy["repository"]:
        raise TrustVerificationError("Base-image repository does not match the required trust policy.")
    try:
        digest_filename(digest)
    except ValueError as error:
        raise TrustVerificationError("Base-image subject digest is invalid.") from error
    if f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}" != digest:
        raise TrustVerificationError("Base-image subject bytes do not match their digest.")
    try:
        if json.loads(manifest_bytes) != manifest:
            raise TrustVerificationError("Base-image subject metadata does not match its bytes.")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise TrustVerificationError("Base-image subject is not valid JSON.") from error
    if policy["provider"] == "cosign":
        try:
            _verify_cosign(policy, repository, digest, manifest, manifest_bytes, client)
        except (OSError, ValueError, BlobIntegrityError, RegistryRequestError) as error:
            raise TrustVerificationError(f"Cosign verification failed: {error}") from error
    else:
        from .trust_notation import NotationTrustError, verify_notation

        try:
            verify_notation(policy, repository, digest, manifest, manifest_bytes, client)
        except NotationTrustError as exc:
            raise TrustVerificationError(str(exc)) from exc
