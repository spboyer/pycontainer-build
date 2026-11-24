"""Opt-in runtime security settings rendered as separate deployment fragments.

Image configuration cannot enforce Linux security controls, so these settings
never change the image. They produce a Kubernetes container ``securityContext``
and an OCI runtime-spec v1.3.0 configuration fragment that a compatible
orchestrator or runtime must apply.
"""
import copy
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

SECURITY_DIR = "security"
KUBERNETES_FRAGMENT = "kubernetes-security-context.json"
OCI_FRAGMENT = "oci-runtime-config.fragment.json"
FRAGMENT_FILES = (KUBERNETES_FRAGMENT, OCI_FRAGMENT)
OCI_RUNTIME_SPEC_VERSION = "1.3.0"
MAX_SECCOMP_PROFILE_BYTES = 1024 * 1024

# Linux capabilities in bit order (capability.h, Linux 5.9+).
LINUX_CAPABILITIES = (
    "CHOWN", "DAC_OVERRIDE", "DAC_READ_SEARCH", "FOWNER", "FSETID", "KILL",
    "SETGID", "SETUID", "SETPCAP", "LINUX_IMMUTABLE", "NET_BIND_SERVICE",
    "NET_BROADCAST", "NET_ADMIN", "NET_RAW", "IPC_LOCK", "IPC_OWNER",
    "SYS_MODULE", "SYS_RAWIO", "SYS_CHROOT", "SYS_PTRACE", "SYS_PACCT",
    "SYS_ADMIN", "SYS_BOOT", "SYS_NICE", "SYS_RESOURCE", "SYS_TIME",
    "SYS_TTY_CONFIG", "MKNOD", "LEASE", "AUDIT_WRITE", "AUDIT_CONTROL",
    "SETFCAP", "MAC_OVERRIDE", "MAC_ADMIN", "SYSLOG", "WAKE_ALARM",
    "BLOCK_SUSPEND", "AUDIT_READ", "PERFMON", "BPF", "CHECKPOINT_RESTORE",
)
OCI_CAPABILITY_SETS = ("bounding", "effective", "permitted", "inheritable", "ambient")
KUBERNETES_PROFILE_TYPES = ("RuntimeDefault", "Localhost")

SECCOMP_ACTIONS = (
    "SCMP_ACT_KILL", "SCMP_ACT_KILL_PROCESS", "SCMP_ACT_KILL_THREAD",
    "SCMP_ACT_TRAP", "SCMP_ACT_ERRNO", "SCMP_ACT_TRACE", "SCMP_ACT_ALLOW",
    "SCMP_ACT_LOG",
)
SECCOMP_ERRNO_ACTIONS = ("SCMP_ACT_ERRNO", "SCMP_ACT_TRACE")
SECCOMP_ARCHITECTURES = (
    "SCMP_ARCH_X86", "SCMP_ARCH_X86_64", "SCMP_ARCH_X32", "SCMP_ARCH_ARM",
    "SCMP_ARCH_AARCH64", "SCMP_ARCH_LOONGARCH64", "SCMP_ARCH_M68K",
    "SCMP_ARCH_MIPS", "SCMP_ARCH_MIPS64", "SCMP_ARCH_MIPS64N32",
    "SCMP_ARCH_MIPSEL", "SCMP_ARCH_MIPSEL64", "SCMP_ARCH_MIPSEL64N32",
    "SCMP_ARCH_PPC", "SCMP_ARCH_PPC64", "SCMP_ARCH_PPC64LE", "SCMP_ARCH_S390",
    "SCMP_ARCH_S390X", "SCMP_ARCH_SH", "SCMP_ARCH_SHEB", "SCMP_ARCH_PARISC",
    "SCMP_ARCH_PARISC64", "SCMP_ARCH_RISCV64",
)
SECCOMP_FLAGS = (
    "SECCOMP_FILTER_FLAG_TSYNC", "SECCOMP_FILTER_FLAG_LOG",
    "SECCOMP_FILTER_FLAG_SPEC_ALLOW", "SECCOMP_FILTER_FLAG_WAIT_KILLABLE_RECV",
)
SECCOMP_OPERATORS = (
    "SCMP_CMP_NE", "SCMP_CMP_LT", "SCMP_CMP_LE", "SCMP_CMP_EQ",
    "SCMP_CMP_GE", "SCMP_CMP_GT", "SCMP_CMP_MASKED_EQ",
)
UNSUPPORTED_SECCOMP_FIELDS = {
    "listenerPath": "seccomp user notification requires a runtime agent and is not supported",
    "listenerMetadata": "seccomp user notification requires a runtime agent and is not supported",
}
UINT32_MAX = 2**32 - 1
UINT64_MAX = 2**64 - 1

