# Offline configuration validation

`pycontainer validate config` checks the effective build configuration without
building an image. It uses the same resolver as `pycontainer build`, so both
commands apply identical defaults, precedence and checks.

```bash
pycontainer validate config [PATH ...] [OVERRIDES]
pycontainer validate config -c PATH [OVERRIDES]
```

- **No path:** validates the defaults `build` uses without `--config`,
  including the base image detected from `requires-python` in the context's
  `pyproject.toml`.
- **Positional paths:** every file is validated and all diagnostics are
  reported; one invalid file does not stop the others.
- **`-c`/`--config`:** validates exactly one file. Repeating `-c` or mixing it
  with positional paths is a usage error.
- **Overrides:** the effective-config options accepted by `build` (`--tag`,
  `--semver-tags`, `--latest`, `--base-image`, `--context`, `--include`,
  `--exclude`, `--no-default-excludes`, `--no-cache`, `--cache-dir`,
  `--cache-from`, `--cache-to`, `--include-deps`, `--requirements`,
  `--platform`, `--no-reproducible`, `--sbom`, `--system-package`,
  `--no-detect-system-packages`) take precedence over file values. Push
  credentials and operational flags
  (`--push`, `--registry`, `--username`, `--password`, `--no-progress`,
  `--verbose`, `--dry-run`) are rejected.

The shared build options also include `--build-arg`, dependency lockfile and
type-stub options, registry transport settings, metadata and release-note
options, scanner configuration, `--stop-signal` and `--volume`. These settings
are resolved offline; scanners are not executed, release-note contents are not
embedded, and no dependency or base-image content is loaded. History recording
flags are operational and are rejected.

Declared build arguments resolve before configuration construction, including
platform-dependent security checks. Repeatable overrides use the last value.
Undeclared inputs and missing required values fail with exit 2. Diagnostics
redact nonempty argument values, including values within success/error source
path labels. Only matching value substrings are replaced.
This does not change the direct Python API:
omitted `BuildConfig.build_args` preserves literal dollars; an explicitly
provided table, including an empty one, enables interpolation.

`pycontainer validate` is a namespace: a bare `validate` is a usage error.
`validate image` is reserved for image linting and is not yet available.

## Checks

- Both supported layouts: `[build]` in `pycontainer.toml` and
  `[tool.pycontainer]` in `pyproject.toml`; a file defining both is rejected.
- A named file that defines neither table (for example a misspelled `[biuld]`
  or `[tool.pycontainer]` missing from `pyproject.toml`) is an error, not a
  silent fall-back to defaults. `build --config` applies the same rule. Only
  omitting every path validates the build defaults.
- Missing files, malformed TOML, unknown keys, wrong TOML types and keys that
  normalize to the same setting.
- Semantic checks shared with `build`: SBOM format, system package names, tag
  and base-image references, platform format, relative include/exclude and
  requirements paths, an existing context directory, and the supported
  dependency-staging platforms when `include-deps` is set. Registry cache
  specifications and their reproducible-build requirement are also checked.

Each diagnostic names its source file and where the offending value came from:
a table field such as `[tool.pycontainer].platform`, a CLI option such as
`CLI option --platform`, or a default.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Every configuration is valid |
| 2 | Invalid configuration, missing file, file without a supported table, conflicting tables or usage error |

## Not performed

Validation is offline. It never constructs the image builder, acquires cache
locks, contacts a registry or writes files, so it cannot verify:

- base image and remote-cache availability, platform match, registry
  authentication, artifact support or push;
- dependency source selection, wheel installation or target Python ABI;
- OCI import or container startup;
- build-context file selection, framework defaults or entrypoint detection.

A successful validation is therefore not proof that a build will succeed.

An optional [pre-commit hook](pre-commit.md) validates selected configuration
files using this same offline command.
