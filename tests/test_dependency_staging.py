import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from pycontainer import dependencies
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.dependencies import (
    MAX_REQUIREMENTS_BYTES,
    _target_options,
    install_requirements,
    staged_packages,
    target_python_version,
    target_wheel_platform,
    validate_requirements,
)
from pycontainer.registry_client import parse_image_reference


BASE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {
        "Env": [
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "PYTHON_VERSION=3.11.8",
        ],
        "WorkingDir": "/",
    },
    "rootfs": {"type": "layers", "diff_ids": []},
}


def _write_wheel(
    wheelhouse: Path, tag: str = "py3-none-any", function: str = "main"
) -> Path:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    wheel_path = wheelhouse / f"fixture_dep-1.0-{tag}.whl"
    files = {
        "fixture_dep/__init__.py": (
            f"def {function}():\n"
            "    print('fixture-dependency-import-ok')\n"
            "    return 0\n"
        ).encode(),
        "fixture_dep-1.0.dist-info/METADATA": (
            "Metadata-Version: 2.1\nName: fixture-dep\nVersion: 1.0\n\n"
        ).encode(),
        "fixture_dep-1.0.dist-info/WHEEL": (
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n"
        ).encode(),
        "fixture_dep-1.0.dist-info/entry_points.txt": (
            f"[console_scripts]\nfixture-dep = fixture_dep:{function}\n"
        ).encode(),
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode()
        writer.writerow((name, f"sha256={digest.rstrip('=')}", len(content)))
    writer.writerow(("fixture_dep-1.0.dist-info/RECORD", "", ""))
    files["fixture_dep-1.0.dist-info/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)
    return wheel_path


def _builder(tmp_path, monkeypatch, *, context, **options):
    monkeypatch.setattr(
        ImageBuilder,
        "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], BASE_CONFIG),
    )
    return ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / "output"),
            use_cache=False,
            **options,
        )
    )


def test_staged_package_metadata_errors_are_explicit_for_sbom(tmp_path):
    metadata = tmp_path / "broken-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Metadata-Version: 2.1\nName: broken\n\n")

    assert staged_packages(tmp_path) == []
    with pytest.raises(ValueError, match="missing Name or Version"):
        staged_packages(tmp_path, strict=True)