_NO_SPACE_OR_CONTROL = re.compile(r"^[^\s\x00-\x1f\x7f]+$")
_SELINUX_COMPONENT = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _fail(path: str, message: str) -> None:
    raise ValueError(f"Invalid security setting {path}: {message}")


def _normalize_table(value: Any, path: str, allowed: tuple) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, f"expected a table, got {type(value).__name__}")
    normalized: Dict[str, Any] = {}
    originals: Dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            _fail(path, f"keys must be strings, got {key!r}")
        name = key.replace("-", "_")
        if name in normalized:
            _fail(path, f"contains both {originals[name]!r} and {key!r}")
        normalized[name] = item
        originals[name] = key
    unknown = sorted(set(normalized) - set(allowed))
    if unknown:
        _fail(
            path,
            f"unknown key(s) {', '.join(map(repr, unknown))}; "
            f"supported keys: {', '.join(allowed)}",
        )
    if not normalized:
        _fail(path, "must not be empty")
    return normalized


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(path, "expected a non-empty string")
    if not _NO_SPACE_OR_CONTROL.match(value):
        _fail(path, "must not contain whitespace or control characters")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, f"expected a boolean, got {type(value).__name__}")
    return value


def _capability_list(value: Any, path: str, *, prefixed: bool, allow_all: bool) -> List[str]:
    if not isinstance(value, list):
        _fail(path, f"expected a list of capability names, got {type(value).__name__}")
    seen = set()
    for item in value:
        if not isinstance(item, str):
            _fail(path, f"capability names must be strings, got {item!r}")
        if item in seen:
            _fail(path, f"duplicate capability {item!r}")
        seen.add(item)
        if item == "ALL":
            if not allow_all:
                _fail(path, "'ALL' is only supported in kubernetes capabilities.drop")
            continue
        if prefixed:
            if not item.startswith("CAP_") or item[4:] not in LINUX_CAPABILITIES:
                _fail(path, f"unknown capability {item!r}; use OCI names such as 'CAP_NET_BIND_SERVICE'")
        else:
            if item.startswith("CAP_"):
                _fail(path, f"{item!r} uses the OCI 'CAP_' prefix; Kubernetes expects {item[4:]!r}")
            if item not in LINUX_CAPABILITIES:
                _fail(path, f"unknown capability {item!r}")
    return list(value)


def _kubernetes_profile(value: Any, path: str, kind: str) -> Dict[str, str]:
    profile = _normalize_table(value, path, ("type", "localhost_profile"))
    profile_type = profile.get("type")
    if profile_type not in KUBERNETES_PROFILE_TYPES:
        _fail(f"{path}.type", f"expected one of {', '.join(KUBERNETES_PROFILE_TYPES)}, got {profile_type!r}")
    local = profile.get("localhost_profile")
    if profile_type != "Localhost":
        if local is not None:
            _fail(f"{path}.localhost_profile", "is only valid when type is 'Localhost'")
        return {"type": profile_type}
    if local is None:
        _fail(f"{path}.localhost_profile", "is required when type is 'Localhost'")
    local = _string(local, f"{path}.localhost_profile")
    if kind == "seccomp":
        parts = PurePosixPath(local).parts
        if "\\" in local or local.startswith("/") or ".." in parts:
            _fail(
                f"{path}.localhost_profile",
                "must be a relative path below the kubelet seccomp directory without '..'",
            )
    return {"type": profile_type, "localhost_profile": local}


