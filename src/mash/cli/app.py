"""``App``: a client for one Mash app, built from the ``mash`` command line.

An app that runs on Mash ships its own command with a few lines::

    from mash.cli import App

    app = App(name="fudge", default_host="fudge", env_prefix="FUDGE")

    def main() -> int:
        return app.main()

``mash`` itself is ``App(name="mash", operator_commands=True)``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .commands import Command
from .render import ToolLine


@dataclass(frozen=True)
class App:
    """What one app's client is called, where it connects and how it looks."""

    # The program name, the REPL's header line and the input history file.
    name: str
    description: str = ""
    # The target when no --host or --agent is given. At most one of the two.
    default_host: str | None = None
    default_agent: str | None = None
    # The deployment when no flag or environment variable names one.
    default_base_url: str | None = None
    # "FUDGE" reads FUDGE_API_BASE_URL, FUDGE_API_KEY and FUDGE_USER_ID
    # before the MASH_ ones.
    env_prefix: str | None = None
    # The user when no flag, variable or saved connection names one.
    default_user: str | Callable[[], str] | None = None
    # Style names to colors, over ``render.DEFAULT_THEME``.
    theme: Mapping[str, str] | None = None
    # Slash commands added to the REPL's own.
    commands: Sequence[Command] = ()
    # Tool name -> (arguments) -> the line shown for a call of that tool.
    tool_lines: Mapping[str, ToolLine] | None = None
    # Workflow id -> (result) -> None, which prints that workflow's result.
    workflow_renderers: Mapping[str, Callable[[dict[str, Any]], None]] | None = None
    # host serve, agent serve, sandbox, browser and compose: for whoever runs
    # the deployment, not for its users.
    operator_commands: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("an App needs a name")
        if self.default_host and self.default_agent:
            raise ValueError("an App takes default_host or default_agent, not both")

    def env(self, name: str) -> str | None:
        """``<PREFIX>_<name>`` when the app has a prefix and it is set, else ``MASH_<name>``."""
        prefixes = [self.env_prefix, "MASH"] if self.env_prefix else ["MASH"]
        for prefix in prefixes:
            value = os.environ.get(f"{prefix}_{name}", "").strip()
            if value:
                return value
        return None

    def user(self) -> str | None:
        """The app's default user."""
        user = self.default_user() if callable(self.default_user) else self.default_user
        return (user or "").strip() or None

    def main(self, argv: Sequence[str] | None = None) -> int:
        """Run the app's command line; returns the exit status."""
        from .main import main

        return main(argv, app=self)
