"""Gate releases on passing OCI import and container startup evidence.

Each required target needs exactly one record written by
``check_runtime_image.py --evidence`` that passed a full OCI import and
startup on a native daemon for the expected commit. Missing, failed,
duplicate or partial records are reported as not validated, never as passing.
"""
import argparse
import json
import os
import sys
from pathlib import Path

EVIDENCE_SCHEMA = 1
FULL_MODE = "oci-import-startup"
RUNTIME_MARKER = "pycontainer-runtime-dependency-ok"
MISSING_REASON = "no evidence recorded (runner unavailable, skipped or cancelled)"


def os_arch(platform):
    return "/".join(platform.split("/")[:2])


def load_records(evidence_dir):
    records, errors = [], []
    if not evidence_dir.is_dir():
        return records, errors
    for path in sorted(evidence_dir.rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: unreadable evidence ({exc})")
            continue
        if not isinstance(record, dict) or not isinstance(record.get("target_platform"), str):
            errors.append(f"{path.name}: evidence has no target platform")
            continue
        records.append(record)
    return records, errors


def rejection_reason(record, target, commit):
    if record.get("schema") != EVIDENCE_SCHEMA:
        return f"unsupported evidence schema {record.get('schema')!r}"
    if record.get("status") != "passed":
        return f"check {record.get('status', 'unknown')}: {record.get('error', 'no error recorded')}"
    if record.get("mode") != FULL_MODE:
        return f"mode {record.get('mode')!r} is not a full OCI import and startup"
    if commit and record.get("commit") != commit:
        return f"evidence is for commit {record.get('commit')!r}, expected {commit}"
    expected = os_arch(target)
    for field in ("daemon_platform", "image_platform"):
        if record.get(field) != expected:
            return f"{field} {record.get(field)!r} does not match {expected}"
    if RUNTIME_MARKER not in str(record.get("container_stdout", "")):
        return "container output lacks the runtime dependency marker"
    return None


def evaluate(records, required, commit):
    by_target = {}
    for record in records:
        by_target.setdefault(record["target_platform"], []).append(record)
    validated, not_validated = [], []
    for target in required:
        matches = by_target.get(target, [])
        if not matches:
            not_validated.append((target, MISSING_REASON))
        elif len(matches) > 1:
            not_validated.append((target, f"{len(matches)} conflicting evidence records"))
        else:
            reason = rejection_reason(matches[0], target, commit)
            if reason:
                not_validated.append((target, reason))
            else:
                validated.append(matches[0])
    extra = sorted(set(by_target) - set(required))
    return validated, not_validated, extra


def render_summary(validated, not_validated, extra, errors, commit):
    lines = ["## Target runtime verification", ""]
    if commit:
        lines += [f"Commit: `{commit}`", ""]
    lines += [
        "Each validated target built an image with pycontainer, imported the OCI",
        "layout with Skopeo into a native Docker daemon, and started a container",
        "that ran a staged dependency entry point.",
        "",
    ]
    if validated:
        lines += [
            "| Target | Base image | Image manifest | Docker | Skopeo | Run |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for record in validated:
            image = record.get("image") or {}
            run = record.get("run_url")
            lines.append(
                f"| `{record['target_platform']}` | `{record.get('base_image')}` | "
                f"`{image.get('manifest_digest')}` | {record.get('docker_version')} | "
                f"{record.get('skopeo_version')} | {f'[logs]({run})' if run else 'local'} |"
            )
    else:
        lines.append("No target passed runtime verification.")
    if not_validated or errors:
        lines += ["", "### Not validated", ""]
        lines += [f"- `{target}`: {reason}" for target, reason in not_validated]
        lines += [f"- {error}" for error in errors]
    if extra:
        lines += ["", "Ignored records for unrequired targets: " + ", ".join(f"`{t}`" for t in extra)]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument(
        "--require",
        action="append",
        required=True,
        help="target platform that must pass, for example linux/amd64 (repeatable)",
    )
    parser.add_argument("--commit", help="commit SHA every record must match")
    parser.add_argument("--summary", type=Path, help="write the markdown summary here")
    args = parser.parse_args(argv)

    records, errors = load_records(args.evidence_dir)
    validated, not_validated, extra = evaluate(records, args.require, args.commit)
    summary = render_summary(validated, not_validated, extra, errors, args.commit)
    print(summary, end="")
    if args.summary:
        args.summary.write_text(summary, encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(summary)
    if not_validated or errors:
        print("Runtime verification gate failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
