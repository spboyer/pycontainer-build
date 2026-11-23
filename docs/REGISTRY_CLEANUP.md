# Registry Cleanup: Delete and Prune

`pycontainer registry` deletes image manifests from a Registry v2 / OCI
Distribution registry. Deletion is destructive and usually irreversible, so
both commands are conservative by design.

## Delete one image

```bash
# Resolve and print the digest only
pycontainer registry delete ghcr.io/user/app:v1.2.3 --dry-run

# Delete after an interactive confirmation
pycontainer registry delete ghcr.io/user/app:v1.2.3

# Delete by digest without a prompt (CI)
pycontainer registry delete ghcr.io/user/app@sha256:<digest> --yes
```

The reference must contain an explicit tag or `@sha256:` digest; an implied
`latest` is rejected. A tag is first resolved to its manifest digest with a
verified `GET` (the returned body must match a `sha256:` or `sha512:`
`Docker-Content-Digest` header), and the digest is what gets deleted. Only
`sha256` digests can be deleted; a `sha512` or unrecognized header algorithm
fails before any `DELETE` is sent.

## Prune old semantic versions

```bash
# Preview (the default): nothing is deleted
pycontainer registry prune ghcr.io/user/app --keep 5

# Only consider 1.x tags, never touch release candidates
pycontainer registry prune ghcr.io/user/app --keep 3 --match "1.*" --protect "*-rc*"

# Execute the plan after confirmation
pycontainer registry prune ghcr.io/user/app --keep 5 --execute
```

- `--keep N` (required, at least 1) keeps the N highest versions by
  [SemVer 2.0.0](https://semver.org/) precedence. Tags must be strict SemVer
  with an optional `v` prefix (`1.2.3`, `v1.2.3-rc.1`). SemVer build metadata
  (`+build`) cannot appear because OCI tags do not allow `+`. Equal versions at the
  boundary (for example `v1.0.0` and `1.0.0`) are all kept.
- `--match GLOB` (default `*`) selects tags to consider. Unmatched tags are
  ignored, never deleted.
- `--protect GLOB` (repeatable) marks matched tags as never deleted.
- Matched tags that are not valid SemVer are skipped, never deleted.
- `--page-size N` requests a tag page size; registries may cap it. Pagination
  follows `Link: <...>; rel="next"` headers only when they stay on the same
  registry and `/v2/<repository>/tags/list` path. Loops, malformed pages, more
  than 10,000 pages or 100,000 tags abort the plan.

The dry-run plan lists every tag with its action (`ignore`, `protect`, `skip`,
`keep`, `delete`, or `retain`), digest and reason, followed by the number of
digests selected for deletion.

## Confirmation

Deletion requires either an interactive `y`/`yes` answer or `--yes`. When
standard input is not a terminal (CI, pipes) and `--yes` is absent, the command
fails before deleting anything. `prune` additionally requires `--execute`.

## Shared digests: why deleting one tag can remove others

Registries delete **manifests by digest**, not tags. Every tag that points to
the same digest disappears with it. Tags such as `latest`, `stable`, `1`, and
`1.2.3` often alias one manifest.

`registry delete` prints this warning before confirming. `prune` resolves the
digest of **every** tag in the repository whenever any deletion candidate
exists, and refuses to delete (`retain`) a candidate digest when any kept,
protected, skipped or unmatched tag points to it. Digests referenced by image
indexes (multi-platform manifests) reachable from those tags are also
protected. Two old tags sharing one digest produce a single `DELETE`.

Resolving every tag costs one manifest `GET` per tag. On registries with pull
quotas, such as Docker Hub, this counts against the limit. A tag that cannot be
resolved, or a digest that is not `sha256`, aborts the whole plan.

## Limitations and guarantees

- **No atomicity.** The plan reflects the registry at the time it was read. A
  concurrent push or retag can make a planned digest shared before it is
  deleted. Avoid pruning while other writers are active.
- **No retries.** `DELETE` is never retried. On a network error the outcome is
  reported as unknown; verify the registry state before running again.
- **Stops on first failure.** `prune` deletes digests sequentially and reports
  how many were deleted before a failure.
- **Referrers.** Signatures, SBOMs and other artifacts that refer to a deleted
  manifest through `subject` are not deleted and may become orphaned.
- **Unsupported deletion.** HTTP 405 or an `UNSUPPORTED` error means the
  registry (or its configuration) does not allow manifest deletion.
  `registry:2` / CNCF Distribution requires
  `REGISTRY_STORAGE_DELETE_ENABLED=true`.
- **Permissions.** Deletion needs `delete` permission on the repository. A
  `401`/`403` error says so; the token scope is re-negotiated when the registry
  challenges for a broader scope. Some registries, such as GHCR, manage package
  versions through their own API and may reject Registry v2 deletion.

## Storage is reclaimed by the registry, not by this command

Deleting a manifest removes the reference. Layer and config blobs remain until
the registry runs garbage collection, and shared blobs are kept while any other
manifest uses them. For example:

- CNCF Distribution (`registry:2`): run
  `registry garbage-collect /etc/docker/registry/config.yml` while the registry
  is read-only or stopped.
- Azure Container Registry: untagged manifests and storage are handled by ACR
  retention policies and its own cleanup; see the ACR documentation.

Consult your registry's documentation for its retention and GC behavior.

## Credentials

Both commands use the same credential discovery as `build --push`
(`--username`/`--password`, environment variables, Docker config, Azure CLI);
see [Authentication Priority](REGISTRY_PUSH.md#authentication-priority). Error
output uses the same redacted diagnostics described in
[Registry errors, rate limits and quotas](REGISTRY_PUSH.md#registry-errors-rate-limits-and-quotas).
Authorization headers are not forwarded on HTTP redirects, and `DELETE`
redirects are not followed.
