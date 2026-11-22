"""Generic, non-runnable OCI 1.1 artifacts in local layouts and registries."""
import base64
import binascii
import hashlib
import json
import os
import re
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

from .blob_store import (
    HASH_CHUNK_SIZE,
    BlobIntegrityError,
    blob_matches,
    create_temporary_file,
    digest_filename,
    publish_content_addressed,
)
from .oci import OCI_INDEX_MEDIA_TYPE, OCI_MANIFEST_MEDIA_TYPE
from .registry_client import RegistryClient, RegistryRequestError


EMPTY_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
EMPTY_CONFIG = b"{}"
EMPTY_DIGEST = "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_PAYLOAD_BYTES = 1024 * 1024 * 1024
MAX_REFERRERS_PAGES = 16
REF_NAME_ANNOTATION = "org.opencontainers.image.ref.name"
LAYOUT_VERSION = "1.0.0"

DOCKER_MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_LIST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.list.v2+json"
SUBJECT_MEDIA_TYPES = {
    OCI_MANIFEST_MEDIA_TYPE,
    OCI_INDEX_MEDIA_TYPE,
    DOCKER_MANIFEST_MEDIA_TYPE,
    DOCKER_LIST_MEDIA_TYPE,
}
# Types that would make an artifact look like (part of) a runnable image.
RESERVED_MEDIA_TYPES = SUBJECT_MEDIA_TYPES | {
    EMPTY_MEDIA_TYPE,
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
    "application/vnd.oci.image.layout.header.v1+json",
}
RESERVED_MEDIA_TYPE_PREFIXES = (
    "application/vnd.oci.image.layer.",
    "application/vnd.docker.image.rootfs.",
)
_MEDIA_TYPE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"
)
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_LINK_NEXT = re.compile(r'<([^>]*)>\s*;[^,]*\brel="?next"?', re.IGNORECASE)


class InvalidArtifactError(ValueError):
    """Raised when artifact metadata is malformed."""


class UnsupportedArtifactError(InvalidArtifactError):
    """Raised for valid OCI content outside the generic-artifact contract."""


STAGE_SUBJECT = "subject"
STAGE_BLOB = "blob"
STAGE_MANIFEST = "manifest"
STAGE_TAG = "tag"
STAGE_REFERRERS = "referrers"

REFERRER_DISCOVERY_ATTEMPTS = 3
REFERRER_DISCOVERY_DELAYS = (1.0, 2.0)


class ArtifactPublicationError(RuntimeError):
    """Raised when registry publication cannot be confirmed.

    ``stage`` names the step that failed: subject, blob, manifest, tag or referrers.
    """

    def __init__(
        self, message: str, published_tags: Tuple[str, ...] = (), stage: Optional[str] = None
    ):
        super().__init__(message)
        self.published_tags = tuple(published_tags)
        self.stage = stage


@dataclass(frozen=True)
class Descriptor:
    media_type: str
    digest: str
    size: int
    artifact_type: Optional[str] = None
    annotations: Optional[Dict[str, str]] = None

    def to_dict(self) -> Dict:
        value = {"mediaType": self.media_type, "digest": self.digest, "size": self.size}
        if self.artifact_type:
            value["artifactType"] = self.artifact_type
        if self.annotations:
            value["annotations"] = dict(self.annotations)
        return value


@dataclass(frozen=True)
class ArtifactManifest:
    artifact_type: str
    payload: Descriptor
    subject: Optional[Descriptor] = None
    annotations: Optional[Dict[str, str]] = None


@dataclass(frozen=True)
class Artifact:
    """A verified artifact in a local OCI layout."""

    descriptor: Descriptor
    manifest: ArtifactManifest
    payload_path: Path

    @property
    def artifact_type(self) -> str:
        return self.manifest.artifact_type

    @property
    def payload(self) -> Descriptor:
        return self.manifest.payload

    @property
    def subject(self) -> Optional[Descriptor]:
        return self.manifest.subject


