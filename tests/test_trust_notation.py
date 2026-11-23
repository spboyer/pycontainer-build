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
CERT = b"test certificate"
FINGERPRINT = hashlib.sha256(CERT).hexdigest()
MEDIA = "application/vnd.oci.image.index.v1+json"


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _cbor_head(major, value):
    if value < 24:
        return bytes([major << 5 | value])
    for info, width in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if value < 1 << (8 * width):
            return bytes([major << 5 | info]) + value.to_bytes(width, "big")
    raise ValueError(value)


def cbor(value):
    if isinstance(value, int):
        return _cbor_head(0, value) if value >= 0 else _cbor_head(1, -1 - value)
    if isinstance(value, bytes):
        return _cbor_head(2, len(value)) + value
    if isinstance(value, str):
        return _cbor_head(3, len(value.encode())) + value.encode()
    if isinstance(value, list):
        return _cbor_head(4, len(value)) + b"".join(cbor(item) for item in value)
    return _cbor_head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())


def cose(cert=CERT, unprotected=None, protected=None):
    unprotected = {33: [cert]} if unprotected is None else unprotected
    return b"\xd2" + cbor([cbor(protected or {1: -37}), unprotected, b"payload", b"signature"])


class Client:
    """Fake strict registry client exposing only digest-bound discovery methods."""

    def __init__(self, subject):
        self.subject = subject
        self.mode = "referrers"
        self.referrers = None
        self.others = {}
        self.calls = []
        self.set_envelope(cose())

    def set_envelope(self, envelope, media="application/cose"):
        self.envelope = envelope
        self.descriptor = {"digest": digest(envelope), "size": len(envelope), "mediaType": media}
        self.manifest = {"schemaVersion": 2, "mediaType": notation._OCI_MANIFEST,
                         "artifactType": notation._SIGNATURE, "subject": self.subject,
                         "layers": [self.descriptor]}
        self.refresh()

    def refresh(self):
        self.raw = encode(self.manifest)
        self.signature_digest = digest(self.raw)

    def add_signature(self, envelope, media="application/cose"):
        descriptor = {"digest": digest(envelope), "size": len(envelope), "mediaType": media}
        manifest = {"schemaVersion": 2, "mediaType": notation._OCI_MANIFEST,
                    "artifactType": notation._SIGNATURE, "subject": self.subject,
                    "layers": [descriptor]}
        raw = encode(manifest)
        self.others[digest(raw)] = (manifest, raw, descriptor, envelope)
        return digest(raw)

    def entry(self, signature_digest, raw):
        return {"mediaType": notation._OCI_MANIFEST, "artifactType": notation._SIGNATURE,
                "digest": signature_digest, "size": len(raw)}

    def index(self):
        manifests = self.referrers
        if manifests is None:
            manifests = [self.entry(self.signature_digest, self.raw)] + [
                self.entry(key, value[1]) for key, value in self.others.items()]
        return {"schemaVersion": 2, "mediaType": MEDIA, "manifests": manifests}

    def pull_referrers(self, subject_digest, artifact_type, max_bytes):
        self.calls.append(("referrers", subject_digest))
        assert subject_digest == self.subject["digest"]
        assert artifact_type == notation._SIGNATURE
        if self.mode != "referrers":
            return None
        index = self.index()
        return index, encode(index)

    def pull_optional_manifest_bytes(self, reference, max_bytes):
        self.calls.append(("tag", reference))
        assert reference == "sha256-" + self.subject["digest"][7:]
        if self.mode != "tag":
            return None
        index = self.index()
        return index, digest(encode(index)), encode(index)

    def pull_manifest_bytes(self, reference, expected_digest=None, expected_size=None):
        self.calls.append(("manifest", reference))
        assert reference == expected_digest
        if reference in self.others:
            manifest, raw = self.others[reference][:2]
            return manifest, reference, raw
        assert reference == self.signature_digest
        return self.manifest, self.signature_digest, self.raw

    def pull_blob(self, blob_digest, destination, expected_size):
        self.calls.append(("blob", blob_digest))
        blobs = {self.descriptor["digest"]: self.envelope}
        blobs.update({value[2]["digest"]: value[3] for value in self.others.values()})
        assert expected_size == len(blobs[blob_digest])
        destination.write_bytes(blobs[blob_digest])
        return True


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "root.crt"
    root.write_bytes(b"test root")
    manifest = {"schemaVersion": 2, "mediaType": MEDIA, "manifests": []}
    raw = encode(manifest)
    subject = {"digest": digest(raw), "size": len(raw), "mediaType": MEDIA}
    client = Client(subject)
    policy = {"provider": "notation", "repository": REPOSITORY, "identity": IDENTITY,
              "signer_fingerprint": FINGERPRINT, "trust_root": str(root),
              "binary": sys.executable}
    return policy, manifest, raw, client


def invoke(case):
    policy, manifest, raw, client = case
    notation.verify_notation(policy, REPOSITORY, digest(raw), manifest, raw, client)


