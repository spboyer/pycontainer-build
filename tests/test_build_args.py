"""Build argument grammar, one-pass expansion and image/cache boundaries."""
import json
import logging
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pycontainer import cli
from pycontainer import auth as auth_module
from pycontainer import builder as builder_module
from pycontainer.auth import RegistryAuth
from pycontainer.build_args import resolve_arguments
from pycontainer.builder import ImageBuilder
from pycontainer.cache import LayerCache
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.registry_client import RegistryClient
from pycontainer.system_packages import report_system_packages


def test_direct_config_without_arguments_preserves_runtime_dollars():
    config = BuildConfig(cmd=["/bin/sh", "-c", "echo ${PORT} $$"])
    assert config.cmd == ["/bin/sh", "-c", "echo ${PORT} $$"]


def test_explicit_empty_arguments_reject_undeclared_references():
    with pytest.raises(ValueError, match="Undeclared build argument reference"):
        BuildConfig(build_args={}, cmd=["echo ${PORT}"])


def test_explicit_empty_arguments_expand_escaped_dollars():
    config = BuildConfig(build_args={}, cmd=["echo $$PORT"])
    assert config.cmd == ["echo $PORT"]


def test_cli_without_config_rejects_undeclared_reference(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--tag", "app:${VERSION}"])
    monkeypatch.setattr(cli, "ImageBuilder", lambda config: pytest.fail("builder constructed"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "Undeclared build argument reference" in capsys.readouterr().err


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


@pytest.mark.parametrize("table", ["build", "tool.pycontainer"])
def test_cli_override_replaces_invalid_file_value_before_build_arg_interpolation(tmp_path, table):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[{table}]\ntag = 42\n'
        f'[{table}.build-args]\nVERSION = "default"\n'
    )
    config = config_from_file(
        path, {"tag": "override:${VERSION}"}, ["VERSION=explicit"]
    )
    assert config.tag == "override:explicit"


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


@pytest.mark.parametrize("operation", ["build", "push"])
@pytest.mark.parametrize(("category", "exit_code"), [("findings", 3), ("scanner", 4)])
def test_scan_errors_redact_arguments_and_preserve_exit_code(
    tmp_path, monkeypatch, capsys, operation, category, exit_code,
):
    from pycontainer.scanning import ScanError

    value = "private-scan-value"
    path = tmp_path / "config.toml"
    path.write_text(f'[build.build-args]\nVALUE = "{value}"\n')

    class FailingBuilder:
        def __init__(self, config):
            pass

        def build(self):
            if operation == "build":
                raise ScanError(category, f"failure: {value}")
            return "layout"

        def push(self, **kwargs):
            raise ScanError(category, f"failure: {value}")

    monkeypatch.setattr(cli, "ImageBuilder", FailingBuilder)
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build", "--config", str(path), "--push"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    output = capsys.readouterr()
    assert error.value.code == exit_code
    assert value not in output.out + output.err
    assert "<redacted>" in output.err


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


@pytest.mark.parametrize("fail", [False, True])
def test_cli_redacts_retained_build_argument_diagnostics(
    tmp_path, monkeypatch, capsys, caplog, fail
):
    value = "private-retained-value"
    config = BuildConfig(context_dir=str(tmp_path), build_args={"VALUE": value})
    config.build_args.clear()

    class Builder:
        def __init__(self, config):
            pass

        def build(self):
            logging.getLogger("retained-argument-test").warning("diagnostic %s", value)
            if fail:
                raise RuntimeError(f"synthetic failure: {value}")
            return "layout"

    monkeypatch.setattr(cli, "resolve_build_config", lambda *args: (config, None))
    monkeypatch.setattr(cli, "ImageBuilder", Builder)
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build"])
    with caplog.at_level(logging.WARNING):
        if fail:
            with pytest.raises(SystemExit) as error:
                cli.main()
            assert error.value.code == 2
        else:
            cli.main()

    output = capsys.readouterr()
    assert value not in output.out + output.err + caplog.text
    assert "<redacted>" in caplog.text
    if fail:
        assert "<redacted>" in output.err


@pytest.mark.parametrize("arguments", [None, {}, {"EMPTY": ""}])
def test_cli_preserves_failure_cause_without_redactable_values(
    tmp_path, monkeypatch, arguments
):
    config = BuildConfig(context_dir=str(tmp_path), build_args=arguments)

    class Builder:
        def __init__(self, config):
            pass

        def build(self):
            try:
                raise ValueError("synthetic cause")
            except ValueError as error:
                raise RuntimeError("synthetic failure") from error

    monkeypatch.setattr(cli, "resolve_build_config", lambda *args: (config, None))
    monkeypatch.setattr(cli, "ImageBuilder", Builder)
    monkeypatch.setattr(sys, "argv", ["pycontainer", "build"])
    with pytest.raises(RuntimeError) as error:
        cli.main()
    assert isinstance(error.value.__cause__, ValueError)


def test_cli_label_precedence_interpolates_and_hides_metadata(tmp_path, monkeypatch, capsys):
    (tmp_path / "app.py").write_text("print('hello')")
    path = tmp_path / "config.toml"
    path.write_text(
        '[build.labels]\nversion = "file:${VERSION}"\nretained = "file"\n'
        '[build.build-args]\nVERSION = "private-version"\n'
    )
    configs = []

    class CaptureBuilder(ImageBuilder):
        def __init__(self, config):
            super().__init__(config)
            configs.append(config)

    monkeypatch.setattr(cli, "ImageBuilder", CaptureBuilder)
    monkeypatch.setattr(sys, "argv", [
        "pycontainer", "build", "--config", str(path), "--context", str(tmp_path),
        "--label", "version=cli:${VERSION}", "--manifest-annotation", "example=manifest-value",
        "--index-annotation", "example=index-value", "--dry-run",
    ])
    cli.main()
    assert configs[0].labels == {"version": "cli:private-version", "retained": "file"}
    assert configs[0].manifest_annotations == {"example": "manifest-value"}
    assert configs[0].index_annotations == {"example": "index-value"}
    output = capsys.readouterr()
    for value in ("private-version", "manifest-value", "index-value"):
        assert value not in output.out + output.err


def test_dry_run_redacts_values_in_non_expandable_fields(tmp_path, capsys):
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=[], dry_run=True,
        build_args={"PACKAGE": "libpq5", "PORT": "8000"},
        system_packages=["libpq5"], exposed_ports=[8000],
    )
    ImageBuilder(config).build()
    output = capsys.readouterr().out
    assert "libpq5" not in output
    assert "8000" not in output


def test_security_validation_redacts_interpolated_platform():
    with pytest.raises(ValueError, match="Linux targets only") as error:
        BuildConfig(
            platform="${PLATFORM}",
            build_args={"PLATFORM": "windows/amd64"},
            security={"kubernetes": {"read_only_root_filesystem": True}},
        )
    assert "windows/amd64" not in str(error.value)
    assert "<redacted>" in str(error.value)


def test_security_fragment_dry_run_redacts_build_arguments(tmp_path, capsys):
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=[], dry_run=True,
        build_args={"NAME": "security"},
        security={"kubernetes": {"read_only_root_filesystem": True}},
    )
    ImageBuilder(config).build()
    output = capsys.readouterr().out
    assert "security" not in output
    assert "<redacted>/kubernetes-<redacted>-context.json" in output


