"""Offline OCI layout verification and image configuration linting."""

import hashlib
import json
import posixpath
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from fractions import Fraction
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

MIB = 1024 * 1024
SIZE_THRESHOLDS = ("max_image_size_mb", "max_layer_size_mb")
MAX_SIZE_THRESHOLD_DIGITS = 1000
MAX_SIZE_THRESHOLD_EXPONENT = 1000
STORED_SIZE_NOTE = (
    "Sizes are stored blob sizes recorded in the layout: compressed layers count compressed "
    "bytes and uncompressed tar layers count their full size. Network transfer and "
    "registry or cache reuse may differ."
)

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
    "PCB006": (
        "warning",
        "Stored image size exceeds max_image_size_mb.",
        "Reduce the base image or layer contents, or raise max_image_size_mb.",
    ),
    "PCB007": (
        "warning",
        "A stored layer exceeds max_layer_size_mb.",
        "Slim the largest layer or choose a smaller base image, or raise max_layer_size_mb.",
    ),
    "PCB008": (
        "error",
        "Image exposes an invalid or duplicate port.",
        "Expose each port once as <1-65535>/tcp, /udp or /sctp.",
    ),
    "PCB009": (
        "warning",
        "The entrypoint listener and the exposed TCP ports differ.",
        "Expose the port the server listens on, or change the listener to match exposed_ports.",
    ),
    "PCB010": (
        "info",
        "The listen-port check was skipped because the listener cannot be determined.",
        "Pass an explicit port option to a recognized server, or ignore PCB010.",
    ),
    "PCB011": (
        "warning",
        "A privileged port below 1024 is exposed while the image runs as a non-root user.",
        "Listen on a port of 1024 or higher, or ensure the runtime permits binding it.",
    ),
    "PCB012": (
        "info",
        "Non-TCP exposed ports are preserved unchanged.",
        "No action is needed if the protocol is intended; they are excluded from the listener check.",
    ),
    "PCB013": (
        "info",
        "Configured exposed ports replaced the base image's exposed ports.",
        "Add base-image ports to exposed_ports if the application still needs them.",
    ),
}

PORT_PROTOCOLS = ("tcp", "udp", "sctp")
PRIVILEGED_PORT_LIMIT = 1024
MAX_REPORTED_KEYS = 5
MAX_REPORTED_KEY_LENGTH = 32
PYTHON_EXECUTABLE = re.compile(r"python(?:\d+(?:\.\d+)*)?")
JUPYTER_SUBCOMMANDS = ("lab", "notebook", "server")
JUPYTER_MODULES = {"jupyterlab": "lab", "notebook": "notebook", "jupyter_server": "server"}
MAX_PORT_DIGITS = 5
RUNSERVER_VALUE_OPTIONS = ("--settings", "--pythonpath", "--verbosity", "-v")
RUNSERVER_FLAGS = (
    "--ipv6", "-6", "--nothreading", "--noreload", "--nostatic", "--insecure",
    "--traceback", "--no-color", "--force-color", "--skip-checks",
)
_UNKNOWN = object()


class ImageValidationError(ValueError):
    """The layout is missing, malformed, ambiguous, or fails verification."""


def validate_ignores(ignores):
    unknown = sorted(set(ignores) - RULES.keys())
    if unknown:
        names = ", ".join(repr(name) for name in unknown)
        raise ValueError(f"Unknown image validation rule ID(s): {names}.")


