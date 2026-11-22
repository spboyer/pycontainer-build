# Build preview

`pycontainer preview` answers "what would this build change?" before you build.
It plans the build from the same configuration `build` uses, serializes the
planned application layer in memory with the builder's own tar writer, and
compares the result with one explicitly selected baseline image.

```text
pycontainer preview --baseline IMAGE [build flags] [--json]
```

## Selecting a baseline

The baseline is never guessed:

| Baseline | Example | Network |
|----------|---------|---------|
| OCI layout directory | `dist/image`, `oci:./out:v1`, `oci:./out@sha256:...` | none |
| `docker save` archive | `docker-archive:./app.tar`, `docker-archive:./app.tar:app:v1` | none |
| Registry image | `docker://ghcr.io/me/app:v1`, `docker://ghcr.io/me/app@sha256:...` | registry only |

A bare name such as `app:v1` is rejected; prefix registry images with
`docker://`. A multi-platform index is resolved for the planned `--platform`
(default `linux/amd64`); a missing, duplicated, or mismatched platform is an
error.

Registry baselines use the same credential providers as `build --push`.
Credentials are sent only to the registry origin and are never forwarded on
redirects. Manifests are capped at 16 MiB and blobs are verified against their
descriptor digest and size.

## What is read and written

Only the baseline's manifest, config, and final layer are read. The final
layer's DiffID is verified while it is streamed, with the same entry, size and
path-safety limits as `pycontainer inspect`. Temporary files live in one
temporary directory that is removed before the command returns. Preview never
writes to the build context, output directory, or layer cache, and a local
baseline never opens a network connection. When the planned base is a local
`oci:`/`docker-archive:` image, its config is read offline too; a registry base
is resolved only for a `docker://` baseline.

No user code, `pip`, or container runtime is executed.

## Provenance

File-level changes depend on identifying the baseline's application layer:

| Level | Evidence | Reported |
|-------|----------|----------|
| `exact` | The planned app layer DiffID equals the baseline's final DiffID (reproducible builds only). | No file changes. Application is `identical`. |
| `positional` | The planned base DiffIDs are the baseline's lower layers, followed by one or two layers, and the final layer has the shape of an app layer. | Added, modified, and deleted files; dependency layer `identical` or `removed`. |
| `inferred` | Only the final layer's shape matches: regular files, symlinks and hardlinks under the baseline `WorkingDir`, without directories or whiteouts. | File changes marked `inferred`; application and dependencies `unknown`. |
| `none` | No application layer could be identified. | Files and application `unknown`. |

Files are compared by type, content digest, size, mode, owner, and link target.
A `WorkingDir` change appears as deleted plus added paths.

## Settings

When the planned base config is known, the planned image config is computed
exactly as `build` computes it, and `WorkingDir`, `Entrypoint`, `Cmd`, `User`,
`ExposedPorts`, each `Env` variable, and each label are compared. When the base
is not resolved, only explicitly configured values are compared; inherited
values are `unknown`. Environment values are always shown as `<redacted>`, so
only the status reveals that a value changed.

## Accuracy limits

- Base layers are compared by the DiffIDs their configs declare; base blobs
  are not downloaded or verified.
- With `--include-deps`, dependencies are resolved at build time, so the
  dependency component and the `PATH`/`PYTHONPATH` settings are `unknown`.
- Non-reproducible builds (`--no-reproducible`) embed modification times and
  can reuse a cached layer, so provenance is never `exact` and the application
  component is `unknown` unless file changes are found.
- Modification times are not compared.
- The exit code is `0` for a report and `1` for any error; errors never
  produce a partial report.
