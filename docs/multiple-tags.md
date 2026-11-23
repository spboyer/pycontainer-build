# Multiple tags in a single build

One build can publish the same image under several tags. Layers, the config
blob, and the manifest are built once; with `--push`, blobs are uploaded once
and the manifest is pushed once per tag.

## CLI

```bash
pycontainer build --push \
  --tag ghcr.io/me/app:v1.2.3 \
  --tag ghcr.io/me/app:stable
```

`--tag` is repeatable. The first value is the primary tag (used for the build
result and as the default push target) and must be a full reference. Later
values may be full references in the same repository or bare tags, for example
`--tag ghcr.io/me/app:v1 --tag v2`.

### Semantic version expansion

```bash
pycontainer build --tag ghcr.io/me/app:v1.2.3 --semver-tags --latest
```

| Flag | Effect |
|------|--------|
| `--semver-tags` | `v1.2.3` also gets `v1.2` and `v1` (a `v` prefix is optional) |
| `--latest` | also adds `latest` (opt-in, never implicit) |

Pre-release versions such as `v1.2.3-rc1` are not expanded. Using
`--semver-tags` when no tag looks like `MAJOR.MINOR.PATCH` is an error.

## Configuration file

```toml
[build]
tag = "ghcr.io/me/app:v1.2.3"
tags = ["stable", "ghcr.io/me/app:prod"]
semver-tags = true
latest-tag = true
```

On the command line, any `--tag` replaces both `tag` and `tags` from the file.

## Validation

- Tags must match the OCI tag syntax (`[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`).
- Every full reference must target the same registry and repository as the
  primary tag; otherwise the build fails with a message naming both.
- Digest references are rejected.
- Duplicates are removed.

## Output

The OCI layout gets one `refs/tags/<tag>` file per tag, all pointing at the
same manifest or index digest. After a push, `builder.pushed_refs` lists every
pushed reference. For multi-platform index pushes, it is reset at the start of each
attempt and records confirmed successes as they happen. If a tag push fails,
the error reports confirmed pushes, the failed or unknown-outcome reference,
and references not attempted, regardless of progress output; tags are not
rolled back. A single `--tag` behaves exactly as before.

For multi-platform builds, the layout `index.json` has one image-index
descriptor per configured tag. Each has the same digest and size, and an
`org.opencontainers.image.ref.name` annotation holding the full configured
reference (for example `registry.example/team/app:stable`). Because several
descriptors exist, select with an explicit reference such as
`oci:<layout>:registry.example/team/app:stable`. Hermetic tests check this
descriptor shape; a Skopeo selection test runs only when Skopeo is installed.
The normalized local reference can differ from the input spelling, and
registry-valid tags such as `_dev`, `v1_`, or `v1..2` may not be valid OCI
reference selectors. [#185](https://github.com/spboyer/pycontainer-build/pull/185)
tracks consistent, collision-safe local names; registry pushes are unaffected.