def runner(case, monkeypatch, verify=None, verify_output=None):
    observed = []
    _, _, raw, client = case

    def run(args, env):
        observed.append(args)
        assert env["NOTATION_EXPERIMENTAL"] == "1"
        assert env["PATH"] == ""
        assert "NOTATION_PASSWORD" not in env
        assert env["REGISTRY_AUTH_FILE"] != os.environ.get("REGISTRY_AUTH_FILE")
        assert "DOCKER_AUTH_CONFIG" not in env
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
        assert wrapper["subject"] == client.subject
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
    assert len(observed) == 1
    client = case[3]
    assert client.calls == [("referrers", client.subject["digest"]),
                            ("manifest", client.signature_digest),
                            ("blob", client.descriptor["digest"])]


def test_referrers_tag_fallback_is_used_only_when_api_is_absent(case, monkeypatch):
    client = case[3]
    client.mode = "tag"
    runner(case, monkeypatch)
    invoke(case)
    assert [call[0] for call in client.calls] == ["referrers", "tag", "manifest", "blob"]


def test_missing_signature_evidence_fails_closed(case, monkeypatch):
    client = case[3]
    client.mode = "none"
    observed = runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError, match="No Notation signature evidence"):
        invoke(case)
    assert observed == []
    assert [call[0] for call in client.calls] == ["referrers", "tag"]


@pytest.mark.parametrize("output", [
    b"", b"Verification skipped\n", b"Successfully verified signature for sha256:" + b"0" * 64 + b"\n",
    b"Verification skipped\nSuccessfully verified signature for another-reference\n",
])
def test_zero_exit_without_exact_verification_message_fails(case, monkeypatch, output):
    observed = runner(case, monkeypatch, verify_output=output)
    with pytest.raises(notation.NotationTrustError, match="did not confirm"):
        invoke(case)
    assert len(observed) == 1


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


def test_other_signer_cannot_rescue_invalid_pinned_signature(case, monkeypatch):
    client = case[3]
    other = client.add_signature(cose(b"other certificate"))

    def reject(layout, statement):
        raise notation.NotationTrustError("invalid pinned signature")

    observed = runner(case, monkeypatch, verify=reject)
    with pytest.raises(notation.NotationTrustError, match="invalid pinned"):
        invoke(case)
    assert len(observed) == 1
    assert ("manifest", other) in client.calls


def test_registry_thumbprint_annotations_are_not_trusted(case, monkeypatch):
    client = case[3]
    client.set_envelope(cose(b"other certificate"))
    client.referrers = [dict(client.entry(client.signature_digest, client.raw), annotations={
        "io.cncf.notary.x509chain.thumbprint#S256": json.dumps([FINGERPRINT])})]
    observed = runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError, match="pinned signer fingerprint"):
        invoke(case)
    assert observed == []


def test_two_pinned_signatures_are_ambiguous(case, monkeypatch):
    case[3].add_signature(cose(unprotected={33: [CERT], "io.cncf.notary.signingAgent": "x"}))
    observed = runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError, match="exactly one"):
        invoke(case)
    assert observed == []


@pytest.mark.parametrize("change", [
    "media", "schema", "not-list", "too-many-referrers", "too-many-candidates",
    "descriptor-media", "descriptor-size", "conflict", "only-other-artifacts",
])
def test_invalid_or_unbounded_referrers_fail_before_fetch(case, monkeypatch, change):
    client = case[3]
    entry = client.entry(client.signature_digest, client.raw)
    index = client.index()
    if change == "media":
        index["mediaType"] = notation._OCI_MANIFEST
    elif change == "schema":
        index["schemaVersion"] = 1
    elif change == "not-list":
        index["manifests"] = {}
    elif change == "too-many-referrers":
        index["manifests"] = [{"artifactType": "other"}] * (notation._MAX_REFERRERS + 1)
    elif change == "too-many-candidates":
        index["manifests"] = [dict(entry, digest="sha256:%064x" % n)
                              for n in range(notation._MAX_CANDIDATES + 1)]
    elif change == "descriptor-media":
        index["manifests"] = [dict(entry, mediaType="application/other")]
    elif change == "descriptor-size":
        index["manifests"] = [dict(entry, size=notation._MAX_MANIFEST + 1)]
    elif change == "conflict":
        index["manifests"] = [entry, dict(entry, size=entry["size"] + 1)]
    else:
        index["manifests"] = [dict(entry, artifactType="application/other")]
    client.index = lambda: index
    observed = runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError):
        invoke(case)
    assert observed == []
    assert [call for call in client.calls if call[0] != "referrers"] == []


def test_duplicate_referrer_descriptors_are_evaluated_once(case, monkeypatch):
    client = case[3]
    entry = client.entry(client.signature_digest, client.raw)
    client.referrers = [entry, dict(entry)]
    runner(case, monkeypatch)
    invoke(case)
    assert client.calls.count(("manifest", client.signature_digest)) == 1


