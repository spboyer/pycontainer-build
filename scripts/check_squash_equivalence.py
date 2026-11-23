"""Opt-in check that a squashed image behaves like its unsquashed build.

Builds the same context twice (default layout and ``--squash``), imports both
layouts into Docker with Skopeo, then compares the application output and a
filesystem listing (type, mode, owner, size, content digest and link target)
of the working directory inside each running container. The build and runtime
phases can also run on separate GitHub Actions runners.
"""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from pycontainer.blob_store import blob_matches, digest_filename, hash_file
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
from pycontainer.local_image import MAX_METADATA_BYTES

from check_runtime_image import (
    RUNTIME_MARKER,
    command_output,
    create_dependency_wheel,
    require_executable,
    run_url,
)

LISTING_SCRIPT = r"""
import hashlib, os, stat, sys
root = sys.argv[1]
if not os.path.isdir(root):
    raise SystemExit("listing root is not a directory")
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
        platform="linux/amd64",
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


def build_layouts(output: Path, base_image: str):
    output.mkdir(parents=True, exist_ok=False)
    images = {}
    with tempfile.TemporaryDirectory(prefix="pycontainer-squash-context-") as temp_dir:
        context = Path(temp_dir)
        (context / "app.py").write_text("print('pycontainer-squash-runtime-ok')\n")
        create_dependency_wheel(context / "wheelhouse")
        (context / "requirements.txt").write_text(
            "--no-index\n--find-links ./wheelhouse\nruntime-fixture==1.0\n"
        )
        for name, squash in (("plain", False), ("squashed", True)):
            layout = output / name
            builder = build_layout(context, layout, base_image, squash)
            images[name] = {
                "index_digest": hash_file(layout / "index.json")[0],
                "manifest_digest": builder.manifest_digest,
                "config_digest": builder.config_digest,
                "layer_digests": [layer.digest for layer in builder.layers],
            }
    metadata = {
        "commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "run_url": run_url(),
        "builder_os": platform.system(),
        "builder_os_version": platform.version(),
        "builder_arch": platform.machine(),
        "builder_python": platform.python_version(),
        "base_image": base_image,
        "images": images,
    }
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def validate_provenance(metadata, expected):
    for key in ("commit", "run_id", "run_attempt"):
        if not expected.get(key) or metadata.get(key) != expected[key]:
            raise RuntimeError(f"artifact provenance does not match expected {key}")
    if metadata.get("builder_os") != "Windows":
        raise RuntimeError("artifact provenance does not identify a Windows builder")


def _checked_blob(layout, descriptor, *, metadata=False):
    try:
        digest = descriptor["digest"]
        size = descriptor["size"]
        filename = digest_filename(digest)
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("invalid OCI blob descriptor") from error
    if type(size) is not int or size < 0:
        raise RuntimeError("invalid OCI blob size")
    if metadata and size > MAX_METADATA_BYTES:
        raise RuntimeError("OCI metadata blob exceeds the size limit")
    for directory in (layout, layout / "blobs", layout / "blobs" / "sha256"):
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError(f"invalid OCI blob directory: {directory}")
    path = layout / "blobs" / "sha256" / filename
    if not blob_matches(path, digest, size):
        raise RuntimeError(f"OCI blob does not match its descriptor: {digest}")
    return path


def validate_layout(layout, expected):
    marker = layout / "oci-layout"
    index_path = layout / "index.json"
    if marker.is_symlink() or not marker.is_file():
        raise RuntimeError("missing OCI layout marker")
    if marker.stat().st_size > MAX_METADATA_BYTES:
        raise RuntimeError("OCI layout marker exceeds the size limit")
    if json.loads(marker.read_text(encoding="utf-8")).get("imageLayoutVersion") != "1.0.0":
        raise RuntimeError("invalid OCI layout marker")
    if index_path.is_symlink() or not index_path.is_file():
        raise RuntimeError("missing OCI layout index")
    if index_path.stat().st_size > MAX_METADATA_BYTES:
        raise RuntimeError("OCI layout index exceeds the size limit")
    if hash_file(index_path)[0] != expected["index_digest"]:
        raise RuntimeError("OCI layout index does not match the recorded digest")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    descriptors = index.get("manifests")
    if index.get("schemaVersion") != 2 or not isinstance(descriptors, list) or len(descriptors) != 1:
        raise RuntimeError("expected exactly one OCI image descriptor")
    descriptor = descriptors[0]
    if descriptor.get("digest") != expected["manifest_digest"]:
        raise RuntimeError("OCI manifest does not match the recorded digest")
    target = descriptor.get("platform", {})
    if target.get("os") != "linux" or target.get("architecture") != "amd64":
        raise RuntimeError("OCI descriptor platform is not linux/amd64")
    manifest = json.loads(_checked_blob(layout, descriptor, metadata=True).read_text(encoding="utf-8"))
    config_descriptor = manifest["config"]
    if config_descriptor["digest"] != expected["config_digest"]:
        raise RuntimeError("OCI config does not match the recorded digest")
    config = json.loads(_checked_blob(layout, config_descriptor, metadata=True).read_text(encoding="utf-8"))
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise RuntimeError("OCI config platform is not linux/amd64")
    layers = manifest["layers"]
    if not isinstance(layers, list) or [item["digest"] for item in layers] != expected["layer_digests"]:
        raise RuntimeError("OCI layers do not match the recorded digests")
    for descriptor in layers:
        _checked_blob(layout, descriptor)
    diff_ids = config.get("rootfs", {}).get("diff_ids")
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
        raise RuntimeError("OCI config DiffIDs do not match its layer count")
    for digest in diff_ids:
        digest_filename(digest)
    return {
        "config_digest": config_descriptor["digest"],
        "layer_digests": [item["digest"] for item in layers],
        "diff_ids": diff_ids,
    }


def validate_import(docker, image_ref, image, timeout):
    def inspect(template):
        return command_output(
            [docker, "image", "inspect", "--format", template, image_ref], timeout=timeout
        )

    if inspect("{{.Id}}") != image["config_digest"]:
        raise RuntimeError("imported Docker image config digest differs from the OCI layout")
    if inspect("{{.Os}}/{{.Architecture}}") != "linux/amd64":
        raise RuntimeError("imported Docker image platform is not linux/amd64")
    if json.loads(inspect("{{json .RootFS.Layers}}")) != image["diff_ids"]:
        raise RuntimeError("imported Docker image DiffIDs differ from the OCI layout")


def assert_equivalence(plain, squashed, plain_count, squashed_count):
    if squashed_count != plain_count - 1:
        raise RuntimeError(f"expected one fewer layer after squashing: {plain_count} -> {squashed_count}")
    if plain != squashed:
        raise RuntimeError("squashed image output or filesystem differs from the unsquashed image")
    if plain[0].strip() != RUNTIME_MARKER:
        raise RuntimeError("container did not import and run its staged dependency")


def compare_layouts(root, metadata, docker, skopeo, skopeo_image, timeout, workdir):
    images = {
        name: validate_layout(root / name, metadata["images"][name])
        for name in ("plain", "squashed")
    }
    plain_layers = images["plain"]["layer_digests"]
    squash_layers = images["squashed"]["layer_digests"]
    if len(plain_layers) < 2 or plain_layers[:-2] != squash_layers[:-1]:
        raise RuntimeError("plain and squashed layouts do not share the same base layers")
    daemon_platform = command_output(
        [docker, "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"], timeout=timeout
    )
    if daemon_platform != "linux/amd64":
        raise RuntimeError(f"Docker daemon platform is {daemon_platform}, not linux/amd64")
    docker_version = command_output(
        [docker, "version", "--format", "{{.Server.Version}}"], timeout=timeout
    )
    skopeo_version = (
        command_output([skopeo, "--version"], timeout=timeout)
        if skopeo else f"container:{skopeo_image}"
    )
    suffix = uuid.uuid4().hex[:12]
    refs = {name: f"pycontainer-squash-check:{name}-{suffix}" for name in images}
    observations = {}
    try:
        for name in ("plain", "squashed"):
            image_ref = refs[name]
            import_layout(docker, skopeo, skopeo_image, root / name, image_ref, timeout)
            validate_import(docker, image_ref, images[name], timeout)
            observations[name] = (
                run(docker, image_ref, timeout, ["--network", "none"]),
                run(docker, image_ref, timeout, ["--network", "none", "--entrypoint", "python"],
                    ["-c", LISTING_SCRIPT, workdir]),
            )
        assert_equivalence(
            observations["plain"], observations["squashed"],
            len(plain_layers), len(squash_layers),
        )
        print(observations["plain"][0], end="")
        print(f"Squash runtime equivalence check passed ({len(plain_layers)} -> {len(squash_layers)} layers).")
        return {
            "status": "passed",
            "build": metadata,
            "docker_version": docker_version,
            "skopeo_version": skopeo_version,
            "daemon_platform": daemon_platform,
            "runtime_os_version": platform.platform(),
            "runtime_python": platform.python_version(),
            "container_stdout": observations["plain"][0],
            "listing_sha256": hashlib.sha256(observations["plain"][1].encode()).hexdigest(),
        }
    finally:
        for image_ref in refs.values():
            cleanup = subprocess.run(
                [docker, "image", "rm", "--force", image_ref],
                capture_output=True, text=True, check=False, timeout=30,
            )
            if cleanup.returncode:
                print(f"warning: unable to remove {image_ref}: {cleanup.stderr.strip()}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-image", default="python:3.11-slim")
    parser.add_argument("--workdir", default="/app")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--skopeo-image",
        help="run Skopeo from this container image (for hosts without a skopeo binary)",
    )
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument("--build-only", type=Path, help="write both layouts for a separate runner")
    phases.add_argument("--check-only", type=Path, help="check Windows layouts from this Actions run")
    parser.add_argument("--evidence", type=Path, help="write a passing runtime evidence record")
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if args.build_only:
        if args.evidence:
            parser.error("--evidence requires a runtime comparison")
        build_layouts(args.build_only, args.base_image)
        return

    metadata = None
    if args.check_only:
        metadata = json.loads((args.check_only / "metadata.json").read_text(encoding="utf-8"))
        validate_provenance(metadata, {
            "commit": os.environ.get("GITHUB_SHA"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        })
        if not os.environ.get("SQUASH_ARTIFACT_ID") or not os.environ.get("SQUASH_ARTIFACT_DIGEST"):
            raise RuntimeError("missing artifact identity for the runtime evidence")

    docker = require_executable("docker")
    skopeo = None if args.skopeo_image else require_executable("skopeo")
    subprocess.run([docker, "info"], check=True, timeout=30, stdout=subprocess.DEVNULL)

    def compare(root, metadata):
        evidence = compare_layouts(
            root, metadata, docker, skopeo, args.skopeo_image, args.timeout, args.workdir
        )
        if args.check_only:
            evidence["artifact"] = {
                "id": os.environ["SQUASH_ARTIFACT_ID"],
                "digest": os.environ["SQUASH_ARTIFACT_DIGEST"],
            }
        if args.evidence:
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            args.evidence.write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    if args.check_only:
        compare(args.check_only, metadata)
    else:
        with tempfile.TemporaryDirectory(prefix="pycontainer-squash-check-") as temp_dir:
            output = Path(temp_dir) / "layouts"
            compare(output, build_layouts(output, args.base_image))


if __name__ == "__main__":
    main()
