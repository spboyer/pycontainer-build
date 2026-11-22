"""Opt-in generic OCI artifact round trip against a disposable real registry."""
import json
import os
import shutil
import subprocess
import uuid

import pytest

from pycontainer.artifact import (
    SUBJECT_CONFIRMED,
    list_referrers,
    pull_artifact,
    push_artifact,
    write_artifact_layout,
)
from pycontainer.auth import get_registry_auth
from pycontainer.registry_client import RegistryClient, parse_image_reference


pytestmark = pytest.mark.registry_integration

ARTIFACT_TYPE = "application/vnd.pycontainer.test.artifact.v1"
PAYLOAD_TYPE = "application/vnd.pycontainer.test.payload.v1+json"


def _client():
    repository = os.getenv("PYCONTAINER_ARTIFACT_TEST_REPOSITORY")
    if not repository:
        pytest.skip("Set PYCONTAINER_ARTIFACT_TEST_REPOSITORY to run this real-registry artifact test.")
    registry, repo, _ = parse_image_reference(repository + ":unused")
    if os.getenv("PYCONTAINER_ARTIFACT_TEST_PLAIN_HTTP_LOOPBACK") == "1":
        client = RegistryClient(registry, repo, timeout=60)
        client.base_url = f"http://{registry}/v2"
        return f"{registry}/{repo}", client
    auth = get_registry_auth(registry)
    client = RegistryClient(
        registry, repo, auth_token=auth.bearer_token,
        username=auth.username, password=auth.password, timeout=60,
    )
    return f"{registry}/{repo}", client


def test_real_registry_artifact_round_trip_and_referrers(tmp_path):
    repository, client = _client()
    unique = uuid.uuid4().hex
    subject_payload = tmp_path / "subject.json"
    subject_payload.write_text(json.dumps({"subject": unique}))
    subject = write_artifact_layout(
        tmp_path / "subject", subject_payload, "application/vnd.pycontainer.test.subject.v1",
        PAYLOAD_TYPE, tag=f"subject-{unique[:12]}",
    )
    push_artifact(client, tmp_path / "subject", tags=[f"subject-{unique[:12]}"])

    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"referrer": unique}))
    referrer = write_artifact_layout(
        tmp_path / "referrer", payload, ARTIFACT_TYPE, PAYLOAD_TYPE,
        subject=subject.descriptor, tag=f"artifact-{unique[:12]}",
    )
    result = push_artifact(client, tmp_path / "referrer", tags=[f"artifact-{unique[:12]}"])
    print(f"OCI-Subject status: {result.subject_status}")

    pulled = pull_artifact(
        client, f"artifact-{unique[:12]}", tmp_path / "pulled",
        expected_artifact_type=ARTIFACT_TYPE, expected_subject_digest=subject.descriptor.digest,
    )
    assert pulled.descriptor == referrer.descriptor
    assert pulled.payload_path.read_bytes() == payload.read_bytes()

    referrers = list_referrers(client, subject.descriptor.digest, ARTIFACT_TYPE)
    print(f"Referrers API: {referrers.status}; server filter applied: {referrers.registry_filter_applied}")
    if result.subject_status == SUBJECT_CONFIRMED:
        assert referrers.supported
    if referrers.supported:
        assert referrer.descriptor.digest in {item.digest for item in referrers.manifests}

    oras = shutil.which("oras")
    if oras is None:
        pytest.skip("oras is not installed; skipped the independent ORAS interoperability check.")
    plain_http = ["--plain-http"] if os.getenv("PYCONTAINER_ARTIFACT_TEST_PLAIN_HTTP_LOOPBACK") == "1" else []
    fetched = subprocess.run(
        [oras, "manifest", "fetch", *plain_http, f"{repository}@{referrer.descriptor.digest}"],
        check=True, capture_output=True, timeout=120,
    ).stdout
    assert json.loads(fetched)["artifactType"] == ARTIFACT_TYPE
    discovered = subprocess.run(
        [oras, "discover", *plain_http, "--format", "json", "--artifact-type", ARTIFACT_TYPE,
         f"{repository}@{subject.descriptor.digest}"],
        capture_output=True, text=True, timeout=120,
    )
    if referrers.supported:
        assert discovered.returncode == 0, discovered.stderr
        assert referrer.descriptor.digest in discovered.stdout
