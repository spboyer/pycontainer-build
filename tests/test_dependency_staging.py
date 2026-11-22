import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

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


def _write_wheel(wheelhouse: Path, tag: str = "py3-none-any") -> Path:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    wheel_path = wheelhouse / f"fixture_dep-1.0-{tag}.whl"
    files = {
        "fixture_dep/__init__.py": (
            "def main():\n"
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
            "[console_scripts]\nfixture-dep = fixture_dep:main\n"
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


def test_requirements_install_stages_importable_target_wheel_and_sbom(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    _write_wheel(context / "wheelhouse")
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
        assert script.mode & 0o111
        assert script.mtime == script.uid == script.gid == 0
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


ARM_V7_CONFIG = {**BASE_CONFIG, "architecture": "arm", "variant": "v7"}


def _arm_v7_builder(tmp_path, monkeypatch, context):
    monkeypatch.setattr(
        ImageBuilder,
        "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], ARM_V7_CONFIG),
    )
    return ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / "output"),
            use_cache=False,
            include_deps=True,
            platform="linux/arm/v7",
        )
    )


def test_arm_v7_maps_to_armv7l_wheel_tags_and_requires_variant():
    verified = {"base_image_tag_verified": True, "variant": "v7"}
    assert _target_options("linux", "arm", "3.11", **verified)[1] == "manylinux2014_armv7l"
    assert _target_options(
        "linux", "arm", "3.11", "python:3.11-alpine", **verified
    )[1] == "musllinux_1_2_armv7l"
    custom_config = {"config": {"Env": ["PYTHON_LIBC=glibc-2.17"]}}
    assert _target_options(
        "linux", "arm", "3.11", "example/python:custom", custom_config, variant="v7"
    )[1] == "manylinux_2_17_armv7l"
    assert "--only-binary=:all:" in _target_options("linux", "arm", "3.11", **verified)
    for variant, target in ((None, "linux/arm;"), ("v6", "linux/arm/v6;")):
        with pytest.raises(ValueError, match=target):
            _target_options(
                "linux", "arm", "3.11", base_image_tag_verified=True, variant=variant
            )


def test_arm_v7_requirements_stage_only_armv7l_wheels(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    _write_wheel(context / "wheelhouse", tag="cp311-cp311-manylinux2014_armv7l")
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    builder = _arm_v7_builder(tmp_path, monkeypatch, context)
    (tmp_path / "layers").mkdir()

    layer = builder._create_deps_layer(
        tmp_path / "layers", base_config=ARM_V7_CONFIG, os_name="linux", arch="arm", variant="v7"
    )

    with tarfile.open(layer.tar_path) as archive:
        assert "app/.python/site-packages/fixture_dep/__init__.py" in archive.getnames()


def test_arm_v7_missing_wheel_fails_without_source_fallback(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    _write_wheel(context / "wheelhouse", tag="cp311-cp311-manylinux2014_x86_64")
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    builder = _arm_v7_builder(tmp_path, monkeypatch, context)

    with pytest.raises(RuntimeError, match=r"linux/arm/v7.*never used as a fallback.*armv7l"):
        builder._create_deps_layer(
            tmp_path / "layers", base_config=ARM_V7_CONFIG, os_name="linux", arch="arm", variant="v7"
        )


def test_dependency_layer_rejects_conflicting_base_config_variant(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "requirements.txt").write_text("fixture-dep==1.0\n")
    builder = _arm_v7_builder(tmp_path, monkeypatch, context)

    with pytest.raises(ValueError, match="does not match the requested target linux/arm/v7"):
        builder._create_deps_layer(
            tmp_path / "layers",
            base_config={**ARM_V7_CONFIG, "variant": "v6"},
            os_name="linux",
            arch="arm",
            variant="v7",
        )
