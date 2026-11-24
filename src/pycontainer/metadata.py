"""Validation and merging for image labels and OCI annotations."""

from typing import Dict, Optional

REFERENCE_ANNOTATION = "org.opencontainers.image.ref.name"


def validate_metadata(metadata: object, name: str, *, protect_reference: bool = False) -> None:
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise ValueError(f"{name} must be a string-to-string mapping.")
    for key, value in metadata.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{name} keys must be non-empty strings.")
        if not isinstance(value, str):
            raise ValueError(f"{name} values must be strings.")
        if protect_reference and key == REFERENCE_ANNOTATION:
            raise ValueError(
                f"{name} cannot set protected annotation {REFERENCE_ANNOTATION!r}; "
                "reference names are managed by the builder."
            )


def merge_image_labels(
    base_labels: Optional[Dict[str, str]] = None,
    labels: Optional[Dict[str, str]] = None,
    generated_labels: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Merge labels, rejecting generated keys that collide with explicit labels."""
    validate_metadata(base_labels, "Base image labels")
    validate_metadata(labels, "Image labels")
    validate_metadata(generated_labels, "Generated image labels")

    explicit = labels or {}
    generated = generated_labels or {}
    conflicts = sorted(explicit.keys() & generated.keys())
    if conflicts:
        names = ", ".join(repr(key) for key in conflicts)
        raise ValueError(
            f"Generated image label key(s) {names} conflict with explicitly "
            "configured labels; choose a different key."
        )

    merged = {**(base_labels or {}), **explicit, **generated}
    return {key: merged[key] for key in sorted(merged)}
