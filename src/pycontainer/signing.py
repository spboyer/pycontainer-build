"""Optional, non-interactive registry image signing through provider CLIs."""
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import BuildConfig


VERSION_TIMEOUT_SECONDS = 10
SIGN_TIMEOUT_SECONDS = 300
VERIFY_TIMEOUT_SECONDS = 300
COSIGN_MIN_VERSION = (2, 6, 1)
COSIGN_MAX_VERSION = (4, 0, 0)
NOTATION_MIN_VERSION = (1, 3, 2)
NOTATION_MAX_VERSION = (2, 0, 0)


class SigningError(RuntimeError):
    """Raised when an optional signing provider cannot complete signing."""


def _check_version(executable: str, provider: str) -> None:
    args = [executable, "version"]
    if provider == "cosign":
        args.append("--json")
    try:
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, errors="replace", timeout=VERSION_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SigningError(f"Unable to check the {provider} version.") from None
    if result.returncode:
        raise SigningError(f"Unable to check the {provider} version.")
    try:
        if provider == "cosign":
            version = json.loads(result.stdout)["gitVersion"]
        else:
            match = re.search(r"(?m)^\s*Version:\s*(\S+)\s*$", result.stdout)
            version = match.group(1) if match else None
        match = re.fullmatch(
            r"v?(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
            version or "",
        )
        parsed = tuple(int(part) for part in match.groups()) if match else None
    except (ValueError, KeyError, TypeError):
        parsed = None
    minimum, maximum = (
        (COSIGN_MIN_VERSION, COSIGN_MAX_VERSION)
        if provider == "cosign"
        else (NOTATION_MIN_VERSION, NOTATION_MAX_VERSION)
    )
    if parsed is None or not minimum <= parsed < maximum:
        raise SigningError(
            f"Unsupported {provider} version; expected "
            f">={'.'.join(map(str, minimum))} and <{'.'.join(map(str, maximum))}."
        )


