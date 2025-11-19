import argparse, os, logging
from pathlib import Path
from .config import BuildConfig
from .builder import ImageBuilder
from .config_loader import config_from_file

def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="cmd",required=True)
    b=sub.add_parser("build")
    b.add_argument("--config","-c",help="Path to pycontainer.toml config file")
    b.add_argument("--tag")
    b.add_argument("--base-image",help="Base image to layer on (e.g., python:3.11-slim)")
    b.add_argument("--context",default=".")
    b.add_argument("--push",action="store_true",help="Push image to registry after build")
    b.add_argument("--registry",help="Override registry from tag (e.g., ghcr.io/user/repo:v1)")
    b.add_argument("--username",help="Registry username (or use REGISTRY_USERNAME env var)")
    b.add_argument("--password",help="Registry password/token (or use env vars)")
    b.add_argument("--no-progress",action="store_true",help="Suppress progress output")
    b.add_argument("--no-cache",action="store_true",help="Disable layer caching, force full rebuild")
    b.add_argument("--cache-dir",help="Custom cache directory (default: ~/.pycontainer/cache)")
    b.add_argument("--include-deps",action="store_true",help="Include dependencies from venv or requirements.txt")
    b.add_argument("--requirements",default="requirements.txt",help="Requirements file for dependency layer")
    b.add_argument("--verbose","-v",action="store_true",help="Verbose output with detailed logging")
    b.add_argument("--dry-run",action="store_true",help="Show build plan without building")
    b.add_argument("--platform",default="linux/amd64",help="Target platform (e.g., linux/amd64, linux/arm64)")
    b.add_argument("--no-reproducible",action="store_true",help="Disable reproducible builds")
    b.add_argument("--sbom",action="store_true",help="Generate SBOM (Software Bill of Materials)")
    args=parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s')

    if args.config:
        cli_overrides={
            'tag': args.tag,
            'base_image': args.base_image,
            'context_dir': args.context,
            'use_cache': not args.no_cache if args.no_cache else None,
            'cache_dir': args.cache_dir,
            'include_deps': args.include_deps,
            'requirements_file': args.requirements,
            'verbose': args.verbose,
            'dry_run': args.dry_run,
            'platform': args.platform,
            'reproducible': not args.no_reproducible if args.no_reproducible else None,
            'generate_sbom': args.sbom
        }
        cli_overrides={k:v for k,v in cli_overrides.items() if v is not None}
        cfg=config_from_file(Path(args.config), cli_overrides)
    else:
        tag=args.tag or "local/test:latest"
        cfg=BuildConfig(
            tag=tag, 
            base_image=args.base_image,
            context_dir=args.context,
            use_cache=not args.no_cache,
            cache_dir=args.cache_dir,
            include_deps=args.include_deps,
            requirements_file=args.requirements,
            verbose=args.verbose,
            dry_run=args.dry_run,
            platform=args.platform,
            reproducible=not args.no_reproducible
        )
        cfg.generate_sbom=args.sbom
    builder=ImageBuilder(cfg)
    out=builder.build()
    print("Built:", out)
    
    if args.push:
        auth_token=args.password or os.getenv('GITHUB_TOKEN') or os.getenv('REGISTRY_TOKEN')
        username=args.username or os.getenv('REGISTRY_USERNAME')
        builder.push(registry_url=args.registry, auth_token=auth_token, username=username, show_progress=not args.no_progress)

if __name__=="__main__":
    main()
