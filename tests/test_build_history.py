import json
import logging
import multiprocessing
import os
import stat
import sys
import uuid
from pathlib import Path

import pytest

from pycontainer import cli
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.history import BuildHistory, HistoryStorageError


BASE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {"Env": [], "WorkingDir": "/"},
    "rootfs": {"type": "layers", "diff_ids": []},
}


def _record(number=0, status="success"):
    return {
        "run_id": str(uuid.UUID(int=number, version=4)),
        "started_at": "2026-11-22T12:00:00.000000Z",
        "status": status,
        "total_ns": 10,
        "phases_ns": {"application": 5},
    }


def _history_config(tmp_path, **overrides):
    context = tmp_path / "context"
    context.mkdir(exist_ok=True)
    (context / "app.py").write_text("print('hello')\n")
    values = {
        "context_dir": str(context),
        "output_dir": str(tmp_path / "output"),
        "include_paths": ["app.py"],
        "use_cache": False,
        "record_history": True,
        "history_dir": str(tmp_path / "history"),
        "detect_system_packages": False,
    }
    values.update(overrides)
    return BuildConfig(**values)


def _offline_base(monkeypatch):
    monkeypatch.setattr(
        ImageBuilder,
        "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], BASE_CONFIG),
    )


def _run_cli(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["pycontainer", *args])
    cli.main()


def _write_history(directory, records):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "history.json").write_text(
        json.dumps({"schema_version": 1, "records": records}), encoding="utf-8"
    )


def _concurrent_writer(directory, barrier, first):
    history = BuildHistory(Path(directory))
    barrier.wait(timeout=20)
    for number in range(first, first + 5):
        history.append(_record(number))


def _interrupted_writer(directory, ready):
    import pycontainer.history as history_module

    def pause_before_replace(source, target):
        ready.set()
        if not multiprocessing.Event().wait(timeout=30):
            raise TimeoutError("test did not terminate the interrupted writer")

    history_module.os.replace = pause_before_replace
    BuildHistory(Path(directory)).append(_record(2))


