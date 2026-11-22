"""Configuration file loading and validation."""
import types
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union, get_args, get_origin, get_type_hints
from .config import BuildConfig


BUILD_TABLE = "[build]"
TOOL_TABLE = "[tool.pycontainer]"
PLUGIN_ONLY_HINTS = {
    "push": "push is a Poetry plugin setting; use 'pycontainer build --push' for direct builds",
}


def _load_config_table(path: Path, required: bool = False) -> Tuple[str, Dict[str, Any]]:
    """Return the label and contents of the single configuration table in path."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, 'rb') as f:
        data=tomllib.load(f)

    found = []
    if 'build' in data:
        if not isinstance(data['build'], dict):
            raise ValueError("The [build] configuration must be a TOML table.")
        found.append((BUILD_TABLE, data['build']))
    if 'tool' in data:
        if not isinstance(data['tool'], dict):
            raise ValueError("The [tool] configuration must be a TOML table.")
        if 'pycontainer' in data['tool']:
            if not isinstance(data['tool']['pycontainer'], dict):
                raise ValueError("The [tool.pycontainer] configuration must be a TOML table.")
            found.append((TOOL_TABLE, data['tool']['pycontainer']))

    if len(found) > 1:
        raise ValueError(
            f"{path} defines both [build] and [tool.pycontainer]; keep exactly one "
            "pycontainer configuration table."
        )
    if not found and required:
        raise ValueError(
            f"Config file {path} defines neither [build] nor [tool.pycontainer]; "
            "add one of these tables (check the table name for typos)."
        )
    return found[0] if found else (BUILD_TABLE, {})


def load_config_file(path: Path) -> Dict[str, Any]:
    """Load the [build] or [tool.pycontainer] table from a TOML config file."""
    return _load_config_table(path)[1]


def _normalize_config_keys(config: Dict[str, Any], source: str) -> Dict[str, Any]:
    normalized = {}
    original_keys = {}

    for key, value in config.items():
        normalized_key = key.replace('-', '_')
        if normalized_key in normalized:
            raise ValueError(
                f"{source} contains both {original_keys[normalized_key]!r} and "
                f"{key!r}, which normalize to the same setting "
                f"{normalized_key!r}."
            )
        normalized[normalized_key] = value
        original_keys[normalized_key] = key

    return normalized


def _matches_type(value: Any, expected_type: Any) -> bool:
    origin = get_origin(expected_type)
    if origin in (Union, types.UnionType):
        return any(_matches_type(value, option) for option in get_args(expected_type))
    if origin is list:
        item_type = get_args(expected_type)[0]
        return isinstance(value, list) and all(
            _matches_type(item, item_type) for item in value
        )
    if origin is dict:
        key_type, value_type = get_args(expected_type)
        return isinstance(value, dict) and all(
            _matches_type(key, key_type) and _matches_type(item, value_type)
            for key, item in value.items()
        )
    if expected_type is bool:
        return isinstance(value, bool)
    if expected_type is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected_type)


def _format_type(expected_type: Any) -> str:
    origin = get_origin(expected_type)
    if origin in (Union, types.UnionType):
        return " or ".join(_format_type(option) for option in get_args(expected_type))
    if origin is list:
        return f"list[{_format_type(get_args(expected_type)[0])}]"
    if origin is dict:
        key_type, value_type = get_args(expected_type)
        return f"dict[{_format_type(key_type)}, {_format_type(value_type)}]"
    if expected_type is type(None):
        return "None"
    return getattr(expected_type, "__name__", str(expected_type))


def _validate_config(config: Dict[str, Any], table: str = BUILD_TABLE) -> None:
    valid_fields = {field.name for field in fields(BuildConfig)}
    unknown_fields = sorted(set(config) - valid_fields)
    if unknown_fields:
        supported = ", ".join(sorted(valid_fields))
        unknown = ", ".join(repr(key) for key in unknown_fields)
        hints = "".join(
            f" ({PLUGIN_ONLY_HINTS[key]}.)" for key in unknown_fields if key in PLUGIN_ONLY_HINTS
        )
        raise ValueError(
            f"Unknown {table} setting(s): {unknown}.{hints} Supported settings: {supported}."
        )

    type_hints = get_type_hints(BuildConfig)
    for key, value in config.items():
        if key == "validate":
            nested_table = f"{table[:-1]}.validate]"
            if not isinstance(value, dict):
                raise ValueError(f"Invalid value for {nested_table}: expected a table.")
            from .image_validation import SIZE_THRESHOLDS, parse_size_threshold, validate_ignores
            unknown = sorted(set(value) - {"ignore", *SIZE_THRESHOLDS})
            if unknown:
                raise ValueError(f"Unknown {nested_table} setting(s): {', '.join(unknown)}.")
            ignores = value.get("ignore", [])
            if not isinstance(ignores, list) or any(not isinstance(item, str) for item in ignores):
                raise ValueError(f"Invalid value for {nested_table}.ignore: expected list[str].")
            try:
                validate_ignores(ignores)
            except ValueError as exc:
                raise ValueError(f"{nested_table}.ignore: {exc}") from None
            for name in SIZE_THRESHOLDS:
                if name in value and isinstance(value[name], str):
                    raise ValueError(f"Invalid value for {nested_table}.{name}: expected a number.")
                if name in value:
                    parse_size_threshold(value[name], f"{nested_table}.{name}")
            continue
        expected_type = type_hints[key]
        if not _matches_type(value, expected_type):
            raise ValueError(
                f"Invalid value for {table}.{key}: expected "
                f"{_format_type(expected_type)}, got {type(value).__name__}."
            )


def merge_configs(file_config: Dict[str, Any], cli_args: Dict[str, Any]) -> Dict[str, Any]:
    """Merge file config with CLI args (CLI takes precedence)."""
    merged = _normalize_config_keys(file_config, "Config file")
    cli_args = _normalize_config_keys(cli_args, "CLI options")

    for key, value in cli_args.items():
        if value is not None:
            if (
                key in ('env', 'labels')
                and isinstance(value, dict)
                and isinstance(merged.get(key), dict)
            ):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value

    return merged


def load_validated_file_config(path: Path) -> Tuple[str, Dict[str, Any]]:
    """Return the table label and normalized, type-checked settings from an explicit path.

    An explicitly named file must define [build] or [tool.pycontainer]; a missing
    or misspelled table is an error rather than a silent fall-back to defaults.
    """
    table, file_config = _load_config_table(path, required=True)
    file_config = _normalize_config_keys(file_config, f"Config file {table}")
    _validate_config(file_config, table)
    return table, file_config


def config_from_file(path: Path, cli_overrides: Optional[Dict]=None) -> BuildConfig:
    """Create a validated BuildConfig from file values and explicit CLI overrides."""
    _, file_config = load_validated_file_config(path)
    config = merge_configs(file_config, cli_overrides or {})
    _validate_config(config)
    return BuildConfig(**config)
