"""Remote and shared layer cache backends (import/export) with digest verification."""
import json
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .blob_store import (
    BlobIntegrityError,
    blob_matches,
    create_temporary_file,
    digest_filename,
    hash_file,
    publish_content_addressed,
)
from .cache import LayerCache
from .registry_client import RegistryClient, parse_image_reference


logger = logging.getLogger(__name__)

INDEX_ANNOTATION = "org.pycontainer.cache.index"
CACHE_CONFIG_MEDIA_TYPE = "application/vnd.pycontainer.cache.config.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
LOCAL_INDEX_NAME = "index.json"
_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

# key -> (digest, size)
CacheIndex = Dict[str, Tuple[str, int]]


class RemoteCacheError(ValueError):
    """Raised for invalid remote cache configuration or unusable backends."""


class CacheBackend(ABC):
    """Pluggable storage for a shared cache.

    Implement this interface to add another store (for example S3, Azure Blob
    or GCS) without changing the builder. Implementations must never return
    unverified content: ``fetch_blob`` publishes only blobs matching the digest
    and size it was asked for.
    """

    @abstractmethod
    def read_index(self) -> CacheIndex:
        """Return the stored key index, or an empty index if none exists."""

    @abstractmethod
    def write_index(self, index: CacheIndex) -> None:
        """Replace the stored key index (blobs are already uploaded)."""

    @abstractmethod
    def has_blob(self, digest: str) -> bool:
        """Return whether a blob is already stored."""

    @abstractmethod
    def put_blob(self, digest: str, path: Path) -> None:
        """Store a blob from a local file."""

    @abstractmethod
    def fetch_blob(self, digest: str, size: int, dest: Path) -> bool:
        """Download a blob to ``dest``; return False if missing or corrupt."""


def _parse_index(raw: object) -> CacheIndex:
    index: CacheIndex = {}
    if not isinstance(raw, dict):
        return index
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        digest, size = value.get("digest"), value.get("size")
        try:
            digest_filename(digest)
        except ValueError:
            continue
        if type(size) is not int or size < 0:
            continue
        index[key] = (digest, size)
    return index


def _serialize_index(index: CacheIndex) -> str:
    return json.dumps(
        {k: {"digest": d, "size": s} for k, (d, s) in sorted(index.items())},
        sort_keys=True,
        separators=(",", ":"),
    )


class LocalDirectoryBackend(CacheBackend):
    """Cache in a directory (for example a CI-restored or network-mounted path)."""

    def __init__(self, directory: Path):
        self.directory = Path(directory).expanduser()
        self.blobs_dir = self.directory / "blobs" / "sha256"
        self.index_file = self.directory / LOCAL_INDEX_NAME

    def read_index(self) -> CacheIndex:
        try:
            return _parse_index(json.loads(self.index_file.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            logger.warning("Ignoring malformed cache index %s: %s", self.index_file, error)
            return {}

    def write_index(self, index: CacheIndex) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".index-", suffix=".tmp", dir=self.directory
        )
        temp = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(_serialize_index(index))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, self.index_file)
        finally:
            temp.unlink(missing_ok=True)

    def _blob_path(self, digest: str) -> Path:
        return self.blobs_dir / digest_filename(digest)

    def has_blob(self, digest: str) -> bool:
        path = self._blob_path(digest)
        return path.is_file() and hash_file(path)[0] == digest

    def put_blob(self, digest: str, path: Path) -> None:
        actual, size = hash_file(path)
        if actual != digest:
            raise BlobIntegrityError(f"Cache blob hashes to {actual}, not {digest}.")
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        destination = self._blob_path(digest)
        if blob_matches(destination, digest, size):
            return
        staged = create_temporary_file(self.blobs_dir, destination.name)
        try:
            _copy(path, staged)
            destination.unlink(missing_ok=True)  # replace a corrupt blob
            publish_content_addressed(staged, destination, digest, size)
        finally:
            staged.unlink(missing_ok=True)

    def fetch_blob(self, digest: str, size: int, dest: Path) -> bool:
        source = self._blob_path(digest)
        if not blob_matches(source, digest, size):
            if source.exists():
                logger.warning("Discarding corrupt cache blob %s", source)
            return False
        _copy(source, dest)
        return blob_matches(dest, digest, size)


