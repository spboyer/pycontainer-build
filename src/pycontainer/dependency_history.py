"""Bounded, opt-in Git evidence for dependency grouping."""
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .lockfiles import (
    LockSelection,
    MAX_LOCKFILE_BYTES,
    _check_uv_environments,
    _require_python,
    _uv_package_index,
    _uv_selected_graph,
    canonical_name,
)


MAX_HISTORY_SNAPSHOTS = 20
GIT_TIMEOUT_SECONDS = 30


def _git(context: Path, *args: str, purpose: str, allow_missing: bool = False) -> bytes:
    environment = dict(os.environ)
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(name, None)
    environment.update(
        GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
        GIT_LITERAL_PATHSPECS="1",
    )
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(context), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise ValueError("Dependency history requires Git to be installed.") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"Dependency history timed out while {purpose}.") from error
    except OSError as error:
        raise ValueError(f"Dependency history could not start Git while {purpose}.") from error
    if result.returncode and not (allow_missing and result.returncode == 1):
        raise ValueError(f"Dependency history is unavailable while {purpose}.")
    return result.stdout


def _versions(
    blob: bytes, environment: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, set]:
    try:
        lock = tomllib.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("Dependency history contains a malformed uv.lock snapshot.") from error
    entries = lock.get("package")
    if lock.get("version") != 1 or not isinstance(entries, list):
        raise ValueError("Dependency history contains an unsupported uv.lock snapshot.")
    versions: Dict[str, set] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Dependency history contains an invalid package entry.")
        name, version = entry.get("name"), entry.get("version")
        if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
            raise ValueError("Dependency history contains a package without a name or version.")
        versions.setdefault(canonical_name(name), set()).add(version)
    if environment is not None:
        if "manifest" in lock:
            raise ValueError("Dependency history does not support uv workspace or override snapshots.")
        _check_uv_environments(
            lock, {"environments": lock.get("supported-markers")}, environment
        )
        _require_python(lock.get("requires-python"), environment, "historical uv.lock")
        by_name, root = _uv_package_index(lock)
        selected, _ = _uv_selected_graph(by_name, root, environment)
        versions = {}
        for name, version in selected:
            if name in versions and version not in versions[name]:
                raise ValueError(f"Dependency history selects multiple versions of {name} for the target.")
            versions.setdefault(name, set()).add(version)
    return versions


def read_dependency_history(
    context: Path, lock_path: Path, history_ref: Optional[str],
    *, environment: Optional[Dict[str, Optional[str]]] = None,
) -> Tuple[str, Tuple[str, ...], List[Dict[str, set]]]:
    context = Path(context).resolve()
    root = Path(
        os.fsdecode(_git(context, "rev-parse", "--show-toplevel", purpose="finding the repository")).strip()
    ).resolve()
    if not context.is_relative_to(root):
        raise ValueError("Dependency history requires the build context to be inside its Git repository.")
    shallow = _git(
        context, "rev-parse", "--is-shallow-repository", purpose="checking repository history"
    ).decode("ascii").strip()
    if shallow != "false":
        raise ValueError("Dependency history requires a complete, non-shallow Git repository.")
    partial = _git(
        context, "config", "--local", "--get-regexp",
        r"^(extensions\.partialclone|remote\..*\.promisor)$",
        purpose="checking for a partial clone", allow_missing=True,
    ).decode("utf-8", errors="strict")
    for line in partial.splitlines():
        key, _, value = line.partition(" ")
        if key.lower() == "extensions.partialclone" or value.strip().lower() == "true":
            raise ValueError("Dependency history does not support partial or promisor clones.")

    relative = (context / lock_path).relative_to(root).as_posix()
    entries = _git(
        root, "ls-files", "--error-unmatch", "-v", "-z", "--", relative,
        purpose="checking the tracked lockfile",
    )
    if not entries.startswith(b"H ") or entries.count(b"\0") != 1:
        raise ValueError(
            "Dependency history requires a tracked, unmodified lockfile without "
            "assume-unchanged or skip-worktree flags."
        )
    status = _git(
        root, "status", "--porcelain=v1", "--untracked-files=normal", "--", relative,
        purpose="checking lockfile changes",
    )
    if status:
        raise ValueError("Dependency history requires a tracked, unmodified lockfile.")
    ref = history_ref or "HEAD"
    snapshot = _git(
        root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}",
        purpose="resolving the requested snapshot",
    ).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", snapshot):
        raise ValueError("Dependency history did not resolve an immutable commit.")
    output = _git(
        root, "log", "--first-parent", f"--max-count={MAX_HISTORY_SNAPSHOTS}",
        "--format=%H", snapshot, "--", relative, purpose="reading lockfile history",
    ).decode("ascii")
    commits = tuple(line for line in output.splitlines() if line)
    if len(commits) < 2:
        raise ValueError("Dependency history needs at least two tracked lockfile snapshots.")

    histories = []
    blob_ids = set()
    for commit in commits:
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ValueError("Dependency history returned an invalid commit identifier.")
        object_ref = f"{commit}:{relative}"
        blob_id = _git(
            root, "rev-parse", "--verify", "--end-of-options", object_ref,
            purpose="locating a historical lockfile",
        ).decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", blob_id):
            raise ValueError("Dependency history returned an invalid lockfile object.")
        kind = _git(root, "cat-file", "-t", blob_id, purpose="checking a lockfile object")
        if kind.strip() != b"blob":
            raise ValueError("Dependency history contains a non-file lockfile object.")
        size = _git(root, "cat-file", "-s", blob_id, purpose="checking lockfile size").strip()
        if not size.isdigit() or int(size) > MAX_LOCKFILE_BYTES:
            raise ValueError(f"Dependency history lockfiles must not exceed {MAX_LOCKFILE_BYTES} bytes.")
        blob = _git(root, "cat-file", "blob", blob_id, purpose="reading a historical lockfile")
        if len(blob) != int(size):
            raise ValueError("Dependency history returned an incomplete lockfile object.")
        blob_ids.add(blob_id)
        histories.append(_versions(blob, environment))
    if len(blob_ids) < 2:
        raise ValueError("Dependency history needs at least two distinct lockfile snapshots.")

    return snapshot, commits, histories


def analyze_dependency_history(
    context: Path, selection: LockSelection, history_ref: Optional[str],
    *, environment: Optional[Dict[str, Optional[str]]] = None,
    report: Callable[[str], None] = print,
) -> Tuple[str, Tuple[str, ...], Dict[str, Tuple[str, str]]]:
    snapshot, commits, histories = read_dependency_history(
        context, selection.path, history_ref, environment=environment
    )
    result = {}
    for package in selection.packages:
        versions = [history.get(package.name, set()) for history in histories]
        if environment is None:
            result[package.name] = ("frequent", "target environment unavailable for history classification")
        elif any(not entry for entry in versions):
            result[package.name] = ("frequent", "absent from at least one history snapshot")
        elif any(entry != versions[0] for entry in versions[1:]):
            result[package.name] = ("frequent", "historical version set changed")
        elif package.version not in versions[0]:
            result[package.name] = ("frequent", "current target version differs from history")
        else:
            result[package.name] = ("stable", f"unchanged in {len(commits)} lockfile snapshots")
    report(
        f"Dependency history snapshot: {snapshot}; first-parent lock commits: "
        + ", ".join(commits)
    )
    return snapshot, commits, result
