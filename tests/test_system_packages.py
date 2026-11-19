import gzip
import io
import logging
import sys
import tarfile

import pytest

from pycontainer import cli
from pycontainer.config import BuildConfig
from pycontainer.config_loader import config_from_file
from pycontainer.oci import OCILayer
from pycontainer.system_packages import (
    detect_required_packages,
    inspect_base_layers,
    report_system_packages,
    validate_system_packages,
)

DPKG_STATUS = (
    "Package: libc6\nStatus: install ok installed\nVersion: 2.36\n\n"
    "Package: libpq5\nStatus: install ok installed\nVersion: 15\n\n"
    "Package: removed-pkg\nStatus: deinstall ok config-files\n\n"
)
APK_INSTALLED = "P:musl\nV:1.2\n\nP:libpq\nV:15\n\n"


def _layer(tmp_path, name, files, compress=False):
    path = tmp_path / name
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for member, content in files.items():
            info = tarfile.TarInfo(member)
            data = content.encode()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    raw = buffer.getvalue()
    path.write_bytes(gzip.compress(raw) if compress else raw)
    return OCILayer("application/vnd.oci.image.layer.v1.tar", "sha256:" + "0" * 64, 1, str(path))


def _debian_layers(tmp_path):
    return [
        _layer(tmp_path, "os", {"etc/os-release": "ID=debian\n"}),
        _layer(tmp_path, "dpkg", {"var/lib/dpkg/status": DPKG_STATUS}, compress=True),
    ]


def test_validate_accepts_and_dedupes_valid_names():
    assert validate_system_packages(["libpq5", "g++", "libpq5", "python3.11"]) == [
        "libpq5",
        "g++",
        "python3.11",
    ]


@pytest.mark.parametrize("bad", ["", "Lib PQ", "--help", "pkg;rm -rf /", "-x", 3])
def test_validate_rejects_invalid_names(bad):
    with pytest.raises(ValueError, match="system package"):
        validate_system_packages([bad])


def test_validate_rejects_non_list():
    with pytest.raises(ValueError, match="system_packages"):
        validate_system_packages("libpq5")


def test_detect_from_requirements_per_distro(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "# comment\nPsycopg2==2.9\nrequests>=2\nlxml[html_clean]; python_version>'3'\n"
    )
    deb = detect_required_packages(tmp_path, "requirements.txt", "debian")
    apk = detect_required_packages(tmp_path, "requirements.txt", "alpine")
    assert deb["libpq5"] == "psycopg2"
    assert "libxml2" in deb and "libxslt1.1" in deb
    assert apk["libpq"] == "psycopg2"
    assert "libxslt" in apk
    assert "requests" not in deb


def test_detect_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\ndependencies=["mysqlclient>=2", "psycopg2-binary"]\n'
    )
    found = detect_required_packages(tmp_path, "requirements.txt", "debian")
    assert found == {"libmariadb3": "mysqlclient"}


def test_inspect_base_layers_debian_and_alpine(tmp_path):
    family, installed = inspect_base_layers(_debian_layers(tmp_path))
    assert family == "debian"
    assert installed == {"libc6", "libpq5"}

    alpine = [
        _layer(tmp_path, "a1", {"etc/os-release": "ID=alpine\n"}),
        _layer(tmp_path, "a2", {"lib/apk/db/installed": APK_INSTALLED}),
    ]
    family, installed = inspect_base_layers(alpine)
    assert family == "alpine"
    assert installed == {"musl", "libpq"}


def test_inspect_unknown_base_returns_none(tmp_path):
    family, installed = inspect_base_layers([_layer(tmp_path, "x", {"a": "b"})])
    assert family is None
    assert installed == set()


def test_report_warns_only_about_missing_packages(tmp_path, caplog):
    config = BuildConfig(
        context_dir=str(tmp_path), system_packages=["libpq5", "libssl3"]
    )
    with caplog.at_level(logging.WARNING):
        report = report_system_packages(config, _debian_layers(tmp_path))
    assert report.missing == ["libssl3"]
    assert report.satisfied == ["libpq5"]
    assert "libssl3" in caplog.text
    assert "libpq5" not in caplog.text


def test_report_autodetects_and_respects_opt_out(tmp_path, caplog):
    (tmp_path / "requirements.txt").write_text("mysqlclient\n")
    config = BuildConfig(context_dir=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        report = report_system_packages(config, _debian_layers(tmp_path))
    assert report.missing == ["libmariadb3"]
    assert "mysqlclient" in caplog.text

    config.detect_system_packages = False
    assert report_system_packages(config, _debian_layers(tmp_path)).required == []


def test_report_with_unknown_distro_lists_without_claiming_missing(tmp_path, caplog):
    config = BuildConfig(context_dir=str(tmp_path), system_packages=["libpq5"])
    with caplog.at_level(logging.WARNING):
        report = report_system_packages(config, [_layer(tmp_path, "x", {"a": "b"})])
    assert report.missing == []
    assert report.unverified == ["libpq5"]
    assert "could not" in caplog.text.lower()


def test_report_noop_without_packages(tmp_path):
    config = BuildConfig(context_dir=str(tmp_path))
    assert report_system_packages(config, []).required == []


def test_config_file_and_cli_flags(tmp_path, monkeypatch):
    toml = tmp_path / "pycontainer.toml"
    toml.write_text('[build]\nsystem-packages = ["libpq5"]\n')
    assert config_from_file(toml).system_packages == ["libpq5"]

    configs = []

    class FakeBuilder:
        def __init__(self, config):
            configs.append(config)

        def build(self):
            return "layout"

    monkeypatch.setattr(cli, "ImageBuilder", FakeBuilder)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pycontainer", "build", "--base-image", "x", "--system-package", "libpq5",
         "--system-package", "libssl3", "--no-detect-system-packages"],
    )
    cli.main()
    assert configs[0].system_packages == ["libpq5", "libssl3"]
    assert configs[0].detect_system_packages is False


def test_build_config_rejects_invalid_system_package():
    with pytest.raises(ValueError, match="system package"):
        BuildConfig(system_packages=["bad name"])
