"""Deployed pool: agent registrations, their processes' clients, hosts, and workflows."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Dict

from mash.core.database import resolve_database_url
from mash.memory.store import MemoryStore, PostgresStore
from mash.workflows import AgentStep, CodeStep, WorkflowRegistry, WorkflowService, WorkflowSpec
from mash.workflows.dbos import register_runner as register_workflow_runner
from mash.workflows.dbos import resolve_runner_id
from mash.workflows.dbos import unregister_runner as unregister_workflow_runner
from mash.workflows.store import WorkflowStore

from ..client import AgentClient, AgentClientLike
from ..events import PostgresRuntimeStore, RuntimeStore
from ..spec import AgentSpec
from .processes import STATUS_UNAVAILABLE, AgentProvider
from .subagents import AgentMetadata
from .types import AgentRegistration, Host

if TYPE_CHECKING:
    from mash.browser.manager import BrowserManager
    from mash.sandbox.manager import SandboxManager


STATUS_DISABLED = "disabled"


class AgentDisabledError(RuntimeError):
    """A request named an agent that is disabled."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"agent '{agent_id}' is disabled")


class AgentInUseError(RuntimeError):
    """An agent cannot be disabled while a host or workflow names it."""

    def __init__(self, agent_id: str, references: list[str]) -> None:
        self.agent_id = agent_id
        self.references = references
        super().__init__(
            f"agent '{agent_id}' is in use by {', '.join(references)}; "
            "remove it from them before disabling it"
        )


