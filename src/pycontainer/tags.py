"""Resolve the set of tags for a single build."""
import re
from typing import List, Optional, Tuple

from .registry_client import parse_image_reference

_SEMVER = re.compile(r"(?P<prefix>v?)(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_OCI_COMPONENT = r"[A-Za-z0-9]+(?:(?:[-._:@+]|--)[A-Za-z0-9]+)*"
_OCI_REF_NAME = re.compile(rf"{_OCI_COMPONENT}(?:/{_OCI_COMPONENT})*")


def semver_expansion(tag: str) -> List[str]:
    """Return the minor and major tags for a stable semantic version tag.

    v1.2.3 -> [v1.2, v1]. Pre-release and build-metadata tags expand to nothing.
    """
    match = _SEMVER.fullmatch(tag)
    if not match:
        return []
    prefix, major, minor = match["prefix"], match["major"], match["minor"]
    return [f"{prefix}{major}.{minor}", f"{prefix}{major}"]


def _split(ref: str) -> Tuple[str, str, str]:
    if "@" in ref:
        raise ValueError(f"Tag {ref!r} must not be a digest reference.")
    return parse_image_reference(ref)


def resolve_tags(
    tag: str,
    extra_tags: Optional[List[str]] = None,
    semver: bool = False,
    latest: bool = False,
) -> List[str]:
    """Return ordered, de-duplicated full image references for a build.

    ``tag`` is the primary reference. Entries in ``extra_tags`` are either full
    references (which must share the primary registry and repository) or bare
    tags such as ``v1`` that are applied to the primary repository.
    """
    registry, repo, primary_tag = _split(tag)
    base = f"{registry}/{repo}"
    names = [primary_tag]

    for entry in extra_tags or []:
        if not isinstance(entry, str) or not entry:
            raise ValueError("Tags must be non-empty strings.")
        if "/" in entry or ":" in entry:
            entry_registry, entry_repo, entry_tag = _split(entry)
            if (entry_registry, entry_repo) != (registry, repo):
                raise ValueError(
                    f"Tag {entry!r} targets {entry_registry}/{entry_repo}, but the "
                    f"primary tag targets {base}. All tags in one build must share "
                    "a repository."
                )
            names.append(entry_tag)
        else:
            if not _TAG.fullmatch(entry):
                raise ValueError(f"Invalid tag: {entry!r}.")
            names.append(entry)

    if semver:
        expanded = [t for name in list(names) for t in semver_expansion(name)]
        if not expanded and not any(_SEMVER.fullmatch(n) for n in names):
            raise ValueError(
                "Semantic version expansion requires a tag like v1.2.3 or 1.2.3; "
                f"got {', '.join(names)}."
            )
        names.extend(expanded)
    if latest:
        names.append("latest")

    ordered = list(dict.fromkeys(names))
    return [f"{base}:{name}" for name in ordered]


def local_reference_names(primary: str, all_tags: List[str]) -> List[str]:
    """Return OCI layout ``ref.name`` values for a build's resolved tags.

    Names use the primary's repository spelling. If a name is not a valid OCI
    annotation reference, encode its UTF-8 bytes as hex after ``pycontainer-``.
    """
    last_segment = primary.rsplit("/", 1)[-1]
    repository = primary.rsplit(":", 1)[0] if ":" in last_segment else primary
    names = list(dict.fromkeys([primary] + [
        f"{repository}:{parse_image_reference(ref)[2]}" for ref in all_tags[1:]
    ]))
    used = {name for name in names if _OCI_REF_NAME.fullmatch(name)}
    result = []
    for name in names:
        if _OCI_REF_NAME.fullmatch(name):
            result.append(name)
            continue
        encoded = "pycontainer-" + name.encode("utf-8").hex()
        while encoded in used:
            encoded += "0"
        used.add(encoded)
        result.append(encoded)
    return result
