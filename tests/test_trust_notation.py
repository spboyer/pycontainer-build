import base64
import hashlib
import json
import os
from pathlib import Path
import ssl
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


def test_notation_executable_symlink_is_resolved(case, monkeypatch, tmp_path):
    executable = Path(sys.executable).resolve()
    link = tmp_path / ("notation.exe" if os.name == "nt" else "notation")
    try:
        link.symlink_to(executable)
    except OSError:
        pytest.skip("symlink creation unavailable")
    case[0]["binary"] = str(link)
    observed = runner(case, monkeypatch)
    invoke(case)
    assert Path(observed[0][0]) == executable


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


def _jws_case(case, envelope):
    client = case[3]
    client.envelope = envelope
    client.descriptor.update(
        digest=digest(envelope), size=len(envelope), mediaType="application/jose+json"
    )
    client.raw = encode(client.manifest)
    client.signature_digest = digest(client.raw)
    case[4].update(digest=client.signature_digest, mediaType="application/jose+json")


def _jws(cert=b"test certificate"):
    protected = encode({
        "alg": "PS256",
        "cty": "application/vnd.cncf.notary.payload.v1+json",
        "crit": ["io.cncf.notary.signingScheme"],
        "io.cncf.notary.signingScheme": "notary.x509",
        "io.cncf.notary.signingTime": "2026-01-01T00:00:00Z",
    })
    b64url = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    return {
        "payload": b64url(b"payload"),
        "protected": b64url(protected),
        "header": {"x5c": [base64.b64encode(cert).decode()]},
        "signature": b64url(b"signature"),
    }


def test_flattened_jws_is_bound_to_pinned_certificate(case, monkeypatch):
    cert = b"test certificate"
    fingerprint = hashlib.sha256(cert).hexdigest()
    case[0]["signer_fingerprint"] = fingerprint
    case[4]["certificates"][0]["SHA256Fingerprint"] = fingerprint
    _jws_case(case, encode(_jws(cert)))
    observed = runner(case, monkeypatch)
    invoke(case)
    assert len(observed) == 2


@pytest.mark.parametrize("change", [
    "wrong-certificate", "general-jws", "duplicate-root", "duplicate-header",
    "duplicate-protected", "protected-x5c", "noncanonical-x5c", "padding",
    "unencoded-payload", "invalid-algorithm", "overlapping-header", "alternate-key",
    "unknown-critical", "malformed-critical", "missing-content-type", "nan",
    "bom", "utf16", "missing-signing-time", "wrong-scheme-time", "invalid-time",
])
def test_jws_ambiguous_or_unpinned_envelopes_fail(case, monkeypatch, change):
    cert = b"test certificate"
    fingerprint = hashlib.sha256(cert).hexdigest()
    case[0]["signer_fingerprint"] = fingerprint
    case[4]["certificates"][0]["SHA256Fingerprint"] = fingerprint
    obj = _jws(cert)
    if change == "wrong-certificate":
        obj["header"]["x5c"] = [base64.b64encode(b"other cert").decode()]
    elif change == "general-jws":
        obj["signatures"] = []
    elif change == "protected-x5c":
        obj["protected"] = base64.urlsafe_b64encode(encode({
            "alg": "PS256", "x5c": [base64.b64encode(b"other cert").decode()]
        })).rstrip(b"=").decode()
    elif change == "noncanonical-x5c":
        obj["header"]["x5c"][0] += "\n"
    elif change == "padding":
        obj["signature"] += "="
    elif change == "unencoded-payload":
        obj["protected"] = base64.urlsafe_b64encode(
            encode({"alg": "PS256", "b64": False, "crit": ["b64"]})
        ).rstrip(b"=").decode()
    elif change == "duplicate-protected":
        obj["protected"] = base64.urlsafe_b64encode(
            b'{"alg":"PS256","alg":"PS256"}'
        ).rstrip(b"=").decode()
    elif change == "invalid-algorithm":
        obj["protected"] = base64.urlsafe_b64encode(
            encode({"alg": []})
        ).rstrip(b"=").decode()
    elif change == "overlapping-header":
        obj["header"]["alg"] = "PS256"
    elif change == "alternate-key":
        obj["header"]["jwk"] = {}
    elif change in ("unknown-critical", "malformed-critical", "missing-content-type",
                    "missing-signing-time", "wrong-scheme-time", "invalid-time"):
        protected = json.loads(base64.urlsafe_b64decode(obj["protected"] + "==="))
        if change == "unknown-critical":
            protected["crit"].append("unknown")
        elif change == "malformed-critical":
            protected["crit"] = [None]
        elif change == "missing-content-type":
            del protected["cty"]
        elif change == "missing-signing-time":
            del protected["io.cncf.notary.signingTime"]
        elif change == "wrong-scheme-time":
            protected["io.cncf.notary.signingScheme"] = "notary.x509.signingAuthority"
            protected["io.cncf.notary.authenticSigningTime"] = "2026-01-01T00:00:00Z"
            protected["crit"].append("io.cncf.notary.authenticSigningTime")
        else:
            protected["io.cncf.notary.signingTime"] = "2026-02-31T00:00:00Z"
        obj["protected"] = base64.urlsafe_b64encode(encode(protected)).rstrip(b"=").decode()
    raw = encode(obj)
    if change == "duplicate-root":
        raw = raw[:-1] + b',"signature":"other"}'
    elif change == "duplicate-header":
        raw = raw.replace(b'"header":{', b'"header":{"x5c":[],')
    elif change == "nan":
        raw = raw.replace(b'"header":{', b'"header":{"other":NaN,')
    elif change == "bom":
        raw = b"\xef\xbb\xbf" + raw
    elif change == "utf16":
        raw = raw.decode().encode("utf-16")
    _jws_case(case, raw)
    observed = runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError):
        invoke(case)
    assert len(observed) == 1


