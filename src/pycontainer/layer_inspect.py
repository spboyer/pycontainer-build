"""Daemon-free inspection of the layers in a local OCI image.

Images are read through ``pycontainer.local_image`` sources. Each layer is
streamed once: its decompressed tar is hashed to verify the config DiffID
while member metadata and regular-file content digests are recorded. Nothing
is extracted. Whiteouts follow the OCI image-spec layer rules, so the reported
filesystem is the one a runtime would assemble.
"""
import gzip
import hashlib
import heapq
import json
import tarfile
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterator, List, Optional, Tuple

from .blob_store import BlobIntegrityError, HASH_CHUNK_SIZE, digest_filename
from .builder import (
    IMAGE_CONFIG_MEDIA_TYPES,
    IMAGE_MANIFEST_MEDIA_TYPES,
    OCI_INDEX_MEDIA_TYPES,
    UnsupportedPlatformError,
    _select_index_descriptor,
    _validate_registry_descriptor,
    parse_platform,
    parse_platform_variant,
)
from .local_image import (
    BZIP2_MAGIC,
    GZIP_LAYER_MEDIA_TYPES,
    GZIP_MAGIC,
    MAX_ARCHIVE_MEMBERS,
    MAX_LAYER_UNCOMPRESSED_BYTES,
    OCI_TRANSPORT,
    TAR_LAYER_MEDIA_TYPES,
    XZ_MAGIC,
    ZSTD_LAYER_MEDIA_TYPES,
    ZSTD_MAGIC,
    LocalImageError,
    LocalImageReference,
    _BoundedTarInfo,
    check_local_image_bounds,
    open_local_image_source,
    parse_local_image_reference,
)
from .registry_client import RegistryRequestError

MAX_IMAGE_MEMBERS = 1_000_000
MAX_PATH_BYTES = 4096
MAX_LAYER_PATH_BYTES = 16 * 1024 * 1024
MAX_IMAGE_NODES = 1_000_000
MAX_IMAGE_PATH_BYTES = 64 * 1024 * 1024
MAX_EXTENDED_HEADERS = 100
MAX_LAYER_EXTENDED_HEADER_BYTES = 16 * 1024 * 1024
TOP_ITEMS = 10

WHITEOUT_PREFIX = ".wh."
OPAQUE_WHITEOUT = ".wh..wh..opq"
ADDED, MODIFIED, DELETED, UNCHANGED = "added", "modified", "deleted", "unchanged"
CHANGE_MARKS = {ADDED: "A", MODIFIED: "M", DELETED: "D", UNCHANGED: " "}

BLOAT_PATTERNS = (
    ("python-bytecode", "Python bytecode caches (__pycache__, *.pyc)"),
    ("pip-cache", "pip download cache"),
    ("apt-cache", "APT package lists and downloaded archives"),
    ("vcs", "version-control metadata (.git, .hg, .svn)"),
)

INSPECTION_ERRORS = (
    LocalImageError, RegistryRequestError, UnsupportedPlatformError, BlobIntegrityError, ValueError,
)


class _InspectionTarInfo(_BoundedTarInfo):
    """Bound chained extended headers before tarfile recursively processes them."""

    def _proc_member(self, tarfile_obj):
        extended = self.type in (
            tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
            tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK,
        )
        if extended:
            count = getattr(tarfile_obj, "_inspection_extended_header_count", 0) + 1
            size = getattr(tarfile_obj, "_inspection_extended_header_bytes", 0) + self.size
            if count > MAX_EXTENDED_HEADERS:
                raise LayerInspectionError(
                    f"Layer has more than {MAX_EXTENDED_HEADERS} consecutive extended headers."
                )
            if size > MAX_LAYER_EXTENDED_HEADER_BYTES:
                raise LayerInspectionError(
                    f"Layer extended headers exceed {MAX_LAYER_EXTENDED_HEADER_BYTES} bytes."
                )
            tarfile_obj._inspection_extended_header_count = count
            tarfile_obj._inspection_extended_header_bytes = size
        else:
            tarfile_obj._inspection_extended_header_count = 0
        return super()._proc_member(tarfile_obj)


class LayerInspectionError(RuntimeError):
    """Raised when an image or layer cannot be inspected completely and safely."""


@dataclass(slots=True)
class _Inode:
    size: int
    digest: str
    layer: int
    links: int = 0


@dataclass(slots=True)
class Entry:
    kind: str
    layer: int
    mode: Optional[int] = None
    uid: int = 0
    gid: int = 0
    mtime: float = 0.0
    xattrs: Tuple[Tuple[str, str], ...] = ()
    size: int = 0
    digest: Optional[str] = None
    link: Optional[str] = None
    inode: Optional[_Inode] = None

    def key(self):
        return (
            self.kind, self.mode, self.uid, self.gid, self.mtime, self.xattrs,
            self.size, self.digest, self.link,
        )

    def to_dict(self) -> Dict:
        data = {"type": self.kind, "size": self.size, "layer": self.layer}
        if self.mode is not None:
            data.update(mode=f"{self.mode:04o}", uid=self.uid, gid=self.gid, mtime=self.mtime)
        if self.xattrs:
            data["xattrs"] = dict(self.xattrs)
        if self.digest:
            data["digest"] = self.digest
        if self.link is not None:
            data["linkTarget"] = self.link
        return data


