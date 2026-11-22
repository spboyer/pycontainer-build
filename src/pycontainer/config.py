import posixpath
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

SBOM_FORMATS = ("spdx", "cyclonedx")

# Linux signal names in the OCI "SIGNAME" format, matching Docker's Linux table.
STOP_SIGNALS = frozenset(
    [
        "SIGABRT", "SIGALRM", "SIGBUS", "SIGCHLD", "SIGCONT", "SIGFPE",
        "SIGHUP", "SIGILL", "SIGINT", "SIGIO", "SIGKILL", "SIGPIPE",
        "SIGPROF", "SIGPWR", "SIGQUIT", "SIGSEGV", "SIGSTKFLT",
        "SIGSTOP", "SIGSYS", "SIGTERM", "SIGTRAP", "SIGTSTP", "SIGTTIN",
        "SIGTTOU", "SIGURG", "SIGUSR1", "SIGUSR2", "SIGVTALRM", "SIGWINCH",
        "SIGXCPU", "SIGXFSZ", "SIGRTMIN", "SIGRTMAX",
    ]
    + [f"SIGRTMIN+{n}" for n in range(1, 16)]
    + [f"SIGRTMAX-{n}" for n in range(1, 15)]
)


def validate_stop_signal(value: str) -> str:
    """Return value if it is a supported OCI SIGNAME, else raise ValueError."""
    if not isinstance(value, str) or value not in STOP_SIGNALS:
        raise ValueError(
            f"stop_signal must be a Linux signal name such as SIGTERM, SIGINT or "
            f"SIGRTMIN+3; got {value!r}."
        )
    return value


def validate_volume(path: str) -> str:
    """Return path if it is a normalized absolute container path, else raise ValueError."""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path == "/"
        or path.startswith("//")
        or "\\" in path
        or any(unicodedata.category(ch).startswith("C") for ch in path)
        or posixpath.normpath(path) != path
    ):
        raise ValueError(
            "volumes entries must be normalized absolute container paths other "
            f"than '/', without '.', '..', empty segments, backslashes or control "
            f"characters; got {path!r}."
        )
    return path


@dataclass
class BuildConfig:
    tag: str = "local/test:latest"
    base_image: str = "python:3.11-slim"
    context_dir: str = "."
    workdir: str = "/app"
    entrypoint: Optional[List[str]] = None
    env: Dict[str, str] = field(default_factory=dict)
    exposed_ports: List[int] = field(default_factory=list)
    include_paths: Optional[List[str]] = None
    exclude_paths: List[str] = field(default_factory=list)
    exclude_defaults: bool = True
    output_dir: str = "dist/image"
    use_cache: bool = True
    cache_dir: Optional[str] = None
    max_cache_size_mb: int = 5000
    labels: Optional[Dict[str, str]] = None
    user: Optional[str] = None
    cmd: Optional[List[str]] = None
    include_deps: bool = False
    requirements_file: str = "requirements.txt"
    verbose: bool = False
    dry_run: bool = False
    platform: str = "linux/amd64"
    reproducible: bool = True
    generate_sbom: Union[bool, str] = False
    system_packages: List[str] = field(default_factory=list)
    detect_system_packages: bool = True
    tags: Optional[List[str]] = None
    semver_tags: bool = False
    latest_tag: bool = False
    stop_signal: Optional[str] = None
    volumes: List[str] = field(default_factory=list)

    def __post_init__(self):
        from .system_packages import validate_system_packages

        self.system_packages = validate_system_packages(self.system_packages)
        if self.stop_signal is not None:
            validate_stop_signal(self.stop_signal)
        if not isinstance(self.volumes, list):
            raise ValueError("volumes must be a list of absolute container paths.")
        self.volumes = list(dict.fromkeys(validate_volume(p) for p in self.volumes))
        if not isinstance(self.generate_sbom, (bool, str)):
            raise ValueError(
                "generate_sbom must be a boolean or one of: spdx, cyclonedx"
            )
        if isinstance(self.generate_sbom, str) and self.generate_sbom not in SBOM_FORMATS:
            raise ValueError(
                "generate_sbom must be a boolean or one of: spdx, cyclonedx"
            )
        if self.tags or self.semver_tags or self.latest_tag:
            from .tags import resolve_tags

            resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)

    def all_tags(self) -> List[str]:
        """Return every full image reference this build is tagged with."""
        from .tags import resolve_tags

        return resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)