@pytest.mark.parametrize("function", ["main", "ma\u00efn"])
def test_requirements_install_stages_importable_target_wheel_and_sbom(
    tmp_path, monkeypatch, function
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    _write_wheel(context / "wheelhouse", function=function)
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    builder = _builder(
        tmp_path,
        monkeypatch,
        context=context,
        include_deps=True,
        generate_sbom=True,
        entrypoint=["fixture-dep"],
    )

    builder.build()

    dependency_layer = builder.layers[0]
    with tarfile.open(dependency_layer.tar_path) as archive:
        names = archive.getnames()
        assert "app/.python/site-packages/fixture_dep/__init__.py" in names
        assert (
            "app/.python/site-packages/fixture_dep-1.0.dist-info/METADATA"
            in names
        )
        script = archive.getmember("app/.python/bin/fixture-dep")
        assert script.isfile()
        assert script.mode == 0o755
        assert script.mtime == script.uid == script.gid == 0
        script_file = archive.extractfile(script)
        assert script_file is not None
        script_bytes = script_file.read()
        assert script_bytes.partition(b"\n")[0] == b"#!/usr/bin/env python"
        assert b"\r" not in script_bytes
        assert script_bytes.endswith(b"\n")
        script_text = script_bytes.decode("utf-8")
        assert f"for _part in {[function]!r}:\n" in script_text
        assert "sys.exit(_value())\n" in script_text
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        for member in archive.getmembers():
            if member.isfile():
                target = extracted / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                assert source is not None
                target.write_bytes(source.read())

    staged = extracted / "app" / ".python"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(staged / "site-packages")
    environment["PATH"] = f"{staged / 'bin'}:{environment['PATH']}"
    result = subprocess.run(
        [sys.executable, str(staged / "bin" / "fixture-dep")],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.stdout.strip() == "fixture-dependency-import-ok"
    repeated = _builder(
        tmp_path / "repeated",
        monkeypatch,
        context=context,
        include_deps=True,
        generate_sbom=True,
        entrypoint=["fixture-dep"],
    )
    repeated.build()
    assert dependency_layer.digest == repeated.layers[0].digest
    assert builder.manifest_digest == repeated.manifest_digest
    with tarfile.open(repeated.layers[0].tar_path) as archive:
        repeated_script = archive.extractfile("app/.python/bin/fixture-dep")
        assert repeated_script is not None
        assert repeated_script.read() == script_bytes
    record = (
        staged / "site-packages" / "fixture_dep-1.0.dist-info" / "RECORD"
    ).read_text()
    record_paths = [line.split(",", 1)[0] for line in record.splitlines() if line]
    assert "fixture_dep/__init__.py" in record_paths
    assert not [path for path in record_paths if ".." in path.replace("\\", "/")]

    config_path = (
        Path(builder.config.output_dir)
        / "blobs"
        / "sha256"
        / builder.config_digest.split(":", 1)[1]
    )
    image_config = json.loads(config_path.read_text())
    image_env = dict(item.split("=", 1) for item in image_config["config"]["Env"])
    assert image_env["PYTHONPATH"] == "/app/.python/site-packages"
    assert image_env["PATH"].startswith("/app/.python/bin:")
    sbom = json.loads((Path(builder.config.output_dir) / "sbom.json").read_text())
    assert [(package["name"], package["versionInfo"]) for package in sbom["packages"]] == [
        ("fixture-dep", "1.0")
    ]


def test_excluded_explicit_requirements_still_install_dependencies(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    generated = context / "gen"
    generated.mkdir(parents=True)
    (context / "app.py").write_text("print('app')\n")
    _write_wheel(generated / "wheelhouse")
    (generated / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    builder = _builder(
        tmp_path,
        monkeypatch,
        context=context,
        include_deps=True,
        requirements_file="gen/requirements.txt",
        exclude_paths=["gen"],
    )

    builder.build()

    dependency_layer, app_layer = builder.layers
    with tarfile.open(dependency_layer.tar_path) as archive:
        assert "app/.python/site-packages/fixture_dep/__init__.py" in archive.getnames()
    with tarfile.open(app_layer.tar_path) as archive:
        app_names = archive.getnames()
    assert "app/app.py" in app_names
    assert not [name for name in app_names if name.startswith("app/gen")]


def test_builder_selects_cyclonedx_for_staged_inventory(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    builder = _builder(
        tmp_path,
        monkeypatch,
        context=context,
        generate_sbom="cyclonedx",
    )

    builder.build()

    sbom = json.loads((Path(builder.config.output_dir) / "sbom.json").read_text())
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["components"] == []


def test_sbom_metadata_does_not_change_reproducible_image_digest(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    first = _builder(
        tmp_path / "first",
        monkeypatch,
        context=context,
        generate_sbom=True,
        reproducible=True,
    )
    second = _builder(
        tmp_path / "second",
        monkeypatch,
        context=context,
        generate_sbom=True,
        reproducible=True,
    )

    first.build()
    second.build()

    first_sbom = json.loads((Path(first.config.output_dir) / "sbom.json").read_text())
    second_sbom = json.loads((Path(second.config.output_dir) / "sbom.json").read_text())
    assert first.manifest_digest == second.manifest_digest
    assert first_sbom["documentNamespace"] != second_sbom["documentNamespace"]
    assert (
        first_sbom["creationInfo"]["created"]
        != second_sbom["creationInfo"]["created"]
    )


def test_windows_virtualenv_layout_stages_only_pure_python_packages(tmp_path, monkeypatch):
    context = tmp_path / "context"
    site_packages = context / ".venv" / "Lib" / "site-packages"
    metadata = site_packages / "windows_fixture-2.0.dist-info"
    metadata.mkdir(parents=True)
    (context / ".venv" / "pyvenv.cfg").write_text("version = 3.11.7\n")
    (site_packages / "windows_fixture.py").write_text("value = 2\n")
    executable = site_packages / "package_tool"
    executable.write_text("#!/usr/bin/env python\n")
    executable.chmod(0o755)
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: windows-fixture\nVersion: 2.0\n\n"
    )
    (metadata / "WHEEL").write_text(
        "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    (metadata / "entry_points.txt").write_text(
        "[console_scripts]\nwindows-fixture = windows_fixture:main\n"
    )
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    layer_dir = tmp_path / "layers"
    layer_dir.mkdir()

    layer = builder._create_deps_layer(layer_dir, base_config=BASE_CONFIG)

    with tarfile.open(layer.tar_path) as archive:
        names = archive.getnames()
        assert "app/.python/site-packages/windows_fixture.py" in names
        assert "app/.venv/Lib/site-packages/windows_fixture.py" not in names
        assert "app/.python/bin/windows-fixture" in names
        if os.name != "nt":
            assert (
                archive.getmember("app/.python/site-packages/package_tool").mode
                & 0o111
            )
        assert builder._staged_packages == [("windows-fixture", "2.0")]


def test_virtualenv_rejects_native_host_binaries(tmp_path, monkeypatch):
    context = tmp_path / "context"
    site_packages = context / ".venv" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    native_module = site_packages / "host_extension.pyd"
    native_module.write_bytes(b"not a target binary")
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    with pytest.raises(ValueError, match="Native virtualenv binary"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)


def test_virtualenv_target_python_must_match_image(tmp_path, monkeypatch):
    context = tmp_path / "context"
    site_packages = context / ".venv" / "lib" / "python3.10" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "package.py").write_text("value = 1\n")
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    with pytest.raises(ValueError, match="targets Python 3.10"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)


def test_reproducible_dependency_tar_ignores_source_timestamps(tmp_path, monkeypatch):
    context = tmp_path / "context"
    site_packages = context / "venv" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    package = site_packages / "package.py"
    package.write_text("value = 1\n")
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    layers_dir = tmp_path / "layers"
    layers_dir.mkdir()

    first = builder._create_deps_layer(layers_dir, base_config=BASE_CONFIG)
    os.utime(package, (1_600_000_000, 1_600_000_000))
    second = builder._create_deps_layer(layers_dir, base_config=BASE_CONFIG)

    assert first.digest == second.digest


def test_dependency_staging_rejects_unsupported_target_and_unbounded_requirements(
    tmp_path,
):
    with pytest.raises(ValueError, match="does not support target platform"):
        _target_options("windows", "amd64", "3.11")
    assert _target_options(
        "linux", "amd64", "3.11", "python:3.11-alpine",
        base_image_tag_verified=True,
    )[1] == "musllinux_1_2_x86_64"
    with pytest.raises(ValueError, match="custom base image"):
        target_python_version(
            "example/python:3.11",
            {"config": {"Env": ["PYTHON_VERSION=3.11.8"]}},
        )
    custom_config = {
        "config": {
            "Env": [
                "PYTHON_IMPLEMENTATION=CPython",
                "PYTHON_VERSION=3.11.8",
                "PYTHON_LIBC=glibc-2.17",
            ]
        }
    }
    assert target_python_version("example/python:custom", custom_config) == "3.11"
    assert _target_options(
        "linux", "amd64", "3.11", "example/python:custom", custom_config
    )[1] == "manylinux_2_17_x86_64"
    assert _target_options(
        "linux", "arm64", "3.11", base_image_tag_verified=True
    )[1] == "manylinux2014_aarch64"

    context = tmp_path / "context"
    context.mkdir()
    requirements = context / "requirements.txt"
    requirements.write_bytes(b"#" * (MAX_REQUIREMENTS_BYTES + 1))
    (context / "app.py").write_text("print('app')\n")
    builder = ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            include_deps=True,
            use_cache=False,
        )
    )
    with pytest.raises(ValueError, match="Requirements files exceed"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)


def _libc_config(*env):
    return {"config": {"Env": ["PYTHON_VERSION=3.11.8", *env]}}


DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("base_image", "libc", "expected"),
    [
        (f"python@{DIGEST}", "musl-1.2", "musllinux_1_2_x86_64"),
        (f"docker.io/library/python@{DIGEST}", "glibc-2.17", "manylinux_2_17_x86_64"),
        (f"python:3.11-alpine@{DIGEST}", "musl-1.2", "musllinux_1_2_x86_64"),
        (f"python:3.11-slim@{DIGEST}", "glibc-2.17", "manylinux_2_17_x86_64"),
        (f"python:3.11-slim@{DIGEST}", "musl-1.2", "musllinux_1_2_x86_64"),
        ("python:3.11-alpine", "musl-1.2", "musllinux_1_2_x86_64"),
        ("python:3.11-slim", "glibc-2.17", "manylinux2014_x86_64"),
        ("python", None, "manylinux2014_x86_64"),
    ],
)
def test_official_image_libc_uses_tag_or_explicit_metadata(base_image, libc, expected):
    config = _libc_config(f"PYTHON_LIBC={libc}") if libc else _libc_config()
    assert _target_options(
        "linux", "amd64", "3.11", base_image, config,
        base_image_tag_verified=True,
    )[1] == expected


@pytest.mark.parametrize(
    ("base_image", "env", "message"),
    [
        (f"python@{DIGEST}", (), "unverified"),
        (f"python:3.11-slim@{DIGEST}", (), "unverified"),
        (f"python:3.11-alpine@{DIGEST}", (), "unverified"),
        (f"python@{DIGEST}", ("PYTHON_LIBC=musl-1.1",), "Unsupported PYTHON_LIBC"),
        (f"python@{DIGEST}", ("PYTHON_LIBC=",), "Unsupported PYTHON_LIBC"),
        ("python:3.11-alpine", ("PYTHON_LIBC=glibc-2.17",), "conflicts"),
        ("python:3.11-slim", ("PYTHON_LIBC=musl-1.2",), "conflicts"),
        ("python:3.11-slim", ("PYTHON_LIBC=glibc",), "Unsupported PYTHON_LIBC"),
    ],
)
def test_official_image_libc_fails_closed_when_ambiguous_or_inconsistent(
    base_image, env, message
):
    with pytest.raises(ValueError, match=message):
        _target_options(
            "linux", "amd64", "3.11", base_image, _libc_config(*env),
            base_image_tag_verified=True,
        )


def test_digest_only_official_image_without_config_fails_closed():
    with pytest.raises(ValueError, match="unverified"):
        _target_options("linux", "amd64", "3.11", f"python@{DIGEST}", None)


@pytest.mark.parametrize("base_image", ["python", "python:3.11-slim", "python:3.11-alpine"])
def test_unverified_local_image_name_cannot_establish_libc(base_image):
    with pytest.raises(ValueError, match="unverified"):
        target_wheel_platform("linux", "amd64", base_image, _libc_config())
    assert target_wheel_platform(
        "linux", "amd64", base_image, _libc_config("PYTHON_LIBC=musl-1.2")
    ) == "musllinux_1_2_x86_64"


@pytest.mark.parametrize("tag", ["3.11-slim", "3.11-alpine"])
def test_unverified_tag_plus_digest_rejects_before_pip(tmp_path, monkeypatch, tag):
    base_image = f"python:{tag}@{DIGEST}"
    assert parse_image_reference(base_image)[2] == DIGEST
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fixture-dep==1.0\n")

    def unexpected_pip(*args, **kwargs):
        pytest.fail("pip must not run without validated target libc")

    monkeypatch.setattr(subprocess, "Popen", unexpected_pip)
    with pytest.raises(ValueError, match="unverified"):
        install_requirements(
            requirements, tmp_path / "staged", tmp_path,
            "linux", "amd64", "3.11", base_image, BASE_CONFIG,
        )


@pytest.mark.parametrize("base_image", ["python:3.11-slim", f"python:3.11-slim@{DIGEST}"])
@pytest.mark.parametrize("verified", [False, True])
def test_target_python_abi_cannot_come_from_unverified_name(base_image, verified):
    if verified and "@" not in base_image:
        assert target_python_version(
            base_image, None, base_image_tag_verified=True
        ) == "3.11"
        return
    with pytest.raises(ValueError, match="Python ABI"):
        target_python_version(
            base_image, _libc_config(), base_image_tag_verified=verified
        )
    with pytest.raises(ValueError, match="CPython version"):
        target_python_version(
            base_image, {"config": {"Env": ["PYTHON_IMPLEMENTATION=CPython"]}},
            base_image_tag_verified=verified,
        )
    assert target_python_version(
        base_image, _libc_config("PYTHON_IMPLEMENTATION=CPython"),
        base_image_tag_verified=verified,
    ) == "3.11"


@pytest.mark.parametrize(
    ("missing", "message"),
    [(None, None), ("PYTHON_LIBC", "unverified"), ("PYTHON_VERSION", "CPython version"),
     ("PYTHON_IMPLEMENTATION", "Python ABI")],
)
def test_digest_virtualenv_staging_requires_explicit_target_metadata(
    tmp_path, monkeypatch, missing, message
):
    context = tmp_path / "context"
    site_packages = context / "venv" / "lib" / "python3.11" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "fixture.py").write_text("x = 1\n")
    env = ["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.8",
           "PYTHON_LIBC=musl-1.2"]
    config = {
        **BASE_CONFIG,
        "config": {
            "Env": [entry for entry in env if missing is None or not entry.startswith(missing + "=")]
        },
    }
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_deps=True,
        base_image=f"python:3.11-slim@{DIGEST}",
    )
    layers = tmp_path / "layers"
    layers.mkdir()
    if missing is not None:
        with pytest.raises(ValueError, match=message):
            builder._create_deps_layer(layers, base_config=config)
    else:
        layer = builder._create_deps_layer(layers, base_config=config)
        with tarfile.open(layer.tar_path) as archive:
            assert "app/.python/site-packages/fixture.py" in archive.getnames()


