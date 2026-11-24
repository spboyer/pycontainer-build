import hashlib
import json
import sys
from copy import deepcopy
from dataclasses import asdict

import pytest

from pycontainer import cli
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.framework import apply_framework_defaults
from pycontainer.release_notes import (
    DEFAULT_RELEASE_NOTES_LABEL,
    MAX_RELEASE_NOTES_BYTES,
    ReleaseNotesError,
    read_release_notes,
)

SECRET = "release-notes-content-marker"


def _context(tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hello')\n", encoding="utf-8")
    return context


def _build(tmp_path, monkeypatch, base_config=None, output="output", **settings):
    monkeypatch.setattr(
        ImageBuilder,
        "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], base_config),
    )
    settings.setdefault("context_dir", str(tmp_path / "context"))
    builder = ImageBuilder(BuildConfig(
        output_dir=str(tmp_path / output), use_cache=False, **settings
    ))
    builder.build()
    blobs = tmp_path / output / "blobs" / "sha256"
    config_bytes = (blobs / builder.config_digest.split(":", 1)[1]).read_bytes()
    manifest_bytes = (blobs / builder.manifest_digest.split(":", 1)[1]).read_bytes()
    assert builder.config_digest == "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    assert manifest["config"]["size"] == len(config_bytes)
    return builder, json.loads(config_bytes)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"a" * MAX_RELEASE_NOTES_BYTES, "a" * MAX_RELEASE_NOTES_BYTES),
        # Two-byte characters: exactly 8 KiB of UTF-8 but only 4096 characters.
        (("\u00e9" * 4096).encode("utf-8"), "\u00e9" * 4096),
        # CRLF input is twice the cap before normalization and exactly the cap after.
        (b"\r\n" * MAX_RELEASE_NOTES_BYTES, "\n" * MAX_RELEASE_NOTES_BYTES),
        (b"\xef\xbb\xbf" + b"x" * MAX_RELEASE_NOTES_BYTES, "x" * MAX_RELEASE_NOTES_BYTES),
        # Largest raw input that can normalize to the cap: BOM plus 8192 CRLFs.
        (b"\xef\xbb\xbf" + b"\r\n" * MAX_RELEASE_NOTES_BYTES, "\n" * MAX_RELEASE_NOTES_BYTES),
        (b"one\r\ntwo\rthree\n", "one\ntwo\nthree\n"),
        (b"", ""),
    ],
    ids=["ascii-cap", "utf8-cap", "crlf-cap", "bom-cap", "bom-crlf-cap", "mixed-lines", "empty"],
)
def test_release_notes_at_cap_are_accepted_and_normalized(tmp_path, raw, expected):
    path = tmp_path / "notes.md"
    path.write_bytes(raw)
    assert read_release_notes(path) == expected


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"a" * (MAX_RELEASE_NOTES_BYTES + 1), "8193 bytes after line-ending normalization"),
        (("\u00e9" * 4096).encode("utf-8") + b"a", "8193 bytes after"),
        (b"\r\n" * MAX_RELEASE_NOTES_BYTES + b"a", "8193 bytes after"),
        (b"\r\n" * (MAX_RELEASE_NOTES_BYTES + 2), "exceeds 8192 bytes"),
        (b"a" * 1_000_000, "exceeds 8192 bytes"),
    ],
    ids=["ascii-over", "utf8-over", "crlf-over", "raw-over", "large-file"],
)
def test_release_notes_over_cap_are_rejected_without_truncation(tmp_path, raw, message):
    path = tmp_path / "notes.md"
    path.write_bytes(raw)
    with pytest.raises(ReleaseNotesError, match=message):
        read_release_notes(path)


