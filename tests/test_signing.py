import hashlib
import json
import os
import subprocess
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

from pycontainer import builder as builder_module
from pycontainer import signing
from pycontainer.blob_store import BlobIntegrityError
from pycontainer.builder import ImageBuilder, LayerIntegrityError
from pycontainer.config import BuildConfig
from pycontainer.registry_client import RegistryAuthenticationError, RegistryRequestError


COSIGN_KEYLESS = {
    "sign_certificate_identity": "https://github.com/example/app/.github/workflows/release.yml@refs/heads/main",
    "sign_certificate_oidc_issuer": "https://token.actions.githubusercontent.com",
}
COSIGN_KEY = {"sign_key": "env://COSIGN_KEY", "sign_verify_key": "cosign.pub"}


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _builder(tmp_path, monkeypatch, platform="linux/amd64"):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n")
    config = BuildConfig(
        tag="registry.example/team/app:v1",
        tags=["stable"],
        context_dir=str(context),
        output_dir=str(tmp_path / "out"),
        include_paths=["app.py"],
        use_cache=False,
        platform=platform,
        sign=True,
        sign_provider="cosign",
        **COSIGN_KEYLESS,
    )
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], None),
    )
    builder = ImageBuilder(config)
    builder.build()
    return builder


class FakeRegistry:
    def __init__(self):
        self.events = []
        self.manifests = {}
        self.fail_put = None
        self.fail_after_put = False
        self.fail_read = None
        self.fail_head = None
        self.registry = None
        self.repository = None

    def __call__(self, registry, repository, **kwargs):
        self.registry = registry
        self.repository = repository
        return self

    def push_blob(self, digest, path, check_exists=True):
        assert Path(path).is_file()
        self.events.append(("blob", digest))
        return True

    def _put(self, kind, reference, data):
        self.events.append((kind, reference))
        if reference == self.fail_put:
            if self.fail_after_put:
                self.manifests[reference] = data
            raise RegistryRequestError("simulated registry failure containing secret")
        self.manifests[reference] = data
        return True

    def push_manifest(self, reference, data):
        return self._put("manifest", reference, data)

    def push_index(self, reference, data):
        return self._put("index", reference, data)

    def pull_manifest(self, reference, expected_digest=None, expected_size=None):
        self.events.append(("read", reference))
        if reference == self.fail_read:
            raise BlobIntegrityError("simulated mismatched registry response")
        data = self.manifests[reference]
        digest = _digest(data)
        if digest != expected_digest or len(data) != expected_size:
            raise BlobIntegrityError("registry content did not match its descriptor")
        return json.loads(data), digest

    def verify_manifest_descriptor(self, reference, expected_digest, expected_size, expected_media_type):
        self.events.append(("head", reference))
        if reference == self.fail_head:
            raise BlobIntegrityError("simulated mismatched signer-visible descriptor")
        data = self.manifests[reference]
        if (
            _digest(data) != expected_digest
            or len(data) != expected_size
            or json.loads(data).get("mediaType") != expected_media_type
        ):
            raise BlobIntegrityError("registry HEAD did not match its descriptor")


class FakeSigner:
    def __init__(self, registry, fail=False, race=False, fail_verify=False):
        self.registry = registry
        self.fail = fail
        self.race = race
        self.fail_verify = fail_verify

    def sign(self, subject):
        self.registry.events.append(("sign", subject))
        if self.race:
            self.registry.manifests["v2"] = b"another writer's manifest"
        if self.fail:
            raise signing.SigningError("provider exited unsuccessfully")

    def verify(self, subject):
        self.registry.events.append(("verify", subject))
        if self.fail_verify:
            raise signing.SigningError("verifier exited unsuccessfully")


def _use_registry(monkeypatch, registry, *, fail=False, race=False, fail_verify=False):
    monkeypatch.setattr(builder_module, "RegistryClient", registry)
    monkeypatch.setattr(
        builder_module, "get_registry_auth",
        lambda registry, bearer_token=None, username=None, password=None: SimpleNamespace(
            bearer_token=bearer_token, username=username, password=password
        ),
    )
    monkeypatch.setattr(
        builder_module, "create_signer",
        lambda config: FakeSigner(registry, fail=fail, race=race, fail_verify=fail_verify),
    )


