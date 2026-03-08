"""Mash CLI example with tools and skills enabled."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from mash_cli import CLIAppShell

from .app_definition import HelloMashDefinition


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Mash SDK example app with tools and skills enabled."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Working directory for sqlite/log files and BashTool (default: current dir).",
    )
    args = parser.parse_args(argv)

    definition = HelloMashDefinition(root=Path(args.root).resolve())
    shell = CLIAppShell.from_definition(definition)
    try:
        shell.run()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        shell.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
