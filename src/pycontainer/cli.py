import argparse, json, os, logging, sys
from contextlib import ExitStack
from pathlib import Path
from .config import SBOM_FORMATS
from .config_loader import load_validated_file_config
from .builder import ImageBuilder
from .effective_config import NOT_PERFORMED, resolve_build_config
from .fs_utils import output_directory_lock
from .image_validation import (
    MIB, SIZE_THRESHOLDS, finding_exit_code, parse_size_threshold, validate_ignores, validate_image,
)

EFFECTIVE_CONFIG_FIELDS = (
    "semver_tags", "latest_tag", "base_image", "context_dir", "include_paths", "exclude_paths",
    "exclude_defaults", "use_cache", "cache_dir", "include_deps", "requirements_file", "platform",
    "reproducible", "generate_sbom", "system_packages", "detect_system_packages",
)

def _add_effective_config_arguments(p):
    """Options that change the effective BuildConfig; shared by build and validate config."""
    p.add_argument("--tag",action="append",help="Image tag (repeatable; the first is the primary tag)")
    p.add_argument("--semver-tags",action="store_true",default=None,help="Also tag the minor and major versions (v1.2.3 -> v1.2, v1)")
    p.add_argument("--latest",dest="latest_tag",action="store_true",default=None,help="Also tag the image as latest")
    p.add_argument("--base-image",help="Base image to layer on (auto-detected from requires-python if not specified)")
    p.add_argument("--context",dest="context_dir")
    p.add_argument("--include",dest="include_paths",action="append",help="Include a context-root-relative file or directory (repeatable)")
    p.add_argument("--exclude",dest="exclude_paths",action="append",help="Exclude a context-root-relative path or glob (repeatable)")
    p.add_argument("--no-default-excludes",dest="exclude_defaults",action="store_false",default=None,help="Include normally excluded generated, test, virtualenv, and VCS paths")
    p.add_argument("--no-cache",dest="use_cache",action="store_false",default=None,help="Disable layer caching, force full rebuild")
    p.add_argument("--cache-dir",help="Custom cache directory (default: ~/.pycontainer/cache)")
    p.add_argument(
        "--include-deps",
        action="store_true",
        default=None,
        help="Stage target-compatible dependencies from a venv or requirements file",
    )
    p.add_argument("--requirements",dest="requirements_file",help="Requirements file for dependency layer")
    p.add_argument("--platform",help="Target platform (e.g., linux/amd64, linux/arm64)")
    p.add_argument("--no-reproducible",dest="reproducible",action="store_false",default=None,help="Disable reproducible builds")
    p.add_argument(
        "--sbom",
        dest="generate_sbom",
        nargs="?",
        const="spdx",
        choices=SBOM_FORMATS,
        default=None,
        help="Generate an SBOM (default format: spdx; formats: spdx, cyclonedx)",
    )
    p.add_argument("--system-package",dest="system_packages",action="append",help="Declare a system package the base image must provide, e.g. libpq5 (repeatable); verified, not installed")
    p.add_argument("--no-detect-system-packages",dest="detect_system_packages",action="store_false",default=None,help="Disable inferring system packages from Python dependencies")

def _cli_overrides(args, *operational):
    overrides={
        'tag': args.tag[0] if args.tag else None,
        'tags': args.tag[1:] if args.tag else None,
        **{name: getattr(args, name) for name in (*EFFECTIVE_CONFIG_FIELDS, *operational)},
    }
    return {k:v for k,v in overrides.items() if v is not None}

def _validate_config_command(parser, args):
    if args.config_paths and len(args.config_paths) > 1:
        parser.error("--config accepts one path; pass several paths positionally instead.")
    if args.config_paths and args.paths:
        parser.error("use either positional configuration paths or --config, not both.")
    paths=args.config_paths or args.paths
    overrides=_cli_overrides(args)
    sources=[Path(p) for p in paths] or [None]
    invalid=0
    for path in sources:
        label=str(path) if path is not None else "defaults (no configuration file)"
        try:
            _, table=resolve_build_config(path, overrides)
        except (OSError, ValueError) as exc:
            invalid+=1
            for line in str(exc).splitlines():
                print(f"error: {label}: {line}", file=sys.stderr)
        else:
            print(f"ok: {label}" + (f" {table}" if table else ""))
    print("Not performed (offline validation does not prove a successful build):")
    for check in NOT_PERFORMED:
        print(f"  - {check}")
    if invalid:
        print(f"{invalid} of {len(sources)} configuration(s) invalid.", file=sys.stderr)
        raise SystemExit(2)

def _add_size_threshold_arguments(parser, suffix=""):
    for name in SIZE_THRESHOLDS:
        subject = "total stored image" if name == "max_image_size_mb" else "largest stored layer"
        parser.add_argument(
            "--" + name.replace("_", "-"), metavar="MIB",
            help=f"Warn when the {subject} size exceeds MIB mebibytes{suffix}",
        )

