"""Recreate the deterministic, synthetic wheels referenced by uv.lock."""
import base64
import csv
import hashlib
import io
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
WHEELS = (
    ("runtime_pure", "1.0", "py3-none-any"),
    ("runtime_platform", "2.0", "py3-none-manylinux2014_x86_64"),
    ("runtime_platform", "2.0", "py3-none-manylinux2014_aarch64"),
)


def create_wheel(directory, name, version, tag):
    distribution = name.replace("_", "-")
    dist_info = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": f'IDENTITY = "{distribution} {version}"\n'.encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: {'true' if tag == 'py3-none-any' else 'false'}\n"
            f"Tag: {tag}\n"
        ).encode(),
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow((path, f"sha256={digest}", len(content)))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    files[f"{dist_info}/RECORD"] = record.getvalue().encode()
    directory.mkdir(parents=True, exist_ok=True)
    wheel_path = directory / f"{name}-{version}-{tag}.whl"
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, content in files.items():
            info = zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return wheel_path


if __name__ == "__main__":
    for name, version, tag in WHEELS:
        wheel = create_wheel(ROOT / "wheelhouse", name, version, tag)
        print(f"{wheel.name} sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}")