def _selinux_component(value: Any, path: str) -> str:
    value = _string(value, path)
    if not _SELINUX_COMPONENT.match(value):
        _fail(path, "may contain only letters, digits, '_', '.', and '-'")
    return value


def _selinux_level(value: Any, path: str) -> str:
    value = _string(value, path)
    if value.startswith(":") or value.endswith(":") or "::" in value:
        _fail(path, "is not a valid MLS/MCS level")
    return value


def _selinux_context(value: Any, path: str) -> str:
    value = _string(value, path)
    parts = value.split(":", 3)
    if len(parts) < 3:
        _fail(path, "expected an SELinux context 'user:role:type[:level]'")
    for component in parts[:3]:
        _selinux_component(component, path)
    if len(parts) == 4:
        _selinux_level(parts[3], path)
    return value


def _validate_kubernetes(value: Any) -> Dict[str, Any]:
    path = "security.kubernetes"
    table = _normalize_table(
        value,
        path,
        ("capabilities", "seccomp", "apparmor", "selinux", "read_only_root_filesystem"),
    )
    result: Dict[str, Any] = {}
    if "capabilities" in table:
        caps = _normalize_table(table["capabilities"], f"{path}.capabilities", ("add", "drop"))
        add = _capability_list(caps.get("add", []), f"{path}.capabilities.add", prefixed=False, allow_all=False)
        drop = _capability_list(caps.get("drop", []), f"{path}.capabilities.drop", prefixed=False, allow_all=True)
        if not add and not drop:
            _fail(f"{path}.capabilities", "must add or drop at least one capability")
        overlap = sorted(set(add) & set(drop))
        if overlap:
            _fail(f"{path}.capabilities", f"capabilities both added and dropped: {', '.join(overlap)}")
        result["capabilities"] = {
            key: names for key, names in (("add", add), ("drop", drop)) if key in caps
        }
    if "seccomp" in table:
        result["seccomp"] = _kubernetes_profile(table["seccomp"], f"{path}.seccomp", "seccomp")
    if "apparmor" in table:
        result["apparmor"] = _kubernetes_profile(table["apparmor"], f"{path}.apparmor", "apparmor")
    if "selinux" in table:
        options = _normalize_table(table["selinux"], f"{path}.selinux", ("user", "role", "type", "level"))
        result["selinux"] = {
            key: (_selinux_level if key == "level" else _selinux_component)(item, f"{path}.selinux.{key}")
            for key, item in options.items()
        }
    if "read_only_root_filesystem" in table:
        result["read_only_root_filesystem"] = _boolean(
            table["read_only_root_filesystem"], f"{path}.read_only_root_filesystem"
        )
    return result


