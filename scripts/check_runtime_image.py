"""Opt-in OCI import and container startup check."""
import argparse
import base64
import csv
import hashlib
import io
import json
import os
import platform as platform_module
from email.parser import BytesParser
import shutil
import subprocess
import sys
import tempfile
import tarfile
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import zipfile

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig

EVIDENCE_SCHEMA = 1
RUNTIME_MARKER = "pycontainer-runtime-dependency-ok"
LOCK_MARKER = "pycontainer-lockfile-runtime-ok"
LOCK_FIXTURE_PATH = "tests/fixtures/runtime-lock"
LOCK_FIXTURE = Path(__file__).resolve().parents[1] / LOCK_FIXTURE_PATH
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


def prepare_lock_fixture(context, platform):
    """Verify locked wheel bytes before deriving the in-container assertions."""
    tags = {"linux/amd64": "x86_64", "linux/arm64": "aarch64"}
    if platform not in tags:
        raise ValueError(f"unsupported lock fixture target: {platform}")
    shutil.copytree(LOCK_FIXTURE, context, dirs_exist_ok=True)
    lock_bytes = (context / "uv.lock").read_bytes()
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    expected = []
    for name, module, suffix in (
        ("runtime-pure", "runtime_pure", "py3-none-any.whl"),
        ("runtime-platform", "runtime_platform", f"py3-none-manylinux2014_{tags[platform]}.whl"),
    ):
        entries = [entry for entry in lock["package"] if entry["name"] == name]
        if len(entries) != 1:
            raise ValueError(f"expected exactly one locked package for {name}")
        entry = entries[0]
        wheels = [wheel for wheel in entry["wheels"] if urlparse(wheel["url"]).path.endswith(suffix)]
        if len(wheels) != 1:
            raise ValueError(f"expected exactly one matching locked wheel for {name}")
        wheel = wheels[0]
        filename = Path(urlparse(wheel["url"]).path).name
        expected_filename = f"{module}-{entry['version']}-{suffix}"
        if filename != expected_filename:
            raise ValueError(f"locked wheel name does not match {name} and its version")
        wheel_path = context / "wheelhouse" / filename
        digest = "sha256:" + hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        if digest != wheel["hash"]:
            raise ValueError(f"locked wheel hash mismatch for {name}")
        module_path = f"{module}/__init__.py"
        metadata_path = f"{module}-{entry['version']}.dist-info/METADATA"
        with zipfile.ZipFile(wheel_path) as archive:
            module_bytes = archive.read(module_path)
            metadata_bytes = archive.read(metadata_path)
        metadata = BytesParser().parsebytes(metadata_bytes)
        if metadata["Name"] != name or metadata["Version"] != entry["version"]:
            raise ValueError(f"locked wheel metadata mismatch for {name}")
        expected.append({
            "name": name,
            "version": entry["version"],
            "module": module,
            "module_path": module_path,
            "metadata_path": metadata_path,
            "wheel_sha256": digest,
            "module_sha256": hashlib.sha256(module_bytes).hexdigest(),
            "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
        })
    (context / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    return "sha256:" + hashlib.sha256(lock_bytes).hexdigest(), expected


def record_image(evidence, builder):
    evidence["image"] = {
        "manifest_digest": builder.manifest_digest,
        "config_digest": builder.config_digest,
        "layer_digests": [layer.digest for layer in builder.layers],
    }


def run_layout(
    docker, skopeo, layout, image_ref, timeout, platform, evidence,
    *, source_reference="runtime-check", expected_config_digest=None,
):
    """Import an OCI layout into Docker, run it once, and return stdout."""
    try:
        copy_command = [skopeo]
        if expected_config_digest is not None:
            os_name, arch = platform.split("/")
            copy_command.extend(["--override-os", os_name, "--override-arch", arch])
        copy_command.extend(
            ["copy", f"oci:{layout}:{source_reference}", f"docker-daemon:{image_ref}"]
        )
        subprocess.run(
            copy_command,
            check=True,
            timeout=timeout,
        )
        image_platform = command_output(
            [docker, "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image_ref]
        )
        evidence["image_platform"] = image_platform
        if image_platform != os_arch(platform):
            raise RuntimeError(
                f"imported image platform {image_platform} does not match target {platform}"
            )
        if expected_config_digest is not None:
            imported_config_digest = command_output(
                [docker, "image", "inspect", "--format", "{{.Id}}", image_ref]
            )
            evidence["imported_config_digest"] = imported_config_digest
            if imported_config_digest != expected_config_digest:
                raise RuntimeError(
                    f"imported image config digest {imported_config_digest} does not "
                    f"match selected {platform} config {expected_config_digest}"
                )
        stdout = subprocess.run(
            [docker, "run", "--pull=never", "--rm", image_ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        ).stdout
        evidence["container_stdout"] = stdout[:STDOUT_LIMIT]
        return stdout
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


def shell_command_check(args, docker, skopeo, image_ref, evidence):
    """Import a quoted shell-form ENTRYPOINT, build it, and run it in the target base image."""
    from pycontainer.dockerfile_import import import_dockerfile, parse_shell_option

    shell = parse_shell_option(args.shell) if args.shell is not None else None
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
        builder = ImageBuilder(
            BuildConfig(
                tag="runtime-check",
                base_image=args.base_image,
                context_dir=str(context),
                output_dir=str(layout),
                entrypoint=result.config["entrypoint"],
                env=result.config.get("env", {}),
                platform=args.platform,
                use_cache=False,
            )
        )
        builder.build()
        record_image(evidence, builder)
        stdout = run_layout(
            docker, skopeo, layout, image_ref, args.timeout, args.platform, evidence
        )
    if stdout != SHELL_COMMAND_EXPECTED:
        raise RuntimeError(
            f"shell-form command output mismatch: expected {SHELL_COMMAND_EXPECTED!r}, got {stdout!r}"
        )
    print(stdout, end="")
    print("Shell-form command runtime check passed.")


def cmd_only_check(args, docker, skopeo, image_ref, evidence):
    """Start CMD-only imports under a base entrypoint that marks and forwards argv."""
    from pycontainer.config_loader import config_from_file
    from pycontainer.dockerfile_import import run_import

    cases = {
        "shell": ('printf \'shell=%s\\n\' "$MARKER"', "shell=expanded\n"),
        "exec": (json.dumps(["printf", "exec=%s\\n", "$MARKER  literal"]), "exec=$MARKER  literal\n"),
    }
    with tempfile.TemporaryDirectory(prefix="pycontainer-cmd-check-") as temp_dir:
        root = Path(temp_dir)
        base_context = root / "base-context"
        base_context.mkdir()
        (base_context / "app.py").touch()
        base_layout = root / "base-layout"
        ImageBuilder(BuildConfig(
            tag="runtime-check",
            base_image=args.base_image,
            context_dir=str(base_context),
            output_dir=str(base_layout),
            platform=args.platform,
            entrypoint=["/bin/sh", "-c", 'echo inherited-entrypoint; exec "$@"', "entrypoint"],
            use_cache=False,
        )).build()
        evidence["cases"] = {}
        for form, (command, expected_command_output) in cases.items():
            context = root / form
            context.mkdir()
            (context / "app.py").write_text("print('unexpected auto-detected entrypoint')\n")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text(f"FROM {args.base_image}\nENV MARKER=expanded\nCMD {command}\n")
            config_path = context / "pyproject.toml"
            message, status = run_import(dockerfile, config_path)
            if status:
                raise RuntimeError(f"CMD-only {form}-form import failed: {message}")
            layout = root / f"{form}-layout"
            config = config_from_file(config_path, {
                "tag": "runtime-check",
                "base_image": f"oci:{base_layout}:runtime-check",
                "context_dir": str(context),
                "output_dir": str(layout),
                "platform": args.platform,
                "use_cache": False,
            })
            builder = ImageBuilder(config)
            builder.build()
            case_evidence = evidence["cases"][form] = {}
            record_image(case_evidence, builder)
            stdout = run_layout(
                docker, skopeo, layout, f"{image_ref}-{form}", args.timeout, args.platform,
                case_evidence,
            )
            expected = "inherited-entrypoint\n" + expected_command_output
            if stdout != expected:
                raise RuntimeError(
                    f"CMD-only {form}-form output mismatch: expected {expected!r}, got {stdout!r}"
                )
            print(f"CMD-only {form}-form runtime output:\n{stdout}", end="")


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
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dependency-layer-only",
        action="store_true",
        help="Run the staged dependency layer in the target base image without Skopeo",
    )
    parser.add_argument(
        "--dependency-fixture",
        choices=("requirements", "lockfile"),
        default="requirements",
        help="dependency fixture to verify (lockfile requires a full OCI startup)",
    )
    parser.add_argument(
        "--multi-platform-index",
        action="store_true",
        help="Build an amd64/arm64 image index and import the native target",
    )
    modes.add_argument(
        "--shell-command",
        action="store_true",
        help="Import a quoted shell-form ENTRYPOINT, build it and run it in the target base image",
    )
    modes.add_argument(
        "--cmd-only",
        action="store_true",
        help="Import and run shell- and exec-form CMD under an inherited entrypoint",
    )
    parser.add_argument(
        "--shell",
        help='JSON shell array for --shell-command conversion (default: ["/bin/sh", "-c"])',
    )
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if args.multi_platform_index:
        for option, selected in (
            ("--dependency-layer-only", args.dependency_layer_only),
            ("--shell-command", args.shell_command),
            ("--cmd-only", args.cmd_only),
        ):
            if selected:
                parser.error(f"--multi-platform-index cannot be combined with {option}")
        if args.platform not in INDEX_PLATFORMS:
            parser.error("index check supports linux/amd64 and linux/arm64 only")

    if args.shell is not None and not args.shell_command:
        parser.error("--shell requires --shell-command")
    if args.dependency_fixture == "lockfile" and (
        args.cmd_only or args.shell_command or args.dependency_layer_only or args.multi_platform_index
    ):
        parser.error("--dependency-fixture lockfile requires a full OCI startup")

    if args.multi_platform_index:
        mode = "oci-index-import-startup"
    elif args.cmd_only:
        mode = "cmd-only"
    elif args.shell_command:
        mode = "shell-command"
    elif args.dependency_layer_only:
        mode = "dependency-layer-only"
    else:
        mode = "oci-import-startup"
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "status": "failed",
        "mode": mode,
        "target_platform": args.platform,
        "base_image": args.base_image,
        "dependency_fixture": args.dependency_fixture,
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
    if args.cmd_only:
        cmd_only_check(args, docker, skopeo, image_ref, evidence)
        return
    if args.shell_command:
        shell_command_check(args, docker, skopeo, image_ref, evidence)
        return
    with tempfile.TemporaryDirectory(prefix="pycontainer-runtime-check-") as temp_dir:
        temp_path = Path(temp_dir)
        context = temp_path / "context"
        context.mkdir()
        if args.dependency_fixture == "lockfile":
            evidence["fixture_path"] = LOCK_FIXTURE_PATH
            evidence["lock_digest"], evidence["locked_packages"] = prepare_lock_fixture(
                context, args.platform
            )
            evidence["fixture_app_digest"] = (
                "sha256:" + hashlib.sha256((context / "app.py").read_bytes()).hexdigest()
            )
            evidence["lock_archive_verification"] = "passed"
            entrypoint = ["python", "/app/app.py"]
        else:
            (context / "app.py").write_text("print('pycontainer-oci-runtime-ok')\n")
            create_dependency_wheel(context / "wheelhouse")
            (context / "requirements.txt").write_text(
                "--no-index\n--find-links ./wheelhouse\nruntime-fixture==1.0\n"
            )
            entrypoint = ["runtime-fixture"]
        layout = temp_path / "oci-layout"
        config = BuildConfig(
            tag="runtime.example/check:v1" if args.multi_platform_index else "runtime-check",
            base_image=args.base_image,
            context_dir=str(context),
            output_dir=str(layout),
            entrypoint=entrypoint,
            platform=",".join(INDEX_PLATFORMS) if args.multi_platform_index else args.platform,
            include_deps=True,
            include_paths=["app.py", "expected.json"] if args.dependency_fixture == "lockfile" else None,
            generate_sbom=not args.multi_platform_index,
            use_cache=False,
        )

        builder = ImageBuilder(config)
        if args.dependency_fixture == "lockfile":
            saved = {name: os.environ.get(name) for name in ("PIP_NO_INDEX", "PIP_FIND_LINKS")}
            try:
                os.environ["PIP_NO_INDEX"] = "1"
                os.environ["PIP_FIND_LINKS"] = str(context / "wheelhouse")
                builder.build()
            finally:
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        else:
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
            record_image(evidence, builder)
            sbom = (layout / "sbom.json").read_text()
            package_name = "runtime-pure" if args.dependency_fixture == "lockfile" else "runtime-fixture"
            if f'"name": "{package_name}"' not in sbom:
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

        if args.multi_platform_index:
            stdout = run_layout(
                docker, skopeo, layout, image_ref, args.timeout, args.platform, evidence,
                source_reference=builder.config.all_tags()[0],
                expected_config_digest=image.config_digest,
            )
        else:
            stdout = run_layout(
                docker, skopeo, layout, image_ref, args.timeout, args.platform, evidence
            )
        marker = LOCK_MARKER if args.dependency_fixture == "lockfile" else RUNTIME_MARKER
        if (stdout != marker + "\n" if args.dependency_fixture == "lockfile" else marker not in stdout):
            raise RuntimeError("container did not import and run its staged dependency")
        if args.dependency_fixture == "lockfile":
            evidence["lock_runtime_verification"] = "passed"
        print(stdout, end="")
        print("OCI import and container startup check passed.")


if __name__ == "__main__":
    main()