@pytest.mark.registry_integration
def test_real_notation_verifies_local_jws_snapshot(case, monkeypatch, tmp_path):
    binary = os.environ.get("PYCONTAINER_NOTATION_BIN")
    if not binary:
        pytest.skip("Set PYCONTAINER_NOTATION_BIN to a verified Notation executable")
    config = tmp_path / "signer"
    layout = tmp_path / "signed-layout"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    policy, manifest, raw, client, signature = case
    subject = signature["signedArtifact"]
    (blobs / digest(raw)[7:]).write_bytes(raw)
    (layout / "oci-layout").write_bytes(encode({"imageLayoutVersion": "1.0.0"}))
    (layout / "index.json").write_bytes(encode({"schemaVersion": 2, "manifests": [subject]}))
    env = os.environ.copy()
    env.update({"NOTATION_CONFIG": str(config), "NOTATION_EXPERIMENTAL": "1",
                "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    for args in (
        ["cert", "generate-test", "--default", "pycontainer-jws-test"],
        ["sign", "--oci-layout", "--signature-format", "jws", f"{layout}@{digest(raw)}"],
    ):
        subprocess.run([binary, *args], env=env, check=True, capture_output=True, timeout=30)
    index = json.loads((layout / "index.json").read_bytes())
    signature_descriptor = next(item for item in index["manifests"] if item["digest"] != digest(raw))
    client.raw = (blobs / signature_descriptor["digest"][7:]).read_bytes()
    client.signature_digest = digest(client.raw)
    client.manifest = json.loads(client.raw)
    client.descriptor = client.manifest["layers"][0]
    client.envelope = (blobs / client.descriptor["digest"][7:]).read_bytes()
    parsed = json.loads(client.envelope)
    fingerprint = hashlib.sha256(base64.b64decode(parsed["header"]["x5c"][0])).hexdigest()
    identity = "CN=pycontainer-jws-test,O=Notary,L=Seattle,ST=WA,C=US"
    policy.update(binary=binary, identity=identity, signer_fingerprint=fingerprint,
                  trust_root=str(config / "localkeys" / "pycontainer-jws-test.crt"))
    signature.update(digest=client.signature_digest, mediaType="application/jose+json",
                     certificates=[{"issuedTo": identity, "SHA256Fingerprint": fingerprint}])

    original_run = notation._run

    def run(args, environment):
        if args[1] == "inspect":
            return encode({"Signatures": [signature]})
        return original_run(args, environment)

    monkeypatch.setattr(notation, "_run", run)
    invoke(case)

    policy["signer_fingerprint"] = "0" * 64
    signature["certificates"][0]["SHA256Fingerprint"] = "0" * 64
    with pytest.raises(notation.NotationTrustError, match="fingerprint"):
        invoke(case)
    policy["signer_fingerprint"] = fingerprint
    signature["certificates"][0]["SHA256Fingerprint"] = fingerprint

    subprocess.run(
        [binary, "cert", "generate-test", "untrusted-jws-test"],
        env=env, check=True, capture_output=True, timeout=30,
    )
    root = policy["trust_root"]
    policy["trust_root"] = str(config / "localkeys" / "untrusted-jws-test.crt")
    with pytest.raises(notation.NotationTrustError):
        invoke(case)
    policy["trust_root"] = root

    signature_bytes = base64.urlsafe_b64decode(parsed["signature"] + "===")
    parsed["signature"] = base64.urlsafe_b64encode(
        bytes([signature_bytes[0] ^ 1]) + signature_bytes[1:]
    ).rstrip(b"=").decode()
    _jws_case(case, encode(parsed))
    with pytest.raises(notation.NotationTrustError):
        invoke(case)


@pytest.mark.registry_integration
def test_real_notation_trusted_timestamp_after_signer_expiry(monkeypatch, tmp_path):
    binary = os.environ.get("PYCONTAINER_NOTATION_BIN")
    if not binary:
        pytest.skip("Set PYCONTAINER_NOTATION_BIN to a verified Notation executable")
    fixture_path = Path(__file__).parent / "fixtures" / "notation-expired-timestamp.json"
    fixture = json.loads(fixture_path.read_bytes())
    raw = base64.b64decode(fixture["subject"], validate=True)
    manifest_raw = base64.b64decode(fixture["signature_manifest"], validate=True)
    signature_manifest = json.loads(manifest_raw)
    subject = signature_manifest["subject"]
    client = Client(subject)
    client.raw = manifest_raw
    client.manifest = signature_manifest
    client.signature_digest = digest(manifest_raw)
    client.descriptor = signature_manifest["layers"][0]
    client.envelope = base64.b64decode(fixture["envelope"], validate=True)
    cert = tmp_path / "signer.crt"
    tsa = tmp_path / "tsa.crt"
    wrong_tsa = tmp_path / "wrong-tsa.crt"
    cert.write_text(fixture["signer_certificate"])
    tsa.write_text(fixture["tsa_root"])
    wrong_tsa.write_text(fixture["wrong_tsa_root"])
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(fixture["signer_certificate"])).hexdigest()
    identity = "CN=signer,O=Notary,ST=WA,C=US"
    policy = {
        "provider": "notation", "repository": REPOSITORY, "identity": identity,
        "signer_fingerprint": fingerprint, "trust_root": str(cert), "tsa_root": str(tsa),
        "binary": binary,
    }
    signature = {
        "digest": client.signature_digest, "mediaType": client.descriptor["mediaType"],
        "signedArtifact": subject,
        "certificates": [{"issuedTo": identity, "SHA256Fingerprint": fingerprint}],
    }
    real_run = notation._run

    def run(args, environment):
        if args[1] == "inspect":
            return encode({"Signatures": [signature]})
        return real_run(args, environment)

    monkeypatch.setattr(notation, "_run", run)
    notation.verify_notation(policy, REPOSITORY, digest(raw), json.loads(raw), raw, client)
    policy["tsa_root"] = str(wrong_tsa)
    with pytest.raises(notation.NotationTrustError):
        notation.verify_notation(policy, REPOSITORY, digest(raw), json.loads(raw), raw, client)
    del policy["tsa_root"]
    with pytest.raises(notation.NotationTrustError):
        notation.verify_notation(policy, REPOSITORY, digest(raw), json.loads(raw), raw, client)
