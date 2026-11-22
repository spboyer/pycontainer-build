"""Import a Dockerfile into pycontainer-build configuration (stdlib only)."""
import json
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TABLE_NAME = "tool.pycontainer"
DEFAULT_SHELL = ["/bin/sh", "-c"]
SHELL_FORM_PREFIX = DEFAULT_SHELL

# Linux generic signal numbers (amd64/arm64), matching Docker's Linux table.
LINUX_SIGNAL_NUMBERS = {
    1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 5: "SIGTRAP", 6: "SIGABRT",
    7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL", 10: "SIGUSR1", 11: "SIGSEGV", 12: "SIGUSR2",
    13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM", 16: "SIGSTKFLT", 17: "SIGCHLD",
    18: "SIGCONT", 19: "SIGSTOP", 20: "SIGTSTP", 21: "SIGTTIN", 22: "SIGTTOU",
    23: "SIGURG", 24: "SIGXCPU", 25: "SIGXFSZ", 26: "SIGVTALRM", 27: "SIGPROF",
    28: "SIGWINCH", 29: "SIGIO", 30: "SIGPWR", 31: "SIGSYS", 34: "SIGRTMIN", 64: "SIGRTMAX",
    **{34 + n: f"SIGRTMIN+{n}" for n in range(1, 16)},
    **{64 - n: f"SIGRTMAX-{n}" for n in range(1, 15)},
}
# Linux alias names that Docker accepts, mapped to their canonical SIGNAME.
SIGNAL_ALIASES = {"SIGIOT": "SIGABRT", "SIGCLD": "SIGCHLD", "SIGPOLL": "SIGIO"}

