"""Flatten builder-created OCI layer changesets into one equivalent layer.

Only uncompressed tar changesets are accepted. The result is applied on top of
the unchanged base layers, so OCI whiteouts that may hide base content are kept.
Entries whose effect cannot be represented exactly are rejected, never altered.
"""
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Sequence, Tuple

WHITEOUT_PREFIX = ".wh."
OPAQUE_MARKER = ".wh..wh..opq"
TAR_BLOCK_SIZE = tarfile.BLOCKSIZE
_XATTR_PREFIX = "SCHILY.xattr."
_FORBIDDEN_XATTR_PREFIXES = ("trusted.overlay.",)
# tarfile folds these standard PAX keys into TarInfo fields that are re-emitted.
_FIELD_PAX_KEYS = {
    "path", "linkpath", "size", "uid", "gid", "uname", "gname", "mtime", "atime", "ctime",
}
# Not TarInfo fields, so they are copied through explicitly.
_PRESERVED_TIME_KEYS = ("atime", "ctime")

_ENTRY, _WHITEOUT, _OPAQUE = "entry", "whiteout", "opaque"


class SquashError(ValueError):
    """Raised when builder layers cannot be flattened without changing behavior."""


@dataclass
class SquashStats:
    """Accounting for one squash: inputs, dropped content and the written size."""

    source_layers: int = 0
    source_bytes: int = 0
    output_bytes: int = 0
    output_members: int = 0
    dropped_entries: int = 0
    dropped_file_bytes: int = 0
    predicted_bytes: int = 0


@dataclass(eq=False)
class _Record:
    kind: str
    name: str
    path: str
    info: Optional[tarfile.TarInfo] = None
    source: Optional[int] = None
    link_target: Optional["_Record"] = None
    template: Optional[tarfile.TarInfo] = field(default=None, repr=False)

    def is_dir(self) -> bool:
        return self.kind == _ENTRY and self.info.isdir()


