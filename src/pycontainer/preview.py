"""Pre-build preview: compare a planned build with an existing image without building it.

The planned application layer is serialized with the builder's own tar writer
into a hashing sink, so its file list, metadata and DiffID match what ``build``
would write. The baseline is read through the bounded ``layer_inspect`` readers:
a local OCI layout or docker-save archive (offline), or a registry image named
with an explicit ``docker://`` prefix. Only the baseline config and final layer
are read; every temporary file lives in a temporary directory that is removed
before returning. Nothing is built, cached or executed.

A result is reported as identical, changed, added or removed only when the
evidence supports it; otherwise it is ``unknown``.
"""
import contextlib
import dataclasses
import hashlib
import json
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .auth import get_registry_auth
from .blob_store import digest_filename
from .builder import ImageBuilder, parse_platform, parse_platform_variant, write_app_files
from .config import BuildConfig
from .fs_utils import iter_files
from .layer_inspect import (
    INSPECTION_ERRORS,
    Entry,
    LayerInspectionError,
    LayerReport,
    _load_config,
    _select_manifest,
    display,
    normalize_member_path,
    read_layer,
    resolve_reference,
)
from .local_image import (
    MAX_ARCHIVE_MEMBERS,
    MAX_LAYER_UNCOMPRESSED_BYTES,
    open_local_image_source,
    parse_local_image_reference,
)
from .oci import build_config_json
from .project import detect_entrypoint
from .registry_client import RegistryClient, parse_image_reference

REMOTE_PREFIX = "docker://"
IDENTICAL, CHANGED, UNKNOWN = "identical", "changed", "unknown"
ADDED, MODIFIED, DELETED, REMOVED = "added", "modified", "deleted", "removed"
EXACT, POSITIONAL, INFERRED, NO_PROVENANCE = "exact", "positional", "inferred", "none"
REDACTED = "<redacted>"
SHELLS = ("/bin/sh", "/bin/bash", "sh", "bash")
APP_LAYER_KINDS = {"file", "symlink", "hardlink"}
DEPENDENCY_ENV = ("PATH", "PYTHONPATH")
PROVENANCE_NOTES = {
    EXACT: "The planned application layer DiffID equals the baseline's final layer DiffID.",
    POSITIONAL: "The planned base DiffIDs are the baseline's lower layers, so its upper layers are the "
                "dependency and application layers.",
    INFERRED: "The baseline's final layer has the shape of an application layer under its WorkingDir; "
              "file changes are inferred from that layer alone.",
    NO_PROVENANCE: "The baseline's application layer could not be identified; application changes are unknown.",
}
LIMITS = (
    "Base layers are compared by the DiffIDs their image config declares; base blobs are not downloaded or verified.",
    "Dependencies are staged at build time; the preview does not run pip, so dependency results are unknown "
    "when --include-deps is planned.",
    "Only the baseline config and final layer are read. Files are compared by type, content digest, size, mode, "
    "owner and link target; modification times are not compared.",
    "Non-reproducible builds can embed modification times or reuse cached layers, so exact application "
    "identity cannot be established.",
    "Environment values are never printed.",
)


class PreviewError(RuntimeError):
    """Raised when a preview cannot be produced completely and safely."""


class _HashSink:
    """Write-only file object that hashes and counts what is written to it, up to a byte bound."""

    def __init__(self, limit: int):
        self.hasher = hashlib.sha256()
        self.offset = 0
        self.limit = limit

    def write(self, data) -> int:
        if self.offset + len(data) > self.limit:
            raise PreviewError(f"The planned application layer is larger than {self.limit} bytes.")
        self.hasher.update(data)
        self.offset += len(data)
        return len(data)

    def tell(self) -> int:
        return self.offset


