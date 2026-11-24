import errno
import json
import logging
import shutil
import sys
import tarfile
import traceback
from pathlib import Path

import pytest

from pycontainer import cli
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.dependencies import staged_packages, strip_type_stubs


BASE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {"Env": ["PYTHON_IMPLEMENTATION=CPython", "PYTHON_VERSION=3.11.8", "PYTHON_LIBC=glibc-2.17"], "WorkingDir": "/"},
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


def test_builder_redacts_build_argument_values_in_removed_stub_names(
    tmp_path, monkeypatch, capsys
):
    context = tmp_path / "context"
    site = context / ".venv" / "Lib" / "site-packages"
    _site_packages(site)
    _distribution(
        site, "types_six-1.0.dist-info", "types-six", {"six-stubs/__init__.pyi": ""}
    )
    (context / "app.py").write_text("print('app')\n")

    _builder(tmp_path / "plain", monkeypatch, context, strip_type_stubs=True).build()
    assert "Removed type stub distributions: types-requests, types-six" in (
        capsys.readouterr().out
    )

    _builder(
        tmp_path / "redacted", monkeypatch, context,
        strip_type_stubs=True, build_args={"STUB": "types-requests"},
    ).build()
    output = capsys.readouterr().out
    assert "types-requests" not in output
    assert "Removed type stub distributions: <redacted>, types-six" in output


def _stub_builder(tmp_path, monkeypatch, context, value, retained):
    builder = _builder(
        tmp_path / "build", monkeypatch, context,
        strip_type_stubs=True, build_args={"STUB": value},
    )
    if retained:
        # Resolved values stay redacted after the public field is cleared.
        builder.config.build_args = None
    return builder


@pytest.mark.parametrize("retained", [False, True])
def test_builder_redacts_build_argument_values_in_kept_stub_warning(
    tmp_path, monkeypatch, caplog, retained
):
    context = tmp_path / "context"
    _site_packages(context / ".venv" / "Lib" / "site-packages")
    (context / "app.py").write_text("print('app')\n")
    builder = _stub_builder(tmp_path, monkeypatch, context, "Django_Stubs", retained)

    with caplog.at_level(logging.WARNING):
        builder.build()

    assert "Keeping <redacted>-1.0.dist-info" in caplog.text
    assert "Django_Stubs" not in caplog.text


@pytest.mark.parametrize("retained", [False, True])
def test_builder_redacts_build_argument_values_in_stub_errors(
    tmp_path, monkeypatch, retained
):
    context = tmp_path / "context"
    site = context / ".venv" / "Lib" / "site-packages"
    _site_packages(site)
    (site / "types_requests-1.0.dist-info" / "RECORD").unlink()
    (context / "app.py").write_text("print('app')\n")
    builder = _stub_builder(tmp_path, monkeypatch, context, "types_requests", retained)

    with pytest.raises(ValueError, match="without a RECORD: <redacted>") as error:
        builder.build()

    formatted = "".join(traceback.format_exception(error.value))
    assert "types_requests" not in formatted
    assert error.value.__cause__ is None and error.value.__context__ is None


@pytest.mark.parametrize("retained", [False, True])
def test_builder_redacts_build_argument_values_in_stub_os_errors(
    tmp_path, monkeypatch, retained
):
    context = tmp_path / "context"
    _site_packages(context / ".venv" / "Lib" / "site-packages")
    (context / "app.py").write_text("print('app')\n")
    builder = _stub_builder(tmp_path, monkeypatch, context, "types_requests", retained)
    real_rmtree = shutil.rmtree

    def fail(path, *args, **kwargs):
        if not str(path).endswith(".dist-info"):
            return real_rmtree(path, *args, **kwargs)
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr("pycontainer.dependencies.shutil.rmtree", fail)
    with pytest.raises(PermissionError, match="<redacted>-1.0.dist-info") as error:
        builder.build()

    assert error.value.errno == 13
    assert error.value.strerror == "Access is denied"
    assert error.value.filename.endswith("<redacted>-1.0.dist-info")
    assert "types_requests" not in "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None and error.value.__context__ is None


@pytest.mark.parametrize("build_args", [None, {"STUB": "unrelated-value"}])
def test_builder_keeps_stub_error_cause_without_values_to_redact(
    tmp_path, monkeypatch, build_args
):
    context = tmp_path / "context"
    site = context / ".venv" / "Lib" / "site-packages"
    _site_packages(site)
    (site / "types_requests-1.0.dist-info" / "RECORD").write_bytes(b"\xff\xfe")
    (context / "app.py").write_text("print('app')\n")
    builder = _builder(
        tmp_path / "build", monkeypatch, context,
        strip_type_stubs=True, build_args=build_args,
    )

    with pytest.raises(ValueError) as error:
        builder.build()

    assert isinstance(error.value.__cause__, UnicodeDecodeError)


