"""Conditional context file rules evaluated from declared inputs only."""
import re
from typing import Dict, List, Tuple, Union

from .fs_utils import relative_context_path
from .oci import parse_platform

FileRule = Dict[str, Union[str, List[str]]]
RULE_KEYS = {"when", "include", "exclude"}
TARGET_NAMES = ("target.os", "target.arch")
_TOKEN = re.compile(
    r'\s*(?:(?P<op>==|!=)|(?P<string>"(?:[^"\\]|\\["\\])*")|'
    r'(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?))'
)


class FileRuleError(ValueError):
    """An invalid conditional file rule or expression."""


def _tokenize(expression: str, where: str) -> List[Tuple[str, str]]:
    tokens = []
    position = 0
    while position < len(expression):
        if expression[position:].strip() == "":
            break
        match = _TOKEN.match(expression, position)
        if not match:
            raise FileRuleError(f"{where} has an invalid token at character {position + 1}.")
        kind = match.lastgroup
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


def _unquote(literal: str) -> str:
    return re.sub(r'\\(["\\])', r"\1", literal[1:-1])


def evaluate_condition(expression: str, values: Dict[str, str], where: str) -> bool:
    """Evaluate ``NAME (==|!=) "literal" (and ...)*`` without eval or environment."""
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in expression):
        raise FileRuleError(f"{where} contains unsupported control characters.")
    tokens = _tokenize(expression, where)
    if not tokens:
        raise FileRuleError(f"{where} is empty; use NAME == \"value\".")
    result = True
    index = 0
    while True:
        if index + 3 > len(tokens) or [kind for kind, _ in tokens[index:index + 3]] != [
            "name", "op", "string"
        ]:
            raise FileRuleError(
                f"{where} is malformed; expected NAME == \"value\" or NAME != \"value\" "
                "joined by 'and'."
            )
        name, operator, literal = (value for _, value in tokens[index:index + 3])
        if name not in values:
            raise FileRuleError(
                f"{where} references unknown name {name}; use a declared build "
                f"argument or one of: {', '.join(TARGET_NAMES)}."
            )
        matched = values[name] == _unquote(literal)
        result = result and (matched if operator == "==" else not matched)
        index += 3
        if index == len(tokens):
            return result
        if tokens[index] != ("name", "and") or index + 1 == len(tokens):
            raise FileRuleError(f"{where} is malformed; join comparisons with 'and'.")
        index += 1


def _rule_globs(value, where: str) -> List[str]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items or not all(isinstance(item, str) for item in items):
        raise FileRuleError(f"{where} must be a non-empty string or list of strings.")
    globs = []
    for item in items:
        if not item.strip() or any(ord(char) < 32 or ord(char) == 127 for char in item):
            raise FileRuleError(f"{where} contains an empty pattern or control characters.")
        try:
            globs.append(relative_context_path(item).as_posix())
        except ValueError:
            raise FileRuleError(
                f"{where} pattern {item!r} must be relative and stay inside the context."
            ) from None
    return globs


def evaluate_file_rules(
    rules: List[FileRule], arguments: Dict[str, str], platform: str
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Return active (include, exclude) context globs for resolved inputs."""
    if not isinstance(rules, list):
        raise FileRuleError("file_rules must be a list of tables.")
    if rules and isinstance(platform, str) and "," in platform:
        raise FileRuleError(
            "file_rules require a single target platform; comma-separated targets "
            "are unsupported until rules and application layers are evaluated per target."
        )
    os_name, arch = parse_platform(platform)
    values = {**arguments, "target.os": os_name, "target.arch": arch}
    includes, excludes = [], []
    for number, rule in enumerate(rules, 1):
        where = f"file_rules[{number}]"
        if not isinstance(rule, dict):
            raise FileRuleError(f"{where} must be a table.")
        unknown = sorted(map(str, set(rule) - RULE_KEYS))
        if unknown:
            raise FileRuleError(f"{where} has unknown key(s): {', '.join(unknown)}.")
        if ("include" in rule) == ("exclude" in rule):
            raise FileRuleError(f"{where} must define exactly one of include or exclude.")
        if not isinstance(rule.get("when"), str):
            raise FileRuleError(f"{where}.when must be a condition string.")
        kind = "include" if "include" in rule else "exclude"
        globs = _rule_globs(rule[kind], f"{where}.{kind}")
        if evaluate_condition(rule["when"], values, f"{where}.when"):
            (includes if kind == "include" else excludes).extend(globs)
    return tuple(dict.fromkeys(includes)), tuple(dict.fromkeys(excludes))
