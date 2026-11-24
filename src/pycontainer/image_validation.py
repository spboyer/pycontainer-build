"""Offline OCI layout verification and image configuration linting."""

import hashlib
import json
from pathlib import Path

from .blob_store import digest_filename, hash_file
from .builder import (
    IMAGE_CONFIG_MEDIA_TYPES,
    IMAGE_MANIFEST_MEDIA_TYPES,
    OCI_INDEX_MEDIA_TYPES,
    UnsupportedPlatformError,
    _select_index_descriptor,
    parse_platform,
    parse_platform_variant,
)


MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_GRAPH_METADATA_BYTES = 64 * 1024 * 1024
MAX_INDEX_ENTRIES = 1_000
MAX_IMAGE_LAYERS = 1_000
MAX_GRAPH_DESCRIPTORS = 100_000
MAX_GRAPH_DEPTH = 32
MAX_LAYER_BYTES = 64 * 1024 * 1024 * 1024

REF_NAME = "org.opencontainers.image.ref.name"
CONTAINERD_NAME = "io.containerd.image.name"
SOURCE_LABEL = "org.opencontainers.image.source"
VERSION_LABEL = "org.opencontainers.image.version"

RULES = {
    "PCB001": (
        "warning",
        "Image runs as root.",
        "Set a non-root User in the image configuration.",
    ),
    # Reserved until an independently verified base-image provenance source exists.
    "PCB002": None,
    "PCB003": (
        "error",
        "Image has neither an Entrypoint nor a Cmd.",
        "Set an Entrypoint or Cmd for the image.",
    ),
    "PCB004": (
        "warning",
        "Image is missing a source or version label.",
        "Set org.opencontainers.image.source and org.opencontainers.image.version labels.",
    ),
    "PCB005": (
        "error",
        "Image contains a non-empty secret-like environment variable.",
        "Supply secrets at runtime rather than embedding them in the image.",
    ),
}


class ImageValidationError(ValueError):
    """The layout is missing, malformed, ambiguous, or fails verification."""


def validate_ignores(ignores):
    unknown = sorted(set(ignores) - RULES.keys())
    if unknown:
        names = ", ".join(repr(name) for name in unknown)
        raise ValueError(f"Unknown image validation rule ID(s): {names}.")


def _descriptor(value, description):
    if not isinstance(value, dict):
        raise ImageValidationError(f"{description} is not a descriptor object.")
    digest, size, media_type = value.get("digest"), value.get("size"), value.get("mediaType")
    try:
        digest_filename(digest)
    except ValueError:
        raise ImageValidationError(f"{description} has an invalid digest.") from None
    if type(size) is not int or size < 0:
        raise ImageValidationError(f"{description} has an invalid size.")
    if not isinstance(media_type, str) or not media_type:
        raise ImageValidationError(f"{description} has an invalid media type.")
    return digest, size, media_type


def _regular_file(path, description):
    if path.is_symlink() or not path.is_file():
        raise ImageValidationError(f"{description} {str(path)!r} is missing or not a regular file.")


def _read_metadata_file(path, description):
    _regular_file(path, description)
    with path.open("rb") as source:
        data = source.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise ImageValidationError(f"{description} {str(path)!r} exceeds the metadata size limit.")
    return data


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("Non-standard JSON constant.")


def _json_object(data, description):
    try:
        result = json.loads(
            data, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ImageValidationError(f"{description} is not valid JSON.") from None
    if not isinstance(result, dict):
        raise ImageValidationError(f"{description} is not a JSON object.")
    return result


def _platform(descriptor):
    value = descriptor.get("platform")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ImageValidationError("Image descriptor has an invalid platform.")
    os_name, architecture, variant = value.get("os"), value.get("architecture"), value.get("variant")
    if not isinstance(os_name, str) or not os_name or not isinstance(architecture, str) or not architecture:
        raise ImageValidationError("Image descriptor has an invalid platform.")
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise ImageValidationError("Image descriptor has an invalid platform variant.")
    return os_name, architecture, variant


def _image_config(config, layer_count):
    os_name, architecture = config.get("os"), config.get("architecture")
    variant = config.get("variant")
    if not isinstance(os_name, str) or not os_name or not isinstance(architecture, str) or not architecture:
        raise ImageValidationError("Image config must declare its os and architecture.")
    if variant is not None and (not isinstance(variant, str) or not variant):
        raise ImageValidationError("Image config has an invalid platform variant.")

    rootfs = config.get("rootfs")
    if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
        raise ImageValidationError("Image config has an invalid rootfs.")
    diff_ids = rootfs.get("diff_ids")
    if not isinstance(diff_ids, list) or len(diff_ids) != layer_count:
        raise ImageValidationError("Image config DiffID count does not match its layers.")
    for digest in diff_ids:
        try:
            digest_filename(digest)
        except ValueError:
            raise ImageValidationError("Image config has an invalid DiffID.") from None

    runtime = config.get("config")
    if runtime is None:
        runtime = {}
    if not isinstance(runtime, dict):
        raise ImageValidationError("Image runtime config is not an object.")
    user = runtime.get("User")
    if user is not None and not isinstance(user, str):
        raise ImageValidationError("Image User is not a string.")
    for key in ("Entrypoint", "Cmd"):
        value = runtime.get(key)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            raise ImageValidationError(f"Image {key} must be an array of strings.")
    env = runtime.get("Env")
    if env is not None and (
        not isinstance(env, list)
        or any(not isinstance(item, str) or "=" not in item or not item.split("=", 1)[0] for item in env)
    ):
        raise ImageValidationError("Image environment contains an invalid entry.")
    labels = runtime.get("Labels")
    if labels is not None and (
        not isinstance(labels, dict)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items())
    ):
        raise ImageValidationError("Image labels must be strings.")
    return runtime