@dataclass(slots=True)
class _Node:
    entry: Entry
    children: Optional[Dict[str, "_Node"]] = None


@dataclass
class LayerReport:
    index: int
    digest: str
    media_type: str
    compressed_size: int
    diff_id: Optional[str] = None
    created_by: Optional[str] = None
    uncompressed_size: int = 0
    entries: int = 0
    content_bytes: int = 0
    added: int = 0
    modified: int = 0
    deleted: int = 0
    unchanged: int = 0
    shadowed_bytes: int = 0
    changes: Optional[List[Tuple[str, str, Optional[Entry]]]] = None

    def to_dict(self) -> Dict:
        data = {
            "index": self.index, "digest": self.digest, "mediaType": self.media_type,
            "compressedSize": self.compressed_size, "uncompressedSize": self.uncompressed_size,
            "diffId": self.diff_id, "createdBy": self.created_by, "entries": self.entries,
            "contentBytes": self.content_bytes, "added": self.added, "modified": self.modified,
            "deleted": self.deleted, "unchanged": self.unchanged,
            "shadowedBytes": self.shadowed_bytes,
        }
        if self.changes is not None:
            data["files"] = [
                {"change": change, "path": path, **(entry.to_dict() if entry else {})}
                for change, path, entry in self.changes
            ]
        return data


def display(text: str) -> str:
    """Escape control, formatting and undecodable characters for terminal output."""
    out = []
    for character in text:
        code = ord(character)
        if 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif character == "\\":
            out.append("\\\\")
        elif character.isprintable():
            out.append(character)
        else:
            out.append(character.encode("unicode_escape").decode("ascii"))
    return "".join(out)


def normalize_member_path(name: str, layer: int) -> str:
    """Return a root-relative POSIX path, rejecting names that escape the root."""
    if "\x00" in name:
        raise LayerInspectionError(f"Layer {layer} contains a path with a NUL byte.")
    if len(name.encode("utf-8", "surrogateescape")) > MAX_PATH_BYTES:
        raise LayerInspectionError(f"Layer {layer} contains a path longer than {MAX_PATH_BYTES} bytes.")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if name.startswith("/"):
        raise LayerInspectionError(f"Layer {layer} contains absolute path {display(name)!r}.")
    if ".." in parts:
        raise LayerInspectionError(
            f"Layer {layer} contains unsafe path {display(name)!r} that escapes the image root."
        )
    return "/".join(parts)


def _split(path: str) -> Tuple[str, str]:
    parent, _, name = path.rpartition("/")
    return parent, name


class _CountingReader:
    """Hash and count decompressed bytes, enforcing the uncompressed bound."""

    def __init__(self, source, layer: int):
        self.source, self.layer = source, layer
        self.hasher = hashlib.sha256()
        self.count = 0
        self.trailing_nonzero = False
        self.draining = False

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.count += len(data)
        if self.count > MAX_LAYER_UNCOMPRESSED_BYTES:
            raise LayerInspectionError(
                f"Layer {self.layer} exceeds {MAX_LAYER_UNCOMPRESSED_BYTES} uncompressed bytes."
            )
        if self.draining and data.strip(b"\0"):
            self.trailing_nonzero = True
        self.hasher.update(data)
        return data

    def drain(self) -> None:
        self.draining = True
        while self.read(HASH_CHUNK_SIZE):
            pass


def _check_magic(path: Path, media_type: str, layer: int) -> None:
    with path.open("rb") as blob:
        head = blob.read(len(XZ_MAGIC))
    found = next(
        (name for name, magic in (("gzip", GZIP_MAGIC), ("zstd", ZSTD_MAGIC), ("xz", XZ_MAGIC),
                                  ("bzip2", BZIP2_MAGIC)) if head.startswith(magic)),
        None,
    )
    expected = (
        "gzip" if media_type in GZIP_LAYER_MEDIA_TYPES
        else "zstd" if media_type in ZSTD_LAYER_MEDIA_TYPES
        else None
    )
    if found != expected:
        actual = f"{found}-compressed" if found else "an uncompressed tar"
        raise LayerInspectionError(
            f"Layer {layer} declares media type {media_type!r}, but its content is {actual}."
        )


def _open_decompressed(path: Path, media_type: str, layer: int):
    if media_type in TAR_LAYER_MEDIA_TYPES | GZIP_LAYER_MEDIA_TYPES | ZSTD_LAYER_MEDIA_TYPES:
        _check_magic(path, media_type, layer)
    if media_type in TAR_LAYER_MEDIA_TYPES:
        return path.open("rb")
    if media_type in GZIP_LAYER_MEDIA_TYPES:
        return gzip.open(path, "rb")
    if media_type in ZSTD_LAYER_MEDIA_TYPES:
        try:
            from compression import zstd
        except ImportError as error:
            raise LayerInspectionError(
                f"Layer {layer} is zstd-compressed; inspecting zstd layers requires Python 3.14 or newer."
            ) from error
        return zstd.open(path, "rb")
    raise LayerInspectionError(f"Layer {layer} has unsupported media type {media_type!r}.")


def _corruption_errors() -> Tuple[type, ...]:
    errors = [tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile, UnicodeError]
    try:
        from compression import zstd
        errors.append(zstd.ZstdError)
    except ImportError:
        pass
    return tuple(errors)


