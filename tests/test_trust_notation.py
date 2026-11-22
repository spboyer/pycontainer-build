import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pycontainer import trust_notation as notation
from pycontainer import base_trust


REPOSITORY = "example.com/team/image"
IDENTITY = "CN=Exact,O=Example"
FINGERPRINT = "a" * 64
MEDIA = "application/vnd.oci.image.index.v1+json"


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


class Client:
    def __init__(self, subject):
        self.envelope = b"\xd2\x84approved signature envelope"
        self.descriptor = {"digest": digest(self.envelope), "size": len(self.envelope),
                           "mediaType": "application/cose"}
        self.manifest = {"schemaVersion": 2, "mediaType": notation._OCI_MANIFEST,
                         "artifactType": notation._SIGNATURE, "subject": subject,
                         "layers": [self.descriptor]}
        self.raw = encode(self.manifest)
        self.signature_digest = digest(self.raw)
        self.calls = []

    def pull_manifest_bytes(self, reference, expected_digest=None, expected_size=None):
        self.calls.append(("manifest", reference))
        assert reference == expected_digest == self.signature_digest
        return self.manifest, self.signature_digest, self.raw

    def pull_blob(self, blob_digest, destination, expected_size):
        self.calls.append(("blob", blob_digest))
        assert blob_digest == self.descriptor["digest"]
        assert expected_size == self.descriptor["size"]
        destination.write_bytes(self.envelope)
        return True


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "root.crt"
    root.write_bytes(b"test certificate")
    manifest = {"schemaVersion": 2, "mediaType": MEDIA, "manifests": []}
    raw = encode(manifest)
    subject = {"digest": digest(raw), "size": len(raw), "mediaType": MEDIA}
    client = Client(subject)
    policy = {"provider": "notation", "repository": REPOSITORY, "identity": IDENTITY,
              "signer_fingerprint": FINGERPRINT, "trust_root": str(root),
              "binary": sys.executable}
    signature = {"digest": client.signature_digest, "mediaType": "application/cose",
                 "signedArtifact": subject.copy(),
                 "certificates": [{"issuedTo": IDENTITY, "SHA256Fingerprint": FINGERPRINT}]}
    return policy, manifest, raw, client, signature


def invoke(case):
    policy, manifest, raw, client, _ = case
    notation.verify_notation(policy, REPOSITORY, digest(raw), manifest, raw, client)


def runner(case, monkeypatch, verify=None, signatures=None, verify_output=None):
    observed = []
    _, _, raw, client, signature = case

    def run(args, env):
        observed.append(args)
        assert env["NOTATION_EXPERIMENTAL"] == "1"
        assert env["PATH"] == ""
        assert "NOTATION_PASSWORD" not in env
        assert env["REGISTRY_AUTH_FILE"] != os.environ.get("REGISTRY_AUTH_FILE")
        assert "DOCKER_AUTH_CONFIG" not in env
        if args[1] == "inspect":
            assert args[-1] == REPOSITORY + "@" + digest(raw)
            return encode({"Signatures": signatures if signatures is not None else [signature]})
        assert args[1:5] == ["verify", "--oci-layout", "--scope", REPOSITORY]
        layout_name, sep, subject_digest = args[-1].rpartition("@")
        assert sep and subject_digest == digest(raw)
        layout = Path(layout_name)
        config = Path(env["NOTATION_CONFIG"])
        policy = json.loads((config / "trustpolicy.json").read_bytes())
        statement = policy["trustPolicies"][0]
        assert statement["registryScopes"] == [REPOSITORY]
        assert statement["signatureVerification"]["level"] == "strict"
        assert statement["trustedIdentities"] == ["x509.subject: " + IDENTITY]
        index = json.loads((layout / "index.json").read_bytes())
        assert len(index["manifests"]) == 2
        wrapper_digest = index["manifests"][1]["digest"]
        wrapper = json.loads((layout / "blobs" / "sha256" / wrapper_digest[7:]).read_bytes())
        assert wrapper["subject"] == signature["signedArtifact"]
        assert wrapper["config"]["mediaType"] == notation._SIGNATURE
        assert wrapper["layers"] == [client.descriptor]
        assert (layout / "blobs" / "sha256" / client.descriptor["digest"][7:]).read_bytes() == client.envelope
        if verify:
            verify(layout, statement)
        if verify_output is not None:
            return verify_output
        return ("Successfully verified signature for " + args[-1] + "\n").encode()

    monkeypatch.setattr(notation, "_run", run)
    return observed


