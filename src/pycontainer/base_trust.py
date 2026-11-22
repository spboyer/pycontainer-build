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

from .blob_store import digest_filename
from .registry_client import parse_image_reference


VERIFIER_TIMEOUT_SECONDS = 180
MAX_VERIFIER_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_TRUST_MATERIAL_BYTES = 4 * 1024 * 1024


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
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        raise TrustVerificationError(f"{label} verification timed out.") from error
    finally:
        for thread in threads:
            thread.join(timeout=2)
    if any(thread.is_alive() for thread in threads):
        raise TrustVerificationError(f"{label} output streams did not close.")
    if exceeded.is_set():
        raise TrustVerificationError(f"{label} output exceeded its size limit.")
    if stream_failed.is_set():
        raise TrustVerificationError(f"Cannot read {label} verification output.")
    if process.returncode:
        raise TrustVerificationError(
            f"{label} verification failed with exit code {process.returncode}."
        )
    return bytes(output)


def _run_cosign(command, environment) -> bytes:
    executable = resolve_verifier_executable(command[0])
    if executable is None:
        raise TrustVerificationError("Cosign executable was not found.")
    return run_bounded_verifier(
        [executable, *command[1:]], environment, VERIFIER_TIMEOUT_SECONDS,
        MAX_VERIFIER_OUTPUT_BYTES, "Cosign",
    )


def _verify_cosign(policy: Dict[str, str], repository: str, digest: str) -> None:
    with tempfile.TemporaryDirectory(prefix="pycontainer-trust-") as directory:
        root = Path(directory)
        command = [
            policy.get("binary", "cosign"),
            "verify",
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
        command.append(f"{repository}@{digest}")
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("COSIGN_", "SIGSTORE_", "AWS_", "AZURE_", "GOOGLE_"))
            and key.upper() not in {
                "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                "XDG_DATA_HOME", "XDG_CACHE_HOME", "DOCKER_CONFIG", "DOCKER_AUTH_CONFIG",
                "REGISTRY_AUTH_FILE", "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN",
                "REGISTRY_USERNAME", "REGISTRY_PASSWORD", "DOCKER_USERNAME", "DOCKER_PASSWORD",
            }
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
        _verify_cosign(policy, repository, digest)
    else:
        from .trust_notation import NotationTrustError, verify_notation

        try:
            verify_notation(policy, repository, digest, manifest, manifest_bytes, client)
        except NotationTrustError as exc:
            raise TrustVerificationError(str(exc)) from exc