class _VerifiedLayout:
    def __init__(self, root):
        self.root = Path(root)
        self.indexes = {}
        self.images = {}
        self.heights = {}
        self.kinds = {}
        self.verified_sizes = {}
        self.objects = {}
        self.metadata_bytes = 0
        self.descriptor_count = 0

    def _count(self):
        self.descriptor_count += 1
        if self.descriptor_count > MAX_GRAPH_DESCRIPTORS:
            raise ImageValidationError("OCI layout exceeds the descriptor graph limit.")

    def _blob_path(self, digest):
        name = digest_filename(digest)
        blobs = self.root / "blobs"
        for directory in (blobs, blobs / "sha256"):
            if directory.is_symlink():
                raise ImageValidationError(f"OCI layout directory {str(directory)!r} must not be a symbolic link.")
        path = blobs / "sha256" / name
        _regular_file(path, "OCI layout blob")
        return path

    def _metadata(self, descriptor, description):
        digest, size, _ = _descriptor(descriptor, description)
        if size > MAX_METADATA_BYTES:
            raise ImageValidationError(f"{description} exceeds the metadata size limit.")
        if digest in self.verified_sizes and self.verified_sizes[digest] != size:
            raise ImageValidationError(f"{description} has a conflicting descriptor size.")
        if digest not in self.objects:
            path = self._blob_path(digest)
            data = _read_metadata_file(path, description)
            if len(data) != size:
                raise ImageValidationError(f"{description} {str(path)!r} has a size mismatch.")
            if f"sha256:{hashlib.sha256(data).hexdigest()}" != digest:
                raise ImageValidationError(f"{description} {str(path)!r} has a digest mismatch.")
            self.metadata_bytes += len(data)
            if self.metadata_bytes > MAX_GRAPH_METADATA_BYTES:
                raise ImageValidationError("OCI layout exceeds the metadata graph size limit.")
            self.objects[digest] = _json_object(data, description)
            self.verified_sizes[digest] = size
        return self.objects[digest]

    def _layer(self, descriptor):
        self._count()
        digest, size, media_type = _descriptor(descriptor, "Image layer descriptor")
        if media_type in OCI_INDEX_MEDIA_TYPES | IMAGE_MANIFEST_MEDIA_TYPES | IMAGE_CONFIG_MEDIA_TYPES:
            raise ImageValidationError("Image layer has a conflicting metadata media type.")
        if size > MAX_LAYER_BYTES:
            raise ImageValidationError("Image layer exceeds the layer size limit.")
        if digest in self.verified_sizes:
            if self.verified_sizes[digest] != size:
                raise ImageValidationError("Image layer has a conflicting descriptor size.")
            return
        path = self._blob_path(digest)
        if path.stat().st_size != size:
            raise ImageValidationError(f"Image layer {str(path)!r} has a size mismatch.")
        actual_digest, actual_size = hash_file(path)
        if actual_size != size:
            raise ImageValidationError(f"Image layer {str(path)!r} has a size mismatch.")
        if actual_digest != digest:
            raise ImageValidationError(f"Image layer {str(path)!r} has a digest mismatch.")
        self.verified_sizes[digest] = size

    def _walk(self, descriptor, depth, ancestors):
        self._count()
        digest, _, media_type = _descriptor(descriptor, "Image manifest descriptor")
        if depth > MAX_GRAPH_DEPTH:
            raise ImageValidationError("OCI layout exceeds the graph nesting limit.")
        if media_type not in OCI_INDEX_MEDIA_TYPES | IMAGE_MANIFEST_MEDIA_TYPES:
            raise ImageValidationError("Image manifest has an unsupported media type.")
        if digest in ancestors:
            raise ImageValidationError("OCI layout contains a cyclic index.")
        kind = "index" if media_type in OCI_INDEX_MEDIA_TYPES else "manifest"
        previous_kind = self.kinds.setdefault(digest, kind)
        if previous_kind != kind:
            raise ImageValidationError("One blob has conflicting index and manifest media types.")

        document = self._metadata(descriptor, "Image manifest")
        declared_type = document.get("mediaType")
        if declared_type is not None and declared_type != media_type:
            raise ImageValidationError("Image manifest media type does not match its descriptor.")
        if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 2:
            raise ImageValidationError("Image manifest or index must use schemaVersion 2.")

        if media_type in OCI_INDEX_MEDIA_TYPES:
            if digest in self.indexes:
                if depth + self.heights[digest] > MAX_GRAPH_DEPTH:
                    raise ImageValidationError("OCI layout exceeds the graph nesting limit.")
                return self.heights[digest]
            children = self._index_entries(document)
            references = [*children]
            if document.get("subject") is not None:
                references.append(document["subject"])
            height = 0
            for child in references:
                height = max(height, 1 + self._walk(child, depth + 1, ancestors | {digest}))
            self.indexes[digest] = children
            self.heights[digest] = height
            return height

        if digest not in self.images:
            config_descriptor = document.get("config")
            self._count()
            _, _, config_type = _descriptor(config_descriptor, "Image config descriptor")
            if config_type not in IMAGE_CONFIG_MEDIA_TYPES:
                raise ImageValidationError("Image config has an unsupported media type.")
            layers = document.get("layers")
            if not isinstance(layers, list):
                raise ImageValidationError("Image manifest does not contain a layer list.")
            if len(layers) > MAX_IMAGE_LAYERS:
                raise ImageValidationError("Image manifest exceeds the layer count limit.")
            config = self._metadata(config_descriptor, "Image config")
            runtime = _image_config(config, len(layers))
            for layer in layers:
                self._layer(layer)
            self.images[digest] = (config, runtime)
            subject = document.get("subject")
            self.heights[digest] = (
                1 + self._walk(subject, depth + 1, ancestors | {digest})
                if subject is not None else 0
            )
        elif depth + self.heights[digest] > MAX_GRAPH_DEPTH:
            raise ImageValidationError("OCI layout exceeds the graph nesting limit.")

        config, _ = self.images[digest]
        descriptor_platform = _platform(descriptor)
        if descriptor_platform:
            actual = (config["os"], config["architecture"], config.get("variant"))
            if descriptor_platform[:2] != actual[:2] or (
                descriptor_platform[2] and actual[2] and descriptor_platform[2] != actual[2]
            ):
                raise ImageValidationError("Image descriptor platform does not match its config.")
        return self.heights[digest]

    @staticmethod
    def _index_entries(index):
        entries = index.get("manifests")
        if not isinstance(entries, list) or not entries:
            raise ImageValidationError("OCI image index lists no manifests.")
        if len(entries) > MAX_INDEX_ENTRIES:
            raise ImageValidationError("OCI image index exceeds the manifest count limit.")
        return entries

    def verify(self):
        marker_path = self.root / "oci-layout"
        marker = _json_object(_read_metadata_file(marker_path, "OCI layout marker"), "OCI layout marker")
        if marker.get("imageLayoutVersion") != "1.0.0":
            raise ImageValidationError("OCI layout marker has an unsupported imageLayoutVersion.")
        index_path = self.root / "index.json"
        index = _json_object(_read_metadata_file(index_path, "OCI layout index"), "OCI layout index")
        if type(index.get("schemaVersion")) is not int or index["schemaVersion"] != 2:
            raise ImageValidationError("OCI layout index must use schemaVersion 2.")
        media_type = index.get("mediaType")
        if media_type is not None and (
            not isinstance(media_type, str) or media_type not in OCI_INDEX_MEDIA_TYPES
        ):
            raise ImageValidationError("OCI layout index has an unsupported media type.")
        entries = self._index_entries(index)
        for descriptor in entries:
            self._walk(descriptor, 0, set())
        if index.get("subject") is not None:
            self._walk(index["subject"], 0, set())
        return index, entries

    def _resolved_platform(self, descriptor):
        config, _ = self.images[descriptor["digest"]]
        declared = _platform(descriptor)
        variant = (declared[2] if declared else None) or config.get("variant")
        return config["os"], config["architecture"], variant

    def _select_index(self, descriptors, requested):
        platforms = set()
        for descriptor in descriptors:
            platform = _platform(descriptor)
            if platform is not None:
                platforms.add("/".join(part for part in platform if part))
        available = ", ".join(
            json.dumps(text, ensure_ascii=True)[1:-1] for text in sorted(platforms)
        ) or "none"
        if requested is not None:
            try:
                selected, _ = _select_index_descriptor({"manifests": descriptors}, *requested)
            except UnsupportedPlatformError:
                raise ImageValidationError(
                    f"No unique image matches the requested platform. Available platforms: {available}."
                ) from None
        elif len(descriptors) == 1:
            selected = descriptors[0]
        else:
            raise ImageValidationError(
                f"Image index has {len(descriptors)} manifests; select one with --platform. "
                f"Available platforms: {available}."
            )
        if selected["digest"] in self.indexes:
            raise ImageValidationError("Nested indexes are not supported for image selection.")
        return selected

    def select(self, index, entries, tag, platform):
        requested = None
        if platform is not None:
            os_name, architecture = parse_platform(platform)
            requested = os_name, architecture, parse_platform_variant(platform)

        if tag is not None:
            if not tag:
                raise ImageValidationError("Image tag must not be empty.")
            if tag.startswith("@sha256:"):
                candidates = [entry for entry in entries if entry["digest"] == tag[1:]]
            else:
                candidates = [entry for entry in entries if tag in _references(entry)]
                if not candidates and len(entries) == 1 and tag in _references(index):
                    candidates = entries
            if not candidates:
                raise ImageValidationError("OCI layout has no image matching the requested tag.")
        else:
            names = {next(iter(_references(entry)), None) for entry in entries}
            if len(names) > 1:
                raise ImageValidationError("OCI layout contains multiple images; select one with --tag.")
            candidates = entries

        if len(candidates) > 1 and any(_platform(entry) is None for entry in candidates):
            raise ImageValidationError("Multiple image manifests must declare their platforms.")
        if len(candidates) == 1 and candidates[0]["digest"] in self.indexes:
            selected = self._select_index(self.indexes[candidates[0]["digest"]], requested)
        elif len(candidates) == 1 and _platform(candidates[0]) is None:
            selected = candidates[0]
        else:
            selected = self._select_index(candidates, requested)
        _, runtime = self.images[selected["digest"]]
        actual = self._resolved_platform(selected)
        if requested is not None and (
            actual[:2] != requested[:2] or (requested[2] is not None and actual[2] != requested[2])
        ):
            raise ImageValidationError("Image config platform does not match the requested platform.")
        return selected["digest"], actual, runtime


