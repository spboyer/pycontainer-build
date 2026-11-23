"""Import a Dockerfile into pycontainer-build configuration (stdlib only)."""
import json
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TABLE_NAME = "tool.pycontainer"
SHELL_FORM_PREFIX = ["/bin/sh", "-c"]

# Instructions with no pycontainer equivalent, and what to do instead.
UNSUPPORTED_ADVICE = {
    "RUN": "pycontainer does not execute commands; install dependencies on the host "
           "and use include_deps = true / requirements_file instead",
    "COPY": "files come from context_dir via include_paths/exclude_paths; "
            "copy destinations and --chown/--chmod are not supported",
    "ADD": "files come from context_dir via include_paths/exclude_paths; "
           "remote URLs, archive extraction and --chown are not supported",
    "HEALTHCHECK": "health checks are not supported; configure them in your runtime",
    "VOLUME": "volumes are not supported; declare them in your runtime",
    "SHELL": "custom shells are not supported",
    "STOPSIGNAL": "stop signals are not supported",
    "ONBUILD": "ONBUILD triggers are not supported",
    "MAINTAINER": "deprecated; add a maintainer entry under [tool.pycontainer.labels] if needed",
}


@dataclass
class Instruction:
    name: str
    args: str
    line: int


@dataclass
class Stage:
    base: str
    name: Optional[str]
    platform: Optional[str]
    line: int
    instructions: List[Instruction] = field(default_factory=list)


@dataclass
class ParsedDockerfile:
    directives: Dict[str, str]
    global_args: List[Instruction]
    stages: List[Stage]


@dataclass
class ImportResult:
    config: Dict[str, object]
    warnings: List[str]
    notes: List[str]


def parse_dockerfile(text: str) -> ParsedDockerfile:
    """Parse Dockerfile text into stages, honoring directives and continuations."""
    lines = text.lstrip("\ufeff").splitlines()
    directives: Dict[str, str] = {}
    idx = 0
    while idx < len(lines):
        m = re.match(r"^\s*#\s*([A-Za-z]+)\s*=\s*(\S+)\s*$", lines[idx])
        if not m:
            break
        directives[m.group(1).lower()] = m.group(2)
        idx += 1
    escape = directives.get("escape", "\\")

    instructions: List[Instruction] = []
    n = len(lines)
    while idx < n:
        raw = lines[idx]
        start = idx + 1
        idx += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        buf = raw.rstrip()
        while buf.endswith(escape):
            buf = buf[:-1]
            while idx < n and (not lines[idx].strip() or lines[idx].lstrip().startswith("#")):
                idx += 1
            if idx >= n:
                break
            buf += lines[idx].rstrip()
            idx += 1
        parts = buf.strip().split(None, 1)
        name = parts[0].upper()
        args = parts[1] if len(parts) > 1 else ""
        heredocs = re.findall(r"<<-?\s*[\"']?(\w+)[\"']?", args)
        for delim in heredocs:
            while idx < n:
                done = lines[idx].strip() == delim
                idx += 1
                if done:
                    break
        instructions.append(Instruction(name, args, start))

    global_args: List[Instruction] = []
    stages: List[Stage] = []
    for ins in instructions:
        if ins.name == "FROM":
            stages.append(_parse_from(ins))
        elif stages:
            stages[-1].instructions.append(ins)
        elif ins.name == "ARG":
            global_args.append(ins)
        else:
            raise ValueError(
                f"Line {ins.line}: {ins.name} appears before the first FROM instruction."
            )
    if not stages:
        raise ValueError("Dockerfile contains no FROM instruction.")
    return ParsedDockerfile(directives, global_args, stages)


def _parse_from(ins: Instruction) -> Stage:
    tokens = ins.args.split()
    platform = None
    while tokens and tokens[0].startswith("--"):
        flag = tokens.pop(0)
        if flag.startswith("--platform="):
            platform = flag.split("=", 1)[1]
    if not tokens:
        raise ValueError(f"Line {ins.line}: FROM requires an image.")
    name = None
    if len(tokens) >= 3 and tokens[1].upper() == "AS":
        name = tokens[2]
    return Stage(tokens[0], name, platform, ins.line)


