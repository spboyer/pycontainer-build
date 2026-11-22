"""Build argument grammar, one-pass expansion and image/cache boundaries."""
import json
import logging
import sys
import tarfile
from pathlib import Path

import pytest

from pycontainer import cli
from pycontainer.build_args import resolve_arguments
from pycontainer.builder import ImageBuilder
from pycontainer.cache import LayerCache
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
def test_defaults_required_values_and_cli_precedence(tmp_path, table):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[{table}]\ntag = "app:${{VERSION}}"\n'
        f'[{table}.build-args]\nVERSION = {{default = "v1"}}\nOPTION = {{}}\n'
    )
    config = config_from_file(
        path, {"tag": "override:${VERSION}"},
        ["VERSION=v2", "OPTION=", "VERSION=v3=extra"],
    )
    assert config.tag == "override:v3=extra"
    assert config.build_args == {"VERSION": "v3=extra", "OPTION": ""}


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("${A}", "${B}$$"),
        ("$${A}", "${A}"),
        ("$$${A}", "$${B}$$"),
        ("$HOME/$$/cost$5", "$HOME/$/cost$5"),
        ("${EMPTY}x${B}", "xdone"),
    ],
)
def test_interpolation_is_one_pass_and_does_not_read_environment(monkeypatch, template, expected):
    monkeypatch.setenv("A", "host-value")
    config = BuildConfig(
        build_args={"A": "${B}$$", "B": "done", "EMPTY": ""},
        labels={"result": template, "${A}": "unchanged-key"},
    )
    assert config.labels == {"result": expected, "${A}": "unchanged-key"}
    assert config.env == {}


@pytest.mark.parametrize(
    ("declarations", "overrides", "message"),
    [
        ({"A": {}}, [], "Missing required"),
        ({}, ["A=value"], "Undeclared"),
        ({"A": "ok"}, ["A"], "KEY=VALUE"),
        ({"A": "ok"}, ["A-B=value"], "valid argument name"),
        ({"bad-name": "value"}, [], "Invalid build argument name"),
        ({"A": {"unexpected": "value"}}, [], "only default"),
        ({"A": {"default": 1}}, [], "must be a string"),
        ({"A": "private\nvalue"}, ["A=ok"], "control characters"),
        ({"A": ""}, ["A=private\x7fvalue"], "control characters"),
        ({"A": ""}, ["A=private\x85value"], "control characters"),
    ],
)
def test_invalid_arguments_fail_without_echoing_values(declarations, overrides, message):
    with pytest.raises(ValueError, match=message) as error:
        resolve_arguments(declarations, overrides)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("reference", ["${HOST}", "${A:-x}", "${A", "${}", "${A\n}"])
def test_invalid_references_fail_before_builder(tmp_path, monkeypatch, capsys, reference):
    path = tmp_path / "config.toml"
    path.write_text(f'[build]\nentrypoint = [{json.dumps(reference)}]\n')
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--config", str(path), "--dry-run"])
    monkeypatch.setattr(cli, "ImageBuilder", lambda config: pytest.fail("builder constructed"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    diagnostic = capsys.readouterr().err
    assert "reference" in diagnostic or "control characters" in diagnostic


@pytest.mark.parametrize("arguments", [[], ["--build-arg", "A"], ["--build-arg", "OTHER=x"]])
def test_cli_rejects_missing_malformed_and_undeclared_inputs_before_build(
    tmp_path, monkeypatch, capsys, arguments,
):
    path = tmp_path / "config.toml"
    path.write_text('[build.build-args]\nA = {}\n')
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "build", "--config", str(path), "--dry-run", *arguments,
    ])
    monkeypatch.setattr(cli, "ImageBuilder", lambda config: pytest.fail("builder constructed"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "argument" in capsys.readouterr().err


def test_cli_override_order_and_empty_value(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '[build]\nenv = {OPTION = "${OPTION}"}\nlabels = {version = "${VERSION}"}\n'
        '[build.build-args]\nVERSION = "default"\nOPTION = {}\n'
    )
    configs = []

    class CaptureBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", CaptureBuilder)
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "build", "--config", str(path),
        "--build-arg", "OPTION=", "--build-arg", "VERSION=first",
        "--build-arg", "VERSION=last=with=equals",
    ])
    cli.main()
    assert configs[0].env == {"OPTION": ""}
    assert configs[0].labels == {"version": "last=with=equals"}


