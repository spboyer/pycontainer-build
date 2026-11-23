import errno
import fnmatch
import hashlib
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath


DEFAULT_CONTEXT_EXCLUDES = (
    "**/.git",
    "**/.hg",
    "**/.svn",
    "**/.venv",
    "**/venv",
    "**/env",
    "**/__pycache__",
    "**/.pytest_cache",
    "**/.mypy_cache",
    "**/.ruff_cache",
    "**/.tox",
    "**/.nox",
    "**/.pycontainer",
    "**/.cache",
    "**/.hypothesis",
    "**/build",
    "**/dist",
    "**/tests",
    "**/test",
    "**/fixtures",
    "setup.py",
    "**/test.py",
    "**/test_*.py",
    "**/*_test.py",
)
VIRTUALENV_EXCLUDES = ("**/.venv", "**/venv", "**/env")


def ensure_dir(path):
    p=Path(path); p.mkdir(parents=True, exist_ok=True); return p


def relative_context_path(path):
    normalized = str(path).replace("\\", "/").removeprefix("./").rstrip("/")
    relative = Path(normalized or ".")
    windows_path = PureWindowsPath(normalized)
    if (
        relative.is_absolute()
        or windows_path.drive
        or windows_path.root
        or ".." in relative.parts
    ):
        raise ValueError(f"Context paths must be relative and stay inside the context: {path!r}")
    return relative


def _glob_matches(path_parts, pattern_parts):
    if not pattern_parts:
        return not path_parts
    if pattern_parts[0] == "**":
        return _glob_matches(path_parts, pattern_parts[1:]) or (
            bool(path_parts) and _glob_matches(path_parts[1:], pattern_parts)
        )
    return bool(path_parts) and fnmatch.fnmatchcase(
        path_parts[0], pattern_parts[0]
    ) and _glob_matches(path_parts[1:], pattern_parts[1:])


def path_is_excluded(relative_path, excludes):
    parts = Path(relative_path).as_posix().split("/")
    for exclude in excludes:
        pattern = str(exclude).replace("\\", "/").removeprefix("./").rstrip("/")
        if not pattern:
            continue
        if pattern == ".":
            return True
        has_glob = any(character in pattern for character in "*?[")
        if pattern.startswith("**/") and pattern.count("/") == 1:
            component_pattern = pattern[3:]
            if any(character in component_pattern for character in "*?["):
                if any(
                    fnmatch.fnmatchcase(part, component_pattern) for part in parts
                ):
                    return True
            elif component_pattern in parts:
                return True
            continue
        if has_glob and "/" not in pattern and any(
            fnmatch.fnmatchcase(part, pattern) for part in parts
        ):
            return True
        pattern_parts = pattern.split("/")
        for end in range(1, len(parts) + 1):
            candidate_parts = parts[:end]
            if has_glob:
                if _glob_matches(candidate_parts, pattern_parts):
                    return True
            elif candidate_parts[: len(pattern_parts)] == pattern_parts:
                return True
    return False


def _raise_walk_error(error):
    raise error


def _conda_prefix_in_context(base, relative):
    """Check context-relative ancestors and resolved link targets."""
    for parent in (relative, *relative.parents):
        candidate = base / parent
        if (candidate / "conda-meta").is_dir():
            return candidate.absolute()
    absolute = (base / relative).absolute()
    try:
        resolved = absolute.resolve()
    except RuntimeError:
        raise ValueError(
            f"Context symlink {relative.as_posix()!r} is cyclic."
        ) from None
    if resolved != absolute:
        for parent in (resolved, *resolved.parents):
            if (parent / "conda-meta").is_dir():
                return parent
    return None


def conda_prefix(base, relative):
    """Find a Conda prefix containing a context path without reading metadata."""
    base = Path(base)
    relative = relative_context_path(relative)
    prefix = _conda_prefix_in_context(base, relative)
    if prefix is not None:
        return prefix
    for parent in base.absolute().parents:
        if (parent / "conda-meta").is_dir():
            return parent
    return None


def context_exclude_paths(
    context_dir,
    *,
    output_dir=None,
    cache_dir=None,
    exclude_paths=(),
    exclude_defaults=True,
):
    """Return root-relative paths and patterns excluded from context scans."""
    excludes = list(DEFAULT_CONTEXT_EXCLUDES) if exclude_defaults else []
    context = Path(os.path.abspath(context_dir))
    for generated_path in (output_dir, cache_dir):
        if generated_path is None:
            continue
        absolute_path = Path(os.path.abspath(generated_path))
        try:
            excludes.append(absolute_path.relative_to(context).as_posix())
        except ValueError:
            continue

    for path in exclude_paths:
        excludes.append(relative_context_path(path).as_posix())
    return tuple(dict.fromkeys(excludes))


def _is_directory_link(path):
    """Detect directory symlinks and Windows junctions (mount points)."""
    if not path.is_dir():
        return False
    reparse_tag = getattr(path.lstat(), "st_reparse_tag", 0)
    return path.is_symlink() or reparse_tag == getattr(
        stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003
    )