def substitute(value: str, variables: Dict[str, str]) -> Tuple[str, List[str]]:
    """Expand $VAR, ${VAR}, ${VAR:-default} and ${VAR:+alt}; return unresolved names."""
    missing: List[str] = []

    def repl(m: re.Match) -> str:
        if m.group(0).startswith("\\"):
            return m.group(0)[1:]
        name = m.group("name") or m.group("braced")
        op, word = m.group("op"), m.group("word") or ""
        val = variables.get(name)
        if op in (":-", "-"):
            return val if val else word
        if op in (":+", "+"):
            return word if val else ""
        if val is None:
            missing.append(name)
            return ""
        return val

    pattern = re.compile(
        r"\\\$|\$(?:(?P<name>[A-Za-z_]\w*)|\{(?P<braced>[A-Za-z_]\w*)"
        r"(?:(?P<op>:-|:\+|-|\+)(?P<word>[^}]*))?\})"
    )
    return pattern.sub(repl, value), missing


def _split_words(value: str) -> List[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


def _parse_key_values(args: str) -> List[Tuple[str, Optional[str]]]:
    """Parse `K=V K2="v 2"` or legacy `K value` forms."""
    words = _split_words(args)
    if words and "=" not in words[0]:
        rest = args.split(None, 1)
        return [(rest[0], rest[1].strip() if len(rest) > 1 else "")]
    pairs: List[Tuple[str, Optional[str]]] = []
    for word in words:
        key, sep, val = word.partition("=")
        pairs.append((key, val if sep else None))
    return pairs


def _command_list(args: str) -> List[str]:
    stripped = args.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list) and all(isinstance(p, str) for p in parsed):
                return parsed
        except json.JSONDecodeError:
            pass
    return SHELL_FORM_PREFIX + [stripped]


