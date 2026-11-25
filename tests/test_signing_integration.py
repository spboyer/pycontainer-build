"""Opt-in tests that publish to a disposable real registry and verify signatures."""
import json
import os
import subprocess
import uuid

import pytest

from pycontainer import signing as signing_module
from pycontainer.auth import get_registry_auth
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.registry_client import MANIFEST_ACCEPT, RegistryClient, parse_image_reference
from pycontainer.signing import SigningError, _find_executable


pytestmark = pytest.mark.registry_integration

GITHUB_ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_CLAIM_VARIABLES = (
    ("sha", "PYCONTAINER_COSIGN_TEST_WORKFLOW_SHA"),
    ("repository", "PYCONTAINER_COSIGN_TEST_WORKFLOW_REPOSITORY"),
    ("ref", "PYCONTAINER_COSIGN_TEST_WORKFLOW_REF"),
    ("trigger", "PYCONTAINER_COSIGN_TEST_WORKFLOW_TRIGGER"),
)
VERIFIER_TIMEOUT_SECONDS = 300
# Cosign v2 and v3 diagnostics observed for rejected trust inputs; the
# sigstore-go form covers Cosign v3's new-bundle verification path.
IDENTITY_MISMATCH = (
    "none of the expected identities matched what was in the certificate",
    "no matching CertificateIdentity found",
)
WORKFLOW_SHA_MISMATCH = (
    "expected GitHub Workflow SHA not found in certificate",
    "no matching CertificateIdentity found",
)
KEY_MISMATCH = (
    "invalid signature when validating ASN.1 encoded signature",
    "transparency log certificate does not match",
)


def _required(name):
    value = os.getenv(name)
    if not value:
        pytest.skip(f"Set {name} to run this real-registry signing test.")
    return value


def _notation_options():
    options = {}
    for variable, field in (
        ("PYCONTAINER_NOTATION_TEST_TIMESTAMP_URL", "sign_timestamp_url"),
        ("PYCONTAINER_NOTATION_TEST_TIMESTAMP_ROOT_CERT", "sign_timestamp_root_cert"),
        ("PYCONTAINER_NOTATION_TEST_SIGNATURE_FORMAT", "sign_signature_format"),
    ):
        value = os.getenv(variable)
        if value:
            options[field] = value
    return options


def _write_unique_fixture(context):
    unique = uuid.uuid4().hex
    (context / "fixture.txt").write_text(f"synthetic signing fixture {unique}\n")
    return unique


def _provider_settings(mode):
    if mode == "cosign-key":
        return (
            {
                "sign_provider": "cosign",
                "sign_key": _required("PYCONTAINER_COSIGN_TEST_KEY"),
                "sign_verify_key": _required("PYCONTAINER_COSIGN_TEST_PUBLIC_KEY"),
            },
            ["cosign", "verify", "--key=" + _required("PYCONTAINER_COSIGN_TEST_PUBLIC_KEY")],
        )
    if mode == "cosign-keyless":
        return (
            {
                "sign_provider": "cosign",
                "sign_certificate_identity": _required("PYCONTAINER_COSIGN_TEST_IDENTITY"),
                "sign_certificate_oidc_issuer": _required("PYCONTAINER_COSIGN_TEST_ISSUER"),
            },
            [
                "cosign", "verify",
                "--certificate-identity=" + _required("PYCONTAINER_COSIGN_TEST_IDENTITY"),
                "--certificate-oidc-issuer=" + _required("PYCONTAINER_COSIGN_TEST_ISSUER"),
            ],
        )
    if mode == "notation-key":
        return (
            {
                "sign_provider": "notation", "sign_key": _required("PYCONTAINER_NOTATION_TEST_KEY"),
                **_notation_options(),
            },
            ["notation", "verify"],
        )
    return (
        {
            "sign_provider": "notation",
            "sign_plugin": _required("PYCONTAINER_NOTATION_TEST_PLUGIN"),
            "sign_key_id": _required("PYCONTAINER_NOTATION_TEST_KEY_ID"),
            **_notation_options(),
        },
        ["notation", "verify"],
    )


def _notation_policy_is_strict(policy):
    if not isinstance(policy, dict):
        return False
    statements = policy.get("trustPolicies")
    if not isinstance(statements, list) or not statements:
        return False
    for statement in statements:
        if not isinstance(statement, dict):
            return False
        verification = statement.get("signatureVerification")
        if (
            not isinstance(verification, dict)
            or verification.get("level") != "strict"
            or verification.get("override")
        ):
            return False
    return True