@pytest.mark.parametrize(
    "base_image",
    [
        f"python@{DIGEST}",
        f"docker.io/library/python@{DIGEST}",
        f"index.docker.io/library/python@{DIGEST}",
        f"python:3.11-slim@{DIGEST}",
        "python:3.11-slim",
    ],
)
def test_digest_only_source_name_with_musl_metadata_stages_musl_wheels_only(
    tmp_path, base_image
):
    # Registry references and OCI layout `org.opencontainers.image.base.name`
    # annotations reach install_requirements as the same base image string.
    context = tmp_path / "context"
    context.mkdir()
    requirements = context / "requirements.txt"
    requirements.write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    config = _libc_config("PYTHON_LIBC=musl-1.2")
    _write_wheel(context / "wheelhouse", tag="cp311-cp311-manylinux2014_x86_64")
    with pytest.raises(RuntimeError, match="Only wheels compatible with"):
        install_requirements(
            requirements, tmp_path / "rejected", context,
            "linux", "amd64", "3.11", base_image, config,
        )

    _write_wheel(context / "wheelhouse", tag="cp311-cp311-musllinux_1_2_x86_64")
    destination = tmp_path / "staged"
    install_requirements(
        requirements, destination, context,
        "linux", "amd64", "3.11", base_image, config,
    )
    wheel = (destination / "fixture_dep-1.0.dist-info" / "WHEEL").read_text()
    assert "Tag: cp311-cp311-musllinux_1_2_x86_64" in wheel