def _member_entry(tar: tarfile.TarFile, member: tarfile.TarInfo, layer: int) -> Entry:
    xattrs = tuple(sorted(
        (key, value) for key, value in member.pax_headers.items()
        if key.startswith(("SCHILY.xattr.", "LIBARCHIVE.xattr."))
    ))
    common = {
        "layer": layer, "mode": member.mode & 0o7777, "uid": member.uid, "gid": member.gid,
        "mtime": member.mtime, "xattrs": xattrs,
    }
    if member.issparse():
        raise LayerInspectionError(f"Layer {layer} contains unsupported sparse file {display(member.name)!r}.")
    if member.isdir():
        return Entry("dir", **common)
    if member.issym():
        return Entry("symlink", link=member.linkname, **common)
    if member.islnk():
        target = normalize_member_path(member.linkname, layer)
        if not target:
            raise LayerInspectionError(f"Layer {layer} hardlink {display(member.name)!r} targets the image root.")
        return Entry("hardlink", link=target, **common)
    if member.ischr() or member.isblk():
        kind = "char" if member.ischr() else "block"
        return Entry(kind, link=f"{member.devmajor}:{member.devminor}", **common)
    if member.isfifo():
        return Entry("fifo", **common)
    if member.isreg():
        hasher = hashlib.sha256()
        stream = tar.extractfile(member)
        remaining = member.size
        while remaining:
            chunk = stream.read(min(HASH_CHUNK_SIZE, remaining))
            if not chunk:
                raise LayerInspectionError(f"Layer {layer} ends inside the content of {display(member.name)!r}.")
            hasher.update(chunk)
            remaining -= len(chunk)
        digest = f"sha256:{hasher.hexdigest()}"
        return Entry("file", size=member.size, digest=digest,
                     inode=_Inode(member.size, digest, layer), **common)
    raise LayerInspectionError(
        f"Layer {layer} contains unsupported tar entry type {member.type!r} at {display(member.name)!r}."
    )


def read_layer(path: Path, report: LayerReport, budget: List[int]):
    """Stream one layer blob and return its (path, entry, whiteout-kind) records."""
    index = report.index
    records: List[Tuple[str, Optional[Entry], Optional[str]]] = []
    path_bytes = 0
    archive_end = 0
    try:
        with _open_decompressed(path, report.media_type, index) as raw:
            reader = _CountingReader(raw, index)
            with tarfile.open(fileobj=reader, mode="r|", tarinfo=_InspectionTarInfo,
                              encoding="utf-8", errors="surrogateescape") as tar:
                while (member := tar.next()) is not None:
                    tar.members.clear()
                    report.entries += 1
                    budget[0] += 1
                    if report.entries > MAX_ARCHIVE_MEMBERS:
                        raise LayerInspectionError(f"Layer {index} has more than {MAX_ARCHIVE_MEMBERS} entries.")
                    if budget[0] > MAX_IMAGE_MEMBERS:
                        raise LayerInspectionError(f"Image has more than {MAX_IMAGE_MEMBERS} layer entries.")
                    record = _record(tar, member, index, report)
                    path_bytes += len(record[0].encode("utf-8", "surrogateescape"))
                    if record[1] is not None and record[1].link is not None:
                        path_bytes += len(record[1].link.encode("utf-8", "surrogateescape"))
                    if path_bytes > MAX_LAYER_PATH_BYTES:
                        raise LayerInspectionError(
                            f"Layer {index} exceeds {MAX_LAYER_PATH_BYTES} bytes of member paths."
                        )
                    records.append(record)
                archive_end = tar.offset
                if tar.fileobj.buf.strip(b"\0"):
                    reader.trailing_nonzero = True
            reader.drain()
    except LayerInspectionError:
        raise
    except LocalImageError as error:
        raise LayerInspectionError(f"Layer {index} ({report.digest}) is malformed: {error}") from error
    except _corruption_errors() as error:
        raise LayerInspectionError(f"Layer {index} ({report.digest}) is corrupt: {error}") from error
    except RecursionError as error:
        raise LayerInspectionError(
            f"Layer {index} ({report.digest}) has too many chained extended headers."
        ) from error
    except OSError as error:
        raise LayerInspectionError(f"Layer {index} ({report.digest}) could not be read: {error}") from error
    if reader.trailing_nonzero:
        raise LayerInspectionError(f"Layer {index} ({report.digest}) has data after the end of its tar archive.")
    if reader.count < archive_end + 2 * tarfile.BLOCKSIZE:
        raise LayerInspectionError(f"Layer {index} ({report.digest}) is truncated before the end of its tar archive.")
    report.uncompressed_size = reader.count
    actual = f"sha256:{reader.hasher.hexdigest()}"
    if actual != report.diff_id:
        raise BlobIntegrityError(
            f"Layer {index} has DiffID {actual}, but the image config declares {report.diff_id}."
        )
    return records