def test_default_and_dry_run_do_not_create_history(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    _offline_base(monkeypatch)
    configured_dir = tmp_path / "configured-history"
    config = _history_config(
        tmp_path, record_history=False, history_dir=str(configured_dir)
    )

    ImageBuilder(config).build()
    dry_run = _history_config(
        tmp_path,
        dry_run=True,
        history_dir=str(configured_dir),
    )
    ImageBuilder(dry_run).build()

    assert not configured_dir.exists()
    assert not (tmp_path / "home" / ".pycontainer" / "history").exists()


def test_successful_repeated_builds_record_timings_without_changing_oci(
    tmp_path, monkeypatch
):
    _offline_base(monkeypatch)
    without_history = _history_config(
        tmp_path, record_history=False, output_dir=str(tmp_path / "without")
    )
    with_history = _history_config(tmp_path, output_dir=str(tmp_path / "with"))

    ImageBuilder(without_history).build()
    builder = ImageBuilder(with_history)
    builder.build()
    builder.build()

    records = BuildHistory(Path(with_history.history_dir)).list()
    assert len(records) == 2
    assert len({record["run_id"] for record in records}) == 2
    for record in records:
        assert uuid.UUID(record["run_id"]).version == 4
        assert record["status"] == "success"
        assert record["total_ns"] >= 0
        assert {"base_image", "application"} <= record["phases_ns"].keys()
        assert all(value >= 0 for value in record["phases_ns"].values())

    original = tmp_path / "without"
    recorded = tmp_path / "with"
    files = [path.relative_to(original) for path in original.rglob("*") if path.is_file()]
    recorded_files = [
        path.relative_to(recorded) for path in recorded.rglob("*") if path.is_file()
    ]
    assert files
    assert set(files) == set(recorded_files)
    assert all((original / path).read_bytes() == (recorded / path).read_bytes() for path in files)
    assert not list(recorded.rglob("history.json"))


def test_enabled_optional_phases_are_reported(tmp_path, monkeypatch):
    import pycontainer.builder as builder_module

    _offline_base(monkeypatch)
    monkeypatch.setattr(ImageBuilder, "_create_deps_layer", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        builder_module,
        "generate_sbom",
        lambda context, path, **kwargs: path.write_text("{}"),
    )
    config = _history_config(tmp_path, include_deps=True, generate_sbom=True)

    ImageBuilder(config).build()

    phases = BuildHistory(Path(config.history_dir)).list()[0]["phases_ns"]
    assert set(phases) == {"base_image", "dependencies", "application", "sbom"}


def test_history_and_security_fragments_are_both_preserved(tmp_path, monkeypatch):
    from pycontainer.security import KUBERNETES_FRAGMENT, SECURITY_DIR

    _offline_base(monkeypatch)
    config = _history_config(
        tmp_path, security={"kubernetes": {"read_only_root_filesystem": True}}
    )

    ImageBuilder(config).build()

    fragment = Path(config.output_dir) / SECURITY_DIR / KUBERNETES_FRAGMENT
    assert json.loads(fragment.read_text()) == {"readOnlyRootFilesystem": True}
    record = BuildHistory(config.history_dir).list()[0]
    assert record["status"] == "success"
    assert "base_image" in record["phases_ns"]


def test_history_records_phases_without_build_argument_values(tmp_path, monkeypatch):
    _offline_base(monkeypatch)
    value = "synthetic-build-argument"
    config = _history_config(
        tmp_path, tag="example:${VERSION}", build_args={"VERSION": value},
        generate_sbom=True,
    )

    ImageBuilder(config).build()

    content = (Path(config.history_dir) / "history.json").read_text()
    assert value not in content
    record = BuildHistory(config.history_dir).list()[0]
    assert record["status"] == "success"
    assert {"base_image", "application", "sbom"} <= record["phases_ns"].keys()


def test_failed_history_log_redacts_build_argument_values(tmp_path, monkeypatch, caplog):
    value = "synthetic-build-argument"
    config = _history_config(tmp_path, build_args={"VALUE": value})
    builder = ImageBuilder(config)

    def fail_build(*args, **kwargs):
        raise RuntimeError("original build failure")

    def fail_history(*args, **kwargs):
        raise HistoryStorageError(f"storage failed at {value}")

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", fail_build)
    monkeypatch.setattr(BuildHistory, "append", fail_history)
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="original build failure"):
        builder.build()

    assert "storage failed at <redacted>" in caplog.text
    assert value not in caplog.text


def test_cli_history_error_redacts_build_argument_values(tmp_path, monkeypatch, capsys):
    value = "synthetic-build-argument"
    config = tmp_path / "pycontainer.toml"
    config.write_text('[build.build-args]\nVALUE = "synthetic-build-argument"\n')

    def fail_builder(*args, **kwargs):
        raise HistoryStorageError(f"storage failed at {value}")

    monkeypatch.setattr(cli, "ImageBuilder", fail_builder)
    with pytest.raises(SystemExit) as error:
        _run_cli(monkeypatch, ["build", "--config", str(config), "--record-history"])

    assert error.value.code == 1
    diagnostic = capsys.readouterr().err
    assert "Build history error: storage failed at <redacted>" in diagnostic
    assert value not in diagnostic


def test_failed_build_records_partial_phase_without_error_text(tmp_path, monkeypatch):
    secret = "synthetic-failure-secret"

    def fail_base(self, layers_dir, os_name, arch):
        raise RuntimeError(secret)

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", fail_base)
    config = _history_config(tmp_path)
    builder = ImageBuilder(config)

    with pytest.raises(RuntimeError, match=secret):
        builder.build()

    history = BuildHistory(Path(config.history_dir))
    record = history.list()[0]
    assert record["status"] == "failed"
    assert "base_image" in record["phases_ns"]
    assert "application" not in record["phases_ns"]
    assert secret not in (Path(config.history_dir) / "history.json").read_text()


