import json
import sys
import tarfile
from pathlib import Path

import pytest

from pycontainer import cli
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.dependencies import staged_packages, strip_type_stubs


BASE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {"Env": ["PYTHON_VERSION=3.11.8"], "WorkingDir": "/"},
    "rootfs": {"type": "layers", "diff_ids": []},
}


def _distribution(site, info_name, name, files, *, record_lines=None):
    info = site / info_name
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n\n"
    )
    for relative, content in files.items():
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    lines = record_lines or [f"{path},sha256=x,1\n" for path in files] + [
        f"{info_name}/METADATA,sha256=x,1\n",
        f"{info_name}/RECORD,,\n",
    ]
    (info / "RECORD").write_text("".join(lines), newline="")


def _site_packages(site):
    _distribution(
        site,
        "types_requests-1.0.dist-info",
        "types-requests",
        {
            "requests-stubs/__init__.pyi": "def get() -> None: ...\n",
            "requests-stubs/METADATA.toml": "version = '1.0'\n",
            "shared/py.typed": "",
        },
    )
    _distribution(
        site,
        "runtime_dep-1.0.dist-info",
        "runtime-dep",
        {
            "runtime_dep/__init__.py": "value = 1\n",
            "runtime_dep/__init__.pyi": "value: int\n",
            "shared/py.typed": "",
        },
    )
    _distribution(
        site,
        "Django_Stubs-1.0.dist-info",
        "Django_Stubs",
        {
            "django-stubs/__init__.pyi": "",
            "mypy_django_plugin/__init__.py": "plugin = None\n",
        },
    )


def _snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_strip_type_stubs_removes_stub_distributions_and_rewrites_records(tmp_path):
    site = tmp_path / "site-packages"
    _site_packages(site)
    runtime_record = site / "runtime_dep-1.0.dist-info" / "RECORD"
    original = runtime_record.read_bytes()

    assert strip_type_stubs(site) == ["types-requests"]

    files = set(_snapshot(site))
    assert not [name for name in files if name.endswith(".pyi")]
    assert not (site / "requests-stubs").exists()
    assert not (site / "types_requests-1.0.dist-info").exists()
    assert "shared/py.typed" in files
    assert "mypy_django_plugin/__init__.py" in files
    assert "runtime_dep/__init__.py" in files
    assert runtime_record.read_bytes() == original.replace(
        b"runtime_dep/__init__.pyi,sha256=x,1\n", b""
    )
    django_record = (site / "Django_Stubs-1.0.dist-info" / "RECORD").read_text()
    assert ".pyi" not in django_record and "mypy_django_plugin" in django_record
    assert [name for name, _ in staged_packages(site)] == ["Django_Stubs", "runtime-dep"]


def test_strip_type_stubs_normalizes_shared_paths_and_keeps_external_launchers(tmp_path):
    site = tmp_path / "site-packages"
    _site_packages(site)
    record = site / "runtime_dep-1.0.dist-info" / "RECORD"
    record.write_text(
        ".\\shared\\py.typed,,\n../../Scripts/runtime.exe,sha256=x,1\n"
        "..\\..\\..\\bin\\runtime,sha256=x,1\n"
        "runtime_dep/__init__.pyi,,\r\n",
        newline="",
    )

    strip_type_stubs(site)

    assert (site / "shared" / "py.typed").is_file()
    assert record.read_bytes() == (
        b".\\shared\\py.typed,,\n../../Scripts/runtime.exe,sha256=x,1\n"
        b"..\\..\\..\\bin\\runtime,sha256=x,1\n"
    )


@pytest.mark.parametrize(
    "launcher",
    ["../../Scripts/plugin.exe", "../../../bin/plugin", "..\\..\\Scripts\\plugin.exe"],
)
def test_strip_type_stubs_keeps_stub_named_distribution_with_runtime_launcher(tmp_path, launcher):
    site = tmp_path / "site-packages"
    _distribution(
        site,
        "plugin_stubs-1.0.dist-info",
        "plugin-stubs",
        {"plugin/__init__.py": "value = 1\n", "plugin/__init__.pyi": "value: int\n"},
        record_lines=[
            f"{launcher},sha256=x,1\r\n",
            "plugin/__init__.py,sha256=x,1\n",
            "plugin/__init__.pyi,sha256=x,1\n",
            "plugin_stubs-1.0.dist-info/METADATA,sha256=x,1\n",
            "plugin_stubs-1.0.dist-info/RECORD,,\n",
        ],
    )
    record = site / "plugin_stubs-1.0.dist-info" / "RECORD"

    assert strip_type_stubs(site) == []

    assert (site / "plugin" / "__init__.py").is_file()
    assert not (site / "plugin" / "__init__.pyi").exists()
    assert record.read_bytes().startswith(f"{launcher},sha256=x,1\r\n".encode())
    assert b".pyi" not in record.read_bytes()
    assert staged_packages(site) == [("plugin-stubs", "1.0")]


