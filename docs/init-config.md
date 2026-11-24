# Configuration templates and wizard

`pycontainer init` creates a `[build]` configuration file for
`pycontainer build --config`. It has three mutually exclusive modes:

```bash
pycontainer init --list-templates
pycontainer init --template NAME [--output PATH] [--force] [OPTIONS]
pycontainer init --interactive
```

Generation is offline. It reads only local project metadata (the context's
`pyproject.toml` for `requires-python`), never builds, pulls, pushes, touches
the layer cache or contacts a registry, network or container daemon, and does
not execute template or project content.

## Templates

| Template | Entrypoint | Port | `include_deps` |
|----------|------------|------|----------------|
| `fastapi` | `python -m uvicorn <app> --host 0.0.0.0 --port <port>` | 8000 | `true` |
| `flask` | `python -m flask --app <app> run --host=0.0.0.0 --port=<port>` (Flask 2.2+) | 5000 | `true` |
| `generic` | `python -m <module>` only when `--app` is given; otherwise the builder's existing auto-detection | none unless `--port` | `false` |

Every template writes `tag` (default `local/<context directory name>:latest`),
`base_image` (default `python:<requires-python>-slim`, falling back to
`python:3.11-slim`) and `context_dir` (default `.`). Unknown template names are
a usage error (exit 2).

Template options, all validated before the output is published:

| Option | Meaning |
|--------|---------|
| `--app` | `module:app` for `fastapi`/`flask` (defaults `main:app`, `app:app`); `package.module` for `generic` |
| `--port` | Exposed port, 1-65535 |
| `--tag`, `--base-image`, `--context`, `--platform` | Same meaning as for `build` |
| `--include-deps` / `--no-include-deps` | Override the template's dependency staging default |
| `--output`, `-o` | Relative output path (default `pycontainer.toml`) |
| `--force` | Replace an existing output file |

Omitted options use their defaults; explicitly supplied empty values are
invalid. In the interactive wizard, pressing Enter accepts the prompt default.

The `fastapi` and `flask` templates enable dependency staging, so the build
needs a supported lockfile, `requirements.txt` or a virtualenv in the context
containing the required packages (including `uvicorn` or `flask`). `init`
prints a note when no source is present; it does not validate the source.
Entrypoints use `python -m` so they do not depend on console scripts being on
`PATH`.

### Customization

The output is plain TOML: edit it to add any supported `[build]` setting (for
example `[build.env]`, `[build.labels]`, `workdir` or `generate_sbom`), then run
`pycontainer validate config pycontainer.toml`. CLI options to `build` still
override file values. Templates are versioned with pycontainer; regenerating
with `--force` replaces local edits, so keep customizations under version
control.

### Relationship to framework detection

Templates create configuration only; they do not add application code and do
not perform new framework detection (owned by #56 and its follow-ups). Because
templates set `entrypoint` and `exposed_ports` explicitly, the builder's
framework detection does not replace them; it may still add its existing
`framework` label. The `generic` template without `--app` leaves the
entrypoint to existing detection.

## Interactive wizard

`pycontainer init --interactive` is never started implicitly. It requires both
standard input and output to be a terminal; otherwise it prints an error and
exits 2 without prompting, so scripts and CI must use `--template`. It accepts
no other options.

Prompts, in order (Enter accepts the bracketed default):

1. Template (`fastapi`, `flask`, `generic`)
2. Build context directory
3. Image tag
4. Base image
5. Application target (`generic`: blank keeps auto-detection)
6. Exposed port (`generic`: blank for none)
7. Stage dependencies (`y`/`n`)
8. Target platform
9. Output file
10. Confirmation after a preview of the TOML (`y`/`yes` writes; default is no)

Each answer is checked immediately with the same resolver as
`validate config`; an invalid answer is explained and asked again, and three
invalid answers to one prompt abort with exit 2. The wizard never asks for
registry credentials, tokens, environment variables or labels.

## Output and safety

- The output path must be relative to the current directory, end in `.toml`,
  use only letters, digits, `.`, `_` and `-` in each component, and stay
  inside the current directory. Absolute, drive-qualified or rooted paths,
  `..`, links, directories, Windows reserved device names (`NUL`, `CON`,
  `COM1`, ...), missing parent directories and `pyproject.toml`
  are rejected (exit 2); `init` never creates missing output directories or
  edits project metadata. The parent is checked again immediately before
  staging and publishing.
- If the context's `pyproject.toml` defines `[tool.pycontainer]`, it is left
  untouched and `init` notes that `build --config <output>` reads only the
  generated file.
- The file is staged on the output filesystem, validated with the real
  `build --config` loader and resolver, and published atomically. Without
  `--force` an existing file is never replaced, including one created
  concurrently (exit 1). This requires hard-link support and permission; if
  unavailable, publication fails explicitly (exit 2) rather than writing
  the output in place. The wizard never replaces files and asks for another
  path instead.
- Path checks do not protect against a directory or staging file being replaced
  concurrently; generate files only in a trusted working directory.
- End of input or a declined confirmation (exit 1) publishes nothing. Ctrl+C
  (exit 130) before publication leaves the destination unchanged; an
  interruption during publication may leave the complete output, but never
  a partial output file. If the OS prevents staging-file cleanup, the error is
  reported and the staging file may remain.
- `context_dir` is written as given and resolved relative to the directory
  `build` runs in, not the configuration file's location; run `build` from the
  directory where you ran `init`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | File written, or templates listed |
| 1 | Existing file refused, cancelled by end of input or declined confirmation |
| 2 | Usage error, invalid value or answer, unsafe path, no terminal, or publication failure |
| 130 | Interrupted with Ctrl+C |

## Limitations

- Offline validation does not prove a build or container start succeeds; see
  [configuration validation](validate-config.md#not-performed).
- No real container startup of the generated entrypoints is part of the test
  suite; use the opt-in runtime check described in `AGENTS.md`.
- Templates cannot merge into `pyproject.toml`; use
  [Dockerfile import](dockerfile-import.md) or copy settings into
  `[tool.pycontainer]` by hand.
