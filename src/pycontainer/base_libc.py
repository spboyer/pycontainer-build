"""Bounded libc evidence from verified, ordered base-image layers."""
import gzip
import hashlib
import re
import tarfile
import time
from contextlib import ExitStack
from pathlib import PurePosixPath


MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_LAYER_BYTES = 2 * 1024 * 1024 * 1024
MAX_LAYER_MEMBERS = 100_000
INSPECTION_TIMEOUT_SECONDS = 300
_CHUNK_BYTES = 1024 * 1024
_TAR_END_BYTES = 2 * tarfile.BLOCKSIZE
_DATABASES = {"var/lib/dpkg/status", "lib/apk/db/installed"}
_GZIP_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}
_TAR_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.docker.image.rootfs.diff.tar",
}
_VERSION = re.compile(
    r"(?:\d+:)?([12])\.(0|[1-9]\d?)(?:\.\d+)?(?:[-+~_][A-Za-z0-9.+~:_-]+)?"
)


def _check_deadline(deadline):
    if time.monotonic() > deadline:
        raise TimeoutError("Base libc inspection exceeded its time limit.")


class _BoundedReader:
    def __init__(self, source, deadline):
        self.source = source
        self.deadline = deadline
        self.size = 0
        self.tail = b""

    def read(self, size):
        _check_deadline(self.deadline)
        data = self.source.read(size)
        self.size += len(data)
        if self.size > MAX_LAYER_BYTES:
            raise ValueError("Base libc inspection exceeds the decoded layer size limit.")
        self.tail = (self.tail + data)[-_TAR_END_BYTES:]
        return data


class _BoundedTarInfo(tarfile.TarInfo):
    def _check_extended_header(self):
        if self.size < 0:
            raise ValueError("Base libc inspection found an invalid tar member size.")
        if self.size > MAX_METADATA_BYTES:
            raise ValueError("Base libc extended tar metadata exceeds its size limit.")

    def _proc_pax(self, archive):
        self._check_extended_header()
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        self._check_extended_header()
        return super()._proc_gnulong(archive)

    def _apply_pax_info(self, pax_headers, encoding, errors):
        # Validate before tarfile normalizes or uses a supplemental size.
        size = pax_headers.get("size")
        if size is not None:
            if not size.isascii() or not size.isdecimal():
                raise ValueError("Base libc inspection found an invalid tar member size.")
            normalized = size.lstrip("0") or "0"
            if len(normalized) > len(str(MAX_LAYER_BYTES)) or int(normalized) > MAX_LAYER_BYTES:
                raise ValueError("Base libc inspection found an invalid tar member size.")
        return super()._apply_pax_info(pax_headers, encoding, errors)


def _path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("Base libc inspection found an unsafe layer path.")
    return path.as_posix()


