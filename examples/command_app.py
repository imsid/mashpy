"""Mash CLI example with tools, skills, and a custom slash command."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from mash_cli import CLIAppShell, Command

from .app_definition import HelloMashDefinition

APP_ID = "hello-mash-command"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Mash SDK example app with a custom slash command."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Working directory for sqlite/log files and BashTool (default: current dir).",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    definition = HelloMashDefinition(root=root, app_id=APP_ID)
    shell = CLIAppShell.from_definition(definition)
    shell.register_command(
        Command(
            name="workspace",
            help="Show the workspace folder used by BashTool",
            handler=lambda ctx, _args: ctx.renderer.info(f"Workspace: {root}"),
        )
    )
    try:
        shell.run()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        shell.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