class Pool:
    """Deployed pool of role-less agents, workflows, and the hosts composed over them.

    The pool is the unit of deploy; a :class:`Host` is the unit of
    composition. Hosts are in-memory values: define them in code at build
    time or over the control API, and re-define them after a restart.
    Workflows are registered on the pool directly and served pool-wide; a
    pool with no user agents is a valid, workflow-only deploy.

    Every agent runs in its own process. ``start()`` asks the agent provider
    to start each agent and registers an ``AgentClient`` for it; the pool
    itself runs no agent runtimes.
    """

    def __init__(
        self,
        *,
        runtime_database_url: str | None = None,
        runner_id: str | None = None,
    ) -> None:
        self.runner_id = resolve_runner_id(runner_id)
        self.runtime_database_url = str(runtime_database_url or "").strip() or None
        self._registered: Dict[str, AgentRegistration] = {}
        self._hosts: Dict[str, Host] = {}
        self._agent_provider: AgentProvider | None = None
        self._sandboxes: SandboxManager | None = None
        self._browsers: BrowserManager | None = None
        self._clients: Dict[str, AgentClientLike] = {}
        # Every registered agent starts enabled; the set resets on restart.
        self._disabled: set[str] = set()
        self._model_settings: Dict[str, dict[str, Any]] = {}
        self._shared_runtime_store: RuntimeStore | None = None
        self._shared_memory_store: MemoryStore | None = None
        self._shared_workflow_store: WorkflowStore | None = None
        self._workflow_registry = WorkflowRegistry()
        self._default_workflow_ids: list[str] = []
        # CortexSettings when HostBuilder.enable_cortex() was called. Each
        # agent process imports the same pool and reads it in build_agent_server.
        self.cortex: Any = None
        self._workflow_service = WorkflowService(
            self._workflow_registry,
            self,
            runner_id=self.runner_id,
        )

    def configure_runtime_database_url(self, database_url: str | None) -> None:
        value = str(database_url or "").strip()
        self.runtime_database_url = value or None

    def configure_agent_provider(self, provider: AgentProvider) -> None:
        """Set the provider that runs this pool's agents.

        The pool owns it from here and closes it in ``close()``.
        """
        if self._clients:
            raise RuntimeError("agent provider must be configured before start()")
        self._agent_provider = provider

    def has_agent_provider(self) -> bool:
        return self._agent_provider is not None

    def configure_sandboxes(self, sandboxes: "SandboxManager") -> None:
        """Set where agents' code runs: each request user's sandbox.

        Without sandboxes, agents get no sandbox tools. The pool owns the
        manager from here and closes it in ``close()``.
        """
        if self._clients:
            raise RuntimeError("sandboxes must be configured before start()")
        self._sandboxes = sandboxes

    def get_sandboxes(self) -> "SandboxManager | None":
        return self._sandboxes

    def configure_browsers(self, browsers: "BrowserManager") -> None:
        """Set where agents browse: each request user's browser.

        Without browsers, agents get no browser tools. The pool owns the
        manager from here and closes it in ``close()``.
        """
        if self._clients:
            raise RuntimeError("browsers must be configured before start()")
        self._browsers = browsers

    def get_browsers(self) -> "BrowserManager | None":
        return self._browsers

    def wants_browsers(self) -> bool:
        """Whether any registered agent lists sites to browse."""
        return any(
            registration.definition.browser_sites()
            for registration in self._registered.values()
        )

    def register_agent(
        self,
        definition: AgentSpec,
        *,
        metadata: AgentMetadata | None,
        agent_id: str | None = None,
    ) -> str:
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        if resolved_agent_id in self._registered:
            raise ValueError(f"agent '{resolved_agent_id}' is already registered")
        if metadata is None:
            raise ValueError(f"agent '{resolved_agent_id}' metadata is required")

        self._registered[resolved_agent_id] = AgentRegistration(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=metadata,
        )
        return resolved_agent_id

    def register_workflow_agent(
        self,
        definition: AgentSpec,
        *,
        agent_id: str | None = None,
    ) -> str:
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        if not resolved_agent_id:
            raise ValueError("agent_id is required")
        if resolved_agent_id in self._registered:
            raise ValueError(f"agent '{resolved_agent_id}' is already registered")

        self._registered[resolved_agent_id] = AgentRegistration(
            agent_id=resolved_agent_id,
            definition=definition,
            metadata=None,
            is_workflow_agent=True,
        )
        return resolved_agent_id

    def define_host(self, host: Host) -> Host:
        merged_workflows = tuple(
            dict.fromkeys((*host.workflows, *self._default_workflow_ids))
        )
        if merged_workflows != host.workflows:
            host = Host(
                host_id=host.host_id,
                primary=host.primary,
                subagents=host.subagents,
                workflows=merged_workflows,
            )
        for agent_id in (host.primary, *host.subagents):
            registered = self._registered.get(agent_id)
            if registered is None or registered.is_workflow_agent:
                raise ValueError(
                    f"host '{host.host_id}' references unknown agent '{agent_id}'"
                )
            if agent_id in self._disabled:
                raise ValueError(
                    f"host '{host.host_id}' references disabled agent '{agent_id}'"
                )
        for workflow_id in host.workflows:
            try:
                self._workflow_registry.get(workflow_id)
            except KeyError:
                raise ValueError(
                    f"host '{host.host_id}' references unknown workflow "
                    f"'{workflow_id}'"
                ) from None
        self._hosts[host.host_id] = host
        return host

    def get_host(self, host_id: str) -> Host:
        host = self._hosts.get(str(host_id or "").strip())
        if host is None:
            raise ValueError(f"host '{host_id}' is not defined")
        return host

    def list_hosts(self) -> list[Host]:
        return list(self._hosts.values())

    def describe_host(self, host_id: str) -> dict[str, Any]:
        host = self.get_host(host_id)

        def _member(agent_id: str) -> dict[str, Any]:
            metadata = self.get_agent_metadata(agent_id)
            return {
                "agent_id": agent_id,
                "metadata": asdict(metadata) if metadata is not None else None,
            }

        return {
            "host_id": host.host_id,
            "primary": _member(host.primary),
            "subagents": [_member(agent_id) for agent_id in host.subagents],
            "workflows": list(host.workflows),
        }

    def snapshot_for(self, host: Host) -> dict[str, Any]:
        return {
            "host_id": host.host_id,
            "primary": host.primary,
            "subagents": list(host.subagents),
        }

    def snapshot_host_agent_specs(self, host_id: str) -> dict[str, dict[str, Any]]:
        """Capture the current AgentSpec of every agent in a host.

        Used by eval scoring to record on the experiment what was actually
        evaluated. Best-effort: an agent whose spec can't be built (e.g. missing
        provider credentials) contributes an empty entry rather than failing the
        whole snapshot.
        """
        host = self.get_host(host_id)
        agent_ids = [host.primary, *host.subagents]
        return {agent_id: self._snapshot_agent_spec(agent_id) for agent_id in agent_ids}

    def _snapshot_agent_spec(self, agent_id: str) -> dict[str, Any]:
        spec = self.get_registered_agent_spec(agent_id)
        if spec is None:
            return {}
        snapshot: dict[str, Any] = {"agent_id": agent_id}
        try:
            config = spec.build_agent_config()
            system_prompt = config.system_prompt
            snapshot["system_prompt"] = (
                system_prompt
                if isinstance(system_prompt, str)
                else json.dumps(system_prompt, ensure_ascii=True, sort_keys=True)
            )
            snapshot["max_steps"] = config.max_steps
            snapshot["max_tokens"] = config.max_tokens
            snapshot["temperature"] = config.temperature
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            snapshot["tools"] = sorted(spec.build_tools().list_tools())
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            snapshot["skills"] = sorted(s.name for s in spec.build_skills().list_skills())
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            snapshot["model"] = getattr(spec.build_llm(), "model", None)
        except Exception:  # pragma: no cover - defensive
            pass
        return snapshot

    async def submit_host_request(
        self,
        host_id: str,
        *,
        message: str,
        session_id: str,
        user_id: str,
        structured_output: Any = None,
        context: str | None = None,
        metadata: dict[str, Any] | None = None,
        mode: str = "fg",
    ) -> dict[str, Any]:
        host = self.get_host(host_id)
        request_id = await self.get_client(host.primary).post_request(
            message,
            session_id=session_id,
            structured_output=structured_output,
            metadata=metadata,
            context=context,
            host=self.snapshot_for(host),
            user_id=user_id,
            mode=mode,
        )
        return {
            "request_id": request_id,
            "agent_id": host.primary,
            "session_id": session_id,
            "user_id": user_id,
            "status": "accepted",
        }

    def register_workflow(self, workflow: WorkflowSpec) -> None:
        self._ensure_workflow_task_agents(workflow)
        self._workflow_registry.register(workflow)

    def register_default_workflow(self, workflow: WorkflowSpec) -> None:
        """Register a workflow that is attached to every host in this pool."""
        self.register_workflow(workflow)
        self._default_workflow_ids.append(workflow.workflow_id)

    def require_agent(self, agent_id: str) -> str:
        """Return the registered agent id, or raise ``ValueError`` if unknown."""
        resolved_agent_id = str(agent_id or "").strip()
        if resolved_agent_id not in self._registered:
            raise ValueError(f"agent '{resolved_agent_id}' is not registered")
        return resolved_agent_id

    def get_agent_model_settings(self, agent_id: str) -> dict[str, Any]:
        """The agent's ``model`` and ``max_steps``, read from its spec once.

        Best-effort like ``_snapshot_agent_spec``: a value the spec can't build
        (e.g. missing provider credentials) is None.
        """
        resolved_agent_id = self.require_agent(agent_id)
        cached = self._model_settings.get(resolved_agent_id)
        if cached is not None:
            return cached
        spec = self._registered[resolved_agent_id].definition
        settings: dict[str, Any] = {"model": None, "max_steps": None}
        try:
            settings["max_steps"] = spec.build_agent_config().max_steps
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            settings["model"] = getattr(spec.build_llm(), "model", None)
        except Exception:  # pragma: no cover - defensive
            pass
        self._model_settings[resolved_agent_id] = settings
        return settings

    def get_registered_agent_spec(self, agent_id: str) -> AgentSpec | None:
        registered = self._registered.get(str(agent_id or "").strip())
        return registered.definition if registered is not None else None

    def get_agent_metadata(self, agent_id: str) -> AgentMetadata | None:
        registered = self._registered.get(str(agent_id or "").strip())
        return registered.metadata if registered is not None else None

    async def start(self) -> None:
        if self._clients:
            return
        if not self.runtime_database_url:
            self.runtime_database_url = resolve_database_url()
        if not self.runtime_database_url:
            raise RuntimeError("MASH_DATABASE_URL is required to start hosted Mash runtimes")
        register_workflow_runner(self.runner_id, self)
        try:
            shared_runtime_store = PostgresRuntimeStore(self.runtime_database_url)
            shared_memory_store = PostgresStore(self.runtime_database_url)
            shared_workflow_store = WorkflowStore(self.runtime_database_url)
            self._shared_runtime_store = shared_runtime_store
            self._shared_memory_store = shared_memory_store
            self._shared_workflow_store = shared_workflow_store
            await shared_runtime_store.open()
            await shared_memory_store.open()
            await shared_workflow_store.open()
            if self._sandboxes is not None:
                await self._sandboxes.start()
            if self._browsers is not None:
                await self._browsers.start()

            if self._registered:
                if self._agent_provider is None:
                    raise RuntimeError(
                        "an agent provider is required to start a pool with agents"
                    )
                endpoints = await asyncio.gather(
                    *(self._agent_provider.ensure(agent_id) for agent_id in self._registered)
                )
                for endpoint in endpoints:
                    self._clients[endpoint.agent_id] = AgentClient(
                        endpoint.base_url, endpoint.agent_id, token=endpoint.token
                    )
        except Exception:
            await self.close()
            raise

    def get_client(self, agent_id: str) -> AgentClientLike:
        if agent_id in self._disabled:
            raise AgentDisabledError(agent_id)
        client = self._clients.get(agent_id)
        if client is None:
            raise ValueError(f"agent client '{agent_id}' is not registered")
        return client

    def is_agent_enabled(self, agent_id: str) -> bool:
        return self.require_agent(agent_id) not in self._disabled

    def agent_status(self, agent_id: str) -> str:
        """``running``, ``starting``, ``unavailable`` or ``disabled``."""
        resolved_agent_id = self.require_agent(agent_id)
        if resolved_agent_id in self._disabled:
            return STATUS_DISABLED
        status = (
            self._agent_provider.status(resolved_agent_id)
            if self._agent_provider is not None
            else None
        )
        return status or STATUS_UNAVAILABLE

    def agent_references(self, agent_id: str) -> list[str]:
        """The hosts and workflows that name ``agent_id``."""
        references = [
            f"host '{host.host_id}'"
            for host in self._hosts.values()
            if agent_id == host.primary or agent_id in host.subagents
        ]
        for workflow in self._workflow_registry.list():
            for step in workflow.steps:
                named = (
                    step.agent_id == agent_id
                    if isinstance(step, AgentStep)
                    else agent_id in getattr(step, "agent_ids", ())
                )
                if named:
                    references.append(f"workflow '{workflow.workflow_id}'")
                    break
        return references

    async def enable_agent(self, agent_id: str) -> None:
        """Start the agent's process and accept requests for it again.

        Requests left pending when it was disabled continue once it is up.
        """
        resolved_agent_id = self.require_agent(agent_id)
        if resolved_agent_id not in self._disabled:
            return
        if self._agent_provider is None:
            raise RuntimeError("the pool is not started")
        endpoint = await self._agent_provider.ensure(resolved_agent_id)
        self._clients[resolved_agent_id] = AgentClient(
            endpoint.base_url, resolved_agent_id, token=endpoint.token
        )
        self._disabled.discard(resolved_agent_id)

    async def disable_agent(self, agent_id: str) -> None:
        """Stop accepting requests for the agent and stop its process.

        Refused while a host or workflow names the agent. Requests in flight
        are not cancelled; they stay pending until the agent is enabled again.
        """
        resolved_agent_id = self.require_agent(agent_id)
        if resolved_agent_id in self._disabled:
            return
        references = self.agent_references(resolved_agent_id)
        if references:
            raise AgentInUseError(resolved_agent_id, references)
        self._disabled.add(resolved_agent_id)
        client = self._clients.pop(resolved_agent_id, None)
        if client is not None:
            await client.close()
        if self._agent_provider is not None:
            await self._agent_provider.stop(resolved_agent_id)

    def get_runtime_store(self) -> RuntimeStore | None:
        """Shared runtime event store, or None before the pool is started."""
        return self._shared_runtime_store

    def get_memory_store(self, agent_id: str) -> MemoryStore | None:
        """The pool's shared memory store if ``agent_id`` uses it.

        None before the pool is started, or for an agent that overrides
        ``build_memory_store()``; that store lives only in the agent's runtime.
        """
        spec = self.get_registered_agent_spec(agent_id)
        if spec is None or not _uses_default_memory_store(spec):
            return None
        return self._shared_memory_store

    def get_workflow_store(self) -> WorkflowStore | None:
        """Shared workflow run/step store, or None before the pool is started."""
        return self._shared_workflow_store

    def get_workflow_registry(self) -> WorkflowRegistry:
        return self._workflow_registry

    def get_workflow_service(self) -> WorkflowService:
        return self._workflow_service

    def list_registered_agents(self) -> list[str]:
        """Every registered agent, workflow-only agents included."""
        return list(self._registered)

    def list_agents(self) -> list[str]:
        return [
            agent_id
            for agent_id, registered in self._registered.items()
            if not registered.is_workflow_agent
        ]

    def describe_agents(self) -> list[dict[str, object]]:
        described: list[dict[str, object]] = []
        for registered in self._registered.values():
            if registered.is_workflow_agent:
                continue
            described.append(
                {
                    "agent_id": registered.agent_id,
                    "metadata": (
                        asdict(registered.metadata)
                        if registered.metadata is not None
                        else None
                    ),
                    "enabled": registered.agent_id not in self._disabled,
                    "status": self.agent_status(registered.agent_id),
                }
            )
        return described

    def describe_hosts(self) -> list[dict[str, Any]]:
        return [
            {
                "host_id": host.host_id,
                "primary": host.primary,
                "subagents": list(host.subagents),
                "workflows": list(host.workflows),
            }
            for host in self._hosts.values()
        ]

    async def _describe_all(self) -> dict[str, dict[str, Any]]:
        return {
            agent_id: await self.get_client(agent_id).describe()
            for agent_id in self.list_agents()
            if agent_id not in self._disabled
        }

    async def describe_tools(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        agents_by_tool: dict[str, list[str]] = {}
        for agent_id, described in (await self._describe_all()).items():
            for tool in described.get("tools") or []:
                name = tool["name"]
                seen.setdefault(name, dict(tool))
                agents_by_tool.setdefault(name, []).append(agent_id)
        return [
            {"tool": tool, "agents": agents_by_tool[name]}
            for name, tool in seen.items()
        ]

    async def describe_skills(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        agents_by_skill: dict[str, list[str]] = {}
        for agent_id, described in (await self._describe_all()).items():
            for skill in described.get("skills") or []:
                name = skill["name"]
                seen.setdefault(name, dict(skill))
                agents_by_skill.setdefault(name, []).append(agent_id)
        return [
            {"skill": skill, "agents": agents_by_skill[name]}
            for name, skill in seen.items()
        ]

    async def aggregate_tool_invocations(
        self,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        if self._shared_runtime_store is None:
            return []
        counts: dict[str, dict[str, Any]] = {}
        for agent_id in self.list_agents():
            rows = await self._shared_runtime_store.count_tool_invocations(
                agent_id, from_ts=from_ts, to_ts=to_ts
            )
            for row in rows:
                tool_name = row["tool_name"]
                n = row["count"]
                if tool_name not in counts:
                    counts[tool_name] = {"total": 0, "by_agent": {}}
                counts[tool_name]["total"] += n
                counts[tool_name]["by_agent"][agent_id] = n
        return [{"tool_name": name, **data} for name, data in counts.items()]

    async def aggregate_skill_invocations(
        self,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        if self._shared_runtime_store is None:
            return []
        counts: dict[str, dict[str, Any]] = {}
        for agent_id in self.list_agents():
            rows = await self._shared_runtime_store.count_skill_invocations(
                agent_id, from_ts=from_ts, to_ts=to_ts
            )
            for row in rows:
                skill_name = row["skill_name"]
                n = row["count"]
                if skill_name not in counts:
                    counts[skill_name] = {"total": 0, "by_agent": {}}
                counts[skill_name]["total"] += n
                counts[skill_name]["by_agent"][agent_id] = n
        return [{"skill_name": name, **data} for name, data in counts.items()]

    async def close(self) -> None:
        unregister_workflow_runner(self.runner_id, self)
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        if self._agent_provider is not None:
            # Stops every agent process.
            await self._agent_provider.close()
        if self._sandboxes is not None:
            await self._sandboxes.close()
        if self._browsers is not None:
            await self._browsers.close()

        if self._shared_runtime_store is not None:
            await self._shared_runtime_store.close()
            self._shared_runtime_store = None
        if self._shared_memory_store is not None:
            await self._shared_memory_store.close()
            self._shared_memory_store = None
        if self._shared_workflow_store is not None:
            await self._shared_workflow_store.close()
            self._shared_workflow_store = None

    def _ensure_workflow_task_agents(self, workflow: WorkflowSpec) -> None:
        # Agent steps bind an agent id (optionally with a spec to auto-register).
        # Code steps declare string-only dependencies through agent_ids.
        # Strategy-only workflows register their agents separately.
        bindings = [
            (step.agent_id, step.agent_spec)
            for step in workflow.steps
            if isinstance(step, AgentStep)
        ]
        for raw_agent_id, agent_spec in bindings:
            agent_id = str(raw_agent_id or "").strip()
            if not agent_id:
                raise ValueError("workflow step agent id is required")
            existing = self.get_registered_agent_spec(agent_id)
            if existing is None:
                if agent_spec is None:
                    raise ValueError(
                        f"workflow agent '{agent_id}' is not registered"
                    )
                self.register_workflow_agent(agent_spec, agent_id=agent_id)
            elif agent_spec is not None and existing is not agent_spec:
                raise ValueError(
                    f"workflow agent '{agent_id}' is already registered "
                    "with a different spec"
                )
        for step in workflow.steps:
            if not isinstance(step, CodeStep):
                continue
            for agent_id in step.agent_ids:
                if self.get_registered_agent_spec(agent_id) is None:
                    raise ValueError(
                        f"workflow agent '{agent_id}' declared by code step "
                        f"'{step.step_id}' is not registered"
                    )

    async def __aenter__(self) -> "Pool":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        await self.close()


def _uses_default_memory_store(spec: AgentSpec) -> bool:
    return type(spec).build_memory_store is AgentSpec.build_memory_store


__all__ = ["AgentDisabledError", "AgentInUseError", "Pool"]