def test_aggregate_envelope_budget_is_enforced_before_download(case, monkeypatch):
    client = case[3]
    monkeypatch.setattr(notation, "_MAX_ENVELOPE_TOTAL", len(client.envelope) - 1)
    runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError, match="exceeds size limit"):
        invoke(case)
    assert not [call for call in client.calls if call[0] == "blob"]


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
        client.refresh()
    observed = runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError):
        invoke(case)
    assert observed == []


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


def test_bad_subject_bytes_fail_before_discovery(case):
    with pytest.raises(notation.NotationTrustError):
        notation.verify_notation(case[0], REPOSITORY, "sha256:" + "0" * 64,
                                 case[1], case[2], case[3])
    assert case[3].calls == []


def test_multi_signer_cose_is_rejected(case, monkeypatch):
    client = case[3]
    client.set_envelope(b"\xd8\x62" + cbor([cbor({1: -37}), {}, b"payload", [[b"", {33: [CERT]}, b"s"]]]))
    observed = runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError, match="single-signer"):
        invoke(case)
    assert observed == []


@pytest.mark.parametrize("change", [
    "indefinite", "trailing", "duplicate-key", "protected-x5chain", "overlap",
    "missing-chain", "empty-chain", "non-bytes-cert", "truncated", "deep", "untagged",
])
def test_ambiguous_cose_envelopes_fail(case, monkeypatch, change):
    envelope = cose()
    if change == "indefinite":
        envelope = b"\xd2\x9f" + envelope[2:] + b"\xff"
    elif change == "trailing":
        envelope += b"\x00"
    elif change == "duplicate-key":
        envelope = b"\xd2" + cbor([cbor({1: -37}), {}, b"payload", b"s"]).replace(
            b"\xa0", b"\xa2" + cbor(33) + cbor([CERT]) + cbor(33) + cbor([CERT]), 1)
    elif change == "protected-x5chain":
        envelope = cose(protected={1: -37, 33: [b"other certificate"]})
    elif change == "overlap":
        envelope = cose(unprotected={1: -37, 33: [CERT]})
    elif change == "missing-chain":
        envelope = cose(unprotected={})
    elif change == "empty-chain":
        envelope = cose(unprotected={33: []})
    elif change == "non-bytes-cert":
        envelope = cose(unprotected={33: ["text"]})
    elif change == "truncated":
        envelope = envelope[:-3]
    elif change == "deep":
        nested = b"\x81" * (notation._MAX_CBOR_DEPTH + 2) + b"\x00"
        envelope = b"\xd2" + cbor([cbor({1: -37}), {33: [CERT]}, b"payload", b"s"])[:-2] + nested
    else:
        envelope = envelope[1:]
    case[3].set_envelope(envelope)
    observed = runner(case, monkeypatch)
    with pytest.raises(notation.NotationTrustError):
        invoke(case)
    assert observed == []


def test_single_certificate_x5chain_is_accepted(case, monkeypatch):
    case[3].set_envelope(cose(unprotected={33: CERT}))
    runner(case, monkeypatch)
    invoke(case)


def _jws_case(case, envelope):
    case[3].set_envelope(envelope, "application/jose+json")


def _jws(cert=CERT):
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
    _jws_case(case, encode(_jws(CERT)))
    observed = runner(case, monkeypatch)
    invoke(case)
    assert len(observed) == 1


@pytest.mark.parametrize("change", [
    "wrong-certificate", "general-jws", "duplicate-root", "duplicate-header",
    "duplicate-protected", "protected-x5c", "noncanonical-x5c", "padding",
    "unencoded-payload", "invalid-algorithm", "overlapping-header", "alternate-key",
    "unknown-critical", "malformed-critical", "missing-content-type", "nan",
    "bom", "utf16", "missing-signing-time", "wrong-scheme-time", "invalid-time",
])
def test_jws_ambiguous_or_unpinned_envelopes_fail(case, monkeypatch, change):
    obj = _jws(CERT)
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
    assert observed == []


@pytest.mark.registry_integration
def test_real_notation_verifies_local_jws_snapshot(case, monkeypatch, tmp_path):
    binary = os.environ.get("PYCONTAINER_NOTATION_BIN")
    if not binary:
        pytest.skip("Set PYCONTAINER_NOTATION_BIN to a verified Notation executable")
    config = tmp_path / "signer"
    layout = tmp_path / "signed-layout"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    policy, manifest, raw, client = case
    subject = client.subject
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
    invoke(case)

    policy["signer_fingerprint"] = "0" * 64
    with pytest.raises(notation.NotationTrustError, match="fingerprint"):
        invoke(case)
    policy["signer_fingerprint"] = fingerprint

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
    notation.verify_notation(policy, REPOSITORY, digest(raw), json.loads(raw), raw, client)
    policy["tsa_root"] = str(wrong_tsa)
    with pytest.raises(notation.NotationTrustError):
        notation.verify_notation(policy, REPOSITORY, digest(raw), json.loads(raw), raw, client)
    del policy["tsa_root"]
    with pytest.raises(notation.NotationTrustError):
        notation.verify_notation(policy, REPOSITORY, digest(raw), json.loads(raw), raw, client)
