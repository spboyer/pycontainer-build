import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_benchmark_harness_records_isolated_build_and_transfer_cases(tmp_path):
    output = tmp_path / "benchmark.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmarks" / "run_build_benchmarks.py"),
            "--repetitions",
            "1",
            "--files",
            "2",
            "--file-bytes",
            "64",
            "--transfer-sizes-mib",
            "1",
            "2",
            "--layer-layouts",
            "2x0.25",
            "--request-delay-ms",
            "5",
            "--bandwidth-mbit",
            "1000",
            "--network-repetitions",
            "1",
            "--probe-workers",
            "2",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    results = json.loads(output.read_text(encoding="utf-8"))
    cases = {case["name"]: case for case in results["cases"]}
    assert set(cases) == {
        "app-only",
        "local-full-build-cold",
        "local-full-build-warm",
        "local-full-build-no-op",
        "local-full-build-app-change",
        "local-full-build-dependency-change",
        "registry-blob-transfer-1MiB",
        "registry-blob-transfer-2MiB",
        "registry-network-2x0.25-5ms-1000Mbit",
    }
    network = cases.pop("registry-network-2x0.25-5ms-1000Mbit")
    assert network["network"] == {
        "request_delay_ms": 5,
        "bandwidth_mbit": 1000,
        "bandwidth_scope": "shared",
        "base_layer_bytes": [256 * 1024, 256 * 1024],
    }
    sample = network["samples"][0]
    for variant in ("sequential", "probe_1_worker", "probe_2_workers"):
        result = sample[variant]
        # manifest + config + two layers
        assert result["base_requests"] == 4
        # first push: HEAD/POST/PUT per blob (2 base, app, config) + manifest;
        # repeat push: HEAD per blob + manifest
        assert result["push_requests"] == 18
        assert result["pull_seconds"] > 0
        assert result["push_seconds"] > 0
        assert result["process_peak_rss_bytes"] > 0
    summary = network["summary"]
    assert summary["sequential"]["local_build_seconds"] > 0
    assert 0 < summary["sequential_transfer_share"] < 1
    assert "projected_saving_share" in summary
    assert cases["local-full-build-cold"]["samples"][0]["cache_hits"] == 0
    assert cases["local-full-build-warm"]["samples"][0]["cache_hits"] == 1
    assert cases["local-full-build-no-op"]["samples"][0]["cache_hits"] == 1
    assert cases["local-full-build-app-change"]["samples"][0]["cache_hits"] == 0
    assert cases["local-full-build-dependency-change"]["samples"][0]["cache_hits"] == 1
    for size_mib in (1, 2):
        sample = cases[f"registry-blob-transfer-{size_mib}MiB"]["samples"][0]
        assert sample["uploaded_bytes"] == size_mib * 1024 * 1024
        assert sample["downloaded_bytes"] == size_mib * 1024 * 1024
        assert sample["cache_hits"] == 0
    for case in cases.values():
        sample = case["samples"][0]
        assert sample["wall_seconds"] >= 0
        assert sample["output_bytes"] > 0
        assert sample["process_peak_rss_bytes"] > 0
        assert sample["python_tracemalloc_peak_bytes"] > 0
