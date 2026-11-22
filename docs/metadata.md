# Image metadata

Labels and annotations are separate string-to-string maps:

| Setting | OCI location | Inherited from base? |
|---|---|---|
| `labels` | Image config `config.Labels` | Yes |
| `manifest_annotations` | Image manifest `annotations` | No |
| `index_annotations` | Local layout `index.json` `annotations` | No |

Annotations describe the newly built object, so base manifest, index, and
descriptor annotations are not copied. The builder retains its own
`org.opencontainers.image.ref.name` in the local index independently of user
annotations. On the existing single-image path, a registry push sends the image
manifest and config, not the local layout index. Manifest annotations therefore
travel with that push; index annotations remain in the local layout.

## Configuration and precedence

Use the corresponding TOML tables:

```toml
[build.labels]
framework = "custom"
"example.team" = "platform"

[build.manifest_annotations]
"org.opencontainers.image.title" = "myapp"

[build.index_annotations]
"example.layout" = "production"
```

Or use `BuildConfig(labels=..., manifest_annotations=..., index_annotations=...)`.
The repeatable CLI options override individual keys from the file:

```bash
pycontainer build --config pycontainer.toml --label example.team=runtime \
  --manifest-annotation org.opencontainers.image.title=myapp \
  --index-annotation example.layout=production
```

For each label key, precedence is explicit CLI, configuration file,
framework-derived default, then base image. Repeating a CLI key uses its last
value. An explicit empty string is a value, not a deletion. Framework detection
previously overwrote an explicitly configured `framework` label; it now fills
that label only when the user has not supplied it.

Manifest and index annotation maps merge CLI over file independently; they do
not inherit base annotations or copy values between each other or into labels.
Keys must be strings containing at least one non-whitespace character, and
values must be strings; empty values are allowed. CLI arguments split on the
first `=`. Invalid maps are rejected even when another source would otherwise
overwrite them.

`org.opencontainers.image.ref.name` is protected in both user annotation maps:
the builder owns reference metadata, so even supplying the same value is an
error. This restriction does not apply to a config label with that key.

## Visibility and reproducibility

Metadata is distributed with images or layouts and may be visible through
registries. **Never put secrets in labels or annotations.** CLI arguments can
also appear in shell history and process listings. Dry-run diagnostics show
counts rather than metadata values, and registry errors omit response bodies
that could echo metadata.

Label and annotation keys are serialized in sorted order. Consequently, builds
with the same metadata produce the same metadata bytes regardless of input-map
order. Canonicalizing inherited labels may change config and manifest digests
relative to older versions even when label values are unchanged. Manifest
annotations are serialized before the manifest digest and size are calculated;
changing a label or manifest annotation changes the corresponding content
digests. On the single-image path, changing only an index annotation changes
`index.json`, not the manifest digest.

## Integration touchpoints

- [#140](https://github.com/spboyer/pycontainer-build/issues/140) owns release
  notes. Before framework defaults are applied, use
  `merge_image_labels(labels=explicit, generated_labels=generated)` to reject
  generated keys that collide with explicit CLI or file labels; do not silently
  resolve those collisions by precedence.
- [#150](https://github.com/spboyer/pycontainer-build/pull/150) creates both a
  content-addressed image index for registry pushes and a layout-index wrapper.
  Apply manifest annotations to each platform manifest and index annotations to
  both index objects. Serialize the annotated image index before calculating its
  digest and size for the wrapper descriptors and push. Preserve each platform
  descriptor and the existing verification and publication ordering.
- [#185](https://github.com/spboyer/pycontainer-build/pull/185) adds one layout
  descriptor per tag. Keep user index annotations on the index, preserve each
  descriptor's independent internal reference annotation, and use the final
  manifest or image-index digest and size for every descriptor. Pass the
  primary reference through `OCIIndex.reference_name`, not in its user
  annotation map, which rejects protected keys.
- [#207](https://github.com/spboyer/pycontainer-build/pull/207) covers
  [#159](https://github.com/spboyer/pycontainer-build/issues/159) and
  [#160](https://github.com/spboyer/pycontainer-build/issues/160). Its runtime
  config changes must preserve `StopSignal` and `Volumes` alongside inherited
  and overridden labels; keep a combined regression when those changes meet.
