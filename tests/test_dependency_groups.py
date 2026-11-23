import base64
import contextlib
import csv
import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import pycontainer.builder as builder_module
import pycontainer.dependency_cache as dependency_cache_module
from pycontainer.builder import ImageBuilder
from pycontainer.cache import LayerCache
from pycontainer.config import BuildConfig
from test_dependency_staging import BASE_CONFIG
from test_lockfiles import indexes  # noqa: F401 - pytest fixture


def _wheel(directory, name, version, *, files=None, requires=(), entrypoint=None, script_name=None):
    module = name.replace("-", "_")
    dist = f"{module}-{version}.dist-info"
    contents = {
        f"{module}/__init__.py": f"version = {version!r}\n".encode(),
        f"{dist}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {item}\n" for item in requires)
            + "\n"
        ).encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    contents.update(files or {})
    if entrypoint:
        contents[f"{dist}/entry_points.txt"] = (
            f"[console_scripts]\n{script_name or name} = {entrypoint}\n"
        ).encode()
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for path, content in contents.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow((path, f"sha256={digest}", len(content)))
    writer.writerow((f"{dist}/RECORD", "", ""))
    contents[f"{dist}/RECORD"] = record.getvalue().encode()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{module}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for item, content in contents.items():
            archive.writestr(zipfile.ZipInfo(item, (2020, 1, 1, 0, 0, 0)), content)
    return path


def _write_project(context, registry, wheels, frequent_version="1.0"):
    context.mkdir(parents=True, exist_ok=True)
    app = context / "app.py"
    if not app.exists():
        app.write_text("print('app')\n", encoding="utf-8")
    (context / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
        f'dependencies = ["stable-dep==1.0", "frequent-dep=={frequent_version}"]\n'
        f"\n[[tool.uv.index]]\nurl = {json.dumps(registry)}\n",
        encoding="utf-8",
    )
    lock = [
        'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n',
        '[[package]]\nname = "demo"\nversion = "0.1.0"\nsource = { virtual = "." }\n'
        'dependencies = [{ name = "stable-dep" }, { name = "frequent-dep" }]\n'
        '[package.metadata]\nrequires-dist = ['
        '{ name = "stable-dep", specifier = "==1.0" }, '
        f'{{ name = "frequent-dep", specifier = "=={frequent_version}" }}]\n',
    ]
    for name in ("stable-dep", "frequent-dep", "shared-dep"):
        wheel = wheels[name]
        version = frequent_version if name == "frequent-dep" else "1.0"
        dependencies = (
            'dependencies = [{ name = "shared-dep" }]\n' if name != "shared-dep" else ""
        )
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        lock.append(
            f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
            f"source = {{ registry = {json.dumps(registry)} }}\n"
            f"{dependencies}"
            f'wheels = [{{ url = "https://example.invalid/{wheel.name}", '
            f'hash = "sha256:{digest}" }}]\n'
        )
    (context / "uv.lock").write_text("\n".join(lock), encoding="utf-8")


def _fixture(tmp_path, indexes, *, collision=False):
    context = tmp_path / "context"
    wheels_dir = context / "wheelhouse"
    stable_files = {"shared_namespace/stable.py": b"value = 'stable'\n"}
    frequent_files = {"shared_namespace/frequent.py": b"value = 'frequent'\n"}
    if collision:
        stable_files["collision.py"] = b"stable\n"
        frequent_files["collision.py"] = b"frequent\n"
    wheels = {
        "stable-dep": _wheel(
            wheels_dir,
            "stable-dep",
            "1.0",
            files={
                **stable_files,
                "stable_dep/__init__.py": b"def main():\n    print('stable script')\n",
            },
            requires=("shared-dep>=1.0",),
            entrypoint="stable_dep:main",
        ),
        "frequent-dep": _wheel(
            wheels_dir,
            "frequent-dep",
            "1.0",
            files=frequent_files,
            requires=("shared-dep>=1.0",),
        ),
        "shared-dep": _wheel(wheels_dir, "shared-dep", "1.0"),
    }
    index = indexes(*wheels.values())
    _write_project(context, index.url, wheels)
    return context, wheels, index


