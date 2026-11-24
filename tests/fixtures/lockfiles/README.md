# Lockfile fixtures

Each directory holds a small project (`lockdemo`) and the lock written for it
by the real tool: `poetry lock`, `pipenv lock`, `pdm lock`, and `uv lock`. The
projects share the same dependencies so tests can compare target selection
across formats:

- `click`, plus `colorama` only on Windows, which a Linux target must drop
- `httpx[socks]`, an extra that pulls in transitive dependencies
- a `python_version` fork of `iniconfig` (2.0.0 below 3.12, 2.1.0 from 3.12)
- a dev group (`pluggy`) that must never be staged

The locks were generated through a package mirror. Their index and artifact
URLs were then rewritten to `https://pypi.org/simple` and
`https://files.pythonhosted.org/packages/<filename>`; versions and hashes were
left unchanged. Each lock was rechecked afterwards with `poetry check --lock`,
`pipenv verify`, `pdm lock --check` and `uv lock --check`.

`.gitattributes` disables line-ending conversion so the files stay identical
to the tool output. A manifest edit makes its lock stale, so relock with the
tool and run the same check again.