def test_rejected_package_index_credentials_are_not_recorded(tmp_path, monkeypatch):
    secret = "synthetic-index-secret"
    _offline_base(monkeypatch)
    config = _history_config(tmp_path, include_paths=["requirements.txt"])
    (Path(config.context_dir) / "requirements.txt").write_text(
        f"--index-url https://synthetic-user:{secret}@example.invalid/simple\n"
    )

    with pytest.raises(ValueError, match="literal credentials") as error:
        ImageBuilder(config).build()

    assert secret not in str(error.value)
    history = BuildHistory(config.history_dir)
    record = history.list()[0]
    assert record["status"] == "failed"
    assert "application" in record["phases_ns"]
    assert secret not in history.history_file.read_text()


def test_constructor_failure_is_recorded(tmp_path, monkeypatch):
    import pycontainer.builder as builder_module

    def fail_discovery(*args, **kwargs):
        raise RuntimeError("synthetic setup failure")

    monkeypatch.setattr(builder_module, "default_include_paths", fail_discovery)
    config = _history_config(tmp_path, include_paths=None)

    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        ImageBuilder(config)

    record = BuildHistory(Path(config.history_dir)).list()[0]
    assert record["status"] == "failed"
    assert "initialization" in record["phases_ns"]


def test_constructor_failure_is_preserved_if_history_also_fails(tmp_path, monkeypatch, caplog):
    import pycontainer.builder as builder_module

    original = RuntimeError("original setup failure")

    def fail_discovery(*args, **kwargs):
        raise original

    def fail_history(*args, **kwargs):
        raise HistoryStorageError("synthetic storage failure")

    monkeypatch.setattr(builder_module, "default_include_paths", fail_discovery)
    monkeypatch.setattr(BuildHistory, "append", fail_history)
    config = _history_config(tmp_path, include_paths=None)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as error:
        ImageBuilder(config)

    assert error.value is original
    assert "synthetic storage failure" in caplog.text


def test_handled_interrupt_records_failure(tmp_path, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("synthetic interrupt")

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", interrupt)
    config = _history_config(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        ImageBuilder(config).build()

    assert BuildHistory(Path(config.history_dir)).list()[0]["status"] == "failed"


def test_recording_failure_does_not_mask_original_build_failure(
    tmp_path, monkeypatch, caplog
):
    original = RuntimeError("original build failure")

    def fail_base(*args, **kwargs):
        raise original

    def fail_history(*args, **kwargs):
        raise HistoryStorageError("synthetic storage failure")

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", fail_base)
    monkeypatch.setattr(BuildHistory, "append", fail_history)
    builder = ImageBuilder(_history_config(tmp_path))

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as error:
        builder.build()

    assert error.value is original
    assert "synthetic storage failure" in caplog.text
    assert "history" in caplog.text.lower()


def test_successful_build_reports_history_storage_failure(tmp_path, monkeypatch):
    _offline_base(monkeypatch)
    path = tmp_path / "not-a-directory"
    path.write_text("file")
    builder = ImageBuilder(_history_config(tmp_path, history_dir=str(path)))

    with pytest.raises(HistoryStorageError):
        builder.build()

    assert (tmp_path / "output" / "index.json").exists()


def test_unwritable_history_path_is_reported(tmp_path, monkeypatch):
    _offline_base(monkeypatch)
    config = _history_config(tmp_path)
    directory = Path(config.history_dir)
    mkdir = Path.mkdir

    def deny_history(self, *args, **kwargs):
        if self == directory:
            raise PermissionError("synthetic access denied")
        return mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_history)
    with pytest.raises(HistoryStorageError, match="synthetic access denied"):
        ImageBuilder(config).build()
    assert not directory.exists()


def test_history_never_contains_sensitive_build_inputs(tmp_path, monkeypatch):
    _offline_base(monkeypatch)
    secret = "synthetic-credential-DO-NOT-STORE"
    project_path = tmp_path / "synthetic-project-path"
    project_path.mkdir()
    (project_path / "app.py").write_text("pass\n")
    config = _history_config(
        tmp_path,
        context_dir=str(project_path),
        tag="synthetic.registry.invalid/private:synthetic-tag",
        env={"SYNTHETIC_TOKEN": secret},
        entrypoint=["python", "-c", f"print('{secret}')"],
        cmd=["--secret", secret],
    )

    ImageBuilder(config).build()

    content = (Path(config.history_dir) / "history.json").read_text()
    for sensitive in (
        secret,
        "SYNTHETIC_TOKEN",
        "synthetic.registry.invalid",
        "synthetic-tag",
        str(project_path),
        "--secret",
        "python",
    ):
        assert sensitive not in content


def test_retention_keeps_last_thousand_committed_records(tmp_path):
    directory = tmp_path / "history"
    records = [_record(number) for number in range(1000)]
    _write_history(directory, records)

    history = BuildHistory(directory)
    history.append(_record(1000))

    stored = history.list()
    assert len(stored) == 1000
    assert stored[0]["run_id"] == records[1]["run_id"]
    assert stored[-1]["run_id"] == _record(1000)["run_id"]
    assert (directory / "history.json").stat().st_size <= 512 * 1024


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not describe Windows ACLs")
def test_new_history_directory_and_ledger_are_private(tmp_path):
    directory = tmp_path / "history"
    BuildHistory(directory).append(_record())

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "history.json").stat().st_mode) == 0o600


