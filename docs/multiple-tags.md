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

- Tags must match the registry tag syntax (`[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`).
- Every full reference must target the same registry and repository as the
  primary tag; otherwise the build fails with a message naming both.
- Digest references are rejected.
- Duplicates are removed.

## Output

The layout's `index.json` has one descriptor per tag, pointing to a manifest
for single-platform builds or an image index for multi-platform builds. Every
descriptor has the same digest and size, and its
`org.opencontainers.image.ref.name` annotation is the reference to select.
Names use the primary's repository spelling. For example,
`--tag myapp:v1.2.3 --tag stable` produces
`myapp:v1.2.3` and `myapp:stable`, and `--tag ghcr.io/me/app:v1.2.3
--semver-tags` produces `ghcr.io/me/app:v1.2.3`, `ghcr.io/me/app:v1.2` and
`ghcr.io/me/app:v1`. Single-platform layouts also name the primary descriptor
at the index level.

Standard OCI tools select an image by that annotation:

```bash
skopeo inspect oci:dist/image:ghcr.io/me/app:v1.2.3
skopeo copy oci:dist/image:ghcr.io/me/app:v1 docker-daemon:ghcr.io/me/app:v1
```

With a single tag, `oci:dist/image` without a reference also works. With
several tags, standard tools such as Skopeo require an explicit reference,
even though every descriptor points to the same image. Bare tag names such as
`oci:dist/image:stable` are not descriptor references.

Some valid registry tags do not satisfy the OCI reference-name grammar. For
these, the layout reference is `pycontainer-` followed by lowercase hexadecimal
UTF-8 bytes of the complete reference in the primary's repository spelling.
For example, `myapp:_dev` becomes `pycontainer-6d796170703a5f646576`:

```bash
skopeo inspect oci:dist/image:pycontainer-6d796170703a5f646576
```

In the unlikely event an encoded name collides with another descriptor name,
`0` is appended until it is unique. Inspect `index.json` for the exact
reference. Registry tags and `refs/tags` filenames retain their original values.

The layout also gets one `refs/tags/<tag>` file per tag, all pointing at the
same manifest or index digest; standard tools do not read these files. After a push,
`builder.pushed_refs` lists every pushed reference. Registry pushes publish
each tag separately and are unaffected by the layout index. For multi-platform
pushes, a later tag failure reports confirmed, failed or unknown-outcome, and
unattempted references; previously pushed tags are not rolled back.
