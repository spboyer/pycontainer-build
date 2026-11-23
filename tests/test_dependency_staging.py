import base64
import csv
import gzip
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
from types import SimpleNamespace

import pytest

from pycontainer import dependencies
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.fs_utils import iter_files
from pycontainer.project import detect_conda_inputs
from pycontainer.dependencies import (
    MAX_REQUIREMENTS_BYTES,
    _target_options,
    install_requirements,
    staged_packages,
    target_python_version,
    target_libc_config,
    target_wheel_platform,
    target_wheel_platforms,
    validate_requirements,
)
from pycontainer.registry_client import parse_image_reference
from pycontainer.oci import OCILayer


BASE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {
        "Env": [
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "PYTHON_VERSION=3.11.8",
            "PYTHON_IMPLEMENTATION=CPython",
            "PYTHON_LIBC=glibc-2.17",
        ],
        "WorkingDir": "/",
    },
    "rootfs": {"type": "layers", "diff_ids": []},
}

UNPROVEN_BASE_CONFIG = {
    **BASE_CONFIG,
    "config": {
        **BASE_CONFIG["config"],
        "Env": [
            item for item in BASE_CONFIG["config"]["Env"]
            if not item.startswith("PYTHON_LIBC=")
        ],
    },
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
    options.setdefault("base_image", "python:3.11-slim-bookworm")
    return ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / "output"),
            use_cache=False,
            **options,
        )
    )


@pytest.mark.parametrize("arch, variant", [("amd64", None), ("arm", "v7")])
@pytest.mark.parametrize("implementation", [False, True])
def test_staging_rejects_tag_without_recorded_pull_provenance(
    tmp_path, monkeypatch, arch, variant, implementation
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "requirements.txt").write_text("fixture-dep==1.0\n")
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("pip must not run"))
    layers = tmp_path / "layers"
    layers.mkdir()
    base_config = {
        "os": "linux",
        "architecture": arch,
        **({"variant": variant} if variant else {}),
        "config": {"Env": [
            "PYTHON_VERSION=3.11.8",
            *(["PYTHON_IMPLEMENTATION=CPython"] if implementation else []),
        ]},
    }

    with pytest.raises(ValueError, match="unverified or custom base image"):
        builder._create_deps_layer(
            layers, base_config=base_config, os_name="linux", arch=arch, variant=variant
        )


@pytest.mark.parametrize("signal", [
    "environment.yml", "environment.yaml", "conda-lock.yml", "conda-meta",
])
@pytest.mark.parametrize("dry_run", [False, True])
def test_conda_only_fails_before_pull(tmp_path, monkeypatch, signal, dry_run):
    context = tmp_path / "context"
    context.mkdir()
    path = context / signal
    if signal == "conda-meta":
        path.mkdir()
    else:
        path.write_text("not even valid YAML")
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_deps=True, dry_run=dry_run,
    )
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: pytest.fail("base pull"))

    with pytest.raises(ValueError, match="Conda staging is unsupported.*pip freeze"):
        builder.build()
    assert not Path(builder.config.output_dir).exists()


@pytest.mark.parametrize("signal", [
    "environment.yml", "environment.yaml", "conda-lock.yml", ".venv/conda-meta",
])
def test_conda_with_selected_pip_requirements_warns_and_never_stages_host_env(
    tmp_path, monkeypatch, caplog, signal,
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    path = context / signal
    if path.name == "conda-meta":
        path.mkdir(parents=True)
    else:
        path.write_text("invalid YAML")
    host = context / ".venv" / "Lib" / "site-packages"
    host.mkdir(parents=True)
    (host / "host.pyd").write_bytes(b"host binary")
    requirements = context / "deps" / "export.txt"
    requirements.parent.mkdir()
    requirements.write_text("")
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_deps=True,
        requirements_file="deps\\export.txt", exclude_paths=["deps"],
    )
    installed = []

    def install(path, destination, *args, base_image_tag_verified, variant=None):
        assert base_image_tag_verified is False
        assert variant is None
        installed.append(path)
        destination.mkdir(parents=True)
        (destination / "pip_dep.py").write_text("value = 1\n")
        return [path]

    monkeypatch.setattr("pycontainer.builder.install_requirements", install)
    monkeypatch.setattr(
        "pycontainer.builder.stage_virtualenv", lambda *args: pytest.fail("host staging"),
    )
    builder.build()

    assert installed == [requirements]
    assert len([record for record in caplog.records if "Conda inputs ignored" in record.message]) == 1
    with tarfile.open(builder.layers[0].tar_path) as archive:
        assert "app/.python/site-packages/pip_dep.py" in archive.getnames()
    with tarfile.open(builder.layers[1].tar_path) as archive:
        assert "app/app.py" in archive.getnames()
        assert not any(".venv" in name or "/deps/" in name for name in archive.getnames())


@pytest.mark.parametrize("include", [None, ["."], ["app.py", "local/Lib"], ["app.py", "local"]])
def test_no_deps_build_excludes_conda_prefix_even_without_defaults(
    tmp_path, monkeypatch, caplog, include,
):
    context = tmp_path / "context"
    prefix = context / "local"
    (prefix / "conda-meta").mkdir(parents=True)
    (prefix / "Lib").mkdir()
    (prefix / "Lib" / "host.pyd").write_bytes(b"host binary")
    (context / "app.py").write_text("print('app')\n")
    (context / "environment.yml").write_text("invalid YAML")
    builder = _builder(
        tmp_path, monkeypatch, context=context, include_paths=include,
        exclude_defaults=False,
    )
    builder.build()
    with tarfile.open(builder.layers[0].tar_path) as archive:
        names = archive.getnames()
    assert "app/app.py" in names
    assert not any("/local/" in name for name in names)
    assert not [record for record in caplog.records if "Conda" in record.message]


def test_conda_detection_respects_context_exclusions_and_path_types(tmp_path):
    (tmp_path / "environment.yml").mkdir()
    (tmp_path / "environment.yaml").write_text("invalid YAML")
    (tmp_path / "conda-meta").write_text("not a directory")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conda-lock.yml").write_text("ignored")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "environment.yml").write_text("invalid YAML")
    (tmp_path / ".venv" / "conda-meta").mkdir(parents=True)
    assert detect_conda_inputs(tmp_path, exclude_paths=["nested"]) == [
        Path(".venv/conda-meta"), Path("environment.yaml"),
    ]


def test_conda_detection_independent_of_app_selection(tmp_path, monkeypatch):
    (tmp_path / "environment.yml").write_text("invalid YAML")
    (tmp_path / "app.py").write_text("print('app')\n")
    builder = _builder(
        tmp_path / "run", monkeypatch, context=tmp_path, include_deps=True,
        include_paths=["app.py"],
    )
    with pytest.raises(ValueError, match="Conda staging is unsupported"):
        builder.build()


@pytest.mark.parametrize("selected", [
    "environment.yml", "../requirements.txt", "C:\\requirements.txt", "folder",
])
def test_conda_rejects_invalid_selected_pip_input(tmp_path, monkeypatch, selected):
    (tmp_path / "environment.yml").write_text("invalid YAML")
    (tmp_path / "folder").mkdir()
    builder = _builder(
        tmp_path / "run", monkeypatch, context=tmp_path, include_deps=True,
        requirements_file=selected,
    )
    monkeypatch.setattr(
        "pycontainer.builder.install_requirements", lambda *args: pytest.fail("pip"),
    )
    with pytest.raises(ValueError):
        builder.build()


def test_root_conda_prefix_requires_source_only_context(tmp_path, monkeypatch):
    (tmp_path / "conda-meta").mkdir()
    (tmp_path / "app.py").write_text("print('app')\n")
    builder = _builder(tmp_path / "run", monkeypatch, context=tmp_path)
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: pytest.fail("base pull"))
    with pytest.raises(ValueError, match="source-only build context"):
        builder.build()


@pytest.mark.parametrize("signal", [
    "environment.yml", "environment.yaml", "conda-lock.yml",
])
def test_no_deps_conda_descriptors_do_not_install_or_warn(
    tmp_path, monkeypatch, caplog, signal,
):
    (tmp_path / signal).write_text("invalid YAML")
    (tmp_path / "app.py").write_text("print('app')\n")
    builder = _builder(tmp_path / "run", monkeypatch, context=tmp_path)
    monkeypatch.setattr(builder, "_create_deps_layer", lambda *args, **kwargs: pytest.fail("staging"))
    builder.build()
    assert len(builder.layers) == 1
    assert not [record for record in caplog.records if "Conda" in record.message]


def test_excluded_conda_descriptor_does_not_change_ordinary_venv_selection(
    tmp_path, monkeypatch, caplog,
):
    (tmp_path / "environment.yml").write_text("invalid YAML")
    host = tmp_path / ".venv" / "Lib" / "site-packages"
    host.mkdir(parents=True)
    (host / "pure.py").write_text("value = 1\n")
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "app.py").write_text("print('app')\n")
    builder = _builder(
        tmp_path / "run", monkeypatch, context=tmp_path, include_deps=True,
        exclude_paths=["environment.yml"],
    )
    monkeypatch.setattr("pycontainer.builder.install_requirements", lambda *args: pytest.fail("pip"))
    builder.build()
    with tarfile.open(builder.layers[0].tar_path) as archive:
        assert "app/.python/site-packages/pure.py" in archive.getnames()
    assert not [record for record in caplog.records if "Conda" in record.message]


