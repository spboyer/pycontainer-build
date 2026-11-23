"""Run pycontainer with registry credentials scoped to the destination host."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from pycontainer.registry_client import parse_image_reference


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--tag", action="append", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--cache-dir")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--registry")
    parser.add_argument("--username")
    parser.add_argument("--password")
    args, _ = parser.parse_known_args(argv)
    if any(value is not None for value in (args.registry, args.username, args.password)):
        parser.error("Use a fully qualified --tag and environment credentials.")
    if not args.cache_dir and not args.no_cache:
        parser.error("Specify --cache-dir or --no-cache.")

    try:
        registry, _, _ = parse_image_reference(args.tag[0])
    except ValueError as error:
        parser.error(str(error))
    if "/" not in args.tag[0] or args.tag[0].split("/", 1)[0] != registry:
        parser.error("--tag must include the destination registry hostname.")

    username = os.environ.get("REGISTRY_USERNAME")
    password = os.environ.get("REGISTRY_PASSWORD")
    if not username or not password:
        parser.error("REGISTRY_USERNAME and REGISTRY_PASSWORD must be set.")

    with tempfile.TemporaryDirectory(prefix="pycontainer-registry-") as home:
        home_path = Path(home)
        protected_paths = [Path(args.context)]
        if args.cache_dir:
            protected_paths.append(Path(args.cache_dir))
        if any(home_path.resolve().is_relative_to(path.resolve()) for path in protected_paths):
            parser.error("The temporary credential directory must be outside the build context and cache.")
        docker_dir = home_path / ".docker"
        docker_dir.mkdir(mode=0o700)
        config_path = docker_dir / "config.json"
        descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as config:
            json.dump({"auths": {registry: {"username": username, "password": password}}}, config)

        env = os.environ.copy()
        for key in (
            "REGISTRY_USERNAME", "REGISTRY_PASSWORD", "REGISTRY_TOKEN", "GITHUB_TOKEN",
            "AZURE_CONFIG_DIR",
        ):
            env.pop(key, None)
        env["HOME"] = home
        env["USERPROFILE"] = home
        result = subprocess.run([sys.executable, "-m", "pycontainer.cli", *argv], env=env)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
