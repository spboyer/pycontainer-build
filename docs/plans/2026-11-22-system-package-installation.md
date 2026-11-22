# System package installation design (2026-11-22)

Relates to #31 and depends on #22. **Installation is not implemented.** This
document records the planned flow, the evidence behind it, what has been
delivered so far, and the blockers. Verification-only mode
([system-packages.md](../system-packages.md)) is unchanged.

## Trust model

- **Trusted:** the base image pinned by digest, including its package manager,
  its `apt.conf.d` hooks, its methods, and the distro keys it ships. Hooks such
  as Debian's `DPkg::Post-Invoke` cleanup are base-image code. They run inside
  the sandbox and are not rejected.
- **Trusted only with explicit opt-in:** extra repository keys, pinned by
  SHA-256 fingerprint.
- **Untrusted:** every byte fetched from a repository, until the package
  manager has verified it offline inside the sandbox, and all maintainer
  scripts.
- The builder acts as transport only. It never decides authenticity. It
  enforces bounds: allowed hosts, HTTPS, no credentials, size, and when
  available a hash.

## Planned flow

Every step that runs a package manager or a maintainer script goes through the
#22 executor:

- a private rootfs built from the base or stage layers;
- **no host mounts at all**; inputs are copied in with bounds, and outputs come
  back as the resulting OCI layer or as a verified copy-out;
- `network=none`;
- a full 0–65535 id map, failing closed otherwise;
- no inherited environment, credentials or sockets;
- memory, pids and time limits.

1. **Index transport (builder).** Fetch `InRelease` plus per-architecture
   `Packages.xz` for apt, or `APKINDEX.tar.gz` for apk, from the declared
   HTTPS repository hosts with `fetch_untrusted`. Write them into a mirror
   tree with the upstream layout.
2. **Configuration check (sandbox, offline).** Run `apt-config dump` and pass
   its output to `check_apt_config`. Generated sources use a `file:` mirror
   with `Signed-By=<pinned keyring>`. The base image's sources are disabled
   with `Dir::Etc::SourceList` and `Dir::Etc::SourceParts` overrides. For apk,
   use a generated repositories file and a keys directory that contains only
   the pinned keys. The builder never passes `--allow-untrusted`.
3. **Resolve (sandbox, offline).**
   - apt: `apt-get update`, then `apt-get install --print-uris` gives the
     closure. Pass the output to `parse_apt_print_uris`.
   - apk: `apk add --simulate` gives the packages that would be installed, and
     `apk fetch --simulate --recursive --url` gives their mirror paths. Use
     `parse_apk_simulate`, `parse_apk_fetch_urls` and `select_apk_downloads`.
4. **Package transport (builder).** Download each mirror path from its origin
   repository with the apt size bound. Place it in the mirror at that path.
5. **Install (sandbox, offline).** Use the `file:` mirror. A `policy-rc.d`
   that exits with 101 blocks service starts. Capture the filesystem delta,
   including the dpkg or apk database, as the system layer. apt checks each
   `.deb` against the signed index, and apk checks the index and package
   signatures. If a maintainer script needs the network, the build fails and
   names the package. It is never retried with egress.

## Evidence

The evidence was captured on 2026-11-22 in disposable Docker Desktop
containers on Windows x64. These were not the #22 backend, and they installed
no package. The fixtures are in `tests/fixtures/system_install/`.

| Finding | Images |
|---|---|
| `--print-uris` prints `MD5Sum:` or no hash at all. Its hash cannot be used as a builder integrity check. | debian:bookworm-slim (apt 2.6.2), debian:trixie-slim (apt 3.0.4; `debian-security` lines have no hash), ubuntu:24.04 (apt 2.8.4) |
| `apt-get update` against a `file:` mirror with `--network none` works with the archive keyring. A modified `InRelease` gives `BADSIG`, and the wrong keyring gives `NO_PUBKEY`. Both exit 100 with "is not signed". | bookworm |
| `install --download-only` from the `file:` mirror accepts the genuine `libpq5` `.deb`. A single modified byte gives "Hash Sum mismatch" and exits 100. A missing dependency `.deb` fails closed. | bookworm |
| Official images pass `check_apt_config`. They ship `DPkg::Post-Invoke`, `APT::Update::Post-Invoke` and `DPkg::Pre-Install-Pkgs` hooks, so rejecting every hook would reject every official image. | bookworm, trixie, ubuntu 24.04 |
| `apk add --simulate` lists only packages that are not yet installed, as `(i/n) Installing name (version)`, with no URLs. | alpine:3.22 (apk-tools 2.14.13), alpine:3.23 (apk-tools 3.0.9) |
| `apk fetch --simulate --recursive --url` prints the deterministic closure paths for the current index, offline against a local mirror. This resolves the #31 question about `--simulate` URLs. | 3.22, 3.23 |
| An untrusted (`--keys-dir` without keys) or corrupt `APKINDEX` only produces a `WARNING:`, and apk skips that repository. The parsers therefore treat every `WARNING:` or `ERROR:` line as fatal. | 3.22, 3.23 |

