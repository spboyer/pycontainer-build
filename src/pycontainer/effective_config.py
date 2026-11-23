"""Pure, offline resolution of the effective build configuration.

Shared by ``pycontainer build`` and ``pycontainer validate config``. Resolution
reads configuration and project metadata only: it never constructs an
``ImageBuilder``, touches the layer cache, contacts a registry or writes files.
"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .builder import parse_platform
from .config import BuildConfig
from .config_loader import BUILD_TABLE, _validate_config, load_explicit_file_config, merge_configs
from .dependencies import validate_target_platform
from .fs_utils import relative_context_path
from .project import detect_python_version
from .registry_client import parse_image_reference
from .tags import resolve_tags

CLI_FLAGS = {
    "tag": "--tag",
    "tags": "--tag",
    "semver_tags": "--semver-tags",
    "latest_tag": "--latest",
    "base_image": "--base-image",
    "context_dir": "--context",
    "include_paths": "--include",
    "exclude_paths": "--exclude",
    "exclude_defaults": "--no-default-excludes",
    "use_cache": "--no-cache",
    "cache_dir": "--cache-dir",
    "include_deps": "--include-deps",
    "requirements_file": "--requirements",
    "verbose": "--verbose",
    "dry_run": "--dry-run",
    "platform": "--platform",
    "reproducible": "--no-reproducible",
    "generate_sbom": "--sbom",
    "system_packages": "--system-package",
    "detect_system_packages": "--no-detect-system-packages",
}

NOT_PERFORMED = (
    "registry: base image existence, platform match, authentication and push",
    "dependency resolution: dependency source selection, wheel installation and target Python ABI",
    "runtime: OCI import and container startup",
    "build context: file selection, framework defaults and entrypoint detection",
)


class ConfigValidationError(ValueError):
    """One or more source-qualified configuration diagnostics."""

    def __init__(self, diagnostics: List[str]):
        self.diagnostics = list(diagnostics)
        super().__init__("\n".join(self.diagnostics))


def _origin(field: str, overrides: Dict[str, Any], table: Optional[str], file_keys) -> str:
    if field in overrides:
        return f"CLI option {CLI_FLAGS.get(field, field)}"
    if table and field in file_keys:
        return f"{table}.{field}"
    return f"default {field}"


CONSTRUCTOR_GROUPS = (
    ("system_packages",),
    ("generate_sbom",),
    ("tag", "tags", "semver_tags", "latest_tag"),
)


def _constructor_errors(merged: Dict[str, Any], origin) -> List[str]:
    """Attribute BuildConfig construction failures to the settings that caused them."""
    errors = []
    for group in CONSTRUCTOR_GROUPS:
        try:
            BuildConfig(**{key: merged[key] for key in group if key in merged})
        except ValueError as exc:
            if group[0] == "tag":
                primary = merged.get("tag", BuildConfig.tag)
                try:
                    resolve_tags(primary)
                except ValueError:
                    field = "tag"
                else:
                    try:
                        resolve_tags(primary, merged.get("tags"))
                    except ValueError:
                        field = "tags"
                    else:
                        field = "semver_tags" if merged.get("semver_tags") else "latest_tag"
            else:
                field = group[0]
            errors.append(f"{origin(field)}: {exc}")
    return errors


def semantic_errors(cfg: BuildConfig, origin) -> List[str]:
    """Return offline semantic diagnostics for settings the build would reject."""
    errors = []

    def check(field, func, *values):
        for value in values:
            try:
                func(value)
            except ValueError as exc:
                errors.append(f"{origin(field)}: {exc}")
                return

    check("platform", parse_platform, cfg.platform)
    check("base_image", parse_image_reference, cfg.base_image)
    check("tag", lambda _: cfg.all_tags(), cfg.tag)
    check("include_paths", relative_context_path, *(cfg.include_paths or []))
    check("exclude_paths", relative_context_path, *cfg.exclude_paths)
    context = Path(cfg.context_dir)
    if not context.is_dir():
        errors.append(f"{origin('context_dir')}: build context directory not found: {context}")
    if cfg.include_deps:
        check("requirements_file", relative_context_path, cfg.requirements_file)
        if not any(error.startswith(origin("platform")) for error in errors):
            check("platform", lambda value: validate_target_platform(*parse_platform(value)), cfg.platform)
    return errors


def resolve_build_config(
    config_path: Optional[Path], overrides: Dict[str, Any]
) -> Tuple[BuildConfig, Optional[str]]:
    """Resolve defaults, an optional config file and CLI overrides; return config and table.

    Raises FileNotFoundError for a missing config file and ValueError (including
    ConfigValidationError) for invalid settings.
    """
    table, file_config = None, {}
    if config_path is not None:
        table, file_config = load_explicit_file_config(config_path)
    cfg = resolve_settings(file_config, overrides, table, detect_base_image=config_path is None)
    return cfg, table


def resolve_settings(
    file_config: Dict[str, Any],
    overrides: Dict[str, Any],
    table: Optional[str],
    *,
    detect_base_image: bool = False,
) -> BuildConfig:
    """Validate already-loaded settings exactly as resolve_build_config does after loading."""
    merged = merge_configs(file_config, overrides)
    _validate_config(merged, table or BUILD_TABLE)
    origin = lambda field: _origin(field, overrides, table, file_config)
    try:
        cfg = BuildConfig(**merged)
    except ValueError as exc:
        raise ConfigValidationError(_constructor_errors(merged, origin) or [str(exc)]) from None
    if detect_base_image and "base_image" not in overrides:
        try:
            version = detect_python_version(cfg.context_dir)
        except (AttributeError, TypeError, ValueError) as exc:
            pyproject = Path(cfg.context_dir) / "pyproject.toml"
            raise ConfigValidationError(
                [f"{pyproject} [project].requires-python: cannot detect the Python version: {exc}"]
            ) from None
        cfg.base_image = f"python:{version}-slim"

    errors = semantic_errors(cfg, origin)
    if errors:
        raise ConfigValidationError(errors)
    return cfg
