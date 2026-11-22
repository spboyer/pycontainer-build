"""SLSA provenance statements, completeness policy, and cosign attestations.

A locally written statement is unsigned and therefore unauthenticated. Only an
attestation verified against a trusted key or certificate identity is evidence
of who produced it; neither form asserts a SLSA build level.
"""
import base64
import binascii
import hashlib
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
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from .signing import SigningError, _check_version, _find_executable

if TYPE_CHECKING:
    from .config import BuildConfig


STATEMENT_V1 = "https://in-toto.io/Statement/v1"
SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
# `cosign attest` wraps a predicate in a Statement/v0.1 and ignores --statement for images
# (v2.6.1, v3.1.4), so the exact v1 statement is signed with attest-blob and published as
# an OCI 1.1 referrer in the layout cosign verify-attestation --new-bundle-format discovers.
SIGSTORE_BUNDLE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
OCI_EMPTY_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
OCI_EMPTY_DATA = b"{}"
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
    require_complete_evidence: bool = False,
) -> Dict:
    """Check structure and subject only; this does not authenticate anything."""
    if not isinstance(statement, dict):
        raise ProvenanceError("Provenance statement is not a JSON object.")
    if statement.get("_type") != STATEMENT_V1:
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
    internal = definition.get("internalParameters")
    evidence = internal.get("evidence") if isinstance(internal, dict) else None
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
        definition = predicate.get("buildDefinition") if isinstance(predicate, dict) else None
        build_type = definition.get("buildType") if isinstance(definition, dict) else None
        if build_type != BUILD_TYPE:
            if statement.get("predicateType") != SLSA_PROVENANCE_V1:
                raise ProvenanceError("Verified attestation is not SLSA provenance v1.")
            continue
        validate_statement(
            statement, expected_digest, require_complete_evidence=require_complete_evidence,
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

    def sign_statement(self, statement: Dict) -> bytes:
        """Sign the exact statement into a Sigstore bundle with cosign attest-blob."""
        payload = statement_bytes(statement)
        with tempfile.TemporaryDirectory(prefix="pycontainer-attest-") as workdir:
            statement_path = Path(workdir) / "statement.json"
            bundle_path = Path(workdir) / "bundle.sigstore.json"
            statement_path.write_bytes(payload)
            args = [
                self.executable, "attest-blob", "--yes", "--new-bundle-format",
                f"--statement={statement_path}", f"--bundle={bundle_path}",
            ]
            if self.key:
                args.append(f"--key={self.key}")
            returncode = self._run(args, subprocess.DEVNULL)
            if returncode:
                raise ProvenanceError(
                    f"cosign attest-blob exited with status {returncode}; check "
                    "signing configuration."
                )
            try:
                if bundle_path.stat().st_size > MAX_VERIFY_OUTPUT_BYTES:
                    raise ProvenanceError("cosign attestation bundle exceeds the size limit.")
                bundle = bundle_path.read_bytes()
            except OSError:
                raise ProvenanceError("cosign did not write an attestation bundle.") from None
        check_bundle(bundle, payload)
        return bundle

    def verify(self, subject: str, *, require_complete_evidence: bool = False) -> List[Dict]:
        _require_subject(subject)
        args = [
            self.executable, "verify-attestation", "--new-bundle-format",
            "--type=slsaprovenance1",
        ]
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
        self, subject: str, statement: Dict, publish: Callable[[bytes], None], *,
        require_complete_evidence: bool = False,
    ) -> None:
        """Sign the statement, publish the bundle with ``publish``, then verify it independently."""
        _require_subject(subject)
        validate_statement(
            statement, subject.rsplit("@", 1)[1],
            require_complete_evidence=require_complete_evidence,
        )
        publish(self.sign_statement(statement))
        statements = self.verify(subject, require_complete_evidence=require_complete_evidence)
        if statement not in statements:
            raise ProvenanceError(
                "The verified attestations do not contain the statement that was attested."
            )


def statement_bytes(statement: Dict) -> bytes:
    return (json.dumps(statement, indent=2, sort_keys=True) + "\n").encode("utf-8")


def check_bundle(bundle: bytes, payload: bytes) -> None:
    """Require a Sigstore bundle whose DSSE payload is exactly ``payload``."""
    try:
        document = json.loads(bundle)
        envelope = document["dsseEnvelope"]
        signed = base64.b64decode(envelope["payload"], validate=True)
    except (ValueError, TypeError, KeyError, binascii.Error):
        raise ProvenanceError("cosign wrote a malformed attestation bundle.") from None
    if (
        document.get("mediaType") != SIGSTORE_BUNDLE_MEDIA_TYPE
        or envelope.get("payloadType") != DSSE_PAYLOAD_TYPE
        or signed != payload
    ):
        raise ProvenanceError(
            "cosign attestation bundle does not sign the exact provenance statement."
        )


def _blob_descriptor(media_type: str, data: bytes) -> Dict:
    return {
        "mediaType": media_type,
        "size": len(data),
        "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
    }


def referrer_manifest(bundle: bytes, subject: Dict) -> Tuple[bytes, Dict[str, bytes]]:
    """Return an OCI 1.1 referrer manifest for a Sigstore bundle and the blobs it needs."""
    _hex(subject["digest"])
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": SIGSTORE_BUNDLE_MEDIA_TYPE,
        "config": _blob_descriptor(OCI_EMPTY_MEDIA_TYPE, OCI_EMPTY_DATA),
        "layers": [_blob_descriptor(SIGSTORE_BUNDLE_MEDIA_TYPE, bundle)],
        "annotations": {
            "dev.sigstore.bundle.content": "dsse-envelope",
            "dev.sigstore.bundle.predicateType": SLSA_PROVENANCE_V1,
        },
        "subject": {
            "mediaType": subject["mediaType"], "size": subject["size"],
            "digest": subject["digest"],
        },
    }
    blobs = {
        manifest["config"]["digest"]: OCI_EMPTY_DATA,
        manifest["layers"][0]["digest"]: bundle,
    }
    return json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode(), blobs


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
