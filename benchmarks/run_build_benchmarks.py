"""Record repeatable local build measurements without external registry or runtime access."""
import argparse
import base64
import contextlib
import csv
import ctypes
import hashlib
import http.server
import io
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pycontainer.builder as builder_module
from pycontainer.builder import ImageBuilder
from pycontainer.cache import LayerCache
from pycontainer.config import BuildConfig
from pycontainer.registry_client import RegistryClient


def _peak_rss_bytes():
    if os.name == "nt":
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = kernel32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize

    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _start_benchmark_registry(root):
    root.mkdir(parents=True, exist_ok=True)
    blob_dir = root / "blobs"
    upload_dir = root / "uploads"
    blob_dir.mkdir()
    upload_dir.mkdir()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            self.server.stats["heads"] += 1
            digest = urlsplit(self.path).path.rsplit("/", 1)[-1]
            blob = self.server.blob_dir / digest.split(":", 1)[-1]
            self.send_response(200 if blob.is_file() else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            upload_id = uuid.uuid4().hex
            self.server.uploads[upload_id] = (
                self.server.upload_dir / f"{upload_id}.tmp"
            )
            self.send_response(202)
            self.send_header(
                "Location", f"/upload/{upload_id}?session=benchmark"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self):
            parsed = urlsplit(self.path)
            upload_id = parsed.path.rsplit("/", 1)[-1]
            digest = parse_qs(parsed.query).get("digest", [None])[0]
            staged = self.server.uploads.pop(upload_id, None)
            if staged is None or not digest or not digest.startswith("sha256:"):
                self.send_error(400)
                return

            hasher = hashlib.sha256()
            remaining = int(self.headers["Content-Length"])
            size = 0
            try:
                with staged.open("wb") as target:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ConnectionError("truncated benchmark upload")
                        target.write(chunk)
                        hasher.update(chunk)
                        size += len(chunk)
                        remaining -= len(chunk)
                actual_digest = f"sha256:{hasher.hexdigest()}"
                if actual_digest != digest:
                    staged.unlink(missing_ok=True)
                    self.send_error(400)
                    return
                destination = self.server.blob_dir / digest.split(":", 1)[1]
                os.replace(staged, destination)
                self.server.stats["puts"] += 1
                self.server.stats["uploaded_bytes"] += size
            except OSError:
                staged.unlink(missing_ok=True)
                self.send_error(500)
                return

            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            digest = urlsplit(self.path).path.rsplit("/", 1)[-1]
            blob = self.server.blob_dir / digest.split(":", 1)[-1]
            if not blob.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(blob.stat().st_size))
            self.end_headers()
            with blob.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=1024 * 1024)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.blob_dir = blob_dir
    server.upload_dir = upload_dir
    server.uploads = {}
    server.stats = {"heads": 0, "puts": 0, "uploaded_bytes": 0}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}/v2"


def _write_transfer_fixture(path, size, seed):
    path.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    prefix = f"{seed:08d}".encode()
    chunk = prefix + b"x" * (1024 * 1024 - len(prefix))
    remaining = size
    with path.open("wb") as target:
        while remaining:
            part = chunk[: min(len(chunk), remaining)]
            target.write(part)
            hasher.update(part)
            remaining -= len(part)
    return f"sha256:{hasher.hexdigest()}"


