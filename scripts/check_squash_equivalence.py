"""Opt-in check that a squashed image behaves like its unsquashed build.

Builds the same context twice (default layout and ``--squash``), imports both
layouts into Docker with Skopeo, then compares the application output and a
filesystem listing (type, mode, owner, size, content digest and link target)
of the working directory inside each running container.
"""
import argparse
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig

from check_runtime_image import create_dependency_wheel, require_executable

LISTING_SCRIPT = r"""
import hashlib, os, stat, sys
root = sys.argv[1]
for directory, dirs, files in os.walk(root):
    dirs.sort()
    for name in sorted(dirs + files):
        path = os.path.join(directory, name)
        info = os.lstat(path)
        kind = stat.S_IFMT(info.st_mode)
        extra = ""
        if stat.S_ISLNK(info.st_mode):
            extra = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            with open(path, "rb") as handle:
                extra = hashlib.sha256(handle.read()).hexdigest()
        print(oct(kind), oct(stat.S_IMODE(info.st_mode)), info.st_uid, info.st_gid,
              info.st_size if stat.S_ISREG(info.st_mode) else 0,
              os.path.relpath(path, root), extra)
"""


def build_layout(context: Path, output: Path, base_image: str, squash: bool) -> ImageBuilder:
    builder = ImageBuilder(BuildConfig(
        tag="squash-check",
        base_image=base_image,
        context_dir=str(context),
        output_dir=str(output),
        entrypoint=["runtime-fixture"],
        include_deps=True,
        use_cache=False,
        squash=squash,
    ))
    builder.build()
    return builder


def import_layout(docker, skopeo, skopeo_image, layout: Path, image_ref, timeout):
    if skopeo is not None:
        subprocess.run(
            [skopeo, "copy", f"oci:{layout}", f"docker-daemon:{image_ref}"],
            check=True,
            timeout=timeout,
        )
        return
    subprocess.run(
        [docker, "run", "--rm", "--volume", f"{layout.resolve()}:/layout", skopeo_image,
         "copy", "oci:/layout", f"docker-archive:/layout/image.tar:{image_ref}"],
        check=True,
        timeout=timeout,
    )
    subprocess.run([docker, "load", "--input", str(layout / "image.tar")], check=True, timeout=timeout)


def run(docker, image_ref, timeout, options=(), command=()):
    result = subprocess.run(
        [docker, "run", "--pull=never", "--rm", *options, image_ref, *command],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-image", default="python:3.11-slim")
    parser.add_argument("--workdir", default="/app")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--skopeo-image",
        help="run Skopeo from this container image (for hosts without a skopeo binary)",
    )
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")

    docker = require_executable("docker")
    skopeo = None if args.skopeo_image else require_executable("skopeo")
    subprocess.run([docker, "info"], check=True, timeout=30, stdout=subprocess.DEVNULL)

    suffix = uuid.uuid4().hex[:12]
    refs = {False: f"pycontainer-squash-check:plain-{suffix}", True: f"pycontainer-squash-check:squashed-{suffix}"}
    observations = {}
    with tempfile.TemporaryDirectory(prefix="pycontainer-squash-check-") as temp_dir:
        temp_path = Path(temp_dir)
        context = temp_path / "context"
        context.mkdir()
        (context / "app.py").write_text("print('pycontainer-squash-runtime-ok')\n")
        create_dependency_wheel(context / "wheelhouse")
        (context / "requirements.txt").write_text(
            "--no-index\n--find-links ./wheelhouse\nruntime-fixture==1.0\n"
        )
        try:
            layer_counts = {}
            for squash, image_ref in refs.items():
                layout = temp_path / ("squashed" if squash else "plain")
                builder = build_layout(context, layout, args.base_image, squash)
                layer_counts[squash] = len(builder.layers)
                import_layout(docker, skopeo, args.skopeo_image, layout, image_ref, args.timeout)
                observations[squash] = (
                    run(docker, image_ref, args.timeout),
                    run(docker, image_ref, args.timeout, ["--entrypoint", "python"],
                        ["-c", LISTING_SCRIPT, args.workdir]),
                )
            if layer_counts[True] != layer_counts[False] - 1:
                raise RuntimeError(f"expected one fewer layer after squashing: {layer_counts}")
            if observations[True] != observations[False]:
                raise RuntimeError("squashed image output or filesystem differs from the unsquashed image")
            if "pycontainer-runtime-dependency-ok" not in observations[True][0]:
                raise RuntimeError("container did not import and run its staged dependency")
            print(observations[True][0], end="")
            print(f"Squash runtime equivalence check passed ({layer_counts[False]} -> {layer_counts[True]} layers).")
        finally:
            for image_ref in refs.values():
                cleanup = subprocess.run(
                    [docker, "image", "rm", "--force", image_ref],
                    capture_output=True, text=True, check=False, timeout=30,
                )
                if cleanup.returncode:
                    print(f"warning: unable to remove {image_ref}: {cleanup.stderr.strip()}", file=sys.stderr)


if __name__ == "__main__":
    main()