def test_conda_prefix_cannot_be_staged_when_signal_is_excluded(tmp_path, monkeypatch):
    host = tmp_path / ".venv" / "Lib" / "site-packages"
    host.mkdir(parents=True)
    (host / "pure.py").write_text("value = 1\n")
    (tmp_path / ".venv" / "conda-meta").mkdir()
    builder = _builder(
        tmp_path / "run", monkeypatch, context=tmp_path, include_deps=True,
        exclude_paths=[".venv/conda-meta"],
    )
    with pytest.raises(FileNotFoundError, match="no supported virtualenv"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)


def test_conda_signal_rejects_ordinary_venv_without_exported_requirements(tmp_path, monkeypatch):
    (tmp_path / "environment.yml").write_text("invalid YAML")
    host = tmp_path / ".venv" / "Lib" / "site-packages"
    host.mkdir(parents=True)
    (host / "pure.py").write_text("value = 1\n")
    builder = _builder(tmp_path / "run", monkeypatch, context=tmp_path, include_deps=True)
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: pytest.fail("base pull"))
    with pytest.raises(ValueError, match="Conda staging is unsupported"):
        builder.build()


def test_requirements_inside_conda_prefix_are_not_used(tmp_path, monkeypatch):
    prefix = tmp_path / "local"
    (prefix / "conda-meta").mkdir(parents=True)
    (prefix / "requirements.txt").write_text("")
    builder = _builder(
        tmp_path / "run", monkeypatch, context=tmp_path, include_deps=True,
        requirements_file="local/requirements.txt",
    )
    with pytest.raises(ValueError, match="host Conda environments are never copied"):
        builder.build()


def test_conda_and_pip_dry_run_warns_without_installing(tmp_path, monkeypatch, caplog):
    (tmp_path / "environment.yml").write_text("invalid YAML")
    (tmp_path / "requirements.txt").write_text("")
    builder = _builder(
        tmp_path / "run", monkeypatch, context=tmp_path, include_deps=True, dry_run=True,
    )
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: pytest.fail("base pull"))
    monkeypatch.setattr("pycontainer.builder.install_requirements", lambda *args: pytest.fail("pip"))
    assert builder.build() == builder.config.tag
    assert len([record for record in caplog.records if "Conda inputs ignored" in record.message]) == 1


def _symlink_or_skip(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error}")


@pytest.mark.parametrize("include_deps", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_context_inside_conda_prefix_is_rejected_before_pull(
    tmp_path, monkeypatch, include_deps, dry_run,
):
    prefix = tmp_path / "host-env"
    (prefix / "conda-meta").mkdir(parents=True)
    context = prefix / "Lib" / "site-packages"
    context.mkdir(parents=True)
    (context / "host.pyd").write_bytes(b"host binary")
    (context / "requirements.txt").write_text("")
    builder = _builder(
        tmp_path / "run", monkeypatch, context=context,
        include_deps=include_deps, dry_run=dry_run,
        include_paths=["host.pyd"], exclude_defaults=False,
    )
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: pytest.fail("base pull"))

    with pytest.raises(ValueError, match="source-only build context"):
        builder.build()
    assert list(iter_files(context, ["."], exclude_defaults=False)) == []
    assert not Path(builder.config.output_dir).exists()


def test_context_link_into_conda_prefix_is_rejected_before_pull(tmp_path, monkeypatch):
    prefix = tmp_path / "host-env"
    (prefix / "conda-meta").mkdir(parents=True)
    site = prefix / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (site / "host.pyd").write_bytes(b"host binary")
    context = tmp_path / "context"
    _symlink_or_skip(context, site, directory=True)
    builder = _builder(tmp_path / "run", monkeypatch, context=context, dry_run=True)

    with pytest.raises(ValueError, match="source-only build context"):
        builder.build()


@pytest.mark.parametrize("dry_run", [False, True])
def test_linked_conda_virtualenv_is_detected_before_pull(tmp_path, monkeypatch, dry_run):
    prefix = tmp_path / "host-env"
    (prefix / "conda-meta").mkdir(parents=True)
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    _symlink_or_skip(context / ".venv", prefix, directory=True)
    builder = _builder(
        tmp_path / "run", monkeypatch, context=context,
        include_deps=True, dry_run=dry_run,
    )
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: pytest.fail("base pull"))

    assert detect_conda_inputs(context) == [Path(".venv/conda-meta")]
    assert detect_conda_inputs(context, exclude_paths=[".venv/conda-meta"]) == []
    with pytest.raises(ValueError, match="Conda staging is unsupported.*pip freeze"):
        builder.build()


def test_linked_conda_virtualenv_warns_and_installs_export(tmp_path, monkeypatch, caplog):
    prefix = tmp_path / "host-env"
    (prefix / "conda-meta").mkdir(parents=True)
    (prefix / "host.pyd").write_bytes(b"host binary")
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    requirements = context / "requirements.txt"
    _write_wheel(context / "wheelhouse")
    requirements.write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    _symlink_or_skip(context / ".venv", prefix, directory=True)
    builder = _builder(
        tmp_path / "run", monkeypatch, context=context, include_deps=True,
        include_paths=[".", ".venv"], exclude_defaults=False,
    )
    monkeypatch.setattr(
        "pycontainer.builder.stage_virtualenv", lambda *args: pytest.fail("host staging"),
    )
    builder.build()

    assert len([record for record in caplog.records if "Conda inputs ignored" in record.message]) == 1
    with tarfile.open(builder.layers[0].tar_path) as archive:
        assert "app/.python/site-packages/fixture_dep/__init__.py" in archive.getnames()
    with tarfile.open(builder.layers[1].tar_path) as archive:
        assert not any("host.pyd" in name for name in archive.getnames())


