import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_runtime_image.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("check_runtime_image", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch(script, monkeypatch, stdout):
    built = []

    class FakeBuilder:
        def __init__(self, config):
            built.append(config)
            self.manifest_digest = "sha256:" + "a" * 64
            self.config_digest = "sha256:" + "b" * 64
            self.layers = [SimpleNamespace(digest="sha256:" + "c" * 64)]

        def build(self):
            pass

    monkeypatch.setattr(script, "ImageBuilder", FakeBuilder)
    monkeypatch.setattr(script, "run_layout", lambda *args: stdout)
    return built


def test_shell_command_check_builds_verbatim_shell_entrypoint(script, monkeypatch):
    built = _patch(script, monkeypatch, script.SHELL_COMMAND_EXPECTED)
    args = SimpleNamespace(
        base_image="python:3.11-slim",
        shell='["/bin/bash", "-c"]',
        timeout=5,
        platform="linux/arm64",
    )
    evidence = {}
    script.shell_command_check(args, "docker", "skopeo", "img:tag", evidence)
    (config,) = built
    assert config.entrypoint == [
        "/bin/bash",
        "-c",
        "printf '%s\\n' \"pycontainer-shell-command-$MARKER\" 'quoted  $NOT_EXPANDED'",
    ]
    assert config.env == {"MARKER": "ok"}
    assert config.base_image == "python:3.11-slim"
    assert config.platform == "linux/arm64"
    assert evidence["image"]["manifest_digest"] == "sha256:" + "a" * 64


def test_shell_command_check_rejects_output_mismatch(script, monkeypatch):
    _patch(script, monkeypatch, "pycontainer-shell-command-\n")
    args = SimpleNamespace(
        base_image="python:3.11-slim", shell=None, timeout=5, platform="linux/amd64"
    )
    with pytest.raises(RuntimeError, match="output mismatch"):
        script.shell_command_check(args, "docker", "skopeo", "img:tag", {})


def test_shell_command_check_rejects_empty_shell(script, monkeypatch):
    _patch(script, monkeypatch, script.SHELL_COMMAND_EXPECTED)
    args = SimpleNamespace(
        base_image="python:3.11-slim", shell="", timeout=5, platform="linux/amd64"
    )
    with pytest.raises(ValueError, match="shell"):
        script.shell_command_check(args, "docker", "skopeo", "img:tag", {})


def test_cmd_only_check_builds_two_imports_without_entrypoint_override(script, monkeypatch):
    built = _patch(script, monkeypatch, "")
    outputs = iter([
        "inherited-entrypoint\nshell=expanded\n",
        "inherited-entrypoint\nexec=$MARKER  literal\n",
    ])
    monkeypatch.setattr(script, "run_layout", lambda *args: next(outputs))
    args = SimpleNamespace(base_image="python:3.11-slim", timeout=5, platform="linux/amd64")
    evidence = {}
    script.cmd_only_check(args, "docker", "skopeo", "img:tag", evidence)

    base, shell, exec_form = built
    assert base.entrypoint[:2] == ["/bin/sh", "-c"]
    assert shell.base_image == exec_form.base_image == f"oci:{base.output_dir}:runtime-check"
    assert shell.entrypoint is exec_form.entrypoint is None
    assert shell.cmd == ["/bin/sh", "-c", 'printf \'shell=%s\\n\' "$MARKER"']
    assert exec_form.cmd == ["printf", "exec=%s\\n", "$MARKER  literal"]
    assert set(evidence["cases"]) == {"shell", "exec"}


@pytest.mark.parametrize("bad_case", ["shell", "exec"])
def test_cmd_only_check_requires_inherited_entrypoint_output(script, monkeypatch, bad_case):
    _patch(script, monkeypatch, "")
    outputs = [
        "inherited-entrypoint\nshell=expanded\n",
        "inherited-entrypoint\nexec=$MARKER  literal\n",
    ]
    index = ["shell", "exec"].index(bad_case)
    outputs[index] = outputs[index].split("\n", 1)[1]
    remaining = iter(outputs)
    monkeypatch.setattr(script, "run_layout", lambda *args: next(remaining))
    args = SimpleNamespace(base_image="python:3.11-slim", timeout=5, platform="linux/amd64")
    with pytest.raises(RuntimeError, match=f"CMD-only {bad_case}-form output mismatch"):
        script.cmd_only_check(args, "docker", "skopeo", "img:tag", {})


def test_run_layout_records_platform_and_output(script, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="container output\n", stderr="", returncode=0)

    monkeypatch.setattr(script.subprocess, "run", run)
    monkeypatch.setattr(script, "command_output", lambda command: "linux/amd64")
    evidence = {}
    stdout = script.run_layout(
        "docker", "skopeo", Path("layout"), "img:tag", 5, "linux/amd64", evidence
    )
    assert stdout == "container output\n"
    assert evidence == {
        "image_platform": "linux/amd64",
        "container_stdout": "container output\n",
    }
    assert calls[-1] == ["docker", "image", "rm", "--force", "img:tag"]


def test_run_layout_refuses_wrong_image_platform_before_start(script, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(script.subprocess, "run", run)
    monkeypatch.setattr(script, "command_output", lambda command: "linux/arm64")
    evidence = {}
    with pytest.raises(RuntimeError, match="imported image platform"):
        script.run_layout(
            "docker", "skopeo", Path("layout"), "img:tag", 5, "linux/amd64", evidence
        )
    assert evidence["image_platform"] == "linux/arm64"
    assert not any(command[:2] == ["docker", "run"] for command in calls)
    assert calls[-1] == ["docker", "image", "rm", "--force", "img:tag"]


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
@pytest.mark.parametrize("tamper", ["module", "metadata-hash", "version"])
def test_locked_fixture_assertions_run_and_reject_modified_installed_files(
    script, tmp_path, platform, tamper
):
    context = tmp_path / "context"
    context.mkdir()
    digest, packages = script.prepare_lock_fixture(context, platform)
    assert digest == "sha256:" + hashlib.sha256((context / "uv.lock").read_bytes()).hexdigest()
    site = tmp_path / "site"
    site.mkdir()
    lock = script.tomllib.loads((context / "uv.lock").read_text())
    for package in packages:
        entry = next(item for item in lock["package"] if item["name"] == package["name"])
        wheel = next(item for item in entry["wheels"] if item["hash"] == package["wheel_sha256"])
        with zipfile.ZipFile(context / "wheelhouse" / Path(wheel["url"]).name) as archive:
            archive.extractall(site)

    command = [
        sys.executable, "-I", "-c",
        "import runpy, sys; sys.path.insert(0, sys.argv[1]); runpy.run_path(sys.argv[2])",
        str(site), str(context / "app.py"),
    ]
    passed = subprocess.run(command, capture_output=True, text=True)
    assert passed.returncode == 0, passed.stderr
    assert passed.stdout == script.LOCK_MARKER + "\n"

    if tamper == "module":
        path = site / packages[0]["module_path"]
        path.write_text('IDENTITY = "tampered"\n')
    else:
        path = site / packages[0]["metadata_path"]
        metadata = path.read_text()
        path.write_text(
            metadata.replace("Version: 1.0", "Version: 9.0")
            if tamper == "version" else metadata + "tampered\n"
        )
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode != 0
    assert ("installed distribution does not match" if tamper == "version"
            else "installed file hash mismatch") in failed.stderr
    assert script.LOCK_MARKER not in failed.stdout


@pytest.mark.parametrize("tamper", ["wheel", "hash", "version"])
def test_locked_fixture_rejects_tampering_before_build(script, tmp_path, monkeypatch, tamper):
    fixture = tmp_path / "fixture"
    shutil.copytree(script.LOCK_FIXTURE, fixture)
    monkeypatch.setattr(script, "LOCK_FIXTURE", fixture)
    if tamper == "wheel":
        path = fixture / "wheelhouse" / "runtime_pure-1.0-py3-none-any.whl"
        path.write_bytes(path.read_bytes() + b"tampered")
    else:
        lock = fixture / "uv.lock"
        text = lock.read_text()
        if tamper == "hash":
            wheel = fixture / "wheelhouse" / "runtime_pure-1.0-py3-none-any.whl"
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            text = text.replace(digest, "0" * 64)
        else:
            text = text.replace('name = "runtime-pure"\nversion = "1.0"', 'name = "runtime-pure"\nversion = "9.0"')
        lock.write_text(text)
    context = tmp_path / "context"
    context.mkdir()
    with pytest.raises(ValueError):
        script.prepare_lock_fixture(context, "linux/amd64")


def test_fixture_wheels_are_deterministic(script, tmp_path, monkeypatch):
    path = script.LOCK_FIXTURE / "generate_wheels.py"
    spec = importlib.util.spec_from_file_location("runtime_wheels", path)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    for name, version, tag in generator.WHEELS:
        copies = []
        for system in ("win32", "linux"):
            with monkeypatch.context() as patch:
                patch.setattr(sys, "platform", system)
                wheel = generator.create_wheel(tmp_path / system, name, version, tag)
            copies.append(wheel.read_bytes())
        assert copies[0] == copies[1] == (script.LOCK_FIXTURE / "wheelhouse" / wheel.name).read_bytes()


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
def test_lock_option_builds_offline_and_records_verified_fixture(script, tmp_path, monkeypatch, platform):
    built = []

    class FakeBuilder:
        def __init__(self, config):
            self.config = config
            self.manifest_digest = "sha256:" + "a" * 64
            self.config_digest = "sha256:" + "b" * 64
            self.layers = [SimpleNamespace(digest="sha256:" + "c" * 64)]

        def build(self):
            built.append(self.config)
            assert os.environ["PIP_NO_INDEX"] == "1"
            assert Path(os.environ["PIP_FIND_LINKS"]) == Path(self.config.context_dir) / "wheelhouse"
            assert (Path(self.config.context_dir) / "uv.lock").exists()
            assert self.config.entrypoint == ["python", "/app/app.py"]
            assert self.config.include_paths == ["app.py", "expected.json"]
            output = Path(self.config.output_dir)
            output.mkdir()
            (output / "sbom.json").write_text('{"name": "runtime-pure"}')

    monkeypatch.setattr(script, "ImageBuilder", FakeBuilder)
    monkeypatch.setattr(script, "require_executable", lambda name: name)
    monkeypatch.setattr(script.subprocess, "run", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        script, "command_output",
        lambda command: platform if ".Os" in command[-1] else "version",
    )
    def run_layout(*args):
        args[-1]["image_platform"] = platform
        args[-1]["container_stdout"] = script.LOCK_MARKER + "\n"
        return script.LOCK_MARKER + "\n"
    monkeypatch.setattr(script, "run_layout", run_layout)
    monkeypatch.setenv("PIP_FIND_LINKS", "original")
    monkeypatch.delenv("PIP_NO_INDEX", raising=False)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    evidence_path = tmp_path / "record.json"
    script.main(["--platform", platform, "--dependency-fixture", "lockfile", "--evidence", str(evidence_path)])
    evidence = json.loads(evidence_path.read_text())
    assert built
    assert evidence["status"] == "passed"
    assert evidence["lock_archive_verification"] == "passed"
    assert evidence["lock_runtime_verification"] == "passed"
    assert evidence["fixture_path"] == script.LOCK_FIXTURE_PATH
    assert evidence["fixture_app_digest"] == "sha256:" + hashlib.sha256(
        (script.LOCK_FIXTURE / "app.py").read_bytes()
    ).hexdigest()
    assert evidence["lock_digest"] == "sha256:" + hashlib.sha256(
        (script.LOCK_FIXTURE / "uv.lock").read_bytes()
    ).hexdigest()
    assert [item["name"] for item in evidence["locked_packages"]] == ["runtime-pure", "runtime-platform"]
    verifier_path = SCRIPT.parent / "verify_runtime_evidence.py"
    spec = importlib.util.spec_from_file_location("runtime_evidence", verifier_path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    assert verifier.rejection_reason(evidence, platform, "a" * 40, "123", "2", "lockfile") is None
    assert os.environ["PIP_FIND_LINKS"] == "original"
    assert "PIP_NO_INDEX" not in os.environ


@pytest.mark.parametrize("option", ["--shell-command", "--dependency-layer-only", "--cmd-only"])
def test_lock_option_rejects_partial_modes(script, option):
    with pytest.raises(SystemExit):
        script.main(["--dependency-fixture", "lockfile", option])


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
@pytest.mark.parametrize("bad_hash", [False, True], ids=["locked", "tampered-hash"])
def test_real_offline_lock_staging_checks_wheel_hashes(script, tmp_path, monkeypatch, platform, bad_hash):
    from test_dependency_staging import BASE_CONFIG, _builder

    context = tmp_path / "context"
    context.mkdir()
    script.prepare_lock_fixture(context, platform)
    if bad_hash:
        lock = context / "uv.lock"
        wheel = context / "wheelhouse" / "runtime_pure-1.0-py3-none-any.whl"
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        lock.write_text(lock.read_text().replace(digest, "0" * 64))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(context / "wheelhouse"))
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_deps=True,
        platform=platform, generate_sbom=True, include_paths=["app.py", "expected.json"],
    )
    base = {**BASE_CONFIG, "architecture": platform.split("/")[1]}
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: ([], base))
    if bad_hash:
        with pytest.raises(RuntimeError, match="must match their recorded hashes"):
            builder.build()
        return
    builder.build()
    layer = builder.layers[0]
    with tarfile.open(layer.tar_path, "r") as archive:
        paths = archive.getnames()
        archive.extractall(tmp_path / "staged")
    assert "app/.python/site-packages/runtime_pure/__init__.py" in paths
    assert "app/.python/site-packages/runtime_platform/__init__.py" in paths
    app_layer = builder.layers[1]
    with tarfile.open(app_layer.tar_path, "r") as archive:
        paths = archive.getnames()
        archive.extractall(tmp_path / "staged")
    assert "app/app.py" in paths
    assert "app/expected.json" in paths
    result = subprocess.run(
        [
            sys.executable, "-I", "-c",
            "import runpy, sys; sys.path.insert(0, sys.argv[1]); runpy.run_path(sys.argv[2])",
            str(tmp_path / "staged" / "app" / ".python" / "site-packages"),
            str(tmp_path / "staged" / "app" / "app.py"),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == script.LOCK_MARKER + "\n"
    sbom = json.loads((Path(builder.config.output_dir) / "sbom.json").read_text())
    assert {(item["name"], item["versionInfo"]) for item in sbom["packages"]} == {
        ("runtime-pure", "1.0"), ("runtime-platform", "2.0"),
    }