@pytest.mark.parametrize(
    "platform",
    ["linux/amd64", "linux/amd64,linux/arm64", "linux/amd64,linux/arm64,linux/arm/v7"],
)
def test_signed_push_verifies_and_signs_immutable_top_level_before_tags(
    tmp_path, monkeypatch, platform
):
    builder = _builder(tmp_path, monkeypatch, platform)
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry, race=True)

    builder.push(registry_url="mirror.example/other/app:v2", show_progress=False)

    digest = builder.index_digest or builder.manifest_digest
    subject = f"mirror.example/other/app@{digest}"
    assert registry.registry == "mirror.example"
    assert registry.repository == "other/app"
    assert ("sign", subject) in registry.events
    sign_index = registry.events.index(("sign", subject))
    verify_index = registry.events.index(("verify", subject))
    stage_kind = "index" if builder.index_digest else "manifest"
    assert registry.events.index((stage_kind, digest)) < sign_index < verify_index
    assert registry.events.index(("read", digest)) < sign_index
    assert registry.events.index(("head", digest)) < sign_index
    assert all(
        registry.events.index((stage_kind, tag)) > verify_index
        and registry.events.index(("read", tag)) > verify_index
        for tag in ("v2", "stable")
    )
    assert all(_digest(registry.manifests[tag]) == digest for tag in ("v2", "stable"))
    assert builder.pushed_refs == [
        "mirror.example/other/app:v2", "mirror.example/other/app:stable"
    ]
    assert builder.signed_ref == subject
    assert digest != builder.config_digest
    assert digest != _digest((tmp_path / "out" / "index.json").read_bytes())
    if builder.index_digest:
        for image in builder.platform_images:
            assert registry.events.index(("manifest", image.manifest_digest)) < sign_index
            assert registry.events.index(("read", image.manifest_digest)) < sign_index
            assert registry.events.index(("head", image.manifest_digest)) < sign_index


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_unsigned_push_does_not_invoke_signer_or_signed_readback(
    tmp_path, monkeypatch, platform
):
    builder = _builder(tmp_path, monkeypatch, platform)
    builder.config.sign = False
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)

    def reject_signer(config):
        pytest.fail("Unsigned pushes must not initialize a signer")

    monkeypatch.setattr(builder_module, "create_signer", reject_signer)
    builder.push(show_progress=False)

    assert builder.pushed_refs == [
        "registry.example/team/app:v1", "registry.example/team/app:stable"
    ]
    assert not any(event[0] in {"sign", "read", "head"} for event in registry.events)


def test_signed_push_rejects_insecure_registry_before_publication(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch)
    builder.config.insecure_registries = ["mirror.example"]
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)

    with pytest.raises(ValueError, match="Signed.*HTTPS"):
        builder.push(registry_url="mirror.example/other/app:v2", show_progress=False)

    assert registry.events == []
    assert builder.pushed_refs == []


def test_signed_push_uses_scanned_snapshot_when_source_changes(tmp_path, monkeypatch):
    from pycontainer import scanning

    builder = _builder(tmp_path, monkeypatch)
    builder.config.scan = True
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)
    original_layer = Path(builder.layers[-1].tar_path)
    original_push_blob = registry.push_blob
    scanned = []

    def scan(root, descriptor, config, *, report):
        scanned.append((root, descriptor["digest"]))
        original_layer.write_bytes(b"changed after snapshot")

    def push_blob(digest, path, check_exists=True):
        assert path.is_relative_to(scanned[0][0])
        assert _digest(path.read_bytes()) == digest
        return original_push_blob(digest, path, check_exists)

    monkeypatch.setattr(scanning, "scan_image", scan)
    monkeypatch.setattr(registry, "push_blob", push_blob)
    builder.push(show_progress=False)

    subject = f"registry.example/team/app@{builder.manifest_digest}"
    assert scanned[0][1] == builder.manifest_digest
    assert ("verify", subject) in registry.events
    assert ("manifest", "v1") in registry.events
    assert original_layer.read_bytes() == b"changed after snapshot"


def test_signed_push_scan_failure_leaves_tags_and_signer_untouched(tmp_path, monkeypatch):
    from pycontainer import scanning

    builder = _builder(tmp_path, monkeypatch)
    builder.config.scan = True
    builder.signed_ref = "stale"
    builder.pushed_refs = ["stale"]
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)

    def reject_scan(root, descriptor, config, *, report):
        raise scanning.ScanError("scanner", "simulated scan failure")

    monkeypatch.setattr(scanning, "scan_image", reject_scan)
    with pytest.raises(scanning.ScanError, match="simulated scan failure"):
        builder.push(show_progress=False)
    assert registry.events == []
    assert builder.signed_ref is None
    assert builder.pushed_refs == []