def _padded(size: int) -> int:
    return -(-size // TAR_BLOCK_SIZE) * TAR_BLOCK_SIZE


def _normalize(name: str, description: str) -> str:
    if not name or any(ord(char) < 0x20 or char in "\x7f\\" for char in name):
        raise SquashError(
            f"{description} {name!r} contains control characters or backslashes."
        )
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts:
        raise SquashError(f"{description} {name!r} names the layer root.")
    if ".." in parts:
        raise SquashError(f"{description} {name!r} contains '..' traversal.")
    return "/".join(parts)


def _ancestors(path: str) -> List[str]:
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _whiteout_key(path: str) -> str:
    directory, _, base = path.rpartition("/")
    return _join(directory, WHITEOUT_PREFIX + base)


def _opaque_key(directory: str) -> str:
    return _join(directory, OPAQUE_MARKER)


def _validate_member(info: tarfile.TarInfo, where: str) -> None:
    if info.sparse is not None:
        raise SquashError(f"{where} is a sparse file, which cannot be squashed exactly.")
    if info.type == tarfile.CONTTYPE or not (
        info.isreg() or info.isdir() or info.issym() or info.islnk()
    ):
        raise SquashError(
            f"{where} is a device, FIFO or unsupported tar entry type {info.type!r}."
        )
    if not info.isreg() and info.size:
        raise SquashError(f"{where} is not a regular file but carries {info.size} bytes.")
    for key in info.pax_headers:
        if key.startswith(_XATTR_PREFIX):
            attribute = key[len(_XATTR_PREFIX):]
            if attribute.startswith(_FORBIDDEN_XATTR_PREFIXES):
                raise SquashError(f"{where} carries unsupported xattr {attribute!r}.")
        elif key not in _FIELD_PAX_KEYS:
            raise SquashError(f"{where} carries unsupported PAX header {key!r}.")
    if (info.issym() or info.islnk()) and (
        not info.linkname or "\x00" in info.linkname
    ):
        raise SquashError(f"{where} has an empty or invalid link target.")


def _classify(info: tarfile.TarInfo, where: str) -> Tuple[str, str, str]:
    """Return (kind, normalized member name, affected filesystem path)."""
    name = _normalize(info.name, where)
    directory, _, base = name.rpartition("/")
    if any(part.startswith(WHITEOUT_PREFIX) for part in name.split("/")[:-1]):
        raise SquashError(f"{where} has a whiteout name in a parent directory.")
    if not base.startswith(WHITEOUT_PREFIX):
        return _ENTRY, name, name
    if not info.isreg() or info.size:
        raise SquashError(f"{where} whiteout marker must be an empty regular file.")
    if base == OPAQUE_MARKER:
        if not directory:
            raise SquashError(f"{where} makes the image root opaque, which is not supported.")
        return _OPAQUE, name, directory
    target = base[len(WHITEOUT_PREFIX):]
    if not target or target in (".", "..") or target.startswith(WHITEOUT_PREFIX):
        raise SquashError(f"{where} is a malformed or reserved whiteout name.")
    return _WHITEOUT, name, _join(directory, target)


class _Squasher:
    def __init__(self) -> None:
        self.state: Dict[str, _Record] = {}
        self.stats = SquashStats()

    def _drop(self, key: str) -> None:
        record = self.state.pop(key)
        if record.kind == _ENTRY:
            info = record.info
            self.stats.dropped_entries += 1
            if info.isreg():
                self.stats.dropped_file_bytes += info.size

    def _drop_under(self, path: str) -> None:
        prefix = path + "/"
        for key in [key for key in self.state if key.startswith(prefix)]:
            self._drop(key)

    def _is_deleted(self, path: str) -> bool:
        return path not in self.state and _whiteout_key(path) in self.state

    def _deleted_ancestor(self, path: str, include_self: bool = False) -> bool:
        dirs = _ancestors(path) + ([path] if include_self else [])
        return any(self._is_deleted(directory) for directory in dirs)

    def apply_layer(self, layer: int, members: List[Tuple[str, str, str, tarfile.TarInfo]]) -> None:
        # OCI whiteouts only hide lower layers, so apply them before this layer's entries.
        explicit_dirs = {path for kind, _, path, info in members if kind == _ENTRY and info.isdir()}
        for kind, name, path, info in members:
            if kind != _OPAQUE:
                continue
            if self._deleted_ancestor(path, include_self=True):
                if path not in explicit_dirs or self._deleted_ancestor(path):
                    raise SquashError(
                        f"Layer {layer + 1} marks deleted directory {path!r} opaque "
                        "without recreating it explicitly."
                    )
                continue
            self._drop_under(path)
            self.state.setdefault(name, _Record(_OPAQUE, name, path, template=info))
        for kind, name, path, info in members:
            if kind == _WHITEOUT and not self._deleted_ancestor(path):
                if path in self.state:
                    self._drop(path)
                self._drop_under(path)
                self.state.setdefault(name, _Record(_WHITEOUT, name, path, template=info))
        for kind, name, path, info in members:
            if kind == _ENTRY:
                self._apply_entry(layer, path, info)

    def _apply_entry(self, layer: int, path: str, info: tarfile.TarInfo) -> None:
        existing = self.state.get(path)
        if info.isdir() and existing is not None and existing.is_dir():
            existing.info, existing.source = info, layer
            return
        whiteout = _whiteout_key(path)
        had_whiteout = whiteout in self.state
        needs_opaque = info.isdir() and (had_whiteout or existing is not None)
        if existing is not None:
            replaced_dir = existing.is_dir()
            self._drop(path)
            if replaced_dir:
                self._drop_under(path)
        if had_whiteout:
            # A replacement entry hides the lower path itself; directories add an opaque marker.
            del self.state[whiteout]
        record = _Record(_ENTRY, path, path, info=info, source=layer)
        if info.islnk():
            record.link_target = self._hardlink_target(path, info)
        self.state[path] = record
        if needs_opaque:
            marker = _opaque_key(path)
            self.state.setdefault(marker, _Record(_OPAQUE, marker, path, template=info))

    def _hardlink_target(self, path: str, info: tarfile.TarInfo) -> _Record:
        target_path = _normalize(info.linkname, f"Hard link {path!r} target")
        target = self.state.get(target_path)
        if target is not None and target.kind == _ENTRY and target.info.islnk():
            target = target.link_target
        if target is None or target.kind != _ENTRY or not target.info.isreg():
            raise SquashError(
                f"Hard link {path!r} must target a regular file created by the squashed "
                f"layers; {info.linkname!r} is missing, a base-image path, or not a file."
            )
        return target

    def validate(self) -> None:
        for record in self.state.values():
            for directory in _required_dirs(record):
                parent = self.state.get(directory)
                if parent is not None and not parent.is_dir():
                    raise SquashError(
                        f"{record.name!r} is beneath {directory!r}, which the squashed "
                        "layers make a non-directory (for example a file or symlink)."
                    )
                if parent is None and _whiteout_key(directory) in self.state:
                    raise SquashError(
                        f"{record.name!r} implicitly recreates deleted directory "
                        f"{directory!r}; add an explicit directory entry."
                    )
            target = record.link_target
            if target is not None and self.state.get(target.path) is not target:
                raise SquashError(
                    f"Hard link {record.path!r} targets {target.path!r}, which a later "
                    "layer replaced or deleted; the link cannot be squashed exactly."
                )


def _required_dirs(record: _Record) -> List[str]:
    if record.kind == _OPAQUE:
        return _ancestors(record.path) + [record.path]
    return _ancestors(record.path)


def _output_info(record: _Record) -> tarfile.TarInfo:
    source = record.info or record.template
    info = tarfile.TarInfo(record.name)
    info.mode, info.uid, info.gid = source.mode, source.uid, source.gid
    info.uname, info.gname, info.mtime = source.uname, source.gname, source.mtime
    if record.kind != _ENTRY:
        info.type = tarfile.REGTYPE
        if record.kind == _OPAQUE and source.isdir():
            info.mode = 0o644
        return info
    info.type = source.type
    info.pax_headers = {
        key: value for key, value in source.pax_headers.items()
        if key.startswith(_XATTR_PREFIX) or key in _PRESERVED_TIME_KEYS
    }
    if source.isreg():
        info.type = tarfile.REGTYPE
        info.size = source.size
    elif source.issym():
        info.linkname = source.linkname
    elif source.islnk():
        info.linkname = record.link_target.path
    return info


def _predicted_size(records) -> int:
    """Size of the squashed tar computed from regenerated headers, without copying data."""
    total = 2 * TAR_BLOCK_SIZE
    for record in records:
        info = _output_info(record)
        total += len(info.tobuf(tarfile.PAX_FORMAT, tarfile.ENCODING, "surrogateescape"))
        total += _padded(info.size)
    return -(-total // tarfile.RECORDSIZE) * tarfile.RECORDSIZE


def _read_members(path: Path, layer: int, tar: tarfile.TarFile):
    members = []
    seen = set()
    for info in tar:
        where = f"Layer {layer + 1} entry {info.name!r}"
        _validate_member(info, where)
        kind, name, affected = _classify(info, where)
        if name in seen:
            raise SquashError(f"{where} appears more than once in {path.name}.")
        seen.add(name)
        members.append((kind, name, affected, info))
    if tar.pax_headers:
        raise SquashError(f"Layer {layer + 1} uses global PAX headers, which are not supported.")
    return members


class _CountingWriter:
    def __init__(self, target: Optional[BinaryIO]) -> None:
        self.target = target
        self.size = 0

    def write(self, data: bytes) -> int:
        if self.target is not None:
            self.target.write(data)
        self.size += len(data)
        return len(data)

    def tell(self) -> int:
        return self.size


def squash_layers(layer_paths: Sequence[Path], destination: Optional[BinaryIO]) -> SquashStats:
    """Write one uncompressed tar equivalent to applying ``layer_paths`` in order.

    ``destination`` may be ``None`` to measure the squashed size without writing it.
    """
    squasher = _Squasher()
    sources: List[tarfile.TarFile] = []
    try:
        for layer, path in enumerate(layer_paths):
            path = Path(path)
            squasher.stats.source_bytes += path.stat().st_size
            tar = tarfile.open(path, mode="r:")
            sources.append(tar)
            squasher.apply_layer(layer, _read_members(path, layer, tar))
        squasher.stats.source_layers = len(sources)
        squasher.validate()
        squasher.stats.predicted_bytes = _predicted_size(squasher.state.values())

        writer = _CountingWriter(destination)
        emitted = set()

        with tarfile.open(fileobj=writer, mode="w", format=tarfile.PAX_FORMAT) as output:
            def emit(record: _Record) -> None:
                if id(record) in emitted:
                    return
                for directory in _required_dirs(record):
                    parent = squasher.state.get(directory)
                    if parent is not None:
                        emit(parent)
                    marker = squasher.state.get(_opaque_key(directory))
                    if marker is not None and marker is not record:
                        emit(marker)
                if record.link_target is not None:
                    emit(record.link_target)
                info = _output_info(record)
                if record.kind == _ENTRY and record.info.isreg():
                    with sources[record.source].extractfile(record.info) as data:
                        output.addfile(info, data)
                else:
                    output.addfile(info)
                emitted.add(id(record))
                squasher.stats.output_members += 1

            for record in list(squasher.state.values()):
                emit(record)
        squasher.stats.output_bytes = writer.size
        return squasher.stats
    finally:
        for tar in sources:
            tar.close()
