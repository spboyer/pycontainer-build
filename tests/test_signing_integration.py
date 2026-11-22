"""Opt-in tests that publish to a disposable real registry and verify signatures."""
import json
import os
import subprocess
import uuid

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.registry_client import parse_image_reference
from pycontainer.signing import _find_executable


pytestmark = pytest.mark.registry_integration


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
            {"sign_provider": "cosign", "sign_key": _required("PYCONTAINER_COSIGN_TEST_KEY")},
            ["cosign", "verify", "--key=" + _required("PYCONTAINER_COSIGN_TEST_PUBLIC_KEY")],
        )
    if mode == "cosign-keyless":
        return (
            {"sign_provider": "cosign"},
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


@pytest.mark.parametrize("mode", ["cosign-key", "cosign-keyless", "notation-key", "notation-plugin"])
@pytest.mark.parametrize("platform", ["linux/amd64", "linux/amd64,linux/arm64"])
def test_real_provider_signs_and_independently_verifies(
    tmp_path, monkeypatch, mode, platform
):
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
    settings, verifier = _provider_settings(mode)
    executable = _find_executable(verifier[0])
    if not executable:
        pytest.fail(f"{verifier[0]} must be installed to run this test.")
    if os.name == "nt" and os.path.splitext(executable)[1].lower() not in {".exe", ".com"}:
        pytest.fail(f"{verifier[0]} must be a native Windows executable.")
    verifier[0] = executable
    notation_policy = _strict_notation_policy(executable) if mode.startswith("notation-") else None

    context = tmp_path / "context"
    context.mkdir()
    unique = _write_unique_fixture(context)
    config = BuildConfig(
        tag=f"{registry}/{repo}:signing-{unique}",
        tags=[f"signing-{unique}-second"],
        context_dir=str(context),
        output_dir=str(tmp_path / "out"),
        include_paths=["fixture.txt"],
        use_cache=False,
        platform=platform,
        sign=True,
        **settings,
    )
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], None),
    )
    builder = ImageBuilder(config)
    builder.build()
    builder.push(show_progress=False)
    subject = f"{registry}/{repo}@{builder.index_digest or builder.manifest_digest}"

    try:
        result = subprocess.run(
            [*verifier, "--", subject], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=300, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("The independent signature verifier failed or timed out.")
    assert result.returncode == 0, (
        "Independent signature verification failed; check registry credentials "
        "and the verifier's identity or trust policy."
    )
    if notation_policy is not None:
        assert _strict_notation_policy(executable) == notation_policy, (
            "The Notation trust policy changed during signing and verification."
        )
