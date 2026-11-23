import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_benchmark_harness_records_isolated_build_and_transfer_cases(tmp_path, monkeypatch):
    pip_config = tmp_path / "pip.conf"
    pip_config.write_text("[install]\nno-deps = true\n", encoding="utf-8")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(pip_config))
    monkeypatch.setenv("PIP_NO_DEPS", "1")
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
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
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    results = json.loads(output.read_text(encoding="utf-8"))
    assert results["environment"]["pip"] == metadata.version("pip")
    cases = {case["name"]: case for case in results["cases"]}
    assert set(cases) == {
        "app-only",
        "local-full-build-cold",
        "local-full-build-warm",
        "local-full-build-no-op",
        "local-full-build-app-change",
        "local-full-build-dependency-change",
        "dependency-layer-cold",
        "dependency-layer-hit",
        "registry-blob-transfer-1MiB",
        "registry-blob-transfer-2MiB",
    }
    assert cases["local-full-build-cold"]["samples"][0]["cache_hits"] == 0
    assert cases["local-full-build-warm"]["samples"][0]["cache_hits"] == 1
    assert cases["local-full-build-no-op"]["samples"][0]["cache_hits"] == 1
    assert cases["local-full-build-app-change"]["samples"][0]["cache_hits"] == 0
    assert cases["local-full-build-dependency-change"]["samples"][0]["cache_hits"] == 1
    assert cases["local-full-build-dependency-change"]["samples"][0]["dependency_cache_hits"] == 0
    assert cases["dependency-layer-cold"]["samples"][0]["dependency_cache_hits"] == 0
    assert cases["dependency-layer-cold"]["samples"][0]["dependency_cache_lookups"] == 1
    assert cases["dependency-layer-hit"]["samples"][0]["dependency_cache_hits"] == 1
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


def test_grouped_benchmark_measures_cache_reuse_and_layer_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("PIP_NO_DEPS", "1")
    output = tmp_path / "grouped-benchmark.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmarks" / "run_build_benchmarks.py"),
            "--grouped-only",
            "--repetitions",
            "1",
            "--files",
            "2",
            "--file-bytes",
            "64",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    results = json.loads(output.read_text(encoding="utf-8"))
    cases = {case["name"]: case["samples"][0] for case in results["cases"]}
    assert set(cases) == {
        "grouped-cold",
        "grouped-warm",
        "grouped-app-change",
        "grouped-dependency-change",
    }
    cold = cases["grouped-cold"]
    warm = cases["grouped-warm"]
    app_change = cases["grouped-app-change"]
    dependency_change = cases["grouped-dependency-change"]
    assert len(cold["layers"]) == 3
    assert cold["registry_head_requests"] == 3
    assert cold["registry_put_requests"] == 3
    assert cold["uploaded_bytes"] == sum(layer["size"] for layer in cold["layers"])
    assert cold["locked_install_calls"] == 1
    assert cold["package_index_requests"] > 0

    assert warm["layers"] == cold["layers"]
    assert warm["group_cache_hits"] == 2
    assert warm["locked_install_calls"] == 0
    assert warm["package_index_requests"] == 0
    assert warm["reused_registry_blobs"] == 3
    assert warm["registry_put_requests"] == 0
    assert warm["uploaded_bytes"] == 0

    assert app_change["layers"][:2] == cold["layers"][:2]
    assert app_change["layers"][2] != cold["layers"][2]
    assert app_change["locked_install_calls"] == 0
    assert app_change["package_index_requests"] == 0
    assert app_change["registry_put_requests"] == 1
    assert app_change["uploaded_bytes"] == app_change["layers"][2]["size"]

    assert dependency_change["layers"][0] == cold["layers"][0]
    assert dependency_change["layers"][1] != cold["layers"][1]
    assert dependency_change["layers"][2] == cold["layers"][2]
    assert dependency_change["group_cache_hits"] == 1
    assert dependency_change["locked_install_calls"] == 1
    assert dependency_change["package_index_requests"] > 0
    assert dependency_change["registry_put_requests"] == 1
    assert dependency_change["uploaded_bytes"] == dependency_change["layers"][1]["size"]

    for sample in cases.values():
        assert sample["process_peak_rss_bytes"] > 0
        assert sample["python_tracemalloc_peak_bytes"] > 0