def _record(tar, member, index: int, report: LayerReport):
    path = normalize_member_path(member.name, index)
    parent, name = _split(path)
    if name == OPAQUE_WHITEOUT:
        if not member.isreg() or member.size:
            raise LayerInspectionError(f"Layer {index} contains invalid whiteout {display(member.name)!r}.")
        return parent, None, "opaque"
    if name.startswith(WHITEOUT_PREFIX):
        target = name[len(WHITEOUT_PREFIX):]
        if target in ("", ".", "..") or target.startswith(WHITEOUT_PREFIX):
            raise LayerInspectionError(f"Layer {index} contains invalid whiteout {display(member.name)!r}.")
        if not member.isreg() or member.size:
            raise LayerInspectionError(f"Layer {index} contains invalid whiteout {display(member.name)!r}.")
        return f"{parent}/{target}" if parent else target, None, "whiteout"
    if not path:
        if not member.isdir():
            raise LayerInspectionError(f"Layer {index} replaces the image root with a non-directory.")
        return "", None, "root"
    entry = _member_entry(tar, member, index)
    report.content_bytes += entry.size
    return path, entry, None


class Filesystem:
    """Union filesystem assembled from layer records with OCI whiteout semantics."""

    def __init__(self):
        self.root = _Node(Entry("dir", 0), {})
        self.shadowed_total = 0
        self.shadowed: List[Tuple[int, str, int, int]] = []
        self.before: Optional[Dict[str, Optional[Entry]]] = None
        self.node_count = 1
        self.path_bytes = 0
        self.before_path_bytes = 0
        self.reported_path_bytes = 0

    def _add_node(self, path: str) -> None:
        path_size = len(path.encode("utf-8", "surrogateescape"))
        if self.node_count >= MAX_IMAGE_NODES:
            raise LayerInspectionError(
                f"Image has more than {MAX_IMAGE_NODES} filesystem nodes."
            )
        if self.path_bytes + path_size > MAX_IMAGE_PATH_BYTES:
            raise LayerInspectionError(
                f"Image exceeds {MAX_IMAGE_PATH_BYTES} bytes of retained filesystem paths."
            )
        self.node_count += 1
        self.path_bytes += path_size

    def _report_path(self, path: str) -> None:
        self.reported_path_bytes += len(path.encode("utf-8", "surrogateescape"))
        if self.reported_path_bytes > MAX_IMAGE_PATH_BYTES:
            raise LayerInspectionError(
                f"Inspection report exceeds {MAX_IMAGE_PATH_BYTES} bytes of file paths."
            )

    @staticmethod
    def _retain_entry(entry: Entry) -> None:
        if entry.inode is not None:
            entry.inode.links += 1

    def _release_entry(self, entry: Entry, path: str, layer: int) -> None:
        inode = entry.inode
        if inode is None:
            return
        inode.links -= 1
        if inode.links == 0 and inode.size:
            self._shadow(inode.size, path, inode.layer, layer)

    def lookup(self, path: str) -> Optional[_Node]:
        node = self.root
        for part in path.split("/") if path else ():
            if node.children is None or part not in node.children:
                return None
            node = node.children[part]
        return node

    def walk(self, node: Optional[_Node] = None, prefix: str = "") -> Iterator[Tuple[str, Entry]]:
        stack = [(prefix, node or self.root)]
        while stack:
            base, current = stack.pop()
            for name, child in sorted((current.children or {}).items(), reverse=True):
                child_path = f"{base}/{name}" if base else name
                yield child_path, child.entry
                if child.children:
                    stack.append((child_path, child))

    def _touch(self, path: str, before: Optional[Entry]) -> None:
        if self.before is not None and path not in self.before:
            self.before_path_bytes += len(path.encode("utf-8", "surrogateescape"))
            if self.before_path_bytes > MAX_IMAGE_PATH_BYTES:
                raise LayerInspectionError(
                    f"Image diff exceeds {MAX_IMAGE_PATH_BYTES} bytes of file paths."
                )
            self.before[path] = before

    def _remove(self, parent: _Node, name: str, path: str, layer: int) -> List[Tuple[str, Entry]]:
        node = parent.children.pop(name)
        removed = [(path, node.entry), *self.walk(node, path)]
        self.node_count -= len(removed)
        self.path_bytes -= sum(
            len(removed_path.encode("utf-8", "surrogateescape"))
            for removed_path, _ in removed
        )
        for removed_path, entry in removed:
            self._touch(removed_path, entry)
            self._release_entry(entry, removed_path, layer)
        return removed

    def _shadow(self, size: int, path: str, origin: int, layer: int) -> None:
        self.shadowed_total += size
        item = (size, path, origin, layer)
        (heapq.heappush if len(self.shadowed) < TOP_ITEMS else heapq.heappushpop)(self.shadowed, item)

    def _validate_whiteout_parent(self, path: str, layer: int) -> None:
        node = self.root
        walked = []
        for part in path.split("/") if path else ():
            walked.append(part)
            child = node.children.get(part)
            if child is None:
                return
            parent_path = "/".join(walked)
            if child.entry.kind == "symlink":
                raise LayerInspectionError(
                    f"Layer {layer} places a whiteout beneath symlink {display(parent_path)!r}."
                )
            if child.children is None:
                raise LayerInspectionError(
                    f"Layer {layer} places a whiteout beneath non-directory {display(parent_path)!r}."
                )
            node = child

    def _parent(self, path: str, layer: int, created: List[Tuple[str, str, Entry]]) -> Tuple[_Node, str]:
        """Return the parent node, creating implicit directories for missing parents."""
        node, walked = self.root, []
        *parents, name = path.split("/")
        for part in parents:
            walked.append(part)
            parent_path = "/".join(walked)
            child = node.children.get(part)
            if child is not None and child.entry.kind == "symlink":
                raise LayerInspectionError(
                    f"Layer {layer} places {display(path)!r} beneath symlink {display(parent_path)!r}."
                )
            if child is None or child.children is None:
                change = ADDED if child is None else MODIFIED
                if child is not None:
                    self._remove(node, part, parent_path, layer)
                else:
                    self._touch(parent_path, None)
                self._add_node(parent_path)
                child = _Node(Entry("dir", layer), {})
                node.children[part] = child
                created.append((change, parent_path, child.entry))
            node = child
        return node, name

    def _resolve_hardlink(self, path: str, entry: Entry, layer: int) -> None:
        target = self.lookup(entry.link)
        if target is None:
            raise LayerInspectionError(
                f"Layer {layer} hardlink {display(path)!r} targets missing path {display(entry.link)!r}."
            )
        if target.entry.kind == "dir":
            raise LayerInspectionError(f"Layer {layer} hardlink {display(path)!r} targets a directory.")
        if target.entry.kind not in ("file", "hardlink") or target.entry.inode is None:
            raise LayerInspectionError(
                f"Layer {layer} hardlink {display(path)!r} does not target a regular file."
            )
        entry.inode = target.entry.inode
        entry.size = entry.inode.size
        entry.digest = entry.inode.digest

    def apply(self, report: LayerReport, records, keep_changes: bool) -> None:
        layer = report.index
        shadowed_before = self.shadowed_total
        changes: Optional[list] = [] if keep_changes else None

        def deleted(removed: List[Tuple[str, Entry]]) -> None:
            report.deleted += len(removed)
            if changes is not None:
                for removed_path, entry in removed:
                    self._report_path(removed_path)
                    changes.append((DELETED, removed_path, entry))

        # Whiteouts hide lower-layer content only, so they apply before this layer's additions.
        for path, _, kind in records:
            if kind not in ("whiteout", "opaque"):
                continue
            parent_path = path if kind == "opaque" else _split(path)[0]
            self._validate_whiteout_parent(parent_path, layer)
            node = self.lookup(path)
            if kind == "opaque":
                if node is None or node.children is None:
                    continue
                victims = [(node, name, f"{path}/{name}" if path else name) for name in sorted(node.children)]
            else:
                if node is None:
                    continue
                parent, name = _split(path)
                victims = [(self.lookup(parent), name, path)]
            for parent_node, name, victim in victims:
                deleted(self._remove(parent_node, name, victim, layer))
        layer_changes: Dict[str, str] = {}

        def record(change: str, path: str, entry: Entry) -> None:
            if path not in layer_changes:
                layer_changes[path] = change
                setattr(report, change, getattr(report, change) + 1)
            if changes is not None:
                self._report_path(path)
                changes.append((layer_changes[path], path, entry))

        for path, entry, kind in records:
            if kind is not None:
                continue
            if entry.kind == "hardlink":
                self._resolve_hardlink(path, entry, layer)
            created: List[Tuple[str, str, Entry]] = []
            parent, name = self._parent(path, layer, created)
            for item in created:
                record(*item)
            existing = parent.children.get(name)
            if existing is None:
                change = ADDED
                self._touch(path, None)
                self._add_node(path)
                self._retain_entry(entry)
                parent.children[name] = _Node(entry, {} if entry.kind == "dir" else None)
            elif existing.entry.kind == "dir" and entry.kind == "dir":
                if existing.entry.mode is None:
                    change = ADDED if existing.entry.layer == layer else UNCHANGED
                else:
                    change = UNCHANGED if existing.entry.key() == entry.key() else MODIFIED
                if change == MODIFIED:
                    self._touch(path, existing.entry)
                existing.entry = entry
            else:
                old = existing.entry
                self._touch(path, old)
                if old.kind == "dir":
                    for child in sorted(existing.children):
                        deleted(self._remove(existing, child, f"{path}/{child}", layer))
                self._release_entry(old, path, layer)
                change = MODIFIED if old.key() != entry.key() else UNCHANGED
                existing.entry = entry
                existing.children = {} if entry.kind == "dir" else None
                self._retain_entry(entry)
            record(change, path, entry)
        report.shadowed_bytes = self.shadowed_total - shadowed_before
        report.changes = changes

    def diff(self) -> List[Tuple[str, str, Optional[Entry], Optional[Entry]]]:
        result = []
        for path, before in sorted((self.before or {}).items()):
            node = self.lookup(path)
            after = node.entry if node else None
            if before is None and after is None:
                continue
            if before is None:
                change = ADDED
            elif after is None:
                change = DELETED
            elif before.key() == after.key() or (before.kind == after.kind == "dir" and None in (before.mode, after.mode)):
                continue
            else:
                change = MODIFIED
            result.append((change, path, before, after))
        return result


