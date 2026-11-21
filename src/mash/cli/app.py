"""``App``: a client for one Mash app, built from the ``mash`` command line.

An app that runs on Mash ships its own command with a few lines::

    from mash.cli import App

    app = App(name="fudge", default_host="fudge", env_prefix="FUDGE")

    def main() -> int:
        return app.main()

``mash`` itself is ``App(name="mash", operator_commands=True)``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from dotenv import find_dotenv, load_dotenv

from mash import __version__, get_docs_url
from mash.agents.companion import COMPANION_AGENT_ID
from mash.api.main import add_agent_serve_parser, add_serve_parser
from mash.cli.browser import add_browser_parser
from mash.cli.sandbox import add_sandbox_parser

from .ask import menu
from .client import MashHostClient
from .commands import Command
from .config import CLIConfig, load_config, save_config
from .oneshot import read_message, run_once
from .render import RichRenderer, ToolLine
from .session import Repl, Target
from .sessions import PICKER_HINT, PICKER_LIMIT, session_options, session_title


@dataclass(frozen=True)
class App:
    """What one app's client is called, where it connects and how it looks."""

    # The program name, the REPL's header line and the input history file.
    name: str
    description: str = ""
    # The host when no --host is given.
    default_host: str | None = None
    # The deployment when no flag or environment variable names one.
    default_base_url: str | None = None
    # "FUDGE" reads FUDGE_API_BASE_URL, FUDGE_API_KEY, FUDGE_APP_ID and
    # FUDGE_POLICY_ID before the MASH_ ones.
    env_prefix: str | None = None
    # The app and the policy when no flag, variable or saved connection names
    # one: requests and workflow runs act for the app under the policy.
    default_app: str | Callable[[], str] | None = None
    default_policy: str | Callable[[], str] | None = None
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

    def env(self, name: str) -> str | None:
        """``<PREFIX>_<name>`` when the app has a prefix and it is set, else ``MASH_<name>``."""
        prefixes = [self.env_prefix, "MASH"] if self.env_prefix else ["MASH"]
        for prefix in prefixes:
            value = os.environ.get(f"{prefix}_{name}", "").strip()
            if value:
                return value
        return None

    def app_id(self) -> str | None:
        """The app id requests act for by default."""
        return _resolved(self.default_app)

    def policy_id(self) -> str | None:
        """The policy requests run under by default."""
        return _resolved(self.default_policy)


    def main(self, argv: Sequence[str] | None = None) -> int:
        """Run the app's command line; returns the exit status."""
        return main(argv, app=self)


def _resolved(value: str | Callable[[], str] | None) -> str | None:
    text = value() if callable(value) else value
    return (text or "").strip() or None

# The `mash` command line: every command, and the saved connection alone.
MASH = App(name="mash", operator_commands=True)


def _print_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


def _connection(
    args: argparse.Namespace, app: App
) -> tuple[str, str | None, CLIConfig | None]:
    """The deployment's URL, its API key, and the saved connection when it applies.

    A flag wins over the environment, which wins over the app's default URL,
    which wins over the connection ``connect`` saved. The saved
    connection applies only when it was saved for the resolved URL, so a
    key saved for one deployment is never sent to another.
    """
    saved = load_config()
    base_url = (
        getattr(args, "api_base_url", None)
        or app.env("API_BASE_URL")
        or app.default_base_url
        or (saved.api_base_url if saved else "")
    ).strip()
    if (
        saved
        and saved.api_base_url.rstrip("/") != base_url.rstrip("/")
    ):
        saved = None
    api_key = (
        getattr(args, "api_key", None)
        or app.env("API_KEY")
        or (saved.api_key if saved else None)
    )
    return base_url, api_key, saved


def _identity(
    args: argparse.Namespace, app: App, saved: CLIConfig | None
) -> tuple[str | None, str | None]:
    """The app and policy: a flag, then the environment, the saved connection, the App."""
    app_id = (
        getattr(args, "app_id", None)
        or app.env("APP_ID")
        or (saved.app_id if saved else None)
        or app.app_id()
    )
    policy_id = (
        getattr(args, "policy_id", None)
        or app.env("POLICY_ID")
        or (saved.policy_id if saved else None)
        or app.policy_id()
    )
    return app_id, policy_id


