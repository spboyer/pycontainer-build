"""Content-addressed application layer cache."""
import hashlib
import io
import json
import logging
import os
import shutil
import stat
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .fs_utils import file_lock


logger = logging.getLogger(__name__)
_HASH_CHUNK_SIZE = 1024 * 1024
_CACHE_KEY_VERSION = 3


def app_layer_arcname(workdir: str, relative_path: Path) -> str:
    return f"{workdir.lstrip('/')}/{Path(relative_path).as_posix()}"


def ordered_app_files(
    files: List[Tuple[Path, Path]], reproducible: bool
) -> List[Tuple[Path, Path]]:
    if reproducible:
        return sorted(files, key=lambda item: Path(item[1]).as_posix())
    return list(files)


def emitted_hardlinks(
    ordered_files: List[Tuple[Path, Path]], members: List[tarfile.TarInfo]
) -> Dict[str, str]:
    """Map context-relative hardlink members to their context-relative targets."""
    if len(ordered_files) != len(members):
        raise RuntimeError("Application layer members do not match its source files")
    relative_by_arcname = {
        member.name: Path(relative).as_posix()
        for (_, relative), member in zip(ordered_files, members)
    }
    return {
        Path(relative).as_posix(): relative_by_arcname[member.linkname]
        for (_, relative), member in zip(ordered_files, members)
        if member.islnk()
    }


class CacheIndexError(RuntimeError):
    """Raised when the persisted cache index cannot be trusted."""

    def __init__(self, message: str, index_file: Optional[Path] = None):
        if index_file is not None:
            message = (
                f"{message}. Delete {index_file} to discard the cached entries "
                "(their layers are rebuilt on the next build), or rerun with --no-cache."
            )
        super().__init__(message)


class CacheIntegrityError(RuntimeError):
    """Raised when a layer does not match its content-addressed digest."""


@dataclass
class CacheEntry:
    digest: str
    size: int
    created: float
    last_used: float
    source_files: List[str]

    def touch(self):
        self.last_used = time.time()