def _observations(fs: Filesystem) -> Tuple[Dict, List[Dict]]:
    totals = {"files": 0, "directories": 0, "symlinks": 0, "other": 0, "bytes": 0}
    largest: List[Tuple[int, str]] = []
    by_digest: Dict[str, List] = {}
    counted_inodes = set()
    bloat = {key: [0, 0, []] for key, _ in BLOAT_PATTERNS}
    for path, entry in fs.walk():
        kind = {"file": "files", "hardlink": "files",
                "dir": "directories", "symlink": "symlinks"}.get(entry.kind, "other")
        totals[kind] += 1
        if entry.kind not in ("file", "hardlink"):
            continue
        inode = id(entry.inode) if entry.inode is not None else id(entry)
        if inode not in counted_inodes:
            totals["bytes"] += entry.size
            counted_inodes.add(inode)
        (heapq.heappush if len(largest) < TOP_ITEMS else heapq.heappushpop)(largest, (entry.size, path))
        if entry.size and entry.digest:
            group = by_digest.setdefault(entry.digest, [entry.size, set(), []])
            group[1].add(inode)
            if len(group[2]) < 3:
                group[2].append(path)
        category = _bloat_category(path)
        if category:
            item = bloat[category]
            item[0] += 1
            item[1] += entry.size
            if len(item[2]) < 3:
                item[2].append(path)

    observations = []
    if fs.shadowed_total:
        observations.append({
            "kind": "shadowed-files",
            "bytes": fs.shadowed_total,
            "message": "File content in earlier layers that later layers delete or replace; it is not visible "
                       "in the final filesystem but is still stored and transferred with those layers.",
            "paths": [{"path": p, "size": s, "layer": o, "removedBy": r}
                      for s, p, o, r in sorted(fs.shadowed, reverse=True)],
        })
    duplicates = sorted(
        ((size * (len(inodes) - 1), size, len(inodes), paths)
         for size, inodes, paths in by_digest.values() if len(inodes) > 1),
        reverse=True,
    )
    if duplicates:
        observations.append({
            "kind": "duplicate-content",
            "bytes": sum(item[0] for item in duplicates),
            "message": "Regular files with identical content at more than one visible path "
                       "(bytes beyond the first copy, uncompressed).",
            "paths": [{"paths": paths, "size": size, "copies": count} for _, size, count, paths in duplicates[:TOP_ITEMS]],
        })
    for key, description in BLOAT_PATTERNS:
        count, size, paths = bloat[key]
        if count:
            observations.append({
                "kind": key, "bytes": size, "files": count,
                "message": f"{description} present in the final filesystem; check whether the image needs them.",
                "paths": [{"path": p} for p in paths],
            })
    if largest:
        observations.append({
            "kind": "largest-files",
            "bytes": sum(size for size, _ in largest),
            "message": f"The {len(largest)} largest visible files.",
            "paths": [{"path": p, "size": s} for s, p in sorted(largest, reverse=True)],
        })
    return totals, observations