def _builder(tmp_path, monkeypatch, context, name, **options):
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image", lambda self, *_args: ([], BASE_CONFIG)
    )
    return ImageBuilder(
        BuildConfig(
            context_dir=str(context),
            output_dir=str(tmp_path / name),
            cache_dir=str(tmp_path / "cache"),
            include_paths=["app.py"],
            include_deps=True,
            generate_sbom=True,
            **options,
        )
    )


def _layer_files(layers):
    files = {}
    for layer in layers:
        with tarfile.open(layer.tar_path) as archive:
            for member in archive:
                if member.isfile():
                    assert member.name not in files
                    files[member.name] = (archive.extractfile(member).read(), member.mode)
    return files


def _image_json(builder, digest):
    output = Path(builder.config.output_dir)
    return json.loads((output / "blobs" / "sha256" / digest.split(":", 1)[1]).read_text())


def test_grouped_lock_preserves_image_and_reuses_unchanged_groups(
    tmp_path, monkeypatch, indexes, capsys
):
    context, wheels, index = _fixture(tmp_path, indexes)
    options = dict(
        dependency_layer_strategy="explicit",
        stable_dependencies=["stable-dep"],
        frequent_dependencies=["frequent-dep"],
    )
    single = _builder(tmp_path, monkeypatch, context, "single", use_cache=False)
    single.build()
    grouped = _builder(tmp_path, monkeypatch, context, "grouped", **options)
    grouped.build()

    assert len(single.layers) == 2
    assert len(grouped.layers) == 3
    assert _layer_files(single.layers) == _layer_files(grouped.layers)
    stable, frequent, app = grouped.layers
    stable_files = _layer_files([stable])
    frequent_files = _layer_files([frequent])
    assert "app/.python/site-packages/shared_namespace/stable.py" in stable_files
    assert "app/.python/site-packages/shared_namespace/frequent.py" in frequent_files
    assert "app/.python/site-packages/shared_dep/__init__.py" in frequent_files
    assert "app/.python/bin/stable-dep" in stable_files
    manifest = _image_json(grouped, grouped.manifest_digest)
    config = _image_json(grouped, grouped.config_digest)
    assert manifest["layers"] == [layer.to_descriptor() for layer in grouped.layers]
    assert config["rootfs"]["diff_ids"] == [layer.digest for layer in grouped.layers]
    for layer in grouped.layers:
        data = Path(layer.tar_path).read_bytes()
        assert len(data) == layer.size
        assert f"sha256:{hashlib.sha256(data).hexdigest()}" == layer.digest
    sbom = json.loads((Path(grouped.config.output_dir) / "sbom.json").read_text())
    assert {(item["name"], item["versionInfo"]) for item in sbom["packages"]} == {
        ("stable-dep", "1.0"), ("frequent-dep", "1.0"), ("shared-dep", "1.0")
    }

    extracted = tmp_path / "extracted"
    for name, (content, _mode) in _layer_files(grouped.layers).items():
        target = extracted / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    site = extracted / "app" / ".python" / "site-packages"
    environment = {**os.environ, "PYTHONPATH": str(site)}
    imported = subprocess.run(
        [sys.executable, "-c", "import shared_namespace.stable, shared_namespace.frequent, shared_dep"],
        env=environment, capture_output=True, text=True, check=True,
    )
    assert imported.returncode == 0
    script = subprocess.run(
        [sys.executable, str(extracted / "app" / ".python" / "bin" / "stable-dep")],
        env=environment, capture_output=True, text=True, check=True,
    )
    assert script.stdout.strip() == "stable script"

    requests = list(index.requests)
    original_install = builder_module.install_locked_requirements
    monkeypatch.setattr(
        builder_module, "install_locked_requirements",
        lambda *_args, **_kwargs: pytest.fail("unchanged grouped build invoked pip"),
    )
    (context / "app.py").write_text("print('changed')\n", encoding="utf-8")
    warm = _builder(tmp_path, monkeypatch, context, "warm", **options)
    warm.build()
    assert index.requests == requests
    assert [layer.digest for layer in warm.layers[:2]] == [stable.digest, frequent.digest]
    assert warm.layers[-1].digest != app.digest

    monkeypatch.setattr(builder_module, "install_locked_requirements", original_install)
    wheels["frequent-dep"] = _wheel(
        context / "wheelhouse", "frequent-dep", "2.0",
        files={"shared_namespace/frequent.py": b"value = 'changed'\n"},
        requires=("shared-dep>=1.0",),
    )
    index.add(wheels["frequent-dep"])
    _write_project(context, index.url, wheels, "2.0")
    changed = _builder(tmp_path, monkeypatch, context, "changed", **options)
    changed.build()
    assert changed.layers[0].digest == stable.digest
    assert changed.layers[1].digest != frequent.digest
    assert "group cache hit" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("strategy", ["single", "explicit"])
