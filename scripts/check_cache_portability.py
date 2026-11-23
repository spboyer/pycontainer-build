"""Measure --cache-dir save/restore portability with a synthetic empty base.

``populate`` builds a context into a fresh cache and writes a JSON report.
``verify`` rebuilds a (relocated) context against a restored cache and
compares the result with a source report. No registry, network or container
runtime is used; the base image is an empty synthetic configuration.

Same-OS restores must hit and reproduce the image; a mismatch exits non-zero.
Cross-OS outcomes are recorded with the per-file differences that explain them.
"""
import argparse
import hashlib
import json
import os
import platform
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pycontainer.builder import ImageBuilder  # noqa: E402
from pycontainer.config import BuildConfig  # noqa: E402
from pycontainer.fs_utils import iter_files  # noqa: E402

_SYNTHETIC_BASE_CONFIG = {
    "architecture": "amd64",
    "os": "linux",
    "config": {"Env": [], "WorkingDir": "/"},
    "rootfs": {"type": "layers", "diff_ids": []},
}


def _source_files(builder):
    files = {}
    for absolute, relative in iter_files(
        Path(builder.config.context_dir),
        builder.context_include_paths,
        exclude_paths=builder.context_excludes,
        exclude_defaults=builder.config.exclude_defaults,
    ):
        data = Path(absolute).read_bytes()
        files[relative.as_posix()] = {
            "mode": oct(stat.S_IMODE(os.lstat(absolute).st_mode)),
            "sha256": hashlib.sha256(data).hexdigest(),
            "crlf": b"\r\n" in data,
        }
    return files


def build_and_report(context, cache_dir, output_dir):
    builder = ImageBuilder(
        BuildConfig(
            tag="cache-portability:local",
            context_dir=str(context),
            output_dir=str(output_dir),
            cache_dir=str(cache_dir),
            use_cache=True,
            entrypoint=["python", "main.py"],
        )
    )
    builder._pull_base_image = lambda *_args: ([], json.loads(json.dumps(_SYNTHETIC_BASE_CONFIG)))
    lookups = []
    original_get_layer = builder.cache.get_layer

    def recording_get_layer(*args, **kwargs):
        result = original_get_layer(*args, **kwargs)
        lookups.append(result is not None)
        return result

    builder.cache.get_layer = recording_get_layer
    builder.build()
    return {
        "os": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "context": str(Path(context).resolve()),
        "cache_dir": str(Path(cache_dir).resolve()),
        "cache_hit": lookups == [True],
        "manifest": builder.manifest_digest,
        "config": builder.config_digest,
        "layers": [layer.digest for layer in builder.layers],
        "files": _source_files(builder),
    }


def _differences(source, current):
    reasons = []
    for name in sorted(set(source["files"]) | set(current["files"])):
        before = source["files"].get(name)
        after = current["files"].get(name)
        if before is None or after is None:
            reasons.append(f"{name}: present only in {'verify' if before is None else 'source'}")
            continue
        if before["mode"] != after["mode"]:
            reasons.append(f"{name}: mode {before['mode']} -> {after['mode']}")
        if before["sha256"] != after["sha256"]:
            line_endings = " (line endings differ)" if before["crlf"] != after["crlf"] else ""
            reasons.append(f"{name}: content differs{line_endings}")
    return reasons


def _summary(source, current, reasons):
    outcome = "hit" if current["cache_hit"] else "miss"
    same_image = current["manifest"] == source["manifest"]
    lines = [
        f"### Cache restore: {source['os']} -> {current['os']}",
        "",
        f"- Outcome: **{outcome}**; identical manifest: **{same_image}**",
        f"- Source cache {source['cache_dir']}, context {source['context']}",
        f"- Restored cache {current['cache_dir']}, context {current['context']}",
    ]
    lines += [f"- Key difference: {reason}" for reason in reasons] or ["- Key difference: none"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("populate", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--context", required=True)
        command.add_argument("--cache-dir", required=True)
        command.add_argument("--output-dir", required=True)
        command.add_argument("--report", required=True)
        if name == "verify":
            command.add_argument("--source-report", required=True)
    args = parser.parse_args(argv)

    report = build_and_report(args.context, args.cache_dir, args.output_dir)
    if args.command == "populate":
        if report["cache_hit"]:
            parser.error("populate expects an empty cache")
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0

    source = json.loads(Path(args.source_report).read_text(encoding="utf-8"))
    reasons = _differences(source, report)
    report["source"] = {key: source[key] for key in ("os", "machine", "manifest")}
    report["key_differences"] = reasons
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = _summary(source, report, reasons)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)

    same_os = (source["os"], source["machine"]) == (report["os"], report["machine"])
    if same_os and not reasons and (
        not report["cache_hit"] or report["manifest"] != source["manifest"]
    ):
        print("Same-OS restore with identical sources did not reproduce the image.", file=sys.stderr)
        return 1
    if report["cache_hit"] and report["manifest"] != source["manifest"]:
        print("Cache hit produced a different image.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
