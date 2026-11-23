"""Daemon-free readers for local OCI image layouts and Docker-save archives.

Local sources expose the same pull contract as ``RegistryClient``
(``pull_manifest`` and ``pull_blob``) so the builder applies one set of
manifest, configuration and platform validation to every base image.
"""
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Dict, List, Optional, Tuple

from .blob_store import (
    BlobIntegrityError,
    HASH_CHUNK_SIZE,
    blob_matches,
    create_temporary_file,
    digest_filename,
    publish_content_addressed,
)
from .registry_client import parse_image_reference

OCI_TRANSPORT = "oci"
DOCKER_ARCHIVE_TRANSPORT = "docker-archive"

MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000
MAX_INDEX_ENTRIES = 1_000
MAX_IMAGE_LAYERS = 1_000
MAX_LAYER_UNCOMPRESSED_BYTES = 64 * 1024 * 1024 * 1024
MAX_LINK_HOPS = 8
MAX_EXTENDED_HEADER_BYTES = 1024 * 1024

REF_NAME_ANNOTATION = "org.opencontainers.image.ref.name"
CONTAINERD_NAME_ANNOTATION = "io.containerd.image.name"

OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
INDEX_MEDIA_TYPES = {
    OCI_INDEX_MEDIA_TYPE,
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_TAR_LAYER = "application/vnd.oci.image.layer.v1.tar"
OCI_GZIP_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
OCI_ZSTD_LAYER = "application/vnd.oci.image.layer.v1.tar+zstd"
TAR_LAYER_MEDIA_TYPES = {OCI_TAR_LAYER, "application/vnd.docker.image.rootfs.diff.tar"}
GZIP_LAYER_MEDIA_TYPES = {OCI_GZIP_LAYER, "application/vnd.docker.image.rootfs.diff.tar.gzip"}
ZSTD_LAYER_MEDIA_TYPES = {OCI_ZSTD_LAYER}

GZIP_MAGIC = b"\x1f\x8b"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
BZIP2_MAGIC = b"BZh"
XZ_MAGIC = b"\xfd7zXZ\x00"

_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
_SHA256_FILENAME = re.compile(r"(?:blobs/sha256/)?([0-9a-f]{64})(?:\.json)?")


class LocalImageError(RuntimeError):
    """Raised when a local base image is missing, ambiguous or malformed."""


@dataclass(frozen=True)
class LocalImageReference:
    transport: str
    path: Path
    reference: Optional[str] = None
    digest: Optional[str] = None

    @property
    def selector(self) -> Optional[str]:
        return self.digest or self.reference


def parse_local_image_reference(value: str) -> Optional[LocalImageReference]:
    """Parse ``oci:`` and ``docker-archive:`` base images; return None otherwise.

    The path ends at the first ``:`` after an optional Windows drive prefix,
    matching skopeo's transport syntax. An OCI layout may instead select an
    index descriptor with an ``@sha256:<hex>`` suffix.
    """
    if not isinstance(value, str):
        return None
    for transport in (OCI_TRANSPORT, DOCKER_ARCHIVE_TRANSPORT):
        if value.startswith(f"{transport}:"):
            rest = value[len(transport) + 1 :]
            break
    else:
        return None
    if transport == OCI_TRANSPORT and rest.startswith("//"):
        rest = rest[2:]

    digest = None
    if transport == OCI_TRANSPORT:
        path_part, separator, digest_part = rest.rpartition("@")
        if separator and digest_part.startswith("sha256:"):
            try:
                digest_filename(digest_part)
            except ValueError as error:
                raise ValueError(
                    f"Invalid digest in local base image {value!r}; use "
                    "@sha256:<64 lowercase hex characters>."
                ) from error
            rest, digest = path_part, digest_part

    start = 2 if _WINDOWS_DRIVE.match(rest) else 0
    colon = rest.find(":", start)
    path_text, reference = (rest, None) if colon < 0 else (rest[:colon], rest[colon + 1 :])
    if not path_text:
        raise ValueError(f"Local base image {value!r} is missing a path.")
    if reference is not None:
        if not reference:
            raise ValueError(f"Local base image {value!r} has an empty reference.")
        if digest:
            raise ValueError(
                f"Local base image {value!r} specifies both a reference and a digest."
            )
        if any(character.isspace() or ord(character) < 32 for character in reference):
            raise ValueError(f"Local base image {value!r} has an invalid reference.")
    return LocalImageReference(transport, Path(path_text), reference, digest)


def open_local_image_source(
    reference: LocalImageReference,
    layers_dir: Path,
    output_dir: Path,
    os_name: Optional[str],
    arch: Optional[str],
    variant: Optional[str] = None,
    *,
    progress: Callable[[str], None] = print,
):
    """Open a local base image source after rejecting overlap with the output.

    ``os_name=None`` and ``arch=None`` skip the Docker archive's early platform
    check for inspection; Docker archive callers must validate the returned
    ``DockerArchiveSource.platform`` themselves.
    """
    if _paths_overlap(reference.path, Path(output_dir)):
        raise LocalImageError(
            f"Local base image {reference.path} overlaps the build output "
            f"directory {output_dir}; copy it elsewhere or choose another output."
        )
    if reference.transport == OCI_TRANSPORT:
        return OCILayoutSource(reference)
    return DockerArchiveSource(
        reference, Path(layers_dir), os_name, arch, variant, progress=progress
    )


def check_local_image_bounds(config_size: int, layer_sizes: List[int]) -> None:
    """Reject local images whose descriptors exceed the reader's bounds."""
    if config_size > MAX_METADATA_BYTES:
        raise LocalImageError(
            f"Local base image config exceeds {MAX_METADATA_BYTES} bytes."
        )
    if len(layer_sizes) > MAX_IMAGE_LAYERS:
        raise LocalImageError(
            f"Local base image has more than {MAX_IMAGE_LAYERS} layers."
        )
    for index, size in enumerate(layer_sizes, 1):
        if size > MAX_LAYER_UNCOMPRESSED_BYTES:
            raise LocalImageError(
                f"Local base image layer {index} exceeds "
                f"{MAX_LAYER_UNCOMPRESSED_BYTES} bytes."
            )


def verify_layer_diff_ids(layers, config: Dict) -> None:
    """Check that each base layer's uncompressed digest matches rootfs.diff_ids."""
    rootfs = config.get("rootfs")
    diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
        raise LocalImageError(
            f"Local base image config lists "
            f"{len(diff_ids) if isinstance(diff_ids, list) else 'no'} DiffIDs "
            f"for {len(layers)} layers."
        )
    for index, (layer, expected) in enumerate(zip(layers, diff_ids), 1):
        try:
            digest_filename(expected)
        except ValueError as error:
            raise LocalImageError(
                f"Local base image DiffID {index} is not a sha256 digest."
            ) from error
        if layer.media_type in TAR_LAYER_MEDIA_TYPES:
            _require_plain_tar(Path(layer.tar_path), index)
            actual = layer.digest
        elif layer.media_type in GZIP_LAYER_MEDIA_TYPES:
            actual = _decompressed_digest(Path(layer.tar_path), gzip.open, "gzip")
        elif layer.media_type in ZSTD_LAYER_MEDIA_TYPES:
            try:
                from compression import zstd
            except ImportError as error:
                raise LocalImageError(
                    "Verifying zstd-compressed local base layers requires "
                    "Python 3.14 or newer."
                ) from error
            actual = _decompressed_digest(Path(layer.tar_path), zstd.open, "zstd")
        else:
            raise LocalImageError(
                f"Local base image layer {index} has unsupported media type "
                f"{layer.media_type!r}."
            )
        if actual != expected:
            raise BlobIntegrityError(
                f"Local base image layer {index} has DiffID {actual}, but its "
                f"config declares {expected}."
            )


def _require_plain_tar(path: Path, index: int) -> None:
    """Reject plain-tar layers whose bytes are compressed or not a tar stream."""
    invalid = f"Local base image layer {index} is not a valid uncompressed tar stream."
    try:
        with path.open("rb") as source:
            magic = source.read(len(XZ_MAGIC))
            if not magic:
                return
            if magic.startswith((GZIP_MAGIC, ZSTD_MAGIC, BZIP2_MAGIC, XZ_MAGIC)):
                raise BlobIntegrityError(
                    f"Local base image layer {index} is compressed, but its media "
                    "type declares an uncompressed tar."
                )
            source.seek(0)
            with tarfile.open(fileobj=source, mode="r:", tarinfo=_BoundedTarInfo) as tar:
                while tar.next() is not None:
                    tar.members.clear()
                end = tar.offset
            # tarfile stops silently at an invalid later header; require a real end.
            source.seek(end)
            trailer = source.read(tarfile.BLOCKSIZE)
    except BlobIntegrityError:
        raise
    except (tarfile.TarError, OSError) as error:
        raise BlobIntegrityError(invalid) from error
    if trailer.strip(b"\0"):
        raise BlobIntegrityError(invalid)


def _decompressed_digest(path: Path, opener, name: str) -> str:
    hasher = hashlib.sha256()
    size = 0
    try:
        with opener(path, "rb") as source:
            while chunk := source.read(HASH_CHUNK_SIZE):
                size += len(chunk)
                if size > MAX_LAYER_UNCOMPRESSED_BYTES:
                    raise LocalImageError(
                        f"Local base layer {path.name} decompresses beyond "
                        f"{MAX_LAYER_UNCOMPRESSED_BYTES} bytes."
                    )
                hasher.update(chunk)
    except LocalImageError:
        raise
    except (OSError, EOFError, zlib.error, ValueError) as error:
        raise BlobIntegrityError(
            f"Local base layer {path.name} is not a valid {name} stream."
        ) from error
    return f"sha256:{hasher.hexdigest()}"


def _paths_overlap(first: Path, second: Path) -> bool:
    left = os.path.normcase(str(first.resolve()))
    right = os.path.normcase(str(second.resolve()))
    try:
        common = os.path.commonpath([left, right])
    except ValueError:
        return False
    return common in (left, right)


def _load_json_object(data: bytes, description: str) -> Dict:
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise LocalImageError(f"{description} is not valid JSON.") from error
    if not isinstance(value, dict):
        raise LocalImageError(f"{description} is not a JSON object.")
    return value


def _validate_expected_size(expected_size: int) -> None:
    if type(expected_size) is not int or expected_size < 0:
        raise ValueError("Expected blob size must be a non-negative integer.")


def _existing_blob(destination: Path, digest: str, size: int) -> bool:
    if destination.exists() or destination.is_symlink():
        if blob_matches(destination, digest, size):
            return True
        raise BlobIntegrityError(
            f"Existing blob {destination} does not match {digest} and size "
            f"{size}; refusing to overwrite it."
        )
    return False


def _stage_stream(
    source: BinaryIO,
    directory: Path,
    expected_digest: Optional[str],
    expected_size: Optional[int],
    description: str,
) -> Tuple[str, int]:
    """Copy a stream into ``directory`` in chunks and publish it by digest."""
    directory.mkdir(parents=True, exist_ok=True)
    staged = create_temporary_file(directory, "local-blob")
    try:
        hasher = hashlib.sha256()
        size = 0
        with staged.open("wb") as target:
            while chunk := source.read(HASH_CHUNK_SIZE):
                size += len(chunk)
                if expected_size is not None and size > expected_size:
                    raise BlobIntegrityError(
                        f"{description} exceeds its expected size {expected_size}."
                    )
                hasher.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        digest = f"sha256:{hasher.hexdigest()}"
        if expected_size is not None and size != expected_size:
            raise BlobIntegrityError(
                f"{description} has size {size}, expected {expected_size}."
            )
        if expected_digest is not None and digest != expected_digest:
            raise BlobIntegrityError(
                f"{description} hashes to {digest}, expected {expected_digest}."
            )
        return publish_content_addressed(
            staged, directory / digest_filename(digest), digest, size
        )
    finally:
        staged.unlink(missing_ok=True)


def _descriptor_aliases(descriptor: Dict) -> List[str]:
    annotations = descriptor.get("annotations")
    if not isinstance(annotations, dict):
        return []
    return [
        name
        for key in (REF_NAME_ANNOTATION, CONTAINERD_NAME_ANNOTATION)
        if isinstance(name := annotations.get(key), str) and name
    ]


def _descriptor_name(descriptor: Dict) -> Optional[str]:
    aliases = _descriptor_aliases(descriptor)
    return aliases[0] if aliases else None


def _image_name(annotations) -> Optional[str]:
    if not isinstance(annotations, dict):
        return None
    for key in (CONTAINERD_NAME_ANNOTATION, REF_NAME_ANNOTATION):
        value = annotations.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _has_platform(descriptor: Dict) -> bool:
    platform = descriptor.get("platform")
    return (
        isinstance(platform, dict)
        and isinstance(platform.get("os"), str)
        and bool(platform.get("os"))
        and isinstance(platform.get("architecture"), str)
        and bool(platform.get("architecture"))
    )


class OCILayoutSource:
    """Read verified manifests and blobs from an OCI image layout directory."""

    def __init__(self, reference: LocalImageReference):
        self._media_types: Dict[str, str] = {}
        self.root = reference.path
        self.reference = reference
        self.source_name = str(reference.path)
        if not self.root.is_dir():
            raise LocalImageError(f"OCI layout {self.root} is not a directory.")
        layout = _load_json_object(
            self._read_file(self.root / "oci-layout", "OCI layout marker"),
            f"OCI layout marker in {self.root}",
        )
        if layout.get("imageLayoutVersion") != "1.0.0":
            raise LocalImageError(
                f"OCI layout {self.root} has unsupported imageLayoutVersion "
                f"{layout.get('imageLayoutVersion')!r}; expected '1.0.0'."
            )
        self.index = _load_json_object(
            self._read_file(self.root / "index.json", "OCI layout index"),
            f"OCI layout index in {self.root}",
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        """OCI layouts hold no open handles."""

    def _read_file(self, path: Path, description: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise LocalImageError(f"{description} {path} is missing or not a regular file.")
        with path.open("rb") as source:
            data = source.read(MAX_METADATA_BYTES + 1)
        if len(data) > MAX_METADATA_BYTES:
            raise LocalImageError(f"{description} {path} exceeds {MAX_METADATA_BYTES} bytes.")
        return data

    def _blob_path(self, digest: str) -> Path:
        name = digest_filename(digest)
        blobs = self.root / "blobs"
        for directory in (blobs, blobs / "sha256"):
            if directory.is_symlink():
                raise LocalImageError(
                    f"OCI layout directory {directory} must not be a symbolic link."
                )
        path = blobs / "sha256" / name
        if path.is_symlink() or not path.is_file():
            raise LocalImageError(
                f"OCI layout {self.root} is missing blob {digest} or it is not a "
                "regular file."
            )
        return path

    def _descriptors(self) -> List[Dict]:
        if self.index.get("schemaVersion") != 2:
            raise LocalImageError(f"OCI layout index in {self.root} must use schemaVersion 2.")
        descriptors = self.index.get("manifests")
        if not isinstance(descriptors, list) or not descriptors:
            raise LocalImageError(f"OCI layout index in {self.root} lists no manifests.")
        if len(descriptors) > MAX_INDEX_ENTRIES:
            raise LocalImageError(
                f"OCI layout index in {self.root} lists more than "
                f"{MAX_INDEX_ENTRIES} manifests."
            )
        for position, descriptor in enumerate(descriptors, 1):
            if not isinstance(descriptor, dict):
                raise LocalImageError(f"OCI layout index descriptor {position} is not an object.")
            try:
                digest_filename(descriptor.get("digest"))
            except ValueError as error:
                raise LocalImageError(
                    f"OCI layout index descriptor {position} has an invalid digest."
                ) from error
            size = descriptor.get("size")
            if type(size) is not int or size < 0:
                raise LocalImageError(f"OCI layout index descriptor {position} has an invalid size.")
            media_type = descriptor.get("mediaType")
            if not isinstance(media_type, str) or not media_type:
                raise LocalImageError(
                    f"OCI layout index descriptor {position} has an invalid media type."
                )
        return descriptors

    def _select(self) -> List[Dict]:
        descriptors = self._descriptors()
        if self.reference.digest:
            return self._unique(
                [d for d in descriptors if d["digest"] == self.reference.digest]
            )
        if self.reference.reference:
            wanted = self.reference.reference
            matches = [d for d in descriptors if wanted in _descriptor_aliases(d)]
            if (
                not matches
                and len(descriptors) == 1
                and wanted in _descriptor_aliases(self.index)
            ):
                matches = descriptors
            return self._unique(matches)
        return descriptors

    def _unique(self, matches: List[Dict]) -> List[Dict]:
        """Collapse selected descriptors that repeat one manifest under several names."""
        unique: Dict[Tuple[str, Optional[str]], Dict] = {}
        for descriptor in matches:
            platform = descriptor.get("platform")
            key = (
                descriptor["digest"],
                json.dumps(platform, sort_keys=True) if platform is not None else None,
            )
            existing = unique.get(key)
            if existing is None:
                unique[key] = descriptor
                continue
            if (existing["size"], existing["mediaType"]) != (
                descriptor["size"], descriptor["mediaType"]
            ):
                raise LocalImageError(
                    f"OCI layout {self.root} has conflicting descriptors for "
                    f"{descriptor['digest']}."
                )
            if _image_name(existing.get("annotations")) != _image_name(
                descriptor.get("annotations")
            ):
                unique[key] = {k: v for k, v in existing.items() if k != "annotations"}
        return list(unique.values())

    def _available(self) -> str:
        names = sorted(
            {
                name
                for d in self.index.get("manifests", [])
                if isinstance(d, dict)
                for name in _descriptor_aliases(d)
            }
        )
        return ", ".join(names) or "none"

    def pull_manifest(
        self,
        reference: Optional[str] = None,
        expected_digest: Optional[str] = None,
        expected_size: Optional[int] = None,
    ) -> Tuple[Dict, Optional[str]]:
        """Resolve the selected image (``reference=None``) or read a manifest blob."""
        if reference is not None:
            if expected_digest is None or reference != expected_digest or expected_size is None:
                raise LocalImageError("Local OCI manifests can only be read by verified digest.")
            return self._read_manifest(
                expected_digest, expected_size, self._media_types.get(expected_digest)
            )

        candidates = self._select()
        selector = self.reference.selector
        if not candidates:
            raise LocalImageError(
                f"OCI layout {self.root} has no image matching {selector!r}. "
                f"Available references: {self._available()}."
            )
        if len(candidates) == 1:
            descriptor = candidates[0]
            self.source_name = (
                _image_name(descriptor.get("annotations"))
                or _image_name(self.index.get("annotations"))
                or self.source_name
            )
            if (
                descriptor["mediaType"] in INDEX_MEDIA_TYPES
                or not _has_platform(descriptor)
            ):
                return self._read_manifest(
                    descriptor["digest"], descriptor["size"], descriptor["mediaType"]
                )

        names = {_descriptor_name(d) for d in candidates}
        if selector is None and len(names) > 1:
            raise LocalImageError(
                f"OCI layout {self.root} contains multiple images; select one "
                f"with oci:<path>:<reference>. Available references: {self._available()}."
            )
        for descriptor in candidates:
            if not _has_platform(descriptor):
                raise LocalImageError(
                    f"OCI layout {self.root} has multiple manifests for "
                    f"{selector or 'the image'} without platform metadata; select "
                    "one with oci:<path>@sha256:<digest>."
                )
        name = names.pop() if len(names) == 1 else None
        if name:
            self.source_name = name
        self._remember_media_types(candidates)
        return {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX_MEDIA_TYPE,
            "manifests": candidates,
        }, None

    def _remember_media_types(self, descriptors) -> None:
        for descriptor in descriptors:
            if (
                isinstance(descriptor, dict)
                and isinstance(descriptor.get("digest"), str)
                and isinstance(descriptor.get("mediaType"), str)
            ):
                self._media_types[descriptor["digest"]] = descriptor["mediaType"]

    def _read_manifest(
        self, digest: str, size: int, media_type: Optional[str]
    ) -> Tuple[Dict, str]:
        _validate_expected_size(size)
        if size > MAX_METADATA_BYTES:
            raise LocalImageError(f"Local OCI manifest {digest} exceeds {MAX_METADATA_BYTES} bytes.")
        data = self._read_file(self._blob_path(digest), "OCI layout manifest")
        if len(data) != size:
            raise BlobIntegrityError(
                f"Local OCI manifest {digest} has size {len(data)}, expected {size}."
            )
        actual = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if actual != digest:
            raise BlobIntegrityError(f"Local OCI manifest hashes to {actual}, expected {digest}.")
        manifest = _load_json_object(data, f"Local OCI manifest {digest}")
        declared = manifest.get("mediaType")
        if media_type is not None:
            if declared is None:
                manifest["mediaType"] = media_type
            elif declared != media_type:
                raise LocalImageError(
                    f"Local OCI manifest {digest} declares media type {declared!r}, "
                    f"but its index descriptor declares {media_type!r}."
                )
        if isinstance(manifest.get("manifests"), list):
            if len(manifest["manifests"]) > MAX_INDEX_ENTRIES:
                raise LocalImageError(
                    f"Local OCI index {digest} lists more than "
                    f"{MAX_INDEX_ENTRIES} manifests."
                )
            self._remember_media_types(manifest["manifests"])
        return manifest, digest

    def pull_blob(self, digest: str, dest_path: Path, expected_size: int) -> bool:
        """Copy a verified blob into the output store; return False if present."""
        _validate_expected_size(expected_size)
        dest_path = Path(dest_path)
        source_path = self._blob_path(digest)
        actual_size = source_path.stat().st_size
        if actual_size != expected_size:
            raise BlobIntegrityError(
                f"Local OCI blob {digest} has size {actual_size}, expected {expected_size}."
            )
        if _existing_blob(dest_path, digest, expected_size):
            if not blob_matches(source_path, digest, expected_size):
                raise BlobIntegrityError(
                    f"Local OCI blob {digest} does not match its digest."
                )
            return False
        with source_path.open("rb") as source:
            _stage_stream(
                source, dest_path.parent, digest, expected_size, f"Local OCI blob {digest}"
            )
        return True


def _normalize_member_name(name: str) -> Optional[str]:
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or re.match(r"[A-Za-z]:", name)
    ):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _repository_tag_key(tag: str):
    try:
        return parse_image_reference(tag)
    except ValueError:
        return tag


class _BoundedTarInfo(tarfile.TarInfo):
    """Reject oversized PAX and GNU long-name headers before tarfile buffers them."""

    def _proc_member(self, tarfile_obj):
        if (
            self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                          tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK)
            and self.size > MAX_EXTENDED_HEADER_BYTES
        ):
            raise LocalImageError(
                f"Docker archive extended header exceeds {MAX_EXTENDED_HEADER_BYTES} bytes."
            )
        return super()._proc_member(tarfile_obj)


class DockerArchiveSource:
    """Read a ``docker save`` tar archive without extracting it to disk."""

    def __init__(
        self,
        reference: LocalImageReference,
        layers_dir: Path,
        os_name: Optional[str],
        arch: Optional[str],
        variant: Optional[str] = None,
        *,
        progress: Callable[[str], None] = print,
    ):
        self.path = reference.path
        self.reference = reference
        self.layers_dir = layers_dir
        self.source_name = str(reference.path)
        self._progress = progress
        self.platform: Optional[Dict[str, str]] = None
        self._published: Dict[str, int] = {}
        if not self.path.is_file():
            raise LocalImageError(f"Docker archive {self.path} is not a file.")
        with self.path.open("rb") as source:
            magic = source.read(len(XZ_MAGIC))
        if magic.startswith((GZIP_MAGIC, ZSTD_MAGIC, BZIP2_MAGIC, XZ_MAGIC)):
            raise LocalImageError(
                f"Docker archive {self.path} is compressed; decompress it to the "
                "uncompressed tar produced by docker save."
            )
        try:
            self._tar = tarfile.open(self.path, mode="r:", tarinfo=_BoundedTarInfo)
        except (tarfile.TarError, OSError) as error:
            raise LocalImageError(f"Docker archive {self.path} is not a valid tar file.") from error
        try:
            self._members = self._index_members()
            self._manifest = self._build_manifest(os_name, arch, variant)
        except BaseException:
            self._tar.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self._tar.close()

    def _index_members(self) -> Dict[str, tarfile.TarInfo]:
        members: Dict[str, tarfile.TarInfo] = {}
        try:
            for count, member in enumerate(self._tar, 1):
                if count > MAX_ARCHIVE_MEMBERS:
                    raise LocalImageError(
                        f"Docker archive {self.path} has more than {MAX_ARCHIVE_MEMBERS} entries."
                    )
                name = _normalize_member_name(member.name)
                if name is None:
                    raise LocalImageError(
                        f"Docker archive {self.path} contains unsafe path {member.name!r}."
                    )
                if name in members:
                    raise LocalImageError(
                        f"Docker archive {self.path} contains duplicate entry {name!r}."
                    )
                if member.issparse():
                    raise LocalImageError(
                        f"Docker archive {self.path} contains unsupported sparse entry {name!r}."
                    )
                members[name] = member
        except tarfile.TarError as error:
            raise LocalImageError(f"Docker archive {self.path} is truncated or corrupt.") from error
        return members

    def _resolve(self, name, description: str) -> tarfile.TarInfo:
        if not isinstance(name, str):
            raise LocalImageError(f"Docker archive {description} path is not a string.")
        current = _normalize_member_name(name)
        for _ in range(MAX_LINK_HOPS + 1):
            if current is None:
                raise LocalImageError(
                    f"Docker archive {description} path {name!r} escapes the archive."
                )
            member = self._members.get(current)
            if member is None:
                raise LocalImageError(f"Docker archive {self.path} is missing {description} {current!r}.")
            if member.isreg():
                return member
            if member.issym():
                if member.linkname.startswith("/"):
                    current = None
                    continue
                current = _normalize_member_name(
                    posixpath.normpath(posixpath.join(posixpath.dirname(current), member.linkname))
                )
            elif member.islnk():
                current = _normalize_member_name(member.linkname)
            else:
                raise LocalImageError(
                    f"Docker archive {description} {current!r} is not a regular file."
                )
        raise LocalImageError(f"Docker archive {description} {name!r} has too many links.")

    def _open_member(self, member: tarfile.TarInfo) -> BinaryIO:
        source = self._tar.extractfile(member)
        if source is None:
            raise LocalImageError(f"Docker archive entry {member.name!r} is not readable.")
        return source

    def _read_member(self, name, description: str) -> bytes:
        member = self._resolve(name, description)
        if member.size > MAX_METADATA_BYTES:
            raise LocalImageError(
                f"Docker archive {description} exceeds {MAX_METADATA_BYTES} bytes."
            )
        try:
            data = self._open_member(member).read()
        except (tarfile.TarError, OSError) as error:
            raise LocalImageError(f"Docker archive {description} is truncated.") from error
        if len(data) != member.size:
            raise BlobIntegrityError(f"Docker archive {description} is truncated.")
        return data

    def _select_entry(self) -> Dict:
        try:
            entries = json.loads(self._read_member("manifest.json", "manifest.json"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise LocalImageError(f"Docker archive {self.path} manifest.json is not valid JSON.") from error
        if not isinstance(entries, list) or not entries:
            raise LocalImageError(f"Docker archive {self.path} manifest.json lists no images.")
        if len(entries) > MAX_INDEX_ENTRIES:
            raise LocalImageError(
                f"Docker archive {self.path} lists more than {MAX_INDEX_ENTRIES} images."
            )
        tags_by_entry = []
        for position, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise LocalImageError(f"Docker archive manifest entry {position} is not an object.")
            tags = entry.get("RepoTags") or []
            if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
                raise LocalImageError(f"Docker archive manifest entry {position} has invalid RepoTags.")
            tags_by_entry.append(tags)
        available = ", ".join(sorted({t for tags in tags_by_entry for t in tags})) or "none"

        wanted = self.reference.reference
        if wanted is None:
            if len(entries) != 1:
                raise LocalImageError(
                    f"Docker archive {self.path} contains {len(entries)} images; select "
                    f"one with docker-archive:<path>:<tag>. Available tags: {available}."
                )
            if tags_by_entry[0]:
                self.source_name = tags_by_entry[0][0]
            return entries[0]

        key = _repository_tag_key(wanted)
        matches = [
            entry
            for entry, tags in zip(entries, tags_by_entry)
            if any(_repository_tag_key(tag) == key for tag in tags)
        ]
        if not matches:
            raise LocalImageError(
                f"Docker archive {self.path} has no image tagged {wanted!r}. "
                f"Available tags: {available}."
            )
        if len(matches) > 1:
            raise LocalImageError(
                f"Docker archive {self.path} has multiple images tagged {wanted!r}."
            )
        self.source_name = wanted
        return matches[0]

    def _build_manifest(self, os_name: str, arch: str, variant: Optional[str]) -> Dict:
        entry = self._select_entry()
        config_path = entry.get("Config")
        layer_paths = entry.get("Layers")
        if not isinstance(layer_paths, list) or not all(isinstance(p, str) for p in layer_paths):
            raise LocalImageError(f"Docker archive {self.path} image has an invalid layer list.")
        if len(layer_paths) > MAX_IMAGE_LAYERS:
            raise LocalImageError(
                f"Docker archive {self.path} image has more than {MAX_IMAGE_LAYERS} layers."
            )

        config_bytes = self._read_member(config_path, "image config")
        config_digest = f"sha256:{hashlib.sha256(config_bytes).hexdigest()}"
        self._check_path_digest(config_path, config_digest, "image config")
        config = _load_json_object(config_bytes, f"Docker archive {self.path} image config")
        self._check_platform(config, os_name, arch, variant)
        rootfs = config.get("rootfs")
        diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
        if not isinstance(diff_ids, list) or len(diff_ids) != len(layer_paths):
            raise LocalImageError(
                f"Docker archive {self.path} image config DiffIDs do not match its "
                f"{len(layer_paths)} layers."
            )

        layer_members = [
            self._resolve(path, f"layer {position}")
            for position, path in enumerate(layer_paths, 1)
        ]
        check_local_image_bounds(
            len(config_bytes), [member.size for member in layer_members]
        )

        _stage_stream(
            io.BytesIO(config_bytes), self.layers_dir, config_digest,
            len(config_bytes), "Docker archive image config",
        )
        self._published[config_digest] = len(config_bytes)

        layers = []
        for position, (layer_path, member) in enumerate(zip(layer_paths, layer_members), 1):
            description = f"layer {position}"
            path_digest = self._path_digest(layer_path)
            self._progress(f"  Loading layer {position}/{len(layer_paths)} from {self.path.name}")
            try:
                digest, size = _stage_stream(
                    self._open_member(member), self.layers_dir, path_digest,
                    member.size, f"Docker archive {description}",
                )
            except (tarfile.TarError, OSError) as error:
                raise BlobIntegrityError(f"Docker archive {description} is truncated.") from error
            self._published[digest] = size
            layers.append(
                {"mediaType": self._layer_media_type(digest), "digest": digest, "size": size}
            )
        return {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "config": {
                "mediaType": OCI_CONFIG_MEDIA_TYPE,
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": layers,
        }

    def _check_platform(self, config: Dict, os_name: Optional[str], arch: Optional[str], variant: Optional[str]) -> None:
        self.platform = {
            key: value
            for key, value in (
                ("os", config.get("os")),
                ("architecture", config.get("architecture")),
                ("variant", config.get("variant")),
            )
            if isinstance(value, str) and value
        }
        if os_name is None and arch is None:
            return
        actual = (config.get("os"), config.get("architecture"))
        if actual != (os_name, arch) or (
            variant is not None and config.get("variant") != variant
        ):
            found = "/".join(str(part) for part in (*actual, config.get("variant")) if part)
            target = "/".join(part for part in (os_name, arch, variant) if part)
            raise LocalImageError(
                f"Docker archive {self.path} image platform {found} does not match "
                f"requested {target}."
            )

    def _path_digest(self, name: str) -> Optional[str]:
        normalized = _normalize_member_name(name) or ""
        match = _SHA256_FILENAME.fullmatch(normalized)
        return f"sha256:{match.group(1)}" if match else None

    def _check_path_digest(self, name: str, digest: str, description: str) -> None:
        expected = self._path_digest(name)
        if expected is not None and expected != digest:
            raise BlobIntegrityError(
                f"Docker archive {description} hashes to {digest}, but its path "
                f"names {expected}."
            )

    def _layer_media_type(self, digest: str) -> str:
        with (self.layers_dir / digest_filename(digest)).open("rb") as source:
            magic = source.read(len(ZSTD_MAGIC))
        if magic.startswith(GZIP_MAGIC):
            return OCI_GZIP_LAYER
        if magic.startswith(ZSTD_MAGIC):
            return OCI_ZSTD_LAYER
        return OCI_TAR_LAYER

    def pull_manifest(
        self,
        reference: Optional[str] = None,
        expected_digest: Optional[str] = None,
        expected_size: Optional[int] = None,
    ) -> Tuple[Dict, str]:
        """Return the OCI manifest synthesized from the selected archive image."""
        if reference is not None:
            raise LocalImageError("Docker archives do not contain nested manifests.")
        data = json.dumps(self._manifest, separators=(",", ":")).encode()
        return json.loads(data), f"sha256:{hashlib.sha256(data).hexdigest()}"

    def pull_blob(self, digest: str, dest_path: Path, expected_size: int) -> bool:
        """Confirm a blob staged from the archive is present in the output store."""
        _validate_expected_size(expected_size)
        dest_path = Path(dest_path)
        if (
            self._published.get(digest) == expected_size
            and dest_path == self.layers_dir / digest_filename(digest)
            and not dest_path.is_symlink()
            and dest_path.is_file()
            and dest_path.stat().st_size == expected_size
        ):
            return False
        raise LocalImageError(f"Docker archive {self.path} did not provide blob {digest}.")