def test_requirements_install_rejects_incompatible_cpython_wheel(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    _write_wheel(
        context / "wheelhouse",
        tag="cp312-cp312-manylinux2014_x86_64",
    )
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    with pytest.raises(RuntimeError, match="Only wheels compatible with"):
        builder._create_deps_layer(
            tmp_path / "layers", base_config=BASE_CONFIG
        )


def test_requirements_includes_are_bounded_context_local_and_cannot_override_target(
    tmp_path,
):
    context = tmp_path / "context"
    context.mkdir()
    requirements = context / "requirements.txt"
    nested = context / "nested.txt"
    requirements.write_text("-r nested.txt\n")
    nested.write_bytes(b"#" * MAX_REQUIREMENTS_BYTES)
    with pytest.raises(ValueError, match="combined"):
        validate_requirements(requirements, context)

    requirements.write_text("-r ../outside.txt\n")
    (tmp_path / "outside.txt").write_text("package==1.0\n")
    with pytest.raises(ValueError, match="inside the build context"):
        validate_requirements(requirements, context)

    requirements.write_text("--no-binary :all:\n")
    with pytest.raises(ValueError, match="cannot override target install option"):
        validate_requirements(requirements, context)


def test_requirements_install_terminates_when_staging_limit_is_exceeded(
    tmp_path, monkeypatch
):
    import pycontainer.dependencies as dependencies

    context = tmp_path / "context"
    context.mkdir()
    requirements = context / "requirements.txt"
    requirements.write_text("fixture-dep==1.0\n")
    destination = tmp_path / "stage" / "site-packages"
    destination.parent.mkdir()
    process_state = {"terminated": False}

    class Process:
        def wait(self, timeout=None):
            if process_state["terminated"]:
                return -15
            raise subprocess.TimeoutExpired("pip", timeout)

        def terminate(self):
            process_state["terminated"] = True

        def kill(self):
            process_state["terminated"] = True

    def start_process(*args, **kwargs):
        (destination.parent / "oversized-wheel.whl").write_bytes(b"too large")
        return Process()

    monkeypatch.setattr(dependencies.subprocess, "Popen", start_process)
    monkeypatch.setattr(dependencies, "MAX_STAGED_BYTES", 1)

    with pytest.raises(ValueError, match="Staged dependencies exceed"):
        install_requirements(
            requirements,
            destination,
            context,
            "linux",
            "amd64",
            "3.11",
            "python:3.11-slim",
            BASE_CONFIG,
            base_image_tag_verified=True,
        )
    assert process_state["terminated"]


def test_requirements_install_tolerates_files_pip_moves_during_limit_check(
    tmp_path, monkeypatch
):
    import pycontainer.dependencies as dependencies

    context = tmp_path / "context"
    context.mkdir()
    requirements = context / "requirements.txt"
    requirements.write_text("fixture-dep==1.0\n")
    destination = tmp_path / "stage" / "site-packages"
    destination.parent.mkdir()
    waits = []
    moving = destination.parent / "pip-target-x" / "lib" / "moved-by-pip.py"
    moving.parent.mkdir(parents=True)
    moving.write_text("x = 1\n")
    real_stat = Path.stat
    moving_stats = []

    # Reproduces the observed window: the file passes the type checks, then pip
    # moves it before its size is read.
    def stat_then_move(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == moving:
            moving_stats.append(True)
            if len(moving_stats) == 2:
                self.unlink()
        return result

    class Process:
        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("pip", timeout)
            return 0

    monkeypatch.setattr(dependencies.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(Path, "stat", stat_then_move)

    install_requirements(
        requirements,
        destination,
        context,
        "linux",
        "amd64",
        "3.11",
        "python:3.11-slim",
        BASE_CONFIG,
        base_image_tag_verified=True,
    )
    assert len(waits) == 2


def test_include_deps_without_dependency_source_fails_explicitly(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)

    with pytest.raises(FileNotFoundError, match="no supported virtualenv"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)

def _write_named_wheel(wheelhouse: Path, name: str, requires=(), extras=()) -> None:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    module = name.replace("-", "_")
    dist_info = f"{module}-1.0.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
    metadata += "".join(f"Provides-Extra: {extra}\n" for extra in extras)
    metadata += "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
    files = {
        f"{module}/__init__.py": b"",
        f"{dist_info}/METADATA": (metadata + "\n").encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode(),
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode()
        writer.writerow((path, f"sha256={digest.rstrip('=')}", len(content)))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    files[f"{dist_info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(wheelhouse / f"{module}-1.0-py3-none-any.whl", "w") as wheel:
        for path, content in files.items():
            wheel.writestr(path, content)


def _install_marker_fixture(tmp_path, requirements, arch, python_version, full_version):
    context = tmp_path / "context"
    requirements_file = context / "requirements.txt"
    requirements_file.write_text(
        "--no-index\n--find-links ./wheelhouse\n" + "".join(f"{line}\n" for line in requirements)
    )
    destination = tmp_path / "stage" / "site-packages"
    install_requirements(
        requirements_file,
        destination,
        context,
        "linux",
        arch,
        python_version,
        f"python:{python_version}-slim",
        {"config": {"Env": [f"PYTHON_VERSION={full_version}"]}},
        base_image_tag_verified=True,
    )
    return {name for name, _ in staged_packages(destination)}


@pytest.mark.parametrize(
    ("arch", "expected"),
    [
        ("amd64", {"marker-amd", "marker-root-amd"}),
        ("arm64", {"marker-arm", "marker-feat", "marker-root-arm"}),
    ],
)
def test_requirements_install_evaluates_root_and_transitive_markers_for_target(
    tmp_path, arch, expected
):
    wheelhouse = tmp_path / "context" / "wheelhouse"
    # A context-local pip must not replace the builder's pip.
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "pip.py").write_text("raise SystemExit('shadowed pip')\n")
    _write_named_wheel(
        wheelhouse,
        "marker-root",
        requires=[
            "marker-plain",
            'marker-arm; platform_machine == "aarch64"',
            'marker-amd; platform_machine == "x86_64"',
            'marker-win; sys_platform == "win32"',
            'marker-oldpy; python_version < "3.10"',
            'marker-linux; platform_system == "Linux" and os_name == "posix"',
            'marker-feat; extra == "feat" and platform_machine == "aarch64"',
        ],
        extras=["feat"],
    )
    for name in (
        "marker-plain", "marker-arm", "marker-amd", "marker-win", "marker-oldpy",
        "marker-linux", "marker-feat", "marker-root-arm", "marker-root-amd",
    ):
        _write_named_wheel(wheelhouse, name)

    installed = _install_marker_fixture(
        tmp_path,
        [
            "marker-root[feat]",
            'marker-root-arm; platform_machine == "aarch64"',
            'marker-root-amd; platform_machine == "x86_64"',
        ],
        arch,
        "3.9",
        "3.9.18",
    )

    assert installed == {
        "marker-root", "marker-plain", "marker-oldpy", "marker-linux", *expected
    }


def test_requirements_install_keeps_markers_that_match_host_and_target(tmp_path):
    wheelhouse = tmp_path / "context" / "wheelhouse"
    _write_named_wheel(
        wheelhouse,
        "marker-root",
        requires=[
            "marker-plain",
            'marker-cpython; implementation_name == "cpython"',
            'marker-full; python_full_version >= "3.11.0"',
        ],
    )
    for name in ("marker-plain", "marker-cpython", "marker-full"):
        _write_named_wheel(wheelhouse, name)

    installed = _install_marker_fixture(
        tmp_path, ["marker-root"], "amd64", "3.11", "3.11.8"
    )

    assert installed == {"marker-root", "marker-plain", "marker-cpython", "marker-full"}


def test_requirements_install_without_markers_needs_no_full_python_version(tmp_path):
    wheelhouse = tmp_path / "context" / "wheelhouse"
    _write_named_wheel(wheelhouse, "marker-root", requires=["marker-plain"])
    _write_named_wheel(wheelhouse, "marker-plain")

    installed = _install_marker_fixture(tmp_path, ["marker-root"], "arm64", "3.11", "3.11")

    assert installed == {"marker-root", "marker-plain"}


@pytest.mark.parametrize(
    ("requires", "requirement", "full_version", "variable"),
    [
        (['marker-kernel; platform_release >= "5"'], "marker-root", "3.11.8", "platform_release"),
        ([], 'marker-root; python_full_version >= "3.11.1"', "3.11", "python_full_version"),
    ],
)
def test_requirements_install_fails_closed_for_unknown_target_marker_values(
    tmp_path, requires, requirement, full_version, variable
):
    wheelhouse = tmp_path / "context" / "wheelhouse"
    _write_named_wheel(wheelhouse, "marker-root", requires=requires)
    _write_named_wheel(wheelhouse, "marker-kernel")

    with pytest.raises(ValueError, match=f"refusing to stage.*{variable}"):
        _install_marker_fixture(tmp_path, [requirement], "arm64", "3.11", full_version)

def _target_config(arch, python_version="3.11.8"):
    return {
        **BASE_CONFIG,
        "architecture": arch,
        "config": {"Env": [f"PYTHON_VERSION={python_version}"], "WorkingDir": "/"},
    }


def _write_venv(context, distributions, extra_dirs=()):
    site_packages = context / ".venv" / "lib" / "python3.11" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    for name, version, headers in distributions:
        module = name.replace("-", "_")
        dist_info = site_packages / f"{module}-{version}.dist-info"
        dist_info.mkdir()
        (site_packages / f"{module.lower()}.py").write_text("value = 1\n")
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"{header}\n" for header in headers)
            + "\n"
        )
        (dist_info / "WHEEL").write_text(
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
    for directory in extra_dirs:
        (site_packages / directory).mkdir()
    return site_packages


def _stage_venv(tmp_path, monkeypatch, distributions, arch="arm64", config=None):
    context = tmp_path / "context"
    _write_venv(context, distributions)
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    layers = tmp_path / "layers"
    layers.mkdir(exist_ok=True)
    builder._create_deps_layer(
        layers, base_config=config or _target_config(arch), os_name="linux", arch=arch
    )
    return {name for name, _ in builder._staged_packages}


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_virtualenv_rejects_missing_target_only_dependency(tmp_path, monkeypatch, arch):
    root = ("app-lib", "1.0", ['Requires-Dist: linux-only; sys_platform == "linux"'])

    with pytest.raises(ValueError, match=f"linux/{arch}.*'linux-only.*not installed"):
        _stage_venv(tmp_path, monkeypatch, [root], arch=arch)


@pytest.mark.parametrize(("arch", "error"), [("amd64", None), ("arm64", "arm-only")])
def test_virtualenv_closure_evaluates_markers_for_each_target(
    tmp_path, monkeypatch, arch, error
):
    distributions = [
        (
            "app-lib",
            "1.0",
            [
                "Requires-Python: >=3.8",
                'Requires-Dist: arm-only; platform_machine == "aarch64"',
                'Requires-Dist: amd-only; platform_machine == "x86_64"',
                'Requires-Dist: win-only; sys_platform == "win32"',
                'Requires-Dist: old-only; python_version < "3.10"',
                'Requires-Dist: linux-dep>=1.0; platform_system == "Linux" and os_name == "posix"',
            ],
        ),
        ("amd-only", "1.0", []),
        ("Linux_Dep", "1.2", []),
    ]

    if error:
        with pytest.raises(ValueError, match=f"{error}.*not installed"):
            _stage_venv(tmp_path, monkeypatch, distributions, arch=arch)
    else:
        assert _stage_venv(tmp_path, monkeypatch, distributions, arch=arch) == {
            "app-lib", "amd-only", "Linux_Dep"
        }


@pytest.mark.parametrize(
    ("distributions", "error"),
    [
        (
            [
                ("app-lib", "1.0", ["Requires-Dist: middle"]),
                ("middle", "1.0", ['Requires-Dist: leaf; sys_platform == "linux"']),
            ],
            "middle 1.0 requires 'leaf",
        ),
        (
            [("app-lib", "1.0", ["Requires-Dist: middle>=2"]), ("middle", "1.0", [])],
            "middle 1.0 is installed",
        ),
        (
            [
                ("app-lib", "1.0", ["Requires-Dist: Middle[Fast_Mode]"]),
                (
                    "middle",
                    "1.0",
                    [
                        "Provides-Extra: fast-mode",
                        'Requires-Dist: speedup; extra == "fast-mode" and sys_platform == "linux"',
                    ],
                ),
            ],
            r"middle 1.0 \[fast-mode\] requires 'speedup",
        ),
        (
            [
                (
                    "app-lib",
                    "1.0",
                    [
                        "Provides-Extra: linux",
                        'Requires-Dist: linux-extra; extra == "linux" and sys_platform == "linux"',
                    ],
                )
            ],
            r"Cannot determine whether extra \[linux\] of app-lib was selected",
        ),
        (
            [
                (
                    "app-lib",
                    "1.0",
                    [
                        "Provides-Extra: linux",
                        'Requires-Dist: present; extra == "linux"',
                        'Requires-Dist: linux-extra; extra == "linux" and sys_platform == "linux"',
                    ],
                ),
                ("present", "1.0", []),
            ],
            r"Cannot determine whether extra \[linux\]",
        ),
    ],
)
def test_virtualenv_rejects_incomplete_transitive_versions_and_extras(
    tmp_path, monkeypatch, distributions, error
):
    with pytest.raises(ValueError, match=error):
        _stage_venv(tmp_path, monkeypatch, distributions)


def test_virtualenv_accepts_complete_closure_and_unselected_extras(tmp_path, monkeypatch):
    distributions = [
        (
            "App_Lib",
            "1.0",
            [
                "Requires-Dist: Middle[Fast_Mode]>=1.0",
                "Provides-Extra: socks",
                'Requires-Dist: pysocks; extra == "socks" and sys_platform == "win32"',
                "Provides-Extra: test",
                'Requires-Dist: pytest; extra == "test" and sys_platform == "win32"',
            ],
        ),
        (
            "middle",
            "1.1rc1",
            [
                "Provides-Extra: fast-mode",
                'Requires-Dist: speedup; extra == "fast-mode" and sys_platform == "linux"',
            ],
        ),
        ("speedup", "1.0", []),
    ]

    assert _stage_venv(tmp_path, monkeypatch, distributions) == {
        "App_Lib", "middle", "speedup"
    }


@pytest.mark.parametrize(
    ("headers", "error", "python_version"),
    [
        (["Requires-Dist: bad requirement ;;"], "malformed Requires-Dist", "3.11.8"),
        (["Version: 2.0"], "exactly one Version", "3.11.8"),
        (['Requires-Dist: kernel; platform_release >= "5"'], "platform_release without", "3.11.8"),
        (['Requires-Dist: full; python_full_version >= "3.11.1"'], "python_full_version without", "3.11"),
        (["Requires-Python: >=3.12"], r"requires Python >=3\.12", "3.11.8"),
        (["Requires-Python: ==3.11.5"], r"requires Python ==3\.11\.5.*3\.11\.8", "3.11.8"),
        (["Requires-Python: >=3.11.2"], "depends on the target patch", "3.11"),
        (["Requires-Python: <3.11.0.post1"], "depends on the target patch", "3.11"),
        (["Requires-Python: ~=3.11.post1"], "depends on the target patch", "3.11"),
        (["Requires-Dist: direct @ https://example.invalid/direct.whl"], "direct URL", "3.11.8"),
    ],
)
def test_virtualenv_rejects_malformed_or_unverifiable_metadata(
    tmp_path, monkeypatch, headers, error, python_version
):
    with pytest.raises(ValueError, match=error):
        _stage_venv(
            tmp_path,
            monkeypatch,
            [("app-lib", "1.0", headers), ("direct", "1.0", [])],
            config=_target_config("arm64", python_version),
        )


def test_virtualenv_rejects_unreadable_package_metadata(tmp_path, monkeypatch):
    site_packages = _write_venv(tmp_path / "context", [("good", "1.0", [])])
    (site_packages / "good-1.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: other\nVersion: 1.0\n\n"
    )
    builder = _builder(tmp_path, monkeypatch, context=tmp_path / "context", include_deps=True)
    with pytest.raises(ValueError, match="does not match its metadata"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)

    (site_packages / "good-1.0.dist-info" / "METADATA").unlink()
    with pytest.raises(ValueError, match="missing METADATA"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)

    (site_packages / "good-1.0.dist-info" / "METADATA").write_bytes(
        b"Name: good\nVersion: 1.0\nSummary: \xff\n\n"
    )
    with pytest.raises(ValueError, match="not valid UTF-8"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)

    shutil.rmtree(site_packages / "good-1.0.dist-info")
    (site_packages / "legacy-1.0.egg-info").mkdir()
    (site_packages / "legacy-1.0.egg-info" / "PKG-INFO").write_text("Name: legacy\n")
    with pytest.raises(ValueError, match="egg-info"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)

    shutil.rmtree(site_packages / "legacy-1.0.egg-info")
    (site_packages / "develop.egg-link").write_text("/src/develop\n.\n")
    with pytest.raises(ValueError, match="egg distributions: develop.egg-link"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)


def test_virtualenv_closure_fails_closed_without_target_marker_support(
    tmp_path, monkeypatch
):
    broken = tmp_path / "broken_closure.py"
    broken.write_text("import sys\nsys.exit(3)\n")
    monkeypatch.setattr(dependencies, "_VENV_CLOSURE_SCRIPT", broken)

    with pytest.raises(RuntimeError, match="refusing to stage an unverified"):
        _stage_venv(tmp_path, monkeypatch, [("app-lib", "1.0", [])])


def _write_base_venv(context, dists, pyvenv="version = 3.11.7\n", lib=("Lib",)):
    site_packages = context.joinpath(".venv", *lib, "site-packages")
    site_packages.mkdir(parents=True, exist_ok=True)
    (context / ".venv" / "pyvenv.cfg").write_text(pyvenv)
    for name, headers in dists.items():
        metadata = site_packages / f"{name.replace('-', '_')}-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
            + "".join(f"{header}\n" for header in headers)
            + "\n"
        )
        (metadata / "WHEEL").write_text(
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )


def _stage_base_venv(tmp_path, monkeypatch, context, arch="amd64"):
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    layer_dir = tmp_path / f"layers-{arch}"
    layer_dir.mkdir(exist_ok=True)
    return builder._create_deps_layer(
        layer_dir,
        base_config={**BASE_CONFIG, "architecture": arch},
        os_name="linux",
        arch=arch,
    )


def test_virtualenv_transitive_markers_are_verified_for_each_target(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(
        context,
        {"venv-root": ['Requires-Dist: venv-arm-helper; platform_machine == "aarch64"']},
    )

    assert _stage_base_venv(tmp_path, monkeypatch, context, "amd64") is not None
    with pytest.raises(ValueError, match="refusing to stage.*venv-arm-helper"):
        _stage_base_venv(tmp_path, monkeypatch, context, "arm64")

    _write_base_venv(context, {"venv-arm-helper": []})
    assert _stage_base_venv(tmp_path, monkeypatch, context, "arm64") is not None


def test_virtualenv_root_markers_are_verified_for_the_target(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []})
    (context / "requirements.txt").write_text(
        "venv-root\n"
        "venv-windows-only; sys_platform == 'win32'\n"
        "venv-linux-only; sys_platform == 'linux'  # comment\n"
    )

    with pytest.raises(ValueError, match="requirements file requires .*venv-linux-only"):
        _stage_base_venv(tmp_path, monkeypatch, context)

    _write_base_venv(context, {"venv-linux-only": []})
    assert _stage_base_venv(tmp_path, monkeypatch, context) is not None


def test_virtualenv_project_dependency_markers_are_verified(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []})
    (context / "pyproject.toml").write_text(
        '[project]\nname = "app"\n'
        "dependencies = [\"venv-linux-only; sys_platform == 'linux'\"]\n"
    )

    with pytest.raises(ValueError, match="dependencies requires .*venv-linux-only"):
        _stage_base_venv(tmp_path, monkeypatch, context)


@pytest.mark.parametrize(
    "headers, root, error",
    [
        (
            [
                "Provides-Extra: speed",
                'Requires-Dist: venv-fast; extra == "speed"',
                'Requires-Dist: venv-linux-speed; sys_platform == "linux" and extra == "speed"',
            ],
            "",
            None,
        ),
        (
            [
                "Provides-Extra: broker",
                'Requires-Dist: venv-broker; platform_system == "Windows" and extra == "broker"',
                'Requires-Dist: venv-broker; platform_system == "Darwin" and extra == "broker"',
                'Requires-Dist: venv-broker; platform_system == "Linux" and extra == "broker"',
            ],
            "",
            None,
        ),
        (
            [
                "Provides-Extra: gpu",
                'Requires-Dist: venv-gpu; platform_machine == "x86_64" and extra == "gpu"',
            ],
            "",
            "Cannot determine whether extra [gpu]",
        ),
        (
            [
                "Provides-Extra: speed",
                'Requires-Dist: venv-fast; extra == "speed"',
                'Requires-Dist: venv-linux-speed; sys_platform == "linux" and extra == "speed"',
            ],
            "venv-root[speed]\n",
            "venv-root 1.0 [speed] requires 'venv-fast",
        ),
    ],
)
def test_virtualenv_extras_fail_only_when_their_request_cannot_be_ruled_out(
    tmp_path, monkeypatch, headers, root, error
):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": headers})
    (context / "requirements.txt").write_text(root)

    if error is None:
        assert _stage_base_venv(tmp_path, monkeypatch, context) is not None
    else:
        with pytest.raises(ValueError, match=re.escape(error)):
            _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_unknown_target_marker_values_fail_closed_when_missing(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _write_base_venv(
        context, {"venv-root": ['Requires-Dist: venv-kernel; platform_release >= "5"']}
    )

    with pytest.raises(ValueError, match="venv-kernel.*platform_release without a known target"):
        _stage_base_venv(tmp_path, monkeypatch, context)

    _write_base_venv(context, {"venv-kernel": []})
    with pytest.raises(ValueError, match="venv-kernel.*platform_release without a known target"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_egg_info_requires_sections_are_rejected(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {})
    egg_info = context / ".venv" / "Lib" / "site-packages" / "venv_egg.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text("Metadata-Version: 1.1\nName: venv-egg\nVersion: 1.0\n")
    (egg_info / "requires.txt").write_text('[:sys_platform == "linux"]\nvenv-linux-only\n')

    with pytest.raises(ValueError, match="egg distributions: venv_egg.egg-info"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_unparsed_root_markers_fail_closed(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": ['Requires-Dist: venv-any; python_version >= "3"']})
    (context / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\nvenv-linux-only = { version = "*", platform = "linux" }\n'
    )

    with pytest.raises(ValueError, match=r"\[tool.poetry\] may declare environment markers"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_uv_virtualenv_version_info_must_match_image(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []}, pyvenv="implementation = CPython\nversion_info = 3.14\n")

    with pytest.raises(ValueError, match="targets Python 3.14"):
        _stage_base_venv(tmp_path, monkeypatch, context)

def test_virtualenv_roots_include_files_seen_first_as_constraints(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []})
    (context / "requirements.txt").write_text("-r base.txt\n-c constraints.txt\nvenv-root\n")
    (context / "constraints.txt").write_text("-r base.txt\n")
    (context / "base.txt").write_text("venv-linux-only; sys_platform != 'win32'\n")

    with pytest.raises(ValueError, match="requirements file requires .*venv-linux-only"):
        _stage_base_venv(tmp_path, monkeypatch, context)


@pytest.mark.parametrize(
    "files",
    [
        {"Pipfile": "[packages]\nvenv-linux-only = {version = \"*\", markers = \"sys_platform != 'win32'\"}\n"},
        {
            "pyproject.toml": (
                '[project]\nname = "app"\ndynamic = ["dependencies"]\n'
                '[tool.setuptools.dynamic]\ndependencies = {file = ["deps.txt"]}\n'
            ),
            "deps.txt": "venv-linux-only; sys_platform != 'win32'\n",
        },
        {"pyproject.toml": '[project]\nname = "app"\ndynamic = ["dependencies"]\n'},
    ],
)
def test_virtualenv_unparsed_dependency_sources_fail_closed(tmp_path, monkeypatch, files):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": ['Requires-Dist: venv-any; python_version >= "3"']})
    for name, text in files.items():
        (context / name).write_text(text)

    with pytest.raises(ValueError, match="may declare environment markers|cannot be read"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_reads_roots_from_install_option_and_utf16_requirements(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []})
    (context / "requirements.txt").write_text(
        "--only-binary=:all:\nvenv-root\nvenv-linux-only; sys_platform == 'linux'\n",
        encoding="utf-16",
    )

    with pytest.raises(ValueError, match="requirements file requires .*venv-linux-only"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_definite_extras_match_unnormalized_marker_spellings(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-egg": [
        "Provides-Extra: Speed_Ups",
        'Requires-Dist: venv-fast; extra == "Speed_Ups"',
    ]})
    (context / "requirements.txt").write_text("venv-egg[speed-ups]\n")

    with pytest.raises(ValueError, match=r"venv-egg 1.0 \[speed-ups\] requires .*venv-fast"):
        _stage_base_venv(tmp_path, monkeypatch, context)


_REPRODUCER_MARKER = 'extra != "" and extra in "other,Speed_Ups"'
_LEGACY_PACKAGING = (
    "import pip._vendor.packaging.markers as _markers\n"
    "_markers._normalize = lambda *values, key: values\n"
)


def _write_extra_venv(context, marker, requested="speed-ups", installed=False):
    dists = {"marker-root": [
        "Provides-Extra: Speed_Ups",
        f"Requires-Dist: missing-child; {marker}",
    ]}
    if installed:
        dists["missing-child"] = []
    _write_base_venv(context, dists)
    (context / "requirements.txt").write_text(f"marker-root[{requested}]\n")


def _simulate_legacy_packaging(tmp_path, monkeypatch):
    # Simulate packaging 21.3, which compares extra strings literally.
    script = tmp_path / "_venv_closure.py"
    script.write_text(
        _LEGACY_PACKAGING + dependencies._VENV_CLOSURE_SCRIPT.read_text(encoding="utf-8")
    )
    monkeypatch.setattr(dependencies, "_VENV_CLOSURE_SCRIPT", script)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_virtualenv_normalizes_extra_list_operands(
    tmp_path, monkeypatch, arch, legacy
):
    if legacy:
        _simulate_legacy_packaging(tmp_path, monkeypatch)
    context = tmp_path / "context"
    _write_extra_venv(context, _REPRODUCER_MARKER)
    with pytest.raises(
        ValueError, match=r"marker-root 1.0 \[speed-ups\] requires .*missing-child.*not installed"
    ):
        _stage_base_venv(tmp_path, monkeypatch, context, arch)

    shutil.rmtree(context)
    _write_extra_venv(context, _REPRODUCER_MARKER, installed=True)
    assert _stage_base_venv(tmp_path, monkeypatch, context, arch) is not None


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    ("marker", "requested", "missing"),
    [
        ('extra == "Speed_Ups"', "speed-ups", True),
        ('"Speed_Ups" == extra', "speed-ups", True),
        ('extra != "" and extra != "Speed_Ups"', "speed-ups", False),
        ('extra != "" and extra in "Other, Fast"', "speed-ups", False),
        ('extra != "" and extra not in "other,Speed_Ups"', "speed-ups", False),
        ('extra != "" and extra not in "other,fast"', "speed-ups", True),
        # PEP 508 `in` is a Python substring test: "" is in every string.
        ('extra in "other,fast"', "speed-ups", True),
        ('extra not in "other,Speed_Ups"', "speed-ups", False),
        ('extra != "" and extra in "Speed_Ups"', "ups", True),
        ('"Speed" in extra', "speed-ups", True),
        ('"other" in extra', "speed-ups", False),
        ('"Fast" not in extra', "speed-ups", True),
        ('extra in "other,Speed_Ups" and sys_platform == "linux"', "speed-ups", True),
        ('extra in "other,Speed_Ups" and sys_platform == "win32"', "speed-ups", False),
        ('sys_platform == "win32" or "Speed_Ups" == extra', "speed-ups", True),
    ],
)
def test_virtualenv_extra_operators_are_verified(
    tmp_path, monkeypatch, marker, requested, missing, legacy
):
    if legacy:
        _simulate_legacy_packaging(tmp_path, monkeypatch)
    context = tmp_path / "context"
    _write_extra_venv(context, marker, requested=requested)
    if requested != "speed-ups":
        metadata = context / ".venv" / "Lib" / "site-packages" / "marker_root-1.0.dist-info" / "METADATA"
        metadata.write_text(metadata.read_text().replace("Speed_Ups\n", f"{requested}\n", 1))
    if missing:
        with pytest.raises(ValueError, match="missing-child.*not installed"):
            _stage_base_venv(tmp_path, monkeypatch, context)
    else:
        assert _stage_base_venv(tmp_path, monkeypatch, context) is not None


@pytest.mark.parametrize(
    "marker", ['extra >= "speed"', 'extra === "speed"', 'extra ~= "1.0"']
)
def test_virtualenv_unsupported_extra_comparisons_fail_closed(
    tmp_path, monkeypatch, marker
):
    context = tmp_path / "context"
    _write_extra_venv(context, marker, installed=True)
    with pytest.raises(ValueError, match="only extra ==, !=, in and not in comparisons"):
        _stage_base_venv(tmp_path, monkeypatch, context)


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        # Substring matches differ between pip versions, so they cannot prove
        # that an unselected extra was never requested.
        ('extra != "" and extra in "fast,turbo"', r"Cannot determine whether extra \[fast\]"),
        ('extra in "other,Speed_Ups"', "missing-child.*not installed"),
    ],
)
def test_virtualenv_substring_extras_cannot_prove_unrequested(
    tmp_path, monkeypatch, marker, message
):
    context = tmp_path / "context"
    _write_base_venv(context, {"marker-root": [
        "Provides-Extra: Fast",
        f"Requires-Dist: missing-child; {marker}",
    ]})
    (context / "requirements.txt").write_text("marker-root\n")
    with pytest.raises(ValueError, match=message):
        _stage_base_venv(tmp_path, monkeypatch, context)


