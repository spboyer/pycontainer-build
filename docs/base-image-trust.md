# Base image trust

Base image signature verification is disabled unless `[build.base_image_trust]`
is configured. A configured policy is required, not advisory: a missing
verifier, untrusted signature, invalid policy, unavailable evidence, or
repository mismatch fails the build. Removing the table restores the existing
unverified behavior.

The policy applies to one exact canonical registry/repository, without a tag,
digest, or wildcard (for example, `docker.io/library/python`). It does not
verify local `oci:` layouts or `docker-archive:` files; those sources fail
when a registry trust policy is required.

The builder resolves a registry tag once and verifies the exact
`repository@sha256:...` subject before reading its base config or layers. For
a signed multi-platform index, it subsequently checks the selected child
descriptor's digest, size, media type, and platform. Any supplied
`Docker-Content-Digest` header must match the fetched bytes. Signatures establish
authenticity and integrity, **not freshness or rollback protection**: pin a
digest and use an independent update policy when those properties matter.

`--dry-run` validates the policy configuration but neither contacts the
registry nor verifies a signature. It is not evidence that a build is trusted.

## Cosign

Install and independently verify the Cosign executable. A keyless policy pins
both the exact certificate identity and HTTPS OIDC issuer:

```toml
[build]
base_image = "registry.example.com/team/python:stable"

[build.base_image_trust]
provider = "cosign"
repository = "registry.example.com/team/python"
identity = "release@example.com"
issuer = "https://issuer.example.com"
binary = "/path/to/cosign" # optional; defaults to cosign on PATH
```

Alternatively, replace `identity` and `issuer` with a trusted public-key file:

```toml
key = "/path/to/signing-public-key.pem"
```

The key and identity/issuer modes are mutually exclusive. Cosign claim,
transparency-log and signed-certificate-timestamp checks are not disabled.
The signed Cosign payload must also name the exact configured repository;
signatures with an absent or different `docker-reference` are rejected even
when their digest matches.
Optionally set `trust_root` to a trusted Sigstore trusted-root JSON file;
otherwise Cosign uses its normal trust-root configuration. The builder copies
provided trust material into a temporary directory and removes inherited
`COSIGN_*` and `SIGSTORE_*` overrides before invoking the verifier. Cosign
signature discovery is isolated from local registry credentials and currently
supports public registries only; private-registry verification fails closed.
Its isolated trust cache may require access to Sigstore's public trust data on
each build unless an appropriate `trust_root` is configured. Cosign
v3.1.4 has been exercised by the opt-in checks below; other versions are not
guaranteed compatible. On Windows, an unqualified verifier is searched on
`PATH` without implicitly searching the current directory; use a trusted
`PATH` or an explicit trusted executable path.

## Notation

The supported Notation policy pins a trust-root certificate, the exact displayed
X.509 subject, and the lowercase SHA-256 fingerprint of the signer leaf
certificate. Obtain and authenticate these values independently; do not treat
unverified registry inspection output as a trust anchor.

```toml
[build]
base_image = "registry.example.com/team/python:stable"

[build.base_image_trust]
provider = "notation"
repository = "registry.example.com/team/python"
identity = "CN=Release Signing,O=Example Corp,C=US"
signer_fingerprint = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
trust_root = "/path/to/signing-root.crt"
tsa_root = "/path/to/timestamp-root.crt" # optional; requires a valid timestamp
binary = "/path/to/notation" # optional; defaults to notation on PATH
```

The example fingerprint is a placeholder and must be replaced. The identity
must match Notation's displayed subject exactly, without an `x509.subject:`
prefix. When `tsa_root` is supplied, the isolated policy requires a verified
timestamp. An invalid or unavailable timestamp fails verification.

