"""Opt-in OCI import and container startup check."""
import argparse
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig


def require_executable(name):
    executable = shutil.which(name)
    if executable is None:
        raise SystemExit(f"required executable not found: {name}")
    return executable


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
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")

    docker = require_executable("docker")
    skopeo = require_executable("skopeo")
    subprocess.run([docker, "info"], check=True, timeout=30, stdout=subprocess.DEVNULL)

    image_ref = f"pycontainer-runtime-check:{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="pycontainer-runtime-check-") as temp_dir:
        temp_path = Path(temp_dir)
        context = temp_path / "context"
        context.mkdir()
        (context / "app.py").write_text("print('pycontainer-oci-runtime-ok')\n")
        layout = temp_path / "oci-layout"
        config = BuildConfig(
            tag="runtime-check",
            base_image=args.base_image,
            context_dir=str(context),
            output_dir=str(layout),
            entrypoint=["python", "-c", "print('pycontainer-oci-runtime-ok')"],
            use_cache=False,
        )

        ImageBuilder(config).build()
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
            if "pycontainer-oci-runtime-ok" not in result.stdout:
                raise RuntimeError("container did not print the expected startup marker")
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
