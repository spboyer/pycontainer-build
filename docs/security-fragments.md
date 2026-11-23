# Runtime security fragments

pycontainer can emit **opt-in deployment fragments** that describe runtime
security controls for a Linux container: Linux capabilities, seccomp,
AppArmor, SELinux and a read-only root filesystem.

> **These fragments are not enforced by the image.** OCI image configuration
> has no field that can enforce capabilities, seccomp, AppArmor, SELinux or a
> read-only root, and pycontainer does not add labels or annotations that claim
> otherwise. Controls take effect only when you apply a fragment to the
> Kubernetes Pod or OCI runtime that starts the container, and only on nodes
> that support the requested control.

When `security` is not configured, nothing is written and the image layout is
byte-for-byte identical. When it is configured, the image layout (manifest,
config, layers, `index.json`) is still identical; only extra files are written
beside it.

## Output

After a successful build, fragments are written to `<output_dir>/security/`:

| File | Target | Content |
|------|--------|---------|
| `kubernetes-security-context.json` | Kubernetes `core/v1` | A container `securityContext` object |
| `oci-runtime-config.fragment.json` | OCI runtime-spec 1.3.0 | A partial `config.json` to merge into a runtime bundle |

Only the targets you configure are written. Fragments from an earlier build
that are no longer configured are removed; other files in `security/` are left
alone. `--dry-run` lists the fragments it would write and writes nothing.
Invalid settings fail the build before any base image is pulled.
Multi-platform index builds also write one set of fragments beside the layout,
not inside the platform images. Choose profiles appropriate for each target
runtime where the fragments will be applied.

## Configuration

Settings live in `pycontainer.toml` under `[build.security.kubernetes]` and
`[build.security.oci]`, or in `pyproject.toml` under
`[tool.pycontainer.security.kubernetes]` and `[tool.pycontainer.security.oci]`.
From Python, pass the same structure as `BuildConfig(security={...})`. There are
no CLI flags. Keys may use hyphens or underscores.

Each target uses its own native semantics. pycontainer never translates one
target's settings into the other's, because the models differ (for example
Kubernetes adds/drops capabilities relative to the runtime default, while OCI
lists every capability set explicitly). Configure each target you deploy to.

### Kubernetes

```toml
[build.security.kubernetes]
read_only_root_filesystem = true

[build.security.kubernetes.capabilities]
drop = ["ALL"]
add = ["NET_BIND_SERVICE"]

[build.security.kubernetes.seccomp]
type = "RuntimeDefault"            # or "Localhost" with localhost_profile

[build.security.kubernetes.apparmor]
type = "Localhost"
localhost_profile = "k8s-myapp"    # profile name loaded on the node

[build.security.kubernetes.selinux]
type = "container_t"
level = "s0:c123,c456"
```

Produces:

```json
{
  "appArmorProfile": {"localhostProfile": "k8s-myapp", "type": "Localhost"},
  "capabilities": {"add": ["NET_BIND_SERVICE"], "drop": ["ALL"]},
  "readOnlyRootFilesystem": true,
  "seLinuxOptions": {"level": "s0:c123,c456", "type": "container_t"},
  "seccompProfile": {"type": "RuntimeDefault"}
}
```

Rules:

- `capabilities.add` / `drop` use bare Kubernetes names (`NET_BIND_SERVICE`,
  not `CAP_NET_BIND_SERVICE`). `ALL` is allowed only in `drop`. Duplicates, a
  name in both lists, unknown names and two empty lists are rejected.
- `seccomp.type` and `apparmor.type` are `RuntimeDefault` or `Localhost`.
  `localhost_profile` is required for `Localhost` and rejected otherwise.
  `Unconfined` is rejected: these fragments only tighten defaults.
- A seccomp `localhost_profile` is relative to the kubelet seccomp root
  (`/var/lib/kubelet/seccomp` by default) and may not be absolute, use `\` or
  contain `..`.
- `selinux` accepts `user`, `role`, `type` and `level`.

### OCI runtime-spec

```toml
[build.security.oci]
seccomp_profile = "security/seccomp.json"  # relative to the build context
apparmor_profile = "myapp"
selinux_label = "system_u:system_r:container_t:s0:c1,c2"
mount_label = "system_u:object_r:container_file_t:s0:c1,c2"
read_only_root = true

