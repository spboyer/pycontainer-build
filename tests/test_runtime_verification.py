import importlib.util
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
COMMIT = "a" * 40
RUN_ID = "12345"
RUN_ATTEMPT = "2"


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
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "started_at": "2026-11-22T14:40:00+00:00",
        "finished_at": "2026-11-22T14:41:00+00:00",
        "daemon_platform": target,
        "image_platform": target,
        "image": {
            "manifest_digest": "sha256:" + "b" * 64,
            "config_digest": "sha256:" + "c" * 64,
            "layer_digests": ["sha256:" + "d" * 64],
        },
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
        "--run-id", RUN_ID,
        "--run-attempt", RUN_ATTEMPT,
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


@pytest.mark.parametrize(
    ("shell_command", "mode"),
    [(False, "oci-import-startup"), (True, "shell-command")],
)
@pytest.mark.parametrize("startup_fails", [False, True], ids=["passed", "failed-startup"])
def test_runtime_modes_preserve_release_evidence_boundary(
    tmp_path, monkeypatch, shell_command, mode, startup_fails
):
    configs = []
    calls = []

    class FakeBuilder:
        def __init__(self, config):
            configs.append(config)
            self.config = config
            self.manifest_digest = "sha256:" + "b" * 64
            self.config_digest = "sha256:" + "c" * 64
            self.layers = [SimpleNamespace(digest="sha256:" + "d" * 64)]

        def build(self):
            output = Path(self.config.output_dir)
            output.mkdir()
            (output / "sbom.json").write_text('{"name": "runtime-fixture"}')

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "version"]:
            stdout = "linux/arm64" if ".Os" in command[-1] else "28.0.0"
        elif command[:3] == ["docker", "image", "inspect"]:
            stdout = "linux/arm64"
        elif command[:2] == ["skopeo", "--version"]:
            stdout = "skopeo version 1.13.3"
        elif command[:2] == ["docker", "run"]:
            if startup_fails:
                raise subprocess.CalledProcessError(
                    1, command, output="startup failed", stderr="container error"
                )
            stdout = (
                runtime_check.SHELL_COMMAND_EXPECTED
                if shell_command
                else runtime_check.RUNTIME_MARKER + "\n"
            )
        else:
            stdout = ""
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(runtime_check, "ImageBuilder", FakeBuilder)
    monkeypatch.setattr(runtime_check.shutil, "which", lambda name: name)
    monkeypatch.setattr(runtime_check.subprocess, "run", run)
    monkeypatch.setenv("GITHUB_SHA", COMMIT)
    monkeypatch.setenv("GITHUB_RUN_ID", RUN_ID)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", RUN_ATTEMPT)
    evidence_path = tmp_path / "evidence.json"
    args = ["--platform", "linux/arm64", "--evidence", str(evidence_path)]
    if shell_command:
        args += ["--shell-command", "--shell", '["/bin/bash", "-c"]']
    if startup_fails:
        with pytest.raises(subprocess.CalledProcessError):
            runtime_check.main(args)
    else:
        runtime_check.main(args)
    record = json.loads(evidence_path.read_text())

    assert record["status"] == ("failed" if startup_fails else "passed")
    assert record["mode"] == mode
    assert configs[0].platform == "linux/arm64"
    assert any(command[:3] == ["docker", "image", "rm"] for command in calls)
    reason = verifier.rejection_reason(
        record, "linux/arm64", COMMIT, RUN_ID, RUN_ATTEMPT
    )
    if startup_fails:
        assert "CalledProcessError" in record["error"]
        assert reason.startswith("check failed:")
    elif shell_command:
        assert configs[0].entrypoint[:2] == ["/bin/bash", "-c"]
        assert "not a full OCI import and startup" in reason
    else:
        assert reason is None


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


def _stale_attempt(record):
    record["run_attempt"] = "1"


def _stale_run(record):
    record["run_id"] = "54321"


def _malformed_digest(record):
    record["image"]["manifest_digest"] = "sha256:not-a-digest"


def _missing_config(record):
    del record["image"]["config_digest"]


def _missing_layers(record):
    record["image"]["layer_digests"] = []


def _missing_time(record):
    del record["finished_at"]


def _invalid_image(record):
    record["image"] = "not an image object"


def _reversed_time(record):
    record["finished_at"] = "2026-11-22T14:39:00+00:00"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (_failed, "check failed: RuntimeError: startup failed"),
        (_partial, "is not a full OCI import and startup"),
        (_emulated, "daemon_platform 'linux/amd64' does not match linux/arm64"),
        (_stale, "expected " + COMMIT),
        (_no_digest, "image digest"),
        (_stale_attempt, "run_attempt"),
        (_stale_run, "run_id"),
        (_malformed_digest, "image digest"),
        (_missing_config, "image digest"),
        (_missing_layers, "layer digests"),
        (_missing_time, "timestamps"),
        (_invalid_image, "image digest"),
        (_reversed_time, "timestamps"),
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
    monkeypatch.setenv("GITHUB_RUN_ID", RUN_ID)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", RUN_ATTEMPT)
    evidence = tmp_path / "evidence.json"

    with pytest.raises(SystemExit, match="docker"):
        runtime_check.main(["--platform", "linux/arm64", "--evidence", str(evidence)])

    record = json.loads(evidence.read_text())
    assert record["status"] == "failed"
    assert record["target_platform"] == "linux/arm64"
    assert record["run_id"] == RUN_ID
    assert record["run_attempt"] == RUN_ATTEMPT
    assert "required executable not found: docker" in record["error"]


@pytest.mark.parametrize("extra_args", [[], ["--shell-command"]], ids=["full", "shell"])
def test_runtime_check_refuses_emulated_daemon(tmp_path, monkeypatch, extra_args):
    monkeypatch.setattr(runtime_check.shutil, "which", lambda name: f"/usr/bin/{name}")

    class Result:
        stdout = "linux/amd64\n"

    monkeypatch.setattr(runtime_check.subprocess, "run", lambda *args, **kwargs: Result())

    def unexpected_build(*args, **kwargs):
        raise AssertionError("image must not be built for a mismatched daemon")

    monkeypatch.setattr(runtime_check, "ImageBuilder", unexpected_build)
    evidence = tmp_path / "evidence.json"

    with pytest.raises(RuntimeError, match="emulated runs are not runtime evidence"):
        runtime_check.main(
            ["--platform", "linux/arm64", "--evidence", str(evidence), *extra_args]
        )

    record = json.loads(evidence.read_text())
    assert record["status"] == "failed"
    assert record["daemon_platform"] == "linux/amd64"
    assert record["mode"] == ("shell-command" if extra_args else "oci-import-startup")


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
    assert "pattern: runtime-evidence-linux-*" in release
    assert "python3 scripts/verify_runtime_evidence.py" in release
    assert "--commit ${{ github.sha }}" in release
    assert "--run-id ${{ github.run_id }}" in release
    assert "--run-attempt ${{ github.run_attempt }}" in release
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
    assert "--run-id ${{ github.run_id }}" in gate
    assert "--run-attempt ${{ github.run_attempt }}" in gate


def test_pull_request_ci_runs_the_real_runtime_check():
    job = job_block("tests.yml", "runtime-check")

    assert "uses: ./.github/workflows/runtime-check.yml" in job