def _layer_records(layer, deadline):
    updates = {}
    deleted = set()
    with ExitStack() as stack:
        source = stack.enter_context(open(layer.tar_path, "rb"))
        digest = hashlib.sha256()
        size = 0
        while chunk := source.read(_CHUNK_BYTES):
            _check_deadline(deadline)
            size += len(chunk)
            if size > MAX_LAYER_BYTES:
                raise ValueError("Base libc inspection exceeds the layer size limit.")
            digest.update(chunk)
        if size != layer.size or f"sha256:{digest.hexdigest()}" != layer.digest:
            raise ValueError("Base libc layer digest or size does not match its descriptor.")
        source.seek(0)
        magic = source.read(2)
        source.seek(0)
        if layer.media_type in _GZIP_TYPES:
            if magic != b"\x1f\x8b":
                raise ValueError("Base libc layer media type does not match gzip content.")
            decoded = stack.enter_context(gzip.GzipFile(fileobj=source))
        elif layer.media_type in _TAR_TYPES and magic != b"\x1f\x8b":
            decoded = source
        else:
            raise ValueError("Base libc inspection requires a supported tar layer media type.")
        reader = _BoundedReader(decoded, deadline)
        with tarfile.open(fileobj=reader, mode="r|", tarinfo=_BoundedTarInfo) as archive:
            for index, member in enumerate(archive, 1):
                _check_deadline(deadline)
                if index > MAX_LAYER_MEMBERS:
                    raise ValueError("Base libc inspection exceeds the layer member limit.")
                if member.size < 0:
                    raise ValueError("Base libc inspection found an invalid tar member size.")
                if member.size > MAX_LAYER_BYTES:
                    raise ValueError("Base libc inspection exceeds the decoded layer size limit.")
                if member.sparse is not None:
                    raise ValueError("Base libc inspection does not support sparse layer entries.")
                name = _path(member.name)
                path = PurePosixPath(name)
                if path.name.startswith(".wh."):
                    whiteout_type = member.isfile() or (
                        member.ischr() and member.devmajor == member.devminor == 0
                    )
                    if not whiteout_type or member.size:
                        raise ValueError("Base libc inspection found an invalid whiteout.")
                    opaque = path.name == ".wh..wh..opq"
                    removed = path.parent if opaque else path.with_name(path.name[4:])
                    for database in _DATABASES:
                        candidate = PurePosixPath(database)
                        if removed == candidate or removed in candidate.parents:
                            deleted.add(database)
                    continue
                ancestor = any(
                    path in PurePosixPath(database).parents for database in _DATABASES
                )
                if ancestor and not member.isdir():
                    raise ValueError("Base libc metadata has a non-directory or linked ancestor.")
                if name not in _DATABASES:
                    continue
                if not member.isfile() or name in updates:
                    raise ValueError("Base libc metadata must be one regular file per layer.")
                if member.size > MAX_METADATA_BYTES:
                    raise ValueError("Base libc metadata exceeds its size limit.")
                record = archive.extractfile(member)
                if record is None:
                    raise ValueError("Base libc metadata cannot be read.")
                data = record.read(MAX_METADATA_BYTES + 1)
                if len(data) != member.size:
                    raise ValueError("Base libc metadata is truncated.")
                updates[name] = data.decode("utf-8")
            end = archive.offset
        # Drain under the same bounds to validate gzip trailers and tar end blocks.
        while reader.read(_CHUNK_BYTES):
            pass
        if reader.size < end + _TAR_END_BYTES or reader.tail != b"\0" * _TAR_END_BYTES:
            raise ValueError("Base libc layer has missing tar end blocks.")
    return deleted, updates


def _package_version(text, family, architecture):
    separator, package_key, version_key, arch_key = (
        (": ", "Package", "Version", "Architecture")
        if family == "glibc" else (":", "P", "V", "A")
    )
    package = "libc6" if family == "glibc" else "musl"
    required = {package_key, version_key, arch_key}
    if family == "glibc":
        required.add("Status")
    versions = []
    for stanza in re.split(r"\n\s*\n", text):
        fields = {}
        duplicate = set()
        continued = set()
        previous = None
        for line in stanza.splitlines():
            if line[:1].isspace():
                continued.add(previous)
                continue
            key, sep, value = line.partition(separator)
            if not sep:
                continue
            if key in fields:
                duplicate.add(key)
            fields[key] = value
            previous = key
        if fields.get(package_key) != package:
            continue
        if not required <= fields.keys() or required & (duplicate | continued):
            raise ValueError("Base libc package metadata is incomplete or ambiguous.")
        if fields[arch_key] != architecture:
            raise ValueError("Base libc package architecture does not match the target.")
        if family == "glibc" and fields["Status"] != "install ok installed":
            raise ValueError("Base libc package is not installed.")
        version = _VERSION.fullmatch(fields[version_key])
        if not version or version[1] != ("2" if family == "glibc" else "1"):
            raise ValueError("Base libc package version is unsupported.")
        versions.append(f"{family}-{version[1]}.{version[2]}")
    if len(versions) != 1:
        raise ValueError("Base libc package metadata is missing or ambiguous.")
    return versions[0]


def infer_libc_from_layers(layers, family, architecture, machine):
    """Read effective package metadata; the caller establishes source provenance."""
    deadline = time.monotonic() + INSPECTION_TIMEOUT_SECONDS
    files = {}
    for layer in layers:
        deleted, updates = _layer_records(layer, deadline)
        for name in deleted:
            files.pop(name, None)
        files.update(updates)
    expected = "var/lib/dpkg/status" if family == "glibc" else "lib/apk/db/installed"
    if set(files) != {expected}:
        raise ValueError(
            "Cannot validate target libc from absent or ambiguous base metadata. "
            "Pin a mapped codename tag or provide supported base PYTHON_LIBC."
        )
    package_arch = (
        ("armhf" if architecture == "arm" else architecture)
        if family == "glibc" else ("armv7" if machine == "armv7l" else machine)
    )
    result = _package_version(files[expected], family, package_arch)
    _check_deadline(deadline)
    return result
