import argparse, os, logging, sys
from pathlib import Path
from .config import BuildConfig, SBOM_FORMATS
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

def _add_plan_arguments(p):
    """Add the flags that determine the image content to a build or preview parser."""
    p.add_argument("--config","-c",help="Path to pycontainer.toml config file")
    p.add_argument("--tag",action="append",help="Image tag (repeatable; the first is the primary tag)")
    p.add_argument("--base-image",help="Base image to layer on: a registry reference, oci:<path>[:<ref>|@sha256:<digest>], or docker-archive:<path>[:<repo-tag>] (auto-detected from requires-python if not specified)")
    p.add_argument("--context",dest="context_dir")
    p.add_argument("--include",dest="include_paths",action="append",help="Include a context-root-relative file or directory (repeatable)")
    p.add_argument("--exclude",dest="exclude_paths",action="append",help="Exclude a context-root-relative path or glob (repeatable)")
    p.add_argument("--no-default-excludes",dest="exclude_defaults",action="store_false",default=None,help="Include normally excluded generated, test, virtualenv, and VCS paths")
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

def _load_build_config(parser, args, overrides):
    overrides={k:v for k,v in overrides.items() if v is not None}
    try:
        if args.config:
            return config_from_file(Path(args.config), overrides)
        cfg=BuildConfig(**overrides)
        if 'base_image' not in overrides:
            py_ver=detect_python_version(cfg.context_dir)
            cfg.base_image=f"python:{py_ver}-slim"
        return cfg
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

def _plan_overrides(args):
    return {
        'tag': args.tag[0] if args.tag else None,
        'tags': args.tag[1:] if args.tag else None,
        'base_image': args.base_image,
        'context_dir': args.context_dir,
        'include_paths': args.include_paths,
        'exclude_paths': args.exclude_paths,
        'exclude_defaults': args.exclude_defaults,
        'cache_dir': args.cache_dir,
        'include_deps': args.include_deps,
        'requirements_file': args.requirements_file,
        'platform': args.platform,
        'reproducible': args.reproducible,
    }

def main():
    _tolerate_unencodable_output()
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="cmd",required=True)
    b=sub.add_parser("build")
    _add_plan_arguments(b)
    b.add_argument("--semver-tags",action="store_true",default=None,help="Also tag the minor and major versions (v1.2.3 -> v1.2, v1)")
    b.add_argument("--latest",dest="latest_tag",action="store_true",default=None,help="Also tag the image as latest")
    b.add_argument("--push",action="store_true",help="Push image to registry after build")
    b.add_argument("--registry",help="Override registry from tag (e.g., ghcr.io/user/repo:v1)")
    b.add_argument("--username",help="Registry username (or use REGISTRY_USERNAME env var)")
    b.add_argument("--password",help="Registry password/token (or use env vars)")
    b.add_argument("--no-progress",action="store_true",help="Suppress progress output")
    b.add_argument("--no-cache",dest="use_cache",action="store_false",default=None,help="Disable layer caching, force full rebuild")
    b.add_argument("--verbose","-v",action="store_true",default=None,help="Verbose output with detailed logging")
    b.add_argument("--dry-run",action="store_true",default=None,help="Show build plan without building")
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
    # --- import subcommand (Dockerfile migration) ---
    imp=sub.add_parser("import",help="Convert a Dockerfile to [tool.pycontainer] settings")
    imp.add_argument("dockerfile",help="Path to the Dockerfile")
    imp.add_argument("--output","-o",help="Write settings to this pyproject.toml (default: print to stdout)")
    imp.add_argument("--force",action="store_true",help="Merge into an existing output file")
    imp.add_argument("--build-arg",action="append",default=[],metavar="KEY=VALUE",help="Value for a Dockerfile ARG (repeatable)")
    # --- end import subcommand ---
    ins=sub.add_parser("inspect",help="Inspect the layers of a local OCI layout or docker-save archive")
    ins.add_argument("image",help="oci:<dir>[:<ref>|@sha256:<digest>], docker-archive:<file>[:<tag>], or an OCI layout directory such as dist/image")
    ins.add_argument("--platform",help="Select a platform from a multi-platform index or verify an image's platform (os/arch[/variant])")
    ins.add_argument("--files",action="store_true",help="List the entries of every layer")
    ins.add_argument("--layer",dest="layers",type=int,action="append",help="List the entries of this 1-based layer (repeatable)")
    ins.add_argument("--diff",nargs=2,type=int,metavar=("FROM","TO"),help="Compare the filesystem after layer FROM (0 = empty) with the filesystem after layer TO")
    ins.add_argument("--json",action="store_true",help="Emit the report as JSON")
    pre=sub.add_parser("preview",help="Compare the planned build with an existing image without building it")
    pre.add_argument("--baseline",required=True,help="Image to compare with: oci:<dir>[:<ref>|@sha256:<digest>], docker-archive:<file>[:<tag>], an OCI layout directory (offline), or docker://<registry>/<repository>[:<tag>|@sha256:<digest>]")
    _add_plan_arguments(pre)
    pre.add_argument("--json",action="store_true",help="Emit the report as JSON")
    args=parser.parse_args()

    if args.cmd=="inspect":
        from .layer_inspect import run_inspect
        output,code=run_inspect(args.image,args.platform,args.files,args.layers,args.diff,args.json)
        print(output,file=sys.stderr if code else sys.stdout)
        raise SystemExit(code)

    if args.cmd=="preview":
        from .preview import run_preview
        cfg=_load_build_config(parser,args,_plan_overrides(args))
        output,code=run_preview(cfg,args.baseline,args.json)
        print(output,file=sys.stderr if code else sys.stdout)
        raise SystemExit(code)

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
        **_plan_overrides(args),
        'semver_tags': args.semver_tags,
        'latest_tag': args.latest_tag,
        'use_cache': args.use_cache,
        'verbose': args.verbose,
        'dry_run': args.dry_run,
        'generate_sbom': args.generate_sbom,
        'system_packages': args.system_packages,
        'detect_system_packages': args.detect_system_packages,
    }
    cfg=_load_build_config(parser,args,cli_overrides)

    if cfg.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')

    builder=ImageBuilder(cfg)
    out=builder.build()
    print("Built:", out)
    
    if args.push:
        password=args.password
        if args.username and not password:
            password=os.getenv('REGISTRY_PASSWORD')
        builder.push(
            registry_url=args.registry,
            username=args.username,
            password=password,
            show_progress=not args.no_progress,
        )

if __name__=="__main__":
    main()
