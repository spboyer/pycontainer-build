"""Opt-in acceptance test, not evidence of compatibility until actually run."""
import gzip
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from pycontainer.blob_store import hash_file
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.oci import OCILayer
from pycontainer.scanning import VulnerabilityFoundError


def _tar(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, text in files.items():
            data = text.encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


def _package(files, root, name, version):
    files[f"{root}/{name}-{version}.dist-info/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    )
    files[f"{root}/{name}-{version}.dist-info/WHEEL"] = (
        "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    files[f"{root}/{name}/__init__.py"] = ""


@pytest.mark.scanner_integration
@pytest.mark.parametrize("whiteouts,report_format", [(False, "json"), (True, "json"), (False, "sarif")])
def test_real_generated_layout_os_python_and_whiteouts(tmp_path, monkeypatch, whiteouts, report_format):
    required = ("PYCONTAINER_GRYPE", "PYCONTAINER_SCAN_DB_DIR", "PYCONTAINER_SCAN_DB_SHA256")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        pytest.fail("Explicit integration requires: " + ", ".join(missing))
    db_dir = Path(os.environ["PYCONTAINER_SCAN_DB_DIR"]).resolve()
    digest, _ = hash_file(db_dir / "6" / "vulnerability.db")
    assert digest == os.environ["PYCONTAINER_SCAN_DB_SHA256"], "controlled DB digest mismatch"

    lower = {
        "etc/os-release": 'ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian GNU/Linux 12"\n',
        "var/lib/dpkg/status": (
            "Package: openssl\nStatus: install ok installed\nArchitecture: amd64\n"
            "Version: 3.0.9-1\nDescription: controlled vulnerable OS package\n\n"
        ),
    }
    site = "usr/local/lib/python3.11/site-packages"
    _package(lower, site, "urllib3", "1.24.1")
    _package(lower, "opaque/site-packages", "jinja2", "2.10")
    lower_tar = _tar(lower)
    upper_tar = _tar({
        f"{site}/.wh.urllib3-1.24.1.dist-info": "",
        f"{site}/.wh.urllib3": "",
        "opaque/site-packages/.wh..wh..opq": "",
    })

    def base(self, layers_dir, *args):
        layers, diff_ids = [], []
        for data in ((lower_tar, upper_tar) if whiteouts else (lower_tar,)):
            compressed = gzip.compress(data, mtime=0)
            blob_digest = "sha256:" + hashlib.sha256(compressed).hexdigest()
            path = layers_dir / blob_digest.split(":")[1]
            path.write_bytes(compressed)
            layers.append(OCILayer("application/vnd.oci.image.layer.v1.tar+gzip",
                                   blob_digest, len(compressed), str(path)))
            diff_ids.append("sha256:" + hashlib.sha256(data).hexdigest())
        return layers, {
            "architecture": "amd64", "os": "linux",
            "config": {"Env": ["PYTHON_VERSION=3.11.0", "PYTHON_IMPLEMENTATION=CPython",
                               "PYTHON_LIBC=glibc"], "WorkingDir": "/"},
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        }

    monkeypatch.setattr(ImageBuilder, "_pull_base_image", base)
    context = tmp_path / "context"
    context.mkdir()
    (context / "app.py").write_text("print('integration fixture')\n")
    (context / "requirements.txt").write_text("# staged fixture only\n")
    inventory = {}
    _package(inventory, ".venv/lib/python3.11/site-packages", "requests", "2.19.1")
    for name, text in inventory.items():
        path = context.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    builder = ImageBuilder(BuildConfig(
        context_dir=str(context), output_dir=str(tmp_path / "output"),
        include_paths=["app.py"], include_deps=True, use_cache=False,
        scan=True, scan_executable=os.environ["PYCONTAINER_GRYPE"],
        scan_db_dir=str(db_dir), scan_offline=True, scan_report_format=report_format,
        scan_db_max_age_hours=int(os.getenv("PYCONTAINER_SCAN_DB_MAX_AGE_HOURS", "120")),
    ))
    with pytest.raises(VulnerabilityFoundError):
        builder.build()
    report = json.loads((tmp_path / "output" / f"vulnerabilities.{report_format}").read_text())
    if report_format == "sarif":
        run = report["runs"][0]
        packages = {result["properties"]["package"]["name"].lower() for result in run["results"]}
        metadata = run["properties"]
        assert run["invocations"][0]["executionSuccessful"] is True
    else:
        packages = {match["artifact"]["name"].lower() for match in report["matches"]}
        metadata = report["pycontainer"]
        assert report["source"]["target"]["manifestDigest"] == builder.manifest_digest
    assert {"openssl", "requests"} <= packages, "controlled DB must match OS and staged Python fixtures"
    if whiteouts:
        assert "urllib3" not in packages and "jinja2" not in packages, "whiteouted packages must not be scanned"
    else:
        assert {"urllib3", "jinja2"} <= packages, "control must prove the removed packages were detectable"
    assert metadata["database"]["digest"] == digest
    assert metadata["image_digest"] == builder.manifest_digest
    assert metadata["scanner"]["version"] == "0.119.0" and metadata["scan_status"] == "complete"
