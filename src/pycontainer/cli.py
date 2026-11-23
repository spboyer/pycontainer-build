import argparse, os, logging, sys
from pathlib import Path
from .config import BuildConfig, SBOM_FORMATS
from .builder import ImageBuilder
from .config_loader import config_from_file, load_config_file, merge_configs
from .project import detect_python_version
from .build_args import redact_values, resolve_arguments
from .devloop import (
    DevLoopError,
    TILT_PLACEHOLDER_TAG,
    single_platform,
    skaffold_request,
    tilt_content_tag,
    tilt_expected_reference,
    write_reference,
)

class _BuildArgumentLogFilter(logging.Filter):
    def __init__(self, config):
        super().__init__()
        self.config = config

    def filter(self, record):
        record.msg = self.config.redact(record.getMessage())
        record.args = ()
        return True

def _tolerate_unencodable_output():
    # Redirected output on Windows defaults to a legacy code page that cannot encode status symbols.
    for stream in (sys.stdout, sys.stderr):
        encoding=getattr(stream,"encoding",None)
        reconfigure=getattr(stream,"reconfigure",None)
        if not encoding or reconfigure is None: continue
        try: "\u2713".encode(encoding)
        except (UnicodeEncodeError, LookupError): reconfigure(errors="replace")

def _add_build_options(b,*,tagging=True,context=True,push=True,platform=True):
    b.add_argument("--config","-c",help="Path to pycontainer.toml config file")
    b.add_argument("--build-arg",action="append",default=[],metavar="KEY=VALUE",help="Override a declared build argument (repeatable; last wins; not a secret)")
    if tagging:
        b.add_argument("--tag",action="append",help="Image tag (repeatable; the first is the primary tag)")
        b.add_argument("--semver-tags",action="store_true",default=None,help="Also tag the minor and major versions (v1.2.3 -> v1.2, v1)")
        b.add_argument("--latest",dest="latest_tag",action="store_true",default=None,help="Also tag the image as latest")
    b.add_argument("--base-image",help="Base image to layer on: a registry reference, oci:<path>[:<ref>|@sha256:<digest>], or docker-archive:<path>[:<repo-tag>] (auto-detected from requires-python if not specified)")
    if context:
        b.add_argument("--context",dest="context_dir")
    if push:
        b.add_argument("--push",action="store_true",help="Push image to registry after build")
        b.add_argument("--registry",help="Override registry from tag (e.g., ghcr.io/user/repo:v1)")
        b.add_argument("--username",help="Registry username (or use REGISTRY_USERNAME env var)")
        b.add_argument("--password",help="Registry password/token (or use env vars)")
    b.add_argument("--insecure-registry",dest="insecure_registries",action="append",metavar="HOST[:PORT]",help="Use plain HTTP for this exact registry host[:port] (repeatable; unsafe outside trusted networks)")
    b.add_argument("--registry-ca-file",metavar="PATH",help="Trust an additional PEM CA bundle for registry HTTPS (verification stays enabled)")
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
    if push:
        b.add_argument("--dry-run",action="store_true",default=None,help="Show build plan without building")
    if platform:
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