@pytest.mark.parametrize(
    "raw, offset",
    [
        (SECRET.encode() + b"\xff", len(SECRET)),
        (b"\xef\xbb\xbf" + SECRET.encode() + b"\xc3", 3 + len(SECRET)),
        (SECRET.encode("utf-16"), 0),
    ],
)
def test_non_utf8_release_notes_report_offset_without_content(tmp_path, raw, offset):
    path = tmp_path / "notes.md"
    path.write_bytes(raw)
    with pytest.raises(ReleaseNotesError, match=f"not valid UTF-8 .*offset {offset}") as error:
        read_release_notes(path)
    assert SECRET not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_missing_and_directory_release_notes_are_explicit(tmp_path):
    with pytest.raises(ReleaseNotesError, match="Cannot read release notes file"):
        read_release_notes(tmp_path / "missing.md")
    with pytest.raises(ReleaseNotesError, match="Cannot read release notes file"):
        read_release_notes(tmp_path)
    with pytest.raises(ReleaseNotesError, match="invalid path"):
        read_release_notes(tmp_path / "bad\0name.md")


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"release_notes_file": ""}, "release_notes_file must be"),
        ({"release_notes_file": 1}, "release_notes_file must be"),
        ({"release_notes_label": " "}, "release_notes_label keys"),
        ({"release_notes_label": None}, "release_notes_label must be"),
    ],
)
def test_invalid_release_notes_settings(settings, message):
    with pytest.raises(ValueError, match=message):
        BuildConfig(**settings)


def test_default_build_has_no_release_notes_label(tmp_path, monkeypatch):
    _context(tmp_path)
    builder, image_config = _build(tmp_path, monkeypatch)
    assert builder.release_notes_labels == {}
    assert "Labels" not in image_config["config"]


def test_release_notes_label_overrides_base_and_keeps_label_precedence(tmp_path, monkeypatch):
    context = _context(tmp_path)
    (context / "notes.md").write_bytes(b"## 1.0\r\n- fixed\r\n")
    base_config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": [], "Labels": {
            DEFAULT_RELEASE_NOTES_LABEL: "base notes", "shared": "base", "base-only": "kept",
        }},
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    _, image_config = _build(
        tmp_path, monkeypatch, base_config=base_config,
        release_notes_file="notes.md", labels={"shared": "explicit"},
    )
    labels = image_config["config"]["Labels"]
    assert labels == {
        "base-only": "kept",
        DEFAULT_RELEASE_NOTES_LABEL: "## 1.0\n- fixed\n",
        "shared": "explicit",
    }
    assert list(labels) == sorted(labels)


def test_release_notes_coexist_with_runtime_config(tmp_path, monkeypatch):
    context = _context(tmp_path)
    (context / "notes.md").write_text("notes", encoding="utf-8")
    base_config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": [], "StopSignal": "SIGQUIT", "Volumes": {"/base": {}}},
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    _, image_config = _build(
        tmp_path, monkeypatch, base_config=base_config, release_notes_file="notes.md",
        stop_signal="SIGTERM", volumes=["/data"],
    )
    config = image_config["config"]
    assert config["Labels"][DEFAULT_RELEASE_NOTES_LABEL] == "notes"
    assert config["StopSignal"] == "SIGTERM"
    assert config["Volumes"] == {"/base": {}, "/data": {}}


def test_crlf_and_lf_release_notes_produce_identical_digests(tmp_path, monkeypatch):
    _context(tmp_path)
    crlf = tmp_path / "crlf.md"
    lf = tmp_path / "lf.md"
    crlf.write_bytes(b"line one\r\nline two\r\n")
    lf.write_bytes(b"line one\nline two\n")
    first, _ = _build(tmp_path, monkeypatch, output="a", release_notes_file=str(crlf),
                      release_notes_label="example.notes")
    second, labels = _build(tmp_path, monkeypatch, output="b", release_notes_file=str(lf),
                            release_notes_label="example.notes")
    repeat, _ = _build(tmp_path, monkeypatch, output="c", release_notes_file=str(lf),
                       release_notes_label="example.notes")
    assert labels["config"]["Labels"] == {"example.notes": "line one\nline two\n"}
    assert first.config_digest == second.config_digest == repeat.config_digest
    assert first.manifest_digest == second.manifest_digest == repeat.manifest_digest

    lf.write_bytes(b"line one\nline three\n")
    changed, _ = _build(tmp_path, monkeypatch, output="d", release_notes_file=str(lf),
                        release_notes_label="example.notes")
    assert changed.config_digest != first.config_digest
    assert changed.layers[-1].digest == first.layers[-1].digest


