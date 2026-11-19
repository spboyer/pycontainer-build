# Remote cache

`pycontainer build` can import and export application-layer cache entries so CI
jobs, runners and teammates share work. The core keeps zero runtime
dependencies: the supported backends use only the existing Registry v2 client
and the local filesystem.

```powershell
# Export to a registry repository after building
python -m pycontainer.cli build --tag ghcr.io/me/app:1 --cache-to type=registry,ref=ghcr.io/me/cache:main

# Import on another runner (the local cache is populated too)
python -m pycontainer.cli build --tag ghcr.io/me/app:1 --cache-from type=registry,ref=ghcr.io/me/cache:main

# Directory backend, for example a CI-restored or mounted path
python -m pycontainer.cli build --cache-from type=local,src=.\shared-cache --cache-to type=local,dest=.\shared-cache
```

Both options are repeatable and may be set in `pycontainer.toml`:

```toml
[build]
cache-from = ["type=registry,ref=ghcr.io/me/cache:main"]
cache-to = ["type=registry,ref=ghcr.io/me/cache:main,scope=web"]
```

## Specs

| Option | Spec |
|--------|------|
| `--cache-from` | `type=registry,ref=REF[,scope=NAME]` or `type=local,src=DIR[,scope=NAME]` |
| `--cache-to` | `type=registry,ref=REF[,scope=NAME]` or `type=local,dest=DIR[,scope=NAME]` |

`ref` is `registry/repository[:tag]` (tag defaults to `latest`). Registry
credentials come from the usual providers (`REGISTRY_USERNAME`/`REGISTRY_PASSWORD`,
Docker config).

## Keys and scope

The cache key is the same content key as the local cache: file contents, paths,
modes, `workdir` and reproducibility settings. `scope=NAME` namespaces entries
inside one backend so unrelated services or branches can share a repository
without colliding; a build only imports entries from its own scope.

## Storage format

- Registry: blobs are pushed with the existing digest-verified upload. A cache
  manifest at the `ref` tag lists the blobs as layers (so registry garbage
  collection keeps them) and holds the key index in the
  `org.pycontainer.cache.index` annotation. Exports merge into the existing index.
- Local: `index.json` plus `blobs/sha256/<digest>`, written with atomic replace.

## Integrity and failure behavior

- Every imported blob is verified against the indexed digest and size before it
  is used; registry downloads use the streaming, verified `pull_blob` path.
- Corrupt, truncated, missing or tampered blobs and malformed indexes are
  discarded with a warning and the layer is rebuilt. They are never trusted.
- Exports only upload layers that re-verify locally. A corrupt blob already in
  a directory backend is replaced on export.
- Cache outages are best-effort: import or export errors log a warning and do
  not fail the build. Invalid specs fail immediately.
- `--dry-run` does not touch remote caches. Only the application layer is cached.

## Other backends (out of scope)

S3, Azure Blob and GCS are not implemented because they require vendor SDKs.
New stores implement `pycontainer.remote_cache.CacheBackend`
(`read_index`, `write_index`, `has_blob`, `put_blob`, `fetch_blob`) and must
return only digest-verified content from `fetch_blob`.
