"""Registry-backed remote cache for the application layer.

A cache record is a small OCI artifact manifest tagged ``app-<key>`` in a
dedicated repository. Its config blob binds the cache key to exactly one layer
descriptor; the layer is the application tar itself. Digest verification proves
that downloaded bytes are intact, not that a trusted writer produced them:
anyone who can push to the cache repository is a trusted writer.
"""
import hashlib
import json
import logging
import platform
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .auth import RegistryAuth, get_registry_auth
from .blob_store import BlobIntegrityError, digest_filename
from .registry_client import (
    RegistryClient,
    RegistryManifestFormatError,
    RegistryNotFoundError,
    RegistryRequestError,
    parse_image_reference,
)


logger = logging.getLogger(__name__)

REMOTE_CACHE_SCHEMA = 1
APP_LAYER_KIND = "app-layer"
# Bump when the application tar writer changes the bytes it produces.
APP_LAYER_FORMAT = "app-tar-v1"
APP_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
CACHE_ARTIFACT_TYPE = "application/vnd.pycontainer.cache.v1"
CACHE_CONFIG_MEDIA_TYPE = "application/vnd.pycontainer.cache.config.v1+json"
MAX_RECORD_MANIFEST_BYTES = 64 * 1024
MAX_RECORD_CONFIG_BYTES = 16 * 1024
MAX_CACHED_LAYER_BYTES = 4 * 1024 * 1024 * 1024
SUPPORTED_CACHE_TYPES = ("registry",)
_SPEC_KEYS = {"type", "ref"}
_DESCRIPTOR_KEYS = {"mediaType", "digest", "size"}
_MANIFEST_KEYS = {"schemaVersion", "mediaType", "artifactType", "config", "layers"}


class RemoteCacheError(RuntimeError):
    """Raised when a remote cache export cannot be completed."""


class RemoteCacheRecordError(RemoteCacheError):
    """Raised when a remote cache record is not a valid record for the key."""


@dataclass(frozen=True)
class RegistryCacheRef:
    registry: str
    repository: str

    def __str__(self) -> str:
        return f"{self.registry}/{self.repository}"


