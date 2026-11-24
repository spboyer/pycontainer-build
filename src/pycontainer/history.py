"""Opt-in, local-only build history with bounded, atomic storage."""
import errno
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .fs_utils import file_lock


_SCHEMA_VERSION = 1
_MAX_RECORDS = 1000
_MAX_BYTES = 512 * 1024
_MAX_DURATION_NS = 10**18
_PHASES = frozenset(
    {"initialization", "base_image", "dependencies", "application", "sbom"}
)
_RECORD_KEYS = frozenset(
    {"run_id", "started_at", "status", "total_ns", "phases_ns"}
)


class HistoryStorageError(RuntimeError):
    """Raised when local history cannot be read, validated, or persisted."""


class BuildTiming:
    def __init__(self):
        self.run_id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        self.started_ns = time.perf_counter_ns()
        self.phases_ns = {}

    @contextmanager
    def phase(self, name):
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            elapsed = time.perf_counter_ns() - started
            self.phases_ns[name] = self.phases_ns.get(name, 0) + elapsed

    def record(self, status):
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "status": status,
            "total_ns": time.perf_counter_ns() - self.started_ns,
            "phases_ns": dict(self.phases_ns),
        }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _validate_record(record):
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        raise HistoryStorageError("Malformed build history record.")

    run_id = record["run_id"]
    try:
        parsed_id = uuid.UUID(run_id) if isinstance(run_id, str) else None
    except ValueError as error:
        raise HistoryStorageError("Malformed build history run ID.") from error
    if (
        parsed_id is None
        or str(parsed_id) != run_id
        or parsed_id.version != 4
        or parsed_id.variant != uuid.RFC_4122
    ):
        raise HistoryStorageError("Malformed build history run ID.")

    started_at = record["started_at"]
    try:
        if not isinstance(started_at, str) or len(started_at) != 27:
            raise ValueError("invalid UTC timestamp")
        parsed_time = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%S.%fZ")
        if parsed_time.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != started_at:
            raise ValueError("noncanonical UTC timestamp")
    except ValueError as error:
        raise HistoryStorageError("Malformed build history timestamp.") from error

    if record["status"] not in ("success", "failed"):
        raise HistoryStorageError("Malformed build history status.")
    total = record["total_ns"]
    if type(total) is not int or not 0 <= total <= _MAX_DURATION_NS:
        raise HistoryStorageError("Malformed build history duration.")
    phases = record["phases_ns"]
    if (
        not isinstance(phases, dict)
        or not set(phases).issubset(_PHASES)
        or any(
            type(value) is not int or not 0 <= value <= _MAX_DURATION_NS
            for value in phases.values()
        )
        or sum(phases.values()) > total
    ):
        raise HistoryStorageError("Malformed build history phase timings.")


def _check_regular_file(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise HistoryStorageError(
            f"Build history path must be a regular file, not a link or directory: {path}"
        )


def _absolute_path(path):
    value = os.fspath(Path(path))
    if "\0" in value:
        raise ValueError("path contains a null character")
    return Path(os.path.abspath(value))


def _sync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
    finally:
        os.close(descriptor)


class BuildHistory:
    def __init__(self, directory=None):
        try:
            requested = (
                Path(directory).expanduser()
                if directory is not None
                else Path.home() / ".pycontainer" / "history"
            )
            self.requested_directory = _absolute_path(requested)
            self.directory = self.requested_directory.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise HistoryStorageError(f"Invalid build history directory: {error}") from error
        self.history_file = self.directory / "history.json"
        self.temp_file = self.directory / ".history.json.tmp"
        self.lock_file = self.directory / "history.lock"

    def validate_location(self, context_dir, output_dir):
        try:
            for root in (context_dir, output_dir):
                requested_root = _absolute_path(root)
                resolved_root = requested_root.resolve()
                if (
                    self.requested_directory.is_relative_to(requested_root)
                    or self.directory.is_relative_to(resolved_root)
                    or self.requested_directory.resolve().is_relative_to(resolved_root)
                ):
                    raise HistoryStorageError(
                        "Build history directory must be outside the build context "
                        "and OCI output directory."
                    )
        except (OSError, RuntimeError, ValueError) as error:
            if isinstance(error, HistoryStorageError):
                raise
            raise HistoryStorageError(
                f"Cannot validate build history directory: {error}"
            ) from error

    @contextmanager
    def _locked(self, *, create):
        try:
            if create:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                try:
                    self.directory.stat()
                except FileNotFoundError:
                    yield False
                    return

            _check_regular_file(self.lock_file)
            try:
                descriptor = os.open(
                    self.lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            _check_regular_file(self.lock_file)
            with file_lock(self.lock_file):
                _check_regular_file(self.lock_file)
                yield True
        except HistoryStorageError:
            raise
        except OSError as error:
            raise HistoryStorageError(f"Build history storage error: {error}") from error

    def _read(self):
        _check_regular_file(self.history_file)
        try:
            with self.history_file.open("rb") as source:
                contents = source.read(_MAX_BYTES + 1)
        except FileNotFoundError:
            return []
        if len(contents) > _MAX_BYTES:
            raise HistoryStorageError("Build history exceeds its storage limit.")
        try:
            data = json.loads(
                contents.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise HistoryStorageError("Malformed build history JSON.") from error
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "records"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != _SCHEMA_VERSION
            or not isinstance(data["records"], list)
            or len(data["records"]) > _MAX_RECORDS
        ):
            raise HistoryStorageError("Malformed build history ledger.")
        for record in data["records"]:
            _validate_record(record)
        return data["records"]

    def _write(self, records):
        records = records[-_MAX_RECORDS:]
        while True:
            payload = json.dumps(
                {"schema_version": _SCHEMA_VERSION, "records": records},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(payload) <= _MAX_BYTES:
                break
            if len(records) <= 1:
                raise HistoryStorageError("Build history record exceeds its storage limit.")
            records = records[1:]

        _check_regular_file(self.history_file)
        _check_regular_file(self.temp_file)
        self.temp_file.unlink(missing_ok=True)
        descriptor = os.open(
            self.temp_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(self.temp_file, self.history_file)
            _sync_directory(self.directory)
        finally:
            self.temp_file.unlink(missing_ok=True)

    def append(self, record):
        _validate_record(record)
        with self._locked(create=True):
            records = self._read()
            records.append(record)
            self._write(records)

    def list(self):
        with self._locked(create=False) as exists:
            return self._read() if exists else []

    def clear(self):
        with self._locked(create=False) as exists:
            if not exists:
                return
            self.history_file.unlink(missing_ok=True)
            self.temp_file.unlink(missing_ok=True)
            _sync_directory(self.directory)
