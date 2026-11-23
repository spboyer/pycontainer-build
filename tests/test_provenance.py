import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pycontainer import builder as builder_module
from pycontainer import artifact, cli, provenance
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.dependencies import _hash_wheelhouse, _verify_installed_wheels, install_requirements
from pycontainer.provenance import (
    BUILD_TYPE, STATEMENT_V1, IncompleteProvenanceError, ProvenanceError,
    parse_verified_attestations,
)

from test_dependency_staging import BASE_CONFIG, _write_wheel
from test_signing import FakeRegistry, _use_registry

BASE_INDEX = "sha256:" + "1" * 64
BASE_MANIFEST = "sha256:" + "2" * 64
BASE_CONFIG_DIGEST = "sha256:" + "3" * 64
SECRET = "s3cret-value-never-recorded"


def _observed_base(self, layers_dir, os_name, arch):
    self._last_base_evidence = {
        "resolved": BASE_INDEX, "manifest": BASE_MANIFEST, "config": BASE_CONFIG_DIGEST,
    }
    return [], {**BASE_CONFIG, "architecture": arch}


def _context(tmp_path, *, requirements=True):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    if requirements:
        _write_wheel(context / "wheelhouse")
        (context / "requirements.txt").write_text(
            "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
        )
    return context


def _config(tmp_path, context, **options):
    return BuildConfig(
        tag="registry.example/team/app:v1",
        context_dir=str(context),
        output_dir=str(tmp_path / "out"),
        use_cache=False,
        provenance=True,
        **options,
    )


def test_statement_records_installed_wheel_bytes_and_observed_base_digests(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", _observed_base)
    builder = ImageBuilder(_config(
        tmp_path, context, include_deps=True, generate_sbom=True,
        env={"API_TOKEN": SECRET}, labels={"secret": SECRET},
    ))

    builder.build()

    path = tmp_path / "out" / "provenance.intoto.json"
    text = path.read_text()
    statement = json.loads(text)
    assert statement["_type"] == STATEMENT_V1
    assert statement["subject"] == [{
        "name": "registry.example/team/app",
        "digest": {"sha256": builder.manifest_digest.split(":", 1)[1]},
    }]
    predicate = provenance.validate_statement(
        statement, builder.manifest_digest, require_complete_evidence=True
    )
    definition = predicate["buildDefinition"]
    assert definition["buildType"] == BUILD_TYPE
    dependencies = {item["annotations"]["kind"]: item for item in definition["resolvedDependencies"]}
    wheel_bytes = (context / "wheelhouse" / "fixture_dep-1.0-py3-none-any.whl").read_bytes()
    assert dependencies["installed-wheel"] == {
        "name": "fixture_dep-1.0-py3-none-any.whl",
        "digest": {"sha256": hashlib.sha256(wheel_bytes).hexdigest()},
        "annotations": {
            "kind": "installed-wheel", "platform": "linux/amd64",
            "distribution": "fixture-dep", "version": "1.0", "size": len(wheel_bytes),
        },
    }
    assert dependencies["base-image"]["digest"] == {"sha256": "1" * 64}
    assert dependencies["base-image"]["annotations"]["platformManifestDigest"] == BASE_MANIFEST
    assert dependencies["build-context-layer"]["digest"]["sha256"] == builder.layers[-1].digest[7:]
    byproducts = {item["name"] for item in predicate["runDetails"]["byproducts"]}
    assert {"manifest:linux/amd64", "dependency-layer:linux/amd64", "sbom.json"} <= byproducts
    assert definition["externalParameters"]["requirementsFile"] == "requirements.txt"
    assert SECRET not in text
    assert "find-links" not in text and str(tmp_path) not in text
    assert "slsa_build_level" not in text.lower() and "buildLevel" not in text


def test_missing_evidence_fails_closed_before_layout_references(tmp_path, monkeypatch):
    context = _context(tmp_path, requirements=False)
    output = tmp_path / "out"
    output.mkdir()
    (output / "provenance.intoto.json").write_text("stale")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, layers_dir, os_name, arch: ([], None)
    )

    lenient = ImageBuilder(_config(tmp_path, context))
    lenient.build()
    evidence = lenient.provenance_statement["predicate"]["buildDefinition"][
        "internalParameters"]["evidence"]
    assert evidence == {"complete": False, "incomplete": [{
        "material": "base-image:linux/amd64", "reason": "base image digest evidence unavailable",
    }]}

    for item in ("index.json", "oci-layout", "provenance.intoto.json"):
        (output / item).unlink()
    for ref in (output / "refs" / "tags").iterdir():
        ref.unlink()
    strict = ImageBuilder(_config(tmp_path, context, provenance_require_complete=True))
    with pytest.raises(IncompleteProvenanceError, match="base image digest evidence unavailable"):
        strict.build()
    assert not (output / "index.json").exists()
    assert not (output / "provenance.intoto.json").exists()
    assert not list((output / "refs" / "tags").iterdir())