def _measure_transfer_worker(spec):
    source = Path(spec["source"])
    destination = Path(spec["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    registry = urlsplit(spec["registry_url"])
    client = RegistryClient(registry.netloc, "benchmark", insecure=True)

    tracemalloc.start()
    started = time.perf_counter()
    client.push_blob(spec["digest"], source, check_exists=False)
    client.pull_blob(spec["digest"], destination, spec["size_bytes"])
    wall_seconds = time.perf_counter() - started
    _, python_peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "wall_seconds": wall_seconds,
        "process_peak_rss_bytes": _peak_rss_bytes(),
        "python_tracemalloc_peak_bytes": python_peak_bytes,
        "output_bytes": 2 * spec["size_bytes"],
        "app_layer_bytes": 0,
        "dependency_layer_bytes": 0,
        "cache_lookups": 0,
        "cache_hits": 0,
        "dependency_cache_lookups": 0,
        "dependency_cache_hits": 0,
        "uploaded_bytes": spec["size_bytes"],
        "downloaded_bytes": spec["size_bytes"],
    }


NETWORK_REGISTRY_HOST = "benchmark.registry.invalid"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
THROTTLE_CHUNK_BYTES = 64 * 1024
MIB = 1024 * 1024


class _BandwidthLimiter:
    """Pace bytes through a virtual link of a fixed rate."""

    def __init__(self, bytes_per_second):
        self.bytes_per_second = bytes_per_second
        self._next_free = time.perf_counter()
        self._lock = threading.Lock()

    def wait(self, size):
        with self._lock:
            now = time.perf_counter()
            self._next_free = max(self._next_free, now) + size / self.bytes_per_second
            ready_at = self._next_free
        delay = ready_at - time.perf_counter()
        if delay > 0:
            time.sleep(delay)


def _serve_network_registry(spec):
    """Serve a minimal Registry v2 API with injected delay and bandwidth limits.

    Runs in its own process so serving work does not share the measured
    client's interpreter, GIL or CPU accounting. Uploaded blobs are stored by
    their declared digest without re-hashing: the client already verifies
    local content before upload, and duplicating that work here would only
    add contention.
    """
    storage = Path(spec["storage"])
    delay_seconds = spec["request_delay_ms"] / 1000
    rate = spec["bandwidth_mbit"] * 1_000_000 / 8 if spec["bandwidth_mbit"] else None
    shared = spec["bandwidth_scope"] == "shared"
    directions = {"down": _BandwidthLimiter(rate), "up": _BandwidthLimiter(rate)} if rate else {}
    stats = {}
    stats_lock = threading.Lock()

    def limiter(direction):
        if not rate:
            return None
        return directions[direction] if shared else _BandwidthLimiter(rate)

    def repo_dir(repository):
        if not repository or any(part in {"", ".", ".."} for part in repository.split("/")):
            raise ValueError("invalid repository")
        return storage / repository.replace("/", "__")

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _route(self):
            path = urlsplit(self.path).path
            if not path.startswith("/v2/"):
                return None
            rest = path[len("/v2/"):]
            if "/blobs/uploads/" in rest:
                repository, upload = rest.split("/blobs/uploads/", 1)
                return repository, "upload", upload
            for kind in ("manifests", "blobs"):
                marker = f"/{kind}/"
                if marker in rest:
                    repository, reference = rest.rsplit(marker, 1)
                    return repository, kind, reference
            return None

        def _count(self, repository):
            with stats_lock:
                stats[repository] = stats.get(repository, 0) + 1

        def _respond(self, status, headers=None, body=b""):
            time.sleep(delay_seconds)
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def _blob_path(self, repository, digest):
            if not digest.startswith("sha256:") or len(digest) != 71:
                raise ValueError("invalid digest")
            return repo_dir(repository) / "blobs" / digest.split(":", 1)[1]

        def do_GET(self):
            if self.path.startswith("/__stats__/"):
                repository = self.path[len("/__stats__/"):]
                body = json.dumps({"requests": stats.get(repository, 0)}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._read(send_body=True)

        def do_HEAD(self):
            self._read(send_body=False)

        def _read(self, send_body):
            route = self._route()
            if route is None or route[1] == "upload":
                self._respond(404)
                return
            repository, kind, reference = route
            self._count(repository)
            try:
                if kind == "manifests":
                    target = repo_dir(repository) / "manifests" / reference.replace(":", "_")
                    if not target.is_file():
                        self._respond(404)
                        return
                    content = target.read_bytes()
                    headers = {
                        "Content-Type": OCI_MANIFEST_MEDIA_TYPE,
                        "Docker-Content-Digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
                    }
                    self._respond(200, headers, content if send_body else b"")
                    return
                blob = self._blob_path(repository, reference)
            except ValueError:
                self._respond(400)
                return
            if not blob.is_file():
                self._respond(404)
                return
            size = blob.stat().st_size
            time.sleep(delay_seconds)
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if not send_body:
                return
            pace = limiter("down")
            with blob.open("rb") as source:
                while chunk := source.read(THROTTLE_CHUNK_BYTES):
                    if pace:
                        pace.wait(len(chunk))
                    self.wfile.write(chunk)

        def do_POST(self):
            route = self._route()
            if route is None or route[1] != "upload":
                self._respond(404)
                return
            self._count(route[0])
            upload_id = uuid.uuid4().hex
            self._respond(202, {"Location": f"/v2/{route[0]}/blobs/uploads/{upload_id}"})

        def _read_body(self, target):
            remaining = int(self.headers.get("Content-Length", "0"))
            pace = limiter("up")
            while remaining:
                chunk = self.rfile.read(min(THROTTLE_CHUNK_BYTES, remaining))
                if not chunk:
                    raise ConnectionError("truncated benchmark upload")
                if pace:
                    pace.wait(len(chunk))
                target.write(chunk)
                remaining -= len(chunk)

        def do_PUT(self):
            route = self._route()
            if route is None:
                self._respond(404)
                return
            repository, kind, reference = route
            self._count(repository)
            try:
                if kind == "manifests":
                    content = io.BytesIO()
                    self._read_body(content)
                    manifests = repo_dir(repository) / "manifests"
                    manifests.mkdir(parents=True, exist_ok=True)
                    digest = f"sha256:{hashlib.sha256(content.getvalue()).hexdigest()}"
                    for name in (reference, digest):
                        (manifests / name.replace(":", "_")).write_bytes(content.getvalue())
                    self._respond(201, {"Docker-Content-Digest": digest})
                    return
                if kind != "upload":
                    self._respond(405)
                    return
                digest = parse_qs(urlsplit(self.path).query).get("digest", [""])[0]
                destination = self._blob_path(repository, digest)
            except ValueError:
                self._respond(400)
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                with staged.open("wb") as target:
                    self._read_body(target)
                os.replace(staged, destination)
            except (OSError, ConnectionError):
                staged.unlink(missing_ok=True)
                self._respond(500)
                return
            self._respond(201, {"Docker-Content-Digest": digest})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    print(f"PORT {server.server_port}", flush=True)
    watcher = threading.Thread(
        target=lambda: (sys.stdin.read(), server.shutdown()), daemon=True
    )
    watcher.start()
    server.serve_forever()
    server.server_close()


@contextlib.contextmanager
def _network_registry(storage, request_delay_ms, bandwidth_mbit, bandwidth_scope):
    spec = {
        "storage": str(storage),
        "request_delay_ms": request_delay_ms,
        "bandwidth_mbit": bandwidth_mbit,
        "bandwidth_scope": bandwidth_scope,
    }
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--serve-registry", json.dumps(spec)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = process.stdout.readline().strip()
        if not line.startswith("PORT "):
            raise RuntimeError(f"Benchmark registry failed to start: {line!r}")
        yield f"http://127.0.0.1:{int(line.split()[1])}"
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _parse_layout(value):
    """Parse ``COUNTxMIB`` or ``MIB+MIB+...`` into per-layer byte sizes."""
    try:
        if "x" in value:
            count, size = value.split("x", 1)
            sizes = [float(size)] * int(count)
        else:
            sizes = [float(size) for size in value.split("+")]
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid layer layout {value!r}") from error
    layer_bytes = [int(size * MIB) for size in sizes]
    if not layer_bytes or any(size < 1 for size in layer_bytes):
        raise argparse.ArgumentTypeError(f"invalid layer layout {value!r}")
    return {"name": value, "layer_bytes": layer_bytes}


def _seed_base_image(storage, repository, layer_bytes, seed):
    """Write a synthetic base image directly into registry storage."""
    blobs = storage / repository / "blobs"
    layers = []
    for index, size in enumerate(layer_bytes):
        staged = blobs / f"layer-{index}.tmp"
        digest = _write_transfer_fixture(staged, size, seed * 1000 + index)
        os.replace(staged, blobs / digest.split(":", 1)[1])
        layers.append({"mediaType": OCI_LAYER_MEDIA_TYPE, "digest": digest, "size": size})
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Env": ["PATH=/usr/local/bin:/usr/bin:/bin"], "WorkingDir": "/"},
            "rootfs": {"type": "layers", "diff_ids": [layer["digest"] for layer in layers]},
            "history": [{"comment": f"benchmark seed {seed}"}],
        },
        separators=(",", ":"),
    ).encode()
    config_digest = f"sha256:{hashlib.sha256(config).hexdigest()}"
    (blobs / config_digest.split(":", 1)[1]).write_bytes(config)
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "config": {
                "mediaType": OCI_CONFIG_MEDIA_TYPE,
                "digest": config_digest,
                "size": len(config),
            },
            "layers": layers,
        },
        separators=(",", ":"),
    ).encode()
    manifests = storage / repository / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "latest").write_bytes(manifest)