_DECIMAL_LITERAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def parse_size_threshold(value, name):
    """Return a positive, finite MiB threshold as an exact Decimal."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{name} must be a positive number of MiB.")
    if isinstance(value, str):
        value = value.strip()
        if not _DECIMAL_LITERAL.fullmatch(value):
            raise ValueError(f"{name} must be a positive number of MiB.")
    try:
        number = Decimal(repr(value) if isinstance(value, float) else value)
    except InvalidOperation:
        raise ValueError(f"{name} has an unsupported decimal exponent.") from None
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a positive number of MiB.")
    if (
        len(number.as_tuple().digits) > MAX_SIZE_THRESHOLD_DIGITS
        or abs(number.adjusted()) > MAX_SIZE_THRESHOLD_EXPONENT
    ):
        raise ValueError(
            f"{name} must have at most {MAX_SIZE_THRESHOLD_DIGITS} coefficient digits "
            f"and an adjusted decimal exponent between -{MAX_SIZE_THRESHOLD_EXPONENT} "
            f"and {MAX_SIZE_THRESHOLD_EXPONENT}."
        )
    return number


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
    exposed = runtime.get("ExposedPorts")
    if exposed is not None and not isinstance(exposed, dict):
        raise ImageValidationError("Image ExposedPorts must be an object.")
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
            self.images[digest] = (
                config, runtime, config_descriptor["size"],
                [(layer["digest"], layer["size"], layer["mediaType"]) for layer in layers],
            )
            subject = document.get("subject")
            self.heights[digest] = (
                1 + self._walk(subject, depth + 1, ancestors | {digest})
                if subject is not None else 0
            )
        elif depth + self.heights[digest] > MAX_GRAPH_DEPTH:
            raise ImageValidationError("OCI layout exceeds the graph nesting limit.")

        config = self.images[digest][0]
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
        config = self.images[descriptor["digest"]][0]
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
        runtime = self.images[selected["digest"]][1]
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


def _port_number(text):
    if not text or not text.isascii() or not text.isdigit():
        return None
    text = text.lstrip("0")
    if not text or len(text) > MAX_PORT_DIGITS:
        return None
    value = int(text)
    return value if 1 <= value <= 65535 else None


def _exposed_port(key):
    """Normalize an ExposedPorts key to (port, protocol); a missing protocol means tcp."""
    port, slash, protocol = key.partition("/")
    protocol = protocol.lower() if slash else "tcp"
    number = _port_number(port)
    if number is None or protocol not in PORT_PROTOCOLS:
        return None
    return number, protocol


def _display_keys(keys, redact=str):
    shown = [json.dumps(redact(key)[:MAX_REPORTED_KEY_LENGTH], ensure_ascii=True) for key in keys[:MAX_REPORTED_KEYS]]
    if len(keys) > MAX_REPORTED_KEYS:
        shown.append(f"and {len(keys) - MAX_REPORTED_KEYS} more")
    return ", ".join(shown)


def _ports_text(ports):
    return ", ".join(f"{port}/{protocol}" for port, protocol in sorted(ports))


def _ambiguous_option(arg, names):
    """True for attached/clustered short options (`-p80`, `-Rb:80`) or abbreviated long options (`--bin`)."""
    if arg.startswith("--"):
        option = arg.split("=", 1)[0]
        return any(name.startswith("--") and name != option and name.startswith(option) for name in names)
    return arg.startswith("-") and len(arg) > 2 and any(
        len(name) == 2 and name[1] in arg[1:] for name in names
    )


def _option_values(args, names):
    """Return values of `--name value`, `-n value` and `--name=value` options, stopping at `--`.

    Returns _UNKNOWN when an argument may set the option in a form this parser does not model.
    """
    values = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if arg in names:
            values.append(args[index + 1] if index + 1 < len(args) else None)
            index += 1
        else:
            prefix = next((name for name in names if name.startswith("--") and arg.startswith(name + "=")), None)
            if prefix is not None:
                values.append(arg[len(prefix) + 1:])
            elif _ambiguous_option(arg, names):
                return _UNKNOWN
            elif (arg.startswith("-") and "=" not in arg and index + 1 < len(args)
                  and args[index + 1].startswith("-")):
                # An unmodeled option may consume the following option-looking value.
                return _UNKNOWN
        index += 1
    return values


def _last_port(args, names):
    values = _option_values(args, names)
    if values is _UNKNOWN or not values:
        return _UNKNOWN
    port = _port_number(values[-1])
    return _UNKNOWN if port is None else {port}


def _address_port(address):
    """Parse the port of `port`, `host:port` or `[ipv6]:port`."""
    if address is None:
        return None
    if address.startswith("["):
        host, separator, port = address.partition("]:")
        return _port_number(port) if separator else None
    return _port_number(address.rpartition(":")[2])


def _server(argv):
    """Return (server name, arguments) for a recognized server command, else None."""
    if not argv:
        return None
    name, args = posixpath.basename(argv[0]), argv[1:]
    if PYTHON_EXECUTABLE.fullmatch(name):
        if len(args) >= 2 and args[0] == "-m":
            name, args = args[1], args[2:]
            if name in JUPYTER_MODULES:
                return "jupyter", args
            if name == "django":
                name = "django-admin"
        elif args and posixpath.basename(args[0]) == "manage.py":
            name, args = "manage.py", args[1:]
        else:
            return None
    if name.startswith("jupyter-") and name[len("jupyter-"):] in JUPYTER_SUBCOMMANDS:
        return "jupyter", args
    if name == "jupyter":
        return ("jupyter", args[1:]) if args and args[0] in JUPYTER_SUBCOMMANDS else None
    if name in ("manage.py", "django-admin"):
        return ("runserver", args[1:]) if args and args[0] == "runserver" else None
    if name in ("flask", "streamlit"):
        return (name, args[args.index("run") + 1:]) if "run" in args else None
    if name in ("uvicorn", "gunicorn", "gradio"):
        return name, args
    return None


def _runserver_port(args):
    """Parse Django `runserver [addrport]`; any unrecognized option makes the listener unknown."""
    index = 0
    ports = _UNKNOWN
    while index < len(args):
        arg = args[index]
        if arg in RUNSERVER_VALUE_OPTIONS:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                return _UNKNOWN
            index += 2
            continue
        if arg.startswith("-"):
            if arg not in RUNSERVER_FLAGS and arg.split("=", 1)[0] not in RUNSERVER_VALUE_OPTIONS:
                return _UNKNOWN
            index += 1
            continue
        port = _address_port(arg)
        if port is None or ports is not _UNKNOWN:
            return _UNKNOWN
        ports = {port}
        index += 1
    return ports


def _gunicorn_ports(args):
    binds = _option_values(args, ("-b", "--bind"))
    if binds is _UNKNOWN or not binds:
        return _UNKNOWN
    ports = set()
    for bind in binds:
        if bind is not None and bind.startswith("unix:"):
            continue
        port = None if bind is None or bind.startswith("fd://") or ":" not in bind else _address_port(bind)
        if port is None:
            return _UNKNOWN
        ports.add(port)
    return ports if ports else _UNKNOWN


def _listeners(argv):
    """Return None (unrecognized), _UNKNOWN, or the set of TCP ports named in the argv."""
    server = _server(argv)
    if server is None:
        return None
    name, args = server
    if name == "uvicorn":
        if _option_values(args, ("--uds", "--fd")):
            return _UNKNOWN
        return _last_port(args, ("--port",))
    if name == "gunicorn":
        return _gunicorn_ports(args)
    if name == "flask":
        return _last_port(args, ("--port", "-p"))
    if name == "runserver":
        return _runserver_port(args)
    if name == "streamlit":
        return _last_port(args, ("--server.port",))
    if name == "jupyter":
        return _last_port(args, ("--port", "--ServerApp.port", "--NotebookApp.port"))
    return _UNKNOWN


def _port_findings(runtime, non_root, base_exposed_ports, redact=str):
    keys = list(runtime.get("ExposedPorts") or {})
    exposed, invalid, duplicate = set(), [], []
    for key in keys:
        port = _exposed_port(key)
        if port is None:
            invalid.append(key)
        elif port in exposed:
            duplicate.append(key)
        else:
            exposed.add(port)
    triggered = []
    if invalid or duplicate:
        parts = [f"invalid: {_display_keys(invalid, redact)}" if invalid else "",
                 f"duplicate: {_display_keys(duplicate, redact)}" if duplicate else ""]
        triggered.append(("PCB008", "Image ExposedPorts has " + "; ".join(p for p in parts if p) + "."))

    argv = [*(runtime.get("Entrypoint") or []), *(runtime.get("Cmd") or [])]
    tcp = {port for port, protocol in exposed if protocol == "tcp"}
    listeners = _listeners(argv)
    if listeners is _UNKNOWN or (listeners is None and argv and keys):
        reason = "recognized server without a determinable port" if listeners is _UNKNOWN else "unrecognized command"
        triggered.append(("PCB010", f"The listen-port check was skipped ({reason}); no listener was guessed."))
    elif listeners is not None:
        unexposed, unused = listeners - tcp, tcp - listeners
        if unexposed or unused:
            parts = [f"listening on {_ports_text((p, 'tcp') for p in unexposed)} without exposing it" if unexposed else "",
                     f"exposing {_ports_text((p, 'tcp') for p in unused)} without a listener" if unused else ""]
            triggered.append(("PCB009", "The entrypoint is " + " and ".join(p for p in parts if p) + "."))

    privileged = {port for port in exposed if port[0] < PRIVILEGED_PORT_LIMIT}
    if non_root and privileged:
        triggered.append(("PCB011", f"Privileged port(s) {_ports_text(privileged)} are exposed for a non-root user."))
    other = {port for port in exposed if port[1] != "tcp"}
    if other:
        triggered.append(("PCB012", f"Non-TCP port(s) {_ports_text(other)} are preserved unchanged."))

    if base_exposed_ports:
        retained = {_exposed_port(key) or key for key in keys}
        dropped = [key for key in base_exposed_ports if (_exposed_port(key) or key) not in retained]
        if dropped:
            triggered.append((
                "PCB013",
                f"Configured exposed ports replaced base-image port(s) {_display_keys(dropped, redact)}.",
            ))
    return triggered


def _is_root(runtime):
    user = (runtime.get("User") or "").split(":", 1)[0]
    return not user or user == "root" or (user.isascii() and user.isdecimal() and not user.strip("0"))


def _findings(runtime, ignores, base_exposed_ports=None, redact=str):
    triggered = []
    root = _is_root(runtime)
    if root:
        triggered.append(("PCB001", None))
    if not runtime.get("Entrypoint") and not runtime.get("Cmd"):
        triggered.append(("PCB003", None))
    labels = runtime.get("Labels") or {}
    if not labels.get(SOURCE_LABEL, "").strip() or not labels.get(VERSION_LABEL, "").strip():
        triggered.append(("PCB004", None))
    for entry in runtime.get("Env") or []:
        name, value = entry.split("=", 1)
        if value and any(part in name.upper() for part in ("PASSWORD", "TOKEN", "SECRET")):
            triggered.append(("PCB005", None))
            break
    triggered.extend(_port_findings(runtime, not root, base_exposed_ports, redact))
    findings = []
    for rule_id, message in triggered:
        if rule_id in ignores:
            continue
        severity, default_message, remediation = RULES[rule_id]
        findings.append({
            "rule_id": rule_id, "severity": severity,
            "message": message or default_message, "remediation": remediation,
        })
    return findings


def _compression(media_type):
    if media_type.endswith(("+gzip", ".tar.gzip")):
        return "gzip"
    if media_type.endswith("+zstd"):
        return "zstd"
    if media_type.endswith(".tar"):
        return "none"
    return "unknown"


def _size_report(config_size, layers, base_layer_count, thresholds, redact=str):
    if base_layer_count is not None and not 0 <= base_layer_count <= len(layers):
        raise ImageValidationError("Base layer count does not match the selected image.")
    entries = []
    for position, (digest, size, media_type) in enumerate(layers):
        if base_layer_count is None:
            role = "unknown"
        else:
            role = "base" if position < base_layer_count else "application"
        entries.append({
            "digest": digest, "bytes": size, "media_type": redact(media_type),
            "compression": _compression(media_type), "role": role,
        })
    largest = max(entries, key=lambda entry: entry["bytes"], default=None)
    if base_layer_count is None:
        contributions = {"status": "unknown", "base_bytes": None, "application_bytes": None}
    else:
        contributions = {
            "status": "determined",
            "base_bytes": sum(entry["bytes"] for entry in entries if entry["role"] == "base"),
            "application_bytes": sum(
                entry["bytes"] for entry in entries if entry["role"] == "application"
            ),
        }
    return {
        "basis": "stored",
        "image_bytes": config_size + sum(entry["bytes"] for entry in entries),
        "config_bytes": config_size,
        "largest_layer": (
            None if largest is None else
            {key: largest[key] for key in ("digest", "bytes", "media_type")}
        ),
        "contributions": contributions,
        "layers": entries,
        "thresholds": {
            name: None if thresholds.get(name) is None else str(thresholds[name])
            for name in SIZE_THRESHOLDS
        },
        "note": STORED_SIZE_NOTE,
    }


def _size_findings(size, thresholds, ignores):
    measured = {
        "max_image_size_mb": ("PCB006", "Stored image size", size["image_bytes"]),
        "max_layer_size_mb": (
            "PCB007", "Largest stored layer",
            size["largest_layer"]["bytes"] if size["largest_layer"] else 0,
        ),
    }
    findings = []
    for name in SIZE_THRESHOLDS:
        threshold = thresholds.get(name)
        rule_id, subject, actual = measured[name]
        if threshold is None or rule_id in ignores or actual <= Fraction(threshold) * MIB:
            continue
        severity, _, remediation = RULES[rule_id]
        findings.append({
            "rule_id": rule_id,
            "severity": severity,
            "message": (
                f"{subject} is {actual} bytes ({actual / MIB:.2f} MiB), "
                f"above {name} = {threshold}."
            ),
            "remediation": remediation,
        })
    return findings


def validate_image(
    output, tag=None, platform=None, ignores=(), base_exposed_ports=None, redact=str, *,
    thresholds=None, expected_digest=None, base_layer_count=None,
):
    """Verify every reachable blob, then lint one unambiguously selected image.

    ``base_layer_count`` attributes the first layers of the selected manifest to the base
    image. It requires ``expected_digest``, the manifest digest the caller built.
    ``base_exposed_ports`` lists known base ports; ``redact`` sanitizes diagnostic keys and
    reported layer media types.
    """
    if base_layer_count is not None and expected_digest is None:
        raise ValueError("Base layer attribution requires the expected manifest digest.")
    if base_layer_count is not None and type(base_layer_count) is not int:
        raise ValueError("Base layer count must be an integer.")
    validate_ignores(ignores)
    if thresholds is None:
        thresholds = {}
    elif not isinstance(thresholds, Mapping):
        raise ValueError("Image size thresholds must be a mapping or None.")
    if any(not isinstance(name, str) for name in thresholds):
        raise ValueError("Image size threshold names must be strings.")
    unknown = sorted(set(thresholds) - set(SIZE_THRESHOLDS))
    if unknown:
        raise ValueError(f"Unknown image size threshold(s): {', '.join(unknown)}.")
    thresholds = {
        name: parse_size_threshold(value, name)
        for name, value in thresholds.items() if value is not None
    }
    layout = _VerifiedLayout(output)
    index, entries = layout.verify()
    digest, selected_platform, runtime = layout.select(index, entries, tag, platform)
    if expected_digest is not None and digest != expected_digest:
        raise ImageValidationError("The built image no longer matches the selected layout manifest.")
    _, _, config_size, layers = layout.images[digest]
    size = _size_report(config_size, layers, base_layer_count, thresholds, redact)
    ignored = set(ignores)
    return {
        "image": {"digest": digest, "platform": "/".join(part for part in selected_platform if part)},
        "provenance": {"status": "unknown"},
        "findings": (
            _findings(runtime, ignored, base_exposed_ports, redact)
            + _size_findings(size, thresholds, ignored)
        ),
        "size": size,
    }


def finding_exit_code(report, fail_on="warning"):
    if fail_on not in ("warning", "error"):
        raise ValueError("Failure threshold must be warning or error.")
    severities = {"error"} if fail_on == "error" else {"warning", "error"}
    return int(any(finding["severity"] in severities for finding in report["findings"]))
