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


def run_layout(docker, skopeo, layout, image_ref, timeout):
    """Import an OCI layout into Docker, run it once, and return stdout."""
    try:
        subprocess.run(
            [skopeo, "copy", f"oci:{layout}:runtime-check", f"docker-daemon:{image_ref}"],
            check=True,
            timeout=timeout,
        )
        return subprocess.run(
            [docker, "run", "--pull=never", "--rm", image_ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        ).stdout
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


SHELL_COMMAND_DOCKERFILE = (
    "FROM {base}\n"
    "ENV MARKER=ok\n"
    "ENTRYPOINT printf '%s\\n' \"pycontainer-shell-command-$MARKER\" 'quoted  $NOT_EXPANDED'\n"
)
SHELL_COMMAND_EXPECTED = "pycontainer-shell-command-ok\nquoted  $NOT_EXPANDED\n"


def shell_command_check(args, docker, skopeo, image_ref):
    """Import a quoted shell-form ENTRYPOINT, build it, and run it in the target base image."""
    from pycontainer.dockerfile_import import import_dockerfile, parse_shell_option

    shell = parse_shell_option(args.shell) if args.shell else None
    result = import_dockerfile(SHELL_COMMAND_DOCKERFILE.format(base=args.base_image), shell=shell)
    for warning in result.warnings:
        print(f"import warning: {warning}", file=sys.stderr)
    print(f"Imported entrypoint: {result.config['entrypoint']}")
    with tempfile.TemporaryDirectory(prefix="pycontainer-shell-check-") as temp_dir:
        temp_path = Path(temp_dir)
        context = temp_path / "context"
        context.mkdir()
        (context / "app.py").write_text("")
        layout = temp_path / "oci-layout"
        ImageBuilder(
            BuildConfig(
                tag="runtime-check",
                base_image=args.base_image,
                context_dir=str(context),
                output_dir=str(layout),
                entrypoint=result.config["entrypoint"],
                env=result.config.get("env", {}),
                use_cache=False,
            )
        ).build()
        stdout = run_layout(docker, skopeo, layout, image_ref, args.timeout)
    if stdout != SHELL_COMMAND_EXPECTED:
        raise RuntimeError(
            f"shell-form command output mismatch: expected {SHELL_COMMAND_EXPECTED!r}, got {stdout!r}"
        )
    print(stdout, end="")
    print("Shell-form command runtime check passed.")


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
    parser.add_argument(
        "--shell-command",
        action="store_true",
        help="Import a quoted shell-form ENTRYPOINT, build it and run it in the target base image",
    )
    parser.add_argument(
        "--shell",
        help='JSON shell array for --shell-command conversion (default: ["/bin/sh", "-c"])',
    )
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if args.shell and not args.shell_command:
        parser.error("--shell requires --shell-command")
    if args.shell_command and args.dependency_layer_only:
        parser.error("--shell-command cannot be combined with --dependency-layer-only")

    docker = require_executable("docker")
    skopeo = None if args.dependency_layer_only else require_executable("skopeo")
    subprocess.run([docker, "info"], check=True, timeout=30, stdout=subprocess.DEVNULL)

    image_ref = f"pycontainer-runtime-check:{uuid.uuid4().hex[:12]}"
    if args.shell_command:
        shell_command_check(args, docker, skopeo, image_ref)
        return
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

        stdout = run_layout(docker, skopeo, layout, image_ref, args.timeout)
        if "pycontainer-runtime-dependency-ok" not in stdout:
            raise RuntimeError("container did not import and run its staged dependency")
        print(stdout, end="")
        print("OCI import and container startup check passed.")


if __name__ == "__main__":
    main()
