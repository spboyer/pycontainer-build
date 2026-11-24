import argparse, json, os, logging, sys
from contextlib import ExitStack
from pathlib import Path
from .config import SBOM_FORMATS, SCAN_SEVERITIES
from .config_loader import _validate_config, load_explicit_file_config
from .builder import ImageBuilder
from .config_loader import load_config_file, merge_configs
from .history import BuildHistory, HistoryStorageError
from .build_args import redact_values, resolve_arguments
from .effective_config import ConfigValidationError, NOT_PERFORMED, resolve_build_config
from .fs_utils import output_directory_lock
from .image_validation import (
    MIB, SIZE_THRESHOLDS, finding_exit_code, parse_size_threshold, validate_ignores, validate_image,
)
from .release_notes import DEFAULT_RELEASE_NOTES_LABEL, ReleaseNotesError
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

def _metadata_argument(value: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError("expected KEY=VALUE with a non-empty key")
    return key, item

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
        value = getattr(args, name, None)
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
    thresholds=None, base_layer_count=None, redact=str,
):
    try:
        report=validate_image(
            output, tag=tag, platform=platform, ignores=ignores, thresholds=thresholds,
            expected_digest=expected_digest, base_layer_count=base_layer_count,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {redact(str(exc))}", file=sys.stderr)
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
            table, config=load_explicit_file_config(Path(args.config))
            _validate_config(config, table)
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

def _tolerate_unencodable_output():
    # Redirected output on Windows defaults to a legacy code page that cannot encode status symbols.
    for stream in (sys.stdout, sys.stderr):
        encoding=getattr(stream,"encoding",None)
        reconfigure=getattr(stream,"reconfigure",None)
        if not encoding or reconfigure is None: continue
        try: "\u2713".encode(encoding)
        except (UnicodeEncodeError, LookupError): reconfigure(errors="replace")

def _run_registry(parser, args):
    from .blob_store import BlobIntegrityError
    from .registry_cleanup import RegistryConfirmationError, run_delete, run_prune
    from .registry_client import RegistryRequestError
    if args.username and not (args.password or os.getenv('REGISTRY_PASSWORD')):
        parser.error("--username requires --password or REGISTRY_PASSWORD.")
    if args.username and not args.password:
        args.password=os.getenv('REGISTRY_PASSWORD')
    if args.registry_cmd=="prune" and args.keep<1:
        parser.error("--keep must be at least 1.")
    if args.registry_cmd=="prune" and args.page_size is not None and args.page_size<1:
        parser.error("--page-size must be at least 1.")
    try:
        return run_delete(args) if args.registry_cmd=="delete" else run_prune(args)
    except ValueError as exc:
        parser.error(str(exc))
    except (RegistryRequestError, RegistryConfirmationError, BlobIntegrityError) as exc:
        print(f"Error: {exc}",file=sys.stderr)
        return 1

def _add_build_options(b,*,tagging=True,context=True,push=True,platform=True,validation=False):
    if not validation:
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
    b.add_argument("--registry-mirror",dest="registry_mirrors",action="append",metavar="UPSTREAM=MIRROR",help="Pull base images from UPSTREAM through MIRROR host[:port] first (repeatable, tried in order; pushes never use mirrors)")
    b.add_argument("--include",dest="include_paths",action="append",help="Include a context-root-relative file or directory (repeatable)")
    b.add_argument("--exclude",dest="exclude_paths",action="append",help="Exclude a context-root-relative path or glob (repeatable)")
    b.add_argument("--no-default-excludes",dest="exclude_defaults",action="store_false",default=None,help="Include normally excluded generated, test, virtualenv, and VCS paths")
    if not validation:
        b.add_argument("--no-progress",action="store_true",help="Suppress progress output")
    b.add_argument("--no-cache",dest="use_cache",action="store_false",default=None,help="Disable layer caching, force full rebuild")
    b.add_argument("--cache-dir",help="Custom cache directory (default: ~/.pycontainer/cache)")
    if not validation:
        history_options=b.add_mutually_exclusive_group()
        history_options.add_argument("--record-history",dest="record_history",action="store_true",default=None,help="Record private local build timing history")
        history_options.add_argument("--no-record-history",dest="record_history",action="store_false",default=None,help="Disable local build history")
        b.add_argument("--history-dir",help="Local history directory (default: ~/.pycontainer/history; does not enable recording)")
    b.add_argument(
        "--include-deps",
        action="store_true",
        default=None,
        help="Stage target-compatible dependencies from a lockfile, requirements file, or venv",
    )
    b.add_argument("--requirements",dest="requirements_file",help="Requirements file for dependency layer (takes precedence over auto-detected lockfiles and venvs)")
    b.add_argument("--lockfile",help="poetry.lock, Pipfile.lock, pdm.lock or uv.lock to stage dependencies from")
    b.add_argument("--no-lockfile",dest="use_lockfile",action="store_false",default=None,help="Ignore auto-detected lockfiles")
    b.add_argument("--strip-type-stubs",action="store_true",default=None,help="Remove .pyi files and stub-only distributions (types-*, *-stubs) from the dependency layer")
    if not validation:
        b.add_argument("--verbose","-v",action="store_true",default=None,help="Verbose output with detailed logging")
    if push:
        b.add_argument("--dry-run",action="store_true",default=None,help="Show build plan without building")
    if platform:
        b.add_argument("--platform",help="Target platform (e.g., linux/amd64), or a comma-separated list of linux/amd64, linux/arm64 and linux/arm/v7 to build an OCI image index")
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
    b.add_argument("--release-notes",dest="release_notes_file",metavar="FILE",help="Embed a UTF-8 release notes file (max 8 KiB after LF normalization) as an image label; relative to the build context")
    b.add_argument("--release-notes-label",dest="release_notes_label",metavar="KEY",help=f"Label key for --release-notes (default: {DEFAULT_RELEASE_NOTES_LABEL})")
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
    b=sub.add_parser("build",help="Build an OCI image layout")
    _add_build_options(b)
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
    _add_build_options(vc,push=False,validation=True)
    vc.set_defaults(config=None,record_history=None,history_dir=None,verbose=None)
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
    reg=sub.add_parser("registry",help="Delete or prune images in a registry")
    reg_sub=reg.add_subparsers(dest="registry_cmd",required=True)
    rd=reg_sub.add_parser("delete",help="Delete one manifest digest (removes every tag pointing to it)")
    rd.add_argument("reference",help="Image reference with an explicit tag or @sha256 digest")
    rp=reg_sub.add_parser("prune",help="Delete older semantic-version tags; dry run unless --execute")
    rp.add_argument("repository",help="Repository without a tag, e.g. ghcr.io/user/app")
    rp.add_argument("--keep",type=int,required=True,help="Number of highest semantic versions to keep (>= 1)")
    rp.add_argument("--match",default="*",help="Glob selecting tags to consider (default: *)")
    rp.add_argument("--protect",action="append",help="Glob of tags never pruned (repeatable)")
    rp.add_argument("--page-size",type=int,help="Tags requested per page (registry may cap it)")
    mode=rp.add_mutually_exclusive_group()
    mode.add_argument("--dry-run",action="store_true",help="Only print the plan (default)")
    mode.add_argument("--execute",action="store_true",help="Delete the planned digests after confirmation")
    for command in (rd, rp):
        command.add_argument("--yes",action="store_true",help="Confirm deletion without prompting")
        command.add_argument("--username",help="Registry username (or use REGISTRY_USERNAME env var)")
        command.add_argument("--password",help="Registry password/token (or use env vars)")
    rd.add_argument("--dry-run",action="store_true",help="Resolve and print the digest without deleting")
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

    if args.cmd=="registry":
        raise SystemExit(_run_registry(parser,args))
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

    if args.cmd=="validate" and args.validate_cmd=="image":
        _validate_image_command(vi, args)
        return

    if args.cmd=="build" and args.ignore and not args.validate:
        b.error("--ignore requires --validate.")
    if args.cmd=="build":
        for name in SIZE_THRESHOLDS:
            if getattr(args, name) is not None and not args.validate:
                b.error(f"--{name.replace('_', '-')} requires --validate.")
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
    push=devloop or getattr(args,"push",False)
    username=getattr(args,"username",None)
    password=getattr(args,"password",None)
    if push and username and not (password or os.getenv('REGISTRY_PASSWORD')):
        parser.error("--username requires --password or REGISTRY_PASSWORD.")

    registry_mirrors=None
    if args.registry_mirrors:
        registry_mirrors={}
        for entry in args.registry_mirrors:
            upstream,separator,mirror=entry.partition("=")
            if not separator or not upstream or not mirror or "=" in mirror:
                parser.error(f"Invalid --registry-mirror {entry!r}: expected UPSTREAM=MIRROR, e.g. docker.io=mirror.example.com.")
            registry_mirrors.setdefault(upstream,[]).append(mirror)

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
        'record_history': args.record_history,
        'history_dir': args.history_dir,
        'include_deps': args.include_deps,
        'requirements_file': args.requirements_file,
        'lockfile': args.lockfile,
        'use_lockfile': args.use_lockfile,
        'strip_type_stubs': args.strip_type_stubs,
        'verbose': args.verbose,
        'dry_run': getattr(args,"dry_run",None),
        'platform': getattr(args,"platform",None),
        'reproducible': args.reproducible,
        'generate_sbom': args.generate_sbom,
        'system_packages': args.system_packages,
        'detect_system_packages': args.detect_system_packages,
        'labels': dict(args.labels) if args.labels is not None else None,
        'manifest_annotations': dict(args.manifest_annotations) if args.manifest_annotations is not None else None,
        'index_annotations': dict(args.index_annotations) if args.index_annotations is not None else None,
        'release_notes_file': args.release_notes_file,
        'release_notes_label': args.release_notes_label,
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
        'registry_mirrors': registry_mirrors,
    }
    cli_overrides={k:v for k,v in cli_overrides.items() if v is not None}

    if args.cmd=="validate":
        if args.config_paths and (len(args.config_paths)>1 or args.paths):
            vc.error("use positional paths or a single --config path, not both.")
        sources=args.config_paths or args.paths or [None]
        invalid=0
        for path in sources:
            label=path if path is not None else "defaults (no configuration file)"
            try:
                cfg,table=resolve_build_config(Path(path) if path is not None else None,cli_overrides,args.build_arg)
            except (OSError,ValueError) as exc:
                invalid+=1
                if isinstance(exc,ConfigValidationError):
                    label=exc.redact(label)
                for line in str(exc).splitlines():
                    print(f"error: {label}: {line}",file=sys.stderr)
            else:
                print(f"ok: {cfg.redact(label)}"+(f" {table}" if table else ""))
        print("Not performed (offline validation does not prove a successful build):")
        for check in NOT_PERFORMED:
            print(f"  - {check}")
        if invalid:
            print(f"{invalid} of {len(sources)} configuration(s) invalid.",file=sys.stderr)
            raise SystemExit(2)
        return

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
        cfg,_=resolve_build_config(Path(args.config) if args.config else None,cli_overrides,args.build_arg)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    validate = args.cmd=="build" and args.validate
    if validate and cfg.dry_run:
        b.error("--validate cannot be used with --dry-run.")
    ignores=[*cfg.validate.get("ignore", []), *(getattr(args,"ignore",None) or [])]
    try:
        validate_ignores(ignores)
    except ValueError as exc:
        b.error(str(exc))
    thresholds=_size_thresholds(b, args, cfg.validate)
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

    from .scanning import ScanError
    log_filter = _BuildArgumentLogFilter(cfg)
    handlers = list(logging.getLogger().handlers) if cfg.build_args else []
    for handler in handlers:
        handler.addFilter(log_filter)
    try:
        try:
            builder=ImageBuilder(cfg)
            out=builder.build()
        except ReleaseNotesError as exc:
            parser.error(cfg.redact(str(exc)))
        except HistoryStorageError as exc:
            parser.exit(1,f"Build history error: {cfg.redact(str(exc))}\n")
        print("Built:", cfg.redact(str(out)))

        with ExitStack() as stack:
            if validate:
                try:
                    stack.enter_context(output_directory_lock(builder.config.output_dir))
                except (OSError, RuntimeError) as exc:
                    print(f"error: {cfg.redact(str(exc))}", file=sys.stderr)
                    raise SystemExit(2)
                images = builder.platform_images
                multi = len(images) > 1
                for image in images:
                    platform = "/".join(image.platform[key] for key in ("os", "architecture") if key in image.platform)
                    if image.platform.get("variant"):
                        platform += "/" + image.platform["variant"]
                    code=_image_validation(
                        Path(builder.config.output_dir),
                        "@" + builder.manifest_digest if multi else builder.config.tag,
                        platform, ignores, "text", "warning", expected_digest=image.manifest_digest,
                        thresholds=thresholds,
                        base_layer_count=len(image.layers) - image.app_layer_count,
                        redact=cfg.redact,
                    )
                    if code:
                        raise SystemExit(code)

            if push:
                if username and not password:
                    password=os.getenv('REGISTRY_PASSWORD')
                if args.cmd=="tilt-build" and push_target is None:
                    push_target=f"{args.image}:{tilt_content_tag(builder.manifest_digest)}"
                push_options=dict(
                    registry_url=push_target,
                    username=username,
                    password=password,
                    show_progress=not args.no_progress,
                )
                if validate and cfg.scan:
                    push_options["_output_locked"]=True
                pushed=builder.push(**push_options)
                if getattr(args,"output_ref",None):
                    write_reference(Path(args.output_ref),pushed)
                    print("Wrote image reference to", cfg.redact(str(args.output_ref)))
    except ScanError as exc:
        parser.exit(3 if exc.category == "findings" else 4, f"{cfg.redact(str(exc))}\n")
    except (OSError, ValueError, RuntimeError) as exc:
        if not cfg.build_args:
            raise
        parser.error(cfg.redact(str(exc)))
    finally:
        for handler in handlers:
            handler.removeFilter(log_filter)

if __name__=="__main__":
    main()