def _references(descriptor):
    annotations = descriptor.get("annotations")
    if not isinstance(annotations, dict):
        return ()
    return tuple(
        name for key in (REF_NAME, CONTAINERD_NAME)
        if isinstance((name := annotations.get(key)), str) and name
    )


def _findings(runtime, ignores):
    triggered = []
    user = (runtime.get("User") or "").split(":", 1)[0]
    if not user or user == "root" or (user.isascii() and user.isdecimal() and not user.strip("0")):
        triggered.append("PCB001")
    if not runtime.get("Entrypoint") and not runtime.get("Cmd"):
        triggered.append("PCB003")
    labels = runtime.get("Labels") or {}
    if not labels.get(SOURCE_LABEL, "").strip() or not labels.get(VERSION_LABEL, "").strip():
        triggered.append("PCB004")
    for entry in runtime.get("Env") or []:
        name, value = entry.split("=", 1)
        if value and any(part in name.upper() for part in ("PASSWORD", "TOKEN", "SECRET")):
            triggered.append("PCB005")
            break
    findings = []
    for rule_id in triggered:
        if rule_id in ignores:
            continue
        severity, message, remediation = RULES[rule_id]
        findings.append(
            {"rule_id": rule_id, "severity": severity, "message": message, "remediation": remediation}
        )
    return findings


def validate_image(output, tag=None, platform=None, ignores=()):
    """Verify every reachable blob, then lint one unambiguously selected image."""
    validate_ignores(ignores)
    layout = _VerifiedLayout(output)
    index, entries = layout.verify()
    digest, selected_platform, runtime = layout.select(index, entries, tag, platform)
    return {
        "image": {"digest": digest, "platform": "/".join(part for part in selected_platform if part)},
        "provenance": {"status": "unknown"},
        "findings": _findings(runtime, set(ignores)),
    }


def finding_exit_code(report, fail_on="warning"):
    if fail_on not in ("warning", "error"):
        raise ValueError("Failure threshold must be warning or error.")
    severities = {"error"} if fail_on == "error" else {"warning", "error"}
    return int(any(finding["severity"] in severities for finding in report["findings"]))
