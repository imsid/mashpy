"""Agent runtime for orchestrating tool-driven workflows."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from anthropic import Anthropic

from .context import CLIContext
from .logging import AgentTraceEvent, DebugEvent, EventLogger
from .memory import Memory
from .telemetry import TelemetryCollector, TokenUsage
from .tools import ToolRegistry, ToolResult, ToolSpec, format_tool_payload

_TOOL_SEARCH_TOOL_TYPE = "tool_search_tool_bm25_20251119"
_TOOL_SEARCH_TOOL_NAME = "tool_search_tool_bm25"
_TOOL_SEARCH_BETAS = ("advanced-tool-use-2025-11-20",)
_BASH_TOOL_NAME = "bash"
_BASH_TOOL_TYPE = "bash_20250124"

_BASH_SENTINEL_PREFIX = "__mash_bash_done__"
_BASH_EXIT_PREFIX = "__mash_bash_exit__"
_BASH_DEFAULT_TIMEOUT_SECONDS = 30
_BASH_MAX_OUTPUT_LINES = 100


class BashSession:
    """Persistent bash session used by the Claude bash tool."""

    def __init__(self, working_dir: Optional[str]) -> None:
        self.working_dir = working_dir
        self._process: Optional["subprocess.Popen[str]"] = None
        self._stdout_queue: "queue.Queue[str]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._start_process()

    def _start_process(self) -> None:

        self._process = subprocess.Popen(
            ["/bin/bash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=self.working_dir,
        )
        self._stdout_queue = queue.Queue()
        self._reader_thread = threading.Thread(
            target=self._read_stdout, name="bash-session-reader", daemon=True
        )
        self._reader_thread.start()

    def _read_stdout(self) -> None:
        assert self._process is not None
        if self._process.stdout is None:
            return
        for line in self._process.stdout:
            self._stdout_queue.put(line)

    def restart(self, working_dir: Optional[str]) -> None:
        self.shutdown()
        self.working_dir = working_dir
        self._start_process()

    def shutdown(self) -> None:
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=2)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass
        self._process = None

    def execute_command(self, command: str, timeout: int) -> tuple[str, int]:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Bash session is not running.")
        token = uuid.uuid4().hex
        sentinel = f"{_BASH_SENTINEL_PREFIX}{token}"
        exit_marker = f"{_BASH_EXIT_PREFIX}{token}"
        payload = f"{command}\n" f'echo "{exit_marker}$?"\n' f'echo "{sentinel}"\n'
        self._process.stdin.write(payload)
        self._process.stdin.flush()
        output_lines, exit_code, total_lines = self._read_until_sentinel(
            sentinel, exit_marker, timeout
        )
        output_text = self._truncate_output(output_lines, total_lines)
        return output_text, exit_code

    def _read_until_sentinel(
        self,
        sentinel: str,
        exit_marker: str,
        timeout: int,
    ) -> tuple[List[str], int, int]:
        lines: List[str] = []
        exit_code: int = 0
        start = time.time()
        total_lines = 0
        while True:
            remaining = timeout - int(time.time() - start)
            if remaining <= 0:
                raise TimeoutError("command timed out")
            try:
                line = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("command timed out") from exc
            stripped = line.rstrip("\n")
            if stripped.startswith(exit_marker):
                raw = stripped[len(exit_marker) :].strip()
                try:
                    exit_code = int(raw)
                except ValueError:
                    exit_code = 0
                continue
            if stripped == sentinel:
                return lines, exit_code, total_lines
            total_lines += 1
            if total_lines <= _BASH_MAX_OUTPUT_LINES:
                lines.append(line.rstrip("\n"))

    def _truncate_output(self, lines: List[str], total_lines: int) -> str:
        if not lines:
            return ""
        if total_lines <= _BASH_MAX_OUTPUT_LINES:
            return "\n".join(lines)
        truncated = "\n".join(lines)
        return f"{truncated}\n\n... Output truncated ({total_lines} total lines) ..."


@dataclass
class AgentConfig:
    """Configuration for agent workflows."""

    app_id: str
    system_prompt: str = ""
    app_context: str = ""
    model: str = ""
    max_steps: int = 4
    max_tokens: int = 1024
    max_history_messages: int = 10
    tool_search_enabled: bool = True
    anthropic_api_key: Optional[str] = None
    use_bash_tool: bool = False
    bash_working_dir: Optional[str] = None


@dataclass
class ToolCall:
    """Represents a requested tool call."""

    tool_id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class AgentStep:
    """One step in the agent loop."""

    step_id: int
    tool_calls: List[ToolCall]
    usage: TokenUsage


@dataclass
class AgentReply:
    """Reply payload returned to the caller."""

    text: str
    steps: List[AgentStep]
    usage: TokenUsage
    trace_id: str


class AnthropicProvider:
    """Thin wrapper over the Anthropic client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        event_logger: EventLogger,
        app_id: str,
    ) -> None:
        self._event_logger = event_logger
        self._app_id = app_id
        try:
            self._client = Anthropic(api_key=api_key)
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Anthropic client is not installed. Install `anthropic` to enable agent mode."
            ) from exc

    def create_message(
        self,
        *,
        session_id: str,
        model: str,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        betas: Optional[List[str]] = None,
    ) -> Any:
        event = DebugEvent(
            event_type="llm.request",
            app_id=self._app_id,
            session_id=session_id,
            payload={
                "tools": tools,
            },
        )
        self._event_logger.emit(event)
        params: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools
        if betas:
            return self._client.beta.messages.create(**params, betas=betas)
        return self._client.messages.create(**params)


