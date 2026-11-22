"""Opt-in test that attests provenance in a real registry and verifies it independently."""
import base64
import json
import os
import subprocess
import uuid

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.provenance import SLSA_PROVENANCE_V1, STATEMENT_V1, parse_verified_attestations
from pycontainer.registry_client import parse_image_reference
from pycontainer.signing import _find_executable


pytestmark = pytest.mark.registry_integration


def _required(name):
    value = os.getenv(name)
    if not value:
        pytest.skip(f"Set {name} to run this real-registry provenance test.")
    return value


def _verify(executable, key, subject):
    try:
        return subprocess.run(
            [executable, "verify-attestation", "--new-bundle-format", "--type", "slsaprovenance1",
             "--key", key, "--", subject],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=300, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("The independent attestation verifier failed or timed out.")


def test_real_cosign_attests_and_independently_verifies_provenance(tmp_path):
    repository = _required("PYCONTAINER_SIGNING_TEST_REPOSITORY")
    key = _required("PYCONTAINER_COSIGN_TEST_KEY")
    public_key = _required("PYCONTAINER_COSIGN_TEST_PUBLIC_KEY")
    wrong_key = _required("PYCONTAINER_COSIGN_TEST_WRONG_PUBLIC_KEY")
    registry, repo, _ = parse_image_reference(repository)
    executable = _find_executable("cosign")
    if not executable:
        pytest.fail("cosign must be installed to run this test.")

    context = tmp_path / "context"
    context.mkdir()
    unique = uuid.uuid4().hex
    (context / "fixture.txt").write_text(f"synthetic provenance fixture {unique}\n")
    config = BuildConfig(
        tag=f"{registry}/{repo}:provenance-{unique}",
        context_dir=str(context),
        output_dir=str(tmp_path / "out"),
        include_paths=["fixture.txt"],
        use_cache=False,
        sign=True,
        sign_provider="cosign",
        sign_key=key,
        sign_verify_key=public_key,
        attest_provenance=True,
    )
    builder = ImageBuilder(config)
    builder.build()
    builder.push(show_progress=False)
    subject = f"{registry}/{repo}@{builder.manifest_digest}"

    result = _verify(executable, public_key, subject)
    assert result.returncode == 0, "Independent cosign attestation verification failed."
    statements = parse_verified_attestations(result.stdout, builder.manifest_digest)
    assert builder.provenance_statement in statements
    assert all(s["_type"] == STATEMENT_V1 and s["predicateType"] == SLSA_PROVENANCE_V1 for s in statements)
    envelopes = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    payloads = [
        base64.b64decode(envelope.get("dsseEnvelope", envelope)["payload"], validate=True)
        for envelope in envelopes
    ]
    assert (tmp_path / "out" / "provenance.intoto.json").read_bytes() in payloads
    assert _verify(executable, wrong_key, subject).returncode != 0