def test_release_notes_are_read_once_per_builder(tmp_path, monkeypatch):
    context = _context(tmp_path)
    notes = context / "notes.md"
    notes.write_text("first", encoding="utf-8")
    builder, _ = _build(tmp_path, monkeypatch, release_notes_file="notes.md",
                        release_notes_label="example.notes")
    notes.unlink()
    builder.build()
    blobs = tmp_path / "output" / "blobs" / "sha256"
    image_config = json.loads((blobs / builder.config_digest.split(":", 1)[1]).read_bytes())
    assert image_config["config"]["Labels"] == {"example.notes": "first"}


def test_release_notes_key_collision_is_a_configuration_error(tmp_path):
    context = _context(tmp_path)
    (context / "notes.md").write_text(SECRET, encoding="utf-8")
    with pytest.raises(ReleaseNotesError, match="'example.notes' conflict") as error:
        ImageBuilder(BuildConfig(
            context_dir=str(context), use_cache=False, release_notes_file="notes.md",
            release_notes_label="example.notes", labels={"example.notes": "explicit-value"},
        ))
    assert SECRET not in str(error.value) and "explicit-value" not in str(error.value)


def test_release_notes_framework_key_wins_over_detected_default(tmp_path, monkeypatch):
    context = _context(tmp_path)
    (context / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    (context / "notes.md").write_text("notes", encoding="utf-8")
    _, image_config = _build(tmp_path, monkeypatch, release_notes_file="notes.md",
                             release_notes_label="framework")
    assert image_config["config"]["Labels"] == {"framework": "notes"}

    with pytest.raises(ReleaseNotesError, match="'framework' conflict"):
        ImageBuilder(BuildConfig(
            context_dir=str(context), use_cache=False, release_notes_file="notes.md",
            release_notes_label="framework", labels={"framework": "custom"},
        ))


@pytest.mark.parametrize("copy_mode", ["same", "deepcopy", "serialized"])
def test_release_notes_override_previously_generated_framework_label(tmp_path, monkeypatch, copy_mode):
    context = _context(tmp_path)
    (context / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (context / "notes.md").write_text("notes")
    config = BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "output"), use_cache=False,
        release_notes_file="notes.md", release_notes_label="framework",
    )
    apply_framework_defaults(config, context)
    if copy_mode == "deepcopy":
        config = deepcopy(config)
    elif copy_mode == "serialized":
        config = BuildConfig(**json.loads(json.dumps(asdict(config))))
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda *args: ([], None))

    builder = ImageBuilder(config)
    builder.build()
    blobs = tmp_path / "output" / "blobs" / "sha256"
    image = json.loads((blobs / builder.config_digest.split(":", 1)[1]).read_bytes())
    assert image["config"]["Labels"]["framework"] == "notes"
    ImageBuilder(config)


def test_release_notes_reject_explicit_label_matching_a_generated_default(tmp_path):
    context = _context(tmp_path)
    (context / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (context / "notes.md").write_text("notes")
    config = BuildConfig(
        context_dir=str(context), use_cache=False,
        release_notes_file="notes.md", release_notes_label="framework",
    )
    apply_framework_defaults(config, context)
    config.labels["framework"] = "fastapi"

    with pytest.raises(ReleaseNotesError, match="'framework' conflict"):
        ImageBuilder(config)


def _write_toml(tmp_path, context, extra=""):
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        f"""
[build]
context-dir = "{context.as_posix()}"
dry-run = true
release-notes-file = "notes.md"
{extra}
""",
        encoding="utf-8",
    )
    return config_path