class LayerCache:
    def __init__(self, cache_dir: Optional[Path] = None, max_size_mb: int = 5000):
        if max_size_mb < 0:
            raise ValueError("max_size_mb must be non-negative")

        self.enabled = cache_dir is not None
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir is not None else None
        self.max_size_bytes = int(max_size_mb * 1024 * 1024)
        if not self.enabled:
            self.blobs_dir = None
            self.index_file = None
            self.lock_file = None
            return

        self.blobs_dir = self.cache_dir / "blobs" / "sha256"
        self.index_file = self.cache_dir / "index.json"
        self.lock_file = self.cache_dir / "index.lock"
        self._ensure_structure()

    def _ensure_structure(self):
        self.blobs_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _digest_hex(digest: str) -> str:
        algorithm, separator, value = digest.partition(":")
        if (
            not separator
            or algorithm != "sha256"
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"Invalid layer digest: {digest!r}")
        return value

    @staticmethod
    def _hash_file(path: Path) -> Tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while True:
                chunk = source.read(_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                size += len(chunk)
        return f"sha256:{hasher.hexdigest()}", size

    @staticmethod
    def _relative_path(path: Path) -> str:
        return Path(path).as_posix()

    @staticmethod
    def _probe_hardlinks(
        ordered_files: List[Tuple[Path, Path]], workdir: str
    ) -> Dict[str, str]:
        """Predict the hardlinks tarfile emits for these files, in emission order."""
        candidates = []
        for abs_path, rel_path in ordered_files:
            metadata = Path(abs_path).lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                candidates.append((abs_path, rel_path))
        if len(candidates) < 2:
            return {}
        with tarfile.open(fileobj=io.BytesIO(), mode="w") as probe:
            members = [
                probe.gettarinfo(abs_path, arcname=app_layer_arcname(workdir, rel_path))
                for abs_path, rel_path in candidates
            ]
        return emitted_hardlinks(candidates, members)

    def _compute_files_digest(
        self,
        files: List[Tuple[Path, Path]],
        workdir: str = "/app",
        reproducible: bool = True,
        hardlinks: Optional[Dict[str, str]] = None,
        *,
        namespace: Optional[str] = None,
    ) -> str:
        file_records = []
        ordered_files = ordered_app_files(files, reproducible)
        if hardlinks is None:
            hardlinks = self._probe_hardlinks(ordered_files, workdir)
        for abs_path, rel_path in ordered_files:
            abs_path = Path(abs_path)
            rel_name = self._relative_path(rel_path)
            metadata = abs_path.lstat()
            record = {
                "path": rel_name,
                "mode": stat.S_IMODE(metadata.st_mode),
                "size": metadata.st_size,
                "hardlink": hardlinks.get(rel_name),
            }

            if stat.S_ISLNK(metadata.st_mode):
                record["kind"] = "symlink"
                record["content"] = hashlib.sha256(
                    os.fsencode(os.readlink(abs_path))
                ).hexdigest()
            elif stat.S_ISREG(metadata.st_mode):
                record["kind"] = "file"
                file_digest, actual_size = self._hash_file(abs_path)
                if actual_size != metadata.st_size:
                    raise RuntimeError(f"File changed while hashing: {abs_path}")
                record["content"] = file_digest
            else:
                raise ValueError(f"Unsupported cache source file type: {abs_path}")

            if not reproducible:
                record["mtime_ns"] = metadata.st_mtime_ns
                record["uid"] = getattr(metadata, "st_uid", 0)
                record["gid"] = getattr(metadata, "st_gid", 0)
            file_records.append(record)

        identity = {
            "version": _CACHE_KEY_VERSION,
            "workdir": workdir,
            "reproducible": bool(reproducible),
            "files": file_records,
        }
        if namespace is not None:
            identity["namespace"] = namespace
        serialized = json.dumps(
            identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return f"v{_CACHE_KEY_VERSION}:sha256:{hashlib.sha256(serialized).hexdigest()}"

    @contextmanager
    def _index_lock(self):
        with file_lock(self.lock_file):
            yield

    def _load_index(self) -> Dict[str, CacheEntry]:
        if not self.index_file.exists():
            return {}
        try:
            data = json.loads(self.index_file.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CacheIndexError(
                f"Malformed cache index {self.index_file}: {error}", self.index_file
            ) from error
        if not isinstance(data, dict):
            raise CacheIndexError(
                f"Malformed cache index {self.index_file}: expected an object", self.index_file
            )

        entries = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise CacheIndexError(
                    f"Malformed cache index entry in {self.index_file}", self.index_file
                )
            try:
                entry = CacheEntry(**value)
                self._digest_hex(entry.digest)
            except (TypeError, ValueError) as error:
                raise CacheIndexError(
                    f"Malformed cache index entry {key!r} in {self.index_file}: {error}",
                    self.index_file,
                ) from error
            if (
                isinstance(entry.size, bool)
                or not isinstance(entry.size, int)
                or entry.size < 0
                or not isinstance(entry.created, (int, float))
                or not isinstance(entry.last_used, (int, float))
                or not isinstance(entry.source_files, list)
                or any(not isinstance(path, str) for path in entry.source_files)
            ):
                raise CacheIndexError(
                    f"Malformed cache index entry {key!r} in {self.index_file}", self.index_file
                )
            entries[key] = entry

        self._unique_blob_sizes(entries, self.index_file)
        return entries

    def _save_index(self, entries: Dict[str, CacheEntry]):
        data = {key: asdict(entry) for key, entry in entries.items()}
        serialized = json.dumps(data, indent=2, sort_keys=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".index-", suffix=".tmp", dir=self.cache_dir
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                destination.write(serialized)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temp_path, self.index_file)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _unique_blob_sizes(
        entries: Dict[str, CacheEntry], index_file: Optional[Path] = None
    ) -> Dict[str, int]:
        sizes = {}
        for entry in entries.values():
            previous_size = sizes.setdefault(entry.digest, entry.size)
            if previous_size != entry.size:
                raise CacheIndexError(
                    f"Cache index has conflicting sizes for shared blob {entry.digest}",
                    index_file,
                )
        return sizes

    def _blob_path(self, digest: str) -> Path:
        return self.blobs_dir / self._digest_hex(digest)

    def _blob_matches(self, path: Path, digest: str, size: int) -> bool:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
            return False
        actual_digest, actual_size = self._hash_file(path)
        return actual_size == size and actual_digest == digest

    def _publish_blob(self, source: Path, destination: Path, digest: str, size: int):
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent
        )
        temp_path = Path(temp_name)
        try:
            with source.open("rb") as source_file, os.fdopen(descriptor, "wb") as target:
                shutil.copyfileobj(source_file, target, _HASH_CHUNK_SIZE)
                target.flush()
                os.fsync(target.fileno())
            if not self._blob_matches(temp_path, digest, size):
                raise CacheIntegrityError(
                    f"Copied layer does not match {digest} ({size} bytes)"
                )
            os.replace(temp_path, destination)
        finally:
            temp_path.unlink(missing_ok=True)

    def get_layer(
        self,
        files: List[Tuple[Path, Path]],
        *,
        workdir: str = "/app",
        reproducible: bool = True,
        namespace: Optional[str] = None,
    ) -> Optional[Tuple[str, Path]]:
        if not self.enabled:
            logger.debug("Layer cache miss: caching is disabled")
            return None

        files_digest = self._compute_files_digest(
            files, workdir, reproducible, namespace=namespace
        )
        with self._index_lock():
            entries = self._load_index()
            entry = entries.get(files_digest)
            if entry is None:
                logger.debug("Layer cache miss: no entry for %s", files_digest)
                return None

            blob_path = self._blob_path(entry.digest)
            if not blob_path.is_file() or blob_path.is_symlink():
                self._remove_digest_entries(entries, entry.digest)
                self._save_index(entries)
                logger.debug("Layer cache miss: blob %s is missing", entry.digest)
                return None
            if not self._blob_matches(blob_path, entry.digest, entry.size):
                self._remove_digest_entries(entries, entry.digest)
                blob_path.unlink(missing_ok=True)
                self._save_index(entries)
                logger.warning("Layer cache miss: discarded corrupt blob %s", entry.digest)
                return None

            entry.touch()
            self._save_index(entries)
            logger.debug("Layer cache hit: %s", entry.digest)
            return entry.digest, blob_path

    def store_layer(
        self,
        files: List[Tuple[Path, Path]],
        digest: str,
        layer_path: Path,
        *,
        workdir: str = "/app",
        reproducible: bool = True,
        namespace: Optional[str] = None,
        record_source_files: bool = True,
        hardlinks: Optional[Dict[str, str]] = None,
    ) -> Path:
        layer_path = Path(layer_path)
        if not self.enabled:
            return layer_path

        self._digest_hex(digest)
        actual_digest, size = self._hash_file(layer_path)
        if actual_digest != digest:
            raise CacheIntegrityError(
                f"Layer file {layer_path} hashes to {actual_digest}, not {digest}"
            )
        files_digest = self._compute_files_digest(
            files, workdir, reproducible, hardlinks, namespace=namespace
        )
        blob_path = self._blob_path(digest)

        with self._index_lock():
            entries = self._load_index()
            if not self._blob_matches(blob_path, digest, size):
                self._publish_blob(layer_path, blob_path, digest, size)

            entries[files_digest] = CacheEntry(
                digest=digest,
                size=size,
                created=time.time(),
                last_used=time.time(),
                source_files=(
                    [self._relative_path(relative) for _, relative in files]
                    if record_source_files else []
                ),
            )
            self._evict_if_needed(entries)
            self._save_index(entries)
        return blob_path

    def copy_verified_blob(self, source: Path, destination: Path, digest: str) -> Path:
        source = Path(source)
        destination = Path(destination)
        self._digest_hex(digest)
        size = source.stat().st_size
        if not self._blob_matches(destination, digest, size):
            self._publish_blob(source, destination, digest, size)
        return destination

    @staticmethod
    def _remove_digest_entries(entries: Dict[str, CacheEntry], digest: str):
        for key in [key for key, entry in entries.items() if entry.digest == digest]:
            del entries[key]

    def _evict_if_needed(self, entries: Dict[str, CacheEntry]):
        blob_sizes = self._unique_blob_sizes(entries)
        total_size = sum(blob_sizes.values())
        if total_size <= self.max_size_bytes:
            return

        target_size = int(self.max_size_bytes * 0.8)
        for key, entry in sorted(entries.items(), key=lambda item: item[1].last_used):
            if total_size <= target_size:
                break
            del entries[key]
            if any(other.digest == entry.digest for other in entries.values()):
                continue
            self._blob_path(entry.digest).unlink(missing_ok=True)
            total_size -= entry.size

    def clear(self):
        if not self.enabled:
            return
        with self._index_lock():
            shutil.rmtree(self.blobs_dir)
            self._ensure_structure()
            self.index_file.unlink(missing_ok=True)

    def get_stats(self) -> Dict[str, float]:
        if not self.enabled:
            return {"entries": 0, "total_size_mb": 0, "max_size_mb": 0, "usage_percent": 0}
        with self._index_lock():
            entries = self._load_index()
        total_size = sum(self._unique_blob_sizes(entries).values())
        return {
            "entries": len(entries),
            "total_size_mb": total_size / (1024 * 1024),
            "max_size_mb": self.max_size_bytes / (1024 * 1024),
            "usage_percent": (total_size / self.max_size_bytes * 100)
            if self.max_size_bytes > 0
            else 0,
        }
