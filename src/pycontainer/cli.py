import argparse, json, os, logging, sys
from pathlib import Path
from .config import BuildConfig, SBOM_FORMATS, SCAN_SEVERITIES
from .builder import ImageBuilder
from .config_loader import config_from_file
from .history import BuildHistory, HistoryStorageError
from .project import detect_python_version
from .devloop import (
    DevLoopError,
    TILT_PLACEHOLDER_TAG,
    single_platform,
    skaffold_request,
    tilt_content_tag,
    tilt_expected_reference,
    write_reference,
)

def _metadata_argument(value: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError("expected KEY=VALUE with a non-empty key")
    return key, item

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
    history_options=b.add_mutually_exclusive_group()
    history_options.add_argument("--record-history",dest="record_history",action="store_true",default=None,help="Record private local build timing history")
    history_options.add_argument("--no-record-history",dest="record_history",action="store_false",default=None,help="Disable local build history")
    b.add_argument("--history-dir",help="Local history directory (default: ~/.pycontainer/history; does not enable recording)")
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
        b.add_argument("--python-version",help="Comma-separated CPython minors, each built in a separate pyX.Y layout")
        b.add_argument("--default-python-version",help="Matrix version receiving unsuffixed aliases (default: first requested version)")
        b.add_argument("--output",dest="output_dir",help="OCI output directory (matrix layouts use pyX.Y subdirectories)")
    if platform:
        b.add_argument("--platform",help="Target platform (e.g., linux/amd64), or a comma-separated list (linux/amd64,linux/arm64) to build an OCI image index")
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
    b.add_argument("--label",dest="labels",action="append",type=_metadata_argument,metavar="KEY=VALUE",help="Set an image-config label (repeatable)")
    b.add_argument("--manifest-annotation",dest="manifest_annotations",action="append",type=_metadata_argument,metavar="KEY=VALUE",help="Set an image-manifest annotation (repeatable)")
    b.add_argument("--index-annotation",dest="index_annotations",action="append",type=_metadata_argument,metavar="KEY=VALUE",help="Set a local image-index annotation (repeatable)")
    b.add_argument("--scan",action="store_true",default=None,help="Gate the generated image using optional Grype 0.119.0 (experimental)")
    b.add_argument("--scan-executable",help="Grype executable path or name")
    b.add_argument("--scan-threshold",choices=SCAN_SEVERITIES,help="Fail at this severity or higher (default: high)")
    b.add_argument("--scan-unknown",choices=("fail","ignore"),help="Unknown severity policy (default: fail)")
    b.add_argument("--scan-report-format",choices=("json","sarif"),help="Report format (default: json)")
    b.add_argument("--scan-report",help="Report destination (default: output directory)")
    b.add_argument("--scan-offline",action="store_true",default=None,help="Use an imported database without updates; freshness still enforced")
    b.add_argument("--scan-db-dir",help="Grype database cache (default: ~/.pycontainer/grype-db)")
    b.add_argument("--scan-db-max-age-hours",type=int,help="Maximum database build age (default: 120 hours)")
    b.add_argument("--stop-signal",help="OCI StopSignal, e.g. SIGTERM or SIGRTMIN+3 (overrides the base image value)")
    b.add_argument("--volume",dest="volumes",action="append",help="Declare an absolute container path as an OCI volume (repeatable; merged with base volumes; metadata only, no mount is created)")

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
    imp.add_argument("--shell",metavar="JSON",help='Shell for shell-form CMD/ENTRYPOINT conversion as a JSON array, e.g. \'["/bin/bash", "-c"]\' (default: ["/bin/sh", "-c"]); a later Dockerfile SHELL still applies')
    # --- end import subcommand ---
    history=sub.add_parser("history",help="Inspect or delete local build history")
    history_actions=history.add_subparsers(dest="history_action",required=True)
    for action in ("list", "clear"):
        command=history_actions.add_parser(action)
        command.add_argument("--history-dir",help="Local history directory (default: ~/.pycontainer/history)")
    gen=sub.add_parser("generate",help="Generate deployment artifacts for an existing image")
    targets=gen.add_subparsers(dest="target",required=True)
    kubernetes=targets.add_parser("kubernetes",help="Generate Kubernetes resources and a Helm chart")
    compose=targets.add_parser("compose",help="Generate a Docker Compose service")
    for target in (kubernetes,compose):
        target.add_argument("--image",required=True,help="Existing image reference (no credentials)")
        target.add_argument("--port",type=int,required=True,help="Container TCP port")
        target.add_argument("--name",default="app",help="Resource/service name (default: app)")
        target.add_argument("--output","-o",required=True,help="New output directory or file")
    kubernetes.add_argument("--service-port",type=int,help="Kubernetes Service port (default: container port)")
    compose.add_argument("--host-port",type=int,help="Compose loopback host port (default: container port)")
    args=parser.parse_args()

    if args.cmd=="history":
        try:
            store=BuildHistory(args.history_dir)
            if args.history_action=="list":
                print(json.dumps(store.list(),indent=2))
            else:
                store.clear()
                print("Build history cleared.")
        except HistoryStorageError as exc:
            parser.exit(1,f"Build history error: {exc}\n")
        return

    if args.cmd=="import":
        from .dockerfile_import import parse_shell_option, run_import
        build_args=dict(a.partition("=")[::2] for a in args.build_arg)
        try:
            shell=parse_shell_option(args.shell) if args.shell is not None else None
            message,code=run_import(Path(args.dockerfile),Path(args.output) if args.output else None,args.force,build_args,shell)
        except (FileNotFoundError,ValueError) as exc:
            parser.error(str(exc))
        print(message)
        raise SystemExit(code)

    if args.cmd=="generate":
        from .deployment import generate_compose, generate_kubernetes
        try:
            if args.target=="kubernetes":
                output=generate_kubernetes(
                    args.image,args.port,Path(args.output),name=args.name,service_port=args.service_port
                )
            else:
                output=generate_compose(
                    args.image,args.port,Path(args.output),name=args.name,host_port=args.host_port
                )
        except (ValueError,OSError) as exc:
            parser.error(str(exc))
        print("Generated:",output)
        return

    devloop=args.cmd in ("skaffold-build","tilt-build")
    push=devloop or args.push
    username=getattr(args,"username",None)
    password=getattr(args,"password",None)
    if push and username and not (password or os.getenv('REGISTRY_PASSWORD')):
        parser.error("--username requires --password or REGISTRY_PASSWORD.")

    tag_args=getattr(args,"tag",None)
    python_version=getattr(args,"python_version",None)
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
        'record_history': args.record_history,
        'history_dir': args.history_dir,
        'include_deps': args.include_deps,
        'requirements_file': args.requirements_file,
        'verbose': args.verbose,
        'dry_run': getattr(args,"dry_run",None),
        'platform': getattr(args,"platform",None),
        'python_versions': python_version.split(",") if python_version is not None else None,
        'default_python_version': getattr(args,"default_python_version",None),
        'output_dir': getattr(args,"output_dir",None),
        'reproducible': args.reproducible,
        'generate_sbom': args.generate_sbom,
        'system_packages': args.system_packages,
        'detect_system_packages': args.detect_system_packages,
        'labels': dict(args.labels) if args.labels is not None else None,
        'manifest_annotations': dict(args.manifest_annotations) if args.manifest_annotations is not None else None,
        'index_annotations': dict(args.index_annotations) if args.index_annotations is not None else None,
        'scan': args.scan,
        'scan_executable': args.scan_executable,
        'scan_threshold': args.scan_threshold,
        'scan_unknown': args.scan_unknown,
        'scan_report_format': args.scan_report_format,
        'scan_report': args.scan_report,
        'scan_offline': args.scan_offline,
        'scan_db_dir': args.scan_db_dir,
        'scan_db_max_age_hours': args.scan_db_max_age_hours,
        'stop_signal': args.stop_signal,
        'volumes': args.volumes,
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
            platform=single_platform(args.platform,"--platform")
            if platform: cli_overrides['platform']=platform
            cli_overrides['tag']=push_target or f"{args.image}:{TILT_PLACEHOLDER_TAG}"
    except DevLoopError as exc:
        parser.error(str(exc))
    if devloop:
        # The tool owns the pushed reference; extra tags and dry runs would break its contract.
        cli_overrides.update(tags=[],semver_tags=False,latest_tag=False,dry_run=False)

    try:
        if args.config:
            cfg=config_from_file(Path(args.config), cli_overrides)
        else:
            cfg=BuildConfig(**cli_overrides)
            if 'base_image' not in cli_overrides and not cfg.python_versions:
                py_ver=detect_python_version(cfg.context_dir)
                cfg.base_image=f"python:{py_ver}-slim"
        if devloop:
            if cfg.python_versions:
                raise DevLoopError("Python version matrices are not supported by Skaffold or Tilt builds.")
            platform=single_platform(cfg.platform,"platform")
            if not platform: raise DevLoopError("platform must specify a single target platform.")
            cfg.platform=platform
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    if cfg.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')

    from .scanning import ScanError
    try:
        builder=ImageBuilder(cfg)
        out=builder.build()
    except HistoryStorageError as exc:
        parser.exit(1,f"Build history error: {exc}\n")
    except ScanError as exc:
        parser.exit(3 if exc.category == "findings" else 4, f"{exc}\n")
    print("Built:", out)
    
    if push and not cfg.dry_run:
        if username and not password:
            password=os.getenv('REGISTRY_PASSWORD')
        if args.cmd=="tilt-build" and push_target is None:
            push_target=f"{args.image}:{tilt_content_tag(builder.manifest_digest)}"
        try:
            pushed=builder.push(
                registry_url=push_target,
                username=username,
                password=password,
                show_progress=not args.no_progress,
            )
        except ScanError as exc:
            parser.exit(3 if exc.category == "findings" else 4, f"{exc}\n")
        if getattr(args,"output_ref",None):
            write_reference(Path(args.output_ref),pushed)
            print("Wrote image reference to", args.output_ref)

if __name__=="__main__":
    main()