def test_toml_settings_and_dry_run_never_print_contents(tmp_path, capsys, caplog):
    context = _context(tmp_path)
    (context / "notes.md").write_text(SECRET, encoding="utf-8")
    config = config_from_file(
        _write_toml(tmp_path, context, 'release-notes-label = "example.notes"')
    )
    assert config.release_notes_file == "notes.md"
    assert config.release_notes_label == "example.notes"
    builder = ImageBuilder(config)
    builder.build()
    captured = capsys.readouterr()
    assert f"Release Notes: label example.notes, {len(SECRET)} bytes (contents hidden)" in captured.out
    assert SECRET not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["--label", f"{DEFAULT_RELEASE_NOTES_LABEL}=explicit"], "conflict"),
        (["--release-notes", "missing.md"], "Cannot read release notes file"),
    ],
)
def test_cli_release_notes_errors_are_usage_errors(
    tmp_path, monkeypatch, capsys, arguments, message
):
    context = _context(tmp_path)
    (context / "notes.md").write_text(SECRET, encoding="utf-8")
    argv = ["pycontainer", "build", "--context", str(context), "--dry-run",
            "--base-image", "python:3.11-slim", "--release-notes", "notes.md"]
    monkeypatch.setattr(sys, "argv", argv + arguments)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert message in captured.err
    assert SECRET not in captured.out + captured.err


def test_cli_release_notes_label_overrides_file(tmp_path, monkeypatch, capsys):
    context = _context(tmp_path)
    (context / "notes.md").write_text(SECRET, encoding="utf-8")
    config_path = _write_toml(tmp_path, context, 'release-notes-label = "file.notes"')
    builders = []

    class RecordingBuilder(ImageBuilder):
        def __init__(self, config):
            super().__init__(config)
            builders.append(self)

    monkeypatch.setattr(cli, "ImageBuilder", RecordingBuilder)
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "build", "--config", str(config_path),
        "--release-notes-label", "cli.notes",
    ])
    cli.main()
    assert builders[0].release_notes_labels == {"cli.notes": SECRET}
    assert SECRET not in capsys.readouterr().out


def test_release_notes_and_argument_labels_keep_precedence_and_redact_plan(
    tmp_path, monkeypatch, capsys,
):
    context = _context(tmp_path)
    (context / "notes.md").write_text(SECRET, encoding="utf-8")
    value = "private-notes-key"
    settings = {
        "release_notes_file": "notes.md",
        "release_notes_label": value,
        "build_args": {"VALUE": value},
        "labels": {"version": "${VALUE}"},
    }
    base = {
        "architecture": "amd64", "os": "linux",
        "config": {"Env": [], "Labels": {value: "base notes", "version": "base"}},
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    _, image = _build(tmp_path, monkeypatch, base_config=base, **settings)
    assert image["config"]["Labels"] == {value: SECRET, "version": value}

    ImageBuilder(BuildConfig(context_dir=str(context), dry_run=True, **settings)).build()
    output = capsys.readouterr()
    assert "Release Notes: label <redacted>" in output.out
    assert value not in output.out + output.err
    assert SECRET not in output.out + output.err


@pytest.mark.parametrize("failure", ["missing", "encoding", "collision"])
def test_cli_release_notes_errors_redact_argument_values(
    tmp_path, monkeypatch, capsys, failure,
):
    context = _context(tmp_path)
    value = "private-notes-key"
    filename = f"{value}.md"
    if failure != "missing":
        (context / filename).write_bytes(
            SECRET.encode() + (b"\xff" if failure == "encoding" else b"")
        )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[build]\ncontext-dir = "{context.as_posix()}"\ndry-run = true\n'
        f'[build.build-args]\nVALUE = "{value}"\n'
    )
    arguments = [
        "pycontainer", "build", "--config", str(config_path),
        "--release-notes", filename, "--release-notes-label", value,
    ]
    if failure == "collision":
        arguments += ["--label", f"{value}=explicit"]
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "<redacted>" in output.err
    assert value not in output.out + output.err
    assert SECRET not in output.out + output.err