def _require_identity(app: App, app_id: str | None, policy_id: str | None) -> tuple[str, str]:
    if not app_id or not policy_id:
        raise ValueError(
            "an app id and a policy id are required. Use --app-id and --policy-id, "
            f"or `{app.name} connect --app-id <app> --policy-id <policy>`."
        )
    return app_id, policy_id


def _resolve_connection(
    args: argparse.Namespace, app: App
) -> tuple[str, str | None, str | None, str | None, str | None, str | None]:
    base_url, api_key, saved = _connection(args, app)
    agent_id = getattr(args, "agent", None)
    host_id = getattr(args, "host_id", None)
    if not host_id:
        # The app's own host, else the host `compose` pinned.
        host_id = app.default_host or (saved.host_id if saved else None)
    app_id, policy_id = _identity(args, app, saved)
    if not base_url:
        raise ValueError(
            f"API base URL is required. Use --api-base-url or `{app.name} connect`."
        )
    return base_url, api_key, agent_id, host_id, app_id, policy_id


def _build_repl(
    app: App, client: MashHostClient, target: Target, *, debug: bool = False
) -> Repl:
    """A REPL with the app's name, theme, tool lines, commands and renderers."""
    repl = Repl(
        client,
        target,
        debug=debug,
        app_name=app.name,
        theme=app.theme,
        tool_lines=app.tool_lines,
    )
    for command in app.commands:
        repl.command_registry.register(command)
    for workflow_id, render in (app.workflow_renderers or {}).items():
        repl.register_workflow_result_renderer(workflow_id, render)
    return repl


def _run_workflow_command(
    client: MashHostClient, args: argparse.Namespace, target: Target, app: App
) -> int:
    """``mash workflow ...``: the REPL's ``/workflow`` command, run once.

    Returns 1 when the command printed an error.
    """
    words = [args.workflow_command]
    if args.workflow_command == "run":
        words.append(args.workflow_id)
        if args.dedup_key:
            words.append(args.dedup_key)
        if args.no_follow:
            words.append("--no-follow")
        if args.input is not None:
            raw = args.input
            if raw.startswith("@"):
                with open(raw[1:], encoding="utf-8") as handle:
                    raw = handle.read()
            words.extend(["--input", raw.strip()])
    elif args.workflow_command in ("status", "resume"):
        words.extend([args.workflow_id, args.run_id])
    repl = _build_repl(app, client, target)
    failed = False
    report = repl.renderer.error

    def error(text: str) -> None:
        nonlocal failed
        failed = True
        report(text)

    repl.renderer.error = error  # type: ignore[method-assign]
    command = repl.command_registry.get("workflow")
    assert command is not None
    command.handler(repl.context, words)
    return 1 if failed else 0


def _resolve_host(client: MashHostClient, host_id: str | None) -> str:
    """The host to post events to: the one given, or the deployment's only one."""
    if host_id:
        client.get_host(host_id)
        return host_id
    hosts = (client.health().get("deployment") or {}).get("hosts") or []
    if len(hosts) == 1 and isinstance(hosts[0], dict) and hosts[0].get("host_id"):
        return str(hosts[0]["host_id"])
    raise ValueError(
        "a host is required: use --host, or `mash compose` to define one and target it"
    )


