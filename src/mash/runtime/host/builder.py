"""Builder for pool composition."""

from __future__ import annotations

import importlib
from typing import Any

from mash.core.database import resolve_database_url
from mash.workflows import WorkflowSpec

from ..spec import AgentSpec
from .host import Pool
from .subagents import AgentMetadata
from .types import AgentRegistration, Host


def load_masher_components():
    masher = importlib.import_module("mash.agents.masher")
    return (
        masher.EVAL_AGENT_ID,
        masher.EvalAgentSpec,
        masher.build_eval_agent_metadata,
        masher.EVAL_JUDGE_AGENT_ID,
        masher.EvalJudgeAgentSpec,
        masher.build_eval_judge_agent_metadata,
        masher.build_masher_workflows,
    )


class HostBuilder:
    """Builder composing a flat agent pool, workflows, and host definitions."""

    def __init__(self) -> None:
        self._agents: list[AgentRegistration] = []
        self._hosts: list[Host] = []
        self._workflows: list[WorkflowSpec] = []
        self._cortex: Any = None
        self._evals = False

    def agent(
        self,
        definition: AgentSpec,
        *,
        metadata: AgentMetadata,
        agent_id: str | None = None,
    ) -> "HostBuilder":
        resolved_agent_id = (agent_id or definition.get_agent_id()).strip()
        self._agents.append(
            AgentRegistration(
                agent_id=resolved_agent_id,
                definition=definition,
                metadata=metadata,
            )
        )
        return self

    def host(self, host: Host) -> "HostBuilder":
        self._hosts.append(host)
        return self

    def workflow(self, workflow: WorkflowSpec) -> "HostBuilder":
        self._workflows.append(workflow)
        return self

    def enable_cortex(self, *, profile_max_chars: int | None = None) -> "HostBuilder":
        """Turn on Cortex, per-user memory shared by every agent in the pool.

        Agents get the request user's profile in their context and the
        read-only ``search_cortex`` tool. The pool also gets ``cortex-agent``
        and the on-demand ``cortex-build`` workflow.
        """
        from mash.agents.cortex import DEFAULT_PROFILE_MAX_CHARS, CortexSettings

        self._cortex = CortexSettings(
            profile_max_chars=profile_max_chars or DEFAULT_PROFILE_MAX_CHARS
        )
        return self

    def enable_evals(self) -> "HostBuilder":
        """Turn on masher's evals: ``eval-agent``, ``eval-judge-agent``, and
        the ``gen-synthetic-evals`` and ``run-experiment`` workflows, which
        attach to every host.
        """
        self._evals = True
        return self

    def build(self) -> Pool:
        pool = Pool(runtime_database_url=resolve_database_url())
        for registered in self._agents:
            pool.register_agent(
                registered.definition,
                metadata=registered.metadata,
                agent_id=registered.agent_id,
            )
        if self._evals:
            _register_evals(pool)
        for workflow in self._workflows:
            pool.register_workflow(workflow)
        if self._cortex is not None:
            cortex = importlib.import_module("mash.agents.cortex")
            cortex_agent_spec = cortex.CortexAgentSpec(self._cortex)
            cortex_agent_spec.runtime_context.bind_pool(pool)
            pool.register_agent(
                cortex_agent_spec,
                metadata=cortex.build_cortex_agent_metadata(),
                agent_id=cortex.CORTEX_AGENT_ID,
            )
            pool.register_workflow(
                cortex.build_cortex_build_workflow(cortex_agent_spec.runtime_context)
            )
        # Hosts are defined last so workflow-id validation sees every
        # registered workflow. Pool defaults are appended to both these hosts
        # and hosts defined later through the control API.
        for host in self._hosts:
            pool.define_host(host)
        pool.cortex = self._cortex
        return pool


def _register_evals(pool: Pool) -> None:
    (
        eval_agent_id,
        eval_agent_spec_type,
        build_eval_agent_metadata,
        eval_judge_agent_id,
        eval_judge_agent_spec_type,
        build_eval_judge_agent_metadata,
        build_masher_workflows,
    ) = load_masher_components()
    eval_agent_spec = eval_agent_spec_type()
    eval_agent_spec.runtime_context.bind_pool(pool)
    pool.register_agent(
        eval_agent_spec,
        metadata=build_eval_agent_metadata(),
        agent_id=eval_agent_id,
    )
    pool.register_agent(
        eval_judge_agent_spec_type(),
        metadata=build_eval_judge_agent_metadata(),
        agent_id=eval_judge_agent_id,
    )
    for workflow in build_masher_workflows(eval_agent_spec):
        pool.register_default_workflow(workflow)
