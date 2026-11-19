import errno
import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path


def ensure_dir(path):
    p=Path(path); p.mkdir(parents=True, exist_ok=True); return p


def iter_files(base, relative_paths):
    for rel in relative_paths:
        abs_path = base/rel
        if abs_path.is_dir():
            for child in abs_path.rglob("*"):
                if child.is_file():
                    yield child, child.relative_to(base)
        elif abs_path.is_file():
            yield abs_path, abs_path.relative_to(base)


@contextmanager
def file_lock(path, *, blocking=True):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
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