@pytest.mark.parametrize("selected", ["src", "src/nested/environment.yml"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_selected_linked_source_conda_descriptor_is_detected(
    tmp_path, monkeypatch, selected, dry_run,
):
    context = tmp_path / "context"
    context.mkdir()
    source = tmp_path / "linked-source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "environment.yml").write_text("invalid YAML")
    _symlink_or_skip(context / "src", source, directory=True)
    builder = _builder(
        tmp_path / "run", monkeypatch, context=context, include_deps=True,
        include_paths=[selected], dry_run=dry_run,
    )
    monkeypatch.setattr(builder, "_pull_base_image", lambda *args: pytest.fail("base pull"))

    with pytest.raises(ValueError, match="Conda staging is unsupported.*pip freeze"):
        builder.build()


def test_selected_linked_source_detection_respects_excludes(tmp_path, monkeypatch, caplog):
    context = tmp_path / "context"
    context.mkdir()
    source = tmp_path / "linked-source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "environment.yml").write_text("invalid YAML")
    _symlink_or_skip(context / "src", source, directory=True)
    builder = _builder(
        tmp_path / "run", monkeypatch, context=context, include_deps=True,
        include_paths=["src"], exclude_paths=["src/nested"], dry_run=True,
    )

    assert builder.build() == builder.config.tag
    assert not [record for record in caplog.records if "Conda" in record.message]


def test_selected_linked_source_prefers_pip_requirements(tmp_path, monkeypatch, caplog):
    context = tmp_path / "context"
    context.mkdir()
    source = tmp_path / "linked-source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "environment.yml").write_text("invalid YAML")
    (source / "app.py").write_text("print('app')\n")
    _symlink_or_skip(context / "src", source, directory=True)
    venv = context / ".venv" / "Lib" / "site-packages"
    venv.mkdir(parents=True)
    (venv / "venv_dep.py").write_text("value = 1\n")
    requirements = context / "requirements.txt"
    requirements.write_text("")
    builder = _builder(
        tmp_path / "run", monkeypatch, context=context, include_deps=True,
        include_paths=["src"],
    )
    installed = []

    def install(path, destination, *args, base_image_tag_verified, variant=None):
        assert base_image_tag_verified is False
        assert variant is None
        installed.append(path)
        destination.mkdir(parents=True)
        (destination / "pip_dep.py").write_text("value = 1\n")
        return [path]

    monkeypatch.setattr("pycontainer.builder.install_requirements", install)
    monkeypatch.setattr(
        "pycontainer.builder.stage_virtualenv", lambda *args: pytest.fail("venv staging"),
    )
    builder.build()

    assert installed == [requirements]
    assert len([record for record in caplog.records if "Conda inputs ignored" in record.message]) == 1


@pytest.mark.parametrize("selected", ["src", "src/host.pyd"])
def test_selected_link_into_conda_prefix_is_reported_before_pull(
    tmp_path, monkeypatch, selected,
):
    prefix = tmp_path / "host-env"
    (prefix / "conda-meta").mkdir(parents=True)
    site = prefix / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (site / "host.pyd").write_bytes(b"host binary")
    context = tmp_path / "context"
    context.mkdir()
    _symlink_or_skip(context / "src", site, directory=True)
    builder = _builder(
        tmp_path / "run", monkeypatch, context=context, include_deps=True,
        include_paths=[selected], dry_run=True,
    )

    with pytest.raises(ValueError, match="Conda staging is unsupported.*pip freeze"):
        builder.build()


def test_selected_excluded_conda_prefix_does_not_trigger_detection(tmp_path):
    prefix = tmp_path / ".venv"
    (prefix / "conda-meta").mkdir(parents=True)
    site = prefix / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (site / "host.pyd").write_bytes(b"host binary")

    assert detect_conda_inputs(
        tmp_path, exclude_paths=[".venv/conda-meta"],
        selected_paths=[".venv/Lib/site-packages/host.pyd"],
    ) == []


def test_selected_link_into_excluded_conda_prefix_is_not_copied(tmp_path):
    prefix = tmp_path / "host-env"
    (prefix / "conda-meta").mkdir(parents=True)
    site = prefix / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (site / "host.pyd").write_bytes(b"host binary")
    context = tmp_path / "context"
    context.mkdir()
    _symlink_or_skip(context / "src", site, directory=True)

    assert detect_conda_inputs(
        context, exclude_paths=["**/conda-meta"],
        selected_paths=["src/host.pyd"],
    ) == []
    assert list(iter_files(context, ["src/host.pyd"], exclude_defaults=False)) == []


@pytest.mark.parametrize("exclude_paths", [[], ["**/conda-meta"]])
def test_selected_requirements_inside_linked_conda_prefix_is_rejected_before_pull(
    tmp_path, monkeypatch, exclude_paths,
):
    prefix = tmp_path / "host-env"
    (prefix / "conda-meta").mkdir(parents=True)
    exports = prefix / "exports"
    exports.mkdir()
    (exports / "requirements.txt").write_text("")
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    _symlink_or_skip(context / "linked", exports, directory=True)
    builder = _builder(
        tmp_path / "run", monkeypatch, context=context, include_deps=True,
        requirements_file="linked/requirements.txt", exclude_paths=exclude_paths,
        dry_run=True,
    )

    with pytest.raises(ValueError, match="Conda staging is unsupported"):
        builder.build()


@pytest.mark.parametrize("signal", [
    "environment.yml", "environment.yaml", "conda-lock.yml",
])
@pytest.mark.parametrize("exclude_target", [False, True])
def test_requirements_alias_to_conda_descriptor_is_rejected(
    tmp_path, monkeypatch, signal, exclude_target,
):
    descriptor = tmp_path / signal
    descriptor.write_text("numpy\n")
    _symlink_or_skip(tmp_path / "requirements.txt", descriptor)
    builder = _builder(
        tmp_path / "run", monkeypatch, context=tmp_path, include_deps=True,
        exclude_paths=[signal] if exclude_target else [], dry_run=True,
    )

    with pytest.raises(ValueError, match="Conda staging is unsupported"):
        builder.build()


@pytest.mark.parametrize("include", [
    [".", "alias", "alias.pyd"], ["app.py", "alias"], ["app.py", "alias.pyd"],
])
def test_conda_prefix_symlink_aliases_never_enter_app_layer(tmp_path, monkeypatch, include):
    context = tmp_path / "context"
    context.mkdir()
    outside = tmp_path / "host-env"
    (outside / "conda-meta").mkdir(parents=True)
    binary = outside / "host.pyd"
    binary.write_bytes(b"host binary")
    (context / "app.py").write_text("print('app')\n")
    _symlink_or_skip(context / "alias.pyd", binary)
    _symlink_or_skip(context / "alias", outside, directory=True)
    builder = _builder(
        tmp_path, monkeypatch, context=context, exclude_defaults=False,
        include_paths=include,
    )
    builder.build()
    with tarfile.open(builder.layers[0].tar_path) as archive:
        assert archive.getnames() == ["app/app.py"]


def test_conda_rejects_requirements_symlink_outside_context(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "environment.yml").write_text("invalid YAML")
    outside = tmp_path / "export.txt"
    outside.write_text("")
    _symlink_or_skip(context / "requirements.txt", outside)
    builder = _builder(tmp_path, monkeypatch, context=context, include_deps=True)
    with pytest.raises(ValueError, match="stay inside the build context"):
        builder.build()


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
        "linux", "amd64", "3.11", "python:3.11-alpine3.20",
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
        "linux", "arm64", "3.11", "python:3.11-slim-bookworm",
        base_image_tag_verified=True,
    )[1] == "manylinux_2_31_aarch64"

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
            base_image="python:3.11-slim-bookworm",
        )
    )
    with pytest.raises(ValueError, match="Requirements files exceed"):
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)


def _libc_config(*env):
    return {"config": {"Env": ["PYTHON_VERSION=3.11.8", *env]}}


def _base_metadata_layer(tmp_path, entries, *, compressed=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            if isinstance(content, tarfile.TarInfo):
                member = content
                archive.addfile(member)
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    data = gzip.compress(buffer.getvalue()) if compressed else buffer.getvalue()
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    path = tmp_path / digest.split(":")[1]
    path.write_bytes(data)
    media_type = "application/vnd.oci.image.layer.v1.tar" + ("+gzip" if compressed else "")
    return OCILayer(media_type, digest, len(data), str(path))


def _dpkg_libc(version="2.36-9", architecture="amd64"):
    return (
        f"Package: libc6\nStatus: install ok installed\nVersion: {version}\n"
        f"Architecture: {architecture}\n\n"
    ).encode()


def _layer_libc_config(layers, *, base_image="python:3.11-slim", arch="amd64"):
    config = {**UNPROVEN_BASE_CONFIG, "architecture": arch}
    return target_libc_config(
        "linux", arch, base_image, config, layers, base_image_tag_verified=True
    )


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("replacement_first", [False, True])
def test_layer_libc_uses_final_regular_database_after_whiteouts(
    tmp_path, compressed, replacement_first
):
    database = "var/lib/dpkg/status"
    lower = _base_metadata_layer(tmp_path, [(database, _dpkg_libc("2.31-13"))])
    entries = [
        ("var/lib/dpkg/.wh..wh..opq", b""),
        (database, _dpkg_libc("2.36-9")),
    ]
    if replacement_first:
        entries.reverse()
    upper = _base_metadata_layer(tmp_path, entries, compressed=compressed)
    original = json.loads(json.dumps(BASE_CONFIG))
    config = _layer_libc_config([lower, upper])
    assert target_wheel_platform("linux", "amd64", "python:3.11-slim", config) == (
        "manylinux_2_36_x86_64"
    )
    assert BASE_CONFIG == original
    assert config["config"]["Env"] is not BASE_CONFIG["config"]["Env"]
    assert "PYTHON_LIBC=glibc-2.36" in config["config"]["Env"]


@pytest.mark.parametrize(
    "whiteout", ["var/lib/dpkg/.wh.status", "var/lib/.wh.dpkg",
                 "var/lib/dpkg/.wh..wh..opq", ".wh.var"]
)
def test_layer_libc_does_not_reuse_deleted_lower_metadata(tmp_path, whiteout):
    lower = _base_metadata_layer(tmp_path, [("var/lib/dpkg/status", _dpkg_libc())])
    upper = _base_metadata_layer(tmp_path, [(whiteout, b"")])
    with pytest.raises(ValueError, match="absent or ambiguous"):
        _layer_libc_config([lower, upper])


@pytest.mark.parametrize("replace", [False, True])
def test_layer_libc_applies_zero_device_whiteouts_before_additions(tmp_path, replace):
    database = "var/lib/dpkg/status"
    lower = _base_metadata_layer(tmp_path, [(database, _dpkg_libc("2.31-13"))])
    whiteout = tarfile.TarInfo("var/lib/dpkg/.wh.status")
    whiteout.type = tarfile.CHRTYPE
    entries = [(whiteout.name, whiteout)]
    if replace:
        entries.insert(0, (database, _dpkg_libc()))
    upper = _base_metadata_layer(tmp_path, entries)
    if replace:
        config = _layer_libc_config([lower, upper])
        assert "PYTHON_LIBC=glibc-2.36" in config["config"]["Env"]
    else:
        with pytest.raises(ValueError, match="absent or ambiguous"):
            _layer_libc_config([lower, upper])


@pytest.mark.parametrize("pax_size", ["-1", "not-a-size", "9" * 5000],
                         ids=["negative", "non-numeric", "overflow"])
def test_layer_libc_rejects_invalid_device_whiteouts_and_pax_sizes(tmp_path, pax_size):
    whiteout = tarfile.TarInfo("var/lib/dpkg/.wh.status")
    whiteout.type = tarfile.CHRTYPE
    whiteout.devmajor = 1
    layer = _base_metadata_layer(tmp_path, [(whiteout.name, whiteout)])
    with pytest.raises(ValueError, match="invalid whiteout"):
        _layer_libc_config([layer])
    invalid = tarfile.TarInfo("unrelated")
    invalid.pax_headers = {"size": pax_size}
    layer = _base_metadata_layer(tmp_path, [(invalid.name, invalid)])
    with pytest.raises(ValueError, match="invalid tar member size"):
        _layer_libc_config([layer])


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ([], "absent or ambiguous"),
        ([("var/lib/dpkg/status", _dpkg_libc("2.13-38+deb7u12"))], "Unsupported"),
        ([("var/lib/dpkg/status", _dpkg_libc(architecture="arm64"))], "architecture"),
        ([("var/lib/dpkg/status", _dpkg_libc("not-a-version"))], "version"),
        ([("var/lib/dpkg/status", b"Package: libc6\nVersion: 2.36-9\n")], "incomplete"),
        ([("var/lib/dpkg/status", _dpkg_libc().replace(b"install ok", b"deinstall ok"))],
         "not installed"),
        ([("var/lib/dpkg/status", _dpkg_libc() + _dpkg_libc())], "ambiguous"),
        ([("var/lib/dpkg/status", _dpkg_libc().replace(
            b"Version: 2.36-9", b"Version: 2.36-9\nVersion: 2.13-1"))], "ambiguous"),
        ([("var/lib/dpkg/status", b"\xff")], "utf-8"),
        ([("var/lib/dpkg/status", _dpkg_libc()),
          ("lib/apk/db/installed", b"P:musl\nV:1.2.5-r0\nA:x86_64\n")], "ambiguous"),
        ([("var/lib/dpkg/status", _dpkg_libc()),
          ("var/lib/dpkg/status", _dpkg_libc())], "one regular file"),
    ],
)
def test_layer_libc_rejects_missing_ambiguous_or_unsupported_evidence(
    tmp_path, records, message
):
    layer = _base_metadata_layer(tmp_path, records)
    with pytest.raises(ValueError, match=message):
        _layer_libc_config([layer])


