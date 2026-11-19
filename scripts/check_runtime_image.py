"""Opt-in OCI import and container startup check."""
import argparse
import base64
import csv
import hashlib
import io
import shutil
import subprocess
import sys
import tempfile
import tarfile
import uuid
from pathlib import Path
import zipfile

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig


def require_executable(name):
    executable = shutil.which(name)
    if executable is None:
        raise SystemExit(f"required executable not found: {name}")
    return executable


def create_dependency_wheel(wheelhouse):
    wheelhouse.mkdir(parents=True)
    wheel_path = wheelhouse / "runtime_fixture-1.0-py3-none-any.whl"
    files = {
        "runtime_fixture/__init__.py": (
            "def main():\n"
            "    print('pycontainer-runtime-dependency-ok')\n"
            "    return 0\n"
        ).encode(),
        "runtime_fixture-1.0.dist-info/METADATA": (
            "Metadata-Version: 2.1\nName: runtime-fixture\nVersion: 1.0\n\n"
        ).encode(),
        "runtime_fixture-1.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode(),
        "runtime_fixture-1.0.dist-info/entry_points.txt": (
            "[console_scripts]\nruntime-fixture = runtime_fixture:main\n"
        ).encode(),
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode()
        writer.writerow((name, f"sha256={digest.rstrip('=')}", len(content)))
    writer.writerow(("runtime_fixture-1.0.dist-info/RECORD", "", ""))
    files["runtime_fixture-1.0.dist-info/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)


def main():
    parser = argparse.ArgumentParser(
        description="Build an OCI image, import it with Skopeo, and start it with Docker."
    )
    parser.add_argument(
        "--base-image",
        default="python:3.11-slim",
        help="registry base image to pull (default: python:3.11-slim)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="timeout in seconds for Docker and Skopeo commands",
    )
    parser.add_argument(
        "--dependency-layer-only",
        action="store_true",
        help="Run the staged dependency layer in the target base image without Skopeo",
    )
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")

    docker = require_executable("docker")
    skopeo = None if args.dependency_layer_only else require_executable("skopeo")
    subprocess.run([docker, "info"], check=True, timeout=30, stdout=subprocess.DEVNULL)

    image_ref = f"pycontainer-runtime-check:{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="pycontainer-runtime-check-") as temp_dir:
        temp_path = Path(temp_dir)
        context = temp_path / "context"
        context.mkdir()
        (context / "app.py").write_text("print('pycontainer-oci-runtime-ok')\n")
        create_dependency_wheel(context / "wheelhouse")
        (context / "requirements.txt").write_text(
            "--no-index\n--find-links ./wheelhouse\nruntime-fixture==1.0\n"
        )
        layout = temp_path / "oci-layout"
        config = BuildConfig(
            tag="runtime-check",
            base_image=args.base_image,
            context_dir=str(context),
            output_dir=str(layout),
            entrypoint=["runtime-fixture"],
            include_deps=True,
            generate_sbom=True,
            use_cache=False,
        )

        builder = ImageBuilder(config)
        builder.build()
        sbom = (layout / "sbom.json").read_text()
        if '"name": "runtime-fixture"' not in sbom:
            raise RuntimeError("SBOM does not describe the staged runtime dependency")
        if args.dependency_layer_only:
            with tempfile.TemporaryDirectory(
                prefix="pycontainer-dependency-runtime-"
            ) as staged_dir:
                staged_app = Path(staged_dir) / "app"
                staged_app.mkdir()
                with tarfile.open(
                    Path(builder.layers[-2].tar_path), "r"
                ) as dependency_layer:
                    dependency_layer.extractall(staged_dir)
                result = subprocess.run(
                    [
                        docker,
                        "run",
                        "--rm",
                        "--pull=always",
                        "--volume",
                        f"{staged_app}:/app:ro",
                        "--env",
                        "PYTHONPATH=/app/.python/site-packages",
                        "--env",
                        "PATH=/app/.python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "--entrypoint",
                        "/app/.python/bin/runtime-fixture",
                        args.base_image,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                )
                if result.returncode:
                    raise RuntimeError(
                        "target runtime failed to execute the staged dependency: "
                        f"{result.stderr.strip()}"
                    )
                if "pycontainer-runtime-dependency-ok" not in result.stdout:
                    raise RuntimeError(
                        "target runtime did not import the staged dependency"
                    )
                print(result.stdout, end="")
                print("Target dependency import check passed.")
            return

        try:
            subprocess.run(
                [skopeo, "copy", f"oci:{layout}:runtime-check", f"docker-daemon:{image_ref}"],
                check=True,
                timeout=args.timeout,
            )
            result = subprocess.run(
                [docker, "run", "--pull=never", "--rm", image_ref],
                check=True,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            if "pycontainer-runtime-dependency-ok" not in result.stdout:
                raise RuntimeError("container did not import and run its staged dependency")
            print(result.stdout, end="")
            print("OCI import and container startup check passed.")
        finally:
            cleanup = subprocess.run(
                [docker, "image", "rm", "--force", image_ref],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if cleanup.returncode:
                print(
                    f"warning: unable to remove temporary image {image_ref}: {cleanup.stderr.strip()}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    main()
