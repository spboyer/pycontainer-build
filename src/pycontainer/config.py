from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union
from urllib.parse import unquote, urlsplit

SBOM_FORMATS = ("spdx", "cyclonedx")
SIGNING_PROVIDERS = ("cosign", "notation")
NOTATION_SIGNATURE_FORMATS = ("jws", "cose")
MULTI_PLATFORM_TARGETS = ("linux/amd64", "linux/arm64")
ARM_V7_FOLLOW_UP = "https://github.com/spboyer/pycontainer-build/issues/119"


def normalize_platforms(value: str) -> List[str]:
    """Split a comma-separated platform list, validating and de-duplicating it."""
    from .builder import parse_platform

    if not isinstance(value, str):
        raise ValueError("platform must be a string such as 'linux/amd64'.")
    platforms: List[str] = []
    for entry in value.split(","):
        platform = entry.strip()
        if not platform:
            raise ValueError(
                f"Invalid platform list {value!r}: entries must not be empty."
            )
        parse_platform(platform)
        if platform not in platforms:
            platforms.append(platform)
    return platforms


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
    sign: bool = False
    sign_provider: Optional[str] = None
    sign_key: Optional[str] = None
    sign_plugin: Optional[str] = None
    sign_key_id: Optional[str] = None
    sign_timestamp_url: Optional[str] = None
    sign_timestamp_root_cert: Optional[str] = None
    sign_signature_format: Optional[str] = None
    sign_verify_key: Optional[str] = None
    sign_certificate_identity: Optional[str] = None
    sign_certificate_oidc_issuer: Optional[str] = None
    provenance: bool = False
    provenance_require_complete: bool = False
    attest_provenance: bool = False
    provenance_verify_key: Optional[str] = None
    provenance_certificate_identity: Optional[str] = None
    provenance_certificate_oidc_issuer: Optional[str] = None
    security: Optional[dict] = None

    def __post_init__(self):
        from .system_packages import validate_system_packages

        self.system_packages = validate_system_packages(self.system_packages)
        self.validate_signing()
        self.validate_provenance()
        if self.security is not None:
            from .security import validate_security

            for platform in normalize_platforms(self.platform):
                self.security = validate_security(self.security, platform)
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
        platforms = normalize_platforms(self.platform)
        if len(platforms) > 1:
            unsupported = [p for p in platforms if p not in MULTI_PLATFORM_TARGETS]
            if unsupported:
                raise ValueError(
                    "Multi-platform builds support only "
                    f"{', '.join(MULTI_PLATFORM_TARGETS)}; unsupported target(s): "
                    f"{', '.join(unsupported)}. linux/arm/v7 support is tracked in "
                    f"{ARM_V7_FOLLOW_UP}."
                )
            if self.generate_sbom:
                raise ValueError(
                    "SBOM generation is not supported for multi-platform builds; "
                    "build each platform separately to generate its SBOM."
                )

    def validate_signing(self) -> None:
        if not isinstance(self.sign, bool):
            raise ValueError("sign must be a boolean.")
        if self.sign_provider is not None and self.sign_provider not in SIGNING_PROVIDERS:
            raise ValueError("sign_provider must be cosign or notation.")
        if not self.sign:
            return
        if self.sign_provider is None:
            raise ValueError("Signing requires an explicit sign_provider.")
        self._validate_references(
            "sign_key", "sign_plugin", "sign_key_id",
            "sign_timestamp_url", "sign_timestamp_root_cert", "sign_signature_format",
            "sign_verify_key", "sign_certificate_identity", "sign_certificate_oidc_issuer",
        )
        if self.sign_provider == "cosign":
            if any(
                value is not None
                for value in (
                    self.sign_plugin, self.sign_key_id, self.sign_timestamp_url,
                    self.sign_timestamp_root_cert, self.sign_signature_format,
                )
            ):
                raise ValueError("Cosign does not use Notation plugin, timestamp, or format options.")
            keyless_trust = (self.sign_certificate_identity, self.sign_certificate_oidc_issuer)
            if self.sign_key is not None:
                if self.sign_verify_key is None or any(keyless_trust):
                    raise ValueError(
                        "Cosign key signing requires sign_verify_key, the public key used to "
                        "verify the signature, and no keyless certificate options."
                    )
            elif self.sign_verify_key is not None or not all(keyless_trust):
                raise ValueError(
                    "Cosign keyless signing requires sign_certificate_identity and "
                    "sign_certificate_oidc_issuer for verification."
                )
        elif any(
            value is not None
            for value in (
                self.sign_verify_key, self.sign_certificate_identity,
                self.sign_certificate_oidc_issuer,
            )
        ):
            raise ValueError(
                "Notation verification uses its trust policy; Cosign verification "
                "options are not supported."
            )
        elif (
            (self.sign_key is None) == (self.sign_plugin is None)
            or (self.sign_plugin is None) != (self.sign_key_id is None)
        ):
            raise ValueError(
                "Notation requires either a named sign_key or both sign_plugin "
                "and sign_key_id."
            )
        if self.sign_timestamp_url is not None:
            timestamp_url = urlsplit(self.sign_timestamp_url)
            if timestamp_url.scheme not in {"http", "https"} or not timestamp_url.hostname:
                raise ValueError("sign_timestamp_url must be an HTTP(S) URL.")
        if self.sign_timestamp_root_cert is not None and self.sign_timestamp_url is None:
            raise ValueError("sign_timestamp_root_cert requires sign_timestamp_url.")
        if (
            self.sign_signature_format is not None
            and self.sign_signature_format not in NOTATION_SIGNATURE_FORMATS
        ):
            raise ValueError("sign_signature_format must be jws or cose.")

    def _validate_references(self, *names: str) -> None:
        for name in names:
            value = getattr(self, name)
            if value is None:
                continue
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or any(character in value for character in "\0\r\n")
                or "PRIVATE KEY" in value.upper()
            ):
                raise ValueError(f"{name} must be a non-empty reference, not secret material.")
            if value.lower().startswith("pkcs11:"):
                decoded = unquote(value).lower()
                if "pin-value=" in decoded or "pin-source=" in decoded:
                    raise ValueError(
                        f"{name} must not contain a PKCS#11 PIN; use COSIGN_PKCS11_PIN."
                    )
            if "://" in value:
                parsed = urlsplit(value)
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError(f"{name} must not contain credentials or URL parameters.")

    def validate_provenance(self) -> None:
        for name in ("provenance", "provenance_require_complete", "attest_provenance"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean.")
        if self.provenance_require_complete or self.attest_provenance:
            self.provenance = True
        verifier_names = (
            "provenance_verify_key", "provenance_certificate_identity",
            "provenance_certificate_oidc_issuer",
        )
        self._validate_references(*verifier_names)
        if not self.attest_provenance:
            if any(getattr(self, name) is not None for name in verifier_names):
                raise ValueError("Provenance verification options require attest_provenance.")
            return
        if not self.sign or self.sign_provider != "cosign":
            raise ValueError(
                "attest_provenance requires sign = true with sign_provider = cosign; "
                "Notation has no in-toto attestation path."
            )
        if all(getattr(self, name) is None for name in verifier_names):
            # Default to the signature trust policy so the same key is not configured twice.
            self.provenance_verify_key = self.sign_verify_key
            self.provenance_certificate_identity = self.sign_certificate_identity
            self.provenance_certificate_oidc_issuer = self.sign_certificate_oidc_issuer
        identity = self.provenance_certificate_identity
        issuer = self.provenance_certificate_oidc_issuer
        if (self.provenance_verify_key is None) == (identity is None and issuer is None) or (
            (identity is None) != (issuer is None)
        ):
            raise ValueError(
                "attest_provenance requires either provenance_verify_key or both "
                "provenance_certificate_identity and provenance_certificate_oidc_issuer."
            )

    def target_platforms(self) -> List[str]:
        """Return the normalized, de-duplicated target platforms in request order."""
        return normalize_platforms(self.platform)

    def all_tags(self) -> List[str]:
        """Return every full image reference this build is tagged with."""
        from .tags import resolve_tags

        return resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)