def _copy(source: Path, destination: Path) -> None:
    with Path(source).open("rb") as reader, Path(destination).open("wb") as writer:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())


class RegistryBackend(CacheBackend):
    """Cache in an OCI registry repository.

    The index lives in the ``org.pycontainer.cache.index`` annotation of a
    manifest at the cache reference tag; its layers are the cached blobs, so
    registry garbage collection keeps them reachable.
    """

    def __init__(self, ref: str, client: Optional[RegistryClient] = None):
        try:
            self.registry, self.repository, self.tag = parse_image_reference(ref)
        except ValueError as error:
            raise RemoteCacheError(f"Invalid cache ref {ref!r}: {error}") from error
        self._client = client
        self._sizes: Dict[str, int] = {}

    @property
    def client(self) -> RegistryClient:
        if self._client is None:
            from .auth import get_registry_auth

            auth = get_registry_auth(self.registry)
            self._client = RegistryClient(
                self.registry,
                self.repository,
                auth_token=auth.bearer_token,
                username=auth.username,
                password=auth.password,
            )
        return self._client

    def read_index(self) -> CacheIndex:
        from .registry_client import RegistryRequestError

        try:
            manifest, _ = self.client.pull_manifest(self.tag)
        except RegistryRequestError as error:
            logger.debug("No remote cache at %s/%s:%s: %s", self.registry, self.repository, self.tag, error)
            return {}
        annotations = manifest.get("annotations")
        raw = annotations.get(INDEX_ANNOTATION) if isinstance(annotations, dict) else None
        try:
            return _parse_index(json.loads(raw)) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            logger.warning("Ignoring malformed remote cache index annotation")
            return {}

    def has_blob(self, digest: str) -> bool:
        return self.client.blob_exists(digest)

    def put_blob(self, digest: str, path: Path) -> None:
        self.client.push_blob(digest, Path(path), check_exists=True)
        self._sizes[digest] = Path(path).stat().st_size

    def write_index(self, index: CacheIndex) -> None:
        config = b"{}"
        import hashlib

        config_digest = f"sha256:{hashlib.sha256(config).hexdigest()}"
        with tempfile.TemporaryDirectory() as scratch:
            config_path = Path(scratch) / "config.json"
            config_path.write_bytes(config)
            self.client.push_blob(config_digest, config_path, check_exists=True)
        layers = []
        for digest, size in sorted({v for v in index.values()}):
            layers.append({"mediaType": LAYER_MEDIA_TYPE, "digest": digest, "size": size})
        manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST_MEDIA_TYPE,
            "config": {
                "mediaType": CACHE_CONFIG_MEDIA_TYPE,
                "digest": config_digest,
                "size": len(config),
            },
            "layers": layers,
            "annotations": {INDEX_ANNOTATION: _serialize_index(index)},
        }
        self.client.push_manifest(
            self.tag, json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        )

    def fetch_blob(self, digest: str, size: int, dest: Path) -> bool:
        from .registry_client import RegistryRequestError

        dest = Path(dest)
        dest.unlink(missing_ok=True)  # pull_blob refuses to overwrite
        try:
            self.client.pull_blob(digest, dest, size)
        except (RegistryRequestError, BlobIntegrityError) as error:
            logger.warning("Discarding remote cache blob %s: %s", digest, error)
            dest.unlink(missing_ok=True)
            return False
        return blob_matches(dest, digest, size)