@pytest.mark.parametrize("name", ["var", "var/lib/dpkg", "var/lib/dpkg/status"])
@pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_layer_libc_rejects_linked_metadata_or_ancestors(tmp_path, name, link_type):
    link = tarfile.TarInfo(name)
    link.type = link_type
    link.linkname = "elsewhere"
    layer = _base_metadata_layer(tmp_path, [(name, link)])
    with pytest.raises(ValueError, match="linked ancestor|one regular file"):
        _layer_libc_config([layer])


@pytest.mark.parametrize(
    ("base_image", "database", "record", "unrelated", "active", "expected"),
    [
        ("python:3.11-slim", "var/lib/dpkg/status", _dpkg_libc(), "lib", "var",
         "glibc-2.36"),
        ("python:3.11-alpine", "lib/apk/db/installed", b"P:musl\nV:1.2.5-r0\nA:x86_64\n",
         "var", "lib", "musl-1.2"),
    ],
    ids=["glibc-usr-merged-lib", "musl"],
)
def test_layer_libc_ignores_only_links_above_the_other_database(
    tmp_path, base_image, database, record, unrelated, active, expected
):
    # Real python:3.11-slim layers contain lib -> usr/lib (usr-merged Debian).
    def link(name):
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = f"usr/{name}"
        return info

    layer = _base_metadata_layer(tmp_path, [(unrelated, link(unrelated)), (database, record)])
    config = _layer_libc_config([layer], base_image=base_image)
    assert f"PYTHON_LIBC={expected}" in config["config"]["Env"]
    layer = _base_metadata_layer(tmp_path, [(active, link(active)), (database, record)])
    with pytest.raises(ValueError, match="linked ancestor"):
        _layer_libc_config([layer], base_image=base_image)


_FOREIGN_DATABASE_LINK_CASES = [
    pytest.param(
        "python:3.11-slim", "var/lib/dpkg/status", _dpkg_libc(), "lib",
        "usr/lib/apk/db/installed", b"P:musl\nV:1.2.5-r0\nA:x86_64\n",
        "glibc-2.36", id="glibc",
    ),
    pytest.param(
        "python:3.11-alpine", "lib/apk/db/installed", b"P:musl\nV:1.2.5-r0\nA:x86_64\n",
        "var", "usr/var/lib/dpkg/status", _dpkg_libc(), "musl-1.2", id="musl",
    ),
]


@pytest.mark.parametrize(
    ("base_image", "database", "record", "root", "alias", "foreign", "expected"),
    _FOREIGN_DATABASE_LINK_CASES,
)
@pytest.mark.parametrize("placement", ["same-layer", "link-first", "link-last"])
def test_layer_libc_rejects_mixed_databases_hidden_by_foreign_link(
    tmp_path, base_image, database, record, root, alias, foreign, expected, placement
):
    link = tarfile.TarInfo(root)
    link.type = tarfile.SYMTYPE
    link.linkname = f"usr/{root}"
    if placement == "same-layer":
        layers = [_base_metadata_layer(tmp_path, [(root, link), (database, record), (alias, foreign)])]
    elif placement == "link-first":
        layers = [
            _base_metadata_layer(tmp_path, [(root, link), (database, record)]),
            _base_metadata_layer(tmp_path, [(alias, foreign)]),
        ]
    else:
        layers = [
            _base_metadata_layer(tmp_path, [(database, record), (alias, foreign)]),
            _base_metadata_layer(tmp_path, [(root, link)]),
        ]
    with pytest.raises(ValueError, match="ambiguous"):
        _layer_libc_config(layers, base_image=base_image)


@pytest.mark.parametrize(
    ("base_image", "database", "record", "root", "alias", "foreign", "expected"),
    _FOREIGN_DATABASE_LINK_CASES,
)
@pytest.mark.parametrize("whiteout", ["file", "parent", "opaque"])
@pytest.mark.parametrize("replace", [False, True])
def test_layer_libc_applies_whiteouts_to_linked_foreign_database(
    tmp_path, base_image, database, record, root, alias, foreign, expected, whiteout, replace
):
    link = tarfile.TarInfo(root)
    link.type = tarfile.SYMTYPE
    link.linkname = f"usr/{root}"
    lower = _base_metadata_layer(tmp_path, [(root, link), (database, record), (alias, foreign)])
    parent, _, name = alias.rpartition("/")
    if whiteout == "parent":
        grandparent, _, directory = parent.rpartition("/")
        marker = f"{grandparent}/.wh.{directory}"
    elif whiteout == "opaque":
        marker = f"{parent}/.wh..wh..opq"
    else:
        marker = f"{parent}/.wh.{name}"
    entries = [(marker, b"")]
    if replace:
        entries.insert(0, (alias, foreign))
    upper = _base_metadata_layer(tmp_path, entries)
    if replace:
        with pytest.raises(ValueError, match="ambiguous"):
            _layer_libc_config([lower, upper], base_image=base_image)
    else:
        config = _layer_libc_config([lower, upper], base_image=base_image)
        assert f"PYTHON_LIBC={expected}" in config["config"]["Env"]


@pytest.mark.parametrize(
    ("base_image", "database", "record", "root", "alias", "foreign", "expected"),
    _FOREIGN_DATABASE_LINK_CASES,
)
def test_layer_libc_does_not_forget_foreign_database_through_root_replacement(
    tmp_path, base_image, database, record, root, alias, foreign, expected
):
    link = tarfile.TarInfo(root)
    link.type = tarfile.SYMTYPE
    link.linkname = f"usr/{root}"
    canonical = alias.removeprefix("usr/")
    lower = _base_metadata_layer(
        tmp_path, [(root, link), (database, record), (canonical, foreign)]
    )
    upper = _base_metadata_layer(tmp_path, [(f".wh.{root}", b""), (root, link)])
    with pytest.raises(ValueError, match="ambiguous"):
        _layer_libc_config([lower, upper], base_image=base_image)


@pytest.mark.parametrize(
    ("base_image", "database", "record", "root", "alias", "foreign", "expected"),
    _FOREIGN_DATABASE_LINK_CASES,
)
@pytest.mark.parametrize(
    "link_location",
    ["unexpected-root", "escaping-root", "absolute-root", "hardlink-root", "nonempty-root",
     "canonical-target", "nested-target"],
)
def test_layer_libc_rejects_unverifiable_foreign_links(
    tmp_path, base_image, database, record, root, alias, foreign, expected, link_location
):
    root_link = tarfile.TarInfo(root)
    root_link.type = tarfile.LNKTYPE if link_location == "hardlink-root" else tarfile.SYMTYPE
    root_link.linkname = {
        "unexpected-root": "elsewhere",
        "escaping-root": f"../usr/{root}",
        "absolute-root": f"/usr/{root}",
        "hardlink-root": f"usr/{root}",
        "nonempty-root": f"usr/{root}",
        "canonical-target": f"usr/{root}",
        "nested-target": f"usr/{root}",
    }[link_location]
    if link_location == "nonempty-root":
        root_link.size = 1
    entries = [(root, root_link), (database, record)]
    if link_location in {"canonical-target", "nested-target"}:
        nested_path = alias.rsplit("/", 2)[0]
        if link_location == "canonical-target":
            nested_path = nested_path.removeprefix("usr/")
        nested = tarfile.TarInfo(nested_path)
        nested.type = tarfile.SYMTYPE
        nested.linkname = "elsewhere"
        entries.append((nested_path, nested))
    with pytest.raises(ValueError, match="linked ancestor"):
        _layer_libc_config([_base_metadata_layer(tmp_path, entries)], base_image=base_image)