def test_reusing_builder_resolves_changed_lock_again(tmp_path, monkeypatch, indexes, strategy):
    context, wheels, index = _fixture(tmp_path, indexes)
    options = {"dependency_layer_strategy": strategy}
    if strategy == "explicit":
        options["stable_dependencies"] = ["stable-dep"]
    builder = _builder(tmp_path, monkeypatch, context, "repeated", **options)
    builder.build()
    initial = [layer.digest for layer in builder.layers]

    wheels["frequent-dep"] = _wheel(
        context / "wheelhouse", "frequent-dep", "2.0", requires=("shared-dep>=1.0",)
    )
    index.add(wheels["frequent-dep"])
    _write_project(context, index.url, wheels, "2.0")
    builder.build()

    inventory = json.loads((Path(builder.config.output_dir) / "sbom.json").read_text())
    assert ("frequent-dep", "2.0") in {
        (item["name"], item["versionInfo"]) for item in inventory["packages"]
    }
    assert builder.layers[-1].digest == initial[-1]
    if strategy == "explicit":
        assert builder.layers[0].digest == initial[0]
        assert builder.layers[1].digest != initial[1]
    else:
        assert builder.layers[0].digest != initial[0]


def test_grouping_rejects_conflicting_distribution_files(tmp_path, monkeypatch, indexes):
    context, _wheels, _index = _fixture(tmp_path, indexes, collision=True)
    builder = _builder(
        tmp_path, monkeypatch, context, "collision",
        dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"],
    )
    with pytest.raises((ValueError, RuntimeError), match="(?i)(collision|conflict|integrity)"):
        builder.build()


def test_grouping_rejects_console_entrypoints_with_multiple_owners(
    tmp_path, monkeypatch, indexes
):
    context, wheels, index = _fixture(tmp_path, indexes)
    for name in ("stable-dep", "frequent-dep"):
        wheels[name] = _wheel(
            context / "wheelhouse", name, "1.0", requires=("shared-dep>=1.0",),
            entrypoint="stable_dep:main", script_name="shared-command",
        )
        index.add(wheels[name])
    _write_project(context, index.url, wheels)
    builder = _builder(
        tmp_path, monkeypatch, context, "duplicate-script",
        dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"],
    )
    with pytest.raises(ValueError, match="entrypoint.*collision"):
        builder.build()