## Delivered groundwork

`src/pycontainer/system_install.py`, tested in `tests/test_system_install.py`,
uses the standard library only and has no callers yet:

- `check_apt_config` rejects truthy apt-secure overrides, including
  `Binary::<prog>::` scoped ones:
  - `APT::Get::AllowUnauthenticated`
  - `Acquire::AllowInsecureRepositories`, `AllowWeakRepositories` and
    `AllowDowngradeToInsecureRepositories`
  - an `APT::Sandbox::User` other than `_apt`
  - verifier overrides, including named children: `Acquire::gpgv::Options`,
    `APT::Key::gpgvcommand`, `Dir::Bin::apt-key`, `Dir::Bin::gpg`,
    `Dir::Bin::gpgv` and `Dir::Bin::methods::<method>`; `Dir`, `Dir::Bin` and
    `Dir::Bin::methods` must keep their defaults
  - disabled `Check-Valid-Until` or `Check-Date`, unless the caller opts in
    for a pinned snapshot

  Booleans are read the way apt's `StringToBool` reads them, including C
  integer forms such as `00` and `0x0`. Values that apt does not treat as
  false count as enabled. Unknown output lines fail.
- The apt and apk parsers accept only canonical mirror-relative paths: no
  `..`, no `:` (so no Windows drive prefix), no encoded separators, no
  foreign hosts or URLs, no duplicates, and positive bounded sizes. Lines they
  do not recognize, and malformed URIs, fail.
- `select_apk_downloads` rejects upgrades, downgrades and removals of
  base-image packages for now. It also rejects any simulated package that is
  missing from the fetch closure.
- `fetch_untrusted` accepts HTTPS on port 443 to exact lowercase allowed DNS
  hosts only. It rejects userinfo, queries, fragments and IP literals. It
  inherits no proxy, authorization or cookies. It follows up to 3 redirects,
  checking each one again. Each socket operation has a timeout and the whole
  download has a total deadline (600 seconds by default). Downloads are
  streamed with size caps and checked against an optional size or SHA-256,
  then published atomically. Partial files are removed.

## Blockers and open questions

- **#22 executor contract.** Covers the private rootfs, copy-in and copy-out
  without host mounts, the id map, `network=none`, limits, cleanup, and
  whiteout-correct layer export. Until it lands and is verified on real
  runners, no configuration enables installation and no install command runs.
  The #194 host probe does not establish this. crun 1.14.1 bind-mounts host
  device nodes such as `/dev/null` and `/dev/zero` into user-namespace
  containers, which breaks the no-host-mount boundary. crun is therefore not
  an accepted backend until #22 settles a virtualized-device alternative or
  fails closed.
- **Signed local-repository fixtures.** apt fixtures need `dpkg-deb`,
  `apt-ftparchive` and a throwaway `gpg` key. apk fixtures need
  `abuild-sign`. Both need a lib package, a dependent package and a runtime
  load check. Failure cases: bad signature, missing key, missing package, a
  configuration override, a postinst that needs the network, and rollback.
  Keys are generated during the test and never committed. They must run
  through the executor.
- **Environment proxies.** They are refused rather than inherited. Proxy
  support would need explicit, credential-free configuration.
- **Upgrades of base packages.** apk upgrades currently fail closed. The
  equivalent apt check, parsing `apt-get -s` `Inst` lines, is not implemented.
- **Reproducibility.** apt can pin `snapshot.debian.org` or
  `snapshot.ubuntu.com`, which needs `allow_expired_release`. Alpine has no
  official snapshot service and old versions are removed from branches, so apk
  pins are best effort only.
- **Configuration surface.** Package lists, repositories, keys, and
  build-stage versus runtime-stage placement have not been added. They will
  be designed together with the executor integration.