@pytest.mark.parametrize(
    "name",
    ["../usr/lib/apk/db/installed", "/usr/lib/apk/db/installed",
     "usr\\lib\\apk\\db\\installed", "usr/lib/apk/db/installed\0ignored"],
)
def test_layer_libc_rejects_unsafe_foreign_database_paths(tmp_path, name):
    entry = tarfile.TarInfo("placeholder")
    entry.pax_headers = {"path": name}
    layer = _base_metadata_layer(
        tmp_path, [("var/lib/dpkg/status", _dpkg_libc()), (entry.name, entry)]
    )
    with pytest.raises(ValueError, match="unsafe layer path"):
        _layer_libc_config([layer])


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (b"P:musl\nV:1.2.5-r0\nV:1.1.22-r3\nA:x86_64\n", "ambiguous"),
        (b"P:musl\nV:1.2.5-r0\nA:aarch64\n", "architecture"),
    ],
)
def test_layer_libc_rejects_ambiguous_or_foreign_apk_metadata(tmp_path, record, message):
    layer = _base_metadata_layer(tmp_path, [("lib/apk/db/installed", record)])
    with pytest.raises(ValueError, match=message):
        _layer_libc_config([layer], base_image="python:3.11-alpine")


@pytest.mark.parametrize(
    ("limit", "value", "message"),
    [
        ("MAX_METADATA_BYTES", 1, "metadata exceeds"),
        ("MAX_LAYER_BYTES", 1000, "layer size limit"),
        ("MAX_LAYER_MEMBERS", 1, "member limit"),
    ],
)
def test_layer_libc_inspection_is_bounded(tmp_path, monkeypatch, limit, value, message):
    layer = _base_metadata_layer(
        tmp_path, [("unrelated", b""), ("var/lib/dpkg/status", _dpkg_libc())],
        compressed=True,
    )
    monkeypatch.setattr(f"pycontainer.base_libc.{limit}", value)
    with pytest.raises(ValueError, match=message):
        _layer_libc_config([layer])


def test_layer_libc_rejects_corruption_and_timeout(tmp_path, monkeypatch):
    layer = _base_metadata_layer(tmp_path, [("var/lib/dpkg/status", _dpkg_libc())])
    original = Path(layer.tar_path).read_bytes()
    Path(layer.tar_path).write_bytes(b"x" + original[1:])
    with pytest.raises(ValueError, match="digest or size"):
        _layer_libc_config([layer])
    Path(layer.tar_path).write_bytes(original)
    ticks = iter([0, 301])
    monkeypatch.setattr("pycontainer.base_libc.time.monotonic", lambda: next(ticks, 301))
    with pytest.raises(TimeoutError, match="time limit"):
        _layer_libc_config([layer])


def test_layer_libc_bounds_extended_headers_before_tarfile_parses_them(
    tmp_path, monkeypatch
):
    extended = tarfile.TarInfo("unrelated")
    extended.pax_headers = {"comment": "x" * 200}
    layer = _base_metadata_layer(
        tmp_path, [("unrelated", extended), ("var/lib/dpkg/status", _dpkg_libc())]
    )
    monkeypatch.setattr("pycontainer.base_libc.MAX_METADATA_BYTES", 128)
    with pytest.raises(ValueError, match="extended tar metadata"):
        _layer_libc_config([layer])


@pytest.mark.parametrize("damage", ["gzip_crc", "tar_end", "media_type"])
def test_layer_libc_checks_entire_archive_and_media_type(tmp_path, damage):
    layer = _base_metadata_layer(
        tmp_path, [("var/lib/dpkg/status", _dpkg_libc())], compressed=True
    )
    data = Path(layer.tar_path).read_bytes()
    if damage == "gzip_crc":
        data = data[:-8] + bytes([data[-8] ^ 1]) + data[-7:]
    elif damage == "tar_end":
        data = gzip.compress(gzip.decompress(data)[:1024])
    else:
        layer.media_type = "application/vnd.oci.image.layer.v1.tar"
    Path(layer.tar_path).write_bytes(data)
    layer.digest = "sha256:" + hashlib.sha256(data).hexdigest()
    layer.size = len(data)
    with pytest.raises((ValueError, OSError), match="CRC|end blocks|media type"):
        _layer_libc_config([layer])


@pytest.mark.parametrize(
    ("base_image", "verified"),
    [("python:3.11-slim", False), ("example/python:3.11-slim", True),
     ("python:3.11-slim@sha256:" + "a" * 64, True)],
)
def test_layer_libc_cannot_establish_digest_custom_or_local_provenance(
    tmp_path, base_image, verified
):
    layer = _base_metadata_layer(tmp_path, [("var/lib/dpkg/status", _dpkg_libc())])
    config = target_libc_config(
        "linux", "amd64", base_image, UNPROVEN_BASE_CONFIG, [layer],
        base_image_tag_verified=verified,
    )
    assert config is UNPROVEN_BASE_CONFIG
    with pytest.raises(ValueError, match="unverified"):
        target_wheel_platform(
            "linux", "amd64", base_image, config, base_image_tag_verified=verified
        )


@pytest.mark.parametrize(
    ("base_image", "version", "arch", "record", "wheel_platform"),
    [
        ("python:3.12-slim", "3.12", "arm64", _dpkg_libc(architecture="arm64"),
         "manylinux_2_36_aarch64"),
        (None, "3.11", "amd64", _dpkg_libc(), "manylinux_2_36_x86_64"),
        ("python:3.8.0-alpine", "3.8", "amd64",
         b"P:musl\nV:1.1.22-r3\nA:x86_64\n\n", "musllinux_1_1_x86_64"),
        ("python:3.11-alpine", "3.11", "amd64",
         b"P:musl\nV:1.2.5-r0\nA:x86_64\n\n", "musllinux_1_2_x86_64"),
        ("python:3.4-wheezy", "3.4", "amd64",
         _dpkg_libc("2.13-38+deb7u12"), None),
    ],
)
def test_aspire_and_default_cli_stage_using_verified_base_layer_libc(
    tmp_path, monkeypatch, base_image, version, arch, record, wheel_platform
):
    from pycontainer import cli
    from pycontainer.registry_client import RegistryClient

    context = tmp_path / "api"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    requirements = context / ".pycontainer-aspire" / "api" / "requirements.txt"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n")
    abi = "cp" + version.replace(".", "")
    if wheel_platform:
        _write_wheel(context / "wheelhouse", tag=f"{abi}-{abi}-{wheel_platform}")
    else:
        _write_wheel(context / "wheelhouse", tag="cp34-abi3-manylinux2014_x86_64")
    if "alpine" in (base_image or ""):
        for minor in (1, 2, 3):
            _write_wheel(
                context / "wheelhouse", tag=f"{abi}-{abi}-musllinux_1_{minor}_x86_64"
            )
    database = "lib/apk/db/installed" if "alpine" in (base_image or "") else (
        "var/lib/dpkg/status"
    )
    layer = _base_metadata_layer(tmp_path, [(database, record)], compressed=True)
    layer_data = Path(layer.tar_path).read_bytes()
    base_config = {
        "architecture": arch, "os": "linux",
        "config": {"Env": [f"PYTHON_VERSION={version}.0"], "WorkingDir": "/"},
        "rootfs": {"type": "layers", "diff_ids": [
            "sha256:" + hashlib.sha256(gzip.decompress(layer_data)).hexdigest()
        ]},
    }
    config_data = json.dumps(base_config).encode()
    config_digest = "sha256:" + hashlib.sha256(config_data).hexdigest()
    blobs = {layer.digest: layer_data, config_digest: config_data}
    manifest_data = json.dumps({
        "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                   "digest": config_digest, "size": len(config_data)},
        "layers": [{"mediaType": layer.media_type, "digest": layer.digest,
                    "size": layer.size}],
    }).encode()

    def manifest_response(self, method, url, **kwargs):
        assert method == "GET" and "/manifests/" in url
        return 200, manifest_data, {
            "Content-Type": "application/vnd.oci.image.manifest.v1+json",
            "Docker-Content-Digest": "sha256:" + hashlib.sha256(manifest_data).hexdigest(),
        }

    def blob_response(self, url):
        data = blobs[url.rsplit("/", 1)[1]]
        response = io.BytesIO(data)
        response.status = 200
        response.headers = {"Content-Length": str(len(data))}
        return response

    monkeypatch.setattr(
        "pycontainer.builder.get_registry_auth",
        lambda registry: SimpleNamespace(bearer_token=None, username=None, password=None),
    )
    monkeypatch.setattr(RegistryClient, "_make_request", manifest_response)
    monkeypatch.setattr(RegistryClient, "_open_blob_response", blob_response)
    pushes = []
    monkeypatch.setattr(ImageBuilder, "push", lambda self, **kwargs: pushes.append(kwargs))
    output = tmp_path / "obj" / "image"
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text(f'[build]\noutput-dir = "{output.as_posix()}"\nworkdir = "/app"\n')
    arguments = [
        "pycontainer", "build", "--context", str(context), "--tag", "api:latest",
        "--platform", f"linux/{arch}", "--no-progress",
        "--include-deps", "--requirements", ".pycontainer-aspire/api/requirements.txt",
        "--exclude", ".venv", "--exclude", "venv", "--exclude", "env",
        "--exclude", ".pycontainer-aspire", "--push",
        "--registry", "localhost:5443/samples/api:latest", "--no-cache",
    ]
    if base_image:
        arguments.extend(["--base-image", base_image, "--config", str(config_path)])
    else:
        arguments.extend(["--config", str(config_path)])
    monkeypatch.setattr(sys, "argv", arguments)
    if wheel_platform is None:
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **k: pytest.fail("pip must not run for glibc 2.13")
        )
        with pytest.raises(ValueError, match="Unsupported PYTHON_LIBC"):
            cli.main()
        assert pushes == []
        return
    cli.main()
    index = json.loads((output / "index.json").read_text())
    blob_dir = output / "blobs" / "sha256"
    manifest = json.loads(
        (blob_dir / index["manifests"][0]["digest"].split(":")[1]).read_text()
    )
    with tarfile.open(blob_dir / manifest["layers"][1]["digest"].split(":")[1]) as archive:
        wheel = archive.extractfile(
            "app/.python/site-packages/fixture_dep-1.0.dist-info/WHEEL"
        ).read().decode()
    assert f"Tag: {abi}-{abi}-{wheel_platform}" in wheel
    image_config = json.loads(
        (blob_dir / manifest["config"]["digest"].split(":")[1]).read_text()
    )
    assert not any(value.startswith("PYTHON_LIBC=") for value in image_config["config"]["Env"])
    assert json.loads((blob_dir / config_digest.split(":")[1]).read_text()) == base_config
    assert pushes[0]["show_progress"] is False