_INJECT_INVALID_VERSION = (
    "import pip._vendor.packaging.markers as _markers\n"
    "from pip._vendor.packaging.version import InvalidVersion\n"
    "_eval_op = _markers._eval_op\n"
    "def _raising_eval_op(lhs, op, rhs, *args, **kwargs):\n"
    "    if '1-0-0' in (lhs, rhs):\n"
    "        raise InvalidVersion('injected')\n"
    "    return _eval_op(lhs, op, rhs, *args, **kwargs)\n"
    "_markers._eval_op = _raising_eval_op\n"
)


def test_virtualenv_version_like_extra_evaluation_errors_fail_closed(
    tmp_path, monkeypatch
):
    # packaging 22+ raises InvalidVersion comparing "1-0-0" with "==1-0";
    # inject it so every vendored packaging version takes the error path.
    script = tmp_path / "_venv_closure.py"
    script.write_text(
        _INJECT_INVALID_VERSION
        + dependencies._VENV_CLOSURE_SCRIPT.read_text(encoding="utf-8")
    )
    monkeypatch.setattr(dependencies, "_VENV_CLOSURE_SCRIPT", script)
    context = tmp_path / "context"
    _write_base_venv(context, {"marker-root": [
        "Provides-Extra: 1.0.0",
        'Requires-Dist: missing-child; extra == "1.0"',
    ]})
    (context / "requirements.txt").write_text("marker-root[1.0.0]\n")
    with pytest.raises(ValueError, match=r"marker-root 1.0 \[1-0-0\] requires .*missing-child.*not installed"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_substring_extras_without_provides_extra_fail_closed(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _write_base_venv(context, {"marker-root": [
        'Requires-Dist: missing-child; extra != "" and extra in "fast"',
    ]})
    (context / "requirements.txt").write_text("marker-root\n")
    with pytest.raises(ValueError, match="Cannot determine which extras of marker-root"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_installed_versions_must_satisfy_target_constraints(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []})
    (context / "requirements.txt").write_text("venv-root\n-c constraints.txt\n")
    constraints = context / "constraints.txt"
    constraints.write_text(
        "venv-root<1; sys_platform == 'linux'\n"
        "venv-root>=9; sys_platform == 'win32'\n"
        "venv-absent==2\n"
    )
    with pytest.raises(
        ValueError, match=r"requirements constraints requires .venv-root<1.*1.0 is installed"
    ):
        _stage_base_venv(tmp_path, monkeypatch, context)

    constraints.write_text("venv-root<2\nvenv-absent==2\n")
    assert _stage_base_venv(tmp_path, monkeypatch, context) is not None


@pytest.mark.parametrize(
    "header",
    ["", "Metadata-Version: 3.0\n", "Metadata-Version: 2.1\nMetadata-Version: 2.1\n"],
)
def test_virtualenv_requires_one_supported_metadata_version(tmp_path, monkeypatch, header):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []})
    metadata = context / ".venv" / "Lib" / "site-packages" / "venv_root-1.0.dist-info" / "METADATA"
    metadata.write_text(f"{header}Name: venv-root\nVersion: 1.0\n\n")

    with pytest.raises(ValueError, match="Metadata-Version"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_root_versions_and_extras_are_enforced(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": [
        "Provides-Extra: speed",
        'Requires-Dist: venv-fast; extra == "speed" and sys_platform == "linux"',
    ]})
    requirements = context / "requirements.txt"
    requirements.write_text("venv-root>=2\n")
    with pytest.raises(ValueError, match="requirements file requires .*venv-root>=2.*1.0 is installed"):
        _stage_base_venv(tmp_path, monkeypatch, context)

    requirements.write_text("venv-root[speed]\n")
    with pytest.raises(ValueError, match=r"venv-root 1.0 \[speed\] requires .*venv-fast"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_optional_roots_fail_only_when_selection_is_unknown(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []})
    pyproject = context / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "app"\n'
        '[project.optional-dependencies]\n'
        'speed = ["venv-root", "venv-linux; sys_platform == \'linux\'"]\n'
    )
    with pytest.raises(ValueError, match="Cannot determine whether .*speed.*venv-linux"):
        _stage_base_venv(tmp_path, monkeypatch, context)

    pyproject.write_text(
        '[project]\nname = "app"\n'
        '[project.optional-dependencies]\n'
        'speed = ["not-installed", "venv-linux; sys_platform == \'linux\'"]\n'
    )
    # Every plausible host would have installed not-installed, so speed was not selected.
    assert _stage_base_venv(tmp_path, monkeypatch, context) is not None

    pyproject.write_text(
        '[project]\nname = "app"\n'
        '[project.optional-dependencies]\n'
        'speed = ["not-installed; sys_platform == \'win32\'"]\n'
    )
    assert _stage_base_venv(tmp_path, monkeypatch, context) is not None