def test_snapshot_contains_only_digest_bound_signature_and_strict_policy(case, monkeypatch):
    tsa = Path(case[0]["trust_root"]).with_name("tsa.crt")
    tsa.write_bytes(b"test TSA")
    case[0]["tsa_root"] = str(tsa)

    def check(layout, statement):
        assert statement["signatureVerification"] == {"level": "strict", "verifyTimestamp": "always"}
        assert statement["trustStores"] == ["ca:pinned", "tsa:pinned"]
        assert not list(layout.rglob("trustpolicy.json"))

    observed = runner(case, monkeypatch, verify=check)
    invoke(case)
    assert len(observed) == 2
    assert case[3].calls == [("manifest", case[3].signature_digest),
                             ("blob", case[3].descriptor["digest"])]


@pytest.mark.parametrize("output", [
    b"", b"Verification skipped\n", b"Successfully verified signature for sha256:" + b"0" * 64 + b"\n",
    b"Verification skipped\nSuccessfully verified signature for another-reference\n",
])
def test_zero_exit_without_exact_verification_message_fails(case, monkeypatch, output):
    observed = runner(case, monkeypatch, verify_output=output)
    with pytest.raises(notation.NotationTrustError, match="did not confirm"):
        invoke(case)
    assert len(observed) == 2


def test_verification_message_for_same_digest_wrong_layout_fails(case, monkeypatch):
    output = ("Successfully verified signature for wrong-layout@" + digest(case[2]) + "\n").encode()
    runner(case, monkeypatch, verify_output=output)
    with pytest.raises(notation.NotationTrustError, match="did not confirm"):
        invoke(case)


def test_snapshot_uses_system_temp_instead_of_current_directory(case, monkeypatch, tmp_path):
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    cwd = tmp_path / "working"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(notation.tempfile, "tempdir", str(system_temp))

    def check(layout, statement):
        assert layout.parent.parent == system_temp
        assert not list(cwd.iterdir())

    runner(case, monkeypatch, verify=check)
    invoke(case)


def test_other_valid_partial_subject_signature_cannot_rescue_invalid_pinned_signature(case, monkeypatch):
    other = dict(case[4], digest="sha256:" + "b" * 64,
                 certificates=[{"issuedTo": "CN=Exact,O=Example,OU=Extra",
                                "SHA256Fingerprint": "b" * 64}])

    def reject(layout, statement):
        raise notation.NotationTrustError("invalid pinned signature")

    runner(case, monkeypatch, verify=reject, signatures=[case[4], other])
    with pytest.raises(notation.NotationTrustError, match="invalid pinned"):
        invoke(case)
    assert case[3].calls[0] == ("manifest", case[3].signature_digest)


@pytest.mark.parametrize("change", ["fingerprint", "subject", "missing_cert", "missing_signed",
                                    "wrong_size", "wrong_media", "wrong_digest", "missing_signatures"])
def test_missing_or_mismatched_inspection_evidence_fails(case, monkeypatch, change):
    sig = case[4]
    if change == "fingerprint":
        sig["certificates"][0]["SHA256Fingerprint"] = "b" * 64
    elif change == "subject":
        sig["certificates"][0]["issuedTo"] = IDENTITY + ",OU=Extra"
    elif change == "missing_cert":
        del sig["certificates"]
    elif change == "missing_signed":
        del sig["signedArtifact"]
    elif change == "wrong_size":
        sig["signedArtifact"]["size"] += 1
    elif change == "wrong_media":
        sig["signedArtifact"]["mediaType"] = "application/other"
    elif change == "wrong_digest":
        sig["signedArtifact"]["digest"] = "sha256:" + "b" * 64
    runner(case, monkeypatch, signatures=[] if change == "missing_signatures" else [sig])
    with pytest.raises(notation.NotationTrustError):
        invoke(case)
    assert case[3].calls == []