def _registry_requests(registry_url, repository):
    with urllib.request.urlopen(f"{registry_url}/__stats__/{repository}", timeout=30) as response:
        return json.loads(response.read())["requests"]


def _network_client(registry_url, repository):
    return RegistryClient(urlsplit(registry_url).netloc, repository, insecure=True)


def _measure_network_build_worker(spec):
    """Measure the real sequential pull -> local build -> push product path."""
    import pycontainer.builder as builder_module
    from pycontainer.auth import RegistryAuth

    registry_url = spec["registry_url"]

    class LoopbackRegistryClient(RegistryClient):
        def __init__(self, registry, repository, **kwargs):
            kwargs["insecure"] = True
            super().__init__(urlsplit(registry_url).netloc, repository, **kwargs)

    builder_module.RegistryClient = LoopbackRegistryClient
    builder_module.get_registry_auth = lambda *_args, **_kwargs: RegistryAuth()
    config = BuildConfig(
        tag=f"{NETWORK_REGISTRY_HOST}/{spec['push_repository']}:latest",
        base_image=f"{NETWORK_REGISTRY_HOST}/{spec['base_repository']}:latest",
        context_dir=spec["context"],
        output_dir=spec["output"],
        cache_dir=spec["cache"],
        use_cache=True,
        include_deps=False,
        include_paths=["app"],
        reproducible=True,
    )
    timings = {}
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        started = time.perf_counter()
        builder = ImageBuilder(config)
        original_pull = builder._pull_base_image

        def timed_pull(*args):
            pull_started = time.perf_counter()
            try:
                return original_pull(*args)
            finally:
                timings["pull_seconds"] = time.perf_counter() - pull_started

        builder._pull_base_image = timed_pull
        builder.build()
        build_seconds = time.perf_counter() - started
        push_started = time.perf_counter()
        builder.push()
        push_seconds = time.perf_counter() - push_started
        repeat_started = time.perf_counter()
        builder.push()
        repeat_push_seconds = time.perf_counter() - repeat_started

    pull_seconds = timings["pull_seconds"]
    return {
        "pull_seconds": pull_seconds,
        "local_build_seconds": build_seconds - pull_seconds,
        "push_seconds": push_seconds,
        "repeat_push_seconds": repeat_push_seconds,
        "process_peak_rss_bytes": _peak_rss_bytes(),
        "base_requests": _registry_requests(registry_url, spec["base_repository"]),
        "push_requests": _registry_requests(registry_url, spec["push_repository"]),
        "image": {
            "blobs_dir": str(Path(spec["output"]) / "blobs" / "sha256"),
            "layers": [layer.digest for layer in builder.layers],
            "config": builder.config_digest,
            "manifest": builder.manifest_digest,
        },
    }


def _measure_network_probe_worker(spec):
    """Transfer the same blobs via RegistryClient with ``workers`` threads.

    This is a benchmark-only estimate of concurrency headroom, not a product
    code path. Run with one worker it calibrates against the real sequential
    path; dependency order (manifest, then config/layers, then manifest push)
    and per-blob verification are preserved.
    """
    from concurrent.futures import ThreadPoolExecutor

    registry_url = spec["registry_url"]
    workers = spec["workers"]
    destination = Path(spec["output"])
    destination.mkdir(parents=True, exist_ok=True)
    image = spec["image"]
    blobs_dir = Path(image["blobs_dir"])

    def run_all(function, items):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in pool.map(function, items):
                pass

    started = time.perf_counter()
    base = _network_client(registry_url, spec["base_repository"])
    manifest, _ = base.pull_manifest("latest")
    descriptors = [manifest["config"], *manifest["layers"]]
    run_all(
        lambda item: base.pull_blob(
            item["digest"], destination / item["digest"].split(":", 1)[1], item["size"]
        ),
        descriptors,
    )
    pull_seconds = time.perf_counter() - started

    push = _network_client(registry_url, spec["push_repository"])
    blobs = [*image["layers"], image["config"]]
    manifest_data = (blobs_dir / image["manifest"].split(":", 1)[1]).read_bytes()

    def push_image():
        push_started = time.perf_counter()
        run_all(
            lambda digest: push.push_blob(
                digest, blobs_dir / digest.split(":", 1)[1], check_exists=True
            ),
            blobs,
        )
        push.push_manifest("latest", manifest_data)
        return time.perf_counter() - push_started

    push_seconds = push_image()
    repeat_push_seconds = push_image()
    return {
        "workers": workers,
        "pull_seconds": pull_seconds,
        "push_seconds": push_seconds,
        "repeat_push_seconds": repeat_push_seconds,
        "process_peak_rss_bytes": _peak_rss_bytes(),
        "base_requests": _registry_requests(registry_url, spec["base_repository"]),
        "push_requests": _registry_requests(registry_url, spec["push_repository"]),
    }