def _bloat_category(path: str) -> Optional[str]:
    parts = path.split("/")
    if "__pycache__" in parts or path.endswith((".pyc", ".pyo")):
        return "python-bytecode"
    if "/.cache/pip/" in f"/{path}":
        return "pip-cache"
    if path.startswith(("var/lib/apt/lists/", "var/cache/apt/")):
        return "apt-cache"
    if any(part in (".git", ".hg", ".svn") for part in parts[:-1]):
        return "vcs"
    return None


def resolve_reference(image: str) -> LocalImageReference:
    """Parse an inspect target; a bare directory with an oci-layout file means ``oci:<dir>``."""
    reference = parse_local_image_reference(image)
    if reference is not None:
        return reference
    path = Path(image)
    if (path / "oci-layout").is_file():
        return LocalImageReference(OCI_TRANSPORT, path)
    raise LayerInspectionError(
        f"{image!r} is not a local image. Inspection reads local images only: use "
        "oci:<dir>[:<ref>|@sha256:<digest>], docker-archive:<file>[:<tag>], or a directory containing oci-layout."
    )


def _index_platforms(index: Dict) -> List[Dict]:
    descriptors = index.get("manifests")
    if not isinstance(descriptors, list) or not descriptors:
        raise LayerInspectionError("Image index does not list any manifests.")
    for position, descriptor in enumerate(descriptors, 1):
        if not isinstance(descriptor, dict):
            raise LayerInspectionError(f"Image index descriptor {position} is not an object.")
        platform = descriptor.get("platform")
        if platform is None:
            continue
        if not isinstance(platform, dict):
            raise LayerInspectionError(f"Image index descriptor {position} has invalid platform metadata.")
        if any(not isinstance(platform.get(key), str) or not platform[key]
               for key in ("os", "architecture")):
            raise LayerInspectionError(f"Image index descriptor {position} has invalid platform metadata.")
        variant = platform.get("variant")
        if variant is not None and (not isinstance(variant, str) or not variant):
            raise LayerInspectionError(f"Image index descriptor {position} has an invalid platform variant.")
    return descriptors


def _platform_text(platform: Dict) -> str:
    return "/".join(str(platform[k]) for k in ("os", "architecture", "variant") if platform.get(k))