def test_signed_scan_rejects_conflicting_tag_descriptors(tmp_path, monkeypatch):
    from pycontainer import scanning

    builder = _builder(tmp_path, monkeypatch)
    builder.config.scan = True
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)
    index_path = Path(builder.config.output_dir) / "index.json"
    index = json.loads(index_path.read_text())
    index["manifests"][1]["size"] += 1
    index_path.write_text(json.dumps(index))
    monkeypatch.setattr(
        scanning, "scan_image",
        lambda *args, **kwargs: pytest.fail("ambiguous descriptors must not be scanned"),
    )

    with pytest.raises(scanning.ScanError, match="ambiguous"):
        builder.push(show_progress=False)
    assert registry.events == []


def test_signed_multi_platform_scan_rejected_before_publication(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch, "linux/amd64,linux/arm64")
    builder.config.scan = True
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)

    with pytest.raises(ValueError, match="multi-platform"):
        builder.push(show_progress=False)
    assert registry.events == []


def test_unsigned_push_clears_previous_signed_subject(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch)
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)
    builder.push(show_progress=False)
    assert builder.signed_ref is not None

    builder.config.sign = False
    builder.push(show_progress=False)

    assert builder.signed_ref is None


@pytest.mark.parametrize("failure", ["stage", "read", "head", "sign"])
@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_signed_push_does_not_move_tags_after_a_failure(
    tmp_path, monkeypatch, failure, platform
):
    builder = _builder(tmp_path, monkeypatch, platform)
    digest = builder.index_digest or builder.manifest_digest
    registry = FakeRegistry()
    if failure == "stage":
        registry.fail_put = digest
    elif failure == "read":
        registry.fail_read = digest
    elif failure == "head":
        registry.fail_head = digest
    _use_registry(monkeypatch, registry, fail=failure == "sign")

    with pytest.raises((RegistryRequestError, signing.SigningError)) as raised:
        builder.push(show_progress=False)

    assert builder.pushed_refs == []
    assert not any(
        event[1] in {"v1", "stable"}
        for event in registry.events if event[0] in {"manifest", "index"}
    )
    assert "secret" not in str(raised.value)
    assert "not attempted" in str(raised.value).lower()
    if failure in {"read", "head"}:
        acknowledged = str(raised.value).split("Acknowledged manifest writes: ", 1)[1]
        acknowledged = acknowledged.split(". GET-verified manifests:", 1)[0]
        assert f"registry.example/team/app@{digest}" in acknowledged
    if failure != "sign":
        assert not any(event[0] == "sign" for event in registry.events)


def test_signed_push_redacts_registry_authentication_failures(
    tmp_path, monkeypatch, capsys
):
    builder = _builder(tmp_path, monkeypatch)
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)
    synthetic_password = "synthetic-secret"

    def reject_auth(*args, **kwargs):
        raise RegistryAuthenticationError(synthetic_password)

    monkeypatch.setattr(registry, "push_blob", reject_auth)
    with pytest.raises(RegistryRequestError, match="RegistryAuthenticationError") as raised:
        builder.push(username="synthetic-user", password=synthetic_password, show_progress=False)

    rendered = "".join(traceback.format_exception(raised.value))
    assert synthetic_password not in rendered
    assert synthetic_password not in repr(capsys.readouterr())
    assert "not attempted" in str(raised.value).lower()
    assert builder.pushed_refs == []


def test_index_staging_failure_reports_verified_prior_publications(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch, "linux/amd64,linux/arm64")
    registry = FakeRegistry()
    first, second = builder.platform_images
    registry.fail_put = second.manifest_digest
    _use_registry(monkeypatch, registry)

    with pytest.raises(RegistryRequestError) as raised:
        builder.push(show_progress=False)

    message = str(raised.value)
    assert f"registry.example/team/app@{first.manifest_digest}" in message
    assert f"registry.example/team/app@{second.manifest_digest}" in message
    assert "Blob results:" in message
    assert "GET-verified manifests:" in message
    assert "outcome may be unknown" in message
    assert "not attempted" in message.lower()
    assert not any(event[0] == "sign" for event in registry.events)


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_signed_push_does_not_move_tags_when_signature_verification_fails(
    tmp_path, monkeypatch, platform
):
    builder = _builder(tmp_path, monkeypatch, platform)
    digest = builder.index_digest or builder.manifest_digest
    subject = f"registry.example/team/app@{digest}"
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry, fail_verify=True)

    with pytest.raises(signing.SigningError, match="Verifying the signature") as raised:
        builder.push(show_progress=False)

    assert ("verify", subject) in registry.events
    assert "not attempted" in str(raised.value).lower()
    assert builder.signed_ref is None
    assert builder.pushed_refs == []
    assert not any(
        event[1] in {"v1", "stable"}
        for event in registry.events if event[0] in {"manifest", "index"}
    )


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_signed_push_rechecks_subject_after_signing_before_tags(
    tmp_path, monkeypatch, platform
):
    builder = _builder(tmp_path, monkeypatch, platform)
    digest = builder.index_digest or builder.manifest_digest
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)

    class DisappearingSubjectSigner:
        def sign(self, subject):
            registry.events.append(("sign", subject))
            registry.fail_read = digest

        def verify(self, subject):
            registry.events.append(("verify", subject))

    monkeypatch.setattr(builder_module, "create_signer", lambda config: DisappearingSubjectSigner())
    with pytest.raises(RegistryRequestError, match="reverified") as raised:
        builder.push(show_progress=False)

    assert "not attempted" in str(raised.value).lower()
    assert builder.signed_ref is None
    assert not any(
        event[1] in {"v1", "stable"}
        for event in registry.events if event[0] in {"manifest", "index"}
    )