class _HashingReader:
    def __init__(self, source):
        self.source = source
        self.hasher = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.hasher.update(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.source.close()


class _RecordingTar(tarfile.TarFile):
    """TarFile that records the metadata of every member it writes."""

    def addfile(self, tarinfo, fileobj=None, **kwargs):
        super().addfile(tarinfo, fileobj, **kwargs)
        self.recorded.append((tarinfo, getattr(fileobj, "hasher", None)))


class _RegistrySource:
    """Adapt a RegistryClient to the source interface the layer_inspect readers use."""

    def __init__(self, client: RegistryClient, reference: str, name: str):
        self.client, self.reference, self.source_name = client, reference, name

    def pull_manifest(self, digest=None, expected_digest=None, expected_size=None):
        reference = digest or self.reference
        if expected_digest is None and reference.startswith("sha256:"):
            expected_digest = reference
        return self.client.pull_manifest(reference, expected_digest=expected_digest, expected_size=expected_size)

    def pull_blob(self, digest: str, dest_path: Path, expected_size: int) -> bool:
        return self.client.pull_blob(digest, dest_path, expected_size)


def _registry_client(registry: str, repository: str, config: BuildConfig) -> RegistryClient:
    auth = get_registry_auth(registry)
    return RegistryClient(
        registry, repository, auth_token=auth.bearer_token, username=auth.username, password=auth.password,
        insecure=registry.lower() in config.insecure_registries, ca_file=config.registry_ca_file,
    )


def _member_entry(info: tarfile.TarInfo, digest: Optional[str]) -> Entry:
    common = {"layer": 0, "mode": info.mode & 0o7777, "uid": info.uid, "gid": info.gid}
    if info.isreg():
        return Entry("file", size=info.size, digest=digest, **common)
    if info.issym():
        return Entry("symlink", link=info.linkname, **common)
    if info.islnk():
        return Entry("hardlink", link=normalize_member_path(info.linkname, 0), **common)
    if info.isfifo():
        return Entry("fifo", **common)
    if info.ischr() or info.isblk():
        return Entry("char" if info.ischr() else "block", link=f"{info.devmajor}:{info.devminor}", **common)
    raise PreviewError(f"Planned application file {display(info.name)!r} has an unsupported type.")


def plan_app_layer(files, workdir: str, reproducible: bool) -> Tuple[str, Dict[str, Entry]]:
    """Serialize the application layer exactly as the builder would and return (DiffID, entries)."""
    if len(files) > MAX_ARCHIVE_MEMBERS:
        raise PreviewError(f"The build context selects more than {MAX_ARCHIVE_MEMBERS} files.")
    sink = _HashSink(MAX_LAYER_UNCOMPRESSED_BYTES)
    with _RecordingTar.open(fileobj=sink, mode="w") as tar:
        tar.recorded = []
        write_app_files(tar, files, workdir, reproducible,
                        opener=lambda path, _info: _HashingReader(open(path, "rb")))
    entries = {}
    for info, hasher in tar.recorded:
        digest = f"sha256:{hasher.hexdigest()}" if hasher else None
        entries[normalize_member_path(info.name, 0)] = _member_entry(info, digest)
    return f"sha256:{sink.hasher.hexdigest()}", entries


def _parse_baseline(baseline: str):
    if baseline.startswith(REMOTE_PREFIX):
        try:
            return None, parse_image_reference(baseline[len(REMOTE_PREFIX):])
        except ValueError as error:
            raise PreviewError(f"Invalid remote baseline {display(baseline)!r}: {error}") from error
    try:
        return resolve_reference(baseline), None
    except LayerInspectionError as error:
        raise PreviewError(
            f"{display(baseline)!r} is not a baseline image. Use oci:<dir>[:<ref>|@sha256:<digest>], "
            "docker-archive:<file>[:<tag>], an OCI layout directory, or docker://<registry>/<repository>"
            "[:<tag>|@sha256:<digest>] to read a registry image."
        ) from error


@contextlib.contextmanager
def _open_source(local, remote, blobs: Path, platform: str, config: BuildConfig):
    if remote is not None:
        registry, repository, reference = remote
        yield _RegistrySource(_registry_client(registry, repository, config), reference,
                              f"{registry}/{repository}{'@' if reference.startswith('sha256:') else ':'}{reference}")
        return
    os_name, arch = parse_platform(platform)
    with open_local_image_source(local, blobs, blobs / "output", os_name, arch,
                                 parse_platform_variant(platform)) as source:
        yield source


def _resolve_base(base_image: str, remote_allowed: bool, blobs: Path, platform: str, config: BuildConfig):
    """Return (base config, reason) for the planned base, or (None, reason) when it is not resolved."""
    local = parse_local_image_reference(base_image)
    if local is None and not remote_allowed:
        return None, ("The planned base is a registry image and a local baseline is compared offline; "
                      "the base was not resolved.")
    remote = None if local else parse_image_reference(base_image)
    blobs.mkdir()
    with _open_source(local, remote, blobs, platform, config) as source:
        manifest, _, variant = _select_manifest(source, platform)
        config = _load_config(source, manifest, blobs, platform, variant)[1]
    return config, None


def _final_layer(source, layers, diff_ids, blobs: Path) -> List[Tuple[str, Optional[Entry], Optional[str]]]:
    digest, size, media_type = layers[-1]
    path = blobs / digest_filename(digest)
    source.pull_blob(digest, path, size)
    report = LayerReport(len(layers), digest, media_type, size, diff_ids[-1])
    return read_layer(path, report, [0])


def _app_shape(records, workdir) -> Optional[Dict[str, Entry]]:
    """Return the layer's entries when it looks like a pycontainer application layer under ``workdir``."""
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        return None
    prefix = workdir.strip("/") + "/"
    entries = {}
    for path, entry, whiteout in records:
        if whiteout or entry is None or entry.kind not in APP_LAYER_KINDS or not path.startswith(prefix):
            return None
        entries[path] = entry
    return entries or None


def _reasons(before: Entry, after: Entry) -> List[str]:
    reasons = []
    if before.kind != after.kind:
        reasons.append("type")
    if (before.digest, before.size) != (after.digest, after.size):
        reasons.append("content")
    if before.link != after.link:
        reasons.append("link")
    if before.mode != after.mode:
        reasons.append("mode")
    if (before.uid, before.gid) != (after.uid, after.gid):
        reasons.append("owner")
    return reasons


def _file_changes(baseline: Dict[str, Entry], planned: Dict[str, Entry]) -> List[Dict]:
    changes = []
    for path in sorted(baseline.keys() | planned.keys()):
        before, after = baseline.get(path), planned.get(path)
        if before is None:
            changes.append({"change": ADDED, "path": "/" + path, "after": after.to_dict()})
        elif after is None:
            changes.append({"change": DELETED, "path": "/" + path, "before": before.to_dict()})
        elif reasons := _reasons(before, after):
            changes.append({"change": MODIFIED, "path": "/" + path, "reasons": reasons,
                            "before": before.to_dict(), "after": after.to_dict()})
    for change in changes:
        for side in ("before", "after"):
            change.get(side, {}).pop("layer", None)
    return changes


def _env(items) -> Dict[str, str]:
    return {item.split("=", 1)[0]: item.split("=", 1)[1] for item in items or [] if isinstance(item, str) and "=" in item}


def _planned_settings(cfg: BuildConfig, base_config: Optional[Dict]) -> Tuple[Dict, set]:
    """Return the predicted config fields and the names whose planned value cannot be established."""
    entry = cfg.entrypoint or detect_entrypoint(cfg.context_dir)
    os_name, arch = parse_platform(cfg.platform)
    unknown = {f"Env.{name}" for name in DEPENDENCY_ENV} if cfg.include_deps else set()
    if base_config is not None:
        predicted = build_config_json(arch, os_name, dict(cfg.env), cfg.workdir, entry, cfg.exposed_ports,
                                      labels=cfg.labels, user=cfg.user, cmd=cfg.cmd, base_config=base_config,
                                      layer_diff_ids=[])["config"]
        return predicted, unknown
    predicted = {"WorkingDir": cfg.workdir, "Env": [f"{k}={v}" for k, v in cfg.env.items()],
                 "Labels": dict(cfg.labels or {})}
    unknown |= {"Env.*", "Labels.*"}
    if entry and entry[0] not in SHELLS:
        predicted["Entrypoint"] = entry
    else:
        unknown |= {"Entrypoint", "Cmd"}
    for name, value in (("Cmd", cfg.cmd), ("User", cfg.user)):
        if value:
            predicted[name] = value
            unknown.discard(name)
        else:
            unknown.add(name)
    if cfg.exposed_ports:
        predicted["ExposedPorts"] = {f"{port}/tcp": {} for port in cfg.exposed_ports}
    else:
        unknown.add("ExposedPorts")
    return predicted, unknown


def _compare(name: str, before, after, known: bool, redact: bool = False) -> Optional[Dict]:
    if not known:
        status = UNKNOWN
    elif before is None and after is None:
        return None
    elif before is None:
        status = ADDED
    elif after is None:
        status = REMOVED
    else:
        status = IDENTICAL if before == after else CHANGED
    item = {"name": name, "status": status}
    if redact:
        item.update(before=REDACTED if before is not None else None,
                    after=REDACTED if after is not None and known else None)
    else:
        item.update(before=before, after=after if known else None)
    return item


def _settings(baseline_config: Dict, predicted: Dict, unknown: set) -> List[Dict]:
    before = baseline_config.get("config") if isinstance(baseline_config.get("config"), dict) else {}
    results = []
    for field in ("WorkingDir", "Entrypoint", "Cmd", "User", "ExposedPorts"):
        results.append(_compare(field, before.get(field), predicted.get(field), field not in unknown))
    for group, values, redact in (("Env", _env, True), ("Labels", lambda v: v if isinstance(v, dict) else {}, False)):
        old, new = values(before.get(group)), values(predicted.get(group))
        for key in sorted(old.keys() | new.keys()):
            name = f"{group}.{key}"
            known = name not in unknown and (key in new or f"{group}.*" not in unknown)
            results.append(_compare(name, old.get(key), new.get(key), known, redact))
    return [item for item in results if item is not None]


def _planned_builder(cfg: BuildConfig) -> ImageBuilder:
    return ImageBuilder(dataclasses.replace(cfg, dry_run=True, use_cache=False))


def preview_image(cfg: BuildConfig, baseline: str) -> Dict:
    """Compare the image ``cfg`` would build with ``baseline`` and return a JSON-serializable report."""
    parse_platform(cfg.platform)
    local, remote = _parse_baseline(baseline)
    builder = _planned_builder(cfg)
    plan = builder.config
    files = list(iter_files(Path(plan.context_dir), builder.context_include_paths,
                            exclude_paths=builder.context_excludes, exclude_defaults=plan.exclude_defaults))
    planned_diff_id, planned_entries = plan_app_layer(files, plan.workdir, plan.reproducible)

    with tempfile.TemporaryDirectory(prefix="pycontainer-preview-") as temporary, \
            contextlib.redirect_stdout(sys.stderr):
        root = Path(temporary)
        baseline_blobs = root / "baseline"
        baseline_blobs.mkdir()
        with _open_source(local, remote, baseline_blobs, plan.platform, plan) as source:
            manifest, manifest_digest, variant = _select_manifest(source, plan.platform)
            config_ref, config, platform_text, layers, diff_ids, _ = _load_config(
                source, manifest, baseline_blobs, plan.platform, variant
            )
            if not layers:
                raise PreviewError("The baseline image has no layers.")
            records = _final_layer(source, layers, diff_ids, baseline_blobs)
            source_name = getattr(source, "source_name", str(getattr(local, "path", baseline)))
        base_config, base_reason = _resolve_base(plan.base_image, remote is not None, root / "base", plan.platform, plan)

    base_ids = None
    if base_config is not None:
        rootfs = base_config.get("rootfs")
        base_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
        if not isinstance(base_ids, list) or not isinstance(base_config.get("config", {}), dict):
            raise PreviewError("The planned base image config is malformed.")
        base_config.setdefault("config", {})

    baseline_workdir = (config.get("config") or {}).get("WorkingDir") if isinstance(config.get("config"), dict) else None
    shaped = _app_shape(records, baseline_workdir)
    upper = len(diff_ids) - len(base_ids) if base_ids is not None and diff_ids[:len(base_ids)] == base_ids else None

    if base_ids is None:
        base = {"status": UNKNOWN, "reason": base_reason}
    elif upper is not None:
        base = {"status": IDENTICAL, "reason": "The planned base DiffIDs are the baseline's lower layers."}
    else:
        base = {"status": CHANGED, "reason": "The planned base DiffIDs are not the baseline's lower layers."}

    if plan.reproducible and planned_diff_id == diff_ids[-1]:
        provenance = EXACT
    elif upper in (1, 2) and shaped is not None:
        provenance = POSITIONAL
    elif shaped is not None:
        provenance = INFERRED
    else:
        provenance = NO_PROVENANCE

    if provenance == EXACT:
        changes = []
    elif provenance == NO_PROVENANCE:
        changes = None
    else:
        changes = _file_changes(shaped, planned_entries)
        if provenance == INFERRED:
            for change in changes:
                change["inferred"] = True

    if provenance == EXACT:
        app = {"status": IDENTICAL, "reason": PROVENANCE_NOTES[EXACT]}
    elif provenance == POSITIONAL and changes:
        app = {"status": CHANGED, "reason": f"{len(changes)} application paths differ."}
    elif provenance == POSITIONAL and plan.reproducible:
        app = {"status": CHANGED, "reason": "No compared file metadata differs, but the layer DiffID does "
                                            "(member order, names or tar headers changed)."}
    elif provenance == POSITIONAL:
        app = {"status": UNKNOWN, "reason": "Non-reproducible builds embed modification times and may reuse a "
                                            "cached layer, so the layer bytes cannot be predicted."}
    else:
        app = {"status": UNKNOWN, "reason": PROVENANCE_NOTES[provenance]}

    if plan.include_deps:
        deps = {"status": UNKNOWN, "reason": "Dependencies are staged at build time and were not resolved."}
    elif upper == 2 and provenance in (EXACT, POSITIONAL):
        deps = {"status": REMOVED, "reason": "The baseline has a dependency layer; the plan stages none."}
    elif upper == 1 and provenance in (EXACT, POSITIONAL):
        deps = {"status": IDENTICAL, "reason": "Neither the baseline nor the plan has a dependency layer."}
    else:
        deps = {"status": UNKNOWN, "reason": "The baseline's dependency layer could not be identified."}

    predicted, unknown = _planned_settings(plan, base_config)
    return {
        "baseline": {
            "reference": baseline, "image": source_name, "source": "registry" if remote else "local",
            "platform": platform_text, "manifest": {"digest": manifest_digest, "mediaType": manifest.get("mediaType")},
            "config": {"digest": config_ref[0], "size": config_ref[1]}, "layers": len(layers),
            "finalLayerDiffId": diff_ids[-1],
        },
        "plan": {
            "baseImage": plan.base_image, "platform": plan.platform, "workdir": plan.workdir,
            "reproducible": plan.reproducible, "includeDeps": plan.include_deps,
            "useCache": cfg.use_cache,
            "appLayerDiffId": planned_diff_id if plan.reproducible else None,
            "files": len(planned_entries),
        },
        "provenance": {"level": provenance, "reason": PROVENANCE_NOTES[provenance]},
        "components": {"base": base, "dependencies": deps, "application": app},
        "files": changes,
        "settings": _settings(config, predicted, unknown),
        "limits": list(LIMITS),
    }


def _value(value) -> str:
    return display(value if isinstance(value, str) else json.dumps(value))


def render_text(result: Dict) -> str:
    baseline, plan = result["baseline"], result["plan"]
    lines = [
        f"Baseline:   {display(baseline['image'])} ({baseline['source']}, {display(baseline['platform'])}, "
        f"{baseline['layers']} layers)",
        f"Plan:       base {display(plan['baseImage'])}, workdir {display(plan['workdir'])}, {plan['files']} files"
        f"{'' if plan['reproducible'] else ', non-reproducible'}",
        f"Provenance: {result['provenance']['level']} - {result['provenance']['reason']}",
        "",
    ]
    for name, component in result["components"].items():
        lines.append(f"{name:<13} {component['status']:<9} {display(component['reason'] or '')}")
    files = result["files"]
    lines.append("")
    if files is None:
        lines.append("Application files: unknown")
    else:
        lines.append(f"Application files: {len(files)} changed")
        marks = {ADDED: "A", MODIFIED: "M", DELETED: "D"}
        for change in files:
            detail = f" ({', '.join(change['reasons'])})" if change.get("reasons") else ""
            inferred = " [inferred]" if change.get("inferred") else ""
            lines.append(f"  {marks[change['change']]} {display(change['path'])}{detail}{inferred}")
    lines += ["", "Settings:"]
    for item in result["settings"]:
        if item["status"] == IDENTICAL:
            continue
        values = ""
        if item["status"] != UNKNOWN and (item["before"] is not None or item["after"] is not None):
            before = "-" if item["before"] is None else _value(item["before"])
            after = "-" if item["after"] is None else _value(item["after"])
            values = f": {before} -> {after}"
        lines.append(f"  {item['status']:<9} {display(item['name'])}{values}")
    lines += ["", "Limits:"] + [f"  - {limit}" for limit in result["limits"]]
    return "\n".join(lines)


def run_preview(cfg: BuildConfig, baseline: str, as_json: bool = False) -> Tuple[str, int]:
    """Run a preview for the CLI, returning (output, exit code); errors never produce a partial report."""
    try:
        result = preview_image(cfg, baseline)
    except (PreviewError, LayerInspectionError, *INSPECTION_ERRORS, tarfile.TarError, OSError) as error:
        return f"error: {display(str(error))}", 1
    if as_json:
        return json.dumps(result, indent=2), 0
    return render_text(result), 0