class AgentRuntime:
    """Agent runtime that performs tool-driven orchestration."""

    def __init__(
        self,
        config: AgentConfig,
        tool_registry: ToolRegistry,
        memory: Memory,
        event_logger: EventLogger,
        telemetry: TelemetryCollector,
    ) -> None:
        self._config = config
        self._tool_registry = tool_registry
        self._memory = memory
        self._telemetry = telemetry
        self._event_logger = event_logger
        self.use_bash_tool = bool(config.use_bash_tool)
        self.bash_working_dir = config.bash_working_dir
        self._llm = AnthropicProvider(
            api_key=self._config.anthropic_api_key,
            event_logger=event_logger,
            app_id=self._config.app_id,
        )
        self._system_prompt = self._build_system_prompt()
        self._tool_defs: List[Dict[str, Any]] = []
        self._tool_betas: List[str] = []
        self._session_id: Optional[str] = None
        self._bash_session: Optional[BashSession] = None

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def telemetry(self) -> TelemetryCollector:
        return self._telemetry

    def set_tool_registry(self, tool_registry: ToolRegistry) -> None:
        """Replace the tool registry used for MCP/tool invocations."""

        self._tool_registry = tool_registry

    def start_session(self, session_id: str) -> str:
        self._session_id = session_id
        self.refresh_prompt()
        self.refresh_tools(session_id)
        return f"{session_id}-{int(time.time() * 1000)}"

    def handle_message(
        self,
        session_id: str,
        agent_trace_id: str,
        text: str,
        *,
        ctx: Optional[CLIContext] = None,
    ) -> AgentReply:
        trace_id = agent_trace_id
        step_id = 0
        steps: List[AgentStep] = []
        usage_total = TokenUsage()
        self._emit(
            "agent.start",
            session_id,
            trace_id,
            step_id,
            {"text": text, "trace_id": trace_id},
        )
        history = self._memory.get_conversation(
            self._config.app_id,
            session_id,
            limit=self._config.max_history_messages,
        )
        messages = _build_messages(history)
        if text:
            last = messages[-1] if messages else None
            if (
                last is None
                or last.get("role") != "user"
                or last.get("content") != text
            ):
                messages.append({"role": "user", "content": text})
        if not self._tool_defs:
            self.refresh_tools(session_id)
        system_prompt = self._system_prompt
        tool_defs = list(self._tool_defs)
        while step_id < max(1, self._config.max_steps):
            start = time.time()
            response = self._llm.create_message(
                session_id=session_id,
                model=self._config.model,
                system=system_prompt,
                messages=messages,
                tools=tool_defs,
                max_tokens=self._config.max_tokens,
                betas=self._tool_betas,
            )
            latency_ms = int((time.time() - start) * 1000)
            assistant_text, tool_calls, assistant_blocks = _parse_anthropic_response(
                response
            )
            usage = _extract_usage(response)
            usage_total.add(usage)
            self._telemetry.record_request(session_id, usage)
            self._emit(
                "agent.step",
                session_id,
                trace_id,
                step_id,
                {
                    "tool_calls": [call.name for call in tool_calls],
                    "latency_ms": latency_ms,
                    "assistant_text": assistant_text,
                },
                duration_ms=latency_ms,
            )
            steps.append(AgentStep(step_id=step_id, tool_calls=tool_calls, usage=usage))

            if not tool_calls:
                self._emit(
                    "agent.finish",
                    session_id,
                    trace_id,
                    step_id,
                    {"assistant_text": assistant_text, "usage": usage_total.to_dict()},
                )
                return AgentReply(
                    text=assistant_text,
                    steps=steps,
                    usage=usage_total,
                    trace_id=trace_id,
                )

            messages.append({"role": "assistant", "content": assistant_blocks})
            tool_results: List[Dict[str, Any]] = []
            for call in tool_calls:
                self._render_step_status(ctx, step_id, call.name, call.arguments)
                self._emit(
                    "tool.call",
                    session_id,
                    trace_id,
                    step_id,
                    {"tool": call.name, "arguments": call.arguments},
                )
                if call.name == _BASH_TOOL_NAME:
                    payload = self._handle_bash_tool(call.arguments, call.tool_id)
                    self._emit(
                        "tool.result",
                        session_id,
                        trace_id,
                        step_id,
                        {"tool": call.name, "is_error": payload.get("is_error", False)},
                    )
                    tool_results.append(payload)
                    continue

                tool = self._tool_registry.get(call.name)
                if tool is None:
                    result = ToolResult(
                        name=call.name,
                        content="Unknown tool.",
                        is_error=True,
                    )
                else:
                    result = _invoke_tool(tool, call.name, call.arguments, ctx)
                self._emit(
                    "tool.result",
                    session_id,
                    trace_id,
                    step_id,
                    {"tool": call.name, "is_error": result.is_error},
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.tool_id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            step_id += 1

        fallback_text = "Reached max steps without completing."
        self._emit(
            "agent.finish",
            session_id,
            trace_id,
            step_id,
            {"trace_id": trace_id, "usage": usage_total.to_dict()},
        )
        return AgentReply(
            text=fallback_text,
            steps=steps,
            usage=usage_total,
            trace_id=trace_id,
        )

    def _build_system_prompt(self) -> str:
        tool_search_name = (
            _TOOL_SEARCH_TOOL_NAME if self._config.tool_search_enabled else ""
        )
        parts: List[str] = [
            _default_system_prompt(
                tool_search_name=tool_search_name,
                app_context=self._config.app_context,
            )
        ]
        if self._config.system_prompt:
            parts.append(self._config.system_prompt.strip())
        return "\n\n".join(part for part in parts if part)

    def _memory_tools(self, session_id: str) -> Iterable[ToolSpec]:
        app_id = self._config.app_id

        def _get_full_conversation(
            _args: Dict[str, Any],
            _ctx: Optional[CLIContext],
            *,
            _name: str = "get_full_conversation",
        ) -> ToolResult:
            conversation = self._memory.get_conversation(app_id, session_id)
            return ToolResult(
                _name,
                format_tool_payload(conversation),
                conversation,
            )

        def _get_preferences(
            _args: Dict[str, Any],
            _ctx: Optional[CLIContext],
            *,
            _name: str = "get_preferences",
        ) -> ToolResult:
            preferences = self._memory.get_preferences(app_id, session_id)
            return ToolResult(
                _name,
                format_tool_payload(preferences),
                preferences,
            )

        def _set_preferences(
            args: Dict[str, Any],
            _ctx: Optional[CLIContext],
            *,
            _name: str = "set_preferences",
        ) -> ToolResult:
            if "preferences" not in args:
                return ToolResult(_name, "preferences is required.", is_error=True)
            self._memory.set_preferences(app_id, session_id, args.get("preferences"))
            trace_id = _ctx.agent_trace_id if _ctx else None
            self._emit("preferences.write", session_id, trace_id, 0, {})
            return ToolResult(_name, "ok")

        return [
            ToolSpec(
                name="get_full_conversation",
                description="Return the full conversation history for this session.",
                input_schema={"type": "object", "properties": {}, "required": []},
                source="memory",
                tags={"memory"},
                invoke=_get_full_conversation,
            ),
            ToolSpec(
                name="get_preferences",
                description="Fetch stored user preferences for this session.",
                input_schema={"type": "object", "properties": {}, "required": []},
                source="memory",
                tags={"memory"},
                invoke=_get_preferences,
            ),
            ToolSpec(
                name="set_preferences",
                description="Store user preferences for this session.",
                input_schema={
                    "type": "object",
                    "properties": {"preferences": {}},
                    "required": ["preferences"],
                },
                source="memory",
                tags={"memory"},
                invoke=_set_preferences,
            ),
        ]

    def refresh_prompt(self) -> None:
        """Rebuild the system prompt from the latest config."""

        self._system_prompt = self._build_system_prompt()

    def refresh_tools(self, session_id: str) -> None:
        """Rebuild tool definitions and beta flags for the current session."""

        for tool in self._memory_tools(session_id):
            self._tool_registry.register(tool)
        tool_defs = self._tool_registry.to_anthropic_tools(
            enable_search=self._config.tool_search_enabled
        )
        if self._config.tool_search_enabled:
            tool_defs.insert(
                0,
                {"type": _TOOL_SEARCH_TOOL_TYPE, "name": _TOOL_SEARCH_TOOL_NAME},
            )
        if self.use_bash_tool:
            tool_defs.append({"type": _BASH_TOOL_TYPE, "name": _BASH_TOOL_NAME})
        else:
            if self._bash_session is not None:
                self._bash_session.shutdown()
                self._bash_session = None
        self._tool_defs = tool_defs
        self._tool_betas = (
            list(_TOOL_SEARCH_BETAS) if self._config.tool_search_enabled else []
        )

    def _handle_bash_tool(
        self, args: Dict[str, Any], tool_use_id: str
    ) -> Dict[str, Any]:
        if not isinstance(args, dict):
            args = {}
        if not self.use_bash_tool:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "Bash tool is disabled.",
                "is_error": True,
            }
        if args.get("restart"):
            self._restart_bash_session()
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "Bash session restarted",
                "is_error": False,
            }
        command = str(args.get("command") or "")
        if not command.strip():
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "bash tool requires a command.",
                "is_error": True,
            }
        ok, reason = _validate_bash_command(command)
        if not ok:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": reason or "Command blocked by policy.",
                "is_error": True,
            }
        try:
            session = self._get_bash_session()
            output, exit_code = session.execute_command(
                command, timeout=_BASH_DEFAULT_TIMEOUT_SECONDS
            )
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": output,
                "is_error": exit_code != 0,
            }
        except TimeoutError:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": (
                    f"Command timed out after {_BASH_DEFAULT_TIMEOUT_SECONDS} seconds"
                ),
                "is_error": True,
            }
        except Exception as exc:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"Bash tool error: {exc}",
                "is_error": True,
            }

    def _get_bash_session(self) -> BashSession:
        working_dir = self.bash_working_dir or os.getcwd()
        if self._bash_session is None:
            self._bash_session = BashSession(working_dir)
            return self._bash_session
        if self._bash_session.working_dir != working_dir:
            self._bash_session.restart(working_dir)
        return self._bash_session

    def _restart_bash_session(self) -> None:
        working_dir = self.bash_working_dir or os.getcwd()
        if self._bash_session is None:
            self._bash_session = BashSession(working_dir)
            return
        self._bash_session.restart(working_dir)

    def _emit(
        self,
        event_type: str,
        session_id: str,
        trace_id: Optional[str],
        step_id: int,
        payload: Dict[str, Any],
        *,
        duration_ms: Optional[int] = None,
    ) -> None:
        if not self._event_logger:
            return
        event = AgentTraceEvent(
            event_type=event_type,
            app_id=self._config.app_id,
            session_id=session_id,
            trace_id=trace_id,
            step_id=step_id,
            payload=payload,
            duration_ms=duration_ms,
        )
        self._event_logger.emit(event)

    def _render_step_status(
        self,
        ctx: Optional[CLIContext],
        step_id: int,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> None:
        if ctx is None:
            return
        args_text = _format_tool_args(arguments)
        ctx.renderer.info(f"Step {step_id}: calling {tool_name} args={args_text}")


def _invoke_tool(
    tool: ToolSpec,
    tool_name: str,
    args: Dict[str, Any],
    ctx: Optional[CLIContext],
) -> ToolResult:
    try:
        result = tool.invoke(args, ctx)
    except Exception as exc:
        return ToolResult(
            name=tool_name,
            content=f"Tool error: {exc}",
            raw=exc,
            is_error=True,
        )
    if isinstance(result, ToolResult):
        return result
    if isinstance(result, str):
        return ToolResult(name=tool_name, content=result)
    if result is None:
        return ToolResult(name=tool_name, content="ok")
    return ToolResult(
        name=tool_name,
        content=format_tool_payload(result),
        raw=result,
    )


def _validate_bash_command(command: str) -> tuple[bool, Optional[str]]:
    dangerous_patterns = [
        "rm -rf /",
        "mkfs",
        ":(){:|:&};:",
        "shutdown",
        "reboot",
        "sudo",
    ]
    for pattern in dangerous_patterns:
        if pattern in command:
            return False, f"Command contains dangerous pattern: {pattern}"
    return True, None


def _format_tool_args(args: Dict[str, Any], *, max_chars: int = 200) -> str:
    if not args:
        return "{}"
    payload = format_tool_payload(args)
    compact = " ".join(payload.split())
    if len(compact) > max_chars:
        return f"{compact[:max_chars].rstrip()}..."
    return compact


def _build_messages(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for item in history:
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"}:
            continue
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
    return messages


def _parse_anthropic_response(
    response: Any,
) -> tuple[str, List[ToolCall], List[Dict[str, Any]]]:
    content = getattr(response, "content", None)
    if content is None:
        return "", [], []
    if isinstance(content, str):
        return content, [], [{"type": "text", "text": content}]
    tool_calls: List[ToolCall] = []
    text_parts: List[str] = []
    blocks: List[Dict[str, Any]] = []
    for block in content:
        block_type = _block_value(block, "type")
        if block_type == "text":
            text = _block_value(block, "text") or ""
            text_parts.append(text)
            blocks.append({"type": "text", "text": text})
        elif block_type == "tool_use":
            tool_id = _block_value(block, "id")
            name = _block_value(block, "name") or ""
            arguments = _block_value(block, "input") or {}
            tool_calls.append(
                ToolCall(
                    tool_id=str(tool_id), name=str(name), arguments=arguments or {}
                )
            )
            blocks.append(
                {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}
            )
        else:
            blocks.append(_coerce_block_dict(block, block_type))
    return "".join(text_parts).strip(), tool_calls, blocks


def _extract_usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    input_tokens = getattr(usage, "input_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", 0)
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens", input_tokens)
        output_tokens = usage.get("output_tokens", output_tokens)
    return TokenUsage(
        input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0)
    )


def _default_system_prompt(
    *,
    tool_search_name: str,
    app_context: str = "",
) -> str:
    tool_search_line = ""
    if tool_search_name:
        tool_search_line = (
            f"You also have {tool_search_name} for discovering tools by name "
            "or description when you are unsure what to call. "
            f"Use {tool_search_name} if you are not confident about the right "
            "tool name."
        )
    base_prompt = (
        "You are a Mash Agent running inside a Mash CLI app. "
        "Your job is to understand the user's request, use available tools, "
        "and respond clearly and efficiently. "
        "You have built-in tools get_full_conversation, get_preferences, and "
        "set_preferences for working with session context. "
        "Only store user preferences from the conversation, and save them "
        "with set_preferences. "
        f"{tool_search_line}"
    ).strip()
    parts = [base_prompt]
    if app_context:
        parts.append(app_context.strip())
    return "\n\n".join(part for part in parts if part)


def _coerce_block_dict(block: Any, block_type: Optional[str]) -> Dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        try:
            return block.model_dump()
        except TypeError:
            pass
    if hasattr(block, "dict"):
        try:
            return block.dict()
        except TypeError:
            pass
    data = {}
    raw = getattr(block, "__dict__", None)
    if isinstance(raw, dict):
        data.update(raw)
    if block_type:
        data.setdefault("type", block_type)
    if not data:
        data = {"type": block_type or "unknown", "text": str(block)}
    return data


def _block_value(block: Any, key: str) -> Any:
    if isinstance(block, dict):
        return block.get(key)
    value = getattr(block, key, None)
    if value is not None:
        return value
    if hasattr(block, "model_dump"):
        try:
            return block.model_dump().get(key)
        except TypeError:
            pass
    if hasattr(block, "dict"):
        try:
            return block.dict().get(key)
        except TypeError:
            pass
    return None