@pytest.mark.parametrize("timed_out", [False, True])
def test_signed_push_reports_partial_tag_writes(
    tmp_path, monkeypatch, timed_out
):
    builder = _builder(tmp_path, monkeypatch)
    registry = FakeRegistry()
    registry.fail_put = "stable"
    registry.fail_after_put = timed_out
    _use_registry(monkeypatch, registry)

    with pytest.raises(RegistryRequestError) as raised:
        builder.push(show_progress=False)

    message = str(raised.value)
    assert builder.pushed_refs == ["registry.example/team/app:v1"]
    assert "registry.example/team/app:v1" in message
    assert "registry.example/team/app:stable" in message
    assert "outcome unknown" in message.lower()
    assert "rollback" in message.lower()
    assert "secret" not in message


@pytest.mark.parametrize("failing_read", ["fail_read", "fail_head"])
def test_signed_push_stops_if_a_concurrent_writer_changes_a_tag(
    tmp_path, monkeypatch, failing_read
):
    builder = _builder(tmp_path, monkeypatch)
    registry = FakeRegistry()
    setattr(registry, failing_read, "v1")
    _use_registry(monkeypatch, registry)

    with pytest.raises(RegistryRequestError, match="readback") as raised:
        builder.push(show_progress=False)

    assert builder.pushed_refs == ["registry.example/team/app:v1"]
    assert "registry.example/team/app:stable" in str(raised.value)
    assert ("manifest", "stable") not in registry.events


@pytest.mark.parametrize("suffix", ["", ".sig", ".att", ".sbom"])
@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
@pytest.mark.parametrize("other_subject", [False, True])
def test_signature_storage_tags_cannot_be_requested_image_tags(
    tmp_path, monkeypatch, suffix, platform, other_subject
):
    builder = _builder(tmp_path, monkeypatch, platform)
    digest = "sha256:" + "f" * 64 if other_subject else builder.index_digest or builder.manifest_digest
    builder.config.tags = [f"sha256-{digest[7:]}{suffix}"]
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)

    with pytest.raises(ValueError, match="signature"):
        builder.push(show_progress=False)

    assert registry.events == []


def test_signed_push_rejects_tampered_layout_before_network(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch)
    manifest = tmp_path / "out" / "blobs" / "sha256" / builder.manifest_digest[7:]
    manifest.write_bytes(b"tampered")
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)

    with pytest.raises(LayerIntegrityError):
        builder.push(show_progress=False)
    assert registry.events == []


def test_signed_push_rejects_digest_override(tmp_path, monkeypatch):
    builder = _builder(tmp_path, monkeypatch)
    registry = FakeRegistry()
    _use_registry(monkeypatch, registry)
    with pytest.raises(ValueError, match="tag"):
        builder.push(registry_url="mirror.example/team/app@" + builder.manifest_digest)
    assert registry.events == []