@pytest.mark.parametrize("change", ["manifest_bytes", "subject", "extra_layer", "envelope", "type"])
def test_tampered_signature_manifest_and_envelope_fail(case, monkeypatch, change):
    client = case[3]
    if change == "manifest_bytes":
        client.raw += b" "
    elif change == "envelope":
        client.envelope = b"X" + client.envelope[1:]
    else:
        if change == "subject":
            client.manifest["subject"] = dict(client.manifest["subject"], size=999)
        elif change == "extra_layer":
            client.manifest["layers"] = [client.descriptor, client.descriptor]
        else:
            client.manifest["artifactType"] = "application/other"
        client.raw = encode(client.manifest)
        client.signature_digest = digest(client.raw)
        case[4]["digest"] = client.signature_digest
    runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError):
        invoke(case)


@pytest.mark.parametrize("field,value", [
    ("provider", "cosign"), ("repository", "example.com/other"), ("identity", "*"),
    ("identity", "x509.subject: CN=Exact"), ("signer_fingerprint", "A" * 64),
    ("signer_fingerprint", ""), ("trust_root", ""), ("binary", "missing-notation-executable"),
    ("tsa_root", "missing-root.crt"), ("verification", "audit"),
])
def test_untrusted_or_missing_policy_inputs_fail(case, field, value):
    case[0][field] = value
    with pytest.raises(notation.NotationTrustError):
        invoke(case)
    assert case[3].calls == []


def test_symlink_trust_root_rejected(case):
    root = Path(case[0]["trust_root"])
    link = root.with_name("link.crt")
    try:
        link.symlink_to(root)
    except OSError:
        pytest.skip("symlink creation unavailable")
    case[0]["trust_root"] = str(link)
    with pytest.raises(notation.NotationTrustError):
        invoke(case)


def test_timeout_is_trust_error(monkeypatch):
    monkeypatch.setattr(notation, "_TIMEOUT", 0.1)
    with pytest.raises(notation.NotationTrustError, match="timed out"):
        notation._run([sys.executable, "-c", "import time; time.sleep(10)"], os.environ.copy())


def test_notation_subprocess_is_noninteractive_and_hides_stderr(monkeypatch):
    original = base_trust.subprocess.Popen
    observed = []

    def spawn(*args, **kwargs):
        observed.append(kwargs["stdin"])
        return original(*args, **kwargs)

    monkeypatch.setattr(base_trust.subprocess, "Popen", spawn)
    with pytest.raises(notation.NotationTrustError, match="exit code 3") as error:
        notation._run(
            [sys.executable, "-c", "import sys; print('secret', file=sys.stderr); sys.exit(3)"],
            os.environ.copy(),
        )
    assert observed == [subprocess.DEVNULL]
    assert "secret" not in str(error.value)


def test_bad_subject_bytes_fail_before_inspection(case):
    with pytest.raises(notation.NotationTrustError):
        notation.verify_notation(case[0], REPOSITORY, "sha256:" + "0" * 64,
                                 case[1], case[2], case[3])
    assert case[3].calls == []


def test_multi_signer_cose_is_rejected(case, monkeypatch):
    client = case[3]
    client.envelope = b"\xd8\x62\x84" + client.envelope[2:]
    client.descriptor["digest"] = digest(client.envelope)
    client.descriptor["size"] = len(client.envelope)
    client.raw = encode(client.manifest)
    client.signature_digest = digest(client.raw)
    case[4]["digest"] = client.signature_digest
    observed = runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError, match="single-signer"):
        invoke(case)
    assert len(observed) == 1
