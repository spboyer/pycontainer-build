"""OCI image-config StopSignal and Volumes (#159)."""
import hashlib
import json
from pathlib import Path

import pytest

from pycontainer import cli
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.oci import build_config_json
from pycontainer.security import KUBERNETES_FRAGMENT, SECURITY_DIR

BASE = {
    "architecture": "amd64",
    "os": "linux",
    "config": {
        "Env": [],
        "WorkingDir": "/",
        "StopSignal": "SIGQUIT",
        "Volumes": {"/var/lib/base": {}, "/data": {}},
    },
    "rootfs": {"type": "layers", "diff_ids": []},
}


def _cfg(**kwargs):
    return build_config_json("amd64", "linux", {}, "/app", ["python"], [], **kwargs)


def test_absent_settings_preserve_base_values_verbatim():
    result = _cfg(base_config=BASE)
    assert result["config"]["StopSignal"] == "SIGQUIT"
    assert list(result["config"]["Volumes"]) == ["/var/lib/base", "/data"]


def test_explicit_stop_signal_overrides_base():
    result = _cfg(base_config=BASE, stop_signal="SIGTERM")
    assert result["config"]["StopSignal"] == "SIGTERM"
    assert BASE["config"]["StopSignal"] == "SIGQUIT"


def test_declared_volumes_merge_with_base_sorted_and_deduplicated():
    result = _cfg(base_config=BASE, volumes=["/srv/uploads", "/data"])
    assert result["config"]["Volumes"] == {"/data": {}, "/srv/uploads": {}, "/var/lib/base": {}}
    assert list(result["config"]["Volumes"]) == ["/data", "/srv/uploads", "/var/lib/base"]
    assert BASE["config"]["Volumes"] == {"/var/lib/base": {}, "/data": {}}


def test_without_base_only_configured_values_are_emitted():
    assert "StopSignal" not in _cfg()["config"]
    assert "Volumes" not in _cfg()["config"]
    result = _cfg(stop_signal="SIGINT", volumes=["/b", "/a"])
    assert result["config"]["StopSignal"] == "SIGINT"
    assert list(result["config"]["Volumes"]) == ["/a", "/b"]


@pytest.mark.parametrize(
    "signal", ["SIGTERM", "SIGKILL", "SIGUSR1", "SIGWINCH", "SIGRTMIN", "SIGRTMIN+3", "SIGRTMAX-1"]
)
def test_valid_stop_signals(signal):
    assert BuildConfig(stop_signal=signal).stop_signal == signal


@pytest.mark.parametrize(
    "signal", ["", "TERM", "sigterm", "15", "SIGFOO", " SIGTERM", "SIGTERM ", "SIGRTMIN+16", "SIGRTMAX-0", "SIGRTMIN+01"]
)
def test_invalid_stop_signals_are_rejected(signal):
    with pytest.raises(ValueError, match="stop_signal"):
        BuildConfig(stop_signal=signal)


@pytest.mark.parametrize(
    "path", ["", "data", "./data", "/", "/data/../etc", "/data/.", "//data", "/data/", "C:\\data", "/da\x00ta", "/a\nb", "/a\x7fb", "/a\x85b", "/a\u200bb"]
)
def test_unsafe_or_relative_volume_paths_are_rejected(path):
    with pytest.raises(ValueError, match="volumes"):
        BuildConfig(volumes=[path])


def test_duplicate_volume_paths_are_deduplicated_in_order():
    assert BuildConfig(volumes=["/b", "/a", "/b"]).volumes == ["/b", "/a"]


def test_toml_and_cli_overrides(tmp_path):
    path = tmp_path / "pycontainer.toml"
    path.write_text('[build]\nstop_signal = "SIGINT"\nvolumes = ["/data"]\n')
    cfg = config_from_file(path)
    assert (cfg.stop_signal, cfg.volumes) == ("SIGINT", ["/data"])
    cfg = config_from_file(path, {"stop_signal": "SIGTERM", "volumes": ["/cache"]})
    assert (cfg.stop_signal, cfg.volumes) == ("SIGTERM", ["/cache"])
    path.write_text('[build]\nvolumes = ["relative"]\n')
    with pytest.raises(ValueError, match="volumes"):
        config_from_file(path)


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
def test_runtime_metadata_build_arguments_resolve_and_redact(tmp_path, capsys, table):
    path = tmp_path / "pycontainer.toml"
    path.write_text(
        f'[{table}]\nstop_signal = "${{SIGNAL}}"\n'
        'volumes = ["/data/${NAME}", "/price$$5"]\n'
        f'[{table}.build-args]\nSIGNAL = "SIGINT"\nNAME = "private-volume"\n'
    )
    config = config_from_file(path, {"dry_run": True}, ["SIGNAL=SIGTERM"])

    assert config.stop_signal == "SIGTERM"
    assert config.volumes == ["/data/private-volume", "/price$5"]
    ImageBuilder(config).build()
    output = capsys.readouterr().out
    assert "private-volume" not in output
    assert "/data/<redacted>" in output

    other = config_from_file(path, {}, ["NAME=another-volume"])
    assert config.cache_identity() != other.cache_identity()
    with pytest.raises(ValueError) as error:
        config_from_file(path, {}, ["SIGNAL=private-invalid"])
    assert "private-invalid" not in str(error.value)
    assert "<redacted>" in str(error.value)