@pytest.mark.parametrize(
    "options",
    [
        {"sign": True},
        {"sign": True, "sign_provider": "unknown"},
        {"sign": True, "sign_provider": "cosign", "sign_plugin": "plugin"},
        {"sign": True, "sign_provider": "notation"},
        {"sign": True, "sign_provider": "notation", "sign_plugin": "plugin"},
        {
            "sign": True, "sign_provider": "notation", "sign_key": "named",
            "sign_plugin": "plugin", "sign_key_id": "id",
        },
        {"sign": True, "sign_provider": "cosign", "sign_key": "-----BEGIN PRIVATE KEY-----"},
        {"sign": True, "sign_provider": "cosign", "sign_key": "kms://user:secret@host/key"},
        {"sign": True, "sign_provider": "cosign", "sign_key": "pkcs11:object=key?pin-value=1234"},
        {"sign": True, "sign_provider": "cosign", "sign_key": "pkcs11:object=key?pin%2Dvalue%3D1234"},
        {"sign": True, "sign_provider": "cosign", "sign_key": "pkcs11:object=key?pin-source=data:secret"},
        {"sign": True, "sign_provider": "cosign", "sign_timestamp_url": "https://tsa.example"},
        {"sign": True, "sign_provider": "notation", "sign_key": "named", "sign_timestamp_url": "https://user:secret@tsa.example"},
        {"sign": True, "sign_provider": "notation", "sign_key": "named", "sign_timestamp_root_cert": "ca.pem"},
        {"sign": True, "sign_provider": "notation", "sign_key": "named", "sign_signature_format": "unsupported"},
        {"sign": True, "sign_provider": "cosign"},
        {"sign": True, "sign_provider": "cosign", "sign_certificate_identity": "id"},
        {"sign": True, "sign_provider": "cosign", "sign_certificate_oidc_issuer": "https://issuer.example"},
        {"sign": True, "sign_provider": "cosign", **COSIGN_KEYLESS, "sign_verify_key": "cosign.pub"},
        {"sign": True, "sign_provider": "cosign", "sign_key": "cosign.key"},
        {"sign": True, "sign_provider": "cosign", **COSIGN_KEY, **COSIGN_KEYLESS},
        {"sign": True, "sign_provider": "notation", "sign_key": "named", "sign_verify_key": "cosign.pub"},
        {"sign": True, "sign_provider": "notation", "sign_key": "named", **COSIGN_KEYLESS},
        {"sign": True, "sign_provider": "cosign", **COSIGN_KEY, "sign_verify_key": "-----BEGIN PRIVATE KEY-----"},
    ],
)
def test_invalid_signing_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        BuildConfig(**options)


def _fake_version_run(commands, provider, version):
    def run(args, **kwargs):
        commands.append((args, kwargs))
        if args[1] == "version":
            output = (
                json.dumps({"gitVersion": version}) if provider == "cosign"
                else f"Notation - a tool to sign and verify artifacts.\nVersion: {version}\n"
            )
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        return SimpleNamespace(returncode=0)
    return run


@pytest.mark.parametrize(
    ("provider", "options", "expected"),
    [
        ("cosign", COSIGN_KEYLESS, ["--yes", "--upload=true"]),
        ("cosign", COSIGN_KEY, ["--yes", "--upload=true", "--key=env://COSIGN_KEY"]),
        ("notation", {"sign_key": "release-key"}, ["--force-referrers-tag=false", "--key=release-key"]),
        ("notation", {"sign_plugin": "plugin", "sign_key_id": "key-id"}, ["--force-referrers-tag=false", "--plugin=plugin", "--id=key-id"]),
        (
            "notation",
            {"sign_key": "release-key", "sign_timestamp_url": "https://tsa.example", "sign_timestamp_root_cert": "root.pem", "sign_signature_format": "cose"},
            ["--force-referrers-tag=false", "--key=release-key", "--timestamp-url=https://tsa.example", "--timestamp-root-cert=root.pem", "--signature-format=cose"],
        ),
    ],
)
def test_signer_uses_version_checked_argv_without_credentials(
    monkeypatch, provider, options, expected
):
    commands = []
    monkeypatch.setattr(signing, "_find_executable", lambda name: name + ".exe")
    monkeypatch.setattr(
        signing.subprocess, "run",
        _fake_version_run(commands, provider, "v3.1.4" if provider == "cosign" else "1.3.2"),
    )
    monkeypatch.setenv("COSIGN_PASSWORD", "a-private-password")
    monkeypatch.setenv("NOTATION_PASSWORD", "a-registry-password")
    monkeypatch.delenv("COSIGN_REPOSITORY", raising=False)
    signer = signing.create_signer(BuildConfig(sign=True, sign_provider=provider, **options))
    subject = "registry.example/team/app@sha256:" + "a" * 64

    signer.sign(subject)

    args, kwargs = commands[-1]
    assert args == [provider + ".exe", "sign", *expected, "--", subject]
    assert "a-private-password" not in repr(args)
    assert "a-registry-password" not in repr(args)
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["timeout"] > 0