def _strict_notation_policy(executable):
    try:
        result = subprocess.run(
            [executable, "policy", "show"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, errors="replace", timeout=10, check=False,
        )
        policy = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        policy = None
    if not _notation_policy_is_strict(policy):
        pytest.fail(
            "Notation integration tests require a strict trust policy without overrides "
            "for every configured statement."
        )
    return policy


def _test_repository():
    repository = _required("PYCONTAINER_SIGNING_TEST_REPOSITORY")
    if (
        "@" in repository
        or "://" in repository
        or ":" in repository.rsplit("/", 1)[-1]
        or "/" not in repository
    ):
        pytest.fail("The signing test repository must be a registry/repository without a tag.")
    try:
        registry, repo, _ = parse_image_reference(repository)
    except ValueError:
        pytest.fail("The signing test repository is not a valid image repository.")
    return registry, repo


def _verifier_executable(name):
    executable = _find_executable(name)
    if not executable:
        pytest.fail(f"{name} must be installed to run this test.")
    if os.name == "nt" and os.path.splitext(executable)[1].lower() not in {".exe", ".com"}:
        pytest.fail(f"{name} must be a native Windows executable.")
    return executable


def _run_verifier(args, subject, capture=False, capture_errors=False):
    try:
        return subprocess.run(
            [*args, "--", subject], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_errors else subprocess.DEVNULL,
            text=True, errors="replace",
            timeout=VERIFIER_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("The independent signature verifier failed to run or timed out.")


def _failure_category(stderr):
    """Name a fixed failure category without exposing verifier diagnostics."""
    lowered = stderr.lower()
    for category, markers in (
        ("no signatures were found", ("no signatures found",)),
        ("registry authorization failed", ("unauthorized", "denied")),
        ("a network operation timed out", ("timeout", "deadline exceeded")),
    ):
        if any(marker in lowered for marker in markers):
            return category
    return "unrecognized"


def _require_mismatch(stderr, name, reasons):
    if not any(reason in stderr for reason in reasons):
        pytest.fail(
            f"Verification with a {name} failed for a reason other than a trust "
            f"mismatch ({_failure_category(stderr)}), so the rejection is inconclusive."
        )


def _assert_trust_mismatch(args, subject, name, reasons):
    """Require rejection specifically because the trust input does not match."""
    result = _run_verifier(args, subject, capture_errors=True)
    assert result.returncode != 0, f"Independent verification accepted a {name}."
    _require_mismatch(result.stderr, name, reasons)


class _VerifierErrorRecorder:
    """Delegate to subprocess, keeping product verifier stderr in memory only."""

    def __init__(self):
        self.verify_errors = []

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, args, **kwargs):
        if len(args) < 2 or args[1] != "verify":
            return subprocess.run(args, **kwargs)
        result = subprocess.run(args, **{**kwargs, "stderr": subprocess.PIPE})
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        self.verify_errors.append(stderr or "")
        result.stderr = None
        return result


def _github_claim_args():
    """Return exact GitHub workflow claim constraints for GitHub Actions identities."""
    if os.getenv("PYCONTAINER_COSIGN_TEST_ISSUER") != GITHUB_ACTIONS_ISSUER:
        return []
    args = []
    for claim, variable in GITHUB_CLAIM_VARIABLES:
        value = os.getenv(variable)
        if not value:
            pytest.fail(f"Set {variable} to check the GitHub Actions certificate claims.")
        args.append(f"--certificate-github-workflow-{claim}={value}")
    return args


def _rekor_log_indexes(output):
    found = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "logIndex" and (
                    type(item) is int or (isinstance(item, str) and item.isdigit())
                ):
                    found.add(int(item))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for document in [output, *output.splitlines()]:
        try:
            visit(json.loads(document))
        except ValueError:
            continue
    return sorted(found)


def _record_evidence(mode, platform, subject, log_indexes):
    path = os.getenv("PYCONTAINER_SIGNING_EVIDENCE_FILE")
    if not path:
        return
    kind = "index" if "," in platform else "manifest"
    indexes = ",".join(str(index) for index in log_indexes) or "not-reported"
    with open(path, "a", encoding="utf-8") as evidence:
        evidence.write(
            f"{mode} {kind} {subject.rsplit('@', 1)[1]} rekor-log-index={indexes}\n"
        )


def _wrong_trust(mode, executable):
    """Return build settings, verifier arguments and mismatch reasons for untrusted material."""
    if mode == "cosign-key":
        key = _required("PYCONTAINER_COSIGN_TEST_WRONG_PUBLIC_KEY")
        return {"sign_verify_key": key}, [executable, "verify", f"--key={key}"], KEY_MISMATCH
    identity = _required("PYCONTAINER_COSIGN_TEST_IDENTITY") + ".invalid"
    issuer = _required("PYCONTAINER_COSIGN_TEST_ISSUER")
    return (
        {"sign_certificate_identity": identity},
        [
            executable, "verify", f"--certificate-identity={identity}",
            f"--certificate-oidc-issuer={issuer}",
        ],
        IDENTITY_MISMATCH,
    )


def _check_keyless_trust_boundaries(executable, subject):
    """Require exact keyless trust to pass and each wrong trust input to fail."""
    identity = _required("PYCONTAINER_COSIGN_TEST_IDENTITY")
    issuer = _required("PYCONTAINER_COSIGN_TEST_ISSUER")
    claims = _github_claim_args()
    exact = [
        executable, "verify",
        f"--certificate-identity={identity}", f"--certificate-oidc-issuer={issuer}",
    ]
    accepted = _run_verifier([*exact, *claims], subject, capture=True)
    assert accepted.returncode == 0, (
        "Independent verification rejected the exact identity, issuer and workflow claims."
    )
    wrong_trust = {
        "wrong identity": (_wrong_trust("cosign-keyless", executable)[1], IDENTITY_MISMATCH),
        "wrong issuer": ([
            executable, "verify", f"--certificate-identity={identity}",
            f"--certificate-oidc-issuer={issuer.rstrip('/')}/invalid",
        ], IDENTITY_MISMATCH),
    }
    if claims:
        wrong_trust["wrong workflow commit"] = ([
            *exact, "--certificate-github-workflow-sha=" + "0" * 40,
        ], WORKFLOW_SHA_MISMATCH)
    for name, (args, reasons) in wrong_trust.items():
        _assert_trust_mismatch(args, subject, name, reasons)
    assert _run_verifier(exact, subject).returncode == 0, (
        "Exact trust was rejected after the negative checks, so they are inconclusive."
    )
    return _rekor_log_indexes(accepted.stdout)


def _published_tags(registry, repo, tags):
    auth = get_registry_auth(registry)
    client = RegistryClient(
        registry, repo, auth_token=auth.bearer_token,
        username=auth.username, password=auth.password,
    )
    present = []
    for tag in tags:
        status, _, _ = client._make_request(
            "HEAD", f"{client.base_url}/{client.repository}/manifests/{tag}",
            headers={"Accept": MANIFEST_ACCEPT},
        )
        if status == 200:
            present.append(tag)
        elif status != 404:
            pytest.fail(f"Tag lookup returned HTTP {status}; tag absence is unknown.")
    return present


def _signing_config(tmp_path, registry, repo, platform, settings, suffix=""):
    context = tmp_path / "context"
    context.mkdir()
    unique = _write_unique_fixture(context)
    return BuildConfig(
        tag=f"{registry}/{repo}:signing-{unique}{suffix}",
        tags=[f"signing-{unique}{suffix}-second"],
        context_dir=str(context),
        output_dir=str(tmp_path / "out"),
        include_paths=["fixture.txt"],
        use_cache=False,
        platform=platform,
        sign=True,
        **settings,
    )


def _use_synthetic_base(monkeypatch):
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], None),
    )


