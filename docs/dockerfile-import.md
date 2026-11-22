# Dockerfile import

`pycontainer import` converts an existing Dockerfile into `[tool.pycontainer]`
settings and prints a migration report. It uses only the standard library and
never runs Docker.

```powershell
pycontainer import Dockerfile                          # print report and snippet
pycontainer import Dockerfile --output pyproject.toml  # write a new file
pycontainer import Dockerfile -o pyproject.toml --force
pycontainer import Dockerfile --build-arg PY=3.12
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

Shell-form `CMD`/`ENTRYPOINT` become `["/bin/sh", "-c", "<command>"]`.
`ARG`/`ENV` substitution (`$VAR`, `${VAR}`, `${VAR:-default}`, `${VAR:+alt}`)
is applied; `--build-arg KEY=VALUE` overrides ARG defaults.

## Parsing

Parser directives (`# syntax=`, `# escape=`), comments, line continuations
(including comments inside a continuation), JSON and shell forms, and heredoc
bodies are handled. Only the final stage of a multi-stage build is imported.

## Warnings

Nothing is dropped silently. Each of these produces a `WARNING` line with the
Dockerfile line number: `RUN`, `COPY`/`ADD` (including `--from`), `HEALTHCHECK`,
`VOLUME`, `SHELL`, `STOPSIGNAL`, `ONBUILD`, `MAINTAINER`, unknown instructions,
non-final stages, `FROM scratch`, UDP ports and undefined variables.

Because `RUN` is never executed, install dependencies on the host and use
`include_deps = true` / `requirements_file`; project files are selected with
`include_paths` / `exclude_paths` rather than `COPY`.
