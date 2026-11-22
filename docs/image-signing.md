# Signing registry images

Image signing is experimental and strictly opt-in. The core package adds no
runtime dependency or daemon: install the optional provider CLI separately.
Cosign versions `>=2.6.1,<4.0.0` and Notation versions `>=1.3.2,<2.0.0` pass
the version preflight; this is not a claim that every version or registry has
been exercised. The signing and independent verification paths have not yet
been demonstrated against a real registry for this change.

## Prerequisites and use

- `--push` and an HTTPS registry that accepts and serves manifests by digest.
  Signing a local-only layout and signing during `--dry-run` are not supported.
  Registry redirects require HTTPS; authorization is not forwarded across
  origins. Bearer-token exchange endpoints must also use HTTPS.
- A trusted `cosign` or `notation` executable on `PATH`, plus registry access
  and the provider's signing credentials. Pycontainer's registry credentials
  are not translated into signer credentials: configure the signer's Docker
  credential helper or its own supported environment separately. For Notation,
  `NOTATION_USERNAME` and `NOTATION_PASSWORD` are also supported by Notation.
  On Windows, use a native executable, not a `.bat` or `.cmd` wrapper. Signer
  lookup ignores the current directory and relative `PATH` entries.
  Do not put passwords, tokens, private keys or secret-bearing URLs in flags,
  TOML, key references or logs.
- Cosign keyless mode needs an ambient OIDC identity, such as a configured CI
  identity or `SIGSTORE_ID_TOKEN`, and access to Sigstore services. An encrypted
  key can use `COSIGN_PASSWORD`. The subprocess is non-interactive: browser
  device flows and password prompts are unavailable. Signer output is
  suppressed to avoid exposing credentials; a nonzero exit or timeout stops tag
  publication. Cosign `--yes` can consent to publishing signing metadata in
  a transparency log.
- For Notation, configure an X.509-backed named key, or install and configure a
  signing plugin and supply its key identifier. Verification additionally
  requires the verifier's trust store and trust policy. For short-lived
  certificates, configure a timestamp authority and appropriate root certificate.

```bash
# Keyless Cosign: supply an ambient OIDC identity in the environment, plus the
# certificate identity and issuer the signature must be verified against.
pycontainer build --tag ghcr.io/example/app:v1 --push \
  --sign --sign-provider cosign \
  --sign-certificate-identity 'https://github.com/example/app/.github/workflows/release.yml@refs/heads/main' \
  --sign-certificate-oidc-issuer https://token.actions.githubusercontent.com

# Cosign key reference plus the public key used for verification; keep the key
# password in a secure environment.
pycontainer build --tag ghcr.io/example/app:v1 --push \
  --sign --sign-provider cosign --sign-key ./cosign.key --sign-verify-key ./cosign.pub

# Notation named key, or a separately installed key plugin.
pycontainer build --tag ghcr.io/example/app:v1 --push \
  --sign --sign-provider notation --sign-key release-key
pycontainer build --tag ghcr.io/example/app:v1 --push \
  --sign --sign-provider notation --sign-plugin example-plugin --sign-key-id key-id

# Use a previously configured Notation plugin-backed key and timestamping.
pycontainer build --tag ghcr.io/example/app:v1 --push \
  --sign --sign-provider notation --sign-key configured-plugin-key \
  --sign-signature-format cose --sign-timestamp-url https://tsa.example \
  --sign-timestamp-root-cert ./tsa-root.pem
```

Equivalent configuration, enabled only when `sign = true`:

```toml
[build]
tag = "ghcr.io/example/app:v1"
sign = true
sign-provider = "notation"
sign-key = "release-key"
```