def parse_cache_spec(spec: str, option: str = "cache") -> RegistryCacheRef:
    """Parse ``type=registry,ref=REGISTRY/REPOSITORY``."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"{option} must be a non-empty cache specification.")
    values: Dict[str, str] = {}
    for item in spec.split(","):
        key, separator, value = item.partition("=")
        key = key.strip()
        if not separator or not key or not value:
            raise ValueError(
                f"{option} entry {item!r} must use key=value "
                "(for example type=registry,ref=ghcr.io/org/app-cache)."
            )
        if key not in _SPEC_KEYS:
            raise ValueError(
                f"{option} has unsupported key {key!r}; supported keys: type, ref."
            )
        if key in values:
            raise ValueError(f"{option} sets {key!r} more than once.")
        values[key] = value

    cache_type = values.get("type")
    if cache_type not in SUPPORTED_CACHE_TYPES:
        raise ValueError(
            f"{option} type {cache_type!r} is not supported; only type=registry is "
            "implemented. Object-store backends (S3, Azure Blob, GCS) are tracked "
            "in spboyer/pycontainer-build#61 and #34."
        )
    ref = values.get("ref")
    if not ref:
        raise ValueError(f"{option} requires ref=REGISTRY/REPOSITORY.")
    name = ref.split("@", 1)[0]
    if "@" in ref or ":" in name.rsplit("/", 1)[-1]:
        raise ValueError(
            f"{option} ref {ref!r} must name a repository without a tag or digest; "
            "cache records are tagged by key."
        )
    registry, repository, _ = parse_image_reference(ref)
    return RegistryCacheRef(registry, repository)


def app_layer_cache_key(files_identity: str) -> str:
    """Return the versioned remote key for an application layer."""
    material = {
        "schema": REMOTE_CACHE_SCHEMA,
        "kind": APP_LAYER_KIND,
        "layer_format": APP_LAYER_FORMAT,
        "media_type": APP_LAYER_MEDIA_TYPE,
        "tar_format": tarfile.DEFAULT_FORMAT,
        "tar_encoding": tarfile.ENCODING,
        "python_implementation": sys.implementation.name,
        "python_version": platform.python_version(),
        "files": files_identity,
    }
    return hashlib.sha256(_canonical(material)).hexdigest()


def cache_tag(key: str) -> str:
    return f"app-{key}"


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _descriptor(media_type: str, digest: str, size: int) -> Dict:
    return {"mediaType": media_type, "digest": digest, "size": size}


def _record_config(key: str, layer: Dict) -> Dict:
    return {
        "schemaVersion": REMOTE_CACHE_SCHEMA,
        "kind": APP_LAYER_KIND,
        "key": key,
        "layer": layer,
    }


def _record_manifest(config: Dict, layer: Dict) -> Dict:
    return {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "artifactType": CACHE_ARTIFACT_TYPE,
        "config": config,
        "layers": [layer],
    }


def build_record(key: str, digest: str, size: int) -> Tuple[bytes, bytes]:
    """Return deterministic (config, manifest) bytes for a cache record."""
    layer = _descriptor(APP_LAYER_MEDIA_TYPE, digest, size)
    config_bytes = _canonical(_record_config(key, layer))
    config = _descriptor(
        CACHE_CONFIG_MEDIA_TYPE,
        f"sha256:{hashlib.sha256(config_bytes).hexdigest()}",
        len(config_bytes),
    )
    return config_bytes, _canonical(_record_manifest(config, layer))


def _validated_descriptor(value, media_type: str, max_size: int, name: str) -> Dict:
    if not isinstance(value, dict) or set(value) != _DESCRIPTOR_KEYS:
        raise RemoteCacheRecordError(f"{name} descriptor has unexpected fields.")
    if value["mediaType"] != media_type:
        raise RemoteCacheRecordError(
            f"{name} media type is {value['mediaType']!r}, expected {media_type!r}."
        )
    try:
        digest_filename(value["digest"])
    except ValueError as error:
        raise RemoteCacheRecordError(f"{name} digest is invalid.") from error
    size = value["size"]
    if type(size) is not int or size < 0 or size > max_size:
        raise RemoteCacheRecordError(
            f"{name} size must be an integer between 0 and {max_size} bytes."
        )
    return value


def validate_manifest(manifest: Dict) -> Tuple[Dict, Dict]:
    """Return (config, layer) descriptors from a strictly shaped record."""
    if set(manifest) != _MANIFEST_KEYS:
        raise RemoteCacheRecordError("Cache record manifest has unexpected fields.")
    if (
        manifest["schemaVersion"] != 2
        or manifest["mediaType"] != OCI_MANIFEST_MEDIA_TYPE
        or manifest["artifactType"] != CACHE_ARTIFACT_TYPE
    ):
        raise RemoteCacheRecordError("Cache record manifest has the wrong type.")
    layers = manifest["layers"]
    if not isinstance(layers, list) or len(layers) != 1:
        raise RemoteCacheRecordError("Cache record must reference exactly one layer.")
    config = _validated_descriptor(
        manifest["config"], CACHE_CONFIG_MEDIA_TYPE, MAX_RECORD_CONFIG_BYTES, "Config"
    )
    layer = _validated_descriptor(
        layers[0], APP_LAYER_MEDIA_TYPE, MAX_CACHED_LAYER_BYTES, "Layer"
    )
    return config, layer


class RegistryLayerCache:
    """Import or export application layers through one cache repository.

    Credentials are resolved for the cache registry itself; credentials passed
    for image push are never used.
    """

    def __init__(
        self,
        ref: RegistryCacheRef,
        client: Optional[RegistryClient] = None,
        *,
        client_factory: Optional[Callable[[str, str, RegistryAuth], RegistryClient]] = None,
        log_redact: Optional[Callable[[str], str]] = None,
    ):
        self.ref = ref
        self._client = client
        self._client_factory = client_factory
        self._log_redact = log_redact
        self._redact = log_redact if log_redact is not None else str

    @property
    def client(self) -> RegistryClient:
        if self._client is None:
            log_options = (
                {"log_redact": self._log_redact}
                if self._log_redact is not None else {}
            )
            auth = get_registry_auth(
                self.ref.registry, include_generic_environment=False, **log_options
            )
            if self._client_factory is not None:
                self._client = self._client_factory(
                    self.ref.registry, self.ref.repository, auth
                )
            else:
                self._client = RegistryClient(
                    self.ref.registry,
                    self.ref.repository,
                    auth_token=auth.bearer_token,
                    username=auth.username,
                    password=auth.password,
                    **log_options,
                )
        return self._client

    def fetch(self, key: str, layers_dir: Path) -> Optional[Tuple[str, Path, int]]:
        """Return (digest, path, size) for a verified hit, otherwise None."""
        tag = cache_tag(key)
        try:
            manifest, _ = self.client.pull_manifest(
                tag, max_size=MAX_RECORD_MANIFEST_BYTES
            )
            config, layer = validate_manifest(manifest)
            record = self._pull_config(config)
            if record != _record_config(key, layer):
                raise RemoteCacheRecordError(
                    "Cache record config does not bind this key to the layer."
                )
            digest = layer["digest"]
            destination = Path(layers_dir) / digest_filename(digest)
            self.client.pull_blob(digest, destination, layer["size"])
        except RegistryNotFoundError:
            logger.debug("%s", self._redact(f"Remote cache miss: {self.ref} has no record {tag}"))
            return None
        except RemoteCacheRecordError as error:
            logger.warning(
                "%s", self._redact(f"Remote cache miss: rejected record {tag} in {self.ref}: {error}")
            )
            return None
        except (
            RegistryRequestError, BlobIntegrityError, ValueError, OSError, RecursionError
        ) as error:
            logger.warning(
                "%s",
                self._redact(
                    f"Remote cache miss: could not import {tag} from {self.ref} "
                    f"({type(error).__name__}: {error})"
                ),
            )
            return None
        logger.info("%s", self._redact(f"Remote cache hit: {digest} from {self.ref}"))
        return digest, destination, layer["size"]

    def _pull_config(self, descriptor: Dict) -> Dict:
        with tempfile.TemporaryDirectory(prefix="pycontainer-cache-") as directory:
            path = Path(directory) / "config"
            self.client.pull_blob(descriptor["digest"], path, descriptor["size"])
            try:
                record = json.loads(path.read_bytes())
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
                raise RemoteCacheRecordError("Cache record config is not JSON.") from error
        if not isinstance(record, dict):
            raise RemoteCacheRecordError("Cache record config is not an object.")
        return record

    def store(self, key: str, digest: str, layer_path: Path, size: int) -> bool:
        """Publish layer and config blobs, then the record. Return False if current."""
        if size > MAX_CACHED_LAYER_BYTES:
            raise RemoteCacheError(
                self._redact(
                    f"Application layer is {size} bytes; remote cache records are "
                    f"limited to {MAX_CACHED_LAYER_BYTES} bytes."
                )
            )
        tag = cache_tag(key)
        config_bytes, manifest_bytes = build_record(key, digest, size)
        manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
        config_digest = f"sha256:{hashlib.sha256(config_bytes).hexdigest()}"
        try:
            current = self._current_record_digest(tag) == manifest_digest
            self.client.push_blob(digest, Path(layer_path))
            with tempfile.TemporaryDirectory(prefix="pycontainer-cache-") as directory:
                config_path = Path(directory) / "config"
                config_path.write_bytes(config_bytes)
                self.client.push_blob(config_digest, config_path)
            if current:
                logger.info("%s", self._redact(f"Remote cache record {tag} in {self.ref} is current"))
                return False
            # The record is published last so readers never see missing blobs.
            self.client.push_manifest(tag, manifest_bytes)
        except (RegistryRequestError, BlobIntegrityError, OSError, RecursionError) as error:
            message = self._redact(
                f"Failed to export remote cache record {tag} to {self.ref}: {error}"
            )
            if self._log_redact is None:
                raise RemoteCacheError(message) from error
        else:
            logger.info("%s", self._redact(f"Exported remote cache record {tag} to {self.ref}"))
            return True
        # Raise outside the handler so tracebacks cannot retain an unredacted cause.
        raise RemoteCacheError(message)

    def _current_record_digest(self, tag: str) -> Optional[str]:
        try:
            _, digest = self.client.pull_manifest(tag, max_size=MAX_RECORD_MANIFEST_BYTES)
        except RegistryNotFoundError:
            return None
        except RegistryManifestFormatError as error:
            logger.debug("%s", self._redact(f"Replacing unreadable remote cache record {tag}: {error}"))
            return None
        return digest
