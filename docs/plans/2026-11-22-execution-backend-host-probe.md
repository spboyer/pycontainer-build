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
| Runtime-built rootfs | overlay with `userxattr` inside a crun container, whiteout and opaque-directory encoding |
| Limits and cleanup | crun `--cgroup-manager=systemd` limits, OOM enforcement, `kill`/`delete --force` |

Every probe container starts from `runtime spec --rootless` with host `/usr`
bind-mounted read-only, and the overlay test also binds a host temporary
directory read-write. These containers show that the runtime starts and
enforces limits. They are not evidence of a private rootfs, the absence of
host mounts or network isolation, which the backend contract still requires.

## Results

Runs [45053253547](https://github.com/spboyer/pycontainer-build/actions/runs/45053253547),
[45053506038](https://github.com/spboyer/pycontainer-build/actions/runs/45053506038),
[45053797205](https://github.com/spboyer/pycontainer-build/actions/runs/45053797205) and
[45054154688](https://github.com/spboyer/pycontainer-build/actions/runs/45054154688)
show the same results on both architectures: Ubuntu 24.04.5, kernel 7.0
(`-aws` on x64, `-azure` on arm64), and runner uid 1001.

- **User namespaces.** `apparmor_restrict_unprivileged_userns=1`. An
  unconfined process can create a userns, and `unshare --map-auto` gets a
  65536-id map through setuid `newuidmap`. Inside that userns it holds no
  usable capabilities (AppArmor `DENIED ... sys_admin`), so `unshare -Ur`
  cannot write `uid_map` and nothing can mount tmpfs or overlay. Of the
  profiles checked, crun (`/usr/bin/crun`), runc (`/usr/sbin/runc`), podman
  and buildah have a `userns,` rule that grants full capabilities in a new
  userns. Rootfs assembly, overlay mounts and
  cleanup inside the uid map therefore need runtime-launched steps, not the
  unconfined Python builder.
- **Runtimes.**
  - crun 1.14.1 (Ubuntu `crun` package, OCI 1.1.0+dev, seccomp, systemd-user
    cgroups, idmapped mounts) runs a rootless no-op container. crun calls
    `newuidmap` itself; the container map is `0 1001 1` plus
    `1 165536 65536`.
  - The `runc` on `PATH` is containerd.io 1.5.3 at `/usr/bin/runc`, which the
    runc AppArmor profile does not cover. It fails rootless with
    `failed to unshare remaining namespaces: Operation not permitted`.
  - runsc, bwrap and umoci are absent. Backend selection must probe with a
    real no-op container; `features` output and the binary's presence on
    `PATH` are not enough.
- **Subordinate ids.** `runner:165536:65536` appears in both `/etc/subuid` and
  `/etc/subgid`. `newuidmap` and `newgidmap` are setuid-root. The id mapping
  that multi-uid package installs (#31) need is available; no package install
  ran under it.
- **Overlay and whiteouts.** Inside the crun container, with only
  `CAP_SYS_ADMIN` added, overlay with `userxattr` fails with `EACCES`; the
  kernel logs `upper fs does not support tmpfile`. With `CAP_SYS_ADMIN` and
  `CAP_DAC_OVERRIDE`, it mounts on both an ext4 bind mount and a container
  tmpfs; overlayfs creates its work directory with mode 0000. A deleted file
  becomes a 0/0 character-device whiteout, not an xattr. A replaced directory
  gets `user.overlay.opaque=y`. Layer export must map these to `.wh.<name>`
  and `.wh..wh..opq`.
- **cgroups.** The job's own cgroup, `system.slice/hosted-compute-agent.service`,
  is not writable. `user@1001.service` is lingering, with `Delegate=yes` for
  cpu, memory and pids.
  - `crun --cgroup-manager=systemd` works with `DBUS_SESSION_BUS_ADDRESS`
    unset, which is how the job environment leaves it.
  - It applies `memory.max` and `pids.max` in a transient `app.slice` scope.
    A 256 MiB allocation under a 64 MiB limit exits 137, and the kernel logs a
    `CONSTRAINT_MEMCG` oom-kill for that scope.
  - `kill KILL` plus `delete --force` removes the process, the scope and the
    state.
- **Emulation (#63).** `binfmt_misc` has only llvm `lli` and python3.12
  handlers, none with the `F` flag, and no qemu-user handlers. Without
  installing emulation, foreign architectures need native runners.

## Follow-ups kept in the issues

- Alpine has no official package snapshot service, so pinned apk versions can
  disappear (#31).
- Whether `apk add --simulate` output yields deterministic download URLs still
  needs a fixture (#31).