The implementation snapshots the single matching signature envelope into an
isolated temporary OCI layout and uses a generated **strict**, exact-scope
policy. This prevents another signature with a partially matching subject or a
`skip` policy from satisfying the pinned signer. It accepts a tagged
single-signer COSE_Sign1 or flattened single-signer JWS signature. For JWS, it
also checks that the envelope's leaf certificate matches the pinned
fingerprint before Notation verifies the signature and certificate chain.
Multi-signer envelopes, arbitrary Notation policies, plugins, and authenticated
private-registry signature discovery are not supported. Discovery is isolated
from local Notation and registry credentials and fails closed when evidence is
unavailable. Notation v1.3.2 and its experimental OCI-layout mode have been
exercised; other versions are not guaranteed compatible.

## Registry authentication

The normal registry client may use existing credentials. In required-trust
mode, it does not send credentials to a different token-realm URL unless that
exact HTTPS URL is explicitly pinned:

```toml
auth_realm = "https://auth.example.com/token"
```

This setting belongs in `[build.base_image_trust]`. Authorization is retained
for same-origin redirects but is not forwarded to a different origin; base
metadata, token-service, and blob redirect chains in required mode must use
HTTPS. The `auth_realm` setting does not add private-registry authentication to
either verifier's signature discovery.

## Opt-in real-provider checks

These tests contact public registries and external verification services. They
use pinned public image digests, stop before downloading base config or layers,
and include wrong-identity, wrong-key, wrong-fingerprint, wrong-timestamp, and
expired-signer rejections. Upstream availability, certificate status, and verifier versions
can change independently of this repository.

Prepare independently verified Cosign and Notation executables and these public
test materials:

| Material | Public source | SHA-256 of exercised file |
| --- | --- | --- |
| Cosign test key | [travier/cosign-test](https://raw.githubusercontent.com/travier/cosign-test/main/quay.io-travier-containers.pub) | `ca7d5482d2f46489e84eccba85043b28b49d2e770ee422d20c7601e84ca17d63` |
| Notation signing root | [Microsoft Supply Chain RSA Root CA 2022](https://www.microsoft.com/pkiops/certs/Microsoft%20Supply%20Chain%20RSA%20Root%20CA%202022.crt) | `23ffe2b8bdb9a1711515d4cffda04bc7f793d513c76c243f1020507d8669b7db` |
| Notation timestamp root | [Microsoft Root Certificate Authority 2010](https://www.microsoft.com/pki/certs/MicRooCerAut_2010-06-23.crt) | `df545bf919a2439c36983b54cdfc903dfa4f37d3996d8d84b4c31eec6f3c163e` |
| Expired Notation test signer | [Ratify verification certificate](https://ratify.dev/.well-known/pki-validation/ratify-verification.crt) | `b00f9b36efb012d0ac4f643a7ef67beab401a4fd523cc1997cf952b6a507d008` |

Verify these values against your trusted sources before use. For the negative
Cosign key test, supply a different valid public key. The following PowerShell
example runs all checks; use actual absolute paths to your verified files:

```powershell
$env:PYTHONPATH = "src"
$env:PYCONTAINER_COSIGN_BIN = "C:\verified\cosign.exe"
$env:PYCONTAINER_COSIGN_TEST_KEY = "C:\verified\quay.pub"
$env:PYCONTAINER_COSIGN_WRONG_KEY = "C:\verified\other.pub"
$env:PYCONTAINER_NOTATION_BIN = "C:\verified\notation.exe"
$env:PYCONTAINER_NOTATION_ROOT = "C:\verified\signing-root.crt"
$env:PYCONTAINER_NOTATION_TSA = "C:\verified\timestamp-root.crt"
$env:PYCONTAINER_NOTATION_EXPIRED_ROOT = "C:\verified\ratify.crt"
python -m pytest tests\test_trust_real.py -q -o addopts= -m registry_integration
python -m pytest tests\test_trust_notation.py -q -o addopts= -m registry_integration
```

Each test skips if its required environment variables are absent; inspect the
test result rather than treating skipped checks as provider compatibility. The
second command signs a local test layout as JWS and exercises the real Notation
verifier with simulated registry discovery. It also checks a COSE signature
generated on 2026-11-23 using a local test TSA: its signer expired on
2026-11-23, its TSA root expires on 2036-11-20, and verification succeeds only
with the correct TSA root. The fixture contains public certificates and signed
data, not private keys. Neither local check tests remote discovery.