def _select_manifest(source, platform: Optional[str]) -> Tuple[Dict, Optional[str], Optional[str]]:
    manifest, digest = source.pull_manifest(None)
    variant = None
    if manifest.get("mediaType") in OCI_INDEX_MEDIA_TYPES:
        descriptors = _index_platforms(manifest)
        if platform:
            os_name, arch = parse_platform(platform)
            descriptor, variant = _select_index_descriptor(manifest, os_name, arch, parse_platform_variant(platform))
        elif len(descriptors) == 1:
            descriptor = descriptors[0]
            variant = (descriptor.get("platform") or {}).get("variant") if isinstance(descriptor, dict) else None
        else:
            available = ", ".join(
                sorted({_platform_text(d["platform"]) for d in descriptors
                        if isinstance(d, dict) and isinstance(d.get("platform"), dict)})
            ) or "none"
            raise LayerInspectionError(
                f"Image index has {len(descriptors)} manifests; select one with --platform. "
                f"Available platforms: {available}."
            )
        digest, size, media_type = _validate_registry_descriptor(descriptor, "Selected manifest descriptor")
        if media_type not in IMAGE_MANIFEST_MEDIA_TYPES:
            raise LayerInspectionError(
                f"Selected descriptor {digest} has media type {media_type!r}; nested indexes are not supported."
            )
        manifest, _ = source.pull_manifest(digest, expected_digest=digest, expected_size=size)
        if manifest.get("mediaType") != media_type:
            raise LayerInspectionError("Selected manifest media type does not match its index descriptor.")
    elif manifest.get("mediaType") not in IMAGE_MANIFEST_MEDIA_TYPES:
        raise LayerInspectionError(f"Image has unsupported manifest media type {manifest.get('mediaType')!r}.")
    if manifest.get("schemaVersion") != 2:
        raise LayerInspectionError("Image manifest has an unsupported schema version.")
    return manifest, digest, variant


