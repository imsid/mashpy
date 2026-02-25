"""Thin public CLI for package installation validation."""

from __future__ import annotations

import argparse
from typing import Sequence

from mash import __version__, get_docs_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mash",
        description="MashPy framework CLI.",
        epilog=f"Documentation: {get_docs_url()}",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show installed mashpy version and documentation URL.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"mashpy {__version__}")
        print(f"Docs: {get_docs_url()}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