def test_installer_without_wheel_evidence_is_incomplete(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", _observed_base)
    monkeypatch.setattr(builder_module, "install_requirements", lambda *args, **kwargs: None)
    builder = ImageBuilder(_config(tmp_path, context, include_deps=True))

    builder.build()

    assert builder.provenance_statement["predicate"]["buildDefinition"]["internalParameters"][
        "evidence"]["incomplete"] == [{
            "material": "dependencies:linux/amd64",
            "reason": "installer did not report installed wheel hashes",
        }]


@pytest.mark.parametrize("verified", [False, True])
def test_wheel_evidence_uses_verified_base_tag_for_target_selection(tmp_path, monkeypatch, verified):
    context = _context(tmp_path)

    def pull_base(self, layers_dir, os_name, arch):
        layers, config = _observed_base(self, layers_dir, os_name, arch)
        self._base_image_tag_verified = verified
        config["config"] = {"Env": ["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.9"]}
        return layers, config

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", pull_base)
    builder = ImageBuilder(_config(tmp_path, context, include_deps=True))
    if not verified:
        with pytest.raises(ValueError, match="unverified"):
            builder.build()
        return

    builder.build()
    dependencies = builder.provenance_statement["predicate"]["buildDefinition"]["resolvedDependencies"]
    assert any(item["annotations"]["kind"] == "installed-wheel" for item in dependencies)


def test_evidence_mode_rejects_tampered_installs_and_non_wheel_downloads(tmp_path):
    context = _context(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    site = tmp_path / "stage" / "site-packages"
    wheels = install_requirements(
        context / "requirements.txt", site, context, "linux", "amd64", "3.11",
        "python:3.11-slim", BASE_CONFIG, evidence_dir=evidence,
    )
    assert [wheel["filename"] for wheel in wheels] == ["fixture_dep-1.0-py3-none-any.whl"]

    (site / "fixture_dep" / "__init__.py").write_text("tampered = True\n")
    with pytest.raises(RuntimeError, match="does not match its RECORD"):
        _verify_installed_wheels(site, wheels)
    with pytest.raises(RuntimeError, match="one-to-one"):
        _verify_installed_wheels(site, [*wheels, {**wheels[0], "name": "extra"}])

    (evidence / "wheels" / "fixture-dep-1.0.tar.gz").write_bytes(b"sdist")
    with pytest.raises(RuntimeError, match="non-wheel artifact"):
        _hash_wheelhouse(evidence / "wheels")


def _statement(digest="a" * 64, *, complete=True, statement_type=STATEMENT_V1, **overrides):
    statement = {
        "_type": statement_type,
        "predicateType": provenance.SLSA_PROVENANCE_V1,
        "subject": [{"name": "registry.example/app", "digest": {"sha256": digest}}],
        "predicate": {"buildDefinition": {
            "buildType": BUILD_TYPE,
            "internalParameters": {"evidence": {
                "complete": complete,
                "incomplete": [] if complete else [{"material": "m", "reason": "r"}],
            }},
        }},
    }
    statement.update(overrides)
    return statement


def _envelope(statement, payload_type=provenance.DSSE_PAYLOAD_TYPE, bundle=False):
    envelope = {
        "payloadType": payload_type,
        "payload": base64.b64encode(json.dumps(statement).encode()).decode(),
        "signatures": [{"sig": "x"}],
    }
    return json.dumps({"dsseEnvelope": envelope} if bundle else envelope).encode()


DIGEST = "sha256:" + "a" * 64


def test_verified_output_accepts_v1_envelopes_and_bundles():
    output = _envelope(_statement()) + b"\n" + _envelope(_statement(), bundle=True)
    assert len(parse_verified_attestations(output, DIGEST, require_complete_evidence=True)) == 2


@pytest.mark.parametrize("output, message", [
    (b"", "No verified"),
    (b"not json", "malformed"),
    (json.dumps({"payloadType": provenance.DSSE_PAYLOAD_TYPE, "payload": "%%%"}).encode(), "malformed"),
    (_envelope(_statement(), payload_type="text/plain"), "DSSE"),
    (_envelope(_statement(digest="b" * 64)), "subject"),
    (_envelope(_statement(subject=[{"digest": {"sha256": "a" * 64}}])), "named subject"),
    (_envelope(_statement(subject=[{"name": "", "digest": {"sha256": "a" * 64}}])), "named subject"),
    (_envelope(_statement(subject=[
        {"name": "registry.example/app", "digest": {"sha256": "a" * 64}},
        {"name": "registry.example/other", "digest": {"sha256": "a" * 64}},
    ])), "named subject"),
    (_envelope(_statement(predicateType="https://slsa.dev/provenance/v0.2")), "SLSA provenance v1"),
    (_envelope(_statement(_type="https://example.com/Statement")), "_type"),
    (_envelope(_statement(statement_type="https://in-toto.io/Statement/v0.1")), "_type"),
    (_envelope(_statement(predicate={"buildDefinition": {"buildType": "other"}})), "No verified"),
    (_envelope(["not", "an", "object"]), "JSON object"),
    (_envelope(_statement(predicate={"buildDefinition": {
        "buildType": BUILD_TYPE,
        "internalParameters": {"evidence": {"complete": False, "incomplete": [{"material": "m"}]}},
    }})), "malformed evidence"),
    (_envelope(_statement(predicate={"buildDefinition": {
        "buildType": BUILD_TYPE, "internalParameters": ["not", "an", "object"],
    }})), "malformed evidence"),
    (_envelope(_statement(predicate={"buildDefinition": ["not", "an", "object"]})), "No verified"),
])
def test_verified_output_policy_rejections(output, message):
    with pytest.raises(ProvenanceError, match=message):
        parse_verified_attestations(output, DIGEST)


def test_require_complete_rejects_any_incomplete_matching_attestation():
    output = _envelope(_statement()) + b"\n" + _envelope(_statement(complete=False))
    assert len(parse_verified_attestations(output, DIGEST)) == 2
    with pytest.raises(IncompleteProvenanceError):
        parse_verified_attestations(output, DIGEST, require_complete_evidence=True)


def test_verified_statement_must_match_repository(monkeypatch):
    wrong = _statement(subject=[{"name": "registry.example/other", "digest": {"sha256": "a" * 64}}])
    monkeypatch.setattr(provenance.subprocess, "run", FakeCosign(verify_output=_envelope(wrong)))
    with pytest.raises(ProvenanceError, match="image repository"):
        _attestor().verify(SUBJECT)
    with pytest.raises(ProvenanceError, match="image repository"):
        _attestor().attest_and_verify(SUBJECT, wrong, lambda _: None)


class FakeCosign:
    """Simulates cosign attest-blob plus registry referrer discovery for verify-attestation."""

    def __init__(self, *, verify_output=None, sign_code=0, verify_code=0, tamper=False):
        self.calls = []
        self.verify_output = verify_output
        self.sign_code = sign_code
        self.verify_code = verify_code
        self.tamper = tamper
        self.published = []

    def publish(self, bundle):
        self.calls.append(["publish"])
        self.published.append(bundle)
        if self.verify_output is None:
            self.verify_output = json.dumps(json.loads(bundle)).encode()

    def __call__(self, args, stdout=None, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["timeout"] > 0
        self.calls.append(list(args))
        if args[1] == "attest-blob":
            option = lambda name: next(a.split("=", 1)[1] for a in args if a.startswith(name))
            payload = Path(option("--statement=")).read_bytes()
            if self.tamper:
                payload = payload.replace(b"registry.example/app", b"registry.example/evil")
            Path(option("--bundle=")).write_text(json.dumps({
                "mediaType": provenance.SIGSTORE_BUNDLE_MEDIA_TYPE,
                "dsseEnvelope": {
                    "payloadType": provenance.DSSE_PAYLOAD_TYPE,
                    "payload": base64.b64encode(payload).decode(),
                    "signatures": [{"sig": "x"}],
                },
            }))
            return subprocess.CompletedProcess(args, self.sign_code)
        stdout.write(self.verify_output or b"")
        return subprocess.CompletedProcess(args, self.verify_code)


def _attestor():
    return provenance.CosignAttestor("cosign", "cosign.key", "cosign.pub", None, None)


STATEMENT = _statement()
SUBJECT = "registry.example/app@" + DIGEST


def test_attestor_signs_exact_v1_statement_publishes_then_verifies(monkeypatch):
    fake = FakeCosign()
    monkeypatch.setattr(provenance.subprocess, "run", fake)

    _attestor().attest_and_verify(SUBJECT, STATEMENT, fake.publish, require_complete_evidence=True)

    sign, publish, verify = fake.calls
    assert sign[:4] == ["cosign", "attest-blob", "--yes", "--new-bundle-format"]
    assert sign[-1] == "--key=cosign.key"
    assert publish == ["publish"]
    assert verify == [
        "cosign", "verify-attestation", "--new-bundle-format", "--type=slsaprovenance1",
        "--key=cosign.pub", "--", SUBJECT,
    ]
    signed = json.loads(base64.b64decode(json.loads(fake.published[0])["dsseEnvelope"]["payload"]))
    assert signed == STATEMENT and signed["_type"] == STATEMENT_V1


def test_attestor_rejects_other_statements_tampered_bundles_and_failures(monkeypatch):
    replayed = _statement()
    replayed["predicate"]["buildDefinition"]["externalParameters"] = {"tags": ["replayed"]}
    fake = FakeCosign(verify_output=_envelope(replayed))
    monkeypatch.setattr(provenance.subprocess, "run", fake)
    with pytest.raises(ProvenanceError, match="do not contain the statement"):
        _attestor().attest_and_verify(SUBJECT, STATEMENT, fake.publish)

    # Same JSON value, different bytes than were signed: not the attested statement.
    fake = FakeCosign(verify_output=_envelope(STATEMENT))
    monkeypatch.setattr(provenance.subprocess, "run", fake)
    with pytest.raises(ProvenanceError, match="do not contain the statement"):
        _attestor().attest_and_verify(SUBJECT, STATEMENT, fake.publish)

    fake = FakeCosign(tamper=True)
    monkeypatch.setattr(provenance.subprocess, "run", fake)
    with pytest.raises(ProvenanceError, match="exact provenance statement"):
        _attestor().attest_and_verify(SUBJECT, STATEMENT, fake.publish)
    assert fake.published == []

    fake = FakeCosign(sign_code=1)
    monkeypatch.setattr(provenance.subprocess, "run", fake)
    with pytest.raises(ProvenanceError, match="attest-blob exited"):
        _attestor().attest_and_verify(SUBJECT, STATEMENT, fake.publish)
    fake = FakeCosign()
    monkeypatch.setattr(provenance.subprocess, "run", fake)
    with pytest.raises(IncompleteProvenanceError):
        _attestor().attest_and_verify(
            SUBJECT, _statement(complete=False), fake.publish, require_complete_evidence=True,
        )
    with pytest.raises(ProvenanceError, match="subject"):
        _attestor().attest_and_verify(
            "registry.example/app@sha256:" + "b" * 64, STATEMENT, fake.publish,
        )
    assert fake.calls == []

    monkeypatch.setattr(provenance.subprocess, "run", FakeCosign(verify_code=1))
    with pytest.raises(ProvenanceError, match="verify-attestation exited"):
        _attestor().verify(SUBJECT)
    monkeypatch.setattr(provenance, "MAX_VERIFY_OUTPUT_BYTES", 4)
    monkeypatch.setattr(provenance.subprocess, "run", FakeCosign(verify_output=b"x" * 5))
    with pytest.raises(ProvenanceError, match="size limit"):
        _attestor().verify(SUBJECT)
    with pytest.raises(ProvenanceError, match="bundle exceeds"):
        _attestor().sign_statement(STATEMENT)
    with pytest.raises(ProvenanceError, match="immutable"):
        _attestor().verify("registry.example/app:v1")


def test_referrer_manifest_matches_cosign_bundle_layout():
    bundle = b'{"bundle": true}'
    subject = {"mediaType": "application/vnd.oci.image.index.v1+json", "size": 321, "digest": DIGEST}

    data, blobs = provenance.referrer_manifest(bundle, subject)

    manifest = json.loads(data)
    assert manifest["artifactType"] == provenance.SIGSTORE_BUNDLE_MEDIA_TYPE
    assert manifest["subject"] == subject
    assert manifest["config"]["mediaType"] == provenance.OCI_EMPTY_MEDIA_TYPE
    assert [layer["mediaType"] for layer in manifest["layers"]] == [provenance.SIGSTORE_BUNDLE_MEDIA_TYPE]
    assert manifest["annotations"]["dev.sigstore.bundle.predicateType"] == provenance.SLSA_PROVENANCE_V1
    assert blobs == {
        "sha256:" + hashlib.sha256(b"{}").hexdigest(): b"{}",
        "sha256:" + hashlib.sha256(bundle).hexdigest(): bundle,
    }


class ProvenanceRegistry(FakeRegistry):
    def __init__(self, failure=None):
        super().__init__()
        self.failure = failure
        self.blobs = {}
        self.referrer = None
        self.referrer_requests = 0

    def __call__(self, registry, repository, **kwargs):
        super().__call__(registry, repository, **kwargs)
        self.base_url = f"https://{registry}/v2"
        return self

    def verify_manifest_descriptor(self, reference, expected_digest, expected_size, expected_media_type):
        if self.failure == "subject" and self.events and self.events[-1][0] == "attest":
            raise builder_module.BlobIntegrityError("subject mismatch")
        return super().verify_manifest_descriptor(reference, expected_digest, expected_size, expected_media_type)

    def blob_descriptor_matches(self, digest, size):
        return digest in self.blobs and len(self.blobs[digest]) == size

    def upload_blob_monolithic(self, digest, path):
        if self.failure == "blob":
            raise builder_module.RegistryRequestError("sensitive registry detail")
        data = Path(path).read_bytes()
        assert "sha256:" + hashlib.sha256(data).hexdigest() == digest
        self.events.append(("artifact-blob", digest))
        self.blobs[digest] = data

    def put_manifest(self, reference, data, media_type):
        if self.failure == "manifest-put":
            raise builder_module.RegistryRequestError("sensitive registry detail")
        self._put("manifest", reference, data)
        self.referrer = reference
        return {"OCI-Subject": "sha256:" + "f" * 64} if self.failure == "subject-header" else {}

    def fetch_manifest_bytes(self, reference, accept, max_size, expected_digest=None):
        self.events.append(("artifact-read", reference))
        data = self.manifests[reference]
        if self.failure == "manifest-read":
            data += b" "
        content_type = "application/json" if self.failure == "manifest-type" else artifact.OCI_MANIFEST_MEDIA_TYPE
        return data, reference, content_type

    def pull_blob(self, digest, path, expected_size):
        self.events.append(("artifact-blob-read", digest))
        data = self.blobs[digest]
        if self.failure == "blob-read":
            data += b" "
        Path(path).write_bytes(data)
        return True

    def get_referrers(self, url, max_size):
        self.referrer_requests += 1
        self.events.append(("referrers", url))
        if self.failure == "referrers-404":
            return 404, b"", {}
        headers = {"Content-Type": artifact.OCI_INDEX_MEDIA_TYPE}
        if self.failure == "referrers-malformed":
            return 200, b"not-json", headers
        data = self.manifests[self.referrer]
        manifests = [{
            "mediaType": artifact.OCI_MANIFEST_MEDIA_TYPE,
            "digest": "sha256:" + "f" * 64 if self.failure == "referrers-digest" else self.referrer,
            "size": len(data) + (1 if self.failure == "referrers-wrong" else 0),
        }]
        if self.failure == "referrers-type":
            manifests[0]["artifactType"] = "application/example"
        if self.failure == "referrers-missing" or (
            self.failure == "referrers-delayed" and self.referrer_requests == 1
        ):
            manifests = []
        index = {"schemaVersion": 2, "mediaType": artifact.OCI_INDEX_MEDIA_TYPE, "manifests": manifests}
        return 200, json.dumps(index).encode(), headers


class FakeAttestor:
    def __init__(self, registry, fail=False):
        self.registry = registry
        self.fail = fail
        self.statements = []

    def attest_and_verify(self, subject, statement, publish, require_complete_evidence=False):
        self.registry.events.append(("attest", subject))
        self.statements.append(statement)
        publish(b'{"bundle": true}')
        self.registry.events.append(("attestation-verify", subject))
        if self.fail:
            raise ProvenanceError("verification rejected")

def _signed_builder(tmp_path, monkeypatch, registry, attestor, platform="linux/amd64"):
    context = _context(tmp_path, requirements=False)
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", _observed_base)
    builder = ImageBuilder(_config(
        tmp_path, context, include_paths=["app.py"], platform=platform, sign=True, tags=["stable"],
        sign_provider="cosign", sign_key="cosign.key", sign_verify_key="cosign.pub",
        attest_provenance=True,
    ))
    builder.build()
    _use_registry(monkeypatch, registry)
    monkeypatch.setattr(provenance, "create_attestor", lambda config: attestor)
    require_referrer = artifact.require_referrer
    monkeypatch.setattr(
        artifact, "require_referrer",
        lambda client, subject, descriptor: require_referrer(
            client, subject, descriptor, sleep=lambda _: None
        ),
    )
    return builder


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_signed_push_attests_published_digest_before_tags(tmp_path, monkeypatch, platform):
    registry = ProvenanceRegistry()
    attestor = FakeAttestor(registry)
    builder = _signed_builder(tmp_path, monkeypatch, registry, attestor, platform)

    builder.push(show_progress=False)

    digest = builder.index_digest or builder.manifest_digest
    subject = f"registry.example/team/app@{digest}"
    attest_index = registry.events.index(("attest", subject))
    assert registry.events.index(("sign", subject)) < registry.events.index(("verify", subject)) < attest_index
    kind = "index" if builder.index_digest else "manifest"
    assert registry.events.index((kind, "v1")) > attest_index
    assert attestor.statements == [builder.provenance_statement]
    assert attestor.statements[0]["_type"] == STATEMENT_V1
    referrers = [
        (reference, json.loads(data)) for reference, data in registry.manifests.items()
        if reference.startswith("sha256:") and b"subject" in data
    ]
    assert len(referrers) == 1
    reference, manifest = referrers[0]
    assert reference == "sha256:" + hashlib.sha256(registry.manifests[reference]).hexdigest()
    assert manifest["subject"]["digest"] == digest
    assert manifest["subject"]["mediaType"].endswith(f"image.{kind}.v1+json")
    assert manifest["annotations"] == {
        "dev.sigstore.bundle.content": "dsse-envelope",
        "dev.sigstore.bundle.predicateType": provenance.SLSA_PROVENANCE_V1,
    }
    bundle_digest = manifest["layers"][0]["digest"]
    assert registry.blobs[bundle_digest] == b'{"bundle": true}'
    assert registry.events.index(("manifest", reference)) < registry.events.index(("artifact-read", reference))
    assert registry.events.index(("artifact-blob-read", bundle_digest)) < next(
        i for i, event in enumerate(registry.events) if event[0] == "referrers"
    ) < registry.events.index(("attestation-verify", subject)) < registry.events.index((kind, "v1"))
    assert builder.attested_ref == subject


@pytest.mark.parametrize("failure", [
    "subject", "blob", "manifest-put", "subject-header", "manifest-read", "manifest-type",
    "blob-read", "referrers-404", "referrers-malformed", "referrers-missing",
    "referrers-wrong", "referrers-digest", "referrers-type",
])
@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_referrer_publication_failures_leave_image_tags_untouched(tmp_path, monkeypatch, failure, platform):
    registry = ProvenanceRegistry(failure)
    builder = _signed_builder(tmp_path, monkeypatch, registry, FakeAttestor(registry), platform)

    with pytest.raises(ProvenanceError, match="tags not attempted") as error:
        builder.push(show_progress=False)

    assert "sensitive registry detail" not in str(error.value)
    assert "v1" not in registry.manifests and "stable" not in registry.manifests
    assert builder.pushed_refs == []
    if failure in ("referrers-404", "referrers-malformed"):
        assert registry.referrer_requests == 1


def test_referrer_discovery_retries_before_tags(tmp_path, monkeypatch):
    registry = ProvenanceRegistry("referrers-delayed")
    builder = _signed_builder(tmp_path, monkeypatch, registry, FakeAttestor(registry))

    builder.push(show_progress=False)

    assert registry.referrer_requests == 2
    assert "v1" in registry.manifests


def test_corrupt_staged_bundle_leaves_image_tags_untouched(tmp_path, monkeypatch):
    registry = ProvenanceRegistry()
    builder = _signed_builder(tmp_path, monkeypatch, registry, FakeAttestor(registry))
    write_bytes = Path.write_bytes

    def corrupt_bundle(path, data):
        if path.parent.name.startswith("pycontainer-referrer-") and data != artifact.EMPTY_CONFIG:
            data = b"corrupted"
        return write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", corrupt_bundle)
    with pytest.raises(ProvenanceError, match="tags not attempted"):
        builder.push(show_progress=False)
    assert "v1" not in registry.manifests and "stable" not in registry.manifests
    assert registry.referrer is None


def test_attestation_failure_writes_no_tags(tmp_path, monkeypatch):
    registry = ProvenanceRegistry()
    builder = _signed_builder(tmp_path, monkeypatch, registry, FakeAttestor(registry, fail=True))

    with pytest.raises(ProvenanceError, match="tags not attempted: registry.example/team/app:v1"):
        builder.push(show_progress=False)
    assert "v1" not in registry.manifests
    assert builder.pushed_refs == []


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_subject_rechecked_after_attestation_before_tags(tmp_path, monkeypatch, platform):
    registry = ProvenanceRegistry()

    class MutatingAttestor(FakeAttestor):
        def attest_and_verify(self, subject, statement, publish, require_complete_evidence=False):
            super().attest_and_verify(subject, statement, publish, require_complete_evidence)
            self.registry.manifests[subject.rsplit("@", 1)[1]] = b"modified"

    builder = _signed_builder(tmp_path, monkeypatch, registry, MutatingAttestor(registry), platform)
    with pytest.raises(builder_module.RegistryRequestError, match="tags not attempted"):
        builder.push(show_progress=False)
    assert "v1" not in registry.manifests and "stable" not in registry.manifests


def test_attestation_rejects_statement_for_a_different_digest(tmp_path, monkeypatch):
    registry = ProvenanceRegistry()
    attestor = FakeAttestor(registry)
    builder = _signed_builder(tmp_path, monkeypatch, registry, attestor)
    builder.provenance_statement["subject"][0]["digest"]["sha256"] = "f" * 64

    with pytest.raises(ProvenanceError, match="subject does not match"):
        builder.push(show_progress=False)
    assert attestor.statements == [] and "v1" not in registry.manifests


def test_attestation_rejects_push_to_a_different_repository(tmp_path, monkeypatch):
    registry = ProvenanceRegistry()
    attestor = FakeAttestor(registry)
    builder = _signed_builder(tmp_path, monkeypatch, registry, attestor)

    with pytest.raises(ValueError, match="different repository"):
        builder.push(registry_url="mirror.example/team/app:v1", show_progress=False)
    assert registry.events == [] and attestor.statements == []


COSIGN_KEY = {
    "attest_provenance": True, "sign": True, "sign_provider": "cosign",
    "sign_key": "cosign.key", "sign_verify_key": "cosign.pub",
}


@pytest.mark.parametrize("options, message", [
    ({"attest_provenance": True}, "sign = true"),
    ({"attest_provenance": True, "sign": True, "sign_provider": "notation", "sign_key": "k"}, "Notation"),
    ({**COSIGN_KEY, "provenance_verify_key": "k", "provenance_certificate_identity": "i",
      "provenance_certificate_oidc_issuer": "o"}, "either"),
    ({**COSIGN_KEY, "provenance_certificate_identity": "i"}, "either"),
    ({"provenance_verify_key": "cosign.pub"}, "require attest_provenance"),
    ({"provenance": "yes"}, "boolean"),
    ({**COSIGN_KEY, "provenance_verify_key": "-----BEGIN PRIVATE KEY-----"}, "secret material"),
])
def test_invalid_provenance_configuration_is_rejected(options, message):
    with pytest.raises(ValueError, match=message):
        BuildConfig(**options)


def test_completeness_and_attestation_imply_provenance():
    assert BuildConfig(provenance_require_complete=True).provenance is True
    assert BuildConfig(**COSIGN_KEY).provenance is True


def test_attestation_verifier_defaults_to_signature_trust_policy():
    keyed = BuildConfig(**COSIGN_KEY)
    assert keyed.provenance_verify_key == "cosign.pub"
    assert keyed.provenance_certificate_identity is None
    keyless = BuildConfig(
        sign=True, sign_provider="cosign", attest_provenance=True,
        sign_certificate_identity="i", sign_certificate_oidc_issuer="o",
    )
    assert (keyless.provenance_verify_key, keyless.provenance_certificate_identity,
            keyless.provenance_certificate_oidc_issuer) == (None, "i", "o")
    explicit = BuildConfig(**COSIGN_KEY, provenance_verify_key="other.pub")
    assert explicit.provenance_verify_key == "other.pub"


def _run_cli(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["pycontainer", *args])
    cli.main()


def test_cli_rejects_provenance_dry_run_and_unpushed_attestation(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, "build", "--context", str(tmp_path), "--provenance", "--dry-run")
    assert "--provenance cannot be used with --dry-run" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _run_cli(
            monkeypatch, "build", "--context", str(tmp_path), "--sign", "--sign-provider",
            "cosign", "--sign-key", "cosign.key", "--sign-verify-key", "cosign.pub",
            "--attest-provenance",
        )
    assert "requires --push" in capsys.readouterr().err


def test_cli_verify_provenance_reports_and_fails_closed(monkeypatch, capsys):
    class Verifier:
        def __init__(self, result):
            self.result = result
            self.calls = []

        def verify(self, image, require_complete_evidence=False):
            self.calls.append((image, require_complete_evidence))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    ok = Verifier([_statement(), _statement(complete=False)])
    monkeypatch.setattr(provenance, "create_verifier", lambda key, identity, issuer: ok)
    image = "registry.example/app@" + DIGEST
    _run_cli(monkeypatch, "verify-provenance", image, "--key", "cosign.pub")
    assert "Verified 2 provenance attestation(s)" in capsys.readouterr().out
    assert "1 record complete evidence" in capsys.readouterr().out or True

    failing = Verifier(IncompleteProvenanceError("incomplete"))
    monkeypatch.setattr(provenance, "create_verifier", lambda key, identity, issuer: failing)
    with pytest.raises(SystemExit) as exit_info:
        _run_cli(monkeypatch, "verify-provenance", image, "--key", "k", "--require-complete")
    assert exit_info.value.code == 1
    assert failing.calls == [(image, True)]
    assert "Provenance verification failed" in capsys.readouterr().err


def test_verifier_requires_exactly_one_trust_mode():
    with pytest.raises(ValueError, match="either --key"):
        provenance.create_verifier(None, None, None)
    with pytest.raises(ValueError, match="either --key"):
        provenance.create_verifier("k", "i", "o")
    with pytest.raises(ValueError, match="either --key"):
        provenance.create_verifier(None, "i", None)
