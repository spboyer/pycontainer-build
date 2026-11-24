import argparse
import contextlib
import importlib.util
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_benchmarks():
    spec = importlib.util.spec_from_file_location(
        "run_build_benchmarks", ROOT / "benchmarks" / "run_build_benchmarks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("many_timings", "saving", "repeat_saving"),
    [
        # Equal probes: the product-vs-probe 5s gap is overhead, not a saving.
        ((3.0, 4.0, 1.0), 0.0, 0.0),
        # N workers save 2s of transfer and 0.5s of repeat push.
        ((2.0, 3.0, 0.5), 2.0, 0.5),
    ],
)
def test_network_savings_come_from_matched_probes_with_identical_bases(
    tmp_path, monkeypatch, many_timings, saving, repeat_saving
):
    benchmarks = _load_benchmarks()
    seeds = []

    @contextlib.contextmanager
    def fake_registry(*_args):
        yield "http://127.0.0.1:1"

    def fake_seed(_storage, repository, _layer_bytes, seed):
        seeds.append((repository, seed))

    def fake_worker(spec):
        timings = {
            "process_peak_rss_bytes": 1,
            "base_requests": 2,
            "push_requests": 3,
        }
        if spec["mode"] == "network-build":
            # Product pull + push is 12s against a 1-worker probe of 7s.
            return {
                **timings,
                "pull_seconds": 2.0,
                "local_build_seconds": 3.0,
                "push_seconds": 10.0,
                "repeat_push_seconds": 4.0,
                "image": {},
            }
        pull, push, repeat = (3.0, 4.0, 1.0) if spec["workers"] == 1 else many_timings
        return {
            **timings,
            "workers": spec["workers"],
            "pull_seconds": pull,
            "push_seconds": push,
            "repeat_push_seconds": repeat,
        }

    monkeypatch.setattr(benchmarks, "_network_registry", fake_registry)
    monkeypatch.setattr(benchmarks, "_seed_base_image", fake_seed)
    monkeypatch.setattr(benchmarks, "_invoke_worker", fake_worker)
    args = argparse.Namespace(
        bandwidth_mbit=[0],
        request_delay_ms=[0],
        bandwidth_scope="shared",
        layer_layouts=[{"name": "1x1", "layer_bytes": [1]}],
        network_repetitions=2,
        probe_workers=2,
    )

    [case] = benchmarks._network_transfer_cases(args, tmp_path, tmp_path)

    summary = case["summary"]
    assert summary["projected_saving_seconds"] == pytest.approx(saving)
    # Share is relative to the 15s product pull + local build + push.
    assert summary["projected_saving_share"] == pytest.approx(saving / 15.0)
    assert summary["projected_saving_range_seconds"] == pytest.approx([saving, saving])
    assert summary["repeat_push_projected_saving_seconds"] == pytest.approx(repeat_saving)
    for sample in ("s0", "s1"):
        sample_seeds = {seed for repository, seed in seeds if f"-{sample}-" in repository}
        assert len(sample_seeds) == 1
    assert len({seed for _, seed in seeds}) == 2
    assert len({repository for repository, _ in seeds}) == 6


def test_benchmark_harness_records_isolated_build_and_transfer_cases(tmp_path, monkeypatch):
    pip_config = tmp_path / "pip.conf"
    pip_config.write_text("[install]\nno-deps = true\n", encoding="utf-8")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(pip_config))
    monkeypatch.setenv("PIP_NO_DEPS", "1")
    monkeypatch.setenv("NETRC", str(tmp_path / "netrc"))
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "python-user-base"))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
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