def test_virtualenv_unsupported_dependency_group_fails_closed(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []})
    (context / "pyproject.toml").write_text(
        '[dependency-groups]\ndev = [{include-group = "other"}]\n'
        'other = ["venv-linux; sys_platform == \'linux\'"]\n'
    )
    with pytest.raises(ValueError, match="dependency group 'dev'.*unsupported"):
        _stage_base_venv(tmp_path, monkeypatch, context)


def test_virtualenv_errors_do_not_disclose_direct_url_credentials(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(context, {"venv-root": []})
    (context / "requirements.txt").write_text(
        "venv-root @ https://user:secret@example.invalid/package.whl\n"
    )
    with pytest.raises(ValueError, match="direct URL dependency") as error:
        _stage_base_venv(tmp_path, monkeypatch, context)
    assert "secret" not in str(error.value)

@pytest.mark.parametrize("marker", ['extra == "speed"', 'sys_platform == "win32"'])
def test_virtualenv_unrecorded_extra_with_direct_url_is_unverifiable(
    tmp_path, monkeypatch, marker
):
    with pytest.raises(ValueError, match="direct URL dependency") as error:
        _stage_venv(tmp_path, monkeypatch, [
            ("app-lib", "1.0", [
                "Provides-Extra: speed",
                f"Requires-Dist: direct @ https://user:secret@example.invalid/package.whl ; {marker}",
            ]),
            ("direct", "1.0", []),
        ])
    assert "secret" not in str(error.value)

def test_virtualenv_rejects_unverifiable_direct_url_metadata(tmp_path, monkeypatch):
    site_packages = _write_venv(tmp_path / "context", [("good", "1.0", [])])
    (site_packages / "good-1.0.dist-info" / "direct_url.json").write_text(
        '{"url": "https://user:secret@example.invalid/good.whl"}'
    )
    builder = _builder(tmp_path, monkeypatch, context=tmp_path / "context", include_deps=True)
    with pytest.raises(ValueError, match="direct URL metadata") as error:
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)
    assert "secret" not in str(error.value)