def _load_config(source, manifest: Dict, blobs: Path, platform: Optional[str], variant: Optional[str]):
    config_digest, config_size, config_type = _validate_registry_descriptor(manifest.get("config"), "Config descriptor")
    if config_type not in IMAGE_CONFIG_MEDIA_TYPES:
        raise LayerInspectionError(f"Image config has unsupported media type {config_type!r}.")
    descriptors = manifest.get("layers")
    if not isinstance(descriptors, list):
        raise LayerInspectionError("Image manifest does not contain a layer list.")
    layers = [_validate_registry_descriptor(d, f"Layer {i} descriptor") for i, d in enumerate(descriptors, 1)]
    check_local_image_bounds(config_size, [size for _, size, _ in layers])
    config_path = blobs / digest_filename(config_digest)
    source.pull_blob(config_digest, config_path, config_size)
    try:
        config = json.loads(config_path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise LayerInspectionError("Image config is not valid JSON.") from error
    if not isinstance(config, dict):
        raise LayerInspectionError("Image config is not a JSON object.")
    config_variant = config.get("variant")
    if config_variant is not None and (not isinstance(config_variant, str) or not config_variant):
        raise LayerInspectionError("Image config has an invalid platform variant.")
    if variant is not None and config_variant is not None and variant != config_variant:
        raise LayerInspectionError(
            f"Selected index platform variant {variant!r} does not match image config variant "
            f"{config_variant!r}."
        )
    actual = {"os": config.get("os"), "architecture": config.get("architecture"),
              "variant": variant or config_variant}
    if not all(isinstance(actual[k], str) and actual[k] for k in ("os", "architecture")):
        raise LayerInspectionError("Image config does not declare its os and architecture.")
    if platform:
        os_name, arch = parse_platform(platform)
        wanted_variant = parse_platform_variant(platform)
        if (actual["os"], actual["architecture"]) != (os_name, arch) or (
            wanted_variant and actual["variant"] != wanted_variant
        ):
            raise UnsupportedPlatformError(
                f"Image config platform {_platform_text(actual)} does not match requested {platform}."
            )
    rootfs = config.get("rootfs")
    diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
        raise LayerInspectionError(
            f"Image config lists {len(diff_ids) if isinstance(diff_ids, list) else 'no'} DiffIDs for {len(layers)} layers."
        )
    for index, diff_id in enumerate(diff_ids, 1):
        try:
            digest_filename(diff_id)
        except ValueError as error:
            raise LayerInspectionError(f"Image config DiffID {index} is not a sha256 digest.") from error
    history = config.get("history")
    created_by: List[Optional[str]] = [None] * len(layers)
    if isinstance(history, list) and all(isinstance(h, dict) for h in history):
        steps = [h.get("created_by") for h in history if not h.get("empty_layer")]
        if len(steps) == len(layers):
            created_by = [s if isinstance(s, str) else None for s in steps]
    return (config_digest, config_size), config, _platform_text(actual), layers, diff_ids, created_by


def inspect_image(
    image: str,
    platform: Optional[str] = None,
    files: bool = False,
    layers: Optional[List[int]] = None,
    diff: Optional[Tuple[int, int]] = None,
) -> Dict:
    """Inspect a local image and return a JSON-serializable report."""
    if platform:
        parse_platform(platform)
    reference = resolve_reference(image)
    os_name, arch = parse_platform(platform) if platform else (None, None)
    variant = parse_platform_variant(platform) if platform else None
    with tempfile.TemporaryDirectory(prefix="pycontainer-inspect-") as temporary:
        blobs = Path(temporary)
        with open_local_image_source(
            reference, blobs, blobs / "output", os_name, arch, variant, quiet=True
        ) as source:
            manifest, manifest_digest, selected_variant = _select_manifest(source, platform)
            config_ref, _, resolved_platform, descriptors, diff_ids, created_by = _load_config(
                source, manifest, blobs, platform, selected_variant
            )
            count = len(descriptors)
            for value in (layers or []):
                if not 1 <= value <= count:
                    raise LayerInspectionError(f"--layer {value} is out of range; the image has {count} layers.")
            if diff and not (0 <= diff[0] < diff[1] <= count):
                raise LayerInspectionError(
                    f"--diff {diff[0]} {diff[1]} is invalid; use 0 <= FROM < TO <= {count}."
                )
            fs, reports, budget, diff_result = Filesystem(), [], [0], None
            listed = set(layers or (range(1, count + 1) if files else ()))
            for index, (digest, size, media_type) in enumerate(descriptors, 1):
                report = LayerReport(index, digest, media_type, size, diff_ids[index - 1], created_by[index - 1])
                layer_path = blobs / digest_filename(digest)
                source.pull_blob(digest, layer_path, size)
                if diff and index == diff[0] + 1:
                    fs.before = {}
                fs.apply(report, read_layer(layer_path, report, budget), index in listed)
                if reference.transport == OCI_TRANSPORT:
                    layer_path.unlink(missing_ok=True)
                if diff and index == diff[1]:
                    diff_result = fs.diff()
                    fs.before = None
                reports.append(report)
            source_name = getattr(source, "source_name", str(reference.path))
    totals, observations = _observations(fs)
    result = {
        "image": source_name,
        "reference": image,
        "platform": resolved_platform,
        "manifest": {"digest": manifest_digest, "mediaType": manifest.get("mediaType")},
        "config": {"digest": config_ref[0], "size": config_ref[1]},
        "layers": [report.to_dict() for report in reports],
        "filesystem": totals,
        "observations": observations,
    }
    if diff:
        result["diff"] = {
            "from": diff[0], "to": diff[1],
            "changes": [
                {"change": change, "path": path,
                 "before": before.to_dict() if before else None, "after": after.to_dict() if after else None}
                for change, path, before, after in diff_result
            ],
        }
    return result


def human_size(size: Optional[int]) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def _entry_line(change: str, path: str, entry: Optional[Dict]) -> str:
    entry = entry or {}
    link = f" -> {display(entry['linkTarget'])}" if entry.get("type") in ("symlink", "hardlink") else ""
    return (f"  {CHANGE_MARKS.get(change, '?')} {entry.get('type', '-'):<8} {entry.get('mode', '----'):>4} "
            f"{human_size(entry.get('size')):>10}  {display(path)}{link}")


def render_text(result: Dict) -> str:
    lines = [
        f"Image:     {display(result['image'])}",
        f"Platform:  {display(str(result['platform']))}",
        f"Manifest:  {display(str(result['manifest']['digest'] or '-'))} "
        f"({display(str(result['manifest']['mediaType']))})",
        f"Config:    {display(str(result['config']['digest']))}",
        "",
        f"{'#':>3}  {'digest':<19}  {'compressed':>10}  {'uncompressed':>12}  {'members':>7}  "
        f"{'+add':>6}  {'~mod':>6}  {'-del':>6}  created by",
    ]
    for layer in result["layers"]:
        created = display((layer["createdBy"] or "-").strip())
        lines.append(
            f"{layer['index']:>3}  {layer['digest'][:19]:<19}  {human_size(layer['compressedSize']):>10}  "
            f"{human_size(layer['uncompressedSize']):>12}  {layer['entries']:>7}  {layer['added']:>6}  "
            f"{layer['modified']:>6}  {layer['deleted']:>6}  {created[:60]}"
        )
    fs = result["filesystem"]
    lines += [
        "",
        f"Final filesystem: {fs['files']} files ({human_size(fs['bytes'])}), {fs['directories']} directories, "
        f"{fs['symlinks']} symlinks, {fs['other']} other",
    ]
    for layer in result["layers"]:
        if "files" not in layer:
            continue
        lines += ["", f"Layer {layer['index']} contents ({layer['digest']}):"]
        lines += [_entry_line(item["change"], item["path"], item) for item in layer["files"]]
    if "diff" in result:
        diff = result["diff"]
        lines += ["", f"Changes from layer {diff['from']} to layer {diff['to']} ({len(diff['changes'])} paths):"]
        lines += [_entry_line(item["change"], item["path"], item["after"] or item["before"]) for item in diff["changes"]]
    if result["observations"]:
        lines += ["", "Observations (uncompressed sizes from tar headers; not measured savings):"]
    for item in result["observations"]:
        lines.append(f"  - {item['kind']}: {human_size(item['bytes'])}. {item['message']}")
        for path in item["paths"]:
            if "paths" in path:
                names = ", ".join(display(p) for p in path["paths"])
                lines.append(f"      {path['copies']} x {human_size(path['size'])}: {names}")
            elif "removedBy" in path:
                lines.append(f"      {human_size(path['size'])}  {display(path['path'])} "
                             f"(layer {path['layer']}, removed by layer {path['removedBy']})")
            elif "size" in path:
                lines.append(f"      {human_size(path['size'])}  {display(path['path'])}")
            else:
                lines.append(f"      {display(path['path'])}")
    return "\n".join(lines)


def run_inspect(image, platform=None, files=False, layers=None, diff=None, as_json=False) -> Tuple[str, int]:
    """Run inspection for the CLI, returning (output, exit code); errors never produce a partial report."""
    try:
        result = inspect_image(image, platform, files, layers, tuple(diff) if diff else None)
    except (LayerInspectionError, *INSPECTION_ERRORS, tarfile.TarError, OSError) as error:
        return f"error: {display(str(error))}", 1
    if as_json:
        return json.dumps(result, indent=2), 0
    return render_text(result), 0