# Instructions with no pycontainer equivalent, and what to do instead.
UNSUPPORTED_ADVICE = {
    "RUN": "pycontainer does not execute commands; install dependencies on the host "
           "and use include_deps = true / requirements_file instead",
    "COPY": "files come from context_dir via include_paths/exclude_paths; "
            "copy destinations and --chown/--chmod are not supported",
    "ADD": "files come from context_dir via include_paths/exclude_paths; "
           "remote URLs, archive extraction and --chown are not supported",
    "HEALTHCHECK": "health checks are not supported; configure them in your runtime",
    "VOLUME": "only absolute container paths are imported, as image metadata",
    "SHELL": "only exec-form SHELL is applied, to later shell-form CMD/ENTRYPOINT",
    "STOPSIGNAL": "only Linux signal names or numbers are imported",
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


def validate_shell(shell: object, source: str = "shell") -> List[str]:
    """Return a copy of shell if it is a usable exec-form shell array, else raise ValueError."""
    if (
        not isinstance(shell, list)
        or not shell
        or not all(isinstance(part, str) for part in shell)
        or not shell[0].strip()
        or any("\x00" in part for part in shell)
    ):
        raise ValueError(
            f"{source} must be a non-empty JSON array of strings with a non-empty "
            f"executable first and no NUL characters, e.g. [\"/bin/bash\", \"-c\"]; got {shell!r}."
        )
    return list(shell)


def parse_shell_option(value: str) -> List[str]:
    """Parse the import --shell JSON array option."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--shell must be a JSON array of strings: {exc}.") from exc
    return validate_shell(parsed, "--shell")


def _exec_form(args: str) -> Optional[List[str]]:
    stripped = args.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list) and all(isinstance(p, str) for p in parsed):
            return parsed
    return None


def _command_list(args: str, shell: Optional[List[str]] = None) -> List[str]:
    exec_form = _exec_form(args)
    if exec_form is not None:
        return exec_form
    return list(shell or DEFAULT_SHELL) + [args.strip()]


def _import_stop_signal(value: str) -> Optional[str]:
    """Map a Dockerfile STOPSIGNAL value to a canonical SIGNAME, as Docker parses it."""
    from .config import STOP_SIGNALS

    raw = value.strip().upper()
    if raw.isdigit():
        return LINUX_SIGNAL_NUMBERS.get(int(raw))
    name = raw if raw.startswith("SIG") else "SIG" + raw
    name = SIGNAL_ALIASES.get(name, name)
    return name if name in STOP_SIGNALS else None


def _volume_paths(args: str) -> List[str]:
    exec_form = _exec_form(args)
    return exec_form if exec_form is not None else args.split()


def import_dockerfile(
    text: str,
    build_args: Optional[Dict[str, str]] = None,
    shell: Optional[List[str]] = None,
) -> ImportResult:
    """Map a Dockerfile to [tool.pycontainer] settings plus explicit warnings."""
    from .config import validate_volume

    current_shell = validate_shell(shell, "shell") if shell is not None else list(DEFAULT_SHELL)
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
    volumes: List[str] = []
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
            config[key] = _command_list(ins.args, current_shell)
            if _exec_form(ins.args) is None:
                notes.append(
                    f"Line {ins.line}: {name} shell form converted to {current_shell} + command "
                    "(text kept verbatim; the shell must exist in the target image)."
                )
                if "distroless" in str(config.get("base_image", "")).lower():
                    warnings.append(
                        f"Line {ins.line}: {name} shell form needs {current_shell[0]}, which "
                        "distroless images usually lack; use exec form instead."
                    )
        elif name == "SHELL":
            try:
                current_shell = validate_shell(_exec_form(ins.args), "SHELL")
            except ValueError:
                warnings.append(
                    f"Line {ins.line}: SHELL must be a JSON array of strings with a non-empty "
                    "executable; ignored."
                )
            else:
                warnings.append(
                    f"Line {ins.line}: SHELL {current_shell} applied only to later shell-form "
                    "CMD/ENTRYPOINT conversion; RUN is not executed, and the shell is not "
                    "recorded in the image config or inherited by images built from it."
                )
        elif name == "STOPSIGNAL":
            value = expand(ins, ins.args).strip()
            signal = _import_stop_signal(value)
            if signal is None:
                warnings.append(
                    f"Line {ins.line}: STOPSIGNAL {value!r} is not a recognized Linux signal; skipped."
                )
            else:
                config["stop_signal"] = signal
                if signal != value:
                    notes.append(f"Line {ins.line}: STOPSIGNAL {value} mapped to {signal}.")
        elif name == "VOLUME":
            paths = _volume_paths(expand(ins, ins.args))
            if not paths:
                warnings.append(f"Line {ins.line}: VOLUME has no paths; skipped.")
                continue
            for path in paths:
                candidate = path.rstrip("/") or path
                try:
                    validate_volume(candidate)
                except ValueError:
                    warnings.append(
                        f"Line {ins.line}: VOLUME {path!r} is not a normalized absolute, "
                        "non-root container path (relative or unsafe); skipped."
                    )
                    continue
                if candidate not in volumes:
                    volumes.append(candidate)
            notes.append(
                f"Line {ins.line}: VOLUME imported as image metadata only; no mount is created."
            )
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
    if volumes:
        config["volumes"] = volumes
    return ImportResult(config, warnings, notes)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


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
    kept: List[str] = []
    skipping = False
    for line in existing.splitlines():
        header = re.match(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", line)
        if header:
            table = header.group(1).replace('"', "").replace(" ", "")
            skipping = table == TABLE_NAME or table.startswith(TABLE_NAME + ".")
        if not skipping:
            kept.append(line)
    base = "\n".join(kept).rstrip()
    merged = (base + "\n\n" if base else "") + snippet
    tomllib.loads(merged)
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
    shell: Optional[List[str]] = None,
) -> Tuple[str, int]:
    """Run the import; return (message for stdout, exit code)."""
    if not dockerfile.is_file():
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile}")
    result = import_dockerfile(
        dockerfile.read_text(encoding="utf-8"), build_args, shell
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
