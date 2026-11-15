"""Deployment slash commands."""

from __future__ import annotations

import json

from rich.markup import escape

from ..client import MashHostClientError
from ..commands import Command
from ..render import clip
from .groups import GROUP_DEPLOYMENT


def _host_members(host: dict) -> list[dict]:
    agents = host.get("agents")
    if not isinstance(agents, list):
        return []
    return [member for member in agents if isinstance(member, dict)]


def _host_workflow_ids(host: dict) -> set[str]:
    workflows = host.get("workflows")
    if not isinstance(workflows, list):
        return set()
    return {str(workflow_id) for workflow_id in workflows}


def register(shell) -> None:
    def add(
        name: str, help: str, handler, group: str, aliases: tuple[str, ...] = ()
    ) -> None:
        shell.command_registry.register(
            Command(name=name, help=help, handler=handler, aliases=aliases, group=group)
        )

    def agents_command(ctx, _args: list[str]) -> None:
        """The agents this REPL's requests can reach, as the companion sees them."""
        if ctx.host_id:
            members = _host_members(ctx.client.get_host(ctx.host_id))
            if not members:
                ctx.renderer.warn(f"Host '{ctx.host_id}' has no agents.")
                return
        else:
            agent = ctx.client.get_agent(ctx.agent_id).get("agent")
            members = [agent] if isinstance(agent, dict) else []
        for index, member in enumerate(members):
            metadata = member.get("metadata") or {}
            agent_id = str(member.get("agent_id") or "")
            name = str(metadata.get("display_name") or "")
            if index:
                ctx.renderer.print()
            ctx.renderer.print(
                f"[agent]{escape(agent_id)}[/agent]"
                + (f" [muted]· {escape(name)}[/muted]" if name else "")
            )
            description = str(metadata.get("description") or "")
            if description:
                ctx.renderer.print(f"  {escape(description)}")
            capabilities = [str(item) for item in metadata.get("capabilities") or []]
            if capabilities:
                ctx.renderer.muted(f"  Capabilities: {escape(', '.join(capabilities))}")
            guidance = str(metadata.get("usage_guidance") or "")
            if guidance:
                ctx.renderer.muted(f"  Use for: {escape(guidance)}")

    def tools_command(ctx, _args: list[str]) -> None:
        agents = _target_agents(ctx)

        def rows(items: list[dict], key: str) -> list[list[str]]:
            found = []
            for item in items:
                used_by = sorted(set(item.get("agents") or []) & agents)
                described = item.get(key) or {}
                if used_by:
                    found.append(
                        [
                            escape(str(described.get("name") or "")),
                            escape(", ".join(used_by)),
                            escape(clip(str(described.get("description") or ""), 70)),
                        ]
                    )
            return sorted(found)

        tools = rows(ctx.client.list_tools(), "tool")
        skills = rows(ctx.client.list_skills(), "skill")
        if not tools and not skills:
            ctx.renderer.warn("These agents have no tools or skills.")
            return
        if tools:
            ctx.renderer.table(["Tool", "Agents", "Does"], tools)
        if skills:
            ctx.renderer.table(["Skill", "Agents", "Does"], skills)

    def workflow_command(ctx, args: list[str]) -> None:
        # Bare `/workflow` behaves like `/workflow list`.
        subcommand = args[0].strip().lower() if args else "list"
        if subcommand == "list":
            workflows = ctx.client.list_workflows(host=ctx.host_id)
            if not workflows:
                if ctx.host_id:
                    ctx.renderer.warn(f"No workflows attached to host '{ctx.host_id}'.")
                else:
                    ctx.renderer.warn("No workflows registered.")
                return
            rows = []
            for workflow in workflows:
                rendered_steps = []
                steps = workflow.get("step_preview")
                if isinstance(steps, list):
                    for step in steps:
                        if not isinstance(step, dict):
                            continue
                        step_id = str(step.get("step_id") or "")
                        kind = str(step.get("kind") or "")
                        rendered_steps.append(f"{step_id} ({kind})")
                step_count = int(workflow.get("step_count") or 0)
                if step_count > len(rendered_steps):
                    rendered_steps.append(f"+{step_count - len(rendered_steps)} more")
                rows.append(
                    [
                        str(workflow.get("workflow_id") or ""),
                        ", ".join(rendered_steps),
                    ]
                )
            ctx.renderer.table(["Workflow ID", "Steps"], rows)
            return

        if subcommand == "run":
            if len(args) < 2:
                ctx.renderer.error(
                    "Usage: /workflow run <workflow_id> [dedup_key] [--no-follow] [--input JSON_OBJECT]"
                )
                return
            workflow_id = args[1].strip()
            dedup_key = None
            workflow_input = None
            remaining = list(args[2:])
            # Before --input, so JSON that holds the word stays input.
            before_input = (
                remaining[: remaining.index("--input")]
                if "--input" in remaining
                else remaining
            )
            follow = "--no-follow" not in before_input
            if not follow:
                remaining.remove("--no-follow")
            input_index = None
            for index, value in enumerate(remaining):
                if value == "--input":
                    input_index = index
                    break
            if input_index is not None:
                if input_index + 1 >= len(remaining):
                    ctx.renderer.error(
                        "Usage: /workflow run <workflow_id> [dedup_key] [--no-follow] [--input JSON_OBJECT]"
                    )
                    return
                raw_input = " ".join(remaining[input_index + 1 :]).strip()
                if (
                    len(raw_input) >= 2
                    and raw_input[0] == raw_input[-1]
                    and raw_input[0] in {"'", '"'}
                ):
                    raw_input = raw_input[1:-1]
                remaining = remaining[:input_index]
                try:
                    decoded_input = json.loads(raw_input)
                except json.JSONDecodeError as exc:
                    ctx.renderer.error(f"Workflow input must be valid JSON: {exc.msg}")
                    return
                if not isinstance(decoded_input, dict):
                    ctx.renderer.error("Workflow input must be a JSON object")
                    return
                workflow_input = decoded_input
            if len(remaining) > 1:
                ctx.renderer.error(
                    "Usage: /workflow run <workflow_id> [dedup_key] [--no-follow] [--input JSON_OBJECT]"
                )
                return
            if remaining:
                dedup_key = remaining[0].strip() or None
            if not workflow_id:
                ctx.renderer.error(
                    "Usage: /workflow run <workflow_id> [dedup_key] [--no-follow] [--input JSON_OBJECT]"
                )
                return
            if ctx.host_id:
                attached = _host_workflow_ids(ctx.client.get_host(ctx.host_id))
                if workflow_id not in attached:
                    ctx.renderer.error(
                        f"Workflow '{workflow_id}' is not attached to host "
                        f"'{ctx.host_id}'. Use /workflow list to see attached workflows."
                    )
                    return
            run = ctx.client.run_workflow(
                workflow_id,
                dedup_key=dedup_key,
                workflow_input=workflow_input,
                # A workflow of code steps runs without a user.
                user_id=ctx.user_id or None,
            )
            ctx.renderer.info(f"Workflow: {run.get('workflow_id') or workflow_id}")
            run_id = str(run.get("run_id") or "")
            ctx.renderer.info(f"Run ID: {run_id}")
            if not run_id or not follow:
                ctx.renderer.info(f"Status: {run.get('status') or ''}")
                return

            for event in ctx.client.stream_workflow_run(workflow_id, run_id):
                event_name = str(event.get("event") or "")
                payload = event.get("data")
                if not isinstance(payload, dict):
                    continue
                step_id = str(payload.get("step_id") or "")
                step_label = f"Workflow step {step_id}" if step_id else "Workflow step"

                if event_name == "step.started":
                    ctx.renderer.info(f"{step_label} started")
                    continue

                if event_name == "step.completed":
                    ctx.renderer.info(f"{step_label} completed")
                    continue

                if event_name == "step.failed":
                    error = (payload.get("payload") or {}).get("error")
                    ctx.renderer.error(str(error or f"{step_label} failed"))
                    continue

                if event_name == "workflow.completed":
                    ctx.renderer.info("Workflow status: completed")
                    result = payload.get("result")
                    if isinstance(result, dict):
                        shell.render_workflow_result(workflow_id, result)
                    continue

                if event_name == "workflow.error":
                    error = payload.get("error")
                    ctx.renderer.error(str(error or "workflow stream failed"))
                    return
            return

        if subcommand == "status":
            if len(args) < 3:
                ctx.renderer.error("Usage: /workflow status <workflow_id> <run_id>")
                return
            workflow_id = args[1].strip()
            run_id = args[2].strip()
            if not workflow_id or not run_id:
                ctx.renderer.error("Usage: /workflow status <workflow_id> <run_id>")
                return
            if ctx.host_id:
                attached = _host_workflow_ids(ctx.client.get_host(ctx.host_id))
                if workflow_id not in attached:
                    ctx.renderer.error(
                        f"Workflow '{workflow_id}' is not attached to host "
                        f"'{ctx.host_id}'. Use /workflow list to see attached workflows."
                    )
                    return
            run = ctx.client.get_workflow_run(workflow_id, run_id)
            rows = [
                ["run_id", str(run.get("run_id") or "")],
                ["workflow_id", str(run.get("workflow_id") or workflow_id)],
                ["dedup_key", str(run.get("dedup_key") or "")],
                ["status", str(run.get("status") or "")],
                ["created_at", str(run.get("created_at") or "")],
                ["started_at", str(run.get("started_at") or "")],
                ["finished_at", str(run.get("finished_at") or "")],
                ["error", str(run.get("error") or "")],
            ]
            ctx.renderer.table(["Field", "Value"], rows)
            steps = run.get("steps")
            if isinstance(steps, list) and steps:
                step_rows = [
                    [
                        str(step.get("step_id") or ""),
                        str(step.get("kind") or ""),
                        str(step.get("status") or ""),
                    ]
                    for step in steps
                    if isinstance(step, dict)
                ]
                ctx.renderer.table(["Step", "Kind", "Status"], step_rows)
            result = run.get("result")
            if isinstance(result, dict):
                ctx.renderer.print(json.dumps(result, ensure_ascii=True, indent=2))
            return

        if subcommand == "resume":
            if len(args) < 3:
                ctx.renderer.error("Usage: /workflow resume <workflow_id> <run_id>")
                return
            workflow_id = args[1].strip()
            run_id = args[2].strip()
            if not workflow_id or not run_id:
                ctx.renderer.error("Usage: /workflow resume <workflow_id> <run_id>")
                return
            if ctx.host_id:
                attached = _host_workflow_ids(ctx.client.get_host(ctx.host_id))
                if workflow_id not in attached:
                    ctx.renderer.error(
                        f"Workflow '{workflow_id}' is not attached to host "
                        f"'{ctx.host_id}'. Use /workflow list to see attached workflows."
                    )
                    return
            run = ctx.client.resume_workflow_run(workflow_id, run_id)
            ctx.renderer.info(
                f"Resumed run {run.get('run_id') or run_id} "
                f"(status {run.get('status') or 'unknown'})"
            )
            return

        ctx.renderer.error("Usage: /workflow [list|run|status|resume] ...")

    def memory_command(ctx, args: list[str]) -> None:
        usage = "Usage: /memory | /memory add <text> | /memory forget <id>"
        try:
            if not args:
                try:
                    user = ctx.client.get_cortex_user(ctx.user_id)
                except MashHostClientError as exc:
                    if exc.code != "CORTEX_NOT_FOUND":
                        raise
                    ctx.renderer.warn("Nothing is remembered about you yet.")
                    return
                profile = str(user.get("profile") or "").strip()
                if profile:
                    ctx.renderer.markdown(profile)
                entries = ctx.client.list_cortex_entries(ctx.user_id)
                if entries:
                    ctx.renderer.table(
                        ["ID", "Topic", "Remembered", "From"],
                        [
                            [
                                escape(str(entry.get("entry_id") or "")[:8]),
                                escape(str(entry.get("topic") or "")),
                                escape(clip(str(entry.get("content") or ""), 70)),
                                "you"
                                if entry.get("origin") == "app"
                                else "conversations",
                            ]
                            for entry in entries
                        ],
                    )
                elif not profile:
                    ctx.renderer.warn("Nothing is remembered about you yet.")
                return
            if args[0] == "add" and len(args) > 1:
                text = " ".join(args[1:]).strip()
                # "topic: fact" names the topic; otherwise it is a note.
                topic, _, content = text.partition(":")
                if not content.strip() or len(topic) > 60:
                    topic, content = "note", text
                entry = ctx.client.create_cortex_entry(
                    ctx.user_id, topic=topic.strip(), content=content.strip()
                )
                ctx.renderer.info(
                    f"Remembered ({escape(str(entry.get('entry_id') or '')[:8])})."
                )
                return
            if args[0] == "forget" and len(args) == 2:
                entries = ctx.client.list_cortex_entries(ctx.user_id, limit=200)
                matches = [
                    entry
                    for entry in entries
                    if str(entry.get("entry_id") or "").startswith(args[1])
                ]
                if len(matches) != 1:
                    ctx.renderer.error(
                        f"No entry matches '{args[1]}'."
                        if not matches
                        else f"'{args[1]}' matches {len(matches)} entries; type more of the id."
                    )
                    return
                ctx.client.delete_cortex_entry(ctx.user_id, str(matches[0]["entry_id"]))
                ctx.renderer.info(
                    f"Forgot: {escape(clip(str(matches[0].get('content') or ''), 70))}"
                )
                return
            ctx.renderer.error(usage)
        except MashHostClientError as exc:
            # Without Cortex the deployment has no such routes.
            if exc.status_code == 404 and exc.code != "CORTEX_NOT_FOUND":
                ctx.renderer.warn("Cortex is not enabled on this deployment.")
                return
            raise

    def browser_command(ctx, args: list[str]) -> None:
        if not args:
            sites = ctx.client.list_browser_sites(ctx.user_id)
            if not sites:
                ctx.renderer.warn("Your browser is not signed in to any site.")
                return
            ctx.renderer.table(
                ["Site", "Cookies"],
                [
                    [
                        escape(str(site.get("domain") or "")),
                        str(site.get("cookies") or 0),
                    ]
                    for site in sites
                ],
            )
            return
        if args[0] == "forget" and len(args) == 2:
            cleared = ctx.client.clear_browser_site(ctx.user_id, args[1])
            count = int(cleared.get("cookies") or 0)
            ctx.renderer.info(
                f"Signed out of {escape(args[1])} "
                f"({count} cookie{'' if count == 1 else 's'} removed)."
            )
            return
        ctx.renderer.error("Usage: /browser | /browser forget <site>")

    def _target_agents(ctx) -> set[str]:
        if not ctx.host_id:
            return {ctx.agent_id}
        members = _host_members(ctx.client.get_host(ctx.host_id))
        return {str(member.get("agent_id") or "") for member in members}

    add(
        "agents",
        "The agents your requests can reach and what each is for",
        agents_command,
        GROUP_DEPLOYMENT,
    )
    add(
        "tools", "The tools and skills of those agents", tools_command, GROUP_DEPLOYMENT
    )
    add(
        "workflow",
        "List, run, inspect and resume workflows",
        workflow_command,
        GROUP_DEPLOYMENT,
    )
    add(
        "memory",
        "What is remembered about you: /memory | add <text> | forget <id>",
        memory_command,
        GROUP_DEPLOYMENT,
    )
    add(
        "browser",
        "Sites your browser is signed in to: /browser | forget <site>",
        browser_command,
        GROUP_DEPLOYMENT,
    )
