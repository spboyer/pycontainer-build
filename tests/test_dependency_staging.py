import base64
import csv
import gzip
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
from types import SimpleNamespace

import pytest

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
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
    options.setdefault("base_image", "python:3.11-slim-bookworm")
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
    config = {**BASE_CONFIG, "architecture": arch}
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
        "linux", "amd64", base_image, BASE_CONFIG, [layer],
        base_image_tag_verified=verified,
    )
    assert config is BASE_CONFIG
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
    with pytest.raises(ValueError, match="Cannot validate target") as error:
        builder._create_deps_layer(tmp_path / "layers", base_config=BASE_CONFIG)
    assert "python:3.11-slim-bookworm" in str(error.value)


@pytest.mark.parametrize("include_deps", [False, True])
def test_default_image_build_requires_libc_only_for_dependency_staging(
    tmp_path, monkeypatch, include_deps
):
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('app')\n")
    (context / "requirements.txt").write_text("# no dependencies\n")
    monkeypatch.setattr(
        ImageBuilder, "_pull_base_image",
        lambda self, layers_dir, os_name, arch: ([], BASE_CONFIG),
    )
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
        layers = tmp_path / "layers" / base_image.replace(":", "_")
        layers.mkdir(parents=True)
        config = BASE_CONFIG
        if base_image == "python:3.11-slim":
            config = {**BASE_CONFIG, **_libc_config("PYTHON_LIBC=glibc-2.17")}
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
    _write_venv(
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
        with pytest.raises(ValueError, match="extra 'uvloop' of venv-root"):
            _stage_venv(tmp_path, monkeypatch, context)
    else:
        assert _stage_venv(tmp_path, monkeypatch, context) is not None