@pytest.mark.parametrize(
    ("info_name", "name", "record_lines", "message"),
    [
        ("runtime-1.0.dist-info", "runtime", ["runtime.py,sha256=x\n"], "Malformed RECORD"),
        ("runtime-1.0.dist-info", "runtime", ['runtime.py,sha256=x,"1\n'], "Malformed RECORD"),
        ("runtime-1.0.dist-info", "runtime", ["runtime\0.py,,\n"], "Malformed RECORD"),
        ("runtime-1.0.dist-info", "runtime", ["../../outside.pyi,,\n"], "escapes"),
        ("runtime-1.0.dist-info", "runtime", ["../../Scripts/x.pyi,,\n"], "escapes"),
        ("runtime-1.0.dist-info", "runtime", ["../../Scripts/x.PYI,,\n"], "escapes"),
        ("runtime-1.0.dist-info", "runtime", ["../../../share/x/data.txt,,\n"], "escapes"),
        ("runtime-1.0.dist-info", "runtime", ["../../Scripts/../../x.exe,,\n"], "escapes"),
        ("runtime-1.0.dist-info", "runtime", ["/etc/runtime.conf,,\n"], "escapes"),
        ("runtime-1.0.dist-info", "runtime", ["\\\\server\\share\\x.py,,\n"], "escapes"),
        ("runtime-1.0.dist-info", "runtime", ["D:/outside/x.py,,\n"], "escapes"),
        ("types_x-1.0.dist-info", "types-x", ["C:\\outside\\x.py,,\n"], "escapes"),
        ("x_stubs-1.0.dist-info", "x-stubs", ["x-stubs/../../y.pyi,,\n"], "escapes"),
    ],
)
def test_strip_type_stubs_rejects_invalid_records_without_mutation(
    tmp_path, info_name, name, record_lines, message
):
    site = tmp_path / "site-packages"
    _site_packages(site)
    _distribution(site, info_name, name, {"runtime.py": ""}, record_lines=record_lines)
    before = _snapshot(site)

    with pytest.raises(ValueError, match=message):
        strip_type_stubs(site)

    assert _snapshot(site) == before


def test_strip_type_stubs_rejects_stub_distribution_without_record(tmp_path):
    site = tmp_path / "site-packages"
    _site_packages(site)
    (site / "types_requests-1.0.dist-info" / "RECORD").unlink()
    before = _snapshot(site)

    with pytest.raises(ValueError, match="without a RECORD"):
        strip_type_stubs(site)

    assert _snapshot(site) == before


def _builder(tmp_path, monkeypatch, context, **options):
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
            include_deps=True,
            generate_sbom=True,
            **options,
        )
    )


def _layer_names(builder):
    with tarfile.open(builder.layers[0].tar_path) as archive:
        return archive.getnames()


def test_builder_strip_type_stubs_is_opt_in_reproducible_and_updates_sbom(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _site_packages(context / ".venv" / "Lib" / "site-packages")
    (context / "app.py").write_text("print('app')\n")

    default = _builder(tmp_path / "default", monkeypatch, context)
    explicit_off = _builder(
        tmp_path / "off", monkeypatch, context, strip_type_stubs=False
    )
    first = _builder(tmp_path / "first", monkeypatch, context, strip_type_stubs=True)
    second = _builder(tmp_path / "second", monkeypatch, context, strip_type_stubs=True)
    for builder in (default, explicit_off, first, second):
        builder.build()

    prefix = "app/.python/site-packages/"
    assert f"{prefix}runtime_dep/__init__.pyi" in _layer_names(default)
    assert default.layers[0].digest == explicit_off.layers[0].digest
    assert default.manifest_digest == explicit_off.manifest_digest
    stripped = _layer_names(first)
    assert not [name for name in stripped if name.endswith(".pyi")]
    assert not [name for name in stripped if "types_requests" in name]
    assert f"{prefix}shared/py.typed" in stripped
    assert first.layers[0].digest == second.layers[0].digest
    assert first.layers[0].digest != default.layers[0].digest

    def sbom_names(builder):
        sbom = json.loads((Path(builder.config.output_dir) / "sbom.json").read_text())
        return [package["name"] for package in sbom["packages"]]

    assert "types-requests" in sbom_names(default)
    assert sbom_names(first) == ["Django_Stubs", "runtime-dep"]


def test_cli_and_config_enable_strip_type_stubs(tmp_path, monkeypatch):
    configs = []

    class FakeImageBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", FakeImageBuilder)
    config_path = tmp_path / "pycontainer.toml"
    config_path.write_text("[build]\nstrip-type-stubs = true\n")
    for argv in (["build"], ["build", "--strip-type-stubs"], ["build", "--config", str(config_path)]):
        monkeypatch.setattr(sys, "argv", ["pycontainer", *argv])
        cli.main()

    assert [config.strip_type_stubs for config in configs] == [False, True, True]