def test_virtualenv_rejects_malformed_metadata_headers(tmp_path, monkeypatch):
    site_packages = _write_venv(tmp_path / "context", [("good", "1.0", [])])
    (site_packages / "good-1.0.dist-info" / "METADATA").write_text(
        "Name: good\nVersion: 1.0\nnot a header\nRequires-Dist: missing\n\n"
    )
    builder = _builder(tmp_path, monkeypatch, context=tmp_path / "context", include_deps=True)
    with pytest.raises(ValueError, match="malformed headers"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)


@pytest.mark.parametrize(
    ("lib", "home", "fails"),
    [
        (("lib", "python3.11"), "/usr/bin", False),
        (("lib", "python3.11"), "C:/msys64/mingw64/bin", True),
        (("lib", "python3.11"), None, True),
        (("Lib",), "C:\\Python311", True),
    ],
)
def test_virtualenv_posix_layout_rules_out_extras_that_skip_windows(
    tmp_path, monkeypatch, lib, home, fails
):
    context = tmp_path / "context"
    pyvenv = "version = 3.11.7\n" + (f"home = {home}\n" if home else "")
    _write_base_venv(
        context,
        {
            "venv-root": [
                "Provides-Extra: uvloop",
                'Requires-Dist: venv-uvloop; sys_platform != "win32" and extra == "uvloop"',
            ]
        },
        pyvenv=pyvenv,
        lib=lib,
    )
    (context / "requirements.txt").write_text("venv-root\n")

    if fails:
        with pytest.raises(ValueError, match=r"extra \[uvloop\] of venv-root"):
            _stage_base_venv(tmp_path, monkeypatch, context)
    else:
        assert _stage_base_venv(tmp_path, monkeypatch, context) is not None