def _size_thresholds(parser, args, file_validate):
    """Merge TOML thresholds with CLI values; a CLI value wins only when supplied."""
    thresholds = {name: file_validate.get(name) for name in SIZE_THRESHOLDS}
    for name in SIZE_THRESHOLDS:
        value = getattr(args, name)
        if value is not None:
            try:
                thresholds[name] = parse_size_threshold(value, "--" + name.replace("_", "-"))
            except ValueError as exc:
                parser.error(str(exc))
    return thresholds

def _mib(size):
    return f"{size} bytes ({size / MIB:.2f} MiB)"

def _print_size(size):
    largest = size["largest_layer"]
    layers = "no layers" if largest is None else (
        f"largest layer {_mib(largest['bytes'])}, {largest['media_type']}"
    )
    print(f"Stored image size: {_mib(size['image_bytes'])} "
          f"(config and {len(size['layers'])} layer descriptor(s); {layers}).")
    contributions = size["contributions"]
    if contributions["status"] == "determined":
        print(f"Contributions: base {_mib(contributions['base_bytes'])}; application/dependency "
              f"{_mib(contributions['application_bytes'])}; config {_mib(size['config_bytes'])}.")
    else:
        print("Contributions: base versus application/dependency unknown (the layout does not "
              f"record layer origin); config {_mib(size['config_bytes'])}.")
    print(f"Note: {size['note']}")

def _image_validation(
    output, tag, platform, ignores, format_, fail_on, expected_digest=None,
    thresholds=None, base_layer_count=None,
):
    try:
        report=validate_image(
            output, tag=tag, platform=platform, ignores=ignores, thresholds=thresholds,
            expected_digest=expected_digest, base_layer_count=base_layer_count,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if format_ == "json":
        print(json.dumps(report, sort_keys=True))
    else:
        for finding in report["findings"]:
            print(f"{finding['severity']}: {finding['rule_id']}: {finding['message']}")
            print(f"  remediation: {finding['remediation']}")
        if not report["findings"]:
            print("No image validation findings.")
        _print_size(report["size"])
        print("Base image provenance: unknown (PCB002 is deferred).")
    return finding_exit_code(report, fail_on)

def _validate_image_command(parser, args):
    for option in ("output", "tag", "platform", "config"):
        if getattr(args, option) == "":
            parser.error(f"--{option} must not be empty.")
    ignores=list(args.ignore or [])
    file_validate={}
    if args.config is not None:
        try:
            _, config=load_validated_file_config(Path(args.config))
            file_validate=config.get("validate", {})
            ignores=[*file_validate.get("ignore", []), *ignores]
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2)
    try:
        validate_ignores(ignores)
    except ValueError as exc:
        parser.error(str(exc))
    thresholds=_size_thresholds(parser, args, file_validate)
    code=_image_validation(
        Path(args.output), args.tag, args.platform, ignores, args.format, args.fail_on,
        thresholds=thresholds,
    )
    if code:
        raise SystemExit(code)

def _push_image(builder, args):
    password=args.password
    if args.username and not password:
        password=os.getenv('REGISTRY_PASSWORD')
    builder.push(
        registry_url=args.registry,
        username=args.username,
        password=password,
        show_progress=not args.no_progress,
    )

def _tolerate_unencodable_output():
    # Redirected output on Windows defaults to a legacy code page that cannot encode status symbols.
    for stream in (sys.stdout, sys.stderr):
        encoding=getattr(stream,"encoding",None)
        reconfigure=getattr(stream,"reconfigure",None)
        if not encoding or reconfigure is None: continue
        try: "\u2713".encode(encoding)
        except (UnicodeEncodeError, LookupError): reconfigure(errors="replace")