def _measure_worker(spec):
    if spec["mode"] == "transfer":
        return _measure_transfer_worker(spec)
    if spec["mode"] == "network-build":
        return _measure_network_build_worker(spec)
    if spec["mode"] == "network-probe":
        return _measure_network_probe_worker(spec)

    context = Path(spec["context"])
    output = Path(spec["output"])
    output.mkdir(parents=True, exist_ok=True)
    layers_dir = output / "blobs" / "sha256"
    layers_dir.mkdir(parents=True, exist_ok=True)
    cache_hits = {
        "lookups": 0, "hits": 0, "dependency_lookups": 0, "dependency_hits": 0,
        "group_lookups": 0, "group_hits": 0, "locked_installs": 0,
    }
    original_get_layer = LayerCache.get_layer
    original_get_entry = LayerCache.get_entry
    original_locked_install = builder_module.install_locked_requirements

    def tracked_get_layer(cache, files, **kwargs):
        cache_hits["lookups"] += 1
        layer = original_get_layer(cache, files, **kwargs)
        cache_hits["hits"] += layer is not None
        return layer

    def tracked_get_entry(cache, key):
        entry = original_get_entry(cache, key)
        if key.startswith("deps-v"):
            cache_hits["dependency_lookups"] += 1
            cache_hits["dependency_hits"] += entry is not None
        elif key.startswith("deps-group-v"):
            cache_hits["group_lookups"] += 1
            cache_hits["group_hits"] += entry is not None
        return entry

    def tracked_locked_install(*args, **kwargs):
        cache_hits["locked_installs"] += 1
        return original_locked_install(*args, **kwargs)

    LayerCache.get_layer = tracked_get_layer
    LayerCache.get_entry = tracked_get_entry
    builder_module.install_locked_requirements = tracked_locked_install
    config = BuildConfig(
        tag="benchmark:local",
        context_dir=str(context),
        output_dir=str(output),
        cache_dir=spec.get("cache"),
        use_cache=spec["use_cache"],
        include_deps=spec.get("include_deps", False),
        include_paths=["app"],
        reproducible=True,
        dependency_layer_strategy="explicit" if spec.get("grouped") else "single",
        stable_dependencies=["stable-dep"] if spec.get("grouped") else [],
        frequent_dependencies=["frequent-dep"] if spec.get("grouped") else [],
    )

    try:
        tracemalloc.start()
        started = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            builder = ImageBuilder(config)
            if spec["mode"] == "full":
                builder._pull_base_image = lambda *_args: (
                    [],
                    {
                        "architecture": "amd64",
                        "os": "linux",
                        "config": {
                            "Env": [
                                "PYTHON_IMPLEMENTATION=CPython",
                                "PYTHON_VERSION=3.11.8",
                                "PYTHON_LIBC=glibc-2.17",
                            ],
                            "WorkingDir": "/",
                        },
                    },
                )
                builder.build()
                app_layer_bytes = builder.layers[-1].size
                dependency_layer_bytes = sum(layer.size for layer in builder.layers[:-1])
            else:
                layer = builder._create_app_layer(layers_dir, ["app"])
                app_layer_bytes = layer.size
                dependency_layer_bytes = 0

        wall_seconds = time.perf_counter() - started
        _, python_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        process_peak_rss_bytes = _peak_rss_bytes()
        if spec["mode"] == "full":
            output_bytes = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
        else:
            output_bytes = layer.size
        result = {
            "wall_seconds": wall_seconds,
            "process_peak_rss_bytes": process_peak_rss_bytes,
            "python_tracemalloc_peak_bytes": python_peak_bytes,
            "output_bytes": output_bytes,
            "app_layer_bytes": app_layer_bytes,
            "dependency_layer_bytes": dependency_layer_bytes,
            "cache_lookups": cache_hits["lookups"],
            "cache_hits": cache_hits["hits"],
            "dependency_cache_lookups": cache_hits["dependency_lookups"],
            "dependency_cache_hits": cache_hits["dependency_hits"],
            "pip_version": metadata.version("pip"),
        }
        if spec.get("grouped"):
            result.update(
                group_cache_lookups=cache_hits["group_lookups"],
                group_cache_hits=cache_hits["group_hits"],
                locked_install_calls=cache_hits["locked_installs"],
                layers=[{"digest": item.digest, "size": item.size} for item in builder.layers],
            )
            if spec.get("registry_url"):
                client = _network_client(spec["registry_url"], "benchmark")
                push_started = time.perf_counter()
                pushed = [
                    client.push_blob(item.digest, Path(item.tar_path), check_exists=True)
                    for item in builder.layers
                ]
                result.update(
                    layer_push_seconds=time.perf_counter() - push_started,
                    uploaded_bytes=sum(item.size for item, sent in zip(builder.layers, pushed) if sent),
                    reused_registry_blobs=sum(not sent for sent in pushed),
                    pushed_layers=pushed,
                )
        return result
    finally:
        tracemalloc.stop()
        LayerCache.get_layer = original_get_layer
        LayerCache.get_entry = original_get_entry
        builder_module.install_locked_requirements = original_locked_install


def _invoke_worker(spec):
    environment = {
        name: value for name, value in os.environ.items()
        if not name.upper().startswith(("PIP_", "PYTHON"))
    }
    environment["PIP_CONFIG_FILE"] = os.devnull
    environment["NETRC"] = os.devnull
    environment["NO_PROXY"] = "*"
    environment["no_proxy"] = "*"
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", json.dumps(spec)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Benchmark worker failed:\n{error.stderr}") from error
    return json.loads(result.stdout)


def _measured_case(name, description, specs):
    samples = [_invoke_worker(spec) for spec in specs]
    rss_values = [sample["process_peak_rss_bytes"] for sample in samples]
    summary = {
        "median_wall_seconds": statistics.median(sample["wall_seconds"] for sample in samples),
        "median_process_peak_rss_bytes": statistics.median(rss_values),
        "median_python_tracemalloc_peak_bytes": statistics.median(
            sample["python_tracemalloc_peak_bytes"] for sample in samples
        ),
        "median_output_bytes": statistics.median(sample["output_bytes"] for sample in samples),
        "total_cache_hits": sum(sample["cache_hits"] for sample in samples),
        "total_dependency_cache_hits": sum(
            sample["dependency_cache_hits"] for sample in samples
        ),
    }
    if "uploaded_bytes" in samples[0]:
        summary["median_uploaded_bytes"] = statistics.median(
            sample["uploaded_bytes"] for sample in samples
        )
        summary["median_downloaded_bytes"] = statistics.median(
            sample["downloaded_bytes"] for sample in samples
        )
    return {
        "name": name,
        "description": description,
        "samples": samples,
        "summary": summary,
    }


def _median(values):
    return statistics.median(values)


def _summarize_network_samples(samples, workers):
    sequential = [sample["sequential"] for sample in samples]
    calibration = [sample["probe_1_worker"] for sample in samples]
    concurrent = [sample[f"probe_{workers}_workers"] for sample in samples]

    def transfer(result, push_key="push_seconds"):
        return result["pull_seconds"] + result[push_key]

    summary = {}
    for label, results in (
        ("sequential", sequential),
        ("probe_1_worker", calibration),
        (f"probe_{workers}_workers", concurrent),
    ):
        summary[label] = {
            key: _median(result[key] for result in results)
            for key in (
                "pull_seconds",
                "push_seconds",
                "repeat_push_seconds",
                "process_peak_rss_bytes",
                "base_requests",
                "push_requests",
            )
        }
    summary["sequential"]["local_build_seconds"] = _median(
        result["local_build_seconds"] for result in sequential
    )
    totals = [
        result["pull_seconds"] + result["local_build_seconds"] + result["push_seconds"]
        for result in sequential
    ]
    # Concurrency savings come only from the matched probe pair; the product path
    # also differs from the probe in builder overhead, which is not a saving.
    savings = [transfer(one) - transfer(many) for one, many in zip(calibration, concurrent)]
    summary["sequential_build_and_push_seconds"] = _median(totals)
    summary["sequential_transfer_share"] = _median(
        transfer(seq) / total for seq, total in zip(sequential, totals)
    )
    summary["calibration_probe_to_product_transfer_ratio"] = _median(
        transfer(probe) / transfer(seq) for seq, probe in zip(sequential, calibration)
    )
    summary["projected_saving_seconds"] = _median(savings)
    summary["projected_saving_share"] = _median(
        saving / total for saving, total in zip(savings, totals)
    )
    summary["projected_saving_range_seconds"] = [min(savings), max(savings)]
    summary["repeat_push_projected_saving_seconds"] = _median(
        one["repeat_push_seconds"] - many["repeat_push_seconds"]
        for one, many in zip(calibration, concurrent)
    )
    return summary


