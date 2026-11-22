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

    def sign(self, subject: str) -> None:
        if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", subject):
            raise SigningError("Signing requires an immutable sha256 image reference.")
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
    )
