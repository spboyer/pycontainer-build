"""SLSA provenance statements, completeness policy, and cosign attestations.

A locally written statement is unsigned and therefore unauthenticated. Only an
attestation verified against a trusted key or certificate identity is evidence
of who produced it; neither form asserts a SLSA build level.
"""
import base64
import binascii
import json
import os
import platform as host_platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from .signing import SigningError, _check_version, _find_executable

if TYPE_CHECKING:
    from .config import BuildConfig


STATEMENT_V1 = "https://in-toto.io/Statement/v1"
# cosign attest wraps a predicate in its own v0.1 Statement (checked with v2.6.1 and v3.1.4).
STATEMENT_V01 = "https://in-toto.io/Statement/v0.1"
SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
BUILD_TYPE = (
    "https://github.com/spboyer/pycontainer-build/blob/main/docs/provenance.md#build-type-v1"
)
BUILDER_ID = "https://github.com/spboyer/pycontainer-build/builders/local@v1"
STATEMENT_FILENAME = "provenance.intoto.json"
ATTEST_TIMEOUT_SECONDS = 300
MAX_VERIFY_OUTPUT_BYTES = 16 * 1024 * 1024
_SUBJECT = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")


class ProvenanceError(RuntimeError):
    """Raised when provenance cannot be produced, attached, or verified."""