def parse_cache_spec(spec: str, *, export: bool) -> Tuple[CacheBackend, str]:
    """Parse ``type=registry,ref=...`` or ``type=local,src=|dest=...`` specs.

    Returns the backend and its optional scope (``scope=name``).
    """
    if not isinstance(spec, str) or not spec.strip():
        raise RemoteCacheError("Cache spec must be a non-empty string.")
    options: Dict[str, str] = {}
    for part in spec.split(","):
        key, separator, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key or not value or key in options:
            raise RemoteCacheError(
                f"Invalid cache spec {spec!r}: expected comma-separated key=value pairs."
            )
        options[key] = value

    cache_type = options.pop("type", None)
    scope = options.pop("scope", "")
    if scope and not _SCOPE_PATTERN.fullmatch(scope):
        raise RemoteCacheError(f"Invalid cache scope {scope!r}.")

    direction = "dest" if export else "src"
    if cache_type == "registry":
        allowed, ref = {"ref"}, options.get("ref")
        if not ref:
            raise RemoteCacheError("type=registry requires ref=<registry/repository[:tag]>.")
        backend: CacheBackend = RegistryBackend(ref)
    elif cache_type == "local":
        allowed, path = {direction}, options.get(direction)
        if not path:
            raise RemoteCacheError(f"type=local requires {direction}=<directory> for this option.")
        backend = LocalDirectoryBackend(Path(path))
    else:
        raise RemoteCacheError(
            f"Unsupported cache type {cache_type!r}; supported types: registry, local."
        )
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise RemoteCacheError(f"Unknown cache spec option(s): {', '.join(unknown)}.")
    return backend, scope


def files_cache_key(files, workdir: str, reproducible: bool) -> str:
    """Content key for a file set, identical to the local layer cache key."""
    return LayerCache(None)._compute_files_digest(files, workdir, reproducible)


class RemoteCache:
    """Imports verified layers from ``cache_from`` and exports to ``cache_to``."""

    def __init__(self, sources: List[Tuple[CacheBackend, str]], destinations: List[Tuple[CacheBackend, str]]):
        self.sources = sources
        self.destinations = destinations
        self._indexes: Optional[List[CacheIndex]] = None
        self._recorded: List[Tuple[str, str, int, Path]] = []

    @classmethod
    def from_specs(cls, cache_from: Optional[List[str]], cache_to: Optional[List[str]]) -> Optional["RemoteCache"]:
        sources = [parse_cache_spec(s, export=False) for s in cache_from or []]
        destinations = [parse_cache_spec(s, export=True) for s in cache_to or []]
        return cls(sources, destinations) if sources or destinations else None

    @staticmethod
    def _scoped(scope: str, key: str) -> str:
        return f"{scope}/{key}" if scope else key

    def _load(self) -> List[CacheIndex]:
        if self._indexes is None:
            self._indexes = []
            for backend, _ in self.sources:
                try:
                    self._indexes.append(backend.read_index())
                except Exception as error:  # remote cache is best-effort
                    logger.warning("Cache import skipped: %s", error)
                    self._indexes.append({})
        return self._indexes

    def import_layer(self, key: str, scratch_dir: Path) -> Optional[Tuple[str, int, Path]]:
        """Return a verified ``(digest, size, path)`` for ``key`` or None on a miss."""
        for (backend, scope), index in zip(self.sources, self._load()):
            entry = index.get(self._scoped(scope, key))
            if entry is None:
                continue
            digest, size = entry
            staged = create_temporary_file(scratch_dir, "remote-cache")
            try:
                if backend.fetch_blob(digest, size, staged) and blob_matches(staged, digest, size):
                    return digest, size, staged
            except Exception as error:
                logger.warning("Discarding remote cache entry %s: %s", digest, error)
            staged.unlink(missing_ok=True)
        return None

    def record(self, key: str, digest: str, size: int, path: Path) -> None:
        """Register a layer used by this build for export."""
        self._recorded.append((key, digest, size, Path(path)))

    def export(self) -> None:
        if not self._recorded:
            return
        for backend, scope in self.destinations:
            try:
                index = backend.read_index()
                for key, digest, size, path in self._recorded:
                    if not blob_matches(path, digest, size):
                        logger.warning("Not exporting unverified layer %s", digest)
                        continue
                    if not backend.has_blob(digest):
                        backend.put_blob(digest, path)
                    index[self._scoped(scope, key)] = (digest, size)
                backend.write_index(index)
            except Exception as error:  # a cache outage must not fail the build
                logger.warning("Cache export failed: %s", error)