def test_semantic_validation_redacts_escaped_argument_values(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.toml"
    path.write_text(
        '[build]\ntags = ["${VERSION}"]\n'
        "[build.build-args]\nVERSION = 'private\\value'\n"
    )
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--config", str(path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "private" not in capsys.readouterr().err


def test_real_dry_run_redacts_arguments_and_resolves_cli_fields(tmp_path, monkeypatch, capsys, caplog):
    (tmp_path / "app.py").write_text("print('hello')")
    path = tmp_path / "config.toml"
    path.write_text(
        '[build]\ntag = "app:${VERSION}"\nlabels = {version = "${VERSION}"}\n'
        '[build.build-args]\nVERSION = "private-version"\n'
    )
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "build", "--config", str(path), "--context", str(tmp_path),
        "--tag", "cli:${VERSION}", "--include", "app.py", "--dry-run", "--verbose",
    ])
    with caplog.at_level(logging.DEBUG):
        cli.main()
    output = capsys.readouterr()
    assert "private-version" not in output.out + output.err + caplog.text
    assert "cli:<redacted>" in output.out
    assert not (tmp_path / "dist").exists()


def test_build_cache_identity_and_no_implicit_runtime_leakage(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("print('hello')")
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda *args: ([], None))
    cache_dir = tmp_path / "cache"

    def build(version, unused="not-shipped", platform="linux/amd64"):
        config = BuildConfig(
            context_dir=str(tmp_path), include_paths=["app.py"],
            output_dir=str(tmp_path / "image"), cache_dir=str(cache_dir),
            build_args={"VERSION": version, "UNUSED": unused},
            workdir="/srv/${VERSION}", labels={"version": "${VERSION}"}, platform=platform,
        )
        builder = ImageBuilder(config)
        builder.build()
        image = json.loads(
            (tmp_path / "image" / "blobs" / "sha256" / builder.config_digest.split(":")[1]).read_bytes()
        )
        return config, builder, image

    first, builder, image = build("release-one")
    metadata = json.dumps(image)
    assert image["config"]["WorkingDir"] == "/srv/release-one"
    assert image["config"]["Labels"]["version"] == "release-one"
    assert "VERSION=" not in metadata and "UNUSED" not in metadata and "not-shipped" not in metadata
    assert image["rootfs"]["diff_ids"] == [layer.digest for layer in builder.layers]
    with tarfile.open(builder.layers[-1].tar_path) as archive:
        assert archive.getnames() == ["srv/release-one/app.py"]
    assert len(json.loads((cache_dir / "index.json").read_text())) == 1
    repeat, _, _ = build("release-one", unused="also-not-shipped")
    assert repeat.cache_identity() == first.cache_identity()
    assert len(json.loads((cache_dir / "index.json").read_text())) == 1
    build("release-two")
    build("release-two", platform="linux/arm64")
    entries = json.loads((cache_dir / "index.json").read_text())
    assert len(entries) == 3
    assert all(entry["source_files"] == [] for entry in entries.values())
    assert "release-one" not in json.dumps(entries) and "release-two" not in json.dumps(entries)
    assert first.cache_identity("other-backend") != first.cache_identity()
    cache = LayerCache(cache_dir)
    assert cache.get_layer(
        [(tmp_path / "app.py", Path("app.py"))],
        workdir=first.workdir, namespace=first.cache_identity("other-backend"),
    ) is None


def test_storage_locations_do_not_invalidate_equal_source_layer(tmp_path):
    import hashlib

    source = tmp_path / "app.py"
    source.write_text("print('hello')")
    first = BuildConfig(context_dir="first", output_dir="out-one", cache_dir="cache-one")
    second = BuildConfig(context_dir="second", output_dir="out-two", cache_dir="cache-two")
    cache = LayerCache(tmp_path / "cache")
    files = [(source, Path("app.py"))]
    layer = tmp_path / "layer.tar"
    layer.write_bytes(b"layer")
    digest = "sha256:" + hashlib.sha256(layer.read_bytes()).hexdigest()
    cache.store_layer(files, digest, layer, namespace=first.cache_identity())
    assert cache.get_layer(files, namespace=second.cache_identity()) is not None