SUBJECT_CONFIRMED = "confirmed"
SUBJECT_UNCONFIRMED = "unconfirmed"
REFERRERS_SUPPORTED = "supported"
REFERRERS_NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ArtifactPushResult:
    reference: str
    descriptor: Descriptor
    tags: Tuple[str, ...]
    # None without a subject. "confirmed" means the registry returned a matching
    # OCI-Subject header; "unconfirmed" means it did not acknowledge the subject.
    # Neither guarantees later referrers discoverability.
    subject_status: Optional[str]


@dataclass(frozen=True)
class ReferrersResult:
    # "supported" (HTTP 200 OCI index) or "not_found" (HTTP 404: the registry
    # lacks the referrers API, or the repository/subject is unknown).
    status: str
    manifests: Tuple[Descriptor, ...]
    registry_filter_applied: bool
    pages: int

    @property
    def supported(self) -> bool:
        return self.status == REFERRERS_SUPPORTED


def _check_media_type(value, role: str, *, generic: bool = False) -> str:
    if not isinstance(value, str) or not _MEDIA_TYPE.fullmatch(value):
        raise InvalidArtifactError(f"Artifact {role} must be a valid media type.")
    if generic and (
        value.lower() in RESERVED_MEDIA_TYPES
        or value.lower().startswith(RESERVED_MEDIA_TYPE_PREFIXES)
    ):
        raise UnsupportedArtifactError(
            f"Artifact {role} {value!r} is reserved for runnable images."
        )
    return value


def _check_annotations(value, role: str) -> Optional[Dict[str, str]]:
    if value is None:
        return None
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and key and isinstance(item, str)
        for key, item in value.items()
    ):
        raise InvalidArtifactError(f"Artifact {role} annotations must map strings to strings.")
    return dict(value) or None


def _check_tag(tag) -> str:
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise InvalidArtifactError(f"Invalid artifact tag: {tag!r}.")
    return tag


def _check_digest(value, role: str) -> str:
    try:
        digest_filename(value)
    except ValueError:
        raise InvalidArtifactError(
            f"Artifact {role} digest must be a lowercase sha256 digest."
        ) from None
    return value


def _parse_descriptor(value, role: str, *, generic: bool = False) -> Descriptor:
    if not isinstance(value, dict):
        raise InvalidArtifactError(f"Artifact {role} descriptor must be an object.")
    if "urls" in value:
        raise UnsupportedArtifactError(f"Artifact {role} descriptor must not use external URLs.")
    media_type = _check_media_type(value.get("mediaType"), f"{role} mediaType", generic=generic)
    digest = _check_digest(value.get("digest"), role)
    size = value.get("size")
    if type(size) is not int or size < 0:
        raise InvalidArtifactError(f"Artifact {role} size must be a non-negative integer.")
    artifact_type = value.get("artifactType")
    if artifact_type is not None:
        _check_media_type(artifact_type, f"{role} artifactType")
    if "data" in value:
        data = value["data"]
        try:
            decoded = base64.b64decode(data, validate=True) if isinstance(data, str) else None
        except (binascii.Error, ValueError):
            decoded = None
        if decoded is None or len(decoded) != size or (
            "sha256:" + hashlib.sha256(decoded).hexdigest() != digest
        ):
            raise BlobIntegrityError(f"Artifact {role} embedded data does not match its descriptor.")
    return Descriptor(
        media_type, digest, size, artifact_type,
        _check_annotations(value.get("annotations"), role),
    )


def _coerce_subject(subject) -> Optional[Descriptor]:
    if subject is None:
        return None
    if isinstance(subject, Descriptor):
        subject = subject.to_dict()
    descriptor = _parse_descriptor(subject, "subject")
    if descriptor.media_type not in SUBJECT_MEDIA_TYPES:
        raise InvalidArtifactError(
            "Artifact subject must describe an OCI or Docker image manifest or index."
        )
    return descriptor


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidArtifactError(f"Duplicate JSON key {key!r} in OCI metadata.")
        result[key] = value
    return result


def _load_json(data: bytes, role: str):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidArtifactError(f"{role} is not valid UTF-8 JSON.") from None


