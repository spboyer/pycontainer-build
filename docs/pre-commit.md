# Configuration validation with pre-commit

The optional `pycontainer-validate-config` hook runs
`pycontainer validate config --` on selected filenames. It uses pre-commit's
isolated Python environment; neither pre-commit nor its dependencies are core
runtime dependencies. Python 3.11 or newer is required.

## Install from pinned source

Until a package release is available, use this commit-pinned source definition
in your repository's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/spboyer/pycontainer-build
    rev: 8eca986f536e4ea19cde83cc7112b1bb34c3cae7
    hooks:
      - id: pycontainer-validate-config
```

Install pre-commit separately, install the Git hook, and check existing files:

```bash
uv tool install pre-commit==4.5.1
pre-commit install --install-hooks
pre-commit run pycontainer-validate-config --all-files
```

Installation can use the network to fetch the pinned repository, build tools,
and Python packages. Install the environment before disconnecting. Keep the
pinned commit rather than a moving branch; update it deliberately when adopting
a newer hook. No PyPI release of `pycontainer-build` is required.

## Select configurations

By default, only regular files named `pycontainer.toml`, at any repository
depth, are selected. To opt specific `pyproject.toml` files into validation,
override the hook's `files` expression:

```yaml
    hooks:
      - id: pycontainer-validate-config
        files: '(^|/)pycontainer\.toml$|^services/(api|worker)/pyproject\.toml$'
```

Use this only for projects with `[tool.pycontainer]` settings. A selected file
that defines neither `[build]` nor `[tool.pycontainer]` fails validation rather
than falling back to defaults, so do not select unrelated `pyproject.toml`
files. The hook does not scan TOML contents to select files. `exclude`
can narrow selection further. Symlinks and deleted files are excluded by
pre-commit's regular-file filtering.

Pre-commit normally checks staged selected files. `--all-files` checks tracked
selected files; no match means the hook is skipped, not a validation of defaults.
To select files explicitly from the repository root:

```bash
pre-commit run pycontainer-validate-config --files "first service/pycontainer.toml" "second service/pyproject.toml"
```

Quote paths with spaces in your shell. For a filename starting with `-`, pass
`./-name.toml` to pre-commit so its own option parser does not interpret it.
The hook's `--` protects filenames from the validator's option parser.
Filenames remain separate arguments, never shell-expanded or concatenated.
Do not set hook `args`: this filename-only hook places arguments after `--`,
so overrides would be interpreted as filenames. Put settings in the selected
TOML files, or invoke the validation CLI directly for overrides.

Run from the consumer repository root. Relative context settings retain the
build CLI's repository-root interpretation; they are not automatically rebased
to the configuration file's directory.

## Diagnostics and offline limits

The validator checks every supplied path and preserves source-qualified
diagnostics. It exits 0 when all are valid and 2 for invalid configurations,
missing explicit paths, or usage errors. Pre-commit displays a failed hook's
exit code and output, but its overall command normally exits 1 for hook failure.
A missing `pycontainer` executable is reported as an executable-not-found
failure; reinstall the hook environment rather than treating it as success.
Deleted files filtered out by pre-commit are not passed to the validator;
check a missing path explicitly with `pycontainer validate config -- PATH`.

Once installed, validation does not build, pull, push, access the network, or
mutate pycontainer's cache or build output. Pre-commit itself manages its own
environment, cache, logs, and Git staging operations; those are not image
builder mutations. A changed pin, missing environment, or added dependency
can trigger installation again and require network access.

Offline validation cannot prove registry access, dependency resolution, image
startup, or successful building. See [the validator's checks and limitations](validate-config.md).

## Contributor checks

Hook tests are optional development tooling, installed separately from the
locked core test environment:

```powershell
uv sync --locked --group test --group build
uv pip install --python .venv\Scripts\python.exe -r tests\pre-commit-requirements.txt
.venv\Scripts\python.exe -m pytest tests\test_pre_commit_hook.py tests\test_validate_config.py -q
```

Without that separate installation, pytest explicitly skips the hook test
module. CI installs the pinned pre-commit tool and runs it on Linux and Windows.
The tests install the actual Python-language hook from a temporary local Git
repository using bundled virtualenv tools, with package-index access disabled.
They then run the installed console script and real pre-commit command against
multiple paths, block sockets and builder/cache/registry construction, and
compare repository and home files before and after execution.