Invoke the build with `--push`; `--no-sign` disables signing even when the file
enables it. For a Notation plugin use `sign-plugin` and `sign-key-id` instead of
`sign-key`. A Cosign key may be a file path or a supported key reference such
as `env://KEY_VARIABLE`; omit it for keyless OIDC. Cosign key mode requires
`sign-verify-key` (a public key path or the same KMS/PKCS#11 reference); keyless
mode requires both `sign-certificate-identity` and
`sign-certificate-oidc-issuer`. Notation rejects these three options and uses
its configured trust policy instead. `COSIGN_REPOSITORY` is
rejected to prevent silently redirecting signature storage.
For PKCS#11 keys, put the PIN in `COSIGN_PKCS11_PIN`, not the key URI.
Notation plugin configurations requiring `--plugin-config` can be registered
with `notation key add` and used through `--sign-key`; direct `--sign-plugin`
does not pass extra plugin configuration. Do not store credentials in plugin
configuration or arguments.

For programmatic use, `BuildConfig(sign=True, ...)` configures
`ImageBuilder.push()`. Calling `build()` alone creates only an unsigned local
layout; it does not sign it.

## Publication, discovery and trust

The builder checks local content, uploads blobs, stages the actual image
manifest by its SHA-256 digest, verifies its bytes with GET, and checks the
signer-visible digest, size and media type with HEAD. A multi-platform build
stages each platform manifest and signs the published **top-level image index**,
not the local layout's `index.json` or an
individual platform manifest. The signer receives the effective
`registry/repository@sha256:...`, including a `--registry` override, and is
not invoked with a mutable tag. It must exit successfully.

The same provider then verifies the signature on that exact digest before any
tag moves. Cosign runs `cosign verify` with `--key=<sign-verify-key>`, or with
the configured `--certificate-identity` and `--certificate-oidc-issuer`.
Notation first reads `notation policy show` and requires exactly one applicable
trust-policy statement (an exact `registryScopes` match, otherwise a `["*"]`
statement) with verification level `strict` and no overrides; it then requires
`notation verify` to report `Successfully verified signature for <subject>`,
and rejects a trust policy that changes during verification. A verification
failure, timeout or unexpected output stops the push: no requested tag is
written, though the staged subject and signature artifacts may remain. The
subject is then rechecked before any requested tag is written. The CLI prints the signed
digest unless `--no-progress` is set; programmatic callers can read
`builder.signed_ref` after this point.

Cosign v2 uses its digest-derived `.sig` tag by default; Cosign v3 uses its
bundle/referrers behavior. Notation is instructed to prefer native OCI
referrers and use the standard referrers-tag fallback when necessary. Use the
same provider's verification and discovery configuration as the signer.
Requested tags in the digest-derived `sha256-<digest>` namespace, including
`.sig`, `.att` and `.sbom` variants for any digest, are rejected before
publication. Providers may create or update existing tags in this namespace;
do not use them as image tags. Referrers-tag fallback updates can race with
other writers and lose signatures; native registry referrers or external
coordination are safer.

Only after signing and verification are requested tags written, one at a time, and each is read
back against the signed digest. Failures report acknowledged writes, an
unknown or mismatched outcome, and tags not attempted, as well as blob results,
acknowledged manifest writes and previously verified manifests. These are
point-in-time observations.
The staged subject or signature artifacts may remain after a failure; no
rollback is attempted and multi-tag updates are not atomic. Another writer
can retarget a tag after readback. Consumers should resolve and verify the
digest they deploy.

For example, independently verify the printed digest with an expected public
key, or with an expected OIDC identity and issuer:

```bash
cosign verify --key ./cosign.pub ghcr.io/example/app@sha256:<digest>
cosign verify --certificate-identity '<expected-identity>' \
  --certificate-oidc-issuer '<expected-issuer>' \
  ghcr.io/example/app@sha256:<digest>
notation verify ghcr.io/example/app@sha256:<digest>
```

Notation verification uses its configured trust policy and trust store.
Build-time verification uses the trust configuration supplied to the build; it
is not independent verification by the consumer. These verification command
lines and Notation output strings are covered by hermetic tests only and have
not yet been exercised against real providers or registries. A valid
signature authenticates an artifact only under the verifier's policy; it does
not establish that an image is free of vulnerabilities or safe to run.

## Real-registry tests

These tests publish synthetic images, unique tags, and signatures; use a
disposable repository with an appropriate retention policy. The tests do not
delete published objects, and Sigstore transparency entries can be public and
permanent. Configure registry authentication separately for pycontainer and
the signer/verifier.

Set `PYCONTAINER_SIGNING_TEST_REPOSITORY` to an HTTPS registry repository
without a tag, such as `registry.example/team/signing-test`. Set the
mode-specific variables for the tests you want to run:

| Mode | Required variables |
|---|---|
| Cosign key | `PYCONTAINER_COSIGN_TEST_KEY`, `PYCONTAINER_COSIGN_TEST_PUBLIC_KEY` |
| Cosign OIDC | `PYCONTAINER_COSIGN_TEST_IDENTITY`, `PYCONTAINER_COSIGN_TEST_ISSUER`, plus an ambient OIDC identity |
| Notation named key | `PYCONTAINER_NOTATION_TEST_KEY` and a configured strict verifier trust policy without overrides |
| Notation plugin | `PYCONTAINER_NOTATION_TEST_PLUGIN`, `PYCONTAINER_NOTATION_TEST_KEY_ID` and a configured strict verifier trust policy without overrides |

For either Notation mode, optional `PYCONTAINER_NOTATION_TEST_SIGNATURE_FORMAT`,
`PYCONTAINER_NOTATION_TEST_TIMESTAMP_URL` and
`PYCONTAINER_NOTATION_TEST_TIMESTAMP_ROOT_CERT` configure the corresponding
signing options. A plugin that needs additional settings can be registered as
a named key and exercised through the named-key mode.

```bash
uv run --locked --group test python -m pytest tests/test_signing_integration.py \
  -q -o addopts= -m registry_integration
```

Each configured mode signs and independently verifies both a single-platform
manifest and a multi-platform index with unique content. Notation tests reject
skip, audit, permissive or overridden trust policies. Unconfigured modes skip.
This check is not part of the default suite or CI and should be run with an
external timeout.

Cosign SLSA provenance attestation (`--attest-provenance`) and its separate
real-registry test are described in the [provenance guide](provenance.md).