def build_artifact_manifest(
    artifact_type: str,
    payload: Descriptor,
    subject=None,
    annotations: Optional[Dict[str, str]] = None,
) -> bytes:
    """Serialize an OCI 1.1 artifact manifest with the empty config descriptor."""
    _check_media_type(artifact_type, "artifactType", generic=True)
    payload = _parse_descriptor(payload.to_dict(), "payload", generic=True)
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "artifactType": artifact_type,
        "config": {"mediaType": EMPTY_MEDIA_TYPE, "digest": EMPTY_DIGEST, "size": len(EMPTY_CONFIG)},
        "layers": [payload.to_dict()],
    }
    subject = _coerce_subject(subject)
    if subject:
        manifest["subject"] = subject.to_dict()
    annotations = _check_annotations(annotations, "manifest")
    if annotations:
        manifest["annotations"] = annotations
    data = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_MANIFEST_BYTES:
        raise InvalidArtifactError("Artifact manifest exceeds the 4 MiB limit.")
    return data


def parse_artifact_manifest(data: bytes) -> ArtifactManifest:
    """Strictly parse a generic artifact manifest; image manifests are rejected."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise InvalidArtifactError("Artifact manifest exceeds the 4 MiB limit.")
    manifest = _load_json(data, "Artifact manifest")
    if not isinstance(manifest, dict):
        raise InvalidArtifactError("Artifact manifest must be a JSON object.")
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 2:
        raise InvalidArtifactError("Artifact manifest schemaVersion must be 2.")
    if manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE:
        raise UnsupportedArtifactError("Artifact manifest must be an OCI image manifest.")
    if "artifactType" not in manifest:
        raise UnsupportedArtifactError(
            "Manifest has no artifactType; it is not a generic OCI artifact."
        )
    artifact_type = _check_media_type(manifest["artifactType"], "artifactType", generic=True)
    config = _parse_descriptor(manifest.get("config"), "config")
    if (config.media_type, config.digest, config.size) != (
        EMPTY_MEDIA_TYPE, EMPTY_DIGEST, len(EMPTY_CONFIG)
    ):
        raise UnsupportedArtifactError(
            "Artifact config must be the exact empty descriptor for '{}'."
        )
    layers = manifest.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        raise UnsupportedArtifactError("Artifact manifest must contain exactly one payload layer.")
    return ArtifactManifest(
        artifact_type,
        _parse_descriptor(layers[0], "payload", generic=True),
        _coerce_subject(manifest.get("subject")),
        _check_annotations(manifest.get("annotations"), "manifest"),
    )


def _blob_path(layout: Path, digest: str) -> Path:
    return layout / "blobs" / "sha256" / digest_filename(digest)


def _read_bounded(path: Path, limit: int, role: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"{role} is not a regular file: {path}")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise InvalidArtifactError(f"{role} exceeds {limit} bytes.")
    return data


def _publish_bytes(layout: Path, data: bytes, digest: str) -> None:
    destination = _blob_path(layout, digest)
    staged = create_temporary_file(destination.parent, destination.name)
    staged.write_bytes(data)
    publish_content_addressed(staged, destination, digest, len(data))


def _ensure_layout(layout: Path) -> None:
    marker = layout / "oci-layout"
    if layout.exists() and any(layout.iterdir()):
        data = _load_json(_read_bounded(marker, 4096, "OCI layout marker"), "OCI layout marker")
        if not isinstance(data, dict) or data.get("imageLayoutVersion") != LAYOUT_VERSION:
            raise UnsupportedArtifactError(f"{layout} is not an OCI {LAYOUT_VERSION} image layout.")
    else:
        (layout / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"imageLayoutVersion": LAYOUT_VERSION}))
    (layout / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)


def _read_index(layout: Path) -> Dict:
    path = layout / "index.json"
    if not path.exists() and not path.is_symlink():
        return {"schemaVersion": 2, "mediaType": OCI_INDEX_MEDIA_TYPE, "manifests": []}
    index = _load_json(_read_bounded(path, MAX_MANIFEST_BYTES, "OCI layout index"), "OCI layout index")
    if (
        not isinstance(index, dict)
        or index.get("schemaVersion") != 2
        or not isinstance(index.get("manifests"), list)
        or not all(isinstance(entry, dict) for entry in index["manifests"])
    ):
        raise InvalidArtifactError(f"{path} is not a valid OCI image index.")
    return index


def _record_in_index(layout: Path, descriptor: Descriptor, tag: Optional[str]) -> None:
    index = _read_index(layout)
    entry = descriptor.to_dict()
    if tag:
        entry["annotations"] = {REF_NAME_ANNOTATION: tag}
    manifests = [
        item for item in index["manifests"]
        if not (
            (tag and (item.get("annotations") or {}).get(REF_NAME_ANNOTATION) == tag)
            or (not tag and item.get("digest") == descriptor.digest
                and not (item.get("annotations") or {}).get(REF_NAME_ANNOTATION))
        )
    ]
    index["manifests"] = [*manifests, entry]
    data = json.dumps(index, separators=(",", ":")).encode("utf-8")
    staged = create_temporary_file(layout, "index.json")
    try:
        with staged.open("wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(staged, layout / "index.json")
    finally:
        staged.unlink(missing_ok=True)


def _stage_payload(layout: Path, payload_path: Path, max_payload_bytes: int) -> Tuple[str, int]:
    payload_path = Path(payload_path)
    if payload_path.is_symlink() or not payload_path.is_file():
        raise FileNotFoundError(f"Artifact payload is not a regular file: {payload_path}")
    blobs = layout / "blobs" / "sha256"
    staged = create_temporary_file(blobs, "payload")
    try:
        hasher = hashlib.sha256()
        size = 0
        with payload_path.open("rb") as source, staged.open("wb") as target:
            while True:
                chunk = source.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_payload_bytes:
                    raise InvalidArtifactError(
                        f"Artifact payload exceeds {max_payload_bytes} bytes."
                    )
                hasher.update(chunk)
                target.write(chunk)
        digest = f"sha256:{hasher.hexdigest()}"
        publish_content_addressed(staged, blobs / digest_filename(digest), digest, size)
        return digest, size
    finally:
        staged.unlink(missing_ok=True)


def _check_max_payload(max_payload_bytes) -> int:
    if type(max_payload_bytes) is not int or max_payload_bytes < 0:
        raise ValueError("max_payload_bytes must be a non-negative integer.")
    return max_payload_bytes


def write_artifact_layout(
    layout_dir,
    payload_path,
    artifact_type: str,
    payload_media_type: str,
    *,
    subject=None,
    annotations: Optional[Dict[str, str]] = None,
    payload_annotations: Optional[Dict[str, str]] = None,
    tag: Optional[str] = None,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> Artifact:
    """Write one typed payload as an OCI 1.1 artifact into a local OCI layout."""
    _check_max_payload(max_payload_bytes)
    _check_media_type(artifact_type, "artifactType", generic=True)
    _check_media_type(payload_media_type, "payload mediaType", generic=True)
    payload_annotations = _check_annotations(payload_annotations, "payload")
    subject = _coerce_subject(subject)
    if tag is not None:
        _check_tag(tag)
    layout = Path(layout_dir)
    _ensure_layout(layout)
    digest, size = _stage_payload(layout, payload_path, max_payload_bytes)
    payload = Descriptor(payload_media_type, digest, size, annotations=payload_annotations)
    manifest_data = build_artifact_manifest(artifact_type, payload, subject, annotations)
    manifest_digest = "sha256:" + hashlib.sha256(manifest_data).hexdigest()
    _publish_bytes(layout, EMPTY_CONFIG, EMPTY_DIGEST)
    _publish_bytes(layout, manifest_data, manifest_digest)
    descriptor = Descriptor(OCI_MANIFEST_MEDIA_TYPE, manifest_digest, len(manifest_data), artifact_type)
    _record_in_index(layout, descriptor, tag)
    return read_artifact_layout(layout, manifest_digest, max_payload_bytes=max_payload_bytes)


def _select_entry(index: Dict, reference: Optional[str]) -> Dict:
    entries = index["manifests"]
    if reference is None:
        matches = [entry for entry in entries if "artifactType" in entry]
    elif reference.startswith("sha256:"):
        _check_digest(reference, "reference")
        matches = [entry for entry in entries if entry.get("digest") == reference]
    else:
        _check_tag(reference)
        matches = [
            entry for entry in entries
            if (entry.get("annotations") or {}).get(REF_NAME_ANNOTATION) == reference
        ]
    if reference is not None and reference.startswith("sha256:") and matches:
        matches = matches[:1]
    if len(matches) != 1:
        raise InvalidArtifactError(
            "OCI layout reference "
            f"{reference or '(default)'} matched {len(matches)} manifests; expected one."
        )
    return matches[0]


def read_artifact_layout(
    layout_dir, reference: Optional[str] = None, *,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> Artifact:
    """Resolve and fully verify an artifact in a local OCI layout."""
    _check_max_payload(max_payload_bytes)
    layout = Path(layout_dir)
    marker = _load_json(_read_bounded(layout / "oci-layout", 4096, "OCI layout marker"), "OCI layout marker")
    if not isinstance(marker, dict) or marker.get("imageLayoutVersion") != LAYOUT_VERSION:
        raise UnsupportedArtifactError(f"{layout} is not an OCI {LAYOUT_VERSION} image layout.")
    entry = _parse_descriptor(_select_entry(_read_index(layout), reference), "layout entry")
    if entry.media_type != OCI_MANIFEST_MEDIA_TYPE:
        raise UnsupportedArtifactError("OCI layout entry is not an OCI image manifest.")
    if entry.size > MAX_MANIFEST_BYTES:
        raise InvalidArtifactError("Artifact manifest exceeds the 4 MiB limit.")
    manifest_path = _blob_path(layout, entry.digest)
    if not blob_matches(manifest_path, entry.digest, entry.size):
        raise BlobIntegrityError(f"Artifact manifest blob {entry.digest} is missing or corrupt.")
    manifest = parse_artifact_manifest(_read_bounded(manifest_path, entry.size, "Artifact manifest"))
    if entry.artifact_type is not None and entry.artifact_type != manifest.artifact_type:
        raise InvalidArtifactError("OCI layout entry artifactType does not match its manifest.")
    if not blob_matches(_blob_path(layout, EMPTY_DIGEST), EMPTY_DIGEST, len(EMPTY_CONFIG)):
        raise BlobIntegrityError("Artifact empty config blob is missing or corrupt.")
    if manifest.payload.size > max_payload_bytes:
        raise InvalidArtifactError(f"Artifact payload exceeds {max_payload_bytes} bytes.")
    payload_path = _blob_path(layout, manifest.payload.digest)
    if not blob_matches(payload_path, manifest.payload.digest, manifest.payload.size):
        raise BlobIntegrityError(f"Artifact payload blob {manifest.payload.digest} is missing or corrupt.")
    descriptor = Descriptor(OCI_MANIFEST_MEDIA_TYPE, entry.digest, entry.size, manifest.artifact_type)
    return Artifact(descriptor, manifest, payload_path)


def _header(headers: Dict[str, str], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _reference(client: RegistryClient, reference: str) -> str:
    separator = "@" if reference.startswith("sha256:") else ":"
    return f"{client.registry}/{client.repository}{separator}{reference}"


def push_artifact(
    client: RegistryClient,
    layout_dir,
    reference: Optional[str] = None,
    tags: Iterable[str] = (),
    *,
    verify_subject: bool = True,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> ArtifactPushResult:
    """Publish a verified layout artifact by digest, then move any tags."""
    tags = tuple(dict.fromkeys(_check_tag(tag) for tag in tags))
    artifact = read_artifact_layout(layout_dir, reference, max_payload_bytes=max_payload_bytes)
    layout = Path(layout_dir)
    manifest_data = _read_bounded(
        _blob_path(layout, artifact.descriptor.digest), artifact.descriptor.size, "Artifact manifest"
    )
    published = _publish_by_digest(
        client,
        manifest_data,
        [
            (EMPTY_DIGEST, len(EMPTY_CONFIG), _blob_path(layout, EMPTY_DIGEST)),
            (artifact.payload.digest, artifact.payload.size, artifact.payload_path),
        ],
        verify_subject,
        wrap_errors=False,
    )
    digest, size = published.descriptor.digest, published.descriptor.size
    moved = []
    for tag in tags:
        try:
            client.put_manifest(tag, manifest_data, OCI_MANIFEST_MEDIA_TYPE)
        except (RegistryRequestError, BlobIntegrityError) as error:
            raise ArtifactPublicationError(
                f"Artifact {digest} was published, but tag {tag!r} failed; "
                f"tags already moved: {', '.join(moved) or 'none'}.",
                tuple(moved), STAGE_TAG,
            ) from error
        moved.append(tag)
        try:
            client.verify_manifest_descriptor(tag, digest, size, OCI_MANIFEST_MEDIA_TYPE)
        except (RegistryRequestError, BlobIntegrityError) as error:
            raise ArtifactPublicationError(
                f"Artifact {digest} was published, but tag {tag!r} could not be verified; "
                f"tags possibly moved: {', '.join(moved)}.",
                tuple(moved), STAGE_TAG,
            ) from error
    return ArtifactPushResult(published.reference, published.descriptor, tags, published.subject_status)


def publish_artifact_manifest(
    client: RegistryClient,
    manifest_data: bytes,
    blobs: Iterable[Tuple[str, int, Path]],
    *,
    verify_subject: bool = True,
) -> ArtifactPushResult:
    """Publish serialized artifact manifest bytes and staged blobs by digest only.

    ``blobs`` must describe exactly the empty config and the payload as
    ``(digest, size, path)``. The manifest digest is computed from the bytes
    given, which are uploaded unchanged. No tags are moved. Registry, integrity
    and local I/O failures raise :class:`ArtifactPublicationError` with
    ``stage`` set and the original error chained as ``__cause__``.

    Precondition: staged files are hashed only when a blob is uploaded. If the
    registry already reports a blob with the same digest and size, the local
    file is not read, so callers that need proof of their local bytes must
    verify them first.
    """
    return _publish_by_digest(client, manifest_data, blobs, verify_subject, wrap_errors=True)


@contextmanager
def _publication_stage(stage: str, message: str, wrap: bool):
    try:
        yield
    except (RegistryRequestError, BlobIntegrityError, OSError) as error:
        if not wrap:
            raise
        raise ArtifactPublicationError(f"{message}: {error}", stage=stage) from error


def _publish_by_digest(
    client: RegistryClient,
    manifest_data: bytes,
    blobs: Iterable[Tuple[str, int, Path]],
    verify_subject: bool,
    *,
    wrap_errors: bool,
) -> ArtifactPushResult:
    if not isinstance(manifest_data, bytes) or len(manifest_data) > MAX_MANIFEST_BYTES:
        raise InvalidArtifactError(f"Artifact manifest must be bytes of at most {MAX_MANIFEST_BYTES}.")
    manifest = parse_artifact_manifest(manifest_data)
    staged = {}
    for blob_digest, blob_size, blob_path in blobs:
        if type(blob_size) is not int or blob_size < 0:
            raise InvalidArtifactError(f"Blob {blob_digest} size must be a non-negative integer.")
        if blob_digest in staged and staged[blob_digest][0] != blob_size:
            raise InvalidArtifactError(f"Blob {blob_digest} is listed with conflicting sizes.")
        staged.setdefault(blob_digest, (blob_size, Path(blob_path)))
    required = {EMPTY_DIGEST: len(EMPTY_CONFIG), manifest.payload.digest: manifest.payload.size}
    if {key: value[0] for key, value in staged.items()} != required:
        raise InvalidArtifactError(
            "Staged blobs must be exactly the empty config and the manifest payload with matching sizes."
        )
    digest = "sha256:" + hashlib.sha256(manifest_data).hexdigest()
    size = len(manifest_data)
    subject = manifest.subject
    if subject and verify_subject:
        with _publication_stage(
            STAGE_SUBJECT, f"Artifact subject {subject.digest} could not be verified", wrap_errors
        ):
            client.verify_manifest_descriptor(
                subject.digest, subject.digest, subject.size, subject.media_type
            )
    for blob_digest, (blob_size, blob_path) in staged.items():
        with _publication_stage(
            STAGE_BLOB, f"Artifact blob {blob_digest} could not be published", wrap_errors
        ):
            exposed = client.blob_descriptor_matches(blob_digest, blob_size)
            if not exposed:
                client.upload_blob_monolithic(blob_digest, blob_path)
                exposed = client.blob_descriptor_matches(blob_digest, blob_size)
        if not exposed:
            raise ArtifactPublicationError(
                f"Registry did not expose uploaded blob {blob_digest}.", stage=STAGE_BLOB
            )
    with _publication_stage(
        STAGE_MANIFEST, f"Artifact manifest {digest} could not be published", wrap_errors
    ):
        headers = client.put_manifest(digest, manifest_data, OCI_MANIFEST_MEDIA_TYPE)
    subject_status = None
    if subject:
        reported = _header(headers, "OCI-Subject")
        if reported is not None and reported.strip() != subject.digest:
            raise ArtifactPublicationError(
                "Registry reported an OCI-Subject that does not match the artifact subject.",
                stage=STAGE_SUBJECT,
            )
        subject_status = SUBJECT_UNCONFIRMED if reported is None else SUBJECT_CONFIRMED
    with _publication_stage(
        STAGE_MANIFEST, f"Artifact manifest {digest} could not be verified", wrap_errors
    ):
        client.verify_manifest_descriptor(digest, digest, size, OCI_MANIFEST_MEDIA_TYPE)
    descriptor = Descriptor(OCI_MANIFEST_MEDIA_TYPE, digest, size, manifest.artifact_type)
    return ArtifactPushResult(_reference(client, digest), descriptor, (), subject_status)


def pull_artifact(
    client: RegistryClient,
    reference: str,
    layout_dir,
    *,
    tag: Optional[str] = None,
    expected_artifact_type: Optional[str] = None,
    expected_subject_digest: Optional[str] = None,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> Artifact:
    """Download and verify a registry artifact into a local OCI layout."""
    _check_max_payload(max_payload_bytes)
    by_digest = isinstance(reference, str) and reference.startswith("sha256:")
    if by_digest:
        _check_digest(reference, "reference")
    else:
        _check_tag(reference)
    if tag is not None:
        _check_tag(tag)
    if expected_subject_digest is not None:
        _check_digest(expected_subject_digest, "expected subject")
    data, digest, content_type = client.fetch_manifest_bytes(
        reference, OCI_MANIFEST_MEDIA_TYPE, MAX_MANIFEST_BYTES,
        expected_digest=reference if by_digest else None,
    )
    if content_type != OCI_MANIFEST_MEDIA_TYPE:
        raise UnsupportedArtifactError(
            f"Registry returned {content_type!r}, not an OCI image manifest."
        )
    manifest = parse_artifact_manifest(data)
    if expected_artifact_type is not None and manifest.artifact_type != expected_artifact_type:
        raise UnsupportedArtifactError(
            f"Artifact type {manifest.artifact_type!r} is not {expected_artifact_type!r}."
        )
    if expected_subject_digest is not None and (
        manifest.subject is None or manifest.subject.digest != expected_subject_digest
    ):
        raise InvalidArtifactError("Artifact subject does not match the expected subject.")
    if manifest.payload.size > max_payload_bytes:
        raise InvalidArtifactError(f"Artifact payload exceeds {max_payload_bytes} bytes.")
    layout = Path(layout_dir)
    _ensure_layout(layout)
    client.pull_blob(
        manifest.payload.digest, _blob_path(layout, manifest.payload.digest), manifest.payload.size
    )
    _publish_bytes(layout, EMPTY_CONFIG, EMPTY_DIGEST)
    _publish_bytes(layout, data, digest)
    descriptor = Descriptor(OCI_MANIFEST_MEDIA_TYPE, digest, len(data), manifest.artifact_type)
    _record_in_index(layout, descriptor, tag if tag is not None else (None if by_digest else reference))
    return read_artifact_layout(layout, digest, max_payload_bytes=max_payload_bytes)


def list_referrers(
    client: RegistryClient,
    subject_digest: str,
    artifact_type: Optional[str] = None,
) -> ReferrersResult:
    """Query the OCI 1.1 referrers API; HTTP 404 reports unsupported discovery."""
    _check_digest(subject_digest, "subject")
    if artifact_type is not None:
        _check_media_type(artifact_type, "artifactType filter")
    url = f"{client.base_url}/{client.repository}/referrers/{subject_digest}"
    expected_path = urllib.parse.urlsplit(url).path
    if artifact_type:
        url += "?" + urllib.parse.urlencode({"artifactType": artifact_type})
    descriptors = []
    filter_applied = bool(artifact_type)
    seen = set()
    pages = 0
    while True:
        if pages >= MAX_REFERRERS_PAGES:
            raise RegistryRequestError(
                f"Referrers listing exceeded {MAX_REFERRERS_PAGES} pages."
            )
        seen.add(url)
        pages += 1
        status, body, headers = client.get_referrers(url, MAX_MANIFEST_BYTES)
        if status == 404 and pages == 1:
            return ReferrersResult(REFERRERS_NOT_FOUND, (), False, pages)
        if status != 200:
            raise RegistryRequestError(f"Referrers request failed with HTTP {status}.")
        content_type = (_header(headers, "Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != OCI_INDEX_MEDIA_TYPE:
            raise UnsupportedArtifactError(
                f"Referrers response has Content-Type {content_type!r}, not an OCI index."
            )
        index = _load_json(body, "Referrers response")
        if (
            not isinstance(index, dict)
            or type(index.get("schemaVersion")) is not int
            or index["schemaVersion"] != 2
            or index.get("mediaType") != OCI_INDEX_MEDIA_TYPE
            or not isinstance(index.get("manifests"), list)
        ):
            raise InvalidArtifactError("Referrers response is not a valid OCI image index.")
        descriptors.extend(_parse_descriptor(item, "referrer") for item in index["manifests"])
        applied = _header(headers, "OCI-Filters-Applied") or ""
        if "artifacttype" not in {item.strip().lower() for item in applied.split(",")}:
            filter_applied = False
        match = _LINK_NEXT.search(_header(headers, "Link") or "")
        if not match:
            break
        url = urllib.parse.urljoin(url, match.group(1))
        target = urllib.parse.urlsplit(url)
        if target.fragment or target.path != expected_path or url in seen:
            raise RegistryRequestError(
                "Referrers pagination link must stay on the same referrers path without repeating."
            )
    if artifact_type:
        descriptors = [item for item in descriptors if item.artifact_type == artifact_type]
    return ReferrersResult(REFERRERS_SUPPORTED, tuple(descriptors), filter_applied, pages)


def require_referrer(
    client: RegistryClient,
    subject_digest: str,
    descriptor: Descriptor,
    *,
    attempts: int = REFERRER_DISCOVERY_ATTEMPTS,
    delays: Tuple[float, ...] = REFERRER_DISCOVERY_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> Descriptor:
    """Require the OCI referrers API to list ``descriptor`` for ``subject_digest``.

    Uses an unfiltered listing and matches digest, size and mediaType, plus
    artifactType only when the listed entry includes it. HTTP 404 fails
    immediately; only a supported listing that lacks the descriptor is retried,
    up to ``attempts`` times. Raises ArtifactPublicationError(stage="referrers").
    """
    if type(attempts) is not int or attempts < 1 or len(delays) < attempts - 1:
        raise ValueError("Referrer discovery needs at least one attempt and a delay between attempts.")
    for attempt in range(attempts):
        found = list_referrers(client, subject_digest)
        if not found.supported:
            raise ArtifactPublicationError(
                f"Registry did not serve the OCI referrers API for {subject_digest} (HTTP 404).",
                stage=STAGE_REFERRERS,
            )
        for item in found.manifests:
            if (
                item.digest == descriptor.digest
                and item.size == descriptor.size
                and item.media_type == descriptor.media_type
                and (item.artifact_type is None or item.artifact_type == descriptor.artifact_type)
            ):
                return item
        if attempt + 1 < attempts:
            sleep(delays[attempt])
    raise ArtifactPublicationError(
        f"Referrers listing for {subject_digest} did not include {descriptor.digest} "
        f"after {attempts} attempts.",
        stage=STAGE_REFERRERS,
    )
