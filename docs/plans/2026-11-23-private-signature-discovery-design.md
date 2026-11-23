# Private-registry signature discovery for base-image trust

Issue: #17. Status: implemented in #213.

## Problem

Required base-image trust delegated signature discovery to verifier
subprocesses: `cosign verify <repository>@<digest>` and
`notation inspect <repository>@<digest>`. Those subprocesses run with an
isolated, credential-free environment, so private registries could never be
verified. Passing registry credentials to them would bypass pycontainer's
token-realm pinning and redirect isolation.

## Decision

pycontainer discovers and retrieves all signature evidence through the same
strict `RegistryClient` that resolved the subject. It writes only
digest-verified objects to a private OCI layout and runs the verifier
**offline against that layout**. No registry credential, registry URL or
network-capable registry reference is passed to Cosign or Notation.
Both verifiers inherit an **allowlisted** environment rather than a
denylist-scrubbed copy, so an unknown credential variable cannot leak.
Notation, which verifies offline, receives only operating-system essentials.
Keyless Cosign still contacts Sigstore's public TUF repository, so it also
receives proxy and CA-bundle variables, but no registry or cloud credentials.

Probes with Cosign v3.1.4 showed that `cosign verify --local-image` accepts a
layout containing only the subject manifest plus the signature manifest,
config and layers, in key and keyless modes, using the offline Rekor bundle.
Notation v1.3.2 already verified such layouts with `--oci-layout`.

### Cosign

1. Read tag `sha256-<hex>.sig` once with a 4 MiB bound. A missing tag fails
   closed. The tag is mutable, so its manifest digest is computed from the
   bytes and every later read is digest-bound.
2. Require an OCI or Docker image manifest with at most 64 layers, each with
   media type `application/vnd.dev.cosign.simplesigning.v1+json` and at most
   1 MiB; the config is limited to 1 MiB.
3. Pull config and layers with descriptor digest/size checks into a layout
   whose `index.json` lists the subject (`dev.cosignproject.cosign/image` or
   `/imageIndex`) and signatures (`dev.cosignproject.cosign/sigs`).
4. Run `cosign verify --local-image <layout>` with the existing strict flags
   and the allowlisted environment. Existing JSON evidence checks still bind
   `docker-manifest-digest` and `docker-reference` to the exact subject.

Signatures stored only as OCI 1.1 Sigstore bundle referrers are not
discovered and fail closed.

### Notation

1. Query `GET /v2/<repo>/referrers/<digest>?artifactType=application/vnd.cncf.notary.signature`
   (4 MiB bound). On HTTP 404, fall back to the referrers tag
   `sha256-<hex>`; a missing fallback fails closed. Other errors fail closed.
2. Require an OCI index of at most 1,000 descriptors; keep descriptors with
   the Notary artifact type and deduplicate by digest. Registry thumbprint
   annotations are untrusted and ignored, so every candidate is parsed. At
   most 16 candidates (64 MiB of envelopes in total) are fetched; more fails
   closed. A malformed candidate fails closed even if another signer produced
   it; this trades availability for never skipping evidence on an unverified
   hint.
3. Each candidate manifest and envelope is digest/size bound, subject bound,
   and contains exactly one envelope of at most 16 MiB.
4. The leaf certificate is extracted from the envelope itself: `x5c[0]` for
   flattened JWS (existing strict parser) and the unprotected `x5chain`
   (label 33) for tagged COSE_Sign1 with a bounded, definite-length CBOR
   decoder. Its SHA-256 must equal `signer_fingerprint`.
5. Exactly one matching envelope is required. It is verified offline in the
   private layout under the generated strict Notation policy (`notation
   inspect` is no longer used). Notation remains the cryptographic and X.509
   authority, including the exact identity check.

Multi-signer envelopes (COSE_Sign, general JWS serialization) remain outside
the Notary profile and are rejected.

## Security invariants

- Verifier subprocesses never receive credentials or make registry requests;
  their environments are allowlisted.
- All discovery reads use the strict client: pinned token realm, no
  authorization on redirected hosts, bounded bodies, digest/size checks.
- Every object written to the layout is digest-verified before use.
- Base config and layers are still read only after verification succeeds.

## Testing

- Hermetic tests with an authenticated fake registry: discovery succeeds only
  with credentials; verifier environments and commands contain no secrets;
  referrers fallback; bounds; mismatched subject, digest, size and
  fingerprint; missing evidence; no early base-blob reads.
- Opt-in real-provider tests serve real public signatures from a local
  credential-protected registry and verify them with real Cosign and Notation.
  A hosted private registry was not exercised.

## Findings during implementation

The opt-in authenticated real-provider test showed that the previous
denylist environment passed `REGISTRY_PASSWORD` to Notation and other
credential variables (for example `NOTATION_PASSWORD` or `REGISTRY_TOKEN`) to
Cosign. Both verifiers now use allowlists, and hermetic tests inject these
variables to guard the invariant.
