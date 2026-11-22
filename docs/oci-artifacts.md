# Generic OCI 1.1 artifacts (experimental)

`pycontainer.artifact` is a zero-dependency Python API. It packages a single
typed payload as a non-runnable OCI 1.1 artifact manifest, stores it in a local
OCI image layout, and moves it to and from compatible registries. It is
infrastructure for attaching metadata, such as reports, SBOMs or provenance, to
an image by digest.

It does **not** sign anything, generate attestations, or use the layer cache. It
also does not add a CLI subcommand: the Python API is the only interface.

## Manifest contract

Every artifact is an `application/vnd.oci.image.manifest.v1+json` manifest. It
has the following shape:

```json
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "artifactType": "application/vnd.example.report.v1",
  "config": {
    "mediaType": "application/vnd.oci.empty.v1+json",
    "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "size": 2
  },
  "layers": [
    {"mediaType": "application/vnd.example.report.v1+json", "digest": "sha256:...", "size": 123}
  ],
  "subject": {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:...", "size": 456},
  "annotations": {"org.example.key": "value"}
}
```

- **`artifactType`:** required. It must be a valid RFC 6838 media type and must
  not be an image, layer, config, index or empty type.
- **`config`:** always the exact two-byte `{}` blob with media type
  `application/vnd.oci.empty.v1+json`. A consumer can therefore never confuse
  the artifact with a runnable image. An image build that uses an artifact
  layout as an `oci:` base fails with
  `unsupported media type 'application/vnd.oci.empty.v1+json'`.
- **Payload:** exactly one layer of a caller-supplied, non-image media type.
  Descriptor `urls` are rejected. Embedded `data` is accepted only when it
  decodes to bytes that match the descriptor digest and size.
- **`subject`:** optional. It must be an OCI or Docker image manifest or index
  descriptor with a lowercase `sha256` digest and a non-negative integer size.
- **Parsing:** strict. Duplicate JSON keys, boolean sizes, the wrong
  `schemaVersion`, missing fields and non-empty configs are all rejected.
  Unknown extra fields are ignored, as OCI requires.

## API

```python
from pycontainer.artifact import (
    write_artifact_layout, read_artifact_layout,
    push_artifact, pull_artifact, list_referrers,
)
from pycontainer.registry_client import RegistryClient

artifact = write_artifact_layout(
    "dist/artifacts", "report.json",
    artifact_type="application/vnd.example.report.v1",
    payload_media_type="application/vnd.example.report.v1+json",
    subject={"mediaType": "application/vnd.oci.image.manifest.v1+json",
             "digest": image_digest, "size": image_manifest_size},
    tag="report",
)
result = push_artifact(client, "dist/artifacts", "report", tags=["report"])
referrers = list_referrers(client, image_digest, "application/vnd.example.report.v1")
pulled = pull_artifact(client, "report", "dist/pulled",
                       expected_artifact_type="application/vnd.example.report.v1",
                       expected_subject_digest=image_digest)
```

### Writing, reading and pushing

- **`write_artifact_layout`** writes a single local layout entry.
  - It streams the payload into `blobs/sha256` with a size bound (1 GiB by
    default, set by `max_payload_bytes`) and refuses symlinked payloads.
  - It publishes blobs content-addressed and atomically, then atomically
    replaces `index.json`. Writing an existing `tag` replaces only that entry.
  - It returns an `Artifact` that holds the manifest descriptor, the parsed
    manifest and the payload path.
- **`read_artifact_layout`** resolves an entry by tag, by digest, or as the
  only entry. It verifies the manifest, the empty config and the payload
  against their digests and sizes.
- **`push_artifact`** re-verifies the layout, then publishes in this order:
  1. If the artifact has a subject, HEAD-verifies that the subject exists in
     the repository. Pass `verify_subject=False` to skip this check.
  2. Uploads the config and payload blobs unless a HEAD check shows a blob of
     the matching size is already there, then re-checks each blob.
  3. PUTs the manifest by digest and HEAD-verifies its digest, size and media
     type.
  4. PUTs and verifies each tag in turn.

  The returned `subject_status` is either:
  - `confirmed`: the registry returned a matching `OCI-Subject` header, or
    the artifact has no subject.
  - `unconfirmed`: the header was absent.

  A mismatched `OCI-Subject` raises an error.
- **`publish_artifact_manifest`** runs steps 1-3 for manifest bytes that are
  already serialized and blobs that are already staged, given as
  `(digest, size, path)` for exactly the empty config and the payload. It
  computes the manifest digest from the bytes given, uploads them unchanged,
  and moves no tags. Callers that build their own manifest bytes, for example
  attestation publishers, use it to avoid writing a temporary OCI layout.
  Blob HEAD checks confirm metadata only; they do not re-read the blob bytes.
