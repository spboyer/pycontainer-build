"""Resolve the set of tags for a single build."""
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .registry_client import parse_image_reference

_SEMVER = re.compile(r"(?P<prefix>v?)(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_OCI_COMPONENT = r"[A-Za-z0-9]+(?:(?:[-._:@+]|--)[A-Za-z0-9]+)*"
_OCI_REF_NAME = re.compile(rf"{_OCI_COMPONENT}(?:/{_OCI_COMPONENT})*")
_GIT_TIMEOUT_SECONDS = 30
_GIT_REPOSITORY_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_IMPLICIT_WORK_TREE", "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE",
    "GIT_REPLACE_REF_BASE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
)


class GitTagError(ValueError):
    """Git state cannot safely supply an image tag."""


def _validate_worktree_status(context: Path, allow_dirty: bool, generated_paths=()) -> None:
    status = _git(
        context, "status", "--porcelain", "-z", "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status.stderr.strip():
        raise GitTagError(f"Git status could not fully inspect the worktree: {status.stderr.strip()}")
    if allow_dirty or not status.stdout:
        return
    records = status.stdout.split("\0")
    if generated_paths:
        root_result = _git(context, "rev-parse", "--show-toplevel")
        root = root_result.stdout.rstrip("\r\n")
        if root_result.stderr.strip() or not root:
            raise GitTagError(
                f"Git could not determine the worktree root: {root_result.stderr.strip() or 'empty result'}"
            )
        generated = tuple(Path(path).parent.resolve() / Path(path).name for path in generated_paths)
        for record in records:
            if not record:
                continue
            if record.startswith("?? "):
                path = Path(root) / record[3:]
                path = path.parent.resolve() / path.name
                if path in generated:
                    continue
            break
        else:
            return
    raise GitTagError("Git worktree is dirty; commit or remove changes, or pass --allow-dirty.")


def _git(context: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for key in _GIT_REPOSITORY_ENV:
        env.pop(key, None)
    for key in list(env):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key)
    env.update(
        GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
        LC_ALL="C", LANGUAGE="C", LANG="C",
    )
    command = ["git", "-c", "core.fsmonitor=false", "-C", str(context), *args]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT_SECONDS, env=env,
        )
    except FileNotFoundError as error:
        raise GitTagError("Git executable not found; install Git to use --tag-from-git.") from error
    except subprocess.TimeoutExpired as error:
        raise GitTagError(f"Git {args[0]} timed out while inspecting the build context.") from error
    except OSError as error:
        raise GitTagError(f"Could not run Git {args[0]}: {error}") from error
    if check and result.returncode:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise GitTagError(f"Git {args[0]} failed: {detail}")
    return result


def git_tag_snapshot(
    context: str, fallback: Optional[str], allow_dirty: bool
) -> Tuple[str, str, str, bool]:
    """Read the context, HEAD and exact Git tag without modifying Git refs."""
    try:
        path = Path(context).resolve()
    except (OSError, RuntimeError) as error:
        raise GitTagError(f"Cannot resolve Git build context {context!r}: {error}") from error

    worktree = _git(path, "rev-parse", "--is-inside-work-tree", check=False)
    if worktree.returncode or worktree.stdout.strip() != "true":
        detail = worktree.stderr.strip()
        raise GitTagError(
            f"Build context {str(path)!r} is not inside a Git worktree"
            f"{': ' + detail if detail else '.'}"
        )

    head = _git(path, "rev-parse", "--verify", "HEAD^{commit}", check=False)
    commit = head.stdout.strip()
    if head.returncode or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        detail = head.stderr.strip()
        raise GitTagError(
            "Git repository has no usable HEAD commit"
            f"{': ' + detail if detail else '.'}"
        )

    _validate_worktree_status(path, allow_dirty)

    description = _git(path, "describe", "--tags", "--exact-match", commit, check=False)
    if description.returncode:
        matches = _git(path, "tag", "--points-at", commit)
        if matches.stderr.strip():
            raise GitTagError(f"Git tag inspection failed: {matches.stderr.strip()}")
        detail = description.stderr.strip()
        diagnostic = detail.casefold()
        no_match = (
            "no names found, cannot describe anything" in diagnostic
            or "no tag exactly matches" in diagnostic
        )
        if matches.stdout.strip() or not no_match:
            raise GitTagError(f"Git describe failed: {detail or description.returncode}")
        if fallback != "sha":
            raise GitTagError(
                "HEAD is not exactly tagged; use --git-fallback sha to use the commit ID."
            )
        version = f"sha-{commit[:12]}"
        exact_tag = False
    else:
        if description.stderr.strip():
            raise GitTagError(f"Git describe reported an error: {description.stderr.strip()}")
        version = description.stdout.strip()
        if not _TAG.fullmatch(version):
            raise GitTagError(f"Git tag {version!r} is not a valid OCI tag.")
        exact_tag = True

    current_head = _git(path, "rev-parse", "--verify", "HEAD^{commit}")
    if current_head.stdout.strip() != commit:
        raise GitTagError("Git HEAD changed while inspecting the build context.")
    return str(path), commit, version, exact_tag


def validate_git_tag_reference(
    snapshot: Tuple[str, str, str, bool], *, check_head: bool
) -> None:
    """Prevent publishing a build after its Git commit or selected tag changes."""
    context, commit, version, exact_tag = snapshot
    path = Path(context)
    if check_head:
        head = _git(path, "rev-parse", "--verify", "HEAD^{commit}", check=False)
        if head.returncode or head.stdout.strip() != commit:
            detail = head.stderr.strip()
            raise GitTagError(
                "Git HEAD changed or became unavailable during the build"
                f"{': ' + detail if detail else '.'}"
            )
    if exact_tag:
        ref = _git(
            path, "rev-parse", "--verify", f"refs/tags/{version}^{{commit}}",
            check=False,
        )
        if ref.returncode or ref.stdout.strip() != commit:
            detail = ref.stderr.strip()
            raise GitTagError(
                f"Git tag {version!r} no longer identifies the build commit"
                f"{': ' + detail if detail else '.'}"
            )


def replace_primary_tag(ref: str, version: str) -> str:
    """Replace only the primary image reference's tag, preserving its spelling."""
    _split(ref)
    if not _TAG.fullmatch(version):
        raise GitTagError(f"Git tag {version!r} is not a valid OCI tag.")
    separator = ref.rfind(":")
    name = ref[:separator] if separator > ref.rfind("/") else ref
    return f"{name}:{version}"


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
