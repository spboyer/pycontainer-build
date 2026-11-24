"""Declared build inputs and non-recursive configuration interpolation."""
from collections import Counter
import json
import re
from typing import Dict, Iterable, List, Optional, Union
from urllib.parse import quote

BuildArgument = Union[str, Dict[str, str]]
ARGUMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
EXPANDABLE_FIELDS = (
    "tag", "tags", "base_image", "context_dir", "workdir", "entrypoint",
    "cmd", "env", "labels", "user", "include_paths", "exclude_paths",
    "output_dir", "cache_dir", "requirements_file", "platform",
    "stop_signal", "volumes",
)


def redact_values(message: str, values: Iterable[str]) -> str:
    representations = set()
    for value in values:
        if value:
            representations.update((
                value, repr(value)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1],
                json.dumps(value)[1:-1], quote(value, safe=""),
            ))
    for value in sorted(representations, key=len, reverse=True):
        message = message.replace(value, "<redacted>")
    return message


def known_argument_values(declarations) -> List[str]:
    if not isinstance(declarations, dict):
        return []
    values = []
    for declaration in declarations.values():
        value = declaration.get("default") if isinstance(declaration, dict) else declaration
        if isinstance(value, str):
            values.append(value)
    return values


def _check_text(value: str, description: str) -> None:
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError(f"{description} contains unsupported control characters.")


def resolve_arguments(
    declarations: Dict[str, BuildArgument],
    overrides: Optional[List[str]] = None,
) -> Dict[str, str]:
    if not isinstance(declarations, dict):
        raise ValueError("build_args must be a table of declared arguments.")
    values = {}
    for name, declaration in declarations.items():
        if not isinstance(name, str) or not ARGUMENT_NAME.fullmatch(name):
            raise ValueError("Invalid build argument name; use [A-Za-z_][A-Za-z0-9_]*.")
        if isinstance(declaration, str):
            default = declaration
        elif isinstance(declaration, dict) and set(declaration) <= {"default"}:
            default = declaration.get("default")
            if default is not None and not isinstance(default, str):
                raise ValueError(f"Build argument {name} default must be a string.")
            if "default" in declaration and default is None:
                raise ValueError(f"Build argument {name} default must be a string.")
        else:
            raise ValueError(f"Build argument {name} must be a string or a table with only default.")
        if default is not None:
            _check_text(default, f"Build argument {name}")
            values[name] = default

    for item in overrides or []:
        name, separator, value = item.partition("=")
        if not separator or not ARGUMENT_NAME.fullmatch(name):
            raise ValueError("--build-arg requires KEY=VALUE with a valid argument name.")
        if name not in declarations:
            raise ValueError(f"Undeclared build argument {name}.")
        _check_text(value, f"Build argument {name}")
        values[name] = value
    missing = sorted(set(declarations) - set(values))
    if missing:
        raise ValueError(f"Missing required build argument(s): {', '.join(missing)}.")
    return values


def expand(value: str, arguments: Dict[str, str], field: str) -> str:
    _check_text(value, f"Configuration field {field}")
    result = []
    position = 0
    while position < len(value):
        if value.startswith("$$", position):
            result.append("$")
            position += 2
        elif value.startswith("${", position):
            end = value.find("}", position + 2)
            if end == -1:
                raise ValueError(f"Unclosed build argument reference in {field}.")
            name = value[position + 2:end]
            if not ARGUMENT_NAME.fullmatch(name):
                raise ValueError(f"Invalid build argument reference in {field}.")
            if name not in arguments:
                raise ValueError(f"Undeclared build argument reference {name} in {field}.")
            result.append(arguments[name])
            position = end + 1
        else:
            result.append(value[position])
            position += 1
    return "".join(result)


_NO_PREVIOUS_VALUE = object()


def expand_field(value, arguments: Dict[str, str], field: str, previous=_NO_PREVIOUS_VALUE):
    if previous is not _NO_PREVIOUS_VALUE and value == previous:
        return value
    if isinstance(value, str):
        return expand(value, arguments, field)
    if isinstance(value, list):
        existing = Counter(
            item for item in previous if isinstance(item, str)
        ) if isinstance(previous, list) else Counter()
        result = []
        for item in value:
            if isinstance(item, str) and existing[item]:
                existing[item] -= 1
                result.append(item)
            else:
                result.append(expand(item, arguments, field))
        return result
    if isinstance(value, dict):
        existing = previous if isinstance(previous, dict) else {}
        return {
            key: item if key in existing and item == existing[key] else expand(item, arguments, field)
            for key, item in value.items()
        }
    return value