def _network_transfer_cases(args, root, context):
    cases = []
    workers = args.probe_workers
    for bandwidth_mbit in args.bandwidth_mbit:
        for request_delay_ms in args.request_delay_ms:
            storage = root / f"network-registry-{bandwidth_mbit}-{request_delay_ms}"
            with _network_registry(
                storage, request_delay_ms, bandwidth_mbit, args.bandwidth_scope
            ) as registry_url:
                for layout_index, layout in enumerate(args.layer_layouts):
                    samples = []
                    for sample in range(args.network_repetitions):
                        prefix = f"l{layout_index}-s{sample}"
                        seed = (
                            bandwidth_mbit * 1_000_000
                            + request_delay_ms * 10_000
                            + layout_index * 100
                            + sample
                        ) * 10
                        repositories = {}
                        for variant in ("seq", "p1", "pn"):
                            repositories[variant] = f"base-{prefix}-{variant}"
                            _seed_base_image(
                                storage,
                                repositories[variant],
                                layout["layer_bytes"],
                                seed,
                            )
                        sequential = _invoke_worker(
                            {
                                "mode": "network-build",
                                "registry_url": registry_url,
                                "base_repository": repositories["seq"],
                                "push_repository": f"push-{prefix}-seq",
                                "context": str(context),
                                "output": str(root / f"network-{bandwidth_mbit}-{request_delay_ms}-{prefix}"),
                                "cache": str(root / f"network-cache-{bandwidth_mbit}-{request_delay_ms}-{prefix}"),
                            }
                        )
                        image = sequential.pop("image")
                        probes = {}
                        order = (1, workers) if sample % 2 == 0 else (workers, 1)
                        for probe_workers in order:
                            variant = "p1" if probe_workers == 1 else "pn"
                            probes[probe_workers] = _invoke_worker(
                                {
                                    "mode": "network-probe",
                                    "registry_url": registry_url,
                                    "workers": probe_workers,
                                    "base_repository": repositories[variant],
                                    "push_repository": f"push-{prefix}-{variant}",
                                    "output": str(root / f"probe-{bandwidth_mbit}-{request_delay_ms}-{prefix}-{variant}"),
                                    "image": image,
                                }
                            )
                        samples.append(
                            {
                                "sequential": sequential,
                                "probe_1_worker": probes[1],
                                f"probe_{workers}_workers": probes[workers],
                            }
                        )
                    cases.append(
                        {
                            "name": (
                                f"registry-network-{layout['name']}-"
                                f"{request_delay_ms}ms-"
                                f"{bandwidth_mbit or 'unlimited'}"
                                f"{'Mbit' if bandwidth_mbit else ''}"
                            ),
                            "description": (
                                "Real sequential base pull, local build and push through a "
                                "separate-process loopback registry with injected per-request "
                                "delay and bandwidth, plus benchmark-only RegistryClient probes "
                                f"with 1 and {workers} workers."
                            ),
                            "network": {
                                "request_delay_ms": request_delay_ms,
                                "bandwidth_mbit": bandwidth_mbit or None,
                                "bandwidth_scope": args.bandwidth_scope,
                                "base_layer_bytes": layout["layer_bytes"],
                            },
                            "samples": samples,
                            "summary": _summarize_network_samples(samples, workers),
                        }
                    )
    return cases


def _git_revision():
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _create_fixture(context, files, file_bytes):
    app = context / "app"
    app.mkdir(parents=True)
    payload = b"x" * file_bytes
    for index in range(files):
        (app / f"module_{index:05}.py").write_bytes(payload)
    (context / "wheelhouse").mkdir()
    _write_benchmark_wheel(context / "wheelhouse", "1.0")
    (context / "requirements.txt").write_text(
        "--no-index\n--find-links ./wheelhouse\nbenchmark-dependency==1.0\n",
        encoding="utf-8",
    )
    return payload


def _write_benchmark_wheel(wheelhouse, version):
    files = {
        f"benchmark_dependency-{version}.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: benchmark-dependency\nVersion: {version}\n\n"
        ).encode(),
        f"benchmark_dependency-{version}.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode(),
        "benchmark_dependency/__init__.py": f"version = {version!r}\n".encode(),
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode()
        writer.writerow((name, f"sha256={digest.rstrip('=')}", len(content)))
    record_name = f"benchmark_dependency-{version}.dist-info/RECORD"
    writer.writerow((record_name, "", ""))
    files[record_name] = record.getvalue().encode()
    wheel = wheelhouse / f"benchmark_dependency-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _spec(context, output, cache, *, mode="full", use_cache=True, include_deps=False):
    return {
        "context": str(context),
        "output": str(output),
        "cache": str(cache) if cache else None,
        "mode": mode,
        "use_cache": use_cache,
        "include_deps": include_deps,
    }


def _write_group_wheel(wheelhouse, name, version, payload_bytes, requires=()):
    module = name.replace("-", "_")
    dist = f"{module}-{version}.dist-info"
    payload = (f"{name}-{version}:".encode() * (payload_bytes + 1))[:payload_bytes]
    contents = {
        f"{module}/__init__.py": f"version = {version!r}\n".encode(),
        f"{module}/payload.bin": payload,
        f"{dist}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {item}\n" for item in requires)
            + "\n"
        ).encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for path, content in contents.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow((path, f"sha256={digest}", len(content)))
    writer.writerow((f"{dist}/RECORD", "", ""))
    contents[f"{dist}/RECORD"] = record.getvalue().encode()
    path = wheelhouse / f"{module}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for item, content in contents.items():
            archive.writestr(zipfile.ZipInfo(item, (2020, 1, 1, 0, 0, 0)), content)
    return path


