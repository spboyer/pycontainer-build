"""Configuration file loading and validation."""
import types
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict, Optional, Union, get_args, get_origin, get_type_hints
from .config import BuildConfig


def load_config_file(path: Path) -> Dict[str, Any]:
    """Load configuration from pycontainer.toml file."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, 'rb') as f:
        data=tomllib.load(f)

    build_config = data.get('build', {})
    if not isinstance(build_config, dict):
        raise ValueError("The [build] configuration must be a TOML table.")

    return build_config


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


def _validate_config(config: Dict[str, Any]) -> None:
    valid_fields = {field.name for field in fields(BuildConfig) if not field.name.startswith("_")}
    unknown_fields = sorted(set(config) - valid_fields)
    if unknown_fields:
        supported = ", ".join(sorted(valid_fields))
        unknown = ", ".join(repr(key) for key in unknown_fields)
        raise ValueError(
            f"Unknown [build] setting(s): {unknown}. Supported settings: {supported}."
        )

    type_hints = get_type_hints(BuildConfig)
    for key, value in config.items():
        expected_type = type_hints[key]
        if not _matches_type(value, expected_type):
            raise ValueError(
                f"Invalid value for [build].{key}: expected "
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


def config_from_file(path: Path, cli_overrides: Optional[Dict]=None) -> BuildConfig:
    """Create a validated BuildConfig from file values and explicit CLI overrides."""
    config = merge_configs(load_config_file(path), cli_overrides or {})
    _validate_config(config)
    return BuildConfig(**config)