class IncompleteProvenanceError(ProvenanceError):
    """Raised when the completeness policy rejects missing evidence."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def builder_version(pip_version: Optional[str] = None) -> Dict[str, str]:
    from importlib import metadata

    try:
        version = metadata.version("pycontainer-build")
    except metadata.PackageNotFoundError:
        version = "unknown"
    result = {
        "pycontainer-build": version,
        "python": host_platform.python_version(),
        "host": f"{sys.platform}/{host_platform.machine().lower()}",
    }
    if pip_version:
        result["pip"] = pip_version
    return result


def _hex(digest: str) -> str:
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ProvenanceError(f"Unsupported digest {digest!r}.")
    return value


def descriptor(name: str, digest: str, annotations: Optional[Dict] = None) -> Dict:
    entry = {"name": name, "digest": {"sha256": _hex(digest)}}
    if annotations:
        entry["annotations"] = annotations
    return entry


def build_statement(
    *,
    subject_name: str,
    subject_digest: str,
    external_parameters: Dict,
    resolved_dependencies: List[Dict],
    byproducts: List[Dict],
    incomplete: List[Dict[str, str]],
    started_on: str,
    finished_on: str,
    version: Dict[str, str],
) -> Dict:
    """Return an in-toto Statement/v1 with a SLSA provenance/v1 predicate."""
    return {
        "_type": STATEMENT_V1,
        "subject": [descriptor(subject_name, subject_digest)],
        "predicateType": SLSA_PROVENANCE_V1,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": external_parameters,
                "internalParameters": {
                    "evidence": {"complete": not incomplete, "incomplete": incomplete}
                },
                "resolvedDependencies": resolved_dependencies,
            },
            "runDetails": {
                "builder": {"id": BUILDER_ID, "version": version},
                "metadata": {"startedOn": started_on, "finishedOn": finished_on},
                "byproducts": byproducts,
            },
        },
    }


def require_complete(incomplete: List[Dict[str, str]]) -> None:
    if incomplete:
        reasons = "; ".join(f"{item['material']}: {item['reason']}" for item in incomplete)
        raise IncompleteProvenanceError(
            f"Provenance evidence is incomplete and completeness is required: {reasons}."
        )


def validate_statement(
    statement: object,
    expected_digest: str,
    *,
    statement_types=(STATEMENT_V1,),
    require_complete_evidence: bool = False,
) -> Dict:
    """Check structure and subject only; this does not authenticate anything."""
    if not isinstance(statement, dict):
        raise ProvenanceError("Provenance statement is not a JSON object.")
    if statement.get("_type") not in statement_types:
        raise ProvenanceError("Provenance statement has an unsupported _type.")
    if statement.get("predicateType") != SLSA_PROVENANCE_V1:
        raise ProvenanceError("Provenance statement is not SLSA provenance v1.")
    subjects = statement.get("subject")
    expected = _hex(expected_digest)
    if not isinstance(subjects, list) or not subjects or not all(
        isinstance(item, dict) and isinstance(item.get("digest"), dict) for item in subjects
    ):
        raise ProvenanceError("Provenance statement has no valid subject.")
    if any(item["digest"].get("sha256") != expected for item in subjects):
        raise ProvenanceError("Provenance statement subject does not match the image digest.")
    predicate = statement.get("predicate")
    definition = predicate.get("buildDefinition") if isinstance(predicate, dict) else None
    if not isinstance(definition, dict) or definition.get("buildType") != BUILD_TYPE:
        raise ProvenanceError("Provenance statement has an unrecognized build type.")
    evidence = (definition.get("internalParameters") or {}).get("evidence")
    if (
        not isinstance(evidence, dict)
        or type(evidence.get("complete")) is not bool
        or not isinstance(evidence.get("incomplete"), list)
        or evidence["complete"] != (not evidence["incomplete"])
        or not all(
            isinstance(item, dict)
            and all(isinstance(item.get(key), str) and item[key] for key in ("material", "reason"))
            for item in evidence["incomplete"]
        )
    ):
        raise ProvenanceError("Provenance statement has malformed evidence completeness.")
    if require_complete_evidence and not evidence["complete"]:
        raise IncompleteProvenanceError("Provenance statement records incomplete evidence.")
    return predicate


def parse_verified_attestations(
    output: bytes, expected_digest: str, *, require_complete_evidence: bool = False
) -> List[Dict]:
    """Apply payload policy to DSSE envelopes printed by cosign verify-attestation."""
    statements = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            envelope = json.loads(line)
            if isinstance(envelope, dict) and isinstance(envelope.get("dsseEnvelope"), dict):
                envelope = envelope["dsseEnvelope"]
            if not isinstance(envelope, dict) or envelope.get("payloadType") != DSSE_PAYLOAD_TYPE:
                raise ProvenanceError("Verified attestation is not an in-toto DSSE envelope.")
            payload = base64.b64decode(envelope.get("payload") or "", validate=True)
            statement = json.loads(payload)
        except (ValueError, TypeError, binascii.Error):
            raise ProvenanceError("Verified attestation output is malformed.") from None
        if not isinstance(statement, dict):
            raise ProvenanceError("Verified attestation payload is not a JSON object.")
        predicate = statement.get("predicate")
        build_type = (
            (predicate.get("buildDefinition") or {}).get("buildType")
            if isinstance(predicate, dict) else None
        )
        if build_type != BUILD_TYPE:
            if statement.get("predicateType") != SLSA_PROVENANCE_V1:
                raise ProvenanceError("Verified attestation is not SLSA provenance v1.")
            continue
        validate_statement(
            statement, expected_digest,
            statement_types=(STATEMENT_V1, STATEMENT_V01),
            require_complete_evidence=require_complete_evidence,
        )
        statements.append(statement)
    if not statements:
        raise ProvenanceError(
            "No verified pycontainer SLSA provenance attestation matched the image digest."
        )
    return statements


@dataclass(frozen=True)
class CosignAttestor:
    executable: str
    key: Optional[str]
    verify_key: Optional[str]
    certificate_identity: Optional[str]
    certificate_oidc_issuer: Optional[str]

    def _run(self, args: List[str], stdout) -> int:
        try:
            return subprocess.run(
                args, stdin=subprocess.DEVNULL, stdout=stdout,
                stderr=subprocess.DEVNULL, timeout=ATTEST_TIMEOUT_SECONDS, check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            raise ProvenanceError("cosign timed out; attestation state is unknown.") from None
        except OSError:
            raise ProvenanceError("Unable to run cosign.") from None

    def attest(self, subject: str, predicate: Dict) -> None:
        _require_subject(subject)
        descriptor_fd, name = tempfile.mkstemp(prefix="pycontainer-predicate-", suffix=".json")
        try:
            with os.fdopen(descriptor_fd, "w", encoding="utf-8") as target:
                json.dump(predicate, target, sort_keys=True)
            args = [
                self.executable, "attest", "--yes", "--type=slsaprovenance1",
                f"--predicate={name}",
            ]
            if self.key:
                args.append(f"--key={self.key}")
            returncode = self._run([*args, "--", subject], subprocess.DEVNULL)
        finally:
            Path(name).unlink(missing_ok=True)
        if returncode:
            raise ProvenanceError(
                f"cosign attest exited with status {returncode}; check registry "
                "authentication and signing configuration."
            )

    def verify(self, subject: str, *, require_complete_evidence: bool = False) -> List[Dict]:
        _require_subject(subject)
        args = [self.executable, "verify-attestation", "--type=slsaprovenance1"]
        if self.verify_key:
            args.append(f"--key={self.verify_key}")
        else:
            args.extend([
                f"--certificate-identity={self.certificate_identity}",
                f"--certificate-oidc-issuer={self.certificate_oidc_issuer}",
            ])
        with tempfile.TemporaryFile() as output:
            returncode = self._run([*args, "--", subject], output)
            if returncode:
                raise ProvenanceError(
                    f"cosign verify-attestation exited with status {returncode}."
                )
            size = output.seek(0, os.SEEK_END)
            if size > MAX_VERIFY_OUTPUT_BYTES:
                raise ProvenanceError("cosign verify-attestation output exceeds the size limit.")
            output.seek(0)
            data = output.read()
        return parse_verified_attestations(
            data, subject.rsplit("@", 1)[1],
            require_complete_evidence=require_complete_evidence,
        )

    def attest_and_verify(
        self, subject: str, predicate: Dict, *, require_complete_evidence: bool = False
    ) -> None:
        self.attest(subject, predicate)
        statements = self.verify(subject, require_complete_evidence=require_complete_evidence)
        expected = json.dumps(predicate, sort_keys=True)
        if not any(
            json.dumps(statement.get("predicate"), sort_keys=True) == expected
            for statement in statements
        ):
            raise ProvenanceError(
                "The verified attestations do not contain the predicate that was attested."
            )


def _require_subject(subject: str) -> None:
    if not _SUBJECT.fullmatch(subject):
        raise ProvenanceError("Provenance requires an immutable sha256 image reference.")


def _create_cosign(
    key: Optional[str], verify_key: Optional[str],
    identity: Optional[str], issuer: Optional[str],
) -> CosignAttestor:
    if os.getenv("COSIGN_REPOSITORY"):
        raise ProvenanceError(
            "COSIGN_REPOSITORY redirects attestation storage; unset it for this command."
        )
    executable = _find_executable("cosign")
    if not executable:
        raise ProvenanceError("cosign executable not found on PATH.")
    if os.name == "nt" and os.path.splitext(executable)[1].lower() not in {".exe", ".com"}:
        raise ProvenanceError("cosign must be a native Windows executable.")
    try:
        _check_version(executable, "cosign")
    except SigningError as error:
        raise ProvenanceError(str(error)) from None
    return CosignAttestor(executable, key, verify_key, identity, issuer)


def create_attestor(config: "BuildConfig") -> CosignAttestor:
    config.validate_provenance()
    if not config.attest_provenance:
        raise ValueError("Provenance attestation is not enabled.")
    return _create_cosign(
        config.sign_key, config.provenance_verify_key,
        config.provenance_certificate_identity, config.provenance_certificate_oidc_issuer,
    )


def create_verifier(
    verify_key: Optional[str], identity: Optional[str], issuer: Optional[str]
) -> CosignAttestor:
    if bool(verify_key) == bool(identity and issuer) or bool(identity) != bool(issuer):
        raise ValueError(
            "Provenance verification requires either --key or both "
            "--certificate-identity and --certificate-oidc-issuer."
        )
    return _create_cosign(None, verify_key, identity, issuer)