def test_volume_literal_dollars_require_explicit_interpolation():
    assert BuildConfig(volumes=["/data/$$cash"]).volumes == ["/data/$$cash"]
    assert BuildConfig(volumes=["/data/$$cash"], build_args={}).volumes == ["/data/$cash"]
    with pytest.raises(ValueError, match="Undeclared build argument"):
        BuildConfig(volumes=["/data/${NAME}"], build_args={})


def test_cli_flags_reach_build_config(monkeypatch):
    configs = []

    class FakeImageBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", FakeImageBuilder)
    monkeypatch.setattr(
        "sys.argv",
        ["pycontainer", "build", "--base-image", "python:3.12-slim", "--stop-signal", "SIGINT",
         "--volume", "/data", "--volume", "/cache"],
    )
    cli.main()
    assert configs[0].stop_signal == "SIGINT"
    assert configs[0].volumes == ["/data", "/cache"]


def test_cli_rejects_invalid_signal(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["pycontainer", "build", "--stop-signal", "TERM"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "stop_signal" in capsys.readouterr().err


def test_built_layout_serializes_fields_with_consistent_descriptors(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hi')\n")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, layers_dir, os_name, arch: ([], BASE)
    )
    output = tmp_path / "out"
    builder = ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(output),
            include_paths=["app.py"],
            use_cache=False,
            stop_signal="SIGTERM",
            volumes=["/srv/uploads"],
            tags=["stable"],
        )
    )
    builder.build()
    blobs = output / "blobs" / "sha256"
    first_index = (output / "index.json").read_bytes()
    builder.build()
    assert (output / "index.json").read_bytes() == first_index
    index = json.loads(first_index)
    assert len(index["manifests"]) == 2
    manifest_desc = index["manifests"][0]
    manifest_bytes = (blobs / manifest_desc["digest"].split(":", 1)[1]).read_bytes()
    for descriptor in index["manifests"]:
        assert hashlib.sha256(manifest_bytes).hexdigest() == descriptor["digest"].split(":", 1)[1]
        assert len(manifest_bytes) == descriptor["size"]
    manifest = json.loads(manifest_bytes)
    config_desc = manifest["config"]
    config_bytes = (blobs / config_desc["digest"].split(":", 1)[1]).read_bytes()
    assert hashlib.sha256(config_bytes).hexdigest() == config_desc["digest"].split(":", 1)[1]
    assert len(config_bytes) == config_desc["size"]
    image = json.loads(config_bytes)
    assert len(image["rootfs"]["diff_ids"]) == len(manifest["layers"])
    config = image["config"]
    assert config["StopSignal"] == "SIGTERM"
    assert config["Volumes"] == {"/data": {}, "/srv/uploads": {}, "/var/lib/base": {}}


def test_interpolated_runtime_metadata_is_written_to_image(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hi')\n")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, layers_dir, os_name, arch: ([], BASE)
    )
    output = tmp_path / "out"
    builder = ImageBuilder(BuildConfig(
        context_dir=str(context), output_dir=str(output), include_paths=["app.py"],
        use_cache=False, stop_signal="${SIGNAL}", volumes=["/data/${NAME}"],
        build_args={"SIGNAL": "SIGTERM", "NAME": "uploads"},
    ))

    builder.build()

    config_blob = output / "blobs" / "sha256" / builder.config_digest.split(":", 1)[1]
    image = json.loads(config_blob.read_bytes())["config"]
    assert image["StopSignal"] == "SIGTERM"
    assert image["Volumes"] == {"/data": {}, "/data/uploads": {}, "/var/lib/base": {}}


def test_security_fragment_coexists_with_runtime_metadata(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('hi')\n")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, layers_dir, os_name, arch: ([], BASE)
    )
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(
        '[build]\nstop_signal = "SIGINT"\nvolumes = ["/srv/uploads", "/data"]\n'
        '[build.security.kubernetes]\nread_only_root_filesystem = true\n'
    )
    secured_output = tmp_path / "secured"
    secured_config = config_from_file(
        config_path,
        {
            "context_dir": str(context),
            "output_dir": str(secured_output),
            "use_cache": False,
            "stop_signal": "SIGTERM",
        },
    )
    secured = ImageBuilder(secured_config)
    secured.build()

    plain = ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / "plain"),
            use_cache=False,
            stop_signal="SIGTERM",
            volumes=["/srv/uploads", "/data"],
        )
    )
    plain.build()

    assert (secured.config_digest, secured.manifest_digest) == (
        plain.config_digest, plain.manifest_digest
    )
    config_blob = secured_output / "blobs" / "sha256" / secured.config_digest.split(":", 1)[1]
    image_config = json.loads(config_blob.read_bytes())["config"]
    assert image_config["StopSignal"] == "SIGTERM"
    assert image_config["Volumes"] == {"/data": {}, "/srv/uploads": {}, "/var/lib/base": {}}
    fragment = secured_output / SECURITY_DIR / KUBERNETES_FRAGMENT
    assert json.loads(fragment.read_text()) == {"readOnlyRootFilesystem": True}


@pytest.mark.parametrize("alias", ["SIGIOT", "SIGCLD", "SIGPOLL"])
def test_stop_signal_rejects_alias_names(alias):
    with pytest.raises(ValueError, match="stop_signal"):
        BuildConfig(tag="t", stop_signal=alias)