def _resolve_target(
    client: MashHostClient,
    explicit_agent: str | None,
    host_id: str | None,
) -> tuple[str, str | None]:
    """Resolve (agent_id, host_id) for a command.

    An explicit agent targets the bare agent; a host targets the companion,
    which routes every host request, with the host composition wired in.
    """
    if explicit_agent:
        return explicit_agent, None
    if host_id:
        client.get_host(host_id)
        return COMPANION_AGENT_ID, host_id
    health = client.health()
    deployment = health.get("deployment") or {}
    hosts = deployment.get("hosts") or []
    if len(hosts) == 1 and isinstance(hosts[0], dict):
        resolved_host_id = str(hosts[0].get("host_id") or "").strip()
        if resolved_host_id:
            return COMPANION_AGENT_ID, resolved_host_id
    agents = deployment.get("agents") or []
    if len(agents) == 1 and isinstance(agents[0], dict):
        agent_id = str(agents[0].get("agent_id") or "").strip()
        if agent_id:
            return agent_id, None
    raise ValueError(
        "could not resolve a target; specify --agent or --host"
    )


def _split_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _resolve_saved_connection(
    args: argparse.Namespace,
    renderer: RichRenderer,
    app: App,
) -> tuple[str, str | None] | None:
    base_url, api_key, _ = _connection(args, app)
    if not base_url:
        renderer.error(
            f"API base URL is required. Use --api-base-url or `{app.name} connect`."
        )
        return None
    return base_url, api_key


def _agent_rows(agents: list[dict]) -> list[list[str]]:
    rows: list[list[str]] = []
    for agent in sorted(agents, key=lambda a: str(a.get("agent_id") or "")):
        metadata = agent.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        rows.append(
            [
                str(agent.get("agent_id") or ""),
                str(metadata.get("display_name") or ""),
                str(metadata.get("description") or ""),
            ]
        )
    return rows


def _workflow_rows(workflows: list[dict]) -> list[list[str]]:
    rows: list[list[str]] = []
    for workflow in sorted(workflows, key=lambda w: str(w.get("workflow_id") or "")):
        rendered_steps = []
        for step in workflow.get("step_preview") or []:
            if isinstance(step, dict):
                rendered_steps.append(
                    f"{step.get('step_id') or ''} ({step.get('kind') or ''})"
                )
        rows.append([str(workflow.get("workflow_id") or ""), ", ".join(rendered_steps)])
    return rows


def _host_rows(hosts: list[dict]) -> list[list[str]]:
    return [
        [
            str(host.get("host_id") or ""),
            ", ".join(host.get("agents") or []),
            ", ".join(host.get("workflows") or []),
        ]
        for host in hosts
    ]


def _run_browse(client: MashHostClient, renderer: RichRenderer) -> int:
    renderer.info("Agent pool")
    renderer.table(["Agent", "Name", "Description"], _agent_rows(client.list_agents()))

    renderer.info("Workflows (attach to a host with `mash compose ... --workflows <id>`)")
    workflow_rows = _workflow_rows(client.list_workflows())
    if workflow_rows:
        renderer.table(["Workflow", "Steps"], workflow_rows)
    else:
        renderer.info("(none registered)")

    renderer.info("Hosts")
    host_rows = _host_rows(client.list_hosts())
    if host_rows:
        renderer.table(["Host", "Agents", "Workflows"], host_rows)
    else:
        renderer.info("(no hosts defined)")

    renderer.info(
        "Compose a host with `mash compose --host <id> --agents a,b`, then "
        "enter it with `mash repl`."
    )
    return 0


def _render_host_view(renderer: RichRenderer, described: dict) -> None:
    members = ", ".join(item.get("agent_id", "") for item in described.get("agents") or [])
    workflows = ", ".join(described.get("workflows") or [])
    renderer.info(
        f"Host '{described.get('host_id')}': agents [{members}]"
        + (f", workflows [{workflows}]" if workflows else "")
    )


def _run_connect(args: argparse.Namespace, app: App) -> int:
    renderer = RichRenderer(theme=app.theme)
    connection = _resolve_saved_connection(args, renderer, app)
    if connection is None:
        return 1
    base_url, api_key = connection

    # Validate the connection before saving so a wrong URL or key fails here,
    # not on the next command.
    client = MashHostClient(base_url, api_key=api_key)
    try:
        client.health()
    except Exception as exc:
        renderer.error(f"could not connect to {base_url}: {exc}")
        return 1
    finally:
        client.close()

    _, _, saved = _connection(args, app)
    app_id, policy_id = _identity(args, app, saved)
    path = save_config(
        CLIConfig(
            api_base_url=base_url, api_key=api_key, app_id=app_id, policy_id=policy_id
        )
    )
    print(f"Saved connection to {path}")
    return 0


