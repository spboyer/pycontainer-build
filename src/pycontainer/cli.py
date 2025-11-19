import argparse
from pathlib import Path
import tomllib
from .config import BuildConfig
from .builder import ImageBuilder

def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="cmd",required=True)
    b=sub.add_parser("build")
    b.add_argument("--tag"); b.add_argument("--context",default=".")
    args=parser.parse_args()

    tag=args.tag or "local/test:latest"
    cfg=BuildConfig(tag=tag, context_dir=args.context)
    builder=ImageBuilder(cfg)
    out=builder.build()
    print("Built:", out)

if __name__=="__main__":
    main()