@pytest.mark.parametrize(
    ("provider", "version"),
    [("cosign", "v2.5.0"), ("cosign", "v4.0.0"), ("notation", "1.2.0"), ("notation", "2.0.0")],
)
def test_incompatible_signer_versions_are_rejected(monkeypatch, provider, version):
    commands = []
    monkeypatch.delenv("COSIGN_REPOSITORY", raising=False)
    monkeypatch.setattr(signing, "_find_executable", lambda name: name + ".exe")
    monkeypatch.setattr(
        signing.subprocess, "run", _fake_version_run(commands, provider, version)
    )
    options = {"sign_key": "named"} if provider == "notation" else COSIGN_KEYLESS
    with pytest.raises(signing.SigningError, match="version"):
        signing.create_signer(BuildConfig(sign=True, sign_provider=provider, **options))


@pytest.mark.parametrize(
    ("provider", "version"), [("cosign", "v3.1.4+build.1"), ("notation", "1.3.2+unreleased")]
)
def test_supported_versions_with_build_metadata_are_accepted(monkeypatch, provider, version):
    commands = []
    monkeypatch.setattr(signing, "_find_executable", lambda name: name + ".exe")
    monkeypatch.setattr(signing.subprocess, "run", _fake_version_run(commands, provider, version))
    monkeypatch.delenv("COSIGN_REPOSITORY", raising=False)
    options = {"sign_key": "named"} if provider == "notation" else COSIGN_KEYLESS
    assert signing.create_signer(BuildConfig(sign=True, sign_provider=provider, **options))


def test_signer_missing_or_failing_does_not_expose_output(monkeypatch, capsys):
    config = BuildConfig(sign=True, sign_provider="cosign", **COSIGN_KEYLESS)
    monkeypatch.delenv("COSIGN_REPOSITORY", raising=False)
    monkeypatch.setattr(signing, "_find_executable", lambda name: None)
    with pytest.raises(signing.SigningError, match="not found"):
        signing.create_signer(config)

    monkeypatch.setattr(signing, "_find_executable", lambda name: name + ".exe")
    commands = []
    version_run = _fake_version_run(commands, "cosign", "v3.1.4")

    def run(args, **kwargs):
        if args[1] == "version":
            return version_run(args, **kwargs)
        return SimpleNamespace(returncode=1, stdout="secret", stderr="secret")

    monkeypatch.setattr(signing.subprocess, "run", run)
    monkeypatch.delenv("COSIGN_REPOSITORY", raising=False)
    signer = signing.create_signer(config)
    with pytest.raises(signing.SigningError) as raised:
        signer.sign("registry.example/app@sha256:" + "a" * 64)
    assert "secret" not in str(raised.value)
    assert "secret" not in repr(capsys.readouterr())


@pytest.mark.skipif(os.name != "nt", reason="Windows batch files can invoke a shell")
@pytest.mark.parametrize("extension", [".bat", ".CMD"])
def test_windows_batch_signer_is_rejected(monkeypatch, extension):
    monkeypatch.setattr(signing, "_find_executable", lambda name: name + extension)
    monkeypatch.delenv("COSIGN_REPOSITORY", raising=False)
    with pytest.raises(signing.SigningError, match="native Windows executable"):
        signing.create_signer(BuildConfig(sign=True, sign_provider="cosign", **COSIGN_KEYLESS))


@pytest.mark.skipif(os.name != "nt", reason="Windows PATH resolution differs from Unix")
def test_windows_signer_lookup_excludes_the_current_directory(tmp_path, monkeypatch):
    untrusted = tmp_path / "workspace"
    trusted = tmp_path / "trusted"
    untrusted.mkdir()
    trusted.mkdir()
    (untrusted / "cosign.exe").touch()
    (trusted / "cosign.exe").touch()
    monkeypatch.chdir(untrusted)
    monkeypatch.setenv("PATHEXT", ".EXE;.BAT;.CMD")
    monkeypatch.setenv("PATH", str(trusted))
    original_which = signing.shutil.which

    def python311_which(candidate):
        if not Path(candidate).suffix:
            return None
        return original_which(candidate)

    monkeypatch.setattr(signing.shutil, "which", python311_which)
    assert Path(signing._find_executable("cosign")).resolve() == (trusted / "cosign.exe").resolve()
    monkeypatch.setenv("PATH", str(untrusted))
    assert signing._find_executable("cosign") is None