def _run_compose(args: argparse.Namespace, app: App) -> int:
    renderer = RichRenderer(theme=app.theme)
    connection = _resolve_saved_connection(args, renderer, app)
    if connection is None:
        return 1
    base_url, api_key = connection

    client = MashHostClient(base_url, api_key=api_key)
    try:
        described = client.define_host(
            args.host_id,
            agents=_split_ids(args.agents),
            workflows=_split_ids(args.workflows),
        )
    except Exception as exc:
        renderer.error(str(exc))
        return 1
    finally:
        client.close()
    _render_host_view(renderer, described)

    # Pin the composition as the current target.
    _, _, saved = _connection(args, app)
    path = save_config(
        CLIConfig(
            api_base_url=base_url,
            api_key=api_key,
            host_id=args.host_id,
            app_id=saved.app_id if saved else None,
            policy_id=saved.policy_id if saved else None,
        )
    )
    print(f"Saved connection to {path}")
    return 0


def _run_policy(args: argparse.Namespace, app: App) -> int:
    """``policy put <id> --file <path>`` and ``policy get <id>``: print the policy."""
    renderer = RichRenderer(theme=app.theme)
    connection = _resolve_saved_connection(args, renderer, app)
    if connection is None:
        return 1
    base_url, api_key = connection
    client = MashHostClient(base_url, api_key=api_key)
    try:
        if args.policy_command == "put":
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
            document = json.loads(text)
            if not isinstance(document, dict):
                raise ValueError("the policy file must hold a JSON object")
            policy = client.put_policy(args.policy_id, document)
        else:
            policy = client.get_policy(args.policy_id)
    except Exception as exc:
        renderer.error(str(exc))
        return 1
    finally:
        client.close()
    print(json.dumps(policy, indent=2))
    return 0