def test_concurrent_process_writers_do_not_lose_records(tmp_path):
    directory = tmp_path / "history"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    processes = [
        context.Process(target=_concurrent_writer, args=(str(directory), barrier, i * 5))
        for i in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("history writer process did not finish")
        assert process.exitcode == 0

    records = BuildHistory(directory).list()
    assert len(records) == 20
    assert {record["run_id"] for record in records} == {
        _record(number)["run_id"] for number in range(20)
    }


def test_interrupted_writer_preserves_committed_ledger_and_recovers(tmp_path):
    directory = tmp_path / "history"
    history = BuildHistory(directory)
    history.append(_record(1))
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_interrupted_writer, args=(str(directory), ready))
    process.start()
    try:
        assert ready.wait(timeout=20)
    finally:
        process.terminate()
        process.join(timeout=20)
    assert not process.is_alive()

    assert history.list() == [_record(1)]
    history.append(_record(3))
    assert history.list() == [_record(1), _record(3)]
    assert not (directory / ".history.json.tmp").exists()


@pytest.mark.parametrize(
    "content",
    [
        "{",
        '{"schema_version":1,"records":[],"records":[]}',
        '{"schema_version":1,"records":[{"unexpected":"secret"}]}',
        '{"schema_version":1,"records":[],"unexpected":"secret"}',
        pytest.param("[" * 5000 + "0" + "]" * 5000, id="too-deep"),
        pytest.param(" " * (512 * 1024 + 1), id="oversized"),
    ],
)
def test_corrupt_history_is_reported_without_overwriting_and_can_be_cleared(
    tmp_path, content
):
    directory = tmp_path / "history"
    directory.mkdir()
    path = directory / "history.json"
    path.write_text(content, encoding="utf-8")
    history = BuildHistory(directory)

    with pytest.raises(HistoryStorageError):
        history.list()
    with pytest.raises(HistoryStorageError):
        history.append(_record())
    assert path.read_text(encoding="utf-8") == content

    history.clear()
    assert not path.exists()
    assert history.list() == []


def test_incoherent_phase_durations_are_corrupt(tmp_path):
    directory = tmp_path / "history"
    record = _record()
    record["phases_ns"] = {"base_image": 6, "application": 6}
    _write_history(directory, [record])

    with pytest.raises(HistoryStorageError, match="phase"):
        BuildHistory(directory).list()


