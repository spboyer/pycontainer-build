import importlib.util
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
COMMIT = "a" * 40


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verifier = load_script("verify_runtime_evidence")
runtime_check = load_script("check_runtime_image")


def passing_record(target):
    return {
        "schema": 1,
        "status": "passed",
        "mode": "oci-import-startup",
        "target_platform": target,
        "base_image": "python:3.11-slim",
        "commit": COMMIT,
        "daemon_platform": target,
        "image_platform": target,
        "image": {"manifest_digest": "sha256:" + "b" * 64},
        "container_stdout": "pycontainer-runtime-dependency-ok\n",
        "docker_version": "28.0.0",
        "skopeo_version": "skopeo version 1.13.3",
        "run_url": None,
    }


def write_records(directory, records):
    for index, record in enumerate(records):
        subdir = directory / f"artifact-{index}"
        subdir.mkdir(parents=True)
        (subdir / "evidence.json").write_text(json.dumps(record))


def run_verifier(tmp_path, records):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    write_records(evidence, records)
    summary = tmp_path / "summary.md"
    code = verifier.main([
        "--evidence-dir", str(evidence),
        "--require", "linux/amd64",
        "--require", "linux/arm64",
        "--commit", COMMIT,
        "--summary", str(summary),
    ])
    return code, summary.read_text()


def test_gate_passes_only_with_full_evidence_for_every_target(tmp_path):
    code, summary = run_verifier(
        tmp_path, [passing_record("linux/amd64"), passing_record("linux/arm64")]
    )

    assert code == 0
    assert "| `linux/amd64` |" in summary
    assert "| `linux/arm64` |" in summary
    assert "Not validated" not in summary


def _failed(record):
    record.update(status="failed", error="RuntimeError: startup failed")


def _partial(record):
    record["mode"] = "dependency-layer-only"


def _emulated(record):
    record["daemon_platform"] = "linux/amd64"


def _stale(record):
    record["commit"] = "c" * 40


def _no_digest(record):
    del record["image"]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (_failed, "check failed: RuntimeError: startup failed"),
        (_partial, "is not a full OCI import and startup"),
        (_emulated, "daemon_platform 'linux/amd64' does not match linux/arm64"),
        (_stale, "expected " + COMMIT),
        (_no_digest, "lacks the image manifest digest"),
    ],
)
def test_gate_rejects_unproven_target_and_separates_it_from_passing(tmp_path, mutate, reason):
    arm = passing_record("linux/arm64")
    mutate(arm)

    code, summary = run_verifier(tmp_path, [passing_record("linux/amd64"), arm])

    assert code == 1
    passing, not_validated = summary.split("### Not validated")
    assert "| `linux/amd64` |" in passing
    assert "linux/arm64" not in passing
    assert f"- `linux/arm64`: " in not_validated
    assert reason in not_validated


@pytest.mark.parametrize(
    "records",
    [
        [passing_record("linux/amd64")],
        [passing_record("linux/amd64"), passing_record("linux/arm64"), passing_record("linux/arm64")],
    ],
    ids=["missing-target", "duplicate-target"],
)
def test_gate_rejects_missing_or_conflicting_evidence(tmp_path, records):
    code, summary = run_verifier(tmp_path, records)

    assert code == 1
    assert "- `linux/arm64`: " in summary.split("### Not validated")[1]


def test_gate_fails_without_any_evidence_directory(tmp_path):
    code = verifier.main([
        "--evidence-dir", str(tmp_path / "absent"),
        "--require", "linux/amd64",
    ])

    assert code == 1


def test_gate_fails_on_unverified_evidence_transfer(tmp_path):
    evidence = tmp_path / "evidence"
    write_records(evidence, [passing_record("linux/amd64")])
    summary = tmp_path / "summary.md"

    code = verifier.main([
        "--evidence-dir", str(evidence),
        "--require", "linux/amd64",
        "--error", "evidence download did not succeed",
        "--summary", str(summary),
    ])

    assert code == 1
    assert "No target passed runtime verification." in summary.read_text()
    assert "evidence download did not succeed" in summary.read_text()


def test_gate_fails_on_unreadable_evidence(tmp_path):
    records = [passing_record("linux/amd64"), passing_record("linux/arm64")]
    evidence = tmp_path / "evidence"
    write_records(evidence, records)
    (evidence / "broken.json").write_text("{")

    code = verifier.main([
        "--evidence-dir", str(evidence),
        "--require", "linux/amd64",
        "--require", "linux/arm64",
    ])

    assert code == 1


def test_runtime_check_records_failure_when_docker_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_check.shutil, "which", lambda name: None)
    evidence = tmp_path / "evidence.json"

    with pytest.raises(SystemExit, match="docker"):
        runtime_check.main(["--platform", "linux/arm64", "--evidence", str(evidence)])

    record = json.loads(evidence.read_text())
    assert record["status"] == "failed"
    assert record["target_platform"] == "linux/arm64"
    assert "required executable not found: docker" in record["error"]


def test_runtime_check_refuses_emulated_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_check.shutil, "which", lambda name: f"/usr/bin/{name}")

    class Result:
        stdout = "linux/amd64\n"

    monkeypatch.setattr(runtime_check.subprocess, "run", lambda *args, **kwargs: Result())

    def unexpected_build(*args, **kwargs):
        raise AssertionError("image must not be built for a mismatched daemon")

    monkeypatch.setattr(runtime_check, "ImageBuilder", unexpected_build)
    evidence = tmp_path / "evidence.json"

    with pytest.raises(RuntimeError, match="emulated runs are not runtime evidence"):
        runtime_check.main(["--platform", "linux/arm64", "--evidence", str(evidence)])

    record = json.loads(evidence.read_text())
    assert record["status"] == "failed"
    assert record["daemon_platform"] == "linux/amd64"


def job_block(workflow, job):
    text = (WORKFLOWS / workflow).read_text()
    match = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match, f"{workflow} has no job {job}"
    return match.group(1)


def test_release_is_blocked_by_the_runtime_gate():
    runtime = job_block("release.yml", "runtime")
    release = job_block("release.yml", "release")

    assert "uses: ./.github/workflows/runtime-check.yml" in runtime
    assert "continue-on-error" not in runtime
    assert "needs: [build, runtime]" in release
    assert not re.search(r"^    if:", release, re.M)
    assert "name: runtime-evidence-summary" in release
    assert "cat runtime-evidence/runtime-evidence-summary.md >> changelog.txt" in release


def test_runtime_workflow_requires_native_evidence_for_each_target():
    startup = job_block("runtime-check.yml", "startup")
    gate = job_block("runtime-check.yml", "gate")

    assert "fail-fast: false" in startup
    assert "platform: linux/amd64\n            runner: ubuntu-24.04\n" in startup
    assert "platform: linux/arm64\n            runner: ubuntu-24.04-arm\n" in startup
    assert "--platform ${{ matrix.platform }}" in startup
    assert "name: runtime-evidence-${{ matrix.slug }}" in startup
    assert '"status": "failed"' in startup
    assert "if-no-files-found: error" in startup
    assert "continue-on-error" not in startup
    assert "if: always()" in gate
    assert "steps.download.outcome != 'success'" in gate
    assert "needs.startup.result != 'success'" in gate
    assert "--require linux/amd64" in gate
    assert "--require linux/arm64" in gate
    assert "--commit ${{ github.sha }}" in gate


def test_pull_request_ci_runs_the_real_runtime_check():
    job = job_block("tests.yml", "runtime-check")

    assert "uses: ./.github/workflows/runtime-check.yml" in job
