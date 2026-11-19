"""Helpers for streaming and atomically publishing content-addressed blobs."""
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Tuple


HASH_CHUNK_SIZE = 1024 * 1024


class BlobIntegrityError(RuntimeError):
    """Raised when content does not match its content-addressed identity."""


def digest_filename(digest: str) -> str:
    if not isinstance(digest, str) or not re.fullmatch(
        r"sha256:([0-9a-f]{64})", digest
    ):
        raise ValueError("Content-addressed blob digest must be a lowercase sha256 digest.")
    return digest.split(":", 1)[1]


def create_temporary_file(directory: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{prefix}-", suffix=".tmp", dir=directory
    )
    os.close(descriptor)
    return Path(name)


def hash_file(path: Path) -> Tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return f"sha256:{hasher.hexdigest()}", size


def blob_matches(path: Path, digest: str, size: int) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        return False
    actual_digest, actual_size = hash_file(path)
    return actual_digest == digest and actual_size == size


def publish_content_addressed(
    staged: Path, destination: Path, digest: str, size: int
) -> Tuple[str, int]:
    try:
        with staged.open("rb+") as source:
            source.flush()
            os.fsync(source.fileno())
        if blob_matches(destination, digest, size):
            return digest, size
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(
                f"Existing content-addressed blob {destination} does not match "
                f"{digest}; refusing to overwrite it."
            )

        try:
            os.link(staged, destination)
        except FileExistsError:
            if not blob_matches(destination, digest, size):
                raise FileExistsError(
                    f"Existing content-addressed blob {destination} does not match "
                    f"{digest}; refusing to overwrite it."
                )
        return digest, size
    finally:
        staged.unlink(missing_ok=True)
