"""Opt-in OCI import and container startup check."""
import argparse
import base64
import csv
import hashlib
import io
import json
import os
import platform as platform_module
import shutil
import subprocess
import sys
import tempfile
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
import zipfile

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig

EVIDENCE_SCHEMA = 1
RUNTIME_MARKER = "pycontainer-runtime-dependency-ok"
STDOUT_LIMIT = 2000
INDEX_PLATFORMS = ("linux/amd64", "linux/arm64")


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
            f"    print({RUNTIME_MARKER!r})\n"
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


def command_output(command, timeout=30):
    return subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout.strip()


def os_arch(platform):
    return "/".join(platform.split("/")[:2])


def write_evidence(path, evidence):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build an OCI image, import it with Skopeo, and start it with Docker."
    )
    parser.add_argument(
        "--base-image",
        default="python:3.11-slim",
        help="registry base image to pull (default: python:3.11-slim)",
    )
    parser.add_argument(
        "--platform",
        default="linux/amd64",
        help="target platform; must match the Docker daemon (no emulation)",
    )
    parser.add_argument(
        "--evidence",
        help="write a JSON evidence record (passed or failed) to this path",
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
        "--multi-platform-index",
        action="store_true",
        help="Build an amd64/arm64 image index and import the native target",
    )
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if args.multi_platform_index:
        if args.dependency_layer_only:
            parser.error("--multi-platform-index cannot be combined with --dependency-layer-only")
        if args.platform not in INDEX_PLATFORMS:
            parser.error("index check supports linux/amd64 and linux/arm64 only")

    if args.multi_platform_index:
        mode = "oci-index-import-startup"
    else:
        mode = "dependency-layer-only" if args.dependency_layer_only else "oci-import-startup"
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "status": "failed",
        "mode": mode,
        "target_platform": args.platform,
        "base_image": args.base_image,
        "commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "run_url": run_url(),
        "runner": {
            "os": platform_module.system(),
            "machine": platform_module.machine(),
            "python": platform_module.python_version(),
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        run_check(args, evidence)
        evidence["status"] = "passed"
    except BaseException as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        if args.evidence:
            write_evidence(args.evidence, evidence)


def run_url():
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not (server and repository and run_id):
        return None
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    return f"{server}/{repository}/actions/runs/{run_id}/attempts/{attempt}"


def run_check(args, evidence):
    docker = require_executable("docker")
    skopeo = None if args.dependency_layer_only else require_executable("skopeo")
    subprocess.run([docker, "info"], check=True, timeout=30, stdout=subprocess.DEVNULL)
    evidence["docker_version"] = command_output(
        [docker, "version", "--format", "{{.Server.Version}}"]
    )
    daemon_platform = command_output(
        [docker, "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"]
    )
    evidence["daemon_platform"] = daemon_platform
    if daemon_platform != os_arch(args.platform):
        raise RuntimeError(
            f"Docker daemon platform {daemon_platform} does not match target "
            f"{args.platform}; emulated runs are not runtime evidence"
        )
    if skopeo:
        evidence["skopeo_version"] = command_output([skopeo, "--version"])

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
            tag="runtime.example/check:v1" if args.multi_platform_index else "runtime-check",
            base_image=args.base_image,
            context_dir=str(context),
            output_dir=str(layout),
            entrypoint=["runtime-fixture"],
            platform=",".join(INDEX_PLATFORMS) if args.multi_platform_index else args.platform,
            include_deps=True,
            generate_sbom=not args.multi_platform_index,
            use_cache=False,
        )

        builder = ImageBuilder(config)
        builder.build()
        if args.multi_platform_index:
            os_name, arch = args.platform.split("/")
            matches = [
                image for image in builder.platform_images
                if image.platform.get("os") == os_name
                and image.platform.get("architecture") == arch
            ]
            if len(matches) != 1:
                raise RuntimeError(f"image index does not contain exactly one {args.platform} image")
            image = matches[0]
            evidence["image"] = {
                "index_digest": builder.index_digest,
                "manifest_digest": image.manifest_digest,
                "config_digest": image.config_digest,
                "layer_digests": [layer.digest for layer in image.layers],
            }
        else:
            evidence["image"] = {
                "manifest_digest": builder.manifest_digest,
                "config_digest": builder.config_digest,
                "layer_digests": [layer.digest for layer in builder.layers],
            }
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
                        "--platform",
                        args.platform,
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
                if RUNTIME_MARKER not in result.stdout:
                    raise RuntimeError(
                        "target runtime did not import the staged dependency"
                    )
                evidence["container_stdout"] = result.stdout[:STDOUT_LIMIT]
                print(result.stdout, end="")
                print("Target dependency import check passed.")
            return

        try:
            source_reference = (
                builder.config.all_tags()[0]
                if args.multi_platform_index
                else "runtime-check"
            )
            copy_command = [skopeo]
            if args.multi_platform_index:
                copy_command.extend(["--override-os", os_name, "--override-arch", arch])
            copy_command.extend(
                ["copy", f"oci:{layout}:{source_reference}", f"docker-daemon:{image_ref}"]
            )
            subprocess.run(
                copy_command,
                check=True,
                timeout=args.timeout,
            )
            image_platform = command_output(
                [docker, "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image_ref]
            )
            evidence["image_platform"] = image_platform
            if image_platform != os_arch(args.platform):
                raise RuntimeError(
                    f"imported image platform {image_platform} does not match target "
                    f"{args.platform}"
                )
            if args.multi_platform_index:
                imported_config_digest = command_output(
                    [docker, "image", "inspect", "--format", "{{.Id}}", image_ref]
                )
                evidence["imported_config_digest"] = imported_config_digest
                if imported_config_digest != image.config_digest:
                    raise RuntimeError(
                        f"imported image config digest {imported_config_digest} does not "
                        f"match selected {args.platform} config {image.config_digest}"
                    )
            result = subprocess.run(
                [docker, "run", "--pull=never", "--rm", image_ref],
                check=True,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            evidence["container_stdout"] = result.stdout[:STDOUT_LIMIT]
            if RUNTIME_MARKER not in result.stdout:
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