def import_dockerfile(
    text: str, build_args: Optional[Dict[str, str]] = None
) -> ImportResult:
    """Map a Dockerfile to [tool.pycontainer] settings plus explicit warnings."""
    parsed = parse_dockerfile(text)
    overrides = dict(build_args or {})
    warnings: List[str] = []
    notes: List[str] = []
    config: Dict[str, object] = {}

    if "syntax" in parsed.directives:
        notes.append(f"Parser directive syntax={parsed.directives['syntax']} was read and ignored.")

    global_vars: Dict[str, str] = {}
    for ins in parsed.global_args:
        for key, val in _parse_key_values(ins.args):
            global_vars[key] = overrides.get(key, val if val is not None else "")
            if val is None and key not in overrides:
                warnings.append(f"Line {ins.line}: ARG {key} has no default; treated as empty.")

    final = parsed.stages[-1]
    if len(parsed.stages) > 1:
        names = ", ".join(s.name or f"#{i}" for i, s in enumerate(parsed.stages[:-1]))
        warnings.append(
            f"Multi-stage build: only the final stage (line {final.line}) is imported; "
            f"earlier stages ({names}) are not built or copied from."
        )
        for stage in parsed.stages[:-1]:
            for ins in stage.instructions:
                if ins.name in UNSUPPORTED_ADVICE:
                    warnings.append(
                        f"Line {ins.line}: {ins.name} in non-final stage ignored."
                    )

    base, missing = substitute(final.base, global_vars)
    for name in missing:
        warnings.append(f"Line {final.line}: FROM references undefined ARG {name}.")
    earlier = {s.name for s in parsed.stages[:-1] if s.name}
    if base in earlier:
        warnings.append(
            f"Line {final.line}: FROM {base} references an earlier stage; "
            "set base_image manually to a registry image."
        )
    elif base.lower() == "scratch":
        warnings.append(
            f"Line {final.line}: FROM scratch is not supported; "
            "base_image must be a registry image."
        )
    elif base:
        config["base_image"] = base
    if final.platform:
        platform, _ = substitute(final.platform, global_vars)
        config["platform"] = platform

    variables = dict(global_vars)
    stage_args: Dict[str, str] = {}
    env: Dict[str, str] = {}
    labels: Dict[str, str] = {}
    ports: List[int] = []
    workdir = "/"
    workdir_set = False

    def expand(ins: Instruction, value: str) -> str:
        out, miss = substitute(value, variables)
        for name in miss:
            warnings.append(f"Line {ins.line}: {ins.name} references undefined variable {name}.")
        return out

    for ins in final.instructions:
        name = ins.name
        if name == "ARG":
            for key, val in _parse_key_values(ins.args):
                if key in overrides:
                    stage_args[key] = overrides[key]
                elif val is not None:
                    stage_args[key] = substitute(val, variables)[0]
                elif key in global_vars:
                    stage_args[key] = global_vars[key]
                else:
                    stage_args[key] = ""
                    warnings.append(f"Line {ins.line}: ARG {key} has no default; treated as empty.")
                variables[key] = stage_args[key]
        elif name == "ENV":
            for key, val in _parse_key_values(expand(ins, ins.args)):
                env[key] = val or ""
                variables[key] = env[key]
        elif name == "LABEL":
            for key, val in _parse_key_values(expand(ins, ins.args)):
                labels[key] = val or ""
        elif name == "WORKDIR":
            path = expand(ins, ins.args).strip()
            if not path.startswith("/"):
                path = f"{workdir.rstrip('/')}/{path}"
            workdir, workdir_set = path, True
        elif name == "EXPOSE":
            for token in expand(ins, ins.args).split():
                port, _, proto = token.partition("/")
                if proto.lower() == "udp":
                    warnings.append(f"Line {ins.line}: EXPOSE {token} is UDP; only TCP ports are imported.")
                elif re.fullmatch(r"\d+", port):
                    if int(port) not in ports:
                        ports.append(int(port))
                else:
                    warnings.append(f"Line {ins.line}: EXPOSE {token} is not a numeric port; skipped.")
        elif name in ("CMD", "ENTRYPOINT"):
            key = "cmd" if name == "CMD" else "entrypoint"
            config[key] = _command_list(ins.args)
            if not ins.args.strip().startswith("["):
                notes.append(f"Line {ins.line}: {name} shell form converted to {SHELL_FORM_PREFIX} + command.")
        elif name == "USER":
            config["user"] = expand(ins, ins.args).strip()
        elif name == "MAINTAINER":
            warnings.append(f"Line {ins.line}: MAINTAINER unsupported: {UNSUPPORTED_ADVICE[name]}.")
        elif name in UNSUPPORTED_ADVICE:
            detail = UNSUPPORTED_ADVICE[name]
            if name in ("COPY", "ADD") and re.search(r"--from=", ins.args):
                detail = "copying from another stage or image is not supported; " + detail
            warnings.append(f"Line {ins.line}: {name} unsupported: {detail}.")
        else:
            warnings.append(f"Line {ins.line}: unknown instruction {name} ignored.")

    if workdir_set:
        config["workdir"] = workdir
    if env:
        config["env"] = env
    if labels:
        config["labels"] = labels
    if ports:
        config["exposed_ports"] = ports
    return ImportResult(config, warnings, notes)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    # Imported Dockerfile values are already resolved; preserve runtime dollars.
    return json.dumps(str(value).replace("$", "$$"), ensure_ascii=False)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def render_toml(config: Dict[str, object]) -> str:
    """Render config as [tool.pycontainer] with env/labels sub-tables."""
    scalars = {k: v for k, v in config.items() if not isinstance(v, dict)}
    out = [f"[{TABLE_NAME}]"]
    out += [f"{k} = {_toml_value(v)}" for k, v in scalars.items()]
    for table in ("env", "labels"):
        values = config.get(table)
        if isinstance(values, dict) and values:
            out += ["", f"[{TABLE_NAME}.{table}]"]
            out += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in values.items()]
    return "\n".join(out) + "\n"