@pytest.mark.parametrize("invalid", ["history", "context", "output"])
def test_invalid_paths_report_history_storage_errors(tmp_path, invalid):
    if invalid == "history":
        with pytest.raises(HistoryStorageError):
            BuildHistory("\0")
    else:
        history = BuildHistory(tmp_path / "history")
        context = "\0" if invalid == "context" else str(tmp_path / "context")
        output = "\0" if invalid == "output" else str(tmp_path / "output")
        with pytest.raises(HistoryStorageError):
            history.validate_location(context, output)


def test_atomic_replace_failure_preserves_previous_history(tmp_path, monkeypatch):
    import pycontainer.history as history_module

    directory = tmp_path / "history"
    history = BuildHistory(directory)
    history.append(_record(1))

    def fail_replace(source, target):
        raise OSError("synthetic interrupted replace")

    monkeypatch.setattr(history_module.os, "replace", fail_replace)
    with pytest.raises(HistoryStorageError, match="synthetic interrupted replace"):
        history.append(_record(2))

    assert history.list() == [_record(1)]
    assert not (directory / ".history.json.tmp").exists()


def test_fsync_failure_reports_storage_error_without_replacing_history(tmp_path, monkeypatch):
    import pycontainer.history as history_module

    directory = tmp_path / "history"
    history = BuildHistory(directory)
    history.append(_record(1))

    def fail_fsync(descriptor):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(history_module.os, "fsync", fail_fsync)
    with pytest.raises(HistoryStorageError, match="synthetic fsync failure"):
        history.append(_record(2))
    assert history.list() == [_record(1)]


def test_directory_sync_failure_is_reported_after_replace(tmp_path, monkeypatch):
    import pycontainer.history as history_module

    directory = tmp_path / "history"
    history = BuildHistory(directory)
    history.append(_record(1))

    def fail_sync(path):
        raise OSError("synthetic directory sync failure")

    monkeypatch.setattr(history_module, "_sync_directory", fail_sync)
    with pytest.raises(HistoryStorageError, match="synthetic directory sync failure"):
        history.append(_record(2))
    assert history.list() == [_record(1), _record(2)]


@pytest.mark.parametrize("inside", ["context", "output"])
def test_history_directory_is_rejected_inside_build_artifacts(
    tmp_path, inside
):
    config = _history_config(tmp_path)
    root = Path(config.context_dir if inside == "context" else config.output_dir)
    config.history_dir = str(root / "history")

    with pytest.raises(HistoryStorageError, match="outside"):
        ImageBuilder(config)

    assert not (root / "history").exists()