def _validate_oci(value: Any) -> Dict[str, Any]:
    path = "security.oci"
    table = _normalize_table(
        value,
        path,
        ("capabilities", "seccomp_profile", "apparmor_profile", "selinux_label", "mount_label", "read_only_root"),
    )
    result: Dict[str, Any] = {}
    if "capabilities" in table:
        caps_path = f"{path}.capabilities"
        caps = _normalize_table(table["capabilities"], caps_path, OCI_CAPABILITY_SETS)
        missing = [name for name in OCI_CAPABILITY_SETS if name not in caps]
        if missing:
            _fail(
                caps_path,
                f"must list every set explicitly (missing: {', '.join(missing)}); "
                "OCI has no relative add/drop baseline",
            )
        sets = {
            name: _capability_list(caps[name], f"{caps_path}.{name}", prefixed=True, allow_all=False)
            for name in OCI_CAPABILITY_SETS
        }
        bounding, permitted = set(sets["bounding"]), set(sets["permitted"])
        rules = (
            ("effective", permitted, "permitted"),
            ("permitted", bounding, "bounding"),
            ("inheritable", bounding, "bounding"),
            ("ambient", permitted & set(sets["inheritable"]), "both permitted and inheritable"),
        )
        for name, allowed, description in rules:
            extra = sorted(set(sets[name]) - allowed)
            if extra:
                _fail(f"{caps_path}.{name}", f"{', '.join(extra)} must also be in {description}")
        result["capabilities"] = sets
    if "seccomp_profile" in table:
        profile = _string(table["seccomp_profile"], f"{path}.seccomp_profile")
        candidate = PurePosixPath(profile.replace("\\", "/"))
        if candidate.is_absolute() or Path(profile).is_absolute() or ".." in candidate.parts:
            _fail(f"{path}.seccomp_profile", "must be a path relative to the build context without '..'")
        result["seccomp_profile"] = candidate.as_posix()
    if "apparmor_profile" in table:
        result["apparmor_profile"] = _string(table["apparmor_profile"], f"{path}.apparmor_profile")
    for key in ("selinux_label", "mount_label"):
        if key in table:
            result[key] = _selinux_context(table[key], f"{path}.{key}")
    if "read_only_root" in table:
        result["read_only_root"] = _boolean(table["read_only_root"], f"{path}.read_only_root")
    return result


def validate_security(security: Any, platform: str) -> Dict[str, Any]:
    """Validate and normalize ``BuildConfig.security``; idempotent on its own output."""
    table = _normalize_table(security, "security", ("kubernetes", "oci"))
    target_os = platform.split("/", 1)[0] if isinstance(platform, str) else ""
    if target_os != "linux":
        raise ValueError(
            f"Security fragments support Linux targets only; platform {platform!r} is not supported."
        )
    result: Dict[str, Any] = {}
    if "kubernetes" in table:
        result["kubernetes"] = _validate_kubernetes(table["kubernetes"])
    if "oci" in table:
        result["oci"] = _validate_oci(table["oci"])
    return result