def test_cosign_signature_repository_override_is_rejected(monkeypatch):
    monkeypatch.setenv("COSIGN_REPOSITORY", "other.example/team/signatures")
    with pytest.raises(signing.SigningError, match="COSIGN_REPOSITORY"):
        signing.create_signer(BuildConfig(sign=True, sign_provider="cosign", **COSIGN_KEYLESS))


def test_signer_timeout_is_explicit_and_does_not_expose_output(monkeypatch):
    commands = []
    monkeypatch.setattr(signing, "_find_executable", lambda name: name + ".exe")
    monkeypatch.delenv("COSIGN_REPOSITORY", raising=False)
    version_run = _fake_version_run(commands, "cosign", "v3.1.4")

    def run(args, **kwargs):
        if args[1] == "version":
            return version_run(args, **kwargs)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output="secret")

    monkeypatch.setattr(signing.subprocess, "run", run)
    signer = signing.create_signer(BuildConfig(sign=True, sign_provider="cosign", **COSIGN_KEYLESS))
    with pytest.raises(signing.SigningError, match="timed out") as raised:
        signer.sign("registry.example/app@sha256:" + "a" * 64)
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({"trustPolicies": [{"signatureVerification": {"level": "strict"}}]}, True),
        ({"trustPolicies": [{"signatureVerification": {"level": "skip"}}]}, False),
        ({"trustPolicies": [{"signatureVerification": {"level": "audit"}}]}, False),
        ({"trustPolicies": [{"signatureVerification": {"level": "strict", "override": {"authenticity": "log"}}}]}, False),
        ({"trustPolicies": []}, False),
        ({"trustPolicies": [None]}, False),
        (None, False),
    ],
)
def test_real_notation_verification_requires_strict_policy(policy, expected):
    from test_signing_integration import _notation_policy_is_strict

    assert _notation_policy_is_strict(policy) is expected


def _provider_run(commands, provider, *, verify_output="", policy=None, policies=None):
    version_run = _fake_version_run(commands, provider, "v3.1.4" if provider == "cosign" else "1.3.2")
    policies = list(policies) if policies is not None else None

    def run(args, **kwargs):
        if args[1] == "version":
            return version_run(args, **kwargs)
        commands.append((args, kwargs))
        if args[1:3] == ["policy", "show"]:
            current = policies.pop(0) if policies else policy
            return SimpleNamespace(returncode=0, stdout=json.dumps(current), stderr="")
        return SimpleNamespace(returncode=0, stdout=verify_output, stderr="")
    return run


def _create_signer(monkeypatch, commands, provider, options, **run_options):
    monkeypatch.setattr(signing, "_find_executable", lambda name: name + ".exe")
    monkeypatch.delenv("COSIGN_REPOSITORY", raising=False)
    monkeypatch.setattr(signing.subprocess, "run", _provider_run(commands, provider, **run_options))
    return signing.create_signer(BuildConfig(sign=True, sign_provider=provider, **options))


SUBJECT = "registry.example/team/app@sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (COSIGN_KEY, ["--key=cosign.pub"]),
        (
            COSIGN_KEYLESS,
            [
                "--certificate-identity=" + COSIGN_KEYLESS["sign_certificate_identity"],
                "--certificate-oidc-issuer=" + COSIGN_KEYLESS["sign_certificate_oidc_issuer"],
            ],
        ),
    ],
)
def test_cosign_verifies_exact_digest_with_configured_trust(monkeypatch, options, expected):
    commands = []
    signer = _create_signer(monkeypatch, commands, "cosign", options)

    signer.verify(SUBJECT)

    args, kwargs = commands[-1]
    assert args == ["cosign.exe", "verify", *expected, "--", SUBJECT]
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["timeout"] > 0


def test_cosign_verification_failure_is_explicit_and_redacted(monkeypatch):
    commands = []
    signer = _create_signer(monkeypatch, commands, "cosign", COSIGN_KEYLESS)
    monkeypatch.setattr(
        signing.subprocess, "run",
        lambda args, **kwargs: SimpleNamespace(returncode=1, stdout="secret", stderr="secret"),
    )
    with pytest.raises(signing.SigningError, match="verification") as raised:
        signer.verify(SUBJECT)
    assert "secret" not in str(raised.value)


def test_signer_verify_requires_an_immutable_subject(monkeypatch):
    signer = _create_signer(monkeypatch, [], "cosign", COSIGN_KEYLESS)
    with pytest.raises(signing.SigningError, match="immutable"):
        signer.verify("registry.example/team/app:v1")


