# Dockerfile import

`pycontainer import` converts an existing Dockerfile into `[tool.pycontainer]`
settings and prints a migration report. It uses only the standard library and
never runs Docker.

```powershell
pycontainer import Dockerfile                          # print report and snippet
pycontainer import Dockerfile --output pyproject.toml  # write a new file
pycontainer import Dockerfile -o pyproject.toml --force
pycontainer import Dockerfile --build-arg PY=3.12
pycontainer import Dockerfile --shell '["/bin/bash", "-c"]'
```

## Output safety

- Without `--output`, the snippet is printed and nothing is written.
- If the output file exists and `--force` is not given, it is left untouched,
  the snippet is printed, and the command exits with status 1.
- With `--force`, only the `[tool.pycontainer]` tables (including
  `[tool.pycontainer.env]` and `[tool.pycontainer.labels]`) are replaced; the
  rest of the file is preserved and the result is validated as TOML.
- `--force` refuses, leaving the file untouched, if it already defines a
  top-level `[build]` table, because a config file must contain exactly one
  pycontainer table.
- Existing inline `tool.pycontainer` definitions cannot be merged; `--force`
  fails without changing the file. Convert them to tables before retrying.

## Building from the imported settings

Pass the generated file to the build command; it reads `[tool.pycontainer]`
with the same keys, validation and precedence as `[build]`:

```powershell
pycontainer import Dockerfile -o pyproject.toml
pycontainer build --config pyproject.toml --tag myapp:latest
```

Explicit CLI options such as `--base-image` or `--platform` override the
imported values; omitted options keep them. A file that defines both `[build]`
and `[tool.pycontainer]` is rejected instead of silently picking one.

## Mapping

| Dockerfile | Setting |
| --- | --- |
| `FROM image` (final stage) | `base_image` |
| `FROM --platform=...` | `platform` |
| `WORKDIR` (relative paths resolve against the previous one) | `workdir` |
| `ENV` | `[tool.pycontainer.env]` |
| `LABEL` | `[tool.pycontainer.labels]` |
| `EXPOSE` (TCP) | `exposed_ports` |
| `ENTRYPOINT` | `entrypoint` |
| `CMD` | `cmd` |
| `USER` | `user` |
| `STOPSIGNAL` | `stop_signal` |
| `VOLUME` | `volumes` |
| `SHELL` (exec form) | shell used for later shell-form `CMD`/`ENTRYPOINT` |

`ARG`/`ENV` substitution (`$VAR`, `${VAR}`, `${VAR:-default}`, `${VAR:+alt}`)
is applied to the instructions Docker expands at build time, including
`STOPSIGNAL` and `VOLUME`; `--build-arg KEY=VALUE` overrides ARG defaults.
Like Docker, `CMD`/`ENTRYPOINT` text is not substituted; the shell expands it
when the container starts.

`STOPSIGNAL` values are normalized the way Docker parses them (`term`, `15`
and `SIGTERM` all become `SIGTERM`; numbers use Linux numbering, and aliases
such as `IOT` map to their canonical names like `SIGABRT`). Unknown
signals produce a warning and are skipped. `VOLUME` accepts JSON or
space-separated paths; trailing slashes are removed and relative or unsafe
paths produce a warning. Volumes are image metadata only: nothing is mounted
at build time.

## Shell-form commands

Exec-form `CMD`/`ENTRYPOINT` arrays are imported unchanged. Shell-form text
(including JSON-looking text that is not a valid JSON string array) is kept
verbatim, quotes and all, and wrapped with the current shell:

- The default shell is `["/bin/sh", "-c"]`.
- `--shell JSON` sets the initial shell, for example
  `--shell '["/bin/bash", "-c"]'`. It must be a non-empty JSON array of
  strings with a non-empty executable first and no NUL characters; malformed
  values are rejected.
- A Dockerfile `SHELL ["…"]` instruction changes the shell for later
  `CMD`/`ENTRYPOINT` instructions in the final stage, overriding `--shell`.
  It still produces a warning, because `RUN` is never executed and OCI has no
  field to record the shell for later images. A malformed `SHELL` produces a
  warning and is ignored.

Compatibility boundary:

- The importer never runs command text on the build host; the shell only runs
  inside the container at startup.
- Docker inherits a `SHELL` set by the base image's own Dockerfile; OCI images
  do not record it, so pass `--shell` when the base image changed its shell.
- The shell must exist in the target image. A distroless base produces a
  warning because it typically has no shell; prefer exec form there.
- A `CMD`-only import is combined with the entrypoint auto-detected at build
  time, so a shell-form `CMD` becomes arguments to that entrypoint. Use
  `ENTRYPOINT` (or set `entrypoint`) when the command must run on its own
  (tracked in #206).

To check a converted shell-form command in a real runtime (Linux/WSL with
Skopeo and a running Docker daemon; not part of the default tests or CI):

```bash
PYTHONPATH=src timeout 600 python scripts/check_runtime_image.py --shell-command --base-image python:3.11-slim
PYTHONPATH=src timeout 600 python scripts/check_runtime_image.py --shell-command --shell '["/bin/bash", "-c"]'
```

It imports a quoted shell-form `ENTRYPOINT`, builds the image, imports it into
Docker, runs it and compares the exact output.

## Parsing

Parser directives (`# syntax=`, `# escape=`), comments, line continuations
(including comments inside a continuation), JSON and shell forms, and heredoc
bodies are handled. Only the final stage of a multi-stage build is imported.

## Warnings

Nothing is dropped silently. Each of these produces a `WARNING` line with the
Dockerfile line number: `RUN`, `COPY`/`ADD` (including `--from`), `HEALTHCHECK`,
`SHELL` (even when applied), `ONBUILD`, `MAINTAINER`, unknown instructions,
non-final stages (including their `VOLUME`/`STOPSIGNAL`), `FROM scratch`, UDP
ports, undefined variables, invalid `STOPSIGNAL`/`VOLUME` values and
shell-form commands on distroless bases.

Because `RUN` is never executed, install dependencies on the host and use
`include_deps = true` / `requirements_file`; project files are selected with
`include_paths` / `exclude_paths` rather than `COPY`.