def kubernetes_security_context(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Return the Kubernetes container ``securityContext`` for validated settings."""
    context: Dict[str, Any] = {}
    if "capabilities" in settings:
        context["capabilities"] = copy.deepcopy(settings["capabilities"])
    for key, field in (("seccomp", "seccompProfile"), ("apparmor", "appArmorProfile")):
        if key in settings:
            profile = {"type": settings[key]["type"]}
            if "localhost_profile" in settings[key]:
                profile["localhostProfile"] = settings[key]["localhost_profile"]
            context[field] = profile
    if "selinux" in settings:
        context["seLinuxOptions"] = dict(settings["selinux"])
    if "read_only_root_filesystem" in settings:
        context["readOnlyRootFilesystem"] = settings["read_only_root_filesystem"]
    return context


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _uint(value: Any, path: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        _fail(path, f"expected an integer between 0 and {maximum}")
    return value


def _enum(value: Any, path: str, allowed: tuple) -> str:
    if value not in allowed:
        if value == "SCMP_ACT_NOTIFY":
            _fail(path, "SCMP_ACT_NOTIFY requires a seccomp agent and is not supported")
        _fail(path, f"unsupported value {value!r}")
    return value


def _strict_object(value: Any, path: str, allowed: tuple, required: tuple) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, f"expected a JSON object, got {type(value).__name__}")
    for key in value:
        if key in UNSUPPORTED_SECCOMP_FIELDS:
            _fail(f"{path}.{key}", UNSUPPORTED_SECCOMP_FIELDS[key])
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail(path, f"unknown OCI seccomp field(s) {', '.join(map(repr, unknown))}")
    missing = [key for key in required if key not in value]
    if missing:
        _fail(path, f"missing required field(s) {', '.join(missing)}")
    return value


def _string_list(value: Any, path: str, allowed: Optional[tuple] = None, *, min_items: int = 0) -> None:
    if not isinstance(value, list) or len(value) < min_items:
        _fail(path, "expected a list" + (" with at least one item" if min_items else ""))
    if len(set(map(repr, value))) != len(value):
        _fail(path, "contains duplicate entries")
    for index, item in enumerate(value):
        if allowed is None:
            if not isinstance(item, str) or not item:
                _fail(f"{path}[{index}]", "expected a non-empty string")
        else:
            _enum(item, f"{path}[{index}]", allowed)


def validate_oci_seccomp(profile: Any, path: str = "seccomp") -> Dict[str, Any]:
    """Strictly validate an OCI runtime-spec v1.3.0 ``linux.seccomp`` object."""
    _strict_object(
        profile,
        path,
        ("defaultAction", "defaultErrnoRet", "architectures", "flags", "syscalls"),
        ("defaultAction",),
    )
    _enum(profile["defaultAction"], f"{path}.defaultAction", SECCOMP_ACTIONS)
    if "defaultErrnoRet" in profile:
        if profile["defaultAction"] not in SECCOMP_ERRNO_ACTIONS:
            _fail(f"{path}.defaultErrnoRet", "is only valid with SCMP_ACT_ERRNO or SCMP_ACT_TRACE")
        _uint(profile["defaultErrnoRet"], f"{path}.defaultErrnoRet", UINT32_MAX)
    if "architectures" in profile:
        _string_list(profile["architectures"], f"{path}.architectures", SECCOMP_ARCHITECTURES)
    if "flags" in profile:
        _string_list(profile["flags"], f"{path}.flags", SECCOMP_FLAGS)
    syscalls = profile.get("syscalls", [])
    if not isinstance(syscalls, list):
        _fail(f"{path}.syscalls", "expected a list")
    for index, rule in enumerate(syscalls):
        rule_path = f"{path}.syscalls[{index}]"
        _strict_object(rule, rule_path, ("names", "action", "errnoRet", "args"), ("names", "action"))
        _string_list(rule["names"], f"{rule_path}.names", min_items=1)
        _enum(rule["action"], f"{rule_path}.action", SECCOMP_ACTIONS)
        if "errnoRet" in rule:
            if rule["action"] not in SECCOMP_ERRNO_ACTIONS:
                _fail(f"{rule_path}.errnoRet", "is only valid with SCMP_ACT_ERRNO or SCMP_ACT_TRACE")
            _uint(rule["errnoRet"], f"{rule_path}.errnoRet", UINT32_MAX)
        args = rule.get("args", [])
        if not isinstance(args, list):
            _fail(f"{rule_path}.args", "expected a list")
        for arg_index, arg in enumerate(args):
            arg_path = f"{rule_path}.args[{arg_index}]"
            _strict_object(arg, arg_path, ("index", "value", "valueTwo", "op"), ("index", "value", "op"))
            _uint(arg["index"], f"{arg_path}.index", 5)
            _uint(arg["value"], f"{arg_path}.value", UINT64_MAX)
            if "valueTwo" in arg:
                _uint(arg["valueTwo"], f"{arg_path}.valueTwo", UINT64_MAX)
            _enum(arg["op"], f"{arg_path}.op", SECCOMP_OPERATORS)
    return profile


def load_oci_seccomp_profile(context_dir: str, relative_path: str) -> Dict[str, Any]:
    """Read and validate a context-relative OCI seccomp profile file."""
    context = Path(context_dir).resolve()
    candidate = (context / relative_path).resolve()
    if candidate != context and context not in candidate.parents:
        raise ValueError(f"Seccomp profile {relative_path!r} resolves outside the build context.")
    if not candidate.is_file():
        raise ValueError(f"Seccomp profile not found: {relative_path}")
    if candidate.stat().st_size > MAX_SECCOMP_PROFILE_BYTES:
        raise ValueError(f"Seccomp profile {relative_path!r} exceeds {MAX_SECCOMP_PROFILE_BYTES} bytes.")
    try:
        profile = json.loads(
            candidate.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Seccomp profile {relative_path!r} is not valid JSON: {exc}") from exc
    return validate_oci_seccomp(profile, f"security.oci.seccomp_profile ({relative_path})")


def oci_runtime_fragment(settings: Dict[str, Any], context_dir: str) -> Dict[str, Any]:
    """Return a mergeable OCI runtime-spec v1.3.0 fragment for validated settings."""
    process: Dict[str, Any] = {}
    linux: Dict[str, Any] = {}
    fragment: Dict[str, Any] = {}
    if "capabilities" in settings:
        process["capabilities"] = copy.deepcopy(settings["capabilities"])
    if "apparmor_profile" in settings:
        process["apparmorProfile"] = settings["apparmor_profile"]
    if "selinux_label" in settings:
        process["selinuxLabel"] = settings["selinux_label"]
    if "seccomp_profile" in settings:
        linux["seccomp"] = load_oci_seccomp_profile(context_dir, settings["seccomp_profile"])
    if "mount_label" in settings:
        linux["mountLabel"] = settings["mount_label"]
    if process:
        fragment["process"] = process
    if linux:
        fragment["linux"] = linux
    if "read_only_root" in settings:
        fragment["root"] = {"readonly": settings["read_only_root"]}
    return fragment


def render_security_fragments(security: Optional[Dict[str, Any]], context_dir: str) -> Dict[str, Dict[str, Any]]:
    """Return ``{filename: fragment}`` for each requested deployment target."""
    if not security:
        return {}
    fragments = {}
    if "kubernetes" in security:
        fragments[KUBERNETES_FRAGMENT] = kubernetes_security_context(security["kubernetes"])
    if "oci" in security:
        fragments[OCI_FRAGMENT] = oci_runtime_fragment(security["oci"], context_dir)
    return fragments


def serialize_fragment(fragment: Dict[str, Any]) -> bytes:
    return (json.dumps(fragment, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_security_fragments(output_dir: Path, fragments: Dict[str, Dict[str, Any]]) -> List[Path]:
    """Atomically write requested fragments and remove stale owned fragments."""
    directory = Path(output_dir) / SECURITY_DIR
    written = []
    if fragments:
        directory.mkdir(parents=True, exist_ok=True)
    for name in FRAGMENT_FILES:
        target = directory / name
        if name in fragments:
            temp = directory / f".{name}.tmp"
            temp.write_bytes(serialize_fragment(fragments[name]))
            os.replace(temp, target)
            written.append(target)
        elif target.exists():
            target.unlink()
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
    return written


_OCI_REPLACED_PATHS = (
    ("process", "capabilities"),
    ("process", "apparmorProfile"),
    ("process", "selinuxLabel"),
    ("linux", "seccomp"),
    ("linux", "mountLabel"),
    ("root", "readonly"),
)


def merge_oci_runtime_fragment(base_config: Dict[str, Any], fragment: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``base_config`` with each fragment field replacing the base value wholesale."""
    merged = copy.deepcopy(base_config)
    for section, key in _OCI_REPLACED_PATHS:
        if section in fragment and key in fragment[section]:
            target = merged.setdefault(section, {})
            if not isinstance(target, dict):
                raise ValueError(f"Base runtime config field {section!r} is not an object.")
            target[key] = copy.deepcopy(fragment[section][key])
    unknown = {
        f"{section}.{key}"
        for section, values in fragment.items()
        for key in (values if isinstance(values, dict) else {"<value>": None})
    } - {f"{section}.{key}" for section, key in _OCI_REPLACED_PATHS}
    if unknown:
        raise ValueError(f"Unsupported OCI fragment field(s): {', '.join(sorted(unknown))}")
    return merged
