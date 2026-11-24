"""Verify installed files against the hashes extracted from locked wheels."""
import hashlib
import importlib
from importlib import metadata
import json
from pathlib import Path


expected = json.loads(Path(__file__).with_name("expected.json").read_text())
for package in expected:
    distribution = metadata.distribution(package["name"])
    if distribution.metadata["Name"] != package["name"] or distribution.version != package["version"]:
        raise RuntimeError(f"installed distribution does not match {package['name']} {package['version']}")
    module = importlib.import_module(package["module"])
    module_file = Path(module.__file__).resolve()
    if module_file != Path(distribution.locate_file(package["module_path"])).resolve():
        raise RuntimeError(f"imported module path does not match {package['name']}")
    metadata_file = Path(distribution.locate_file(package["metadata_path"]))
    for path, digest in (
        (module_file, package["module_sha256"]),
        (metadata_file, package["metadata_sha256"]),
    ):
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"installed file hash mismatch: {path}")
print("pycontainer-lockfile-runtime-ok")