@pytest.mark.parametrize(
    ("base_image", "version", "wheel_tag", "family"),
    [
        ("python:3.8.0-alpine", "3.8", "cp38-cp38-musllinux_1_2_x86_64", "musl"),
        ("python:3.4-wheezy", "3.4", "cp34-abi3-manylinux2014_x86_64", "glibc"),
    ],
)
def test_unproven_old_image_cannot_select_newer_libc_wheel(
    tmp_path, base_image, version, wheel_tag, family
):
    _write_wheel(tmp_path / "wheelhouse", tag=wheel_tag)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    with pytest.raises(ValueError, match=f"Cannot validate target {family}"):
        install_requirements(
            requirements, tmp_path / "staged", tmp_path,
            "linux", "amd64", version, base_image,
            {"config": {"Env": [f"PYTHON_VERSION={version}"]}},
            base_image_tag_verified=True,
        )
    assert not (tmp_path / "staged").exists()


@pytest.mark.parametrize("source", ["requirements", "venv"])
@pytest.mark.parametrize("base_image", [None, "python:3.11-alpine"])
def test_unproven_default_or_alpine_rejects_before_dependency_staging(
    tmp_path, monkeypatch, source, base_image
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    if source == "requirements":
        (context / "requirements.txt").write_text("fixture-dep==1.0\n")
    else:
        site_packages = context / "venv" / "Lib" / "site-packages"
        site_packages.mkdir(parents=True)
        (site_packages / "fixture_dep.py").write_text("value = 1\n")
    options = {"base_image": base_image} if base_image else {}
    builder = ImageBuilder(
        BuildConfig(context_dir=str(context), include_deps=True, use_cache=False, **options)
    )

    def unexpected_staging(*args, **kwargs):
        pytest.fail("dependencies must not be staged without validated target libc")

    monkeypatch.setattr("pycontainer.builder.install_requirements", unexpected_staging)
    monkeypatch.setattr("pycontainer.builder.stage_virtualenv", unexpected_staging)
    builder._base_image_tag_verified = True
    with pytest.raises(ValueError, match="Cannot validate target") as error:
        builder._create_deps_layer(tmp_path / "layers", base_config=UNPROVEN_BASE_CONFIG)
    assert "python:3.11-slim-bookworm" in str(error.value)


@pytest.mark.parametrize("include_deps", [False, True])
def test_default_image_build_requires_libc_only_for_dependency_staging(
    tmp_path, monkeypatch, include_deps
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    (context / "requirements.txt").write_text("# no dependencies\n")
    def verified_pull(self, layers_dir, os_name, arch):
        self._base_image_tag_verified = True
        return [], UNPROVEN_BASE_CONFIG

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", verified_pull)
    builder = ImageBuilder(
        BuildConfig(
            context_dir=str(context), output_dir=str(tmp_path / "output"),
            use_cache=False, include_deps=include_deps,
        )
    )
    if include_deps:
        with pytest.raises(ValueError, match="Cannot validate target libc"):
            builder.build()
    else:
        builder.build()
        assert (tmp_path / "output" / "index.json").is_file()


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
        ("python:3.11-slim", "glibc-2.17", "manylinux_2_17_x86_64"),
        ("python", "glibc-2.17", "manylinux_2_17_x86_64"),
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
        (f"python@{DIGEST}", ("PYTHON_LIBC=musl-1.0",), "Unsupported PYTHON_LIBC"),
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
            "linux", "amd64", "3.11", base_image, _libc_config(),
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


def _glibc_tags(newest, machine):
    return [f"manylinux_2_{minor}_{machine}" for minor in range(newest, 16, -1)] + [
        f"manylinux2014_{machine}"
    ]


@pytest.mark.parametrize(
    ("arch", "base_image", "env", "verified", "expected"),
    [
        ("amd64", "python:3.11-slim", ("PYTHON_LIBC=glibc-2.17",), True,
         _glibc_tags(17, "x86_64")),
        ("arm64", "python:3.11", ("PYTHON_LIBC=glibc-2.17",), True,
         _glibc_tags(17, "aarch64")),
        ("amd64", "python:3.11-alpine", ("PYTHON_LIBC=musl-1.1",), True,
         ["musllinux_1_1_x86_64"]),
        ("arm64", "python:3.11-slim-bookworm", (), True, _glibc_tags(31, "aarch64")),
        ("amd64", "docker.io/library/python:3.11-bullseye", (), True,
         _glibc_tags(28, "x86_64")),
        ("amd64", "python:3.13-slim-trixie", (), True, _glibc_tags(36, "x86_64")),
        ("amd64", "python:3.8-buster", (), True, _glibc_tags(24, "x86_64")),
        ("amd64", "python:3.6-stretch", (), True, _glibc_tags(19, "x86_64")),
        ("amd64", "python:3.5-jessie", (), True, _glibc_tags(17, "x86_64")),
        ("arm64", "python:3.11-alpine3.20", (), True,
         ["musllinux_1_2_aarch64", "musllinux_1_1_aarch64"]),
        ("amd64", "python:3.9-alpine3.12", (), True, ["musllinux_1_1_x86_64"]),
        ("amd64", "python:3.11-slim-bookworm", ("PYTHON_LIBC=glibc-2.31",), True,
         _glibc_tags(31, "x86_64")),
        ("amd64", "example/python:custom", ("PYTHON_LIBC=glibc-2.28",), True,
         _glibc_tags(28, "x86_64")),
        ("amd64", "python:3.11-slim-bookworm", ("PYTHON_LIBC=glibc-2.17",), False,
         _glibc_tags(17, "x86_64")),
        ("amd64", f"python:3.11-slim-bookworm@{DIGEST}", ("PYTHON_LIBC=glibc-2.17",),
         True, _glibc_tags(17, "x86_64")),
        ("arm64", f"python@{DIGEST}", ("PYTHON_LIBC=musl-1.2",), False,
         ["musllinux_1_2_aarch64", "musllinux_1_1_aarch64"]),
        ("amd64", "python:3.11-notalpine", ("PYTHON_LIBC=glibc-2.17",), True,
         _glibc_tags(17, "x86_64")),
        ("amd64", "python:3.11-Slim-Bookworm", (), True, _glibc_tags(31, "x86_64")),
        ("amd64", "python:3.11-Alpine3.12", (), True, ["musllinux_1_1_x86_64"]),
        ("amd64", "python:3.11-alpine3.24", (), True,
         ["musllinux_1_2_x86_64", "musllinux_1_1_x86_64"]),
        ("amd64", "python:3.11-alpine3.25", ("PYTHON_LIBC=musl-1.2",), True,
         ["musllinux_1_2_x86_64", "musllinux_1_1_x86_64"]),
        ("arm64", "python:3.11-alpine3.99", ("PYTHON_LIBC=musl-1.1",), True,
         ["musllinux_1_1_aarch64"]),
    ],
)
def test_compatible_wheel_platforms_follow_verified_libc(
    arch, base_image, env, verified, expected
):
    platforms = target_wheel_platforms(
        "linux", arch, base_image, _libc_config(*env),
        base_image_tag_verified=verified,
    )
    assert platforms == expected
    options = _target_options(
        "linux", arch, "3.11", base_image, _libc_config(*env),
        base_image_tag_verified=verified,
    )
    assert options[: 2 * len(expected)] == [
        item for platform in expected for item in ("--platform", platform)
    ]
    assert options[2 * len(expected):] == [
        "--python-version", "3.11", "--implementation", "cp", "--abi", "cp311",
        "--only-binary=:all:",
    ]


@pytest.mark.parametrize(
    ("base_image", "env", "verified", "message"),
    [
        ("example/python:custom", ("PYTHON_LIBC=glibc-2",), True, "Unsupported"),
        ("example/python:custom", ("PYTHON_LIBC=glibc-x.y",), True, "Unsupported"),
        ("example/python:custom", ("PYTHON_LIBC=glibc-2.16",), True, "Unsupported"),
        ("example/python:custom", ("PYTHON_LIBC=glibc-2.017",), True, "Unsupported"),
        ("example/python:custom", ("PYTHON_LIBC=glibc-2.100",), True, "Unsupported"),
        ("example/python:custom", ("PYTHON_LIBC=musl-2.1",), True, "Unsupported"),
        ("python:3.11-slim-bookworm", ("PYTHON_LIBC=musl-1.2",), True, "conflicts"),
        ("python:3.11-alpine3.20", ("PYTHON_LIBC=glibc-2.36",), True, "conflicts"),
        ("python:3.11-alpine-bookworm", (), True, "glibc codename"),
        ("python:3.11-alpine3.20-bookworm", (), True, "more than one"),
        ("python:3.8.0-alpine", (), True, "Cannot validate target musl"),
        ("python:3.11-alpine", (), True, "Cannot validate target musl"),
        ("python:3.11-alpine3.25", (), True, "Cannot validate target musl"),
        ("python:3.11-Alpine3.99", (), True, "Cannot validate target musl"),
        ("python:3.4-wheezy", (), True, "Cannot validate target glibc"),
        ("python:3.4-wheezy", ("PYTHON_LIBC=glibc-2.13",), True, "Unsupported"),
        ("python:3.11-slim", (), True, "Cannot validate target glibc"),
        ("python", (), True, "Cannot validate target glibc"),
        ("python:3.11-slim-future", (), True, "Cannot validate target glibc"),
        ("python:3.11-slim-bookworm", (), False, "unverified"),
        (f"python:3.11-slim-bookworm@{DIGEST}", (), True, "unverified"),
        ("example/python:3.11-slim-bookworm", (), True, "unverified"),
    ],
)
def test_wheel_platforms_reject_invalid_or_unverifiable_libc_before_pip(
    tmp_path, monkeypatch, base_image, env, verified, message
):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fixture-dep==1.0\n")
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **k: pytest.fail("pip must not run without a validated libc"),
    )
    with pytest.raises(ValueError, match=message):
        install_requirements(
            requirements, tmp_path / "staged", tmp_path,
            "linux", "amd64", "3.11", base_image, _libc_config(*env),
            base_image_tag_verified=verified,
        )
    assert not (tmp_path / "staged").exists()


def _install_fixture_wheel(tmp_path, wheel_tag, arch, base_image, env=()):
    context = tmp_path / wheel_tag / base_image.replace(":", "_").replace("/", "_")
    context.mkdir(parents=True)
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    _write_wheel(context / "wheelhouse", tag=f"cp311-cp311-{wheel_tag}")
    destination = context / "staged"
    install_requirements(
        context / "requirements.txt", destination, context,
        "linux", arch, "3.11", base_image, _libc_config(*env),
        base_image_tag_verified="@" not in base_image and not env,
    )
    return (destination / "fixture_dep-1.0.dist-info" / "WHEEL").read_text()


@pytest.mark.parametrize(
    ("wheel_tag", "arch", "base_image", "env"),
    [
        ("manylinux_2_28_aarch64", "arm64", "python:3.11-slim-bookworm", ()),
        ("manylinux_2_28_aarch64", "arm64", "python:3.11-bullseye", ()),
        ("manylinux_2_28_x86_64", "amd64", "example/python:custom",
         ("PYTHON_IMPLEMENTATION=CPython", "PYTHON_LIBC=glibc-2.28")),
        ("manylinux_2_36_x86_64", "amd64", "python:3.11-slim-trixie", ()),
        ("musllinux_1_1_x86_64", "amd64", "python:3.11-alpine3.20", ()),
        ("musllinux_1_1_x86_64", "amd64", "python:3.11-alpine3.10", ()),
        ("musllinux_1_2_aarch64", "arm64", "python:3.11-alpine3.20", ()),
        ("musllinux_1_1_x86_64", "amd64", "python:3.11-alpine",
         ("PYTHON_LIBC=musl-1.1",)),
        ("musllinux_1_1_aarch64", "arm64", f"python@{DIGEST}",
         ("PYTHON_LIBC=musl-1.2",)),
    ],
)
def test_real_pip_selects_compatible_newer_or_older_libc_wheels(
    tmp_path, wheel_tag, arch, base_image, env
):
    wheel = _install_fixture_wheel(tmp_path, wheel_tag, arch, base_image, env)
    assert f"Tag: cp311-cp311-{wheel_tag}" in wheel


@pytest.mark.parametrize(
    ("wheel_tag", "arch", "base_image", "env"),
    [
        ("manylinux_2_28_aarch64", "arm64", "python:3.11-slim",
         ("PYTHON_LIBC=glibc-2.17",)),
        ("manylinux_2_28_aarch64", "arm64", "python:3.11-stretch", ()),
        ("manylinux_2_28_x86_64", "amd64", "example/python:custom",
         ("PYTHON_LIBC=glibc-2.27",)),
        ("manylinux_2_32_aarch64", "arm64", "python:3.11-slim-bookworm", ()),
        ("manylinux_2_29_x86_64", "amd64", "python:3.11-bullseye", ()),
        ("manylinux_2_37_x86_64", "amd64", "python:3.11-slim-trixie", ()),
        ("musllinux_1_2_x86_64", "amd64", "python:3.11-alpine",
         ("PYTHON_LIBC=musl-1.1",)),
        ("musllinux_1_2_x86_64", "amd64", "python:3.11-alpine3.12", ()),
        ("musllinux_1_1_x86_64", "amd64", "python:3.11-slim-bookworm", ()),
        ("manylinux2014_x86_64", "amd64", "python:3.11-alpine3.20", ()),
        ("manylinux_2_28_aarch64", "amd64", "python:3.11-slim-bookworm", ()),
    ],
)
def test_real_pip_rejects_wheels_outside_selected_platforms(
    tmp_path, wheel_tag, arch, base_image, env
):
    with pytest.raises(RuntimeError, match="Only wheels compatible with"):
        _install_fixture_wheel(tmp_path, wheel_tag, arch, base_image, env)


def test_real_pip_prefers_newest_compatible_libc_wheel(tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
    )
    for tag in ("manylinux2014_aarch64", "manylinux_2_28_aarch64",
                "manylinux_2_39_aarch64"):
        _write_wheel(context / "wheelhouse", tag=f"cp311-cp311-{tag}")
    for base_image, expected, env in (
        ("python:3.11-slim-bookworm", "manylinux_2_28_aarch64", ()),
        ("python:3.11-slim", "manylinux2014_aarch64", ("PYTHON_LIBC=glibc-2.17",)),
    ):
        destination = tmp_path / base_image.replace(":", "_")
        install_requirements(
            context / "requirements.txt", destination, context,
            "linux", "arm64", "3.11", base_image, _libc_config(*env),
            base_image_tag_verified=True,
        )
        wheel = (destination / "fixture_dep-1.0.dist-info" / "WHEEL").read_text()
        assert f"Tag: cp311-cp311-{expected}" in wheel


def test_codename_tag_keeps_dependency_layer_digest_for_same_wheel(
    tmp_path, monkeypatch
):
    digests = []
    for base_image in ("python:3.11-slim", "python:3.11-slim-bookworm"):
        context = tmp_path / base_image.replace(":", "_")
        context.mkdir()
        _write_wheel(context / "wheelhouse", tag="cp311-cp311-manylinux2014_x86_64")
        (context / "requirements.txt").write_text(
            "--no-index\n--find-links ./wheelhouse\nfixture-dep==1.0\n"
        )
        builder = _builder(
            tmp_path, monkeypatch, context=context, include_deps=True,
            base_image=base_image, workdir="/app", reproducible=True,
        )
        builder._base_image_tag_verified = True
        layers = tmp_path / "layers" / base_image.replace(":", "_")
        layers.mkdir(parents=True)
        config = BASE_CONFIG if base_image == "python:3.11-slim" else UNPROVEN_BASE_CONFIG
        digests.append(builder._create_deps_layer(layers, base_config=config).digest)
    assert digests[0] == digests[1]


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
            "python:3.11-slim-bookworm",
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
        "python:3.11-slim-bookworm",
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


def _install_marker_fixture(
    tmp_path, requirements, arch, python_version, full_version, libc="glibc-2.17"
):
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
        {"config": {"Env": [f"PYTHON_VERSION={full_version}", f"PYTHON_LIBC={libc}"]}},
        variant="v7" if arch == "arm" else None,
        base_image_tag_verified=True,
    )
    return {name for name, _ in staged_packages(destination)}


