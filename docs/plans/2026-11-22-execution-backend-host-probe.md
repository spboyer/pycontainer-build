# Execution backend host probe (2026-11-22)

Relates to #22, #31 and #63. It addresses the first blocker in the #22
execution backend contract: whether GitHub-hosted runners can host a
separately installed rootless OCI runtime.

## What it records

`.github/workflows/execution-backend-probe.yml` runs on `ubuntu-24.04` (x64)
and `ubuntu-24.04-arm` (arm64). It triggers on `workflow_dispatch` and on pull
requests that change the workflow itself. It checks only, so it never fails on
a missing prerequisite. It changes no host settings, installs nothing, does not
use `sudo` and adds no core dependency. Each run uploads a Markdown report
artifact.

| Contract prerequisite | Probe |
|---|---|
| Unprivileged user namespaces (Ubuntu AppArmor gate) | `apparmor_restrict_unprivileged_*` sysctls, profiles and effective label/capabilities inside `unshare -Ur` |
| Mounts in the namespace, overlay `userxattr`, whiteouts | tmpfs and overlay mount in a private mount namespace, then deleting a lower file and directory |
| Multi-id maps for package installs (#31) | runner `/etc/subuid`/`/etc/subgid` entries, `newuidmap` capabilities, `unshare --map-auto` |
| cgroup v2 limits | own cgroup controllers and writability, `user@UID.service` delegation, a `systemd-run --user` scope with `MemoryMax`/`TasksMax` |
| Emulation (#63) | registered `binfmt_misc` handlers, interpreters and flags (`F` required) |
| Runtime availability and provenance | `crun`/`runc`/`runsc`/`bwrap`/`pasta` presence, package owner, version, `features` JSON, sha256 |
| End-to-end | a throwaway rootless no-op container from the preinstalled runtime |

## Results

Pending the first run.

## Follow-ups kept in the issues

- Alpine has no official package snapshot service, so pinned apk versions can
  disappear (#31).
- Whether `apk add --simulate` output yields deterministic download URLs still
  needs a fixture (#31).
