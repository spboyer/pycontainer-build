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
from pycontainer.fs_utils import iter_files
from pycontainer.project import detect_conda_inputs
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

    def install(path, destination, *args):
        installed.append(path)
        destination.mkdir(parents=True)
        (destination / "pip_dep.py").write_text("value = 1\n")

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

    def install(path, destination, *args):
        installed.append(path)
        destination.mkdir(parents=True)
        (destination / "pip_dep.py").write_text("value = 1\n")

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
