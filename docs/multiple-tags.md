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
same manifest digest. After a push, `builder.pushed_refs` lists every pushed
reference.

The layout's index currently has only the primary reference on its manifest
descriptor. Skopeo does not use `refs/tags` to select an additional tag, so
`oci:<layout>:<additional-tag>` cannot select it. Use the complete primary
reference (`oci:<layout>:ghcr.io/me/app:v1.2.3`) or omit the reference
(`oci:<layout>`) when the index has one manifest. Registry pushes still publish
each tag separately.