def _start_benchmark_index(wheelhouse):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.server.requests += 1
            parts = [part for part in urlsplit(self.path).path.split("/") if part]
            body = None
            content_type = "text/html"
            wheels = sorted(wheelhouse.glob("*.whl"))
            if len(parts) == 2 and parts[0] == "simple":
                links = [
                    f'<a href="/files/{wheel.name}#sha256={hashlib.sha256(wheel.read_bytes()).hexdigest()}">'
                    f"{wheel.name}</a>"
                    for wheel in wheels
                    if wheel.name.split("-", 1)[0].replace("_", "-") == parts[1]
                ]
                if links:
                    body = ("<html><body>" + "".join(links) + "</body></html>").encode()
            elif len(parts) == 2 and parts[0] == "files":
                wheel = next((item for item in wheels if item.name == parts[1]), None)
                if wheel:
                    body = wheel.read_bytes()
                    content_type = "application/octet-stream"
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}/simple"


def _write_group_project(context, index_url, wheels, frequent_version):
    (context / "pyproject.toml").write_text(
        '[project]\nname = "benchmark-app"\nversion = "1.0"\nrequires-python = ">=3.10"\n'
        'dependencies = ["stable-dep==1.0", "frequent-dep>=1.0"]\n'
        f"\n[[tool.uv.index]]\nurl = {json.dumps(index_url)}\n",
        encoding="utf-8",
    )
    chunks = [
        'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n',
        '[[package]]\nname = "benchmark-app"\nversion = "1.0"\nsource = { virtual = "." }\n'
        'dependencies = [{ name = "stable-dep" }, { name = "frequent-dep" }]\n'
        '[package.metadata]\nrequires-dist = ['
        '{ name = "stable-dep", specifier = "==1.0" }, '
        '{ name = "frequent-dep", specifier = ">=1.0" }]\n',
    ]
    for name in ("stable-dep", "frequent-dep", "shared-dep"):
        wheel = wheels[name]
        package_version = frequent_version if name == "frequent-dep" else "1.0"
        dependencies = (
            'dependencies = [{ name = "shared-dep" }]\n' if name != "shared-dep" else ""
        )
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        chunks.append(
            f'[[package]]\nname = "{name}"\nversion = "{package_version}"\n'
            f"source = {{ registry = {json.dumps(index_url)} }}\n"
            f"{dependencies}"
            f'wheels = [{{ url = "https://example.invalid/{wheel.name}", '
            f'hash = "sha256:{digest}" }}]\n'
        )
    (context / "uv.lock").write_text("\n".join(chunks), encoding="utf-8")


def _group_case(name, description, root, context, args, index, baseline=None):
    samples = []
    for number in range(args.repetitions):
        cache = root / f"{name}-cache-{number}"
        if baseline:
            shutil.copytree(baseline["cache"], cache)
        server, thread, registry_url = _start_benchmark_registry(
            root / f"{name}-registry-{number}"
        )
        try:
            if baseline:
                for layer in baseline["layers"]:
                    filename = layer["digest"].split(":", 1)[1]
                    shutil.copyfile(
                        baseline["output"] / "blobs" / "sha256" / filename,
                        server.blob_dir / filename,
                    )
            spec = _spec(
                context, root / f"{name}-output-{number}", cache, include_deps=True
            )
            spec.update(grouped=True, registry_url=registry_url)
            requests_before = index.requests
            sample = _invoke_worker(spec)
            sample["package_index_requests"] = index.requests - requests_before
            sample["registry_head_requests"] = server.stats["heads"]
            sample["registry_put_requests"] = server.stats["puts"]
            if sample["uploaded_bytes"] != server.stats["uploaded_bytes"]:
                raise RuntimeError("Measured layer uploads do not match registry-received bytes.")
            samples.append(sample)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    return {
        "name": name,
        "description": description,
        "samples": samples,
        "summary": {
            "median_wall_seconds": statistics.median(sample["wall_seconds"] for sample in samples),
            "median_layer_push_seconds": statistics.median(sample["layer_push_seconds"] for sample in samples),
            "median_uploaded_bytes": statistics.median(sample["uploaded_bytes"] for sample in samples),
            "median_process_peak_rss_bytes": statistics.median(sample["process_peak_rss_bytes"] for sample in samples),
            "median_python_tracemalloc_peak_bytes": statistics.median(sample["python_tracemalloc_peak_bytes"] for sample in samples),
            "total_group_cache_hits": sum(sample["group_cache_hits"] for sample in samples),
            "total_registry_reused_blobs": sum(sample["reused_registry_blobs"] for sample in samples),
        },
    }


def run_grouped_benchmarks(args):
    with tempfile.TemporaryDirectory(prefix="pycontainer-group-bench-") as temp_dir:
        root = Path(temp_dir)
        context = root / "context"
        app = context / "app"
        app.mkdir(parents=True)
        payload = b"x" * args.file_bytes
        for number in range(args.files):
            (app / f"module_{number:05}.py").write_bytes(payload)
        wheelhouse = context / "wheelhouse"
        wheelhouse.mkdir()
        stable = _write_group_wheel(wheelhouse, "stable-dep", "1.0", 128 * 1024, ("shared-dep>=1.0",))
        frequent1 = _write_group_wheel(wheelhouse, "frequent-dep", "1.0", 16 * 1024, ("shared-dep>=1.0",))
        frequent2 = _write_group_wheel(wheelhouse, "frequent-dep", "2.0", 16 * 1024, ("shared-dep>=1.0",))
        shared = _write_group_wheel(wheelhouse, "shared-dep", "1.0", 32 * 1024)
        wheels = {"stable-dep": stable, "frequent-dep": frequent1, "shared-dep": shared}
        index, index_thread, index_url = _start_benchmark_index(wheelhouse)
        try:
            _write_group_project(context, index_url, wheels, "1.0")
            warm_cache = root / "baseline-cache"
            warm_output = root / "baseline-output"
            spec = _spec(context, warm_output, warm_cache, include_deps=True)
            spec["grouped"] = True
            warmup = _invoke_worker(spec)
            baseline = {"cache": warm_cache, "output": warm_output, "layers": warmup["layers"]}
            cases = [
                _group_case(
                    "grouped-cold",
                    "Empty local cache and empty loopback registry for every sample.",
                    root, context, args, index,
                ),
                _group_case(
                    "grouped-warm",
                    "Unchanged inputs with copies of the baseline cache and registry blobs.",
                    root, context, args, index, baseline,
                ),
            ]
            (app / "module_00000.py").write_bytes(payload + b"\n# changed app\n")
            cases.append(
                _group_case(
                    "grouped-app-change",
                    "One app file changes; each sample starts from the original warm cache and blobs.",
                    root, context, args, index, baseline,
                )
            )
            (app / "module_00000.py").write_bytes(payload)
            wheels["frequent-dep"] = frequent2
            _write_group_project(context, index_url, wheels, "2.0")
            cases.append(
                _group_case(
                    "grouped-dependency-change",
                    "The locked frequent distribution changes from 1.0 to 2.0 with unchanged manifest roots; each sample starts from the original warm cache and blobs.",
                    root, context, args, index, baseline,
                )
            )
        finally:
            index.shutdown()
            index.server_close()
            index_thread.join()
        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_revision": _git_revision(),
            "environment": {
                "python": sys.version,
                "pip": metadata.version("pip"),
                "implementation": platform.python_implementation(),
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "cpu_count": os.cpu_count(),
            },
            "fixture": {
                "app_files": args.files,
                "bytes_per_file": args.file_bytes,
                "repetitions": args.repetitions,
                "stable_payload_bytes": 128 * 1024,
                "frequent_payload_bytes": 16 * 1024,
                "shared_payload_bytes": 32 * 1024,
                "dependency_versions": "stable-dep 1.0, shared-dep 1.0, frequent-dep 1.0 -> 2.0",
                "base_image": "synthetic empty Linux/amd64 OCI base with CPython 3.11.8 metadata",
                "excluded": ["external registry pull/push", "config and manifest uploads", "container runtime startup"],
                "os_page_cache": "not flushed or controlled",
            },
            "measurement_notes": {
                "wall_seconds": "Builder construction and full local build; fixture setup is excluded.",
                "layer_push_seconds": "Layer-only HEAD/PUT traffic to a loopback synthetic registry; config and manifest are excluded.",
                "uploaded_bytes": "Actual verified blob bytes received by the loopback registry; protocol overhead is excluded.",
                "process_peak_rss_bytes": "Worker process high-water mark at the end of the build, before registry push, including Python startup/imports; pip child processes are excluded.",
                "python_tracemalloc_peak_bytes": "Python-tracked peak during builder construction and build, excluding the push.",
                "group_cache_hits": "Verified per-group cache lookups; a complete bundle hit performs one lookup per group.",
                "locked_install_calls": "Calls to the locked installer; this fixture uses one index and one pip subprocess per call.",
                "timing_gates": "None; synthetic local observations do not establish external registry throughput or real runtime performance.",
            },
            "cases": cases,
        }


