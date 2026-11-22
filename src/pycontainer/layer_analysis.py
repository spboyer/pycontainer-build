"""Opt-in, per-platform layer size analysis for builds.

Reports measured blob sizes and, where the media type allows streaming, the
uncompressed size. Shared blobs are stored and transferred once per registry
but still occupy every image filesystem that references them; repeated file
content is reported, not deduplicated.
"""
import gzip
import hashlib
import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .blob_store import HASH_CHUNK_SIZE
from .squash import SquashError, SquashStats, squash_layers

UNCOMPRESSED_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.docker.image.rootfs.diff.tar",
}
GZIP_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}


def _uncompressed_size(layer) -> Optional[int]:
    if layer.media_type in UNCOMPRESSED_TYPES:
        return layer.size
    if layer.media_type not in GZIP_TYPES:
        return None
    total = 0
    with gzip.open(layer.tar_path, "rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_SIZE), b""):
            total += len(chunk)
    return total


def _repeated_content(layers: Sequence) -> Dict:
    """Group regular files in builder-created layers by content digest."""
    by_digest: Dict[str, List] = {}
    for position, layer in enumerate(layers):
        with tarfile.open(layer.tar_path, mode="r:") as tar:
            for info in tar:
                if not info.isreg() or info.size == 0:
                    continue
                digest = hashlib.sha256()
                with tar.extractfile(info) as data:
                    for chunk in iter(lambda: data.read(HASH_CHUNK_SIZE), b""):
                        digest.update(chunk)
                by_digest.setdefault("sha256:" + digest.hexdigest(), []).append(
                    (position, info.name.lstrip("/"), info.size)
                )
    groups = [
        {
            "digest": digest,
            "size": entries[0][2],
            "copies": len(entries),
            "paths": [path for _, path, _ in entries],
        }
        for digest, entries in sorted(by_digest.items())
        if len(entries) > 1
    ]
    return {
        "groups": groups,
        "redundant_bytes": sum(group["size"] * (group["copies"] - 1) for group in groups),
    }


def _squash_summary(source_layers: Sequence, stats: Optional[SquashStats], squashed) -> Dict:
    summary: Dict = {"source_layers": len(source_layers), "applied": squashed is not None}
    if len(source_layers) < 2:
        summary["note"] = "Only one builder-created layer; squashing changes nothing."
        return summary
    if stats is None:
        try:
            stats = squash_layers([Path(layer.tar_path) for layer in source_layers], None)
        except SquashError as error:
            summary["unsquashable"] = str(error)
            return summary
    summary.update(
        source_bytes=stats.source_bytes,
        dropped_entries=stats.dropped_entries,
        dropped_file_bytes=stats.dropped_file_bytes,
        predicted_bytes=stats.predicted_bytes,
        measured_bytes=squashed.size if squashed is not None else stats.output_bytes,
    )
    summary["measured_savings_bytes"] = stats.source_bytes - summary["measured_bytes"]
    return summary


def analyze_platform(image, source_layers: Sequence, stats: Optional[SquashStats]) -> Dict:
    """Analyze one built platform image and its pre-squash builder-created layers."""
    builder_start = len(image.layers) - image.app_layer_count
    squashed = image.layers[-1] if stats is not None else None
    digests = [layer.digest for layer in image.layers]
    layers = []
    for position, layer in enumerate(image.layers):
        uncompressed = _uncompressed_size(layer)
        layers.append({
            "digest": layer.digest,
            "mediaType": layer.media_type,
            "origin": "builder" if position >= builder_start else "base",
            "compressed_bytes": layer.size,
            "uncompressed_bytes": uncompressed,
            "repeated_in_manifest": digests.count(layer.digest) > 1,
        })
    known = [layer["uncompressed_bytes"] for layer in layers]
    report = {
        "platform": "/".join(
            image.platform[key] for key in ("os", "architecture", "variant")
            if key in image.platform
        ),
        "layers": layers,
        "compressed_bytes": sum(layer.size for layer in image.layers),
        "uncompressed_bytes": None if None in known else sum(known),
        "repeated_content": _repeated_content(source_layers),
        "squash": _squash_summary(source_layers, stats, squashed),
    }
    report["warnings"] = _warnings(report)
    return report


def _warnings(report: Dict) -> List[str]:
    warnings = []
    squash = report["squash"]
    if not squash["applied"] and squash.get("dropped_entries"):
        warnings.append(
            f"{report['platform']}: builder layers contain {squash['dropped_entries']} "
            f"shadowed or deleted entries ({squash['dropped_file_bytes']} file bytes); "
            "--squash would remove them at the cost of layer-level cache reuse."
        )
    repeated = report["repeated_content"]
    if repeated["redundant_bytes"]:
        warnings.append(
            f"{report['platform']}: {len(repeated['groups'])} file contents are stored at "
            f"multiple paths ({repeated['redundant_bytes']} redundant bytes); squashing "
            "does not deduplicate file content."
        )
    if any(layer["uncompressed_bytes"] is None for layer in report["layers"]):
        warnings.append(
            f"{report['platform']}: some base layers use a compression this analysis "
            "cannot stream; their uncompressed size is reported as unknown."
        )
    return warnings


def shared_blobs(images: Sequence) -> List[Dict]:
    """List layer blobs referenced by more than one platform manifest."""
    owners: Dict[str, List[str]] = {}
    for image in images:
        platform = "/".join(
            image.platform[key] for key in ("os", "architecture", "variant")
            if key in image.platform
        )
        for layer in image.layers:
            if platform not in owners.setdefault(layer.digest, []):
                owners[layer.digest].append(platform)
    return [
        {"digest": digest, "platforms": platforms}
        for digest, platforms in owners.items()
        if len(platforms) > 1
    ]
