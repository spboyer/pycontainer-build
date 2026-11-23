"""Release notes embedded as an image-config label."""

from pathlib import Path
from typing import Dict

from .metadata import merge_image_labels, validate_metadata

DEFAULT_RELEASE_NOTES_LABEL = "io.github.spboyer.pycontainer.release-notes"
MAX_RELEASE_NOTES_BYTES = 8 * 1024
_UTF8_BOM = b"\xef\xbb\xbf"
# Normalizing CRLF can at most halve the size, so larger files always exceed the cap.
_MAX_RAW_BYTES = len(_UTF8_BOM) + 2 * MAX_RELEASE_NOTES_BYTES


class ReleaseNotesError(ValueError):
    """Release notes cannot be embedded; messages never include file contents."""


def validate_release_notes_settings(release_notes_file: object, release_notes_label: object) -> None:
    if release_notes_file is not None and (
        not isinstance(release_notes_file, str) or not release_notes_file.strip()
    ):
        raise ValueError("release_notes_file must be a non-empty path string.")
    if not isinstance(release_notes_label, str):
        raise ValueError("release_notes_label must be a non-empty string.")
    validate_metadata({release_notes_label: ""}, "release_notes_label")


def read_release_notes(path: Path) -> str:
    """Read UTF-8 release notes, normalize line endings to LF and enforce the byte cap."""
    try:
        with path.open("rb") as source:
            raw = source.read(_MAX_RAW_BYTES + 1)
    except (OSError, ValueError) as error:
        reason = getattr(error, "strerror", None) or "invalid path"
        raise ReleaseNotesError(
            f"Cannot read release notes file {str(path)!r}: {reason}."
        ) from None
    if len(raw) > _MAX_RAW_BYTES:
        raise ReleaseNotesError(
            f"Release notes file {str(path)!r} exceeds {MAX_RELEASE_NOTES_BYTES} bytes."
        )

    offset = len(_UTF8_BOM) if raw.startswith(_UTF8_BOM) else 0
    try:
        text = raw[offset:].decode("utf-8")
    except UnicodeDecodeError as error:
        position = offset + error.start
        raise ReleaseNotesError(
            f"Release notes file {str(path)!r} is not valid UTF-8 "
            f"(invalid byte at offset {position})."
        ) from None

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    size = len(text.encode("utf-8"))
    if size > MAX_RELEASE_NOTES_BYTES:
        raise ReleaseNotesError(
            f"Release notes file {str(path)!r} is {size} bytes after line-ending "
            f"normalization; the maximum is {MAX_RELEASE_NOTES_BYTES} bytes."
        )
    return text


def release_notes_path(context_dir: str, release_notes_file: str) -> Path:
    path = Path(release_notes_file)
    return path if path.is_absolute() else Path(context_dir) / path


def release_notes_labels(config) -> Dict[str, str]:
    """Return the generated release-notes label, rejecting explicit label collisions."""
    from .config import _is_generated_label

    validate_release_notes_settings(config.release_notes_file, config.release_notes_label)
    if config.release_notes_file is None:
        return {}
    path = release_notes_path(config.context_dir, config.release_notes_file)
    generated = {config.release_notes_label: read_release_notes(path)}
    labels = config.labels
    if _is_generated_label(config):
        labels = {key: value for key, value in labels.items() if key != "framework"}
    try:
        merge_image_labels(labels=labels, generated_labels=generated)
    except ValueError as error:
        raise ReleaseNotesError(str(error)) from None
    return generated
