"""Partition one installed lock selection into deterministic OCI layers."""
import base64
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PureWindowsPath
from typing import Dict, List, Optional, Tuple

from .lockfiles import LockSelection, SpecifierSet, canonical_name


GROUPS = ("stable", "frequent")
GROUP_POLICY_VERSION = 1
GROUP_CACHE_VERSION = 1
MAX_METADATA_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
HASH_CHUNK_SIZE = 1024 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_GROUP_KEY = re.compile(r"deps-group-v1:sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class GroupPlan:
    strategy: str
    target: str
    assignments: Tuple[Tuple[str, str], ...]
    stable_overrides: Tuple[str, ...]
    frequent_overrides: Tuple[str, ...]
    history_snapshot: Optional[str] = None
    history_commits: Tuple[str, ...] = ()

    def identity(self) -> dict:
        return {
            "version": GROUP_POLICY_VERSION,
            "strategy": self.strategy,
            "target": self.target,
            "assignments": list(self.assignments),
            "stable_overrides": self.stable_overrides,
            "frequent_overrides": self.frequent_overrides,
            "history_snapshot": self.history_snapshot,
            "history_commits": self.history_commits,
        }

def _cache_key(prefix: str, identity: dict) -> str:
    content = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}:sha256:{hashlib.sha256(content).hexdigest()}"


def group_bundle_key(selection_key: str, plan: GroupPlan) -> str:
    return _cache_key(
        f"deps-group-bundle-v{GROUP_CACHE_VERSION}",
        {"version": GROUP_CACHE_VERSION, "selection": selection_key, "plan": plan.identity()},
    )


def group_layer_key(
    selection_key: str, plan: GroupPlan, group: str, files_key: str
) -> str:
    return _cache_key(
        f"deps-group-v{GROUP_CACHE_VERSION}",
        {
            "version": GROUP_CACHE_VERSION,
            "selection": selection_key,
            "plan": plan.identity(),
            "group": group,
            "files": files_key,
        },
    )


def encode_group_bundle(
    selection_key: str, plan: GroupPlan, layers: list, packages: list
) -> bytes:
    return json.dumps(
        {
            "version": GROUP_CACHE_VERSION,
            "selection": selection_key,
            "plan": plan.identity(),
            "layers": layers,
            "packages": packages,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_group_bundle(
    content: bytes, selection_key: str, plan: GroupPlan, selection: LockSelection,
    *, strip_type_stubs: bool = False,
) -> Optional[Tuple[list, List[Tuple[str, str]]]]:
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    expected_plan = json.loads(json.dumps(plan.identity()))
    if (
        not isinstance(document, dict)
        or document.get("version") != GROUP_CACHE_VERSION
        or document.get("selection") != selection_key
        or document.get("plan") != expected_plan
        or not isinstance(document.get("layers"), list)
        or not isinstance(document.get("packages"), list)
    ):
        return None
    expected_packages = {package.name: package for package in selection.packages}
    packages = []
    seen = set()
    for item in document["packages"]:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) and value for value in item)
        ):
            return None
        name, version = item
        normalized = canonical_name(name)
        if normalized in seen or normalized not in expected_packages:
            return None
        try:
            if not SpecifierSet(f"=={expected_packages[normalized].version}").contains(
                version, prereleases=True
            ):
                return None
        except ValueError:
            return None
        seen.add(normalized)
        packages.append((name, version))
    missing = set(expected_packages) - seen
    if missing and (
        not strip_type_stubs
        or any(not (name.startswith("types-") or name.endswith("-stubs")) for name in missing)
    ):
        return None
    layers = document["layers"]
    expected_groups = [
        group for group in GROUPS
        if any(name in seen and value == group for name, value in plan.assignments)
    ]
    if len(layers) != len(expected_groups):
        return None
    for item, group in zip(layers, expected_groups):
        if (
            not isinstance(item, dict)
            or item.get("group") != group
            or not isinstance(item.get("key"), str)
            or not _GROUP_KEY.fullmatch(item["key"])
            or not isinstance(item.get("digest"), str)
            or not _DIGEST.fullmatch(item["digest"])
        ):
            return None
    return layers, packages


def plan_groups(
    selection: LockSelection,
    strategy: str,
    stable: List[str],
    frequent: List[str],
    context: Path,
    history_ref: Optional[str],
    target: str,
) -> GroupPlan:
    if selection.kind != "uv":
        raise ValueError(
            "Grouped dependency layers currently require a target-selected uv.lock; "
            "other lock formats are not yet supported."
        )
    stable_names = tuple(sorted(canonical_name(name) for name in stable))
    frequent_names = tuple(sorted(canonical_name(name) for name in frequent))
    names = {package.name for package in selection.packages}
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name) for name in names):
        raise ValueError("Grouped dependency selection contains an invalid distribution name.")
    for name in sorted((set(stable_names) | set(frequent_names)) - names):
        print(f"Dependency group assignment skipped: {name} is not selected for this target.")

    snapshot = None
    commits: Tuple[str, ...] = ()
    history_groups: Dict[str, Tuple[str, str]] = {}
    if strategy == "history":
        from .dependency_history import analyze_dependency_history

        snapshot, commits, history_groups = analyze_dependency_history(
            context, selection, history_ref
        )
    assignments = []
    for name in sorted(names):
        if name in stable_names:
            group, reason = "stable", "explicit stable assignment"
        elif name in frequent_names:
            group, reason = "frequent", "explicit frequent assignment"
        elif strategy == "history":
            group, reason = history_groups[name]
        else:
            group, reason = "frequent", "unassigned distributions use the frequent group"
        assignments.append((name, group))
        if strategy == "history":
            print(f"Dependency history: {name} -> {group} ({reason}).")
    return GroupPlan(
        strategy, target, tuple(assignments), stable_names, frequent_names, snapshot, commits
    )