def test_history_directory_symlink_cannot_bypass_context_boundary(tmp_path):
    config = _history_config(tmp_path)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(Path(config.context_dir), target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    config.history_dir = str(alias / "history")

    with pytest.raises(HistoryStorageError, match="outside"):
        ImageBuilder(config)
    assert not (Path(config.context_dir) / "history").exists()


def test_history_file_symlink_is_not_followed_and_can_be_cleared(tmp_path):
    directory = tmp_path / "history"
    directory.mkdir()
    target = tmp_path / "unrelated.txt"
    target.write_text("do not modify")
    link = directory / "history.json"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable: {error}")
    history = BuildHistory(directory)

    with pytest.raises(HistoryStorageError, match="regular file"):
        history.list()
    history.clear()
    assert target.read_text() == "do not modify"
    assert not link.exists()


def test_history_list_and_clear_absent_storage_do_not_create_it(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "does-not-exist"
    _run_cli(monkeypatch, ["history", "list", "--history-dir", str(directory)])
    assert json.loads(capsys.readouterr().out) == []
    _run_cli(monkeypatch, ["history", "clear", "--history-dir", str(directory)])
    assert "cleared" in capsys.readouterr().out.lower()
    assert not directory.exists()


def test_cli_history_directory_alone_does_not_enable_recording(tmp_path, monkeypatch):
    _offline_base(monkeypatch)
    config = _history_config(tmp_path)
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        "[build]\n"
        f'context_dir = "{Path(config.context_dir).as_posix()}"\n'
        f'output_dir = "{Path(config.output_dir).as_posix()}"\n'
        'include_paths = ["app.py"]\n'
        "use_cache = false\n"
        "detect_system_packages = false\n",
        encoding="utf-8",
    )

    _run_cli(
        monkeypatch,
        ["build", "--config", str(config_path), "--history-dir", config.history_dir],
    )
    assert not Path(config.history_dir).exists()

    _run_cli(
        monkeypatch,
        [
            "build", "--config", str(config_path),
            "--record-history", "--history-dir", config.history_dir,
        ],
    )
    assert len(BuildHistory(Path(config.history_dir)).list()) == 1


def test_history_cli_reports_corruption(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "history"
    directory.mkdir()
    path = directory / "history.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        _run_cli(monkeypatch, ["history", "list", "--history-dir", str(directory)])

    output = capsys.readouterr()
    assert error.value.code == 1
    assert "Build history error" in output.err
    assert "Traceback" not in output.err
    assert path.read_text(encoding="utf-8") == "{"


def test_cli_does_not_claim_build_success_or_push_if_history_fails(
    tmp_path, monkeypatch, capsys
):
    _offline_base(monkeypatch)
    config = _history_config(tmp_path)
    invalid_dir = tmp_path / "file"
    invalid_dir.write_text("not a directory")
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        "[build]\n"
        f'context_dir = "{Path(config.context_dir).as_posix()}"\n'
        f'output_dir = "{Path(config.output_dir).as_posix()}"\n'
        'include_paths = ["app.py"]\n'
        "use_cache = false\n"
        "detect_system_packages = false\n",
        encoding="utf-8",
    )
    pushed = []
    monkeypatch.setattr(ImageBuilder, "push", lambda self, **kwargs: pushed.append(True))

    with pytest.raises(SystemExit) as error:
        _run_cli(
            monkeypatch,
            [
                "build", "--config", str(config_path), "--record-history",
                "--history-dir", str(invalid_dir), "--push",
            ],
        )

    captured = capsys.readouterr()
    assert error.value.code == 1
    assert "Build history error" in captured.err
    assert "Built:" not in captured.out
    assert not pushed
    assert (Path(config.output_dir) / "index.json").exists()


def test_cli_history_options_and_push_outcome(tmp_path, monkeypatch):
    _offline_base(monkeypatch)
    config = _history_config(tmp_path)
    secret = "synthetic-password-never-in-history"

    def fail_push(self, **kwargs):
        assert kwargs["password"] == secret
        raise RuntimeError("synthetic push failure")

    monkeypatch.setattr(ImageBuilder, "push", fail_push)
    monkeypatch.setattr(cli, "detect_python_version", lambda context: "3.11")
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        "[build]\n"
        f'context_dir = "{Path(config.context_dir).as_posix()}"\n'
        f'output_dir = "{Path(config.output_dir).as_posix()}"\n'
        f'history_dir = "{Path(config.history_dir).as_posix()}"\n'
        'include_paths = ["app.py"]\n'
        "use_cache = false\n"
        "detect_system_packages = false\n"
        "record_history = true\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="synthetic push failure"):
        _run_cli(
            monkeypatch,
            [
                "build", "--config", str(config_path), "--push",
                "--username", "synthetic-user", "--password", secret,
            ],
        )
    path = Path(config.history_dir) / "history.json"
    assert BuildHistory(Path(config.history_dir)).list()[0]["status"] == "success"
    assert secret not in path.read_text(encoding="utf-8")

    _run_cli(
        monkeypatch, ["build", "--config", str(config_path), "--no-record-history"]
    )
    assert len(BuildHistory(Path(config.history_dir)).list()) == 1