def test_security_fragment_output_path_redacts_build_arguments(tmp_path, monkeypatch, caplog):
    value = "private-output-value"
    (tmp_path / "app.py").write_text("print('hello')")
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=["app.py"],
        output_dir=str(tmp_path / "${OUTPUT}"), use_cache=False,
        build_args={"OUTPUT": value},
        security={"kubernetes": {"read_only_root_filesystem": True}},
    )
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda *args: ([], None))
    with caplog.at_level(logging.INFO):
        ImageBuilder(config).build()

    assert (tmp_path / value / "security" / "kubernetes-security-context.json").is_file()
    assert "Security fragment saved" in caplog.text
    assert value not in caplog.text
    assert "<redacted>" in caplog.text


@pytest.mark.parametrize("operation", ["pull", "push"])
@pytest.mark.parametrize("retain_values", [False, True])
def test_direct_build_redacts_registry_rate_limit_logs(
    tmp_path, monkeypatch, caplog, operation, retain_values
):
    value = "private-registry"
    config = BuildConfig(
        base_image="${REGISTRY}.example/team/base:v1",
        tag="${REGISTRY}.example/team/app:v1",
        build_args={"REGISTRY": value},
        context_dir=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        use_cache=False,
    )
    if retain_values:
        config.build_args.clear()
    builder = ImageBuilder(config)
    monkeypatch.setattr(builder_module, "get_registry_auth", lambda *args, **kwargs: RegistryAuth())

    def observe_and_stop(client, *args, **kwargs):
        client._observe_rate_limit(
            {"ratelimit-limit": "100", "ratelimit-remaining": "0"}, client.base_url
        )
        raise RuntimeError("stopped after rate-limit response")

    monkeypatch.setattr(RegistryClient, "pull_manifest", observe_and_stop)
    monkeypatch.setattr(RegistryClient, "push_blob", observe_and_stop)
    builder.manifest_digest = "sha256:" + "a" * 64
    builder.config_digest = "sha256:" + "b" * 64
    builder.layers = []
    manifest = tmp_path / "out" / "blobs" / "sha256" / ("a" * 64)
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"{}")

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="stopped"):
        if operation == "pull":
            builder._pull_base_image(tmp_path / "layers", "linux", "amd64")
        else:
            builder.push(show_progress=False)

    assert "rate limit" in caplog.text
    assert value not in caplog.text


