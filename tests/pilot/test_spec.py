from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Optional
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mash.agents import MasherAgentSpec
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMRequest, LLMResponse
from mash.tools.bash import BashTool
from pilot.spec import (
    API_COPILOT_AGENT_ID,
    APP_NAME,
    CLI_COPILOT_AGENT_ID,
    MCP_COPILOT_AGENT_ID,
    PILOT_AGENT_ID,
    ApiCopilotSpec,
    CliCopilotSpec,
    McpCopilotSpec,
    PilotSpec,
    _cached_docs_for_scope,
    build_host,
)


class _FakeLLMProvider(LLMProvider):
    @property
    def model(self) -> str:
        return "test-model"

    def send(self, request: LLMRequest) -> LLMResponse:
        del request
        raise NotImplementedError

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


def test_primary_and_subagent_prompts_are_app_specific() -> None:
    workspace_root = Path("/tmp/mashpy")

    with patch(
        "pilot.spec._cached_docs_for_scope",
        side_effect=lambda workspace_root, **kwargs: _fake_cached_docs(
            workspace_root,
            doc_roots=kwargs.get("doc_roots", ()),
            extra_doc_paths=kwargs.get("extra_doc_paths", ()),
        ),
    ):
        primary_prompt = PilotSpec(workspace_root).build_system_prompt()
        cli_prompt = CliCopilotSpec(workspace_root).build_system_prompt()
        api_prompt = ApiCopilotSpec(workspace_root).build_system_prompt()
        mcp_prompt = McpCopilotSpec(workspace_root).build_system_prompt()

    primary_text = str(primary_prompt)
    cli_text = str(cli_prompt)
    api_text = str(api_prompt)
    mcp_text = str(mcp_prompt)

    assert APP_NAME in primary_text
    assert "primary Mash codebase guide" in primary_text
    assert "Treat cached docs as the primary source of truth" in primary_text
    assert "DOC: `README.md`" in primary_text
    assert "DOC: `src/mash/AGENTS.md`" in primary_text
    assert "DOC: `src/mash/core/README.md`" in primary_text
    assert "DOC: `src/mash/runtime/AGENTS.md`" in primary_text
    assert "# Repository index" not in primary_text
    assert "# Symbol index" not in primary_text
    assert CLI_COPILOT_AGENT_ID in primary_text
    assert API_COPILOT_AGENT_ID in primary_text
    assert MCP_COPILOT_AGENT_ID in primary_text

    assert APP_NAME in cli_text
    assert "copilot for `src/mash/cli`" in cli_text
    assert "Do not ask the user for permission to inspect code" in cli_text
    assert "For command, inventory, or existence questions, start with one targeted `rg`" in cli_text
    assert "Use the cached CLI docs before using bash." in cli_text
    assert "# Repository index" not in cli_text
    assert "# Symbol index" not in cli_text
    assert "DOC: `src/mash/cli/README.md`" in cli_text
    assert "DOC: `src/mash/cli/AGENTS.md`" in cli_text

    assert APP_NAME in api_text
    assert "copilot for `src/mash/api`" in api_text
    assert "Use the cached API docs before using bash." in api_text
    assert "DOC: `src/mash/api/README.md`" in api_text
    assert "DOC: `src/mash/api/AGENTS.md`" in api_text
    assert "DOC: `src/mash/runtime/AGENTS.md`" not in api_text

    assert APP_NAME in mcp_text
    assert "copilot for `src/mash/mcp`" in mcp_text
    assert "Use the cached MCP docs before using bash." in mcp_text
    assert "DOC: `src/mash/mcp/README.md`" in mcp_text
    assert "DOC: `src/mash/mcp/AGENTS.md`" in mcp_text