def merge_into_pyproject(existing: str, snippet: str) -> str:
    """Replace any [tool.pycontainer*] tables in existing text with snippet."""
    # Keep float tokens comparable, including NaN, when checking unrelated values.
    original = tomllib.loads(existing, parse_float=lambda value: (value,))
    kept: List[str] = []
    trailing: List[str] = []
    skipping = False
    lines = existing.split("\n")
    segment_start = 0

    def complete_prefix(start: int, end: int) -> bool:
        prefix = "\n".join(lines[start:end]) + ("\n" if end > start else "")
        try:
            tomllib.loads(prefix)
        except tomllib.TOMLDecodeError:
            return False
        return True

    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            try:
                header = tomllib.loads(line.rstrip("\r"))
            except tomllib.TOMLDecodeError:
                header = None
            if header is not None:
                tool = header.get("tool")
                is_target = isinstance(tool, dict) and "pycontainer" in tool
                if is_target != skipping:
                    # A header-like line inside a multiline value has an incomplete prefix.
                    is_header = complete_prefix(segment_start, index)
                    if not is_header and segment_start:
                        # Array-of-tables fragments may need earlier context.
                        is_header = complete_prefix(0, index)
                    if is_header:
                        if skipping:
                            kept.extend(trailing)
                        trailing.clear()
                        skipping = is_target
                        segment_start = index
        if skipping:
            if not line.strip() or line.lstrip().startswith("#"):
                trailing.append(line)
            else:
                trailing.clear()
        else:
            kept.append(line)
    if skipping:
        kept.extend(trailing)
    base = "\n".join(kept).rstrip()
    merged = (base + "\n\n" if base else "") + snippet
    try:
        updated = tomllib.loads(merged, parse_float=lambda value: (value,))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Cannot safely merge [tool.pycontainer]: {exc}") from exc
    for data in (original, updated):
        tool = data.get("tool")
        if isinstance(tool, dict):
            tool.pop("pycontainer", None)
            if not tool:
                data.pop("tool")
    if original != updated:
        raise ValueError("Cannot safely merge [tool.pycontainer] without changing unrelated TOML.")
    return merged


def format_report(result: ImportResult, source: str) -> str:
    lines = [f"Migration report for {source}", ""]
    imported = ", ".join(result.config) or "(nothing)"
    lines.append(f"Imported settings: {imported}")
    for note in result.notes:
        lines.append(f"  note: {note}")
    if result.warnings:
        lines += ["", f"Warnings ({len(result.warnings)}):"]
        lines += [f"  WARNING: {w}" for w in result.warnings]
    else:
        lines += ["", "No warnings."]
    return "\n".join(lines)


def run_import(
    dockerfile: Path,
    output: Optional[Path],
    force: bool = False,
    build_args: Optional[Dict[str, str]] = None,
) -> Tuple[str, int]:
    """Run the import; return (message for stdout, exit code)."""
    if not dockerfile.is_file():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")
    result = import_dockerfile(
        dockerfile.read_text(encoding="utf-8"), build_args
    )
    snippet = render_toml(result.config)
    report = format_report(result, str(dockerfile))

    if output is None:
        return f"{report}\n\n{snippet}", 0
    if output.exists() and not force:
        return (
            f"{report}\n\nRefusing to overwrite existing {output}; "
            f"use --force to merge, or copy this snippet:\n\n{snippet}",
            1,
        )
    if output.exists():
        existing = output.read_text(encoding="utf-8")
        if "build" in tomllib.loads(existing):
            raise ValueError(
                f"{output} already defines [build]; refusing to add [tool.pycontainer] "
                "because a config file must use exactly one pycontainer table. "
                "Move the settings by hand or write to a new file."
            )
        content = merge_into_pyproject(existing, snippet)
    else:
        content = snippet
    output.write_text(content, encoding="utf-8")
    return f"{report}\n\nWrote {output}", 0