def build_parser(app: App = MASH) -> argparse.ArgumentParser:
    mash = app is MASH
    parser = argparse.ArgumentParser(
        prog=app.name,
        description=app.description
        or (
            "Mash CLI for self-hosted deployments and host serving."
            if mash
            else f"{app.name}: a client for a Mash deployment."
        ),
        epilog=f"Documentation: {get_docs_url()}" if mash else None,
    )
    parser.add_argument("--version", action="store_true", help="Show installed mash version and documentation URL.")
    subparsers = parser.add_subparsers(dest="command")

    connect = subparsers.add_parser(
        "connect",
        help="Validate and persist a deployment connection",
    )
    connect.add_argument(
        "--api-base-url",
        default=None,
        help="Mash host base URL, e.g. http://127.0.0.1:8000 (falls back to saved config)",
    )
    connect.add_argument("--api-key", default=None, help="Optional bearer API key")
    connect.add_argument(
        "--app-id",
        default=None,
        help="App that requests and workflow runs act for (falls back to saved config)",
    )
    connect.add_argument(
        "--policy-id",
        default=None,
        help="Policy that requests and workflow runs run under (falls back to saved config)",
    )

    compose = (
        subparsers if app.operator_commands else argparse.ArgumentParser().add_subparsers()
    ).add_parser(
        "compose",
        help="Define a host composition on the deployment and target it",
    )
    compose.add_argument(
        "--api-base-url",
        default=None,
        help="Mash host base URL (falls back to saved config)",
    )
    compose.add_argument("--api-key", default=None, help="Optional bearer API key")
    compose.add_argument("--host", dest="host_id", required=True, help="Host id to define or replace")
    compose.add_argument(
        "--agents",
        default=None,
        help="Comma-separated agent ids the companion routes to",
    )
    compose.add_argument(
        "--workflows",
        default=None,
        help="Comma-separated workflow ids",
    )

    common = argparse.ArgumentParser(add_help=False)
    # Every command's connection and identity; `on_host` (repl, run) talks
    # to a host, the others may also name an agent to read.
    on_host = argparse.ArgumentParser(add_help=False)
    on_host.add_argument("--api-base-url", default=None, help="Mash host base URL")
    on_host.add_argument("--api-key", default=None, help="Bearer API key")
    on_host.add_argument("--host", dest="host_id", default=None, help="Target host id")
    on_host.add_argument(
        "--app-id", default=None, help="App to act for (overrides the saved connection's)"
    )
    on_host.add_argument(
        "--policy-id",
        default=None,
        help="Policy to run under (overrides the saved connection's)",
    )
    common = argparse.ArgumentParser(add_help=False, parents=[on_host])
    common.add_argument("--agent", default=None, help="Agent whose sessions to read")

    subparsers.add_parser("status", parents=[common], help="Show deployment status")
    subparsers.add_parser(
        "browse",
        parents=[common],
        help="Browse the pool: agents, workflows, and hosts",
    )
    subparsers.add_parser("agents", parents=[common], help="List deployment agents")
    subparsers.add_parser("workflows", parents=[common], help="List deployment workflows")
    subparsers.add_parser("hosts", parents=[common], help="List defined hosts")

    subparsers.add_parser("sessions", parents=[common], help="List sessions for an agent")

    history = subparsers.add_parser("history", parents=[common], help="Show session history")
    history.add_argument("--session-id", required=True, help="Remote session id")
    history.add_argument("--limit", type=int, default=None, help="Optional turn limit")

    repl = subparsers.add_parser("repl", parents=[on_host], help="Start an interactive remote REPL")
    session = repl.add_mutually_exclusive_group()
    session.add_argument("--session-id", default=None, help="Remote session id")
    session.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Go on in your most recent session",
    )
    session.add_argument(
        "--resume",
        action="store_true",
        help="Choose one of your earlier sessions to go on in",
    )
    repl.add_argument(
        "--debug",
        action="store_true",
        help="Start at the detail level: each model turn, its thinking, and tool results",
    )

    run = subparsers.add_parser(
        "run",
        parents=[on_host],
        help="Submit one request, print its answer and exit",
        description=(
            "Submit one request, print its answer on stdout and exit. Exit status: "
            "0 completed, 1 failed, 2 the request asked for input (it is cancelled), "
            "130 interrupted."
        ),
    )
    run.add_argument("message", nargs="?", default=None, help="The message; read from stdin when omitted")
    run_session = run.add_mutually_exclusive_group()
    run_session.add_argument("--session-id", default=None, help="Remote session id")
    run_session.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Go on in your most recent session",
    )
    run.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Print each stream event as one JSON object per line",
    )

    workflow = subparsers.add_parser(
        "workflow", parents=[common], help="Run, inspect and resume workflow runs"
    )
    workflow_subparsers = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_subparsers.add_parser("list", help="List the workflows you can run")
    workflow_run = workflow_subparsers.add_parser(
        "run", help="Start a run and follow its steps to the end"
    )
    workflow_run.add_argument("workflow_id")
    workflow_run.add_argument(
        "--input", default=None, help="The workflow's input: a JSON object, or @file to read one"
    )
    workflow_run.add_argument("--dedup-key", default=None, help="A key that names the run")
    workflow_run.add_argument(
        "--no-follow", action="store_true", help="Print the run id and exit without following"
    )
    for name, text in (("status", "Show a run's status, steps and result"),
                       ("resume", "Resume a failed run from its failed step")):
        sub = workflow_subparsers.add_parser(name, help=text)
        sub.add_argument("workflow_id")
        sub.add_argument("run_id")

    if not app.operator_commands:
        return parser

    host = subparsers.add_parser("host", help="Host management commands")
    host.set_defaults(handler=lambda _: _print_help(host))
    host_subparsers = host.add_subparsers(dest="host_command")
    add_serve_parser(host_subparsers)

    agent = subparsers.add_parser("agent", help="Agent process commands")
    agent.set_defaults(handler=lambda _: _print_help(agent))
    agent_subparsers = agent.add_subparsers(dest="agent_command")
    add_agent_serve_parser(agent_subparsers)

    policy = subparsers.add_parser(
        "policy", help="Create, replace and read the deployment's policies"
    )
    policy.add_argument("--api-base-url", default=None, help="Mash host base URL")
    policy.add_argument("--api-key", default=None, help="Bearer API key")
    policy.set_defaults(handler=lambda args: _run_policy(args, app))
    policy_subparsers = policy.add_subparsers(dest="policy_command", required=True)
    policy_put = policy_subparsers.add_parser(
        "put", help="Create a policy, or replace the whole of an existing one"
    )
    policy_put.add_argument("policy_id")
    policy_put.add_argument(
        "--file", required=True, help="The policy as a JSON object, or - to read stdin"
    )
    policy_get = policy_subparsers.add_parser("get", help="Print a policy")
    policy_get.add_argument("policy_id")

    add_sandbox_parser(subparsers)
    add_browser_parser(subparsers)
    return parser