def _check_root_links(base, relative):
    """Reject context paths reached through, or naming, a directory link."""
    current = Path(base)
    for index, part in enumerate(relative.parts):
        current = current / part
        if _is_directory_link(current) or (
            current.is_symlink() and index < len(relative.parts) - 1
        ):
            raise ValueError(
                f"Context path {relative.as_posix()!r} traverses a symlink or "
                "junction directory; directory links are never followed."
            )


def _check_selected_link(base, path, relative):
    """Require a selected file symlink to resolve to a file inside the context."""
    link_target = os.readlink(path)
    windows_target = PureWindowsPath(link_target)
    if os.path.isabs(link_target) or windows_target.drive or windows_target.root:
        raise ValueError(
            f"Selected symlink {relative.as_posix()!r} must use a relative target."
        )
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(
            f"Selected symlink {relative.as_posix()!r} is dangling or cyclic."
        ) from None
    if not target.is_relative_to(Path(base).resolve()) or not target.is_file():
        raise ValueError(
            f"Selected symlink {relative.as_posix()!r} must point to a file inside "
            "the build context."
        )


def iter_files(base, relative_paths, *, exclude_paths=None, exclude_defaults=True):
    """Yield unique context files, pruning excluded directories during traversal.

    Directory symlinks and junctions are never followed: an include path that
    names or traverses one is rejected and nested ones are pruned. Selected
    file symlinks must use a relative target resolving to a file inside the
    context.
    """
    base = Path(base)
    excludes = list(DEFAULT_CONTEXT_EXCLUDES) if exclude_defaults else []
    if exclude_paths is not None:
        excludes.extend(exclude_paths)

    def selected(path, relative):
        if path.is_symlink():
            if _conda_prefix_in_context(base, relative) is not None:
                return False
            _check_selected_link(base, path, relative)
        return path.is_file()

    seen = set()
    for relative_path in relative_paths:
        relative = relative_context_path(relative_path)
        if path_is_excluded(relative, excludes):
            continue
        if conda_prefix(base, relative) is not None:
            continue
        absolute = base / relative
        _check_root_links(base, relative)
        if absolute.is_dir():
            for root, directories, filenames in os.walk(
                absolute, topdown=True, onerror=_raise_walk_error
            ):
                root_path = Path(root)
                root_relative = root_path.relative_to(base)
                directories[:] = sorted(
                    directory
                    for directory in directories
                    if not path_is_excluded(root_relative / directory, excludes)
                    and not _is_directory_link(root_path / directory)
                    and _conda_prefix_in_context(base, root_relative / directory) is None
                )
                for filename in sorted(filenames):
                    child = root_path / filename
                    child_relative = child.relative_to(base)
                    relative_name = child_relative.as_posix()
                    if (
                        relative_name not in seen
                        and not path_is_excluded(child_relative, excludes)
                        and selected(child, child_relative)
                    ):
                        seen.add(relative_name)
                        yield child, child_relative
        elif absolute.is_file() or absolute.is_symlink():
            child_relative = absolute.relative_to(base)
            relative_name = child_relative.as_posix()
            if (
                relative_name not in seen
                and not path_is_excluded(child_relative, excludes)
                and selected(absolute, child_relative)
            ):
                seen.add(relative_name)
                yield absolute, child_relative


def select_context_files(base, include_paths, excludes):
    """Return the deterministic (absolute, relative) application file selection.

    ``excludes`` must already contain any default patterns. A selected file
    symlink must target another selected file, so exclusions win over links.
    """
    files = list(
        iter_files(base, include_paths, exclude_paths=excludes, exclude_defaults=False)
    )
    # Lexical membership keeps the link valid inside the image; resolved
    # membership stops ``..`` after an in-context link from naming other files.
    lexical_paths = {os.path.normcase(os.path.abspath(path)) for path, _ in files}
    resolved_paths = {os.path.normcase(os.path.realpath(path)) for path, _ in files}
    for path, relative in files:
        if path.is_symlink():
            target = path.parent / os.readlink(path)
            if (
                os.path.normcase(os.path.abspath(target)) not in lexical_paths
                or os.path.normcase(os.path.realpath(target)) not in resolved_paths
            ):
                raise ValueError(
                    f"Selected symlink {relative.as_posix()!r} must target "
                    "another selected file."
                )
    return files


@contextmanager
def file_lock(path, *, blocking=True):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        raise BlockingIOError(
                            error.errno, "Lock is already held", str(lock_path)
                        ) from error
                    time.sleep(0.05)

            try:
                # Windows can lock beyond EOF, so initialize only after locking.
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(lock.fileno(), operation)
            except OSError as error:
                if not blocking and error.errno in (errno.EACCES, errno.EAGAIN):
                    raise BlockingIOError(
                        error.errno, "Lock is already held", str(lock_path)
                    ) from error
                raise

            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def output_directory_lock(output_dir):
    output_path = Path(output_dir).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_key = hashlib.sha256(os.fsencode(str(output_path))).hexdigest()
    lock_path = output_path.parent / f".pycontainer-build-{output_key}.lock"

    lock = file_lock(lock_path, blocking=False)
    try:
        lock.__enter__()
    except BlockingIOError as error:
        raise RuntimeError(
            f"Another build is using output directory {output_path}; "
            "concurrent writes to the same output are not supported."
        ) from error
    try:
        yield
    finally:
        lock.__exit__(None, None, None)
