#!/usr/bin/env python
"""Generate cli.json for the admin dashboard's CLI reference.

Introspects the argparse parser behind the ``mash`` command and the REPL
slash-command registry, then writes a static JSON document bundled into the
admin SPA. Run by ``make admin-cli-docs`` before the web build so the
reference stays in sync with the code.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mash.cli.commands import CommandRegistry
from mash.cli.default_commands import register_default_commands
from mash.cli.main import build_parser

OUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src/mash/api/web-admin/src/cli.json"
)


def _describe_arg(action: argparse.Action) -> dict[str, Any]:
    positional = not action.option_strings
    default = action.default
    if default is argparse.SUPPRESS:
        default = None
    return {
        "flags": list(action.option_strings) or [action.dest],
        "name": action.dest,
        "help": action.help or "",
        "positional": positional,
        "required": True if positional else bool(getattr(action, "required", False)),
        "default": default if isinstance(default, (str, int, float, bool)) else None,
        "choices": list(action.choices) if action.choices else None,
    }


def _describe_parser(parser: argparse.ArgumentParser) -> dict[str, Any]:
    args: list[dict[str, Any]] = []
    subcommands: list[dict[str, Any]] = []
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no public API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            help_by_name = {
                choice.dest: (choice.help or "")
                for choice in action._choices_actions  # noqa: SLF001
            }
            for name, sub in action.choices.items():
                subcommands.append(
                    {
                        "name": name,
                        "help": help_by_name.get(name, ""),
                        **_describe_parser(sub),
                    }
                )
        elif isinstance(action, argparse._HelpAction):  # noqa: SLF001
            continue
        else:
            args.append(_describe_arg(action))
    return {"args": args, "subcommands": subcommands}


class _RegistryShell:
    """Minimal stand-in so register_default_commands can register commands.

    Only ``command_registry`` is touched at registration time; the command
    handlers that reference other shell attributes are closures and never run
    here.
    """

    def __init__(self) -> None:
        self.command_registry = CommandRegistry(
            app_id="cli-docs", event_logger=None, session_id="cli-docs"
        )


def _repl_commands() -> list[dict[str, Any]]:
    shell = _RegistryShell()
    register_default_commands(shell)
    return [
        {"name": command.name, "help": command.help, "aliases": list(command.aliases)}
        for command in sorted(
            shell.command_registry.list_commands(), key=lambda c: c.name
        )
    ]


def build_docs() -> dict[str, Any]:
    parser = build_parser()
    tree = _describe_parser(parser)
    return {
        "cli": {
            "prog": "mash",
            "args": tree["args"],
            "commands": tree["subcommands"],
        },
        "repl": {"commands": _repl_commands()},
    }


def main() -> int:
    docs = build_docs()
    OUT_PATH.write_text(json.dumps(docs, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