def load_env_file(argv: Sequence[str]) -> None:
    """Load the nearest ``.env`` from the working directory up; set variables win.

    ``mash agent serve`` is the exception: an agent process runs in the host's
    working directory but takes its environment from the host, which leaves
    out the host secret and the API key. Reading ``.env`` again would put
    them back.
    """
    if list(argv[:2]) == ["agent", "serve"]:
        return
    load_dotenv(find_dotenv(usecwd=True))


def main(argv: Sequence[str] | None = None, *, app: App = MASH) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Before the parser: its defaults read the environment.
    load_env_file(argv)
    parser = build_parser(app)
    args = parser.parse_args(argv)

    if args.version:
        if app is MASH:
            print(f"mash {__version__}")
            print(f"Docs: {get_docs_url()}")
        else:
            print(f"{app.name} (mash {__version__})")
        return 0

    if args.command is None and app is not MASH:
        # App clients open on their REPL; a deployment can resolve the target.
        args = parser.parse_args([*argv, "repl"])

    if args.command == "connect":
        return _run_connect(args, app)

    if args.command == "compose":
        return _run_compose(args, app)

    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is not None:
        return int(handler(args))

    if args.command is None:
        parser.print_help()
        return 0

    renderer = RichRenderer(theme=app.theme)
    try:
        (
            base_url,
            api_key,
            configured_agent,
            configured_host,
            app_id,
            policy_id,
        ) = _resolve_connection(args, app)
    except ValueError as exc:
        renderer.error(str(exc))
        return 1

    client = MashHostClient(base_url, api_key=api_key)
    try:
        if args.command == "browse":
            return _run_browse(client, renderer)

        if args.command == "status":
            health = client.health()
            deployment = health.get("deployment") or {}
            hosts = deployment.get("hosts") or []
            renderer.info(f"Deployment: {base_url}")
            renderer.info(f"Agents: {len(deployment.get('agents') or [])}")
            renderer.info(f"Hosts: {len(hosts)}")
            for host in hosts:
                if not isinstance(host, dict):
                    continue
                agents = ", ".join(host.get("agents") or [])
                renderer.info(f"  {host.get('host_id')} [{agents}]")
            return 0

        if args.command == "agents":
            agents = client.list_agents()
            rows = []
            for agent in agents:
                metadata = agent.get("metadata") or {}
                rows.append(
                    [
                        str(agent.get("agent_id") or ""),
                        str(metadata.get("display_name") or ""),
                    ]
                )
            renderer.table(["Agent", "Name"], rows)
            return 0

        if args.command == "workflows":
            workflows = client.list_workflows()
            rows = []
            for workflow in sorted(
                workflows, key=lambda w: str(w.get("workflow_id") or "")
            ):
                kinds = workflow.get("step_kinds") or {}
                rendered_kinds = ", ".join(
                    f"{count} {kind}"
                    for kind, count in sorted(kinds.items())
                    if isinstance(count, int) and count > 0
                )
                rows.append(
                    [
                        str(workflow.get("workflow_id") or ""),
                        str(workflow.get("step_count") or 0),
                        rendered_kinds,
                    ]
                )
            renderer.table(["Workflow", "Steps", "Kinds"], rows)
            return 0

        if args.command == "hosts":
            hosts = client.list_hosts()
            rows = [
                [
                    str(host.get("host_id") or ""),
                    ", ".join(host.get("agents") or []),
                    ", ".join(host.get("workflows") or []),
                ]
                for host in hosts
            ]
            renderer.table(["Host", "Agents", "Workflows"], rows)
            return 0

        if args.command in ("repl", "run"):
            # Both talk to a host; each resolves its own below.
            agent_id, target_host_id = COMPANION_AGENT_ID, None
        else:
            agent_id, target_host_id = _resolve_target(
                client,
                getattr(args, "agent", None) or configured_agent,
                getattr(args, "host_id", None) or configured_host,
            )

        if args.command == "sessions":
            sessions_payload = client.list_sessions(agent_id)
            rows = [
                [
                    str(session.get("session_id") or ""),
                    str(session.get("turn_count") or 0),
                    str(session.get("session_total_tokens") or 0),
                ]
                for session in sessions_payload
            ]
            renderer.table(["Session ID", "Turns", "Tokens"], rows)
            return 0

        if args.command == "history":
            turns = client.get_history(agent_id, args.session_id, limit=args.limit)
            if not turns:
                renderer.warn("No conversation history.")
                return 0
            for index, turn in enumerate(turns, 1):
                renderer.info(f"Turn {index}")
                renderer.print(f"User: {turn['user_message']}")
                renderer.print(f"Agent: {turn['agent_response']}")
            return 0

        if args.command == "run":
            app_id, policy_id = _require_identity(app, app_id, policy_id)
            host_id = _resolve_host(client, configured_host)
            message = read_message(args.message, sys.stdin)
            session_id = args.session_id
            if args.continue_session:
                earlier = client.list_sessions(COMPANION_AGENT_ID, app_id=app_id, limit=1)
                session_id = str(earlier[0]["session_id"]) if earlier else None
            return run_once(
                client,
                host_id=host_id,
                session_id=session_id or Repl.new_session_id(),
                app_id=app_id,
                policy_id=policy_id,
                message=message,
                json_output=args.json_output,
                theme=app.theme,
                tool_lines=app.tool_lines,
            )

        if args.command == "workflow":
            return _run_workflow_command(
                client,
                args,
                Target(
                    api_base_url=base_url,
                    agent_id=agent_id,
                    session_id=Repl.new_session_id(),
                    app_id=app_id or "",
                    policy_id=policy_id or "",
                    host_id=target_host_id or "",
                ),
                app,
            )

        if args.command == "repl":
            app_id, policy_id = _require_identity(app, app_id, policy_id)
            host_id = _resolve_host(client, configured_host)
            session_id, title = args.session_id, ""
            if args.continue_session or args.resume:
                earlier = client.list_sessions(
                    COMPANION_AGENT_ID,
                    app_id=app_id,
                    limit=1 if args.continue_session else PICKER_LIMIT,
                )
                picked = None
                if not earlier:
                    renderer.warn("No earlier sessions. Starting a new one.")
                elif args.continue_session:
                    picked = earlier[0]
                else:
                    try:
                        picked = menu(session_options(earlier), hint=PICKER_HINT).run()
                    except KeyboardInterrupt:
                        return 130
                if picked is not None:
                    session_id, title = str(picked["session_id"]), session_title(picked)
            target = Target(
                api_base_url=base_url,
                session_id=session_id or Repl.new_session_id(),
                app_id=app_id,
                policy_id=policy_id,
                host_id=host_id,
                resumed=bool(session_id),
                title=title,
            )
            _build_repl(app, client, target, debug=args.debug).run()
            return 0
    except Exception as exc:
        renderer.error(str(exc))
        return 1
    finally:
        client.close()

    parser.print_help()
    return 0
