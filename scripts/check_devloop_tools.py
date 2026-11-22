"""Opt-in end-to-end check of the Skaffold and Tilt examples against real tools.

Requires skaffold and/or tilt, kubectl with a current context, and an HTTPS
registry that both this machine and the cluster can pull from. Registry
credentials come from the normal pycontainer providers (REGISTRY_USERNAME /
REGISTRY_PASSWORD, REGISTRY_TOKEN, static ~/.docker/config.json auths, or the
Azure CLI for *.azurecr.io); they are never passed on a command line.

    python scripts/check_devloop_tools.py --registry myregistry.azurecr.io/dev

Each example is copied into a temporary directory and deployed in a new,
unique namespace. The check waits for each rollout and compares the deployed
reference with the built or reported reference. The namespace is removed
afterwards. This check is not part of CI.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from pycontainer.devloop import require_repository, tilt_content_tag
from pycontainer.registry_client import parse_image_reference

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = "deployment/pycontainer-demo"
STEP_TIMEOUT_SECONDS = 600


def run(argv, cwd, env=None):
    print("+", " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=cwd, env=env, timeout=STEP_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise SystemExit(f"{argv[0]} exited with {result.returncode}")
    return result


def cleanup(argv, cwd, env=None):
    handling_failure = sys.exc_info()[0] is not None
    try:
        run(argv, cwd, env)
    except (OSError, subprocess.SubprocessError, SystemExit) as error:
        if not handling_failure:
            raise
        print(f"Cleanup failed: {error}", file=sys.stderr)


def current_context():
    context = subprocess.run(
        ["kubectl", "config", "current-context"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()
    if not context:
        raise SystemExit("kubectl has no current context")
    return context


def deployed_image(cwd, context, namespace):
    return subprocess.run(
        ["kubectl", "--context", context, "--namespace", namespace, "get", DEPLOYMENT,
         "-o", "jsonpath={.spec.template.spec.containers[0].image}"],
        cwd=cwd, capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()


def built_image(cwd):
    index = json.loads((cwd / "dist" / "image" / "index.json").read_text(encoding="utf-8"))
    return index["annotations"]["org.opencontainers.image.ref.name"], index["manifests"][0]["digest"]


def verify_rollout(cwd, registry, context, namespace, reference, digest):
    run(["kubectl", "--context", context, "--namespace", namespace,
         "rollout", "status", DEPLOYMENT, "--timeout=300s"], cwd)
    image = deployed_image(cwd, context, namespace)
    image_tag, separator, image_digest = image.partition("@")
    try:
        expected = parse_image_reference(reference)
        actual = parse_image_reference(image_tag)
        target = parse_image_reference(f"{registry}/pycontainer-demo:verify")
    except ValueError as error:
        raise SystemExit(f"Invalid deployed or built image reference: {error}") from error
    if expected[:2] != target[:2] or actual != expected:
        raise SystemExit(f"Deployed reference {image!r} does not match built reference {reference!r}")
    if separator and image_digest != digest:
        raise SystemExit(f"Deployed digest {image_digest!r} does not match built digest {digest!r}")
    print(f"verified deployed reference {image}")


def tool_env():
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    return env


def check_skaffold(workdir, registry, insecure, context, namespace):
    project = workdir / "skaffold"
    shutil.copytree(REPO_ROOT / "examples" / "skaffold", project)
    env = tool_env()
    try:
        extra = ["--insecure-registry", registry.split("/", 1)[0]] if insecure else []
        run(["skaffold", "run", "--default-repo", registry, "--tag", namespace,
             "--status-check=true",
             "--cache-artifacts=false", "--kube-context", context, "--namespace", namespace,
             *extra], project, env)
        reference, digest = built_image(project)
        verify_rollout(project, registry, context, namespace, reference, digest)
    finally:
        cleanup(["skaffold", "delete", "--kube-context", context, "--namespace", namespace],
                project, env)


def check_tilt(workdir, registry, mode, context, namespace):
    project = workdir / f"tilt-{mode}"
    shutil.copytree(REPO_ROOT / "examples" / "tilt", project)
    env = tool_env()
    env.update(PYCONTAINER_TILT_REGISTRY=registry, PYCONTAINER_TILT_MODE=mode)
    try:
        run(["tilt", "ci", "--timeout", "10m", "--context", context,
             "--namespace", namespace], project, env)
        reference, digest = built_image(project)
        if mode == "output-ref":
            reference = (project / "dist" / "tilt-image-ref.txt").read_text(encoding="utf-8").strip()
            if parse_image_reference(reference)[2] != tilt_content_tag(digest):
                raise SystemExit(f"Output reference {reference!r} does not match the manifest content tag")
        verify_rollout(project, registry, context, namespace, reference, digest)
    finally:
        cleanup(["tilt", "down", "--context", context, "--namespace", namespace], project, env)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", required=True, help="HTTPS registry/namespace, e.g. example.azurecr.io/dev")
    parser.add_argument("--tool", choices=("skaffold", "tilt", "all"), default="all")
    parser.add_argument("--skaffold-insecure-registry", action="store_true", help="Let Skaffold skip TLS verification for its digest lookup (self-signed test registries); pycontainer still verifies TLS")
    args = parser.parse_args()
    registry = args.registry.rstrip("/")
    try:
        require_repository(f"{registry}/pycontainer-demo", "--registry")
    except ValueError as error:
        parser.error(str(error))

    needed = ["kubectl"] + (["skaffold"] if args.tool in ("skaffold", "all") else []) \
        + (["tilt"] if args.tool in ("tilt", "all") else [])
    missing = [tool for tool in needed if shutil.which(tool) is None]
    if missing:
        raise SystemExit(f"Missing required tools: {', '.join(missing)}")
    for tool in needed:
        version = ["kubectl", "version", "--client"] if tool == "kubectl" else [tool, "version"]
        run(version, REPO_ROOT)

    context = current_context()
    namespace = f"pycontainer-devloop-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="pycontainer-devloop-") as temp:
        workdir = Path(temp)
        run(["kubectl", "--context", context, "create", "namespace", namespace], workdir)
        try:
            if args.tool in ("skaffold", "all"):
                check_skaffold(workdir, registry, args.skaffold_insecure_registry, context, namespace)
            if args.tool in ("tilt", "all"):
                check_tilt(workdir, registry, "expected-ref", context, namespace)
                check_tilt(workdir, registry, "output-ref", context, namespace)
        finally:
            cleanup(["kubectl", "--context", context, "delete", "namespace", namespace,
                     "--timeout=300s"], workdir)
    print("Skaffold/Tilt dev-loop checks passed.")


if __name__ == "__main__":
    main()