def run_benchmarks(args):
    with tempfile.TemporaryDirectory(prefix="pycontainer-bench-") as temp_dir:
        root = Path(temp_dir)
        context = root / "context"
        original_app = _create_fixture(context, args.files, args.file_bytes)
        cases = []

        cases.append(
            _measured_case(
                "app-only",
                "Uncached app-layer creation, including builder construction and context discovery.",
                [
                    _spec(
                        context,
                        root / f"app-only-{sample}",
                        None,
                        mode="app-only",
                        use_cache=False,
                    )
                    for sample in range(args.repetitions)
                ],
            )
        )

        cases.append(
            _measured_case(
                "local-full-build-cold",
                "Full local OCI build with an empty synthetic base and a fresh cache/output per sample.",
                [
                    _spec(
                        context,
                        root / f"cold-output-{sample}",
                        root / f"cold-cache-{sample}",
                    )
                    for sample in range(args.repetitions)
                ],
            )
        )

        warm_cache = root / "warm-cache"
        _invoke_worker(_spec(context, root / "warmup-output", warm_cache))
        cases.append(
            _measured_case(
                "local-full-build-warm",
                "Full local build with unchanged inputs, a populated cache, and a fresh output per sample.",
                [
                    _spec(context, root / f"warm-output-{sample}", warm_cache)
                    for sample in range(args.repetitions)
                ],
            )
        )

        no_op_output = root / "no-op-output"
        _invoke_worker(_spec(context, no_op_output, warm_cache))
        cases.append(
            _measured_case(
                "local-full-build-no-op",
                "Repeat the unchanged cached build into the same output directory.",
                [_spec(context, no_op_output, warm_cache) for _ in range(args.repetitions)],
            )
        )

        changed_app = original_app + b"\n# changed input\n"
        (context / "app" / "module_00000.py").write_bytes(changed_app)
        app_change_specs = []
        for sample in range(args.repetitions):
            cache_copy = root / f"app-change-cache-{sample}"
            shutil.copytree(warm_cache, cache_copy)
            app_change_specs.append(
                _spec(context, root / f"app-change-output-{sample}", cache_copy)
            )
        cases.append(
            _measured_case(
                "local-full-build-app-change",
                "Full local build after changing one app file, starting from a copy of the baseline cache.",
                app_change_specs,
            )
        )

        (context / "app" / "module_00000.py").write_bytes(original_app)
        dependency_cache = root / "dependency-baseline-cache"
        _invoke_worker(
            _spec(context, root / "dependency-baseline-output", dependency_cache)
        )
        (context / "requirements.txt").write_text(
            "--no-index\n--find-links ./wheelhouse\nbenchmark-dependency==2.0\n",
            encoding="utf-8",
        )
        _write_benchmark_wheel(context / "wheelhouse", "2.0")
        dependency_change_specs = []
        for sample in range(args.repetitions):
            cache_copy = root / f"dependency-change-cache-{sample}"
            shutil.copytree(dependency_cache, cache_copy)
            dependency_change_specs.append(
                _spec(
                    context,
                    root / f"dependency-change-output-{sample}",
                    cache_copy,
                    include_deps=True,
                )
            )
        cases.append(
            _measured_case(
                "local-full-build-dependency-change",
                "Full local build after changing requirements.txt, with dependency layering enabled.",
                dependency_change_specs,
            )
        )

        pinned_context = root / "pinned-context"
        _create_fixture(pinned_context, args.files, args.file_bytes)
        wheel = pinned_context / "wheelhouse" / "benchmark_dependency-1.0-py3-none-any.whl"
        (pinned_context / "requirements.txt").write_text(
            "--no-index\n--find-links ./wheelhouse\nbenchmark-dependency==1.0 "
            f"--hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n",
            encoding="utf-8",
        )
        cases.append(
            _measured_case(
                "dependency-layer-cold",
                "Full local build of a hash-pinned requirements fixture with an empty cache per sample; pip stages the dependency layer.",
                [
                    _spec(
                        pinned_context,
                        root / f"pinned-cold-output-{sample}",
                        root / f"pinned-cold-cache-{sample}",
                        include_deps=True,
                    )
                    for sample in range(args.repetitions)
                ],
            )
        )
        pinned_cache = root / "pinned-warm-cache"
        _invoke_worker(
            _spec(pinned_context, root / "pinned-warmup-output", pinned_cache, include_deps=True)
        )
        cases.append(
            _measured_case(
                "dependency-layer-hit",
                "Same hash-pinned fixture with a populated cache and fresh output per sample; the dependency layer is reused without running pip install.",
                [
                    _spec(
                        pinned_context,
                        root / f"pinned-hit-output-{sample}",
                        pinned_cache,
                        include_deps=True,
                    )
                    for sample in range(args.repetitions)
                ],
            )
        )

        server, server_thread, registry_url = _start_benchmark_registry(
            root / "synthetic-registry"
        )
        try:
            for size_mib in args.transfer_sizes_mib:
                size_bytes = size_mib * 1024 * 1024
                transfer_specs = []
                for sample in range(args.repetitions):
                    source = root / f"transfer-{size_mib}-{sample}.source"
                    destination = root / f"transfer-{size_mib}-{sample}.download"
                    digest = _write_transfer_fixture(
                        source, size_bytes, size_mib * 1000 + sample
                    )
                    transfer_specs.append(
                        {
                            "mode": "transfer",
                            "registry_url": registry_url,
                            "source": str(source),
                            "destination": str(destination),
                            "digest": digest,
                            "size_bytes": size_bytes,
                        }
                    )
                cases.append(
                    _measured_case(
                        f"registry-blob-transfer-{size_mib}MiB",
                        "One streamed upload and verified download against a local synthetic registry; excludes external registry bandwidth.",
                        transfer_specs,
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        if args.layer_layouts:
            (context / "app" / "module_00000.py").write_bytes(original_app)
            cases.extend(_network_transfer_cases(args, root, context))

        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_revision": _git_revision(),
            "environment": {
                "python": sys.version,
                "implementation": platform.python_implementation(),
                "pip": next(
                    case["samples"][0]["pip_version"]
                    for case in cases if case["name"] == "dependency-layer-cold"
                ),
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "cpu_count": os.cpu_count(),
            },
            "fixture": {
                "app_files": args.files,
                "bytes_per_file": args.file_bytes,
                "repetitions": args.repetitions,
                "registry_transfer_sizes_mib": args.transfer_sizes_mib,
                "network": {
                    "layer_layouts": [layout["name"] for layout in args.layer_layouts],
                    "request_delay_ms": args.request_delay_ms,
                    "bandwidth_mbit": args.bandwidth_mbit,
                    "bandwidth_scope": args.bandwidth_scope,
                    "repetitions": args.network_repetitions,
                    "probe_workers": args.probe_workers,
                    "cache_state": "Fresh output and empty app-layer cache per sample; every sample seeds byte-identical base images into separate per-variant repositories (sequential, 1-worker and N-worker probes), so each variant pulls the same blobs cold and pushes to a fresh repository, then repeats the push (all blobs present).",
                },
                "base_image": "synthetic empty OCI base",
                "excluded": [
                    "external registry pull/push",
                    "container runtime startup",
                ],
            },
            "measurement_notes": {
                "wall_seconds": "Builder construction and measured layer/build work; fixture setup and output-size scans are excluded.",
                "process_peak_rss_bytes": "Per-sample process high-water mark, including Python startup/imports.",
                "python_tracemalloc_peak_bytes": "Python-tracked peak allocations during builder construction and measured work.",
                "output_bytes": "App-layer tar bytes for app-only; total local OCI output file bytes for full builds.",
                "cache_hits": "App-layer cache hits observed during the measured build.",
                "dependency_cache_hits": "Verified dependency-layer cache entries found during the measured build; worker PIP_* and PYTHON* settings are cleared, pip config and netrc disabled and proxies bypassed. Plain requirements still read pip configuration once per build.",
                "registry_transfer": "Per-sample loopback synthetic registry upload/download; server storage and fixture generation are excluded from the measured worker process. These are not external registry throughput results.",
                "registry_network": "The registry runs in a separate process and sleeps request_delay_ms before every response, approximating one round trip per HTTP request. The client opens a new connection per request; real HTTPS adds TCP and TLS handshakes per request, so a delay of about three RTTs approximates one HTTPS request. Bandwidth is paced in 64 KiB chunks either across all connections (shared, one bottleneck link) or per connection (per-connection, models per-flow TCP limits). Probe workers transfer the same blobs through RegistryClient with a thread pool and are an estimate of concurrency headroom, not a product path; probe_1_worker calibrates the probe against the real sequential path. projected_saving_* are paired per-sample medians of the 1-worker minus N-worker probe transfer time; projected_saving_share divides that by the sequential product pull + local build + push total. No tracemalloc runs during network cases.",
                "timing_gates": "None; compare trends only across matching environments and fixture parameters.",
            },
            "cases": cases,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grouped-only",
        action="store_true",
        help="Measure grouped uv.lock builds and loopback layer uploads instead of the default cases",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--file-bytes", type=int, default=1024)
    parser.add_argument("--transfer-sizes-mib", type=int, nargs="+", default=[8, 32])
    parser.add_argument(
        "--layer-layouts",
        type=_parse_layout,
        nargs="*",
        default=[_parse_layout("4x2")],
        help="Synthetic base image layouts as COUNTxMIB or MIB+MIB+...; pass none to skip network cases.",
    )
    parser.add_argument("--request-delay-ms", type=int, nargs="+", default=[0, 50])
    parser.add_argument(
        "--bandwidth-mbit", type=int, nargs="+", default=[0], help="0 means unlimited."
    )
    parser.add_argument(
        "--bandwidth-scope", choices=("shared", "per-connection"), default="shared"
    )
    parser.add_argument("--network-repetitions", type=int, default=3)
    parser.add_argument("--probe-workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results.json"))
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--serve-registry", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.serve_registry:
        _serve_network_registry(json.loads(args.serve_registry))
        return
    if args.worker:
        print(json.dumps(_measure_worker(json.loads(args.worker))))
        return
    if (
        args.repetitions < 1
        or args.files < 1
        or args.file_bytes < 1
        or any(size < 1 for size in args.transfer_sizes_mib)
    ):
        parser.error(
            "--repetitions, --files, --file-bytes, and transfer sizes must be positive"
        )
    if (
        args.network_repetitions < 1
        or args.probe_workers < 2
        or any(delay < 0 for delay in args.request_delay_ms)
        or any(rate < 0 for rate in args.bandwidth_mbit)
    ):
        parser.error(
            "--network-repetitions must be positive, --probe-workers at least 2, "
            "and delays and bandwidths non-negative"
        )

    results = run_grouped_benchmarks(args) if args.grouped_only else run_benchmarks(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(results['cases'])} benchmark cases to {args.output}")


if __name__ == "__main__":
    main()