@pytest.mark.parametrize("mode", ["cosign-key", "cosign-keyless", "notation-key", "notation-plugin"])
@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_real_provider_signs_and_independently_verifies(
    tmp_path, monkeypatch, mode, platform
):
    registry, repo = _test_repository()
    settings, verifier = _provider_settings(mode)
    executable = _verifier_executable(verifier[0])
    verifier[0] = executable
    notation_policy = _strict_notation_policy(executable) if mode.startswith("notation-") else None

    config = _signing_config(tmp_path, registry, repo, platform, settings)
    _use_synthetic_base(monkeypatch)
    builder = ImageBuilder(config)
    builder.build()
    builder.push(show_progress=False)
    subject = f"{registry}/{repo}@{builder.index_digest or builder.manifest_digest}"

    result = _run_verifier(verifier, subject)
    assert result.returncode == 0, (
        "Independent signature verification failed; check registry credentials "
        "and the verifier's identity or trust policy."
    )
    if mode == "cosign-keyless":
        log_indexes = _check_keyless_trust_boundaries(executable, subject)
        _record_evidence(mode, platform, subject, log_indexes)
    if notation_policy is not None:
        assert _strict_notation_policy(executable) == notation_policy, (
            "The Notation trust policy changed during signing and verification."
        )


@pytest.mark.parametrize("mode", ["cosign-key", "cosign-keyless"])
@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_real_provider_wrong_trust_fails_before_tags(
    tmp_path, monkeypatch, mode, platform
):
    registry, repo = _test_repository()
    settings, verifier = _provider_settings(mode)
    verifier[0] = _verifier_executable(verifier[0])
    wrong, wrong_verifier, reasons = _wrong_trust(mode, verifier[0])
    config = _signing_config(
        tmp_path, registry, repo, platform, {**settings, **wrong}, "-wrong-trust",
    )
    tags = [parse_image_reference(tag)[2] for tag in config.all_tags()]
    assert _published_tags(registry, repo, tags) == [], "A requested tag already exists."

    _use_synthetic_base(monkeypatch)
    recorder = _VerifierErrorRecorder()
    monkeypatch.setattr(signing_module, "subprocess", recorder)
    builder = ImageBuilder(config)
    builder.build()
    with pytest.raises(SigningError, match="^Verifying the signature of "):
        builder.push(show_progress=False)
    monkeypatch.setattr(signing_module, "subprocess", subprocess)
    assert len(recorder.verify_errors) == 1, "The product did not run exactly one verification."
    _require_mismatch(recorder.verify_errors[0], "wrong trust input", reasons)

    assert _published_tags(registry, repo, tags) == [], (
        "A requested tag moved after build-time verification rejected the signature."
    )
    subject = f"{registry}/{repo}@{builder.index_digest or builder.manifest_digest}"
    _assert_trust_mismatch(wrong_verifier, subject, "wrong trust input", reasons)
    assert _run_verifier(verifier, subject).returncode == 0, (
        "The correct trust inputs rejected the signature, so the product failure "
        "is not attributable to the wrong trust input."
    )