def test_build_host_registers_primary_cli_api_and_masher() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(
            os.environ,
            {
                "MASH_DATA_DIR": tmp,
                "ANTHROPIC_API_KEY": "test-key",
                "OPENAI_API_KEY": "test-key",
            },
            clear=False,
        ):
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(PilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(CliCopilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(ApiCopilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(McpCopilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(MasherAgentSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch("pilot.spec._cached_docs_for_scope", return_value=[])
                )

                async def _run() -> None:
                    host = build_host(Path.cwd())
                    await host.start()
                    try:
                        described = {
                            item["agent_id"]: item for item in host.describe_agents()
                        }
                        assert sorted(described.keys()) == [
                            API_COPILOT_AGENT_ID,
                            CLI_COPILOT_AGENT_ID,
                            "masher",
                            MCP_COPILOT_AGENT_ID,
                            PILOT_AGENT_ID,
                        ]

                        primary = host.get_agent(PILOT_AGENT_ID)
                        assert primary.get_subagent_ids() == [
                            API_COPILOT_AGENT_ID,
                            CLI_COPILOT_AGENT_ID,
                            "masher",
                            MCP_COPILOT_AGENT_ID,
                        ]
                        assert CLI_COPILOT_AGENT_ID in str(primary.system_prompt)
                        assert API_COPILOT_AGENT_ID in str(primary.system_prompt)
                        assert MCP_COPILOT_AGENT_ID in str(primary.system_prompt)
                        assert "masher" in str(primary.system_prompt)
                    finally:
                        await host.close()

                asyncio.run(_run())


def test_tool_shape_matches_mash_copilot_design() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(
            os.environ,
            {
                "MASH_DATA_DIR": tmp,
                "ANTHROPIC_API_KEY": "test-key",
                "OPENAI_API_KEY": "test-key",
            },
            clear=False,
        ):
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(PilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(CliCopilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(ApiCopilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(McpCopilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(MasherAgentSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch("pilot.spec._cached_docs_for_scope", return_value=[])
                )

                async def _run() -> None:
                    host = build_host(Path.cwd())
                    await host.start()
                    try:
                        primary = host.get_agent(PILOT_AGENT_ID)
                        cli_agent = host.get_agent(CLI_COPILOT_AGENT_ID)
                        api_agent = host.get_agent(API_COPILOT_AGENT_ID)
                        mcp_agent = host.get_agent(MCP_COPILOT_AGENT_ID)
                        masher = host.get_agent("masher")

                        assert "bash" in primary.agent.tools
                        assert "InvokeSubagent" not in primary.agent.tools
                        assert "bash" in cli_agent.agent.tools
                        assert "bash" in api_agent.agent.tools
                        assert "bash" in mcp_agent.agent.tools
                        assert "search_conversations" not in primary.agent.tools
                        assert "search_conversations" not in cli_agent.agent.tools
                        assert "search_conversations" not in api_agent.agent.tools
                        assert "search_conversations" not in mcp_agent.agent.tools
                        assert "get_latest_session" in masher.agent.tools
                        assert "get_trace_logs" in masher.agent.tools
                    finally:
                        await host.close()

                asyncio.run(_run())


def test_build_host_shutdown_closes_bash_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(
            os.environ,
            {
                "MASH_DATA_DIR": tmp,
                "ANTHROPIC_API_KEY": "test-key",
                "OPENAI_API_KEY": "test-key",
            },
            clear=False,
        ):
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(PilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(CliCopilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(ApiCopilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(McpCopilotSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch.object(MasherAgentSpec, "build_llm", return_value=_FakeLLMProvider())
                )
                stack.enter_context(
                    patch("pilot.spec._cached_docs_for_scope", return_value=[])
                )
                shutdown = stack.enter_context(
                    patch.object(BashTool, "shutdown", autospec=True)
                )

                async def _run() -> None:
                    host = build_host(Path.cwd())
                    await host.start()
                    await host.close()

                asyncio.run(_run())

                assert shutdown.call_count >= 5


def test_build_system_prompt_uses_cached_docs_helper() -> None:
    workspace_root = Path("/tmp/mashpy")

    with patch(
        "pilot.spec._cached_docs_for_scope",
        side_effect=lambda workspace_root, **kwargs: _fake_cached_docs(
            workspace_root,
            doc_roots=kwargs.get("doc_roots", ()),
            extra_doc_paths=kwargs.get("extra_doc_paths", ()),
        ),
    ) as cached_docs:
        PilotSpec(workspace_root).build_system_prompt()
        CliCopilotSpec(workspace_root).build_system_prompt()
        ApiCopilotSpec(workspace_root).build_system_prompt()
        McpCopilotSpec(workspace_root).build_system_prompt()

    assert cached_docs.call_count == 4
    primary_call = cached_docs.call_args_list[0]
    assert "docs/rfcs/host-to-agent-protocol.md" in primary_call.kwargs["extra_doc_paths"]


def test_missing_cached_docs_do_not_break_prompt_building() -> None:
    workspace_root = Path("/tmp/mashpy")

    with patch("pilot.spec._cached_docs_for_scope", return_value=[]):
        prompt = PilotSpec(workspace_root).build_system_prompt()

    assert APP_NAME in str(prompt)


def test_cached_docs_for_scope_loads_readme_and_agents_docs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        cli_dir = workspace_root / "src" / "mash" / "cli"
        cli_dir.mkdir(parents=True, exist_ok=True)
        (cli_dir / "README.md").write_text("# CLI\nreadme\n", encoding="utf-8")
        (cli_dir / "AGENTS.md").write_text("# CLI Agents\nagents\n", encoding="utf-8")

        doc_paths = _cached_docs_for_scope(
            workspace_root,
            doc_roots=("src/mash/cli",),
        )

        assert doc_paths == [
            str((cli_dir / "README.md").resolve()),
            str((cli_dir / "AGENTS.md").resolve()),
        ]


def test_scope_prompt_blocks_include_cached_docs_only() -> None:
    workspace_root = Path("/tmp/mashpy")

    with patch(
        "pilot.spec._cached_docs_for_scope",
        side_effect=lambda workspace_root, **kwargs: _fake_cached_docs(
            workspace_root,
            doc_roots=kwargs.get("doc_roots", ()),
            extra_doc_paths=kwargs.get("extra_doc_paths", ()),
        ),
    ):
        primary_prompt = str(PilotSpec(workspace_root).build_system_prompt())
        cli_prompt = str(CliCopilotSpec(workspace_root).build_system_prompt())

    assert "# Cached docs" in primary_prompt
    assert "DOC: `README.md`" in primary_prompt
    assert "DOC: `src/mash/AGENTS.md`" in primary_prompt
    assert "DOC: `src/mash/runtime/AGENTS.md`" in primary_prompt
    assert "host-to-agent-protocol.md" not in primary_prompt
    assert "# Repository index" not in primary_prompt
    assert "# Symbol index" not in primary_prompt
    assert "Cache dir:" not in primary_prompt
    assert "Directory sample:" not in primary_prompt
    assert "Cached files:" not in primary_prompt

    assert "# Cached docs" in cli_prompt
    assert "# Repository index" not in cli_prompt
    assert "# Symbol index" not in cli_prompt
    assert "DOC: `src/mash/cli/README.md`" in cli_prompt
    assert "DOC: `src/mash/cli/AGENTS.md`" in cli_prompt


def test_prompts_remain_compact_and_principle_driven() -> None:
    workspace_root = Path("/tmp/mashpy")

    with patch(
        "pilot.spec._cached_docs_for_scope",
        side_effect=lambda workspace_root, **kwargs: _fake_cached_docs(
            workspace_root,
            doc_roots=kwargs.get("doc_roots", ()),
            extra_doc_paths=kwargs.get("extra_doc_paths", ()),
        ),
    ):
        primary_prompt = str(PilotSpec(workspace_root).build_system_prompt())
        cli_prompt = str(CliCopilotSpec(workspace_root).build_system_prompt())

    assert primary_prompt.count("Delegate") <= 2
    assert "host composition" not in primary_prompt.lower()
    assert "runtime-serving behavior" not in primary_prompt.lower()
    assert "open-ended exploration" not in cli_prompt.lower()
    assert "Treat cached docs as the primary source of truth" in cli_prompt
    assert "CopilotIndexState" not in Path("/Users/sid/Projects/mashpy/pilot/spec.py").read_text()
    assert "IndexScope" not in Path("/Users/sid/Projects/mashpy/pilot/spec.py").read_text()
    assert "code_index" not in Path("/Users/sid/Projects/mashpy/pilot/spec.py").read_text()


def test_copilot_configs_limit_history_and_steps() -> None:
    workspace_root = Path("/tmp/mashpy")

    with patch("pilot.spec._cached_docs_for_scope", return_value=[]):
        cli_config = CliCopilotSpec(workspace_root).build_agent_config()
        api_config = ApiCopilotSpec(workspace_root).build_agent_config()
        mcp_config = McpCopilotSpec(workspace_root).build_agent_config()

    assert cli_config.conversation_history_turns == 0
    assert api_config.conversation_history_turns == 0
    assert mcp_config.conversation_history_turns == 0
    assert cli_config.max_steps == 10
    assert api_config.max_steps == 10
    assert mcp_config.max_steps == 10
    assert cli_config.temperature == 0.2
    assert api_config.temperature == 0.2
    assert mcp_config.temperature == 0.2


def test_missing_docs_do_not_break_prompt_building() -> None:
    workspace_root = Path("/tmp/mashpy")

    with patch(
        "pilot.spec._cached_docs_for_scope",
        side_effect=lambda workspace_root, **kwargs: _fake_cached_docs(
            workspace_root,
            doc_roots=kwargs.get("doc_roots", ()),
            extra_doc_paths=kwargs.get("extra_doc_paths", ()),
            omit_docs={"src/mash/api/AGENTS.md", "src/mash/runtime/README.md"},
        ),
    ):
        primary_prompt = str(PilotSpec(workspace_root).build_system_prompt())
        api_prompt = str(ApiCopilotSpec(workspace_root).build_system_prompt())

    assert APP_NAME in primary_prompt
    assert APP_NAME in api_prompt
    assert "DOC: `src/mash/api/AGENTS.md`" not in api_prompt
    assert "DOC: `src/mash/runtime/README.md`" not in primary_prompt


def test_top_level_src_mash_packages_have_required_docs() -> None:
    package_names = [
        "agents",
        "api",
        "cli",
        "core",
        "logging",
        "mcp",
        "memory",
        "runtime",
        "skills",
        "tools",
    ]

    for package_name in package_names:
        package_dir = _REPO_ROOT / "src" / "mash" / package_name
        assert (package_dir / "README.md").is_file(), package_name
        assert (package_dir / "AGENTS.md").is_file(), package_name


def _fake_cached_docs(
    workspace_root: Path,
    *,
    doc_roots: tuple[str, ...] | list[str] = (),
    extra_doc_paths: tuple[str, ...] | list[str] = (),
    omit_docs: set[str] | None = None,
) -> list[str]:
    workspace_root = workspace_root.resolve()
    omit_docs = omit_docs or set()
    relpaths: list[str] = []
    for root in doc_roots:
        relpaths.extend([f"{root}/README.md", f"{root}/AGENTS.md"])
    relpaths.extend(extra_doc_paths)

    seen: set[str] = set()
    resolved_paths: list[str] = []
    for relpath in relpaths:
        cleaned = relpath.strip()
        if not cleaned or cleaned in seen or cleaned in omit_docs:
            continue
        seen.add(cleaned)
        if relpath in omit_docs:
            continue
        doc_path = workspace_root / relpath
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        title = Path(relpath).name.replace(".md", "")
        doc_path.write_text(f"# {title}\nContent for {relpath}.\n", encoding="utf-8")
        resolved_paths.append(str(doc_path.resolve()))

    return resolved_paths