- **`pull_artifact`** fetches the manifest under a 4 MiB bound and requires the
  exact OCI manifest `Content-Type`. It verifies the body digest against
  `Docker-Content-Digest`, the requested digest and `Content-Length`. It then
  checks the optional expected artifact type and subject, streams and verifies
  each blob, and only then records the manifest in the local layout.

### Discovery

`list_referrers` calls `GET /v2/<name>/referrers/<digest>`.

- **`status == "supported"`:** the registry returned an OCI image index.
  Results are always filtered by `artifactType` on the client, as well as by
  the registry. `registry_filter_applied` reports whether the registry
  advertised `OCI-Filters-Applied: artifactType`.
- **`status == "not_found"`:** an HTTP 404. This can mean the registry has no
  referrers API, or that the subject or repository is unknown; the response
  does not distinguish the two. It is reported as a status, not as an empty
  successful listing.
- **Other statuses** and malformed responses raise explicit errors.
- **`require_referrer`** checks that a just-published descriptor is
  discoverable. It requests an unfiltered listing and matches digest, size and
  `mediaType`, plus `artifactType` only when the listed entry includes it,
  because that field is optional on index entries. A 404 fails immediately.
  Only a supported listing that lacks the descriptor is retried, up to
  `REFERRER_DISCOVERY_ATTEMPTS` (3) times with `REFERRER_DISCOVERY_DELAYS`
  (1 s, then 2 s) between attempts. Malformed responses are not retried.
- **Pagination:** `Link: rel="next"` is followed for at most 16 pages. Each
  next link must resolve to the same referrers path on the same registry
  origin, and repeated or off-path links are rejected.

The OCI **tag-schema fallback** (`sha256-<digest>` tags) is deliberately **not**
written or read. Maintaining it requires racy read-modify-write updates of a
shared tag. Registries without the referrers API therefore support push and
pull by tag or digest, but not discovery through this API.

## Errors

| Exception | Meaning |
|---|---|
| `InvalidArtifactError` (`ValueError`) | Malformed input, manifest, subject, tag or layout reference |
| `UnsupportedArtifactError` | Valid JSON that is not a generic artifact (for example a runnable image manifest, or the wrong `Content-Type`) |
| `BlobIntegrityError` | Digest, size or embedded data mismatch, or a corrupt local or registry blob |
| `RegistryRequestError` | HTTP failure, a response over its bound, or an unsafe redirect or pagination link, outside publication |
| `ArtifactPublicationError` | Publication could not be confirmed. `stage` is `subject`, `blob`, `manifest`, `tag` or `referrers`, and the underlying registry or integrity error is chained as `__cause__`. For `tag`, `published_tags` lists the tags already moved |

## Registry compatibility

Observed on 2026-11-22 with the opt-in test below. This is not a support
guarantee for any registry or version.

| Registry | Push/pull by digest and tag | `OCI-Subject` | Referrers API | ORAS 1.4.0 fetch/discover |
|---|---|---|---|---|
| zot (`ghcr.io/project-zot/zot-minimal:latest`) | yes | confirmed | supported, server filter applied | yes |
| CNCF Distribution (`registry:3`) | yes | unconfirmed | `not_found` (route not served) | fetch only |

Other registries, including GHCR, ACR and Docker Hub, have not been verified by
this project. Their behavior, including whether they accept unknown
`artifactType` values, should be tested before you rely on it.

## Limitations

- **Single payload only.** Multi-layer artifacts are rejected.
- **Not atomic.** Tag updates are not atomic: a later tag failure can leave
  earlier tags moved, and those are reported in `published_tags`.
- **Single writer.** The local layout `index.json` is replaced atomically but
  not locked, so do not write to one layout from concurrent processes. Prefer a
  directory separate from `dist/image`, because image builds rewrite that
  index.
- **Blob checks.** Artifact publication HEAD-checks each blob before reuse and
  after upload, and requires a matching `Content-Length` and any reported
  digest. A mismatch fails before the manifest is published.
- **Auth and redirects.** Registry authentication and redirect behavior are
  the same as for image push and pull. Blob redirects must use HTTPS, and
  authorization is never forwarded to redirected storage hosts.

## Real-registry tests

`tests/test_artifact_integration.py` is marked `registry_integration` and is
excluded by default. It needs a disposable repository, and it pushes a subject
artifact and a referring artifact with unique tags. It then pulls the referrer,
verifies it, and lists referrers. If `oras` is on `PATH`, it also checks that
ORAS can fetch and discover the artifact independently.

```powershell
$env:PYCONTAINER_ARTIFACT_TEST_REPOSITORY = "registry.example.com/team/pycontainer-artifacts"
uv run --locked --group test python -m pytest tests/test_artifact_integration.py -q -s -o addopts= -m registry_integration
```

For a local unauthenticated registry on loopback, for example
`docker run -d -p 127.0.0.1:55001:5000 ghcr.io/project-zot/zot-minimal:latest`,
also set `PYCONTAINER_ARTIFACT_TEST_PLAIN_HTTP_LOOPBACK=1`. In that mode no
credentials are sent.
