# Local build history

Build history is local and **off by default**. It is not benchmark telemetry
and is never uploaded. Enable it for a build and inspect the resulting JSON:

```bash
pycontainer build --record-history
pycontainer history list
pycontainer history clear
```

The default directory is `~/.pycontainer/history`. To use another directory,
set `--history-dir PATH` on both the build and each history command. A directory
option alone does not enable recording. The directory must be outside both the
build context and OCI output directory, including through symbolic links.

The equivalent configuration and Python API are:

```toml
[build]
record-history = true
history-dir = "/path/outside/the/project/history"
```

```python
from pycontainer import BuildConfig
from pycontainer.builder import ImageBuilder

config = BuildConfig(record_history=True, history_dir="/path/outside/the/project/history")
ImageBuilder(config).build()
```

An explicit `--no-record-history` overrides an enabled config file.
`pycontainer history` does not load the build config, so pass its custom
directory explicitly. Inspection or deletion of a nonexistent directory
does not create it.

## What is retained

Each record contains exactly:

- `run_id`: a random UUID4, unrelated to the project or image.
- `started_at`: the UTC start time, with microsecond precision.
- `status`: `success` or `failed`.
- `total_ns`: elapsed time in integer nanoseconds.
- `phases_ns`: elapsed nanoseconds for the phases actually reached.

The phase names are `base_image` (base image pull), `dependencies` (dependency
staging, when enabled), `application` (application layer creation), and `sbom`
(SBOM generation, when enabled). A failed phase includes the time up to its
exception. If builder initialization itself fails, its separate failed record
has an `initialization` phase. For normal `ImageBuilder.build()` calls, total
time includes output locking and image assembly but excludes successful builder
initialization, history persistence, and any subsequent registry push.
For multi-platform builds, repeated phase timings are summed across targets;
the shared application layer is timed once.

No project names or paths, image or registry tags, registry URLs, credentials,
environment keys or values, labels, command lines, or exception messages are
stored. Records are separate from the build context and OCI layout, so they do
not affect reproducible image content. A push failure does not change a
successful build record. Argument/configuration errors before a builder exists,
dry runs, and processes killed before recording do not produce a record.

## Retention and deletion

`history.json` holds at most the latest 1,000 records, in order of completed
writes. The committed file is capped at 512 KiB; older records are removed
first if either limit is reached. A single temporary file, also at most
512 KiB, and a small lock file may exist alongside it. Storage created by the
tool is therefore bounded apart from filesystem metadata and externally
modified files.

`pycontainer history clear` deletes the ledger and any temporary file, including
a corrupt ledger. It leaves the directory and lock file in place so concurrent
processes continue to synchronize. A build still in progress can write a new
record after `clear` returns. Delete backups separately if applicable.

New history directories and files use private POSIX modes (at most `0700` and
`0600`, further restricted by umask); existing directory permissions are not
changed. On Windows, access follows the directory's existing ACL. History is
not encrypted.

## Concurrency and failures

Writers, inspection, and deletion use the same cross-process lock. Each update
validates the old ledger, writes and flushes one temporary file, and atomically
replaces the committed file. On supported POSIX filesystems the directory is
also synchronized. An interrupted writer leaves the previous committed file or
the complete new file, not a partial record; a leftover temporary file is
ignored by readers and removed by the next successful write or `clear`.
Filesystem and power-loss guarantees still depend on the operating system.

Malformed, oversized, or unrecognized records cause an explicit error and are
not overwritten. Inspect or back up the file manually if needed, then use
`pycontainer history clear` to start again. History files and lock files must be
regular files rather than symbolic links.

If a successful image build cannot save or confirm its history, the build
command/API reports a history storage error and does not proceed to a requested
push; the OCI image may already exist. If the build itself fails and history
also fails, the storage error is logged and the original build exception is
preserved. No success is claimed for an unconfirmed history write.
