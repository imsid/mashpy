from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator, Optional
from unittest.mock import patch

from mash.agents import EvalAgentSpec, EvalJudgeAgentSpec
from mash.agents.masher import EVAL_AGENT_ID, EVAL_JUDGE_AGENT_ID
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.sandbox.manager import SandboxManager
from mash.testing.sandboxes import LocalSandboxProvider
from mash.workflows import StepContext
from pilot.catalog._base import APP_NAME, REPO_ROOT
from pilot.catalog.agents.builder import BUILDER_AGENT_ID
from pilot.catalog.agents.pilot import PILOT_AGENT_ID, PilotSpec
from pilot.catalog.workflows.changelog import (
    CHANGELOG_AGENT_ID,
    CHANGELOG_WORKFLOW_ID,
    ChangelogIn,
    build_changelog_workflow_spec,
)
from pilot.spec import GUIDE_HOST_ID, build_pool

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        del request
        raise NotImplementedError

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


@contextmanager
def _keyless_models() -> Iterator[None]:
    """Pilot's Gemini primary and the masher agents on a fake provider; the
    Gemma subagents build against a dummy OpenRouter key."""
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {"MASH_DATA_DIR": tmp, "MASH_DATABASE_URL": "", "OPENROUTER_API_KEY": "test-key"},
            )
        )
        for spec in (PilotSpec, EvalAgentSpec, EvalJudgeAgentSpec):
            stack.enter_context(
                patch.object(spec, "build_llm", return_value=_FakeLLMProvider())
            )
        yield


def _computer_runtime(pool, agent_id: str):
    """The runtime serving ``agent_id`` on its local computer."""
    return pool._computer_provider.agent_servers[agent_id].runtime


def test_pool_has_the_guide_the_builder_and_the_changelog_workflow() -> None:
    with _keyless_models():
        pool = build_pool()
        try:
            described = {str(item["agent_id"]) for item in pool.describe_agents()}
            assert described == {
                PILOT_AGENT_ID,
                BUILDER_AGENT_ID,
                EVAL_AGENT_ID,
                EVAL_JUDGE_AGENT_ID,
            }
            assert CHANGELOG_AGENT_ID in pool.list_registered_agents()
            guide = pool.get_host(GUIDE_HOST_ID)
            assert guide.primary == PILOT_AGENT_ID
            assert guide.subagents == (BUILDER_AGENT_ID,)
            assert CHANGELOG_WORKFLOW_ID in guide.workflows
        finally:
            asyncio.run(pool.close())


def test_agents_on_their_computers() -> None:
    with _keyless_models():

        async def _run() -> None:
            pool = build_pool()
            pool.configure_runtime_database_url("postgresql://test/runtime")
            pool.configure_sandboxes(SandboxManager(LocalSandboxProvider()))
            await pool.start()
            try:
                guide = _computer_runtime(pool, PILOT_AGENT_ID)
                # bash comes from the sandbox, not from the specs.
                assert {"AskUser", "bash"} <= set(guide.agent.tools.list_tools())
                turn_agent = guide.build_turn_agent(
                    session_id="s-1",
                    trace_id="trace-1",
                    host=pool.snapshot_for(pool.get_host(GUIDE_HOST_ID)),
                )
                try:
                    assert "InvokeSubagent" in turn_agent.tools
                finally:
                    await turn_agent.tools.shutdown()

                builder = _computer_runtime(pool, BUILDER_AGENT_ID)
                assert "Skill" in builder.agent.tools
                assert sorted(s.name for s in builder.agent.skills.list_skills()) == [
                    "build-mash-agent",
                    "build-mash-host",
                    "build-mash-workflow",
                ]

                summarizer = _computer_runtime(pool, CHANGELOG_AGENT_ID)
                assert "bash" in summarizer.agent.tools
                assert str(REPO_ROOT) in str(summarizer.agent.config.system_prompt)
            finally:
                await pool.close()

        asyncio.run(_run())


def test_guide_prompt_points_at_the_mounted_repo_and_the_builder() -> None:
    prompt = str(PilotSpec().build_agent_config().system_prompt)

    assert APP_NAME in prompt
    assert str(REPO_ROOT) in prompt
    assert "`builder`" in prompt
    assert "`CLAUDE.md`" in prompt


def test_read_changelog_slices_the_latest_release() -> None:
    workflow = build_changelog_workflow_spec(_REPO_ROOT)
    read_step = workflow.steps[0]

    extract = read_step.run(
        ChangelogIn(versions=1),
        StepContext(run_id="run-1", step_id=read_step.step_id, workflow_input={}),
    )

    assert extract.versions_included == 1
    assert extract.excerpt.startswith(f"## [{extract.latest_version}]")


def test_top_level_src_mash_packages_have_required_docs() -> None:
    # The guide's doc map sends it to each module's README and AGENTS.md.
    package_names = [
        "agents",
        "api",
        "cli",
        "core",
        "logging",
        "mcp",
        "memory",
        "runtime",
        "workflows",
        "skills",
        "tools",
    ]

    for package_name in package_names:
        package_dir = _REPO_ROOT / "src" / "mash" / package_name
        assert (package_dir / "README.md").is_file(), package_name
        assert (package_dir / "AGENTS.md").is_file(), package_name