def _record_path(site_packages: Path, value: str) -> Path:
    parts = value.split("/")
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or PureWindowsPath(value).drive
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ValueError("Dependency RECORD contains an unsafe path.")
    path = site_packages.joinpath(*parts)
    if not path.resolve().is_relative_to(site_packages.resolve()):
        raise ValueError("Dependency RECORD contains a path outside site-packages.")
    for parent in (path, *path.relative_to(site_packages).parents):
        candidate = parent if parent.is_absolute() else site_packages / parent
        if candidate.is_symlink():
            raise ValueError("Dependency RECORD contains a symbolic link.")
    if not path.is_file():
        raise ValueError(f"Dependency RECORD names a missing file: {value}")
    return path


def _verify_record_file(path: Path, digest: str, size: str, relative: str) -> None:
    if size:
        if not size.isdecimal() or int(size) != path.stat().st_size:
            raise ValueError(f"Dependency file integrity mismatch: {relative}")
    if not digest:
        return
    algorithm, separator, expected = digest.partition("=")
    if not separator or algorithm not in {"sha256", "sha384", "sha512"}:
        raise ValueError(f"Unsupported dependency RECORD hash: {relative}")
    hasher = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    actual = base64.urlsafe_b64encode(hasher.digest()).decode("ascii").rstrip("=")
    if actual != expected:
        raise ValueError(f"Dependency file integrity mismatch or collision: {relative}")


def partition_staged_files(
    stage_root: Path,
    selection: LockSelection,
    plan: GroupPlan,
    script_owners: Dict[str, List[Path]],
) -> Dict[str, List[Path]]:
    site_packages = stage_root / "site-packages"
    expected = {package.name: package for package in selection.packages}
    assignments = dict(plan.assignments)
    seen_packages = {}
    owners = {}
    claimed = {}
    grouped: Dict[str, List[Path]] = {group: [] for group in GROUPS}

    for dist_info in sorted(site_packages.glob("*.dist-info")):
        metadata = dist_info / "METADATA"
        record = dist_info / "RECORD"
        if (
            dist_info.is_symlink()
            or not dist_info.is_dir()
            or not metadata.is_file()
            or metadata.is_symlink()
            or not record.is_file()
            or record.is_symlink()
        ):
            raise ValueError(f"Grouped dependency metadata is incomplete: {dist_info.name}")
        if metadata.stat().st_size > MAX_METADATA_BYTES or record.stat().st_size > MAX_RECORD_BYTES:
            raise ValueError(f"Grouped dependency metadata exceeds its size limit: {dist_info.name}")
        with metadata.open(encoding="utf-8", errors="strict") as source:
            message = Parser().parse(source, headersonly=True)
        name, version = message.get("Name"), message.get("Version")
        if not name or not version:
            raise ValueError(f"Grouped dependency metadata lacks a name or version: {dist_info.name}")
        name, version = canonical_name(name.strip()), version.strip()
        if name in seen_packages or name not in expected:
            raise ValueError(f"Grouped dependency selection has duplicate or unexpected package: {name}")
        try:
            matches = SpecifierSet(f"=={expected[name].version}").contains(
                version, prereleases=True
            )
        except ValueError as error:
            raise ValueError(f"Grouped dependency has an invalid version: {name}") from error
        if not matches:
            raise ValueError(f"Grouped dependency version differs from the selected lock: {name}")
        seen_packages[name] = version
        owners[dist_info] = name
        with record.open(encoding="utf-8", errors="strict", newline="") as source:
            try:
                rows = csv.reader(source, strict=True)
                for row in rows:
                    if len(row) != 3:
                        raise ValueError(f"Malformed dependency RECORD: {dist_info.name}")
                    relative, digest, size = row
                    path = _record_path(site_packages, relative)
                    key = relative.casefold()
                    if key in claimed:
                        raise ValueError(
                            f"Dependency file ownership collision: {relative} "
                            f"({claimed[key]} and {name})."
                        )
                    _verify_record_file(path, digest, size, relative)
                    claimed[key] = name
                    grouped[assignments[name]].append(path)
            except csv.Error as error:
                raise ValueError(f"Malformed dependency RECORD: {dist_info.name}") from error

    if set(seen_packages) != set(expected):
        missing = ", ".join(sorted(set(expected) - set(seen_packages)))
        raise ValueError(f"Grouped dependencies lack selected distribution metadata: {missing}")

    script_names = set()
    for name, metadata_paths in sorted(script_owners.items()):
        key = name.casefold()
        if key in script_names:
            raise ValueError(f"Dependency console entrypoint collision: {name}")
        script_names.add(key)
        if any(path not in owners for path in metadata_paths):
            raise ValueError(f"Dependency entrypoint has unknown ownership: {name}")
        if len(metadata_paths) != 1:
            raise ValueError(f"Dependency console entrypoint collision: {name}")
        owner = owners[metadata_paths[0]]
        path = stage_root / "bin" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Dependency entrypoint is missing or unsafe: {name}")
        grouped[assignments[owner]].append(path)

    owned = {path.relative_to(stage_root).as_posix() for files in grouped.values() for path in files}
    for path in stage_root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Grouped dependencies contain a symbolic link.")
        if path.is_file() and path.relative_to(stage_root).as_posix() not in owned:
            raise ValueError(
                f"Grouped dependencies contain an unowned file: "
                f"{path.relative_to(stage_root).as_posix()}"
            )
    return {
        group: sorted(files, key=lambda path: path.relative_to(stage_root).as_posix())
        for group, files in grouped.items()
    }