@pytest.mark.parametrize(
    ("arch", "expected"),
    [
        ("amd64", {"marker-amd", "marker-root-amd"}),
        ("arm64", {"marker-arm", "marker-feat", "marker-root-arm"}),
        ("arm", {"marker-armv7", "marker-root-armv7"}),
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
            'marker-armv7; platform_machine == "armv7l"',
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
        "marker-armv7", "marker-root-armv7",
    ):
        _write_named_wheel(wheelhouse, name)

    installed = _install_marker_fixture(
        tmp_path,
        [
            "marker-root[feat]",
            'marker-root-arm; platform_machine == "aarch64"',
            'marker-root-amd; platform_machine == "x86_64"',
            'marker-root-armv7; platform_machine == "armv7l"',
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


def test_target_marker_selects_newest_compatible_libc_wheel(tmp_path):
    wheelhouse = tmp_path / "context" / "wheelhouse"
    _write_named_wheel(
        wheelhouse, "marker-root",
        requires=['fixture-dep; platform_machine == "aarch64"'],
    )
    for minor in (17, 28, 39):
        _write_wheel(wheelhouse, tag=f"cp311-cp311-manylinux_2_{minor}_aarch64")

    installed = _install_marker_fixture(
        tmp_path, ["marker-root"], "arm64", "3.11", "3.11.8", "glibc-2.28"
    )

    assert installed == {"marker-root", "fixture-dep"}
    wheel = (tmp_path / "stage" / "site-packages" / "fixture_dep-1.0.dist-info" / "WHEEL")
    assert "Tag: cp311-cp311-manylinux_2_28_aarch64" in wheel.read_text()


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
        "config": {
            "Env": [
                "PYTHON_IMPLEMENTATION=CPython",
                f"PYTHON_VERSION={python_version}",
                "PYTHON_LIBC=glibc-2.17",
            ],
            "WorkingDir": "/",
        },
    }

def _multi_platform_marker_builder(tmp_path, monkeypatch, context):
    def pull_base(self, layers_dir, os_name, arch):
        config = {
            **BASE_CONFIG,
            "architecture": arch,
            **({"variant": "v7"} if arch == "arm" else {}),
            "config": {
                **BASE_CONFIG["config"],
                "Env": [
                    "PYTHON_IMPLEMENTATION=CPython",
                    f"PYTHON_VERSION=3.11.{8 if arch == 'amd64' else 9}",
                    "PYTHON_LIBC=glibc-2.17",
                ],
            },
        }
        return [], config

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", pull_base)
    return ImageBuilder(
        BuildConfig(
            tag="app:v1",
            base_image="example.invalid/custom:latest",
            context_dir=str(context),
            output_dir=str(tmp_path / "output"),
            platform="linux/amd64,linux/arm64,linux/arm/v7",
            include_deps=True,
            use_cache=False,
        )
    )


def test_arm_v7_maps_to_armv7l_wheel_tags_and_requires_variant():
    verified = {"base_image_tag_verified": True, "variant": "v7"}
    glibc = target_wheel_platforms(
        "linux", "arm", "python:3.11-slim-bookworm", None, **verified
    )
    assert glibc[0] == "manylinux_2_31_armv7l"
    assert glibc[-2:] == ["manylinux_2_17_armv7l", "manylinux2014_armv7l"]
    assert target_wheel_platforms(
        "linux", "arm", "python:3.11-alpine3.20", None, **verified
    ) == ["musllinux_1_2_armv7l", "musllinux_1_1_armv7l"]
    custom_config = {"config": {"Env": ["PYTHON_LIBC=glibc-2.17"]}}
    options = _target_options(
        "linux", "arm", "3.11", "example/python:custom", custom_config, variant="v7"
    )
    assert options[:4] == [
        "--platform", "manylinux_2_17_armv7l", "--platform", "manylinux2014_armv7l"
    ]
    assert "--only-binary=:all:" in options
    for variant, target in ((None, "linux/arm;"), ("v6", "linux/arm/v6;")):
        with pytest.raises(ValueError, match=target):
            _target_options(
                "linux", "arm", "3.11", base_image_tag_verified=True, variant=variant
            )
        with pytest.raises(ValueError, match=target):
            target_libc_config(
                "linux", "arm", "python:3.11-slim", None, [],
                base_image_tag_verified=True, variant=variant,
            )


def test_arm_v7_verified_tag_inspects_libc_from_base_layers(tmp_path):
    layer = _base_metadata_layer(
        tmp_path, [("var/lib/dpkg/status", _dpkg_libc("2.28-1", "armhf"))]
    )
    config = target_libc_config(
        "linux", "arm", "python:3.11-slim", UNPROVEN_BASE_CONFIG, [layer],
        base_image_tag_verified=True, variant="v7",
    )
    tags = target_wheel_platforms(
        "linux", "arm", "python:3.11-slim", config,
        base_image_tag_verified=True, variant="v7",
    )
    assert tags[0] == "manylinux_2_28_armv7l"
    assert tags[-1] == "manylinux2014_armv7l"


def test_arm_v7_requirements_stage_only_armv7l_wheels(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    _write_wheel(
        context / "wheelhouse",
        tag="cp311-cp311-manylinux2014_armv7l",
    )
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


def test_multi_platform_build_resolves_markers_for_each_base_config(tmp_path, monkeypatch):
    context = tmp_path / "context"
    wheelhouse = context / "wheelhouse"
    _write_named_wheel(
        wheelhouse,
        "marker-root",
        requires=[
            'transitive-amd; platform_machine == "x86_64"',
            'transitive-arm; platform_machine == "aarch64"',
            'transitive-armv7; platform_machine == "armv7l"',
            'new-patch; python_full_version >= "3.11.9"',
        ],
    )
    for name in (
        "transitive-amd", "transitive-arm", "transitive-armv7",
        "new-patch", "root-amd", "root-arm", "root-armv7", "injected-dep",
    ):
        _write_named_wheel(wheelhouse, name)
    injected_requirements = tmp_path / "host-requirements.txt"
    injected_requirements.write_text("injected-dep\n")
    monkeypatch.setenv("PIP_REQUIREMENT", str(injected_requirements))
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\n"
        "marker-root\n"
        'root-amd; platform_machine == "x86_64"\n'
        'root-arm; platform_machine == "aarch64"\n'
        'root-armv7; platform_machine == "armv7l"\n'
    )
    (context / "app.py").write_text("print('hello')\n")
    builder = _multi_platform_marker_builder(tmp_path, monkeypatch, context)

    builder.build()

    expected = {
        "amd64": {"marker_root", "transitive_amd", "root_amd"},
        "arm64": {"marker_root", "transitive_arm", "root_arm", "new_patch"},
        "arm": {"marker_root", "transitive_armv7", "root_armv7", "new_patch"},
    }
    for image in builder.platform_images:
        with tarfile.open(image.layers[0].tar_path) as layer:
            modules = {
                name.split("/")[-2]
                for name in layer.getnames()
                if "/site-packages/" in name and name.endswith("/__init__.py")
            }
        assert modules == expected[image.platform["architecture"]]


def test_multi_platform_unknown_marker_preserves_published_index(tmp_path, monkeypatch):
    context = tmp_path / "context"
    wheelhouse = context / "wheelhouse"
    _write_named_wheel(wheelhouse, "common")
    _write_named_wheel(
        wheelhouse, "arm-root", requires=['unknown-dep; platform_release >= "1"']
    )
    _write_named_wheel(wheelhouse, "unknown-dep")
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\n"
        "common\n"
        'arm-root; platform_machine == "aarch64"\n'
    )
    (context / "app.py").write_text("print('hello')\n")
    out = tmp_path / "output"
    refs = out / "refs" / "tags"
    refs.mkdir(parents=True)
    (out / "index.json").write_text("previous index")
    (refs / "v1").write_text("previous tag")
    builder = _multi_platform_marker_builder(tmp_path, monkeypatch, context)

    with pytest.raises(ValueError, match="refusing to stage.*platform_release"):
        builder.build()

    assert (out / "index.json").read_text() == "previous index"
    assert (refs / "v1").read_text() == "previous tag"


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


def test_arm_v7_virtualenv_checks_target_markers(tmp_path, monkeypatch):
    context = tmp_path / "context"
    _write_base_venv(
        context,
        {"venv-root": ['Requires-Dist: venv-armv7-helper; platform_machine == "armv7l"']},
    )
    builder = _arm_v7_builder(tmp_path, monkeypatch, context)
    layers = tmp_path / "layers"
    layers.mkdir()

    with pytest.raises(ValueError, match="linux/arm/v7.*venv-armv7-helper"):
        builder._create_deps_layer(
            layers, base_config=ARM_V7_CONFIG, os_name="linux", arch="arm", variant="v7"
        )

    _write_base_venv(context, {"venv-armv7-helper": []})
    assert builder._create_deps_layer(
        layers, base_config=ARM_V7_CONFIG, os_name="linux", arch="arm", variant="v7"
    ) is not None


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
        "venv-root @ https://user:synthetic-pw-147@example.invalid/package.whl\n"
    )
    # A literal-credential root is rejected as a shipped input before closure checks.
    with pytest.raises(ValueError, match="URL with literal credentials") as error:
        _stage_base_venv(tmp_path, monkeypatch, context)
    assert "synthetic-pw-147" not in str(error.value)

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
