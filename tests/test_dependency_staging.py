import base64
import csv
import hashlib
import io
import json
import os
import re
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
    validate_requirements,
)


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
        "linux", "amd64", "3.11", "python:3.11-alpine"
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
    assert _target_options("linux", "arm64", "3.11")[1] == "manylinux2014_aarch64"

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


def _write_venv(context, dists, pyvenv="version = 3.11.7\n", lib=("Lib",)):
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


def _stage_venv(tmp_path, monkeypatch, context, arch="amd64"):
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
    _write_venv(
        context,
        {"venv-root": ['Requires-Dist: venv-arm-helper; platform_machine == "aarch64"']},
    )

    assert _stage_venv(tmp_path, monkeypatch, context, "amd64") is not None
    with pytest.raises(ValueError, match="refusing to stage.*venv-arm-helper"):
        _stage_venv(tmp_path, monkeypatch, context, "arm64")

    _write_venv(context, {"venv-arm-helper": []})
    assert _stage_venv(tmp_path, monkeypatch, context, "arm64") is not None


def test_virtualenv_root_markers_are_verified_for_the_target(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_venv(context, {"venv-root": []})
    (context / "requirements.txt").write_text(
        "venv-root\n"
        "venv-windows-only; sys_platform == 'win32'\n"
        "venv-linux-only; sys_platform == 'linux'  # comment\n"
    )

    with pytest.raises(ValueError, match="requirements file requires venv-linux-only"):
        _stage_venv(tmp_path, monkeypatch, context)

    _write_venv(context, {"venv-linux-only": []})
    assert _stage_venv(tmp_path, monkeypatch, context) is not None


def test_virtualenv_project_dependency_markers_are_verified(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_venv(context, {"venv-root": []})
    (context / "pyproject.toml").write_text(
        '[project]\nname = "app"\n'
        "dependencies = [\"venv-linux-only; sys_platform == 'linux'\"]\n"
    )

    with pytest.raises(ValueError, match="dependencies requires venv-linux-only"):
        _stage_venv(tmp_path, monkeypatch, context)


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
            "cannot tell whether extra 'gpu'",
        ),
        (
            [
                "Provides-Extra: speed",
                'Requires-Dist: venv-fast; extra == "speed"',
                'Requires-Dist: venv-linux-speed; sys_platform == "linux" and extra == "speed"',
            ],
            "venv-root[speed]\n",
            "venv-root requires venv-fast",
        ),
    ],
)
def test_virtualenv_extras_fail_only_when_their_request_cannot_be_ruled_out(
    tmp_path, monkeypatch, headers, root, error
):
    context = tmp_path / "context"
    _write_venv(context, {"venv-root": headers})
    (context / "requirements.txt").write_text(root)

    if error is None:
        assert _stage_venv(tmp_path, monkeypatch, context) is not None
    else:
        with pytest.raises(ValueError, match=re.escape(error)):
            _stage_venv(tmp_path, monkeypatch, context)


def test_virtualenv_unknown_target_marker_values_fail_closed_when_missing(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _write_venv(
        context, {"venv-root": ['Requires-Dist: venv-kernel; platform_release >= "5"']}
    )

    with pytest.raises(ValueError, match="venv-kernel.*unknown for the target"):
        _stage_venv(tmp_path, monkeypatch, context)

    _write_venv(context, {"venv-kernel": []})
    assert _stage_venv(tmp_path, monkeypatch, context) is not None


def test_virtualenv_egg_info_requires_sections_are_verified(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_venv(context, {})
    egg_info = context / ".venv" / "Lib" / "site-packages" / "venv_egg.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text("Metadata-Version: 1.1\nName: venv-egg\nVersion: 1.0\n")
    (egg_info / "requires.txt").write_text('[:sys_platform == "linux"]\nvenv-linux-only\n')

    with pytest.raises(ValueError, match="venv-egg requires venv-linux-only"):
        _stage_venv(tmp_path, monkeypatch, context)


def test_virtualenv_unparsed_root_markers_fail_closed(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_venv(context, {"venv-root": ['Requires-Dist: venv-any; python_version >= "3"']})
    (context / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\nvenv-linux-only = { version = "*", platform = "linux" }\n'
    )

    with pytest.raises(ValueError, match=r"\[tool.poetry\] may declare environment markers"):
        _stage_venv(tmp_path, monkeypatch, context)


def test_uv_virtualenv_version_info_must_match_image(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_venv(context, {"venv-root": []}, pyvenv="implementation = CPython\nversion_info = 3.14\n")

    with pytest.raises(ValueError, match="targets Python 3.14"):
        _stage_venv(tmp_path, monkeypatch, context)

def test_virtualenv_roots_include_files_seen_first_as_constraints(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_venv(context, {"venv-root": []})
    (context / "requirements.txt").write_text("-r base.txt\n-c constraints.txt\nvenv-root\n")
    (context / "constraints.txt").write_text("-r base.txt\n")
    (context / "base.txt").write_text("venv-linux-only; sys_platform != 'win32'\n")

    with pytest.raises(ValueError, match="requirements file requires venv-linux-only"):
        _stage_venv(tmp_path, monkeypatch, context)


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
    _write_venv(context, {"venv-root": ['Requires-Dist: venv-any; python_version >= "3"']})
    for name, text in files.items():
        (context / name).write_text(text)

    with pytest.raises(ValueError, match="may declare environment markers|cannot be read"):
        _stage_venv(tmp_path, monkeypatch, context)


def test_virtualenv_reads_roots_from_install_option_and_utf16_requirements(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _write_venv(context, {"venv-root": []})
    (context / "requirements.txt").write_text(
        "--only-binary=:all:\nvenv-root\nvenv-linux-only; sys_platform == 'linux'\n",
        encoding="utf-16",
    )

    with pytest.raises(ValueError, match="requirements file requires venv-linux-only"):
        _stage_venv(tmp_path, monkeypatch, context)


def test_virtualenv_definite_extras_match_unnormalized_marker_spellings(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _write_venv(context, {})
    egg_info = context / ".venv" / "Lib" / "site-packages" / "venv_egg.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text("Metadata-Version: 1.1\nName: venv-egg\nVersion: 1.0\n")
    (egg_info / "requires.txt").write_text("[Speed_Ups]\nvenv-fast\n")
    (context / "requirements.txt").write_text("venv-egg[speed-ups]\n")

    with pytest.raises(ValueError, match="venv-egg requires venv-fast"):
        _stage_venv(tmp_path, monkeypatch, context)

@pytest.mark.parametrize(
    ("lib", "fails"), [(("lib", "python3.11"), False), (("Lib",), True)]
)
def test_virtualenv_posix_layout_rules_out_extras_that_skip_windows(
    tmp_path, monkeypatch, lib, fails
):
    context = tmp_path / "context"
    _write_venv(
        context,
        {
            "venv-root": [
                "Provides-Extra: uvloop",
                'Requires-Dist: venv-uvloop; sys_platform != "win32" and extra == "uvloop"',
            ]
        },
        lib=lib,
    )
    (context / "requirements.txt").write_text("venv-root\n")

    if fails:
        with pytest.raises(ValueError, match="extra 'uvloop' of venv-root"):
            _stage_venv(tmp_path, monkeypatch, context)
    else:
        assert _stage_venv(tmp_path, monkeypatch, context) is not None
