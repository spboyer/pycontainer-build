"""Opt-in end-to-end check of the Skaffold and Tilt examples against real tools.

Requires skaffold and/or tilt, kubectl with a current context, and an HTTPS
registry that both this machine and the cluster can pull from. Registry
credentials come from the normal pycontainer providers (REGISTRY_USERNAME /
REGISTRY_PASSWORD, REGISTRY_TOKEN, static ~/.docker/config.json auths, or the
Azure CLI for *.azurecr.io); they are never passed on a command line.

    python scripts/check_devloop_tools.py --registry myregistry.azurecr.io/dev

Each example is copied into a temporary directory, deployed, and verified by
waiting for the deployment rollout (its readiness probe hits the app over
HTTP) and checking that the running image came from the requested registry.
Resources are removed afterwards. This check is not part of CI.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = "deployment/pycontainer-demo"
STEP_TIMEOUT_SECONDS = 600


def run(argv, cwd, env=None, check=True):
    print("+", " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=cwd, env=env, timeout=STEP_TIMEOUT_SECONDS)
    if check and result.returncode != 0:
        raise SystemExit(f"{argv[0]} exited with {result.returncode}")
    return result


def deployed_image(cwd):
    return subprocess.run(
        ["kubectl", "get", DEPLOYMENT, "-o", "jsonpath={.spec.template.spec.containers[0].image}"],
        cwd=cwd, capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()


def verify_rollout(cwd, registry):
    run(["kubectl", "rollout", "status", DEPLOYMENT, "--timeout=300s"], cwd)
    image = deployed_image(cwd)
    if not image.startswith(f"{registry}/pycontainer-demo"):
        raise SystemExit(f"Deployment runs {image!r}, not an image pushed to {registry}")
    print(f"verified running image {image}")


def tool_env():
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    return env


def check_skaffold(workdir, registry, insecure):
    project = workdir / "skaffold"
    shutil.copytree(REPO_ROOT / "examples" / "skaffold", project)
    env = tool_env()
    try:
        extra = ["--insecure-registry", registry.split("/", 1)[0]] if insecure else []
        run(["skaffold", "run", "--default-repo", registry, "--status-check=true", *extra], project, env)
        verify_rollout(project, registry)
    finally:
        run(["skaffold", "delete"], project, env, check=False)


def check_tilt(workdir, registry, mode):
    project = workdir / f"tilt-{mode}"
    shutil.copytree(REPO_ROOT / "examples" / "tilt", project)
    env = tool_env()
    env.update(PYCONTAINER_TILT_REGISTRY=registry, PYCONTAINER_TILT_MODE=mode)
    try:
        run(["tilt", "ci", "--timeout", "10m"], project, env)
        verify_rollout(project, registry)
        if mode == "output-ref":
            reference = (project / "dist" / "tilt-image-ref.txt").read_text().strip()
            if ":tilt-" not in reference:
                raise SystemExit(f"Unexpected output reference {reference!r}")
    finally:
        run(["tilt", "down"], project, env, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", required=True, help="HTTPS registry/namespace, e.g. example.azurecr.io/dev")
    parser.add_argument("--tool", choices=("skaffold", "tilt", "all"), default="all")
    parser.add_argument("--skaffold-insecure-registry", action="store_true", help="Let Skaffold skip TLS verification for its digest lookup (self-signed test registries); pycontainer still verifies TLS")
    args = parser.parse_args()
    registry = args.registry.rstrip("/")

    needed = ["kubectl"] + (["skaffold"] if args.tool in ("skaffold", "all") else []) \
        + (["tilt"] if args.tool in ("tilt", "all") else [])
    missing = [tool for tool in needed if shutil.which(tool) is None]
    if missing:
        raise SystemExit(f"Missing required tools: {', '.join(missing)}")
    for tool in needed:
        version = ["kubectl", "version", "--client"] if tool == "kubectl" else [tool, "version"]
        run(version, REPO_ROOT)

    with tempfile.TemporaryDirectory(prefix="pycontainer-devloop-") as temp:
        workdir = Path(temp)
        if args.tool in ("skaffold", "all"):
            check_skaffold(workdir, registry, args.skaffold_insecure_registry)
        if args.tool in ("tilt", "all"):
            check_tilt(workdir, registry, "expected-ref")
            check_tilt(workdir, registry, "output-ref")
    print("Skaffold/Tilt dev-loop checks passed.")


if __name__ == "__main__":
    main()