def test_builder_keeps_characters_written_when_redacting_stub_os_errors(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    _site_packages(context / ".venv" / "Lib" / "site-packages")
    (context / "app.py").write_text("print('app')\n")
    builder = _stub_builder(tmp_path, monkeypatch, context, "types_requests", False)
    real_rmtree = shutil.rmtree

    def fail(path, *args, **kwargs):
        if not str(path).endswith(".dist-info"):
            return real_rmtree(path, *args, **kwargs)
        raise BlockingIOError(errno.EAGAIN, f"partial write {path}", 7)

    monkeypatch.setattr("pycontainer.dependencies.shutil.rmtree", fail)
    with pytest.raises(BlockingIOError) as error:
        builder.build()

    assert error.value.errno == errno.EAGAIN
    assert error.value.characters_written == 7
    assert error.value.strerror.endswith("<redacted>-1.0.dist-info")
    assert "types_requests" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("regenerated_field", ["message", "attributes"])
def test_builder_rejects_stub_error_constructors_that_regenerate_build_values(
    tmp_path, monkeypatch, regenerated_field
):
    context = tmp_path / "context"
    _site_packages(context / ".venv" / "Lib" / "site-packages")
    (context / "app.py").write_text("print('app')\n")
    builder = _stub_builder(tmp_path, monkeypatch, context, "synthetic-secret", False)
    real_rmtree = shutil.rmtree

    class RegeneratingError(OSError):
        def __init__(self, *args):
            if regenerated_field == "message":
                super().__init__("synthetic-secret")
            else:
                super().__init__(13, "synthetic-secret", "synthetic-secret")
                self.filename2 = "synthetic-secret"
                self.detail = "synthetic-secret"

    def fail(path, *args, **kwargs):
        if not str(path).endswith(".dist-info"):
            return real_rmtree(path, *args, **kwargs)
        raise RegeneratingError()

    monkeypatch.setattr("pycontainer.dependencies.shutil.rmtree", fail)
    with pytest.raises(OSError) as caught:
        builder.build()

    error = caught.value
    assert not isinstance(error, RegeneratingError)
    assert "synthetic-secret" not in "".join(traceback.format_exception(error))
    assert "synthetic-secret" not in repr(error.args)
    assert "synthetic-secret" not in repr(vars(error))
    assert not hasattr(error, "detail")
    assert error.__cause__ is None and error.__context__ is None
    if regenerated_field == "attributes":
        assert error.errno == 13
        assert error.strerror == error.filename == error.filename2 == "<redacted>"


@pytest.mark.parametrize(
    "hidden_field", ["filename", "args", "detail", "slot_detail", "cause"]
)
def test_builder_redacts_stub_error_fields_hidden_from_formatted_tracebacks(
    tmp_path, monkeypatch, hidden_field
):
    context = tmp_path / "context"
    _site_packages(context / ".venv" / "Lib" / "site-packages")
    (context / "app.py").write_text("print('app')\n")
    builder = _stub_builder(tmp_path, monkeypatch, context, "synthetic-secret", False)
    real_rmtree = shutil.rmtree

    class HiddenError(OSError):
        __slots__ = ("slot_detail",)

        def __str__(self):
            return "permission denied"

    original = HiddenError(13, "permission denied")
    if hidden_field == "cause":
        original.__cause__ = HiddenError()
        original.__cause__.filename = "synthetic-secret"
    else:
        setattr(
            original, hidden_field,
            ("synthetic-secret",) if hidden_field == "args" else "synthetic-secret",
        )
    assert "synthetic-secret" not in "".join(traceback.format_exception(original))

    def fail(path, *args, **kwargs):
        if not str(path).endswith(".dist-info"):
            return real_rmtree(path, *args, **kwargs)
        raise original

    monkeypatch.setattr("pycontainer.dependencies.shutil.rmtree", fail)
    with pytest.raises(OSError) as caught:
        builder.build()

    error = caught.value
    assert error is not original
    assert "synthetic-secret" not in repr(error.args)
    assert "synthetic-secret" not in repr(vars(error))
    assert error.filename != "synthetic-secret"
    assert "synthetic-secret" not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None and error.__context__ is None
    if hidden_field == "filename":
        assert error.errno == 13
        assert error.filename == "<redacted>"


@pytest.mark.parametrize("field", ["filename", "bytes_filename", "nested_detail"])
def test_builder_redacts_raw_stub_error_values_with_mixed_quotes(
    tmp_path, monkeypatch, field
):
    context = tmp_path / "context"
    _site_packages(context / ".venv" / "Lib" / "site-packages")
    (context / "app.py").write_text("print('app')\n")
    value = "synthetic'secret"
    filename = '/tmp/prefix"synthetic\'secret-suffix'
    builder = _stub_builder(tmp_path, monkeypatch, context, value, False)
    original = PermissionError(13, "permission denied", filename)
    if field == "bytes_filename":
        original.filename = filename.encode("utf-8") + b"\xff"
    if field == "nested_detail":
        original.filename = None
        original.detail = {"paths": [filename]}
    real_rmtree = shutil.rmtree

    def fail(path, *args, **kwargs):
        if not str(path).endswith(".dist-info"):
            return real_rmtree(path, *args, **kwargs)
        raise original

    monkeypatch.setattr("pycontainer.dependencies.shutil.rmtree", fail)
    with pytest.raises(PermissionError) as caught:
        builder.build()

    error = caught.value
    assert error is not original
    assert error.errno == 13
    assert error.strerror == "permission denied"
    assert value not in repr(vars(error))
    assert value not in "".join(traceback.format_exception(error))
    assert "synthetic\\'secret" not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None and error.__context__ is None
    if field == "filename":
        assert error.filename == '/tmp/prefix"<redacted>-suffix'
    if field == "bytes_filename":
        assert error.filename == '/tmp/prefix"<redacted>-suffix\udcff'


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
