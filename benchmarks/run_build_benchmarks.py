"""Record repeatable local build measurements without registry or runtime access."""
import argparse
import base64
import contextlib
import csv
import ctypes
import hashlib
import io
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pycontainer.builder import ImageBuilder
from pycontainer.cache import LayerCache
from pycontainer.config import BuildConfig


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


def _measure_worker(spec):
    context = Path(spec["context"])
    output = Path(spec["output"])
    output.mkdir(parents=True, exist_ok=True)
    layers_dir = output / "blobs" / "sha256"
    layers_dir.mkdir(parents=True, exist_ok=True)
    cache_hits = {"lookups": 0, "hits": 0}
    original_get_layer = LayerCache.get_layer

    def tracked_get_layer(cache, files, **kwargs):
        cache_hits["lookups"] += 1
        layer = original_get_layer(cache, files, **kwargs)
        cache_hits["hits"] += layer is not None
        return layer

    LayerCache.get_layer = tracked_get_layer
    config = BuildConfig(
        tag="benchmark:local",
        context_dir=str(context),
        output_dir=str(output),
        cache_dir=spec.get("cache"),
        use_cache=spec["use_cache"],
        include_deps=spec.get("include_deps", False),
        include_paths=["app"],
        reproducible=True,
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
                        "config": {"Env": [], "WorkingDir": "/"},
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
        return {
            "wall_seconds": wall_seconds,
            "process_peak_rss_bytes": process_peak_rss_bytes,
            "python_tracemalloc_peak_bytes": python_peak_bytes,
            "output_bytes": output_bytes,
            "app_layer_bytes": app_layer_bytes,
            "dependency_layer_bytes": dependency_layer_bytes,
            "cache_lookups": cache_hits["lookups"],
            "cache_hits": cache_hits["hits"],
        }
    finally:
        tracemalloc.stop()
        LayerCache.get_layer = original_get_layer


def _invoke_worker(spec):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", json.dumps(spec)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _measured_case(name, description, specs):
    samples = [_invoke_worker(spec) for spec in specs]
    rss_values = [sample["process_peak_rss_bytes"] for sample in samples]
    return {
        "name": name,
        "description": description,
        "samples": samples,
        "summary": {
            "median_wall_seconds": statistics.median(sample["wall_seconds"] for sample in samples),
            "median_process_peak_rss_bytes": statistics.median(rss_values),
            "median_python_tracemalloc_peak_bytes": statistics.median(
                sample["python_tracemalloc_peak_bytes"] for sample in samples
            ),
            "median_output_bytes": statistics.median(sample["output_bytes"] for sample in samples),
            "total_cache_hits": sum(sample["cache_hits"] for sample in samples),
        },
    }


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

        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_revision": _git_revision(),
            "environment": {
                "python": sys.version,
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
                "base_image": "synthetic empty OCI base",
                "excluded": ["registry pull/push", "container runtime startup"],
            },
            "measurement_notes": {
                "wall_seconds": "Builder construction and measured layer/build work; fixture setup and output-size scans are excluded.",
                "process_peak_rss_bytes": "Per-sample process high-water mark, including Python startup/imports.",
                "python_tracemalloc_peak_bytes": "Python-tracked peak allocations during builder construction and measured work.",
                "output_bytes": "App-layer tar bytes for app-only; total local OCI output file bytes for full builds.",
                "cache_hits": "App-layer cache hits observed during the measured build.",
                "timing_gates": "None; compare trends only across matching environments and fixture parameters.",
            },
            "cases": cases,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--file-bytes", type=int, default=1024)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results.json"))
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        print(json.dumps(_measure_worker(json.loads(args.worker))))
        return
    if args.repetitions < 1 or args.files < 1 or args.file_bytes < 1:
        parser.error("--repetitions, --files, and --file-bytes must be positive")

    results = run_benchmarks(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(results['cases'])} benchmark cases to {args.output}")


if __name__ == "__main__":
    main()
