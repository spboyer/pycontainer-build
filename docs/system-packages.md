# System packages

Some Python packages load native libraries at runtime (`psycopg2` needs
`libpq`, `lxml` needs `libxml2`, and so on). pycontainer-build declares and
verifies these requirements; it does not install them.

## Design decision

The builder has no Docker daemon and cannot execute `RUN apt-get install` or
`apk add`. Fetching `.deb`/`.apk` files directly would require resolving the
distro dependency graph and repository indexes, verifying signed metadata,
handling maintainer scripts, and (for modern Debian/Ubuntu) `zstd`-compressed
archives that the Python 3.11 standard library cannot read. That cannot be done
correctly with zero runtime dependencies, so this feature is a validated
declaration plus a verification report:

1. Declare packages explicitly or let common ones be detected.
2. The pulled base image layers are applied in order, honoring OCI whiteouts
   and opaque directories, and the effective package database is inspected
   (`/var/lib/dpkg/status` and `/var/lib/dpkg/status.d/*` for Debian/Ubuntu,
   `/lib/apk/db/installed` for Alpine; distro from `os-release`).
3. A warning is logged for each required package the base image lacks, or
   lists every required package as unverified when the effective database
   cannot be established.

The build is never failed and the image is never modified. To obtain a missing
package, use a base image that already contains it (for example a custom image
built once with your package manager, then used as `base_image`).

## Configuration

```toml
[build]
system-packages = ["libpq5", "libssl3"]
detect-system-packages = true   # default
```

CLI:

```powershell
pycontainer build --system-package libpq5 --system-package libssl3
pycontainer build --no-detect-system-packages
```

Names are validated (lowercase letters, digits, `+ . _ -`). Package names are
distro specific: use `libpq5` on Debian and `libpq` on Alpine.

## Auto-detection

Requirements are read from the configured requirements file and
`[project].dependencies` in `pyproject.toml`. Runtime libraries are mapped per
distro family:

| Python package | Debian/Ubuntu | Alpine |
| --- | --- | --- |
| `psycopg2` | `libpq5` | `libpq` |
| `mysqlclient` | `libmariadb3` | `mariadb-connector-c` |
| `lxml` | `libxml2`, `libxslt1.1` | `libxml2`, `libxslt` |
| `pyodbc` | `unixodbc` | `unixodbc` |
| `cffi` | `libffi8` | `libffi` |

Wheels that bundle their libraries (`psycopg2-binary`, `pillow`) are not
mapped. Detection needs a recognized base distro and is skipped otherwise.

## Limits

- Build-time packages from the issue's example (`gcc`, `build-essential`,
  `libpq-dev`) have no effect: dependencies are staged as target-compatible
  wheels and nothing is compiled in the image.
- Virtual or provided packages are not resolved; a package supplied only via
  `Provides` is reported missing.
- If the base distro cannot be determined, requirements are listed as
  unverified in a warning.
- Verification never reports a package present from stale or partial data.
  Requirements are unverified when a base layer is unreadable, corrupt, or
  uses a compression this Python cannot read (for example zstd before Python
  3.14); when an entry path contains `..`; when the package database, or a
  directory above it, is a symlink, hard link or other non-regular entry;
  when metadata exceeds inspection limits (16 MiB per file, 64 MiB total,
  10,000 `status.d` records); or when the distro's package database is
  absent. Debian packages count as installed only with an `ok installed`
  status in every record that lists them.

## Planned installation (not available)

Installing packages with the distro's own `apt` or `apk` is planned in #31.
It must run inside the opt-in isolated execution backend from #22, never on
the host, and that backend does not exist yet. No configuration option enables
installation, and detected packages will never be installed implicitly.

`pycontainer.system_install` contains only fail-closed groundwork that runs no
package manager: checks for apt settings that weaken signature verification,
and parsers for offline resolution output. It does not download packages or
repository metadata. See the
[installation design](plans/2026-11-22-system-package-installation.md) for
the planned flow, the trust boundaries and the remaining blockers.