def _notation_policy(level="strict", scopes=None, **verification):
    return {
        "version": "1.0",
        "trustPolicies": [
            {
                "name": "release",
                "registryScopes": scopes or ["registry.example/team/app"],
                "signatureVerification": {"level": level, **verification},
                "trustStores": ["ca:release"],
                "trustedIdentities": ["*"],
            }
        ],
    }


def test_notation_verifies_exact_digest_under_strict_policy(monkeypatch):
    commands = []
    signer = _create_signer(
        monkeypatch, commands, "notation", {"sign_key": "release"},
        policy=_notation_policy(),
        verify_output=f"Successfully verified signature for {SUBJECT}\n",
    )

    signer.verify(SUBJECT)

    invoked = [args[1:] for args, _ in commands if args[1:] != ["version"]]
    assert invoked == [["policy", "show"], ["verify", "--", SUBJECT], ["policy", "show"]]


@pytest.mark.parametrize(
    "policy",
    [
        _notation_policy("skip"),
        _notation_policy("audit"),
        _notation_policy("permissive"),
        _notation_policy(override={"revocation": "skip"}),
        _notation_policy(override=None),
        _notation_policy(override={}),
        _notation_policy(scopes=["registry.example/team/other"]),
        _notation_policy(scopes="registry.example/team/app"),
        _notation_policy(scopes=[1, "registry.example/team/other"]),
        _notation_policy(scopes={"registry.example/team/app": True}),
        {"trustPolicies": []},
        None,
    ],
)
def test_notation_rejects_non_strict_or_missing_policy_before_verify(monkeypatch, policy):
    commands = []
    signer = _create_signer(
        monkeypatch, commands, "notation", {"sign_key": "release"},
        policy=policy, verify_output=f"Successfully verified signature for {SUBJECT}\n",
    )
    with pytest.raises(signing.SigningError, match="trust policy"):
        signer.verify(SUBJECT)
    assert not any(args[1] == "verify" for args, _ in commands)


def test_notation_wildcard_policy_applies_only_without_an_exact_scope(monkeypatch):
    policy = _notation_policy()
    policy["trustPolicies"].append(
        {**_notation_policy("skip", ["*"])["trustPolicies"][0], "name": "default"}
    )
    signer = _create_signer(
        monkeypatch, [], "notation", {"sign_key": "release"},
        policy=policy, verify_output=f"Successfully verified signature for {SUBJECT}\n",
    )
    signer.verify(SUBJECT)

    wildcard_only = _notation_policy("skip", ["*"])
    signer = _create_signer(
        monkeypatch, [], "notation", {"sign_key": "release"},
        policy=wildcard_only, verify_output=f"Successfully verified signature for {SUBJECT}\n",
    )
    with pytest.raises(signing.SigningError, match="trust policy"):
        signer.verify(SUBJECT)


@pytest.mark.parametrize(
    "output",
    [
        "",
        f"Trust policy is configured to skip signature verification for {SUBJECT}\n",
        "Successfully verified signature for registry.example/team/app@sha256:" + "b" * 64 + "\n",
    ],
)
def test_notation_requires_success_report_for_the_exact_subject(monkeypatch, output):
    signer = _create_signer(
        monkeypatch, [], "notation", {"sign_key": "release"},
        policy=_notation_policy(), verify_output=output,
    )
    with pytest.raises(signing.SigningError, match="did not report"):
        signer.verify(SUBJECT)


def test_notation_rejects_policy_changes_during_verification(monkeypatch):
    signer = _create_signer(
        monkeypatch, [], "notation", {"sign_key": "release"},
        policies=[_notation_policy(), _notation_policy(trustedIdentities=None)],
        verify_output=f"Successfully verified signature for {SUBJECT}\n",
    )
    with pytest.raises(signing.SigningError, match="changed"):
        signer.verify(SUBJECT)


def test_real_provider_fixtures_have_distinct_subject_digests(tmp_path, monkeypatch):
    from test_signing_integration import _write_unique_fixture

    identities = iter(("first", "second"))
    monkeypatch.setattr(
        "test_signing_integration.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(identities)),
    )
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], None),
    )
    digests = []
    for index in range(2):
        context = tmp_path / f"context-{index}"
        context.mkdir()
        _write_unique_fixture(context)
        builder = ImageBuilder(BuildConfig(
            tag="registry.example/team/app:v1", context_dir=str(context),
            output_dir=str(tmp_path / f"out-{index}"),
            include_paths=["fixture.txt"], use_cache=False,
        ))
        builder.build()
        digests.append(builder.manifest_digest)
    assert digests[0] != digests[1]