[build.security.oci.capabilities]
bounding = ["CAP_NET_BIND_SERVICE"]
effective = ["CAP_NET_BIND_SERVICE"]
permitted = ["CAP_NET_BIND_SERVICE"]
inheritable = []
ambient = []
```

Produces (fields map to `process.capabilities`, `process.apparmorProfile`,
`process.selinuxLabel`, `linux.seccomp`, `linux.mountLabel` and
`root.readonly`):

```json
{
  "linux": {"mountLabel": "...", "seccomp": {"defaultAction": "...", "syscalls": []}},
  "process": {
    "apparmorProfile": "myapp",
    "capabilities": {"ambient": [], "bounding": ["CAP_NET_BIND_SERVICE"], "...": []},
    "selinuxLabel": "system_u:system_r:container_t:s0:c1,c2"
  },
  "root": {"readonly": true}
}
```

Rules:

- `capabilities` must list all five sets using `CAP_` names, because OCI has
  no implicit baseline. The kernel invariants are enforced: effective ⊆
  permitted ⊆ bounding, inheritable ⊆ bounding, and ambient ⊆ permitted ∩
  inheritable.
- `seccomp_profile` points at a JSON file inside the build context containing
  an OCI `LinuxSeccomp` object (`defaultAction`, optional `defaultErrnoRet`,
  `architectures`, `flags`, `syscalls`). It is validated and embedded inline.
  The file must stay inside the context, be at most 1 MiB and contain no
  duplicate keys. Unknown fields, `SCMP_ACT_NOTIFY`, `listenerPath` and
  `listenerMetadata` are rejected, as is `errnoRet`/`defaultErrnoRet` with an
  action other than `SCMP_ACT_ERRNO` or `SCMP_ACT_TRACE`. Docker's
  `seccomp.json` format (`archMap`, `includes`) is not accepted.
- `selinux_label` and `mount_label` must be `user:role:type[:level]` contexts.
- The fragment has no `ociVersion`, `root.path`, `process.args` or mounts; it
  is not a complete bundle configuration.

Unknown keys, empty tables, wrong types and non-Linux `platform` values are
rejected with an error naming the offending setting.

## Applying the fragments

### Kubernetes

Place the object at `spec.containers[].securityContext` of your Pod template,
for example with a kustomize JSON patch:

```yaml
# kustomization.yaml
patches:
  - target: {kind: Deployment, name: myapp}
    patch: |-
      - op: add
        path: /spec/template/spec/containers/0/securityContext
        value: <contents of dist/image/security/kubernetes-security-context.json>
```

Validate before deploying:
`kubectl apply --dry-run=server --validate=strict -f deployment.yaml`.
A Pod Security Admission policy may still reject or require other fields (for
example `runAsNonRoot` or `allowPrivilegeEscalation`), which pycontainer does
not emit.

### OCI runtimes (runc, crun)

Generate a base spec with `runc spec` (or `crun spec`), set `process.args`,
`env`, `cwd` and `root.path` from your image, then merge the fragment. The
fragment's leaf objects **replace** the base values wholesale, which matters
for capabilities: a base spec's capability lists are discarded, not unioned.
The helper does exactly this and rejects any field outside the supported set:

```python
import json
from pycontainer.security import merge_oci_runtime_fragment

spec = json.load(open("bundle/config.json"))
fragment = json.load(open("dist/image/security/oci-runtime-config.fragment.json"))
json.dump(merge_oci_runtime_fragment(spec, fragment), open("bundle/config.json", "w"), indent=2)
```

`docker run` and `podman run` do not read OCI config fragments; use their own
flags (`--cap-drop`, `--security-opt`, `--read-only`) instead.

## Supported versions and node requirements

| Control | Kubernetes | OCI runtime | Node / host requirement |
|---------|------------|-------------|-------------------------|
| Capabilities | all supported versions | runtime-spec 1.0+ | none |
| Read-only root | all supported versions | runtime-spec 1.0+ | none |
| Seccomp `RuntimeDefault` | 1.19+ (`seccompProfile` GA) | n/a | kernel with seccomp; runtime default profile |
| Seccomp `Localhost` / inline | 1.19+ | runtime-spec 1.0+ (runc 1.1+, crun) | profile file under the kubelet seccomp root |
| AppArmor | 1.30+ (`appArmorProfile` field; no annotations are emitted) | runtime-spec 1.0+ | AppArmor enabled; `Localhost` profiles loaded on every node |
| SELinux | all supported versions | runtime-spec 1.0+ | SELinux enabled with a policy defining the labels |

The OCI fragment is validated against the vendored runtime-spec 1.3.0 schema
in tests. If a node lacks the LSM or profile, the result depends on the
runtime: it may refuse to start the container or run it without the control.
Verify on your nodes.

## Verifying on a Linux host

`scripts/check_security_fragments.py` is an opt-in harness that actually
applies the controls. It is not part of the default test suite or CI.

```bash
# Requires Linux, Python 3.11.4+, runc or crun, and network access to pull the base image.
PYTHONPATH=src timeout 900 python scripts/check_security_fragments.py --base-image python:3.12-slim

# Optional controls and Kubernetes:
#   --apparmor-profile <loaded profile>   --selinux-label <label>
#   --kubernetes [--namespace default]    (needs kubectl and pod create access)
```

It builds a probe image, unpacks the OCI layout (gzip or uncompressed layers;
zstd is rejected), merges the OCI fragment into `runc spec` output and runs the
probe as uid 0 and, when run as root, uid 65534. It checks all five capability
sets in `/proc/self/status`, seccomp filter mode and a blocked `mkdir`, `EROFS`
on `/` while `/dev/shm` stays writable, that `CAP_CHOWN` is gone and that
`CAP_NET_BIND_SERVICE` can bind port 80. With `--kubernetes` it runs a
server-side strict dry run and a probe Pod. Each check prints PASS, SKIP or
FAIL; AppArmor, SELinux, the non-root probe in rootless mode and Kubernetes are
reported as SKIP when unavailable. **SKIP is not evidence of enforcement.**

## Limitations

- Linux targets only; Windows and other platforms are rejected.
- No CLI flags; configure through TOML or `BuildConfig`.
- No cross-target mapping; configure Kubernetes and OCI separately.
- `Unconfined` seccomp/AppArmor, seccomp notify (`SCMP_ACT_NOTIFY`) and
  Docker-format seccomp profiles are not supported.
- Other `securityContext` fields (`runAsUser`, `runAsNonRoot`,
  `allowPrivilegeEscalation`, `privileged`, ...) are not emitted.
- The OCI fragment is not a complete bundle and is not consumed by Docker or
  Podman.
