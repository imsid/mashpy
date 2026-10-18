"""File memory: index injection and the queued memory update."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from conftest import build_test_stores
from mash.core.config import AgentConfig
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.core.llm.types import LLMContentBlock, LLMRequest, LLMResponse, LLMTokenUsage
from mash.runtime import AgentRuntime, AgentSpec
from mash.runtime import file_memory
from mash.runtime.events import RuntimeEventType
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry

CURATOR_MARKER = "You maintain the long-term memory"
WRITE_MEMORY = (
    "printf -- '- [prefs.md](prefs.md): the user prefers tea\\n' > INDEX.md"
    " && echo 'The user prefers tea.' > prefs.md"
)


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    return "\n".join(str(block.get("text") or "") for block in system or [])


def _usage() -> LLMTokenUsage:
    return LLMTokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)


class _MemoryLLM(LLMProvider):
    """Answers requests; as the curator, writes one memory file with bash."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    @property
    def model(self) -> str:
        return "test-model"

    async def send(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        curator = CURATOR_MARKER in _system_text(request.system)
        saw_result = any(
            block.type == "tool_result"
            for message in request.messages
            for block in message.content
        )
        if curator and not saw_result:
            call = ToolCall(id="mem-1", name="bash", arguments={"command": WRITE_MEMORY})
            return LLMResponse(
                thought_summary="",
                text="",
                tool_calls=[call],
                content_blocks=[
                    LLMContentBlock.tool_call(
                        tool_call_id="mem-1", name="bash", arguments=call.arguments
                    )
                ],
                stop_reason="tool_call",
                usage=_usage(),
            )
        text = "Added prefs.md." if curator else "Noted, tea it is."
        return LLMResponse(
            thought_summary="",
            text=text,
            tool_calls=[],
            content_blocks=[LLMContentBlock.text(text)],
            stop_reason="end_turn",
            usage=_usage(),
        )

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _MemorySpec(AgentSpec):
    def __init__(self, *, memory_enabled: bool, max_chars: int = 8000) -> None:
        self.llm = _MemoryLLM()
        self._memory_enabled = memory_enabled
        self._max_chars = max_chars

    def get_agent_id(self) -> str:
        return "assistant"

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return self.llm

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id="assistant",
            system_prompt="You are an assistant.",
            memory_enabled=self._memory_enabled,
            memory_index_max_chars=self._max_chars,
        )

    def enable_runtime_tools(self) -> bool:
        return False


class FileMemoryHelperTests(unittest.TestCase):
    def test_read_index_caps_and_marks_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "INDEX.md").write_text("x" * 50)

            self.assertEqual(file_memory.read_index(Path(tmp), 100), "x" * 50)
            capped = file_memory.read_index(Path(tmp), 10)
            self.assertTrue(capped.startswith("x" * 10))
            self.assertIn("truncated at 10 characters", capped)

    def test_missing_index_gives_no_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(file_memory.read_index(Path(tmp), 100), "")
            self.assertEqual(file_memory.memory_block(Path(tmp), ""), "")

    def test_memory_off_resolves_no_dir(self) -> None:
        config = AgentConfig(app_id="a", system_prompt="p")

        self.assertIsNone(file_memory.resolve_memory_dir(config))

    def test_transcript_starts_at_the_request_and_renders_tools(self) -> None:
        context = {
            "messages": [
                {"role": "user", "content": "earlier turn"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "deploy it"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Deploying."},
                        {"type": "tool_use", "name": "bash", "input": {"command": "make deploy"}},
                    ],
                },
                {"role": "tool", "content": [{"type": "tool_result", "content": "ok", "is_error": False}]},
                {"role": "assistant", "content": "Deployed."},
            ]
        }

        transcript = file_memory.build_transcript("deploy it", context)

        self.assertTrue(transcript.startswith("user: deploy it"))
        self.assertNotIn("earlier turn", transcript)
        self.assertIn('[tool call bash: {"command": "make deploy"}]', transcript)
        self.assertIn("[tool result: ok]", transcript)
        self.assertTrue(transcript.endswith("assistant: Deployed."))

    def test_workflow_steps_and_memory_updates_do_not_update_memory(self) -> None:
        self.assertTrue(file_memory.should_update_memory({}))
        self.assertTrue(file_memory.should_update_memory({"subagent_id": "research"}))
        self.assertFalse(file_memory.should_update_memory({"workflow_id": "wf"}))
        self.assertFalse(
            file_memory.should_update_memory({"memory_update": {"request_id": "r"}})
        )


class FileMemoryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.memory_dir = Path(tmp.name) / "memory"
        env = patch.dict(
            os.environ,
            {"MASH_DATA_DIR": tmp.name, file_memory.MASH_MEMORY_DIR_ENV: str(self.memory_dir)},
        )
        env.start()
        self.addCleanup(env.stop)
        self.runtime_store, self.memory_store = build_test_stores()

    async def _runtime(self, spec: _MemorySpec) -> AgentRuntime:
        runtime = AgentRuntime.from_spec(
            spec,
            session_id="host-session",
            runtime_store=self.runtime_store,
            memory_store=self.memory_store,
        )
        await runtime.open()
        self.addAsyncCleanup(runtime.shutdown)
        return runtime

    async def _wait_terminal(self, runtime: AgentRuntime, request_id: str) -> str:
        cursor = 0
        for _ in range(200):
            if await self.runtime_store.has_request(request_id):
                events, cursor, done = await runtime.stream_response_events(
                    request_id, cursor=cursor, wait_timeout=0.05
                )
                for event in events:
                    if event["event"] in ("request.completed", "request.error"):
                        return event["event"]
            await asyncio.sleep(0.01)
        raise AssertionError(f"request {request_id} did not finish")

    async def _run(self, runtime: AgentRuntime, message: str, **kwargs: Any) -> str:
        accepted = await runtime.submit_request(message=message, session_id="s-1", **kwargs)
        request_id = accepted["request_id"]
        self.assertEqual(await self._wait_terminal(runtime, request_id), "request.completed")
        return request_id

    async def test_completed_request_queues_an_update_that_writes_memory(self) -> None:
        spec = _MemorySpec(memory_enabled=True)
        runtime = await self._runtime(spec)

        request_id = await self._run(runtime, "I prefer tea")
        update_id = f"{request_id}-memory"
        self.assertEqual(await self._wait_terminal(runtime, update_id), "request.completed")

        self.assertEqual(
            (self.memory_dir / "INDEX.md").read_text(),
            "- [prefs.md](prefs.md): the user prefers tea\n",
        )
        self.assertEqual((self.memory_dir / "prefs.md").read_text(), "The user prefers tea.\n")

        events = await self.runtime_store.list_request_events(update_id)
        accepted = next(e for e in events if e.event_type == RuntimeEventType.REQUEST_ACCEPTED.value)
        self.assertEqual(accepted.payload["request_metadata"], {"memory_update": {"request_id": request_id}})
        self.assertTrue(accepted.payload["message"].startswith("user: I prefer tea"))
        started = next(e for e in events if e.event_type == RuntimeEventType.TRACE_STARTED.value)
        self.assertEqual(started.payload["trace_kind"], "memory.update")
        self.assertEqual(started.payload["source_request_id"], request_id)

        # The curator sees only bash, its own prompt, and no conversation history.
        curator_request = next(
            r for r in spec.llm.requests if CURATOR_MARKER in _system_text(r.system)
        )
        self.assertEqual([tool.name for tool in curator_request.tools], ["bash"])
        self.assertNotIn("You are an assistant.", _system_text(curator_request.system))
        self.assertEqual(curator_request.messages[0].role, "user")

        # Its turn is kept out of replayed history, and it queues no update of its own.
        turns = await self.memory_store.get_turns(session_id="s-1", app_id="assistant")
        self.assertEqual([t["replayable"] for t in turns], [True, False])
        self.assertFalse(await self.runtime_store.has_request(f"{update_id}-memory"))

    async def test_next_request_sees_the_memory_index(self) -> None:
        spec = _MemorySpec(memory_enabled=True)
        runtime = await self._runtime(spec)
        request_id = await self._run(runtime, "I prefer tea")
        await self._wait_terminal(runtime, f"{request_id}-memory")

        await self._run(runtime, "what should I drink?")

        # The second request's own update may already have run; skip curators.
        last = [r for r in spec.llm.requests if CURATOR_MARKER not in _system_text(r.system)][-1]
        system = _system_text(last.system)
        self.assertIn("You are an assistant.", system)
        self.assertIn("## Memory", system)
        self.assertIn("- [prefs.md](prefs.md): the user prefers tea", system)

    async def test_workflow_step_requests_queue_no_update(self) -> None:
        runtime = await self._runtime(_MemorySpec(memory_enabled=True))

        request_id = await self._run(
            runtime,
            "summarize",
            request_id="run-1-summarize",
            parent={"kind": "workflow", "workflow_id": "wf", "run_id": "run-1", "task_id": "summarize"},
        )
        await asyncio.sleep(0.05)

        self.assertFalse(await self.runtime_store.has_request(f"{request_id}-memory"))

    async def test_memory_off_queues_nothing_and_adds_no_block(self) -> None:
        spec = _MemorySpec(memory_enabled=False)
        runtime = await self._runtime(spec)

        request_id = await self._run(runtime, "I prefer tea")
        await asyncio.sleep(0.05)

        self.assertIsNone(runtime.memory_dir)
        self.assertFalse(await self.runtime_store.has_request(f"{request_id}-memory"))
        self.assertNotIn("## Memory", _system_text(spec.llm.requests[-1].system))
        self.assertFalse(self.memory_dir.exists())


if __name__ == "__main__":
    unittest.main()