def main():
    _tolerate_unencodable_output()
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="cmd",required=True)
    b=sub.add_parser("build")
    _add_build_options(b)
    sk=sub.add_parser("skaffold-build",help="Build and push for a Skaffold custom buildCommand (reads IMAGE, BUILD_CONTEXT, PUSH_IMAGE, PLATFORMS)")
    _add_build_options(sk,tagging=False,context=False,push=False,platform=False)
    ti=sub.add_parser("tilt-build",help="Build and push for a Tilt custom_build with skips_local_docker=True (reads EXPECTED_REF)")
    _add_build_options(ti,tagging=False,push=False)
    ti.add_argument("--image",help="Registry-qualified repository to push when EXPECTED_REF is unset (outputs_image_ref_to mode)")
    ti.add_argument("--output-ref",help="Write the pushed image reference to this file (Tilt outputs_image_ref_to)")
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

    devloop=args.cmd in ("skaffold-build","tilt-build")
    push=devloop or args.push
    username=getattr(args,"username",None)
    password=getattr(args,"password",None)
    if push and username and not (password or os.getenv('REGISTRY_PASSWORD')):
        parser.error("--username requires --password or REGISTRY_PASSWORD.")

    tag_args=getattr(args,"tag",None)
    cli_overrides={
        'tag': tag_args[0] if tag_args else None,
        'tags': tag_args[1:] if tag_args else None,
        'semver_tags': getattr(args,"semver_tags",None),
        'latest_tag': getattr(args,"latest_tag",None),
        'base_image': args.base_image,
        'context_dir': getattr(args,"context_dir",None),
        'include_paths': args.include_paths,
        'exclude_paths': args.exclude_paths,
        'exclude_defaults': args.exclude_defaults,
        'use_cache': args.use_cache,
        'cache_dir': args.cache_dir,
        'include_deps': args.include_deps,
        'requirements_file': args.requirements_file,
        'verbose': args.verbose,
        'dry_run': getattr(args,"dry_run",None),
        'platform': getattr(args,"platform",None),
        'reproducible': args.reproducible,
        'generate_sbom': args.generate_sbom,
        'system_packages': args.system_packages,
        'detect_system_packages': args.detect_system_packages,
        'insecure_registries': args.insecure_registries,
        'registry_ca_file': args.registry_ca_file,
    }
    cli_overrides={k:v for k,v in cli_overrides.items() if v is not None}

    push_target=getattr(args,"registry",None)
    try:
        if args.cmd=="skaffold-build":
            request=skaffold_request(os.environ)
            push_target=request.image
            cli_overrides.update(tag=request.image,context_dir=request.context_dir)
            if request.platform: cli_overrides['platform']=request.platform
        elif args.cmd=="tilt-build":
            push_target=tilt_expected_reference(os.environ,args.image)
            if push_target is None and not args.output_ref:
                raise DevLoopError("--image requires --output-ref so Tilt can read the pushed reference.")
            cli_overrides['tag']=push_target or f"{args.image}:{TILT_PLACEHOLDER_TAG}"
    except DevLoopError as exc:
        try:
            file_config=load_config_file(Path(args.config)) if args.config else {}
            declarations=merge_configs(file_config, {}).get("build_args", {})
            resolved_args=resolve_arguments(declarations, args.build_arg)
        except (OSError, ValueError) as config_error:
            parser.error(str(config_error))
        parser.error(redact_values(str(exc), resolved_args.values()))
    if devloop:
        # The tool owns the pushed reference; extra tags and dry runs would break its contract.
        cli_overrides.update(tags=[],semver_tags=False,latest_tag=False,dry_run=False)

    try:
        if args.config:
            cfg=config_from_file(Path(args.config), cli_overrides, args.build_arg)
        else:
            resolve_arguments({}, args.build_arg)
            cfg=BuildConfig(**cli_overrides, build_args={})
            if 'base_image' not in cli_overrides:
                py_ver=detect_python_version(cfg.context_dir)
                cfg.base_image=f"python:{py_ver}-slim"
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    if devloop:
        try:
            source="--platform" if args.cmd=="tilt-build" and args.platform is not None else "platform"
            platform=single_platform(cfg.platform,source)
            if not platform: raise DevLoopError("platform must specify a single target platform.")
            cfg.platform=platform
        except DevLoopError as exc:
            parser.error(cfg.redact(str(exc)))

    if cfg.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')

    log_filter = _BuildArgumentLogFilter(cfg)
    handlers = list(logging.getLogger().handlers) if cfg.build_args else []
    for handler in handlers:
        handler.addFilter(log_filter)
    try:
        builder=ImageBuilder(cfg)
        out=builder.build()
        print("Built:", cfg.redact(str(out)))

        if push:
            if username and not password:
                password=os.getenv('REGISTRY_PASSWORD')
            if args.cmd=="tilt-build" and push_target is None:
                push_target=f"{args.image}:{tilt_content_tag(builder.manifest_digest)}"
            pushed=builder.push(
                registry_url=push_target,
                username=username,
                password=password,
                show_progress=not args.no_progress,
            )
            if getattr(args,"output_ref",None):
                write_reference(Path(args.output_ref),pushed)
                print("Wrote image reference to", cfg.redact(str(args.output_ref)))
    except (OSError, ValueError, RuntimeError) as exc:
        if not cfg.build_args:
            raise
        parser.error(cfg.redact(str(exc)))
    finally:
        for handler in handlers:
            handler.removeFilter(log_filter)

if __name__=="__main__":
    main()