def main():
    _tolerate_unencodable_output()
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="cmd",required=True)
    b=sub.add_parser("build",help="Build an OCI image layout")
    b.add_argument("--config","-c",help="Path to pycontainer.toml config file")
    _add_effective_config_arguments(b)
    b.add_argument("--push",action="store_true",help="Push image to registry after build")
    b.add_argument("--registry",help="Override registry from tag (e.g., ghcr.io/user/repo:v1)")
    b.add_argument("--username",help="Registry username (or use REGISTRY_USERNAME env var)")
    b.add_argument("--password",help="Registry password/token (or use env vars)")
    b.add_argument("--no-progress",action="store_true",help="Suppress progress output")
    b.add_argument("--verbose","-v",action="store_true",default=None,help="Verbose output with detailed logging")
    b.add_argument("--dry-run",action="store_true",default=None,help="Show build plan without building")
    b.add_argument("--validate",action="store_true",help="Validate the image after building and before pushing")
    b.add_argument("--ignore",action="append",metavar="RULE",help="Ignore an image validation rule (requires --validate; repeatable)")
    _add_size_threshold_arguments(b, " (requires --validate)")
    # --- validate subcommand ---
    val=sub.add_parser("validate",help="Validate configuration or an OCI image offline")
    val_sub=val.add_subparsers(dest="validate_cmd",metavar="{config,image}",required=True)
    vc=val_sub.add_parser(
        "config",
        help="Validate the effective build configuration offline",
        description="Resolve defaults, configuration files and CLI overrides exactly as 'build' does, "
        "without building, pulling, pushing or touching the cache. Exit 0 when valid, 2 otherwise.",
    )
    vc.add_argument("paths",nargs="*",metavar="PATH",help="Configuration files to validate (default: build defaults)")
    vc.add_argument("--config","-c",dest="config_paths",action="append",metavar="PATH",help="Validate a single configuration file")
    _add_effective_config_arguments(vc)
    vi=val_sub.add_parser(
        "image",
        help="Verify and lint an existing OCI image layout offline",
        description="Verify reachable OCI blobs, then lint the selected image. Exit 0 below the "
        "failure threshold, 1 for findings, and 2 for usage, layout or integrity errors.",
    )
    vi.add_argument("--output",default="dist/image",help="OCI layout directory (default: dist/image)")
    vi.add_argument("--tag",help="Select the OCI reference name")
    vi.add_argument("--platform",help="Select the image platform (os/arch[/variant])")
    vi.add_argument("--format",choices=("text","json"),default="text",help="Output format (default: text)")
    vi.add_argument("--fail-on",choices=("warning","error"),default="warning",help="Minimum failing severity (default: warning)")
    vi.add_argument("--config","-c",help="Read validation ignores from a configuration file")
    vi.add_argument("--ignore",action="append",metavar="RULE",help="Ignore a rule ID (repeatable; extends config ignores)")
    _add_size_threshold_arguments(vi, " (overrides the config value)")
    # --- end validate subcommand ---
    # --- import subcommand (Dockerfile migration) ---
    imp=sub.add_parser("import",help="Convert a Dockerfile to [tool.pycontainer] settings")
    imp.add_argument("dockerfile",help="Path to the Dockerfile")
    imp.add_argument("--output","-o",help="Write settings to this pyproject.toml (default: print to stdout)")
    imp.add_argument("--force",action="store_true",help="Merge into an existing output file")
    imp.add_argument("--build-arg",action="append",default=[],metavar="KEY=VALUE",help="Value for a Dockerfile ARG (repeatable)")
    # --- end import subcommand ---
    args=parser.parse_args()

    if args.cmd=="import":
        from .dockerfile_import import run_import
        build_args=dict(a.partition("=")[::2] for a in args.build_arg)
        try:
            message,code=run_import(Path(args.dockerfile),Path(args.output) if args.output else None,args.force,build_args)
        except (FileNotFoundError,ValueError) as exc:
            parser.error(str(exc))
        print(message)
        raise SystemExit(code)

    if args.cmd=="validate":
        if args.validate_cmd=="config":
            _validate_config_command(vc, args)
        else:
            _validate_image_command(vi, args)
        return

    if args.ignore and not args.validate:
        b.error("--ignore requires --validate.")
    for name in SIZE_THRESHOLDS:
        if getattr(args, name) is not None and not args.validate:
            b.error(f"--{name.replace('_', '-')} requires --validate.")
    if args.push and args.username and not (
        args.password or os.getenv('REGISTRY_PASSWORD')
    ):
        parser.error("--username requires --password or REGISTRY_PASSWORD.")

    cli_overrides=_cli_overrides(args, 'verbose', 'dry_run')

    try:
        cfg,_=resolve_build_config(Path(args.config) if args.config else None, cli_overrides)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    if args.validate and cfg.dry_run:
        b.error("--validate cannot be used with --dry-run.")
    ignores=[*cfg.validate.get("ignore", []), *(args.ignore or [])]
    try:
        validate_ignores(ignores)
    except ValueError as exc:
        b.error(str(exc))
    thresholds=_size_thresholds(b, args, cfg.validate)

    if cfg.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')

    builder=ImageBuilder(cfg)
    out=builder.build()
    print("Built:", out)

    if args.validate:
        with ExitStack() as stack:
            try:
                stack.enter_context(output_directory_lock(builder.config.output_dir))
            except (OSError, RuntimeError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                raise SystemExit(2)
            code=_image_validation(
                Path(builder.config.output_dir), builder.config.tag, builder.config.platform,
                ignores, "text", "warning", expected_digest=builder.manifest_digest,
                thresholds=thresholds, base_layer_count=builder.base_layer_count,
            )
            if code:
                raise SystemExit(code)
            if args.push:
                _push_image(builder, args)
    elif args.push:
        _push_image(builder, args)

if __name__=="__main__":
    main()