@dataclass(frozen=True)
class Signer:
    provider: str
    executable: str
    key: Optional[str]
    plugin: Optional[str]
    key_id: Optional[str]
    timestamp_url: Optional[str]
    timestamp_root_cert: Optional[str]
    signature_format: Optional[str]
    verify_key: Optional[str] = None
    certificate_identity: Optional[str] = None
    certificate_oidc_issuer: Optional[str] = None

    def sign(self, subject: str) -> None:
        _require_immutable_subject(subject, "Signing")
        args = [self.executable, "sign"]
        if self.provider == "cosign":
            args.extend(["--yes", "--upload=true"])
            if self.key:
                args.append(f"--key={self.key}")
        else:
            args.append("--force-referrers-tag=false")
            if self.key:
                args.append(f"--key={self.key}")
            else:
                args.extend([f"--plugin={self.plugin}", f"--id={self.key_id}"])
            if self.timestamp_url:
                args.append(f"--timestamp-url={self.timestamp_url}")
            if self.timestamp_root_cert:
                args.append(f"--timestamp-root-cert={self.timestamp_root_cert}")
            if self.signature_format:
                args.append(f"--signature-format={self.signature_format}")
        args.extend(["--", subject])
        try:
            result = subprocess.run(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=SIGN_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            raise SigningError(
                f"{self.provider} timed out; signature publication is unknown."
            ) from None
        except OSError:
            raise SigningError(f"Unable to run {self.provider}.") from None
        if result.returncode:
            raise SigningError(
                f"{self.provider} exited with status {result.returncode}; "
                "check its registry authentication and signing configuration."
            )

    def verify(self, subject: str) -> None:
        """Verify the signature on the exact digest with the configured trust inputs."""
        _require_immutable_subject(subject, "Verification")
        if self.provider == "cosign":
            args = [self.executable, "verify"]
            if self.key:
                args.append(f"--key={self.verify_key}")
            else:
                args.extend([
                    f"--certificate-identity={self.certificate_identity}",
                    f"--certificate-oidc-issuer={self.certificate_oidc_issuer}",
                ])
            self._run_verifier([*args, "--", subject])
            return
        repository = subject.rsplit("@", 1)[0]
        policy = self._strict_notation_policy(repository)
        output = self._run_verifier([self.executable, "verify", "--", subject], capture=True)
        expected = f"Successfully verified signature for {subject}"
        if expected not in (line.strip() for line in output.splitlines()):
            raise SigningError(f"notation did not report successful verification of {subject}.")
        if self._strict_notation_policy(repository) != policy:
            raise SigningError("The Notation trust policy changed during verification.")

    def _run_verifier(self, args, capture: bool = False) -> str:
        try:
            result = subprocess.run(
                args, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, text=capture, errors="replace" if capture else None,
                timeout=VERIFY_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            raise SigningError(f"{self.provider} signature verification timed out.") from None
        except OSError:
            raise SigningError(f"Unable to run {self.provider} signature verification.") from None
        if result.returncode:
            raise SigningError(
                f"{self.provider} signature verification exited with status "
                f"{result.returncode}; check its trust configuration and registry access."
            )
        return result.stdout if capture else ""

    def _strict_notation_policy(self, repository: str) -> dict:
        """Return the trust policy after requiring a strict statement for the repository."""
        try:
            result = subprocess.run(
                [self.executable, "policy", "show"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                errors="replace", timeout=VERSION_TIMEOUT_SECONDS, check=False,
            )
            policy = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            policy = None
        statements = policy.get("trustPolicies") if isinstance(policy, dict) else None
        if not isinstance(statements, list):
            statements = []
        statements = [item for item in statements if isinstance(item, dict)]
        scopes_valid = all(
            isinstance(item.get("registryScopes"), list)
            and all(isinstance(scope, str) for scope in item["registryScopes"])
            for item in statements
        )
        applicable = (
            [item for item in statements if repository in item["registryScopes"]]
            or [item for item in statements if item["registryScopes"] == ["*"]]
        ) if scopes_valid else []
        verification = applicable[0].get("signatureVerification") if len(applicable) == 1 else None
        if (
            not isinstance(verification, dict)
            or verification.get("level") != "strict"
            or "override" in verification
        ):
            raise SigningError(
                f"Notation verification requires one strict trust policy statement "
                f"without overrides for {repository}."
            )
        return policy


def _require_immutable_subject(subject: str, action: str) -> None:
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", subject):
        raise SigningError(f"{action} requires an immutable sha256 image reference.")


def _find_executable(provider: str) -> Optional[str]:
    if os.name != "nt":
        return shutil.which(provider)
    current = os.path.normcase(os.path.realpath(os.getcwd()))
    extensions = (os.getenv("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
    for directory in os.get_exec_path():
        if not directory or not os.path.isabs(directory):
            continue
        if os.path.normcase(os.path.realpath(directory)) == current:
            continue
        for extension in extensions:
            if not re.fullmatch(r"\.[A-Za-z0-9]+", extension):
                continue
            executable = shutil.which(os.path.join(directory, provider + extension))
            if executable:
                return executable
    return None


def create_signer(config: "BuildConfig") -> Signer:
    config.validate_signing()
    if not config.sign:
        raise ValueError("Signing is not enabled.")
    provider = config.sign_provider
    if provider is None:
        raise ValueError("Signing requires an explicit sign_provider.")
    if provider == "cosign" and os.getenv("COSIGN_REPOSITORY"):
        raise SigningError(
            "COSIGN_REPOSITORY redirects signature storage; unset it for this build."
        )
    executable = _find_executable(provider)
    if not executable:
        raise SigningError(f"{provider} executable not found on PATH.")
    if os.name == "nt" and os.path.splitext(executable)[1].lower() not in {".exe", ".com"}:
        raise SigningError(f"{provider} must be a native Windows executable.")
    _check_version(executable, provider)
    return Signer(
        provider, executable, config.sign_key, config.sign_plugin, config.sign_key_id,
        config.sign_timestamp_url, config.sign_timestamp_root_cert, config.sign_signature_format,
        config.sign_verify_key, config.sign_certificate_identity,
        config.sign_certificate_oidc_issuer,
    )