def test_grouping_rejects_unowned_files_and_application_overwrites(
    tmp_path, monkeypatch, indexes
):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    original = builder_module.install_locked_requirements

    def omit_record(*args, **kwargs):
        original(*args, **kwargs)
        record = args[1] / "stable_dep-1.0.dist-info" / "RECORD"
        lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
        record.write_text(
            "".join(line for line in lines if not line.startswith("shared_namespace/stable.py,")),
            encoding="utf-8",
        )

    monkeypatch.setattr(builder_module, "install_locked_requirements", omit_record)
    options = dict(dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"])
    with pytest.raises(ValueError, match="unowned file"):
        _builder(tmp_path, monkeypatch, context, "unowned", **options).build()

    monkeypatch.setattr(builder_module, "install_locked_requirements", original)
    reserved = context / ".python" / "site-packages" / "app_override.py"
    reserved.parent.mkdir(parents=True)
    reserved.write_text("pass\n", encoding="utf-8")
    builder = _builder(tmp_path, monkeypatch, context, "reserved", **options)
    builder.context_include_paths = ["app.py", ".python"]
    with pytest.raises(ValueError, match="reserve the .python directory"):
        builder.build()


def _add_target_and_extra(context, wheels, index):
    for name in ("optional-dep", "arm-only"):
        wheels[name] = _wheel(context / "wheelhouse", name, "1.0")
        index.add(wheels[name])
    manifest = context / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace("stable-dep==1.0", "stable-dep[feature]==1.0")
    text = text.replace(
        '"frequent-dep==1.0"]',
        '"frequent-dep==1.0", "arm-only==1.0; platform_machine == \'aarch64\'"]',
    )
    manifest.write_text(text, encoding="utf-8")
    lock = context / "uv.lock"
    text = lock.read_text(encoding="utf-8")
    text = text.replace(
        'dependencies = [{ name = "stable-dep" }, { name = "frequent-dep" }]',
        'dependencies = [{ name = "stable-dep", extra = ["feature"] }, '
        '{ name = "frequent-dep" }, '
        '{ name = "arm-only", marker = "platform_machine == \'aarch64\'" }]',
        1,
    )
    text = text.replace(
        '{ name = "stable-dep", specifier = "==1.0" }',
        '{ name = "stable-dep", extras = ["feature"], specifier = "==1.0" }',
        1,
    )
    text = text.replace(
        '{ name = "frequent-dep", specifier = "==1.0" }]',
        '{ name = "frequent-dep", specifier = "==1.0" }, '
        '{ name = "arm-only", specifier = "==1.0", '
        'marker = "platform_machine == \'aarch64\'" }]',
        1,
    )
    text = text.replace(
        '[[package]]\nname = "frequent-dep"',
        '[package.optional-dependencies]\nfeature = [{ name = "optional-dep" }]\n\n'
        '[[package]]\nname = "frequent-dep"',
        1,
    )
    for name in ("optional-dep", "arm-only"):
        wheel = wheels[name]
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        text += (
            f'\n[[package]]\nname = "{name}"\nversion = "1.0"\n'
            f"source = {{ registry = {json.dumps(index.url)} }}\n"
            f'wheels = [{{ url = "https://example.invalid/{wheel.name}", '
            f'hash = "sha256:{digest}" }}]\n'
        )
    lock.write_text(text, encoding="utf-8")


def test_grouping_preserves_target_markers_extras_and_target_cache_identity(
    tmp_path, monkeypatch, indexes, capsys
):
    context, wheels, index = _fixture(tmp_path, indexes)
    _add_target_and_extra(context, wheels, index)
    calls = []
    original = builder_module.load_lockfile

    def count_selection(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(builder_module, "load_lockfile", count_selection)
    options = dict(dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"])
    amd = _builder(tmp_path, monkeypatch, context, "amd", **options)
    amd.build()
    assert len(calls) == 1
    amd_sbom = json.loads((Path(amd.config.output_dir) / "sbom.json").read_text())
    amd_names = {item["name"] for item in amd_sbom["packages"]}
    assert "optional-dep" in amd_names
    assert "arm-only" not in amd_names
    capsys.readouterr()

    arm = _builder(tmp_path, monkeypatch, context, "arm", platform="linux/arm64", **options)
    arm_base = {**BASE_CONFIG, "architecture": "arm64"}
    monkeypatch.setattr(ImageBuilder, "_pull_base_image", lambda self, *_args: ([], arm_base))
    arm.build()
    assert len(calls) == 2
    arm_sbom = json.loads((Path(arm.config.output_dir) / "sbom.json").read_text())
    arm_names = {item["name"] for item in arm_sbom["packages"]}
    assert {"optional-dep", "arm-only"} <= arm_names
    assert "creating stable dependency layer" in capsys.readouterr().out.lower()
    assert arm.layers[0].digest == amd.layers[0].digest
    assert arm.layers[1].digest != amd.layers[1].digest


@pytest.mark.parametrize("change", ["installer", "marker_environment"])
def test_group_cache_invalidates_for_target_marker_backend(
    tmp_path, monkeypatch, indexes, change
):
    context, wheels, _index = _fixture(tmp_path, indexes)
    calls = []

    def install(_requirements, destination, *_args, **kwargs):
        calls.append(kwargs)
        destination.mkdir(parents=True, exist_ok=True)
        for wheel in wheels.values():
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(destination)

    monkeypatch.setattr(builder_module, "install_locked_requirements", install)
    options = dict(dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"])
    _builder(tmp_path, monkeypatch, context, "cold", **options).build()
    _builder(tmp_path, monkeypatch, context, "warm", **options).build()
    assert len(calls) == 1

    if change == "installer":
        monkeypatch.setattr(dependency_cache_module, "INSTALLER_BACKEND", "pip-target-v1")
    else:
        original = dependency_cache_module._target_marker_environment
        monkeypatch.setattr(
            dependency_cache_module, "_target_marker_environment",
            lambda *args: {**original(*args), "implementation_version": "3.11.99"},
        )

    _builder(tmp_path, monkeypatch, context, "changed", **options).build()
    assert len(calls) == 2
    assert all(call["base_image_tag_verified"] for call in calls)


@pytest.mark.parametrize("base_image", ["oci:local-base:python:3.11-slim", "python:3.11-slim@sha256:" + "a" * 64])
def test_grouping_does_not_infer_local_or_digest_target_from_tag(
    tmp_path, monkeypatch, indexes, base_image
):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    builder = _builder(
        tmp_path, monkeypatch, context, "unverified",
        base_image=base_image, dependency_layer_strategy="explicit",
    )
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, *_args: ([], {"os": "linux", "architecture": "amd64"}),
    )
    monkeypatch.setattr(
        builder_module, "install_locked_requirements",
        lambda *_args, **_kwargs: pytest.fail("unverified target invoked pip"),
    )
    with pytest.raises(ValueError, match="PYTHON_IMPLEMENTATION|PYTHON_VERSION"):
        builder.build()


def test_grouping_fails_closed_for_requirements_sources(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n", encoding="utf-8")
    (context / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
    builder = _builder(
        tmp_path, monkeypatch, context, "requirements",
        dependency_layer_strategy="explicit", stable_dependencies=["example"],
    )
    with pytest.raises((ValueError, RuntimeError), match="(?i)(lock|requirements)"):
        builder.build()
    dry_run = _builder(
        tmp_path, monkeypatch, context, "dry-run", dry_run=True,
        dependency_layer_strategy="explicit", stable_dependencies=["example"],
    )
    with pytest.raises(ValueError, match="require uv.lock"):
        dry_run.build()


def test_grouping_validates_sources_and_configuration_before_pulling_base(
    tmp_path, monkeypatch
):
    with pytest.raises(ValueError, match="both stable and frequent"):
        BuildConfig(
            include_deps=True, dependency_layer_strategy="explicit",
            stable_dependencies=["Example_Package"], frequent_dependencies=["example-package"],
        )
    with pytest.raises(ValueError, match="require include_deps"):
        BuildConfig(dependency_layer_strategy="explicit")
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n", encoding="utf-8")
    builder = _builder(
        tmp_path, monkeypatch, context, "venv",
        dependency_layer_strategy="explicit",
    )
    monkeypatch.setattr(ImageBuilder, "_dependency_source", lambda self: ("venv", []))
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda *_args: pytest.fail("unsupported grouping must fail before a registry pull"),
    )
    with pytest.raises(ValueError, match="virtualenv grouping"):
        builder.build()


@pytest.mark.parametrize("lockfile", ["poetry.lock", "Pipfile.lock", "pdm.lock"])
def test_grouping_rejects_other_lock_formats_before_pulling_base(
    tmp_path, monkeypatch, lockfile
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n", encoding="utf-8")
    (context / lockfile).write_text("", encoding="utf-8")
    builder = _builder(
        tmp_path, monkeypatch, context, "unsupported-lock",
        dependency_layer_strategy="explicit",
    )
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda *_args: pytest.fail("unsupported grouping must fail before a registry pull"),
    )
    with pytest.raises(ValueError, match="other lock formats"):
        builder.build()


def test_grouping_rebuilds_corrupt_group_and_reuses_the_other(
    tmp_path, monkeypatch, indexes, capsys
):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    options = dict(
        dependency_layer_strategy="explicit",
        stable_dependencies=["stable-dep"],
        frequent_dependencies=["frequent-dep"],
    )
    first = _builder(tmp_path, monkeypatch, context, "first", **options)
    first.build()
    stable_digest = first.layers[0].digest
    cache_blob = tmp_path / "cache" / "blobs" / "sha256" / stable_digest.split(":", 1)[1]
    cache_blob.write_bytes(b"corrupt")
    capsys.readouterr()

    rebuilt = _builder(tmp_path, monkeypatch, context, "rebuilt", **options)
    rebuilt.build()

    assert [layer.digest for layer in rebuilt.layers] == [layer.digest for layer in first.layers]
    output = capsys.readouterr().out.lower()
    assert "stable blob is missing or invalid" in output
    assert "group cache hit (frequent" in output
    assert f"sha256:{hashlib.sha256(cache_blob.read_bytes()).hexdigest()}" == stable_digest


@pytest.mark.parametrize(("changed_lock", "damage"), [(False, "corrupt"), (True, "missing")])
def test_group_cache_recovers_when_verified_blob_changes_during_reuse(
    tmp_path, monkeypatch, indexes, changed_lock, damage
):
    context, wheels, index = _fixture(tmp_path, indexes)
    options = dict(dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"])
    first = _builder(tmp_path, monkeypatch, context, "before-race", **options)
    first.build()
    stable_digest = first.layers[0].digest
    if changed_lock:
        wheels["frequent-dep"] = _wheel(
            context / "wheelhouse", "frequent-dep", "2.0", requires=("shared-dep>=1.0",)
        )
        index.add(wheels["frequent-dep"])
        _write_project(context, index.url, wheels, "2.0")

    original = ImageBuilder._publish_cached_layer
    interrupted = []

    def damage_verified_blob(builder, layers_dir, digest, cache_path, label):
        if label == "deps-group-cache" and digest == stable_digest and not interrupted:
            interrupted.append(True)
            if damage == "missing":
                cache_path.unlink()
            else:
                cache_path.write_bytes(b"corrupt")
        return original(builder, layers_dir, digest, cache_path, label)

    monkeypatch.setattr(ImageBuilder, "_publish_cached_layer", damage_verified_blob)
    rebuilt = _builder(tmp_path, monkeypatch, context, "after-race", **options)
    rebuilt.build()

    assert interrupted
    assert rebuilt.layers[0].digest == stable_digest
    blob = tmp_path / "cache" / "blobs" / "sha256" / stable_digest.split(":", 1)[1]
    assert f"sha256:{hashlib.sha256(blob.read_bytes()).hexdigest()}" == stable_digest


def test_interrupted_group_bundle_publication_is_a_miss(
    tmp_path, monkeypatch, indexes, capsys
):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    options = dict(
        dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"]
    )
    original = LayerCache.store_bytes

    def interrupt(cache, key, content, source_files):
        if key.startswith("deps-group-bundle-"):
            raise OSError("interrupted bundle publication")
        return original(cache, key, content, source_files)

    monkeypatch.setattr(LayerCache, "store_bytes", interrupt)
    with pytest.raises(OSError, match="interrupted bundle"):
        _builder(tmp_path, monkeypatch, context, "interrupted", **options).build()
    monkeypatch.setattr(LayerCache, "store_bytes", original)
    capsys.readouterr()

    recovered = _builder(tmp_path, monkeypatch, context, "recovered", **options)
    recovered.build()
    output = capsys.readouterr().out.lower()
    assert "no complete verified bundle" in output
    assert "group cache hit (stable" in output
    assert "group cache hit (frequent" in output
    assert len(recovered.layers) == 3


def test_group_bundle_with_mismatched_blob_binding_is_rebuilt(
    tmp_path, monkeypatch, indexes, capsys
):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    options = dict(dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"])
    first = _builder(tmp_path, monkeypatch, context, "first", **options)
    first.build()
    cache = LayerCache(tmp_path / "cache")
    index = json.loads((tmp_path / "cache" / "index.json").read_text(encoding="utf-8"))
    key = next(key for key in index if key.startswith("deps-group-bundle-"))
    bundle = json.loads(cache.get_bytes(key, 1024 * 1024))
    bundle["layers"][0]["digest"] = "sha256:" + "0" * 64
    cache.store_bytes(key, json.dumps(bundle).encode(), ["uv.lock"])
    capsys.readouterr()

    rebuilt = _builder(tmp_path, monkeypatch, context, "rebuilt", **options)
    rebuilt.build()

    assert [layer.digest for layer in rebuilt.layers] == [layer.digest for layer in first.layers]
    output = capsys.readouterr().out.lower()
    assert "stable blob is missing or invalid" in output
    assert "group cache hit (stable" in output
    assert "group cache hit (frequent" in output


def _concurrent_build(context, output, cache, barrier, queue):
    try:
        ImageBuilder._pull_base_image = lambda self, *_args: ([], BASE_CONFIG)
        builder = ImageBuilder(
            BuildConfig(
                context_dir=context, output_dir=output, cache_dir=cache,
                include_paths=["app.py"], include_deps=True, generate_sbom=True,
                dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"],
            )
        )
        barrier.wait(timeout=45)
        with contextlib.redirect_stdout(io.StringIO()):
            builder.build()
        queue.put(("ok", [layer.digest for layer in builder.layers]))
    except Exception as error:
        queue.put(("error", repr(error)))
        raise


def test_concurrent_group_writers_publish_verified_cache(
    tmp_path, monkeypatch, indexes
):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    spawn = multiprocessing.get_context("spawn")
    barrier = spawn.Barrier(2)
    queue = spawn.Queue()
    processes = [
        spawn.Process(
            target=_concurrent_build,
            args=(str(context), str(tmp_path / f"writer-{number}"), str(tmp_path / "cache"), barrier, queue),
        )
        for number in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=90)
        if process.is_alive():
            process.terminate()
            process.join()
        assert process.exitcode == 0
    results = [queue.get(timeout=10) for _ in processes]
    assert all(status == "ok" for status, _ in results)
    assert results[0][1] == results[1][1]
    final = _builder(
        tmp_path, monkeypatch, context, "after-concurrent",
        dependency_layer_strategy="explicit", stable_dependencies=["stable-dep"],
    )
    monkeypatch.setattr(
        builder_module, "install_locked_requirements",
        lambda *_args, **_kwargs: pytest.fail("concurrent writers did not publish a reusable bundle"),
    )
    final.build()
    assert [layer.digest for layer in final.layers] == results[0][1]


def _git(context, *args):
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(context), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _history_repository(context):
    _git(context, "init", "-b", "main")
    _git(context, "config", "--local", "user.name", "Fixture")
    _git(context, "config", "--local", "user.email", "fixture@example.invalid")
    _git(context, "add", "--", "uv.lock", "pyproject.toml", "app.py")
    _git(
        context, "commit", "-m", "test: first lock snapshot", "-m",
        "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
    )


def test_history_groups_use_an_immutable_snapshot_and_explicit_overrides(
    tmp_path, monkeypatch, indexes, capsys
):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    lock = context / "uv.lock"
    current = lock.read_text(encoding="utf-8")
    lock.write_text(
        current.replace('name = "frequent-dep"\nversion = "1.0"', 'name = "frequent-dep"\nversion = "0.9"'),
        encoding="utf-8",
    )
    _history_repository(context)
    lock.write_text(current, encoding="utf-8")
    _git(context, "add", "--", "uv.lock")
    _git(
        context, "commit", "-m", "test: second lock snapshot", "-m",
        "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
    )
    head = _git(context, "rev-parse", "HEAD")
    grouped = _builder(
        tmp_path, monkeypatch, context, "history",
        dependency_layer_strategy="history",
        frequent_dependencies=["stable-dep"],
    )

    grouped.build()

    output = capsys.readouterr().out
    assert f"Dependency history snapshot: {head}" in output
    assert "stable-dep -> frequent (explicit frequent assignment)" in output
    assert "frequent-dep -> frequent (historical version set changed)" in output
    assert "shared-dep -> stable (unchanged in 2 lockfile snapshots)" in output
    assert "app/.python/site-packages/shared_dep/__init__.py" in _layer_files([grouped.layers[0]])
    assert "app/.python/site-packages/frequent_dep/__init__.py" in _layer_files([grouped.layers[1]])


def test_history_requires_complete_clean_history(tmp_path, monkeypatch, indexes):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    options = dict(dependency_layer_strategy="history")
    with pytest.raises(ValueError, match="Dependency history"):
        _builder(tmp_path, monkeypatch, context, "no-repository", **options).build()

    _history_repository(context)
    with pytest.raises(ValueError, match="at least two"):
        _builder(tmp_path, monkeypatch, context, "one-snapshot", **options).build()

    (context / "uv.lock").write_text(
        (context / "uv.lock").read_text(encoding="utf-8") + "\n# uncommitted\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unmodified lockfile"):
        _builder(tmp_path, monkeypatch, context, "dirty-lock", **options).build()

    _git(context, "add", "--", "uv.lock")
    _git(
        context, "commit", "-m", "test: second snapshot", "-m",
        "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
    )
    _git(context, "config", "--local", "remote.origin.promisor", "true")
    with pytest.raises(ValueError, match="partial or promisor"):
        _builder(tmp_path, monkeypatch, context, "partial", **options).build()
    _git(context, "config", "--local", "--unset", "remote.origin.promisor")
    (context / ".git" / "shallow").write_text(_git(context, "rev-parse", "HEAD") + "\n")
    with pytest.raises(ValueError, match="non-shallow"):
        _builder(tmp_path, monkeypatch, context, "shallow", **options).build()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_history_rejects_lock_with_hidden_worktree_changes(tmp_path, monkeypatch, indexes, flag):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    _history_repository(context)
    lock = context / "uv.lock"
    lock.write_text(lock.read_text(encoding="utf-8") + "\n# second snapshot\n", encoding="utf-8")
    _git(context, "add", "--", "uv.lock")
    _git(
        context, "commit", "-m", "test: second snapshot", "-m",
        "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
    )
    _git(context, "update-index", flag, "--", "uv.lock")
    lock.write_text(lock.read_text(encoding="utf-8") + "\n# hidden edit\n", encoding="utf-8")
    assert not _git(context, "status", "--porcelain", "--", "uv.lock")
    monkeypatch.setattr(
        builder_module, "install_locked_requirements",
        lambda *_args, **_kwargs: pytest.fail("hidden lock changes reached dependency installation"),
    )

    with pytest.raises(ValueError, match="unmodified lockfile"):
        _builder(
            tmp_path, monkeypatch, context, "hidden-history",
            dependency_layer_strategy="history",
        ).build()


def test_history_dry_run_checks_evidence_without_staging(tmp_path, monkeypatch, indexes, capsys):
    context, _wheels, _index = _fixture(tmp_path, indexes)
    options = dict(dependency_layer_strategy="history", dry_run=True)
    with pytest.raises(ValueError, match="Dependency history"):
        _builder(tmp_path, monkeypatch, context, "dry-no-repository", **options).build()

    _history_repository(context)
    with pytest.raises(ValueError, match="at least two"):
        _builder(tmp_path, monkeypatch, context, "dry-one-snapshot", **options).build()

    lock = context / "uv.lock"
    lock.write_text(lock.read_text(encoding="utf-8") + "\n# second snapshot\n", encoding="utf-8")
    _git(context, "add", "--", "uv.lock")
    _git(
        context, "commit", "-m", "test: second snapshot", "-m",
        "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>",
    )
    snapshot = _git(context, "rev-parse", "HEAD")
    monkeypatch.setattr(
        builder_module, "install_locked_requirements",
        lambda *_args, **_kwargs: pytest.fail("dry-run staged dependencies"),
    )
    _builder(tmp_path, monkeypatch, context, "dry-valid", **options).build()
    assert snapshot in capsys.readouterr().out