@pytest.mark.parametrize("operation", ["pull", "push"])
@pytest.mark.parametrize("retain_values", [False, True])
def test_direct_build_redacts_azure_auth_logs(
    tmp_path, monkeypatch, caplog, operation, retain_values
):
    value = "privatehost"
    config = BuildConfig(
        base_image="${REGISTRY}.azurecr.io/team/base:v1",
        tag="${REGISTRY}.azurecr.io/team/app:v1",
        build_args={"REGISTRY": value},
        context_dir=str(tmp_path),
        output_dir=str(tmp_path / "out"),
        use_cache=False,
    )
    if retain_values:
        config.build_args.clear()
    for provider in (auth_module.EnvironmentAuthProvider, auth_module.DockerConfigAuthProvider):
        monkeypatch.setattr(provider, "get_token", lambda self, registry: None)
        monkeypatch.setattr(provider, "get_credentials", lambda self, registry: None)
    monkeypatch.setattr(
        auth_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1)
    )
    monkeypatch.setattr(
        RegistryClient, "pull_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stopped after auth")),
    )
    monkeypatch.setattr(
        RegistryClient, "push_blob",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stopped after auth")),
    )
    builder = ImageBuilder(config)
    builder.manifest_digest = "sha256:" + "a" * 64
    builder.config_digest = "sha256:" + "b" * 64
    builder.layers = []
    manifest = tmp_path / "out" / "blobs" / "sha256" / ("a" * 64)
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"{}")

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="stopped"):
        if operation == "pull":
            builder._pull_base_image(tmp_path / "layers", "linux", "amd64")
        else:
            builder.push(show_progress=False)

    assert "Azure CLI authentication failed" in caplog.text
    assert value not in caplog.text


def test_mirror_auth_redacts_retained_build_argument(tmp_path, monkeypatch, caplog):
    value = "private-mirror"
    config = BuildConfig(
        base_image="upstream.example/team/base:v1",
        registry_mirrors={"upstream.example": [f"{value}.azurecr.io"]},
        context_dir=str(tmp_path), use_cache=False, build_args={"VALUE": value},
    )
    config.build_args.clear()
    monkeypatch.setattr(
        auth_module.DockerConfigAuthProvider, "get_credentials", lambda *args: None
    )
    monkeypatch.setattr(
        auth_module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1)
    )
    monkeypatch.setattr(
        RegistryClient, "pull_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stopped after auth")),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="stopped"):
        ImageBuilder(config)._pull_base_image(tmp_path / "layers", "linux", "amd64")
    assert "Azure CLI authentication failed" in caplog.text
    assert value not in caplog.text
    assert "<redacted>" in caplog.text


def test_direct_build_redacts_system_package_logs(tmp_path, monkeypatch, caplog):
    value = "libpq5"
    (tmp_path / "app.py").write_text("print('hello')")
    config = BuildConfig(
        context_dir=str(tmp_path), include_paths=["app.py"],
        output_dir=str(tmp_path / "out"), use_cache=False,
        build_args={"PACKAGE": value}, system_packages=[value],
        detect_system_packages=False,
    )
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda *args: ([], None))

    with caplog.at_level(logging.WARNING):
        ImageBuilder(config).build()

    assert "Could not verify system packages" in caplog.text
    assert value not in caplog.text


def test_direct_build_redacts_conda_warning_paths(tmp_path, caplog):
    value = "private-requirements"
    (tmp_path / "environment.yml").write_text("name: example\n")
    (tmp_path / f"{value}.txt").write_text("")
    config = BuildConfig(
        context_dir=str(tmp_path), include_deps=True, dry_run=True,
        requirements_file="${NAME}.txt", build_args={"NAME": value},
    )

    with caplog.at_level(logging.WARNING):
        ImageBuilder(config).build()

    assert "Conda inputs ignored" in caplog.text
    assert "<redacted>.txt" in caplog.text
    assert value not in caplog.text


def test_unreadable_base_layer_diagnostic_redacts_build_arguments(tmp_path, caplog):
    value = "private-path"
    config = BuildConfig(
        build_args={"DIRECTORY": value}, system_packages=["libpq5"],
        detect_system_packages=False,
    )
    layer = SimpleNamespace(tar_path=tmp_path / value / "missing.tar")

    with caplog.at_level(logging.DEBUG):
        report_system_packages(config, [layer], redact=config.redact)

    assert "could not read base layer" in caplog.text
    assert value not in caplog.text


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
