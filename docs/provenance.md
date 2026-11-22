# SLSA provenance (experimental)

pycontainer-build can write an in-toto
[Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md)
with a [SLSA provenance v1](https://slsa.dev/spec/v1.0/provenance) predicate
for the image it builds, and optionally attach that exact statement to the
published image as a signed cosign attestation.

> **Trust model.** A locally written statement is **unsigned and therefore
> unauthenticated**: anyone who can write the output directory can edit it.
> Only an attestation verified against a key or certificate identity that you
> trust tells you who produced it. pycontainer-build does **not** assert a SLSA
> build level; `builder.id` is a self-identifier, not a hosted, isolated or
> hardened build platform. Do not treat this provenance as SLSA Build L2 or L3.

## Usage

```powershell
# Unsigned local statement: dist/image/provenance.intoto.json
pycontainer build --tag ghcr.io/user/app:v1 --include-deps --provenance

# Fail before any tag or layout ref is written if evidence is unavailable
pycontainer build --tag ghcr.io/user/app:v1 --include-deps --provenance-require-complete

# Sign, attest and verify the published digest before moving tags
pycontainer build --tag ghcr.io/user/app:v1 --include-deps --push `
  --sign --sign-provider cosign --sign-key cosign.key `
  --attest-provenance --provenance-verify-key cosign.pub

# Independently verify later (key, or keyless identity and issuer)
pycontainer verify-provenance ghcr.io/user/app@sha256:<digest> --key cosign.pub --require-complete
```

The equivalent `[build]` settings are `provenance`,
`provenance_require_complete`, `attest_provenance`, `provenance_verify_key`,
`provenance_certificate_identity` and `provenance_certificate_oidc_issuer`.
`--provenance-require-complete` and `--attest-provenance` imply `--provenance`.
Provenance cannot be combined with `--dry-run`.

`--attest-provenance` requires `--push` and `--sign --sign-provider cosign`.
Notation has no in-toto attestation path and is rejected. The verifier policy
is mandatory: either `provenance_verify_key`, or both a certificate identity
and an OIDC issuer. The order on push is: publish by digest, sign, attest,
verify the attestation with the configured policy and confirm the verified
statement is exactly the one just attested, re-read the published manifest
digest, then move tags. If attestation or verification fails, no requested tag
is attempted. `COSIGN_REPOSITORY` is rejected so that verification cannot be
silently redirected to another repository.

### How the v1 statement is attached

Released cosign (checked with v2.6.1 and v3.1.4) cannot attach a caller-supplied
statement to an image: `cosign attest` always wraps the predicate in its own
`https://in-toto.io/Statement/v0.1` statement and ignores `--statement` for
images. pycontainer-build therefore signs the exact local statement bytes with
`cosign attest-blob --new-bundle-format --statement`, checks that the returned
Sigstore bundle (`application/vnd.dev.sigstore.bundle.v0.3+json`) signs those
exact bytes, and pushes the bundle itself as an OCI 1.1 referrer of the image
digest, in the layout cosign uses: an empty `{}` config, one bundle layer, and
`dev.sigstore.bundle.content=dsse-envelope` and
`dev.sigstore.bundle.predicateType` annotations. `cosign verify-attestation
--new-bundle-format` then discovers and verifies it.

The registry must support the OCI Referrers API. The referrers tag-schema
fallback (`sha256-<digest>`) is not written; on registries without the
Referrers API, verification finds no attestation and the push fails closed
before any tag moves. cosign v2 only discovers bundle referrers with
`--new-bundle-format`; v3 discovers them by default.

## Build type v1

`buildType` is
`https://github.com/spboyer/pycontainer-build/blob/main/docs/provenance.md#build-type-v1`.

**Subject.** The final digest that is tagged and pushed: the platform image
manifest for a single-platform build, or the top-level image index for a
multi-platform build. The name is `registry/repository` from the primary tag.

**externalParameters.** `baseImage` (or `local:<transport>` for a local base),
`platforms`, `tags`, `workdir`, `includeDependencies`, `reproducible`, and the
context-relative `requirementsFile` when dependencies are included.

**resolvedDependencies.** Only evidence observed during the build:

| `kind` annotation | Digest | Evidence |
|-------------------|--------|----------|
| `base-image` | Digest the reference resolved to in the registry | Annotated with the selected `platformManifestDigest` and `configDigest`, both verified while pulling |
| `installed-wheel` | sha256 of the wheel file that was installed | See below |
| `build-context-layer` | sha256 of the uncompressed application layer tar | Computed from the files that were packaged |

**Installed wheels.** With provenance enabled, dependency staging downloads
wheels into an evidence directory, hashes each file, installs **only those
files** offline (`--no-index --no-deps`), re-hashes them to confirm they did not
change, and requires a one-to-one match between the wheels and the installed
distributions. Every hashed `RECORD` entry, except relocated console scripts,
must match the installed file. A lock
file, requirements file or SBOM inventory is never treated as proof of what was
installed.

**byproducts.** Each platform manifest (`manifest:<os>/<arch>`, annotated with
its config digest), each dependency layer, and `sbom.json` when an SBOM is
generated.

**internalParameters.evidence.** `complete` and a list of `incomplete`
materials with reasons. Evidence is marked incomplete, never invented, when:

- the base image digest could not be observed (for example a stubbed pull);
- the base is a `docker save` archive, which carries no registry digest;
- dependencies are copied from an existing virtualenv rather than installed;
- the installer could not report installed wheel hashes.

The completeness policy (`provenance_require_complete` locally,
`--require-complete` in `verify-provenance`) fails closed. Locally it fails
before `index.json` or tag refs are written, and any stale statement is
removed at the start of each build. When verifying, every matching verified
attestation must record complete evidence, so an older incomplete attestation
cannot be shadowed by a newer complete one.

**Excluded.** Environment variables, labels, entrypoint and command,
credentials, package index URLs, requirements file contents, absolute host
paths and any value derived from them are not recorded.

## Verification

`pycontainer verify-provenance` runs `cosign verify-attestation
--new-bundle-format --type slsaprovenance1` with your policy, then rejects any
pycontainer envelope that is not an in-toto DSSE payload, is not an in-toto
Statement v1, is not SLSA provenance v1, has a subject that does not
match the requested digest, or has malformed completeness fields. Non-pycontainer
SLSA attestations are ignored; at least one pycontainer attestation must match.
Verification uses cosign's transparency log defaults.

## Status and limitations

- Real-provider evidence: a build on `python:3.11-slim` with a real installed
  wheel was published to `ttl.sh` (which supports the Referrers API). Its exact
  Statement v1 was signed with cosign v3.1.4 `attest-blob`, pushed as a bundle
  referrer by pycontainer's registry client, and independently discovered and
  verified by `cosign verify-attestation` v3.1.4 and v2.6.1
  (`--new-bundle-format`) using a key pair **with the transparency log
  disabled**. A wrong key was rejected. Keyless signing, public Rekor upload,
  registries without the Referrers API, GHCR, ACR and Docker Hub have not been
  validated.
- Notation has no attestation path.
- Dependency environment markers are evaluated on the build host
  ([#142](https://github.com/spboyer/pycontainer-build/issues/142)).
- Multi-tag pushes are not atomic; see the [signing guide](image-signing.md).

## Real-registry test

`tests/test_provenance_integration.py` is marked `registry_integration` and is
excluded by default. It requires cosign on `PATH`, a disposable
`PYCONTAINER_SIGNING_TEST_REPOSITORY`, `PYCONTAINER_COSIGN_TEST_KEY`,
`PYCONTAINER_COSIGN_TEST_PUBLIC_KEY` and an unrelated
`PYCONTAINER_COSIGN_TEST_WRONG_PUBLIC_KEY`. It uploads to the transparency log
configured for cosign.

```powershell
uv run --locked --group test python -m pytest tests/test_provenance_integration.py -q -o addopts= -m registry_integration
```
