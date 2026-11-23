import argparse, os, logging, sys
from pathlib import Path
from .config import BuildConfig, SBOM_FORMATS, SCAN_SEVERITIES
from .builder import ImageBuilder
from .config_loader import config_from_file
from .project import detect_python_version

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
    b=sub.add_parser("build")
    b.add_argument("--config","-c",help="Path to pycontainer.toml config file")
    b.add_argument("--tag",action="append",help="Image tag (repeatable; the first is the primary tag)")
    b.add_argument("--semver-tags",action="store_true",default=None,help="Also tag the minor and major versions (v1.2.3 -> v1.2, v1)")
    b.add_argument("--latest",dest="latest_tag",action="store_true",default=None,help="Also tag the image as latest")
    b.add_argument("--base-image",help="Base image to layer on: a registry reference, oci:<path>[:<ref>|@sha256:<digest>], or docker-archive:<path>[:<repo-tag>] (auto-detected from requires-python if not specified)")
    b.add_argument("--context",dest="context_dir")
    b.add_argument("--push",action="store_true",help="Push image to registry after build")
    b.add_argument("--registry",help="Override registry from tag (e.g., ghcr.io/user/repo:v1)")
    b.add_argument("--username",help="Registry username (or use REGISTRY_USERNAME env var)")
    b.add_argument("--password",help="Registry password/token (or use env vars)")
    b.add_argument("--include",dest="include_paths",action="append",help="Include a context-root-relative file or directory (repeatable)")
    b.add_argument("--exclude",dest="exclude_paths",action="append",help="Exclude a context-root-relative path or glob (repeatable)")
    b.add_argument("--no-default-excludes",dest="exclude_defaults",action="store_false",default=None,help="Include normally excluded generated, test, virtualenv, and VCS paths")
    b.add_argument("--no-progress",action="store_true",help="Suppress progress output")
    b.add_argument("--no-cache",dest="use_cache",action="store_false",default=None,help="Disable layer caching, force full rebuild")
    b.add_argument("--cache-dir",help="Custom cache directory (default: ~/.pycontainer/cache)")
    b.add_argument(
        "--include-deps",
        action="store_true",
        default=None,
        help="Stage target-compatible dependencies from a venv or requirements file",
    )
    b.add_argument("--requirements",dest="requirements_file",help="Requirements file for dependency layer")
    b.add_argument("--verbose","-v",action="store_true",default=None,help="Verbose output with detailed logging")
    b.add_argument("--dry-run",action="store_true",default=None,help="Show build plan without building")
    b.add_argument("--platform",help="Target platform (e.g., linux/amd64, linux/arm64)")
    b.add_argument("--no-reproducible",dest="reproducible",action="store_false",default=None,help="Disable reproducible builds")
    b.add_argument(
        "--sbom",
        dest="generate_sbom",
        nargs="?",
        const="spdx",
        choices=SBOM_FORMATS,
        default=None,
        help="Generate an SBOM (default format: spdx; formats: spdx, cyclonedx)",
    )
    b.add_argument("--system-package",dest="system_packages",action="append",help="Declare a system package the base image must provide, e.g. libpq5 (repeatable); verified, not installed")
    b.add_argument("--no-detect-system-packages",dest="detect_system_packages",action="store_false",default=None,help="Disable inferring system packages from Python dependencies")
    b.add_argument("--scan",action="store_true",default=None,help="Gate the generated image using optional Grype 0.119.0 (experimental)")
    b.add_argument("--scan-executable",help="Grype executable path or name")
    b.add_argument("--scan-threshold",choices=SCAN_SEVERITIES,help="Fail at this severity or higher (default: high)")
    b.add_argument("--scan-unknown",choices=("fail","ignore"),help="Unknown severity policy (default: fail)")
    b.add_argument("--scan-report-format",choices=("json","sarif"),help="Report format (default: json)")
    b.add_argument("--scan-report",help="Report destination (default: output directory)")
    b.add_argument("--scan-offline",action="store_true",default=None,help="Use an imported database without updates; freshness still enforced")
    b.add_argument("--scan-db-dir",help="Grype database cache (default: ~/.pycontainer/grype-db)")
    b.add_argument("--scan-db-max-age-hours",type=int,help="Maximum database build age (default: 120 hours)")
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

    if args.push and args.username and not (
        args.password or os.getenv('REGISTRY_PASSWORD')
    ):
        parser.error("--username requires --password or REGISTRY_PASSWORD.")

    cli_overrides={
        'tag': args.tag[0] if args.tag else None,
        'tags': args.tag[1:] if args.tag else None,
        'semver_tags': args.semver_tags,
        'latest_tag': args.latest_tag,
        'base_image': args.base_image,
        'context_dir': args.context_dir,
        'include_paths': args.include_paths,
        'exclude_paths': args.exclude_paths,
        'exclude_defaults': args.exclude_defaults,
        'use_cache': args.use_cache,
        'cache_dir': args.cache_dir,
        'include_deps': args.include_deps,
        'requirements_file': args.requirements_file,
        'verbose': args.verbose,
        'dry_run': args.dry_run,
        'platform': args.platform,
        'reproducible': args.reproducible,
        'generate_sbom': args.generate_sbom,
        'system_packages': args.system_packages,
        'detect_system_packages': args.detect_system_packages,
        'scan': args.scan,
        'scan_executable': args.scan_executable,
        'scan_threshold': args.scan_threshold,
        'scan_unknown': args.scan_unknown,
        'scan_report_format': args.scan_report_format,
        'scan_report': args.scan_report,
        'scan_offline': args.scan_offline,
        'scan_db_dir': args.scan_db_dir,
        'scan_db_max_age_hours': args.scan_db_max_age_hours,
    }
    cli_overrides={k:v for k,v in cli_overrides.items() if v is not None}

    try:
        if args.config:
            cfg=config_from_file(Path(args.config), cli_overrides)
        else:
            cfg=BuildConfig(**cli_overrides)
            if 'base_image' not in cli_overrides:
                py_ver=detect_python_version(cfg.context_dir)
                cfg.base_image=f"python:{py_ver}-slim"
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    if cfg.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')

    builder=ImageBuilder(cfg)
    from .scanning import ScanError
    try:
        out=builder.build()
    except ScanError as exc:
        parser.exit(3 if exc.category == "findings" else 4, f"{exc}\n")
    print("Built:", out)
    
    if args.push:
        password=args.password
        if args.username and not password:
            password=os.getenv('REGISTRY_PASSWORD')
        try:
            builder.push(
                registry_url=args.registry,
                username=args.username,
                password=password,
                show_progress=not args.no_progress,
            )
        except ScanError as exc:
            parser.exit(3 if exc.category == "findings" else 4, f"{exc}\n")

if __name__=="__main__":
    main()
