"""Base CLI wiring for MCP applications."""

# Apps should subclass Mash to build their own interactive experience.

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from mashnet import Host, MCPClientError

from .agent import AgentConfig, AgentRuntime
from .commands import Command, CommandBus
from .context import CLIContext
from .logging import DebugEvent, EventLogger
from .memory import SqliteMemory
from .render import PlainRenderer
from .repl import Repl
from .router import CommandRouter
from .tools import ToolRegistry, ToolSpec, normalize_tool_name
from .telemetry import TelemetryCollector


@dataclass
class Connection:
    """Represents an active MCP server connection."""

    name: str
    url: str
    client: Any


class Mash(ABC):
    """Base class for MCP-aware CLI applications."""

    def __init__(
        self,
        app_name: str,
        servers: List[Dict[str, str]],
        memory_path: Optional[Union[str, Path]] = None,
        log_path: Optional[Union[str, Path]] = None,
        agent_config: Optional[AgentConfig] = None,
    ) -> None:
        """Initialize shared infrastructure."""

        self.app_name = app_name
        self.host = Host()
        self.logger = EventLogger(log_path or self._default_log_path(app_name))
        self.renderer = PlainRenderer()
        self.memory = SqliteMemory(memory_path or self._default_memory_path(app_name))
        self.agent_config = agent_config
        self._agent_runtime: Optional[AgentRuntime] = None
        self._tool_registry: Optional[ToolRegistry] = None
        self._telemetry: Optional[TelemetryCollector] = None
        self._connections: List[Connection] = []
        self._connect_servers(servers)

    def run(self) -> None:
        """Start the interactive session."""

        session_id = str(uuid.uuid4())
        command_bus = CommandBus(event_logger=self.logger)
        self._register_base_commands(command_bus)
        self.register_commands(command_bus)
        if self._agent_enabled():
            self._register_agent_commands(command_bus)

        ctx = CLIContext(
            app_name=self.app_name,
            host=self.host,
            memory=self.memory,
            renderer=self.renderer,
            session_id=session_id,
        )
        router = None
        if self._agent_enabled():
            agent_config = self._ensure_agent_config()
            self._tool_registry = self._build_tool_registry(ctx)
            self._telemetry = TelemetryCollector()
            self._agent_runtime = AgentRuntime(
                agent_config,
                self._tool_registry,
                self.memory,
                telemetry=self._telemetry,
                event_logger=self.logger,
            )
            ctx.agent_trace_id = self._agent_runtime.start_session(session_id)
            ctx.agent = self._agent_runtime
            router = CommandRouter(
                command_bus,
                agent=self._agent_runtime,
                event_logger=self.logger,
            )
        try:
            Repl.run(ctx, command_bus, router=router)
        finally:
            self.host.close()

    @abstractmethod
    def register_commands(self, command_bus: CommandBus) -> None:
        """Register application-specific commands."""

    def _register_base_commands(self, command_bus: CommandBus) -> None:
        """Register framework-provided commands."""

        def help_handler(ctx: CLIContext, _args: list[str]) -> None:
            """Render a list of commands."""

            ctx.renderer.info("Available commands:")
            for command in command_bus.list_commands():
                aliases = ", ".join(f"/{alias}" for alias in command.aliases if alias)
                suffix = f" ({aliases})" if aliases else ""
                ctx.renderer.info(f"/{command.name}{suffix} - {command.help}")

        def exit_handler(ctx: CLIContext, _args: list[str]) -> None:
            """Exit the REPL."""

            ctx.renderer.info("Requesting shutdown...")
            raise SystemExit(0)

        command_bus.register(
            Command(
                name="help",
                help="List available slash commands.",
                handler=help_handler,
            )
        )
        command_bus.register(
            Command(
                name="exit",
                help="Exit the session.",
                handler=exit_handler,
                aliases=("quit", "q"),
            )
        )
        command_bus.register(
            Command(
                name="list",
                help="List resources and tools from connected servers.",
                handler=self._cmd_list,
            )
        )
        command_bus.register(
            Command(
                name="execute",
                help="Execute a tool: /execute <server_name> <tool_name>",
                handler=self._cmd_execute,
            )
        )

    def _register_agent_commands(self, command_bus: CommandBus) -> None:
        def usage_handler(ctx: CLIContext, _args: list[str]) -> None:
            if ctx.agent is None:
                ctx.renderer.warn("Usage telemetry unavailable.")
                return
            totals = ctx.agent.telemetry.session_total(ctx.session_id)
            ctx.renderer.info(
                f"Token usage: input={totals.input_tokens} output={totals.output_tokens} total={totals.total_tokens}"
            )

        command_bus.register(
            Command(
                name="usage",
                help="Show token usage for this session.",
                handler=usage_handler,
            )
        )

    @staticmethod
    def _default_memory_path(app_name: str) -> str:
        """Return a deterministic memory file path."""

        slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in app_name)
        slug = slug.strip("_") or "mash"
        return f".{slug}_memory.sqlite3"

    @staticmethod
    def _default_log_path(app_name: str) -> str:
        """Return default log file path."""

        slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in app_name)
        slug = slug.strip("_") or "mash"
        return f".{slug}.log"

    @property
    def connections(self) -> List[Connection]:
        """Expose established server connections."""

        return list(self._connections)

    def connection_by_name(self, name: str) -> Optional[Connection]:
        """Lookup a connection by its configured name."""

        needle = name.strip().lower()
        for connection in self._connections:
            if connection.name.strip().lower() == needle:
                return connection
        return None

    def _connect_servers(self, configs: List[Dict[str, Any]]) -> None:
        """Initialize configured servers and store connections."""

        if not configs:
            return
        for entry in configs:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url.strip():
                self._emit_debug(
                    "server.config.invalid",
                    {"entry": entry, "reason": "missing_url"},
                )
                continue
            name = str(entry.get("name") or url).strip() or url
            self.renderer.info(f"Connecting to {name} ...")
            headers = self._extract_headers(entry.get("headers"))
            try:
                client = self.host.get_client(url, name, headers=headers)
            except MCPClientError as exc:
                self.renderer.error(f"Failed to connect to {name}: {exc}")
                self._emit_debug(
                    "server.connect.error",
                    {"server": name, "url": url, "error": str(exc)},
                )
                continue
            connection = Connection(name=name, url=url, client=client)
            self._connections.append(connection)
            self._render_server_overview(connection)

    def _render_server_overview(self, connection: Connection) -> None:
        """Display basic server info."""

        try:
            info = connection.client.get_server_info()
        except MCPClientError as exc:
            self.renderer.warn(
                f"Connected to {connection.name} but failed to fetch info: {exc}"
            )
            return
        server_info = info.get("serverInfo", {})
        server_name = server_info.get("name", connection.name)
        version = server_info.get("version", "unknown")
        protocol = info.get("protocolVersion", "unknown")
        self.renderer.info(
            f"Server info: {server_name} (version {version}) - protocol {protocol}"
        )

    # ------------------------------------------------------------------
    # Shared helpers for CLIs
    # ------------------------------------------------------------------
    def safe_list_resources(
        self, ctx: CLIContext, connection: Connection
    ) -> List[Dict[str, Any]]:
        try:
            return connection.client.list_resources()
        except MCPClientError as exc:
            ctx.renderer.warn(f"Failed to list resources for {connection.name}: {exc}")
            return []

    def safe_list_resource_templates(
        self, ctx: CLIContext, connection: Connection
    ) -> List[Dict[str, Any]]:
        try:
            return connection.client.list_resource_templates()
        except MCPClientError as exc:
            ctx.renderer.warn(
                f"Failed to list resource templates for {connection.name}: {exc}"
            )
            return []

    def safe_list_tools(
        self, ctx: CLIContext, connection: Connection
    ) -> List[Dict[str, Any]]:
        try:
            return connection.client.list_tools()
        except MCPClientError as exc:
            ctx.renderer.warn(f"Failed to list tools for {connection.name}: {exc}")
            return []

    def _emit_debug(self, event_type: str, payload: Dict[str, Any]) -> None:
        event = DebugEvent(
            event_type=event_type,
            app_id=self.app_name,
            session_id=None,
            payload=payload,
        )
        self.logger.emit(event)

    # ------------------------------------------------------------------
    # Default command handlers
    # ------------------------------------------------------------------
    def _cmd_list(self, ctx: CLIContext, args: List[str]) -> None:
        targets: List[Connection]
        if args:
            connection = self.connection_by_name(args[0])
            if connection is None:
                ctx.renderer.warn(f"Unknown server '{args[0]}'.")
                return
            targets = [connection]
        else:
            targets = list(self._connections)
        if not targets:
            ctx.renderer.warn("No server connections configured.")
            return
        for connection in targets:
            ctx.renderer.info("")
            ctx.renderer.info(f"[{connection.name}] {connection.url}")
            resources = self.safe_list_resources(ctx, connection)
            templates = self.safe_list_resource_templates(ctx, connection)
            tools = self.safe_list_tools(ctx, connection)
            ctx.renderer.info("Resources:")
            if not resources:
                ctx.renderer.info("  (no static resources)")
            else:
                for resource in resources:
                    uri = resource.get("uri", "<unknown>")
                    desc = resource.get("description", "") or resource.get("name", "")
                    ctx.renderer.info(
                        f"  - {uri} {f'- {desc}' if desc else ''}".rstrip()
                    )
            ctx.renderer.info("Resource templates:")
            if not templates:
                ctx.renderer.info("  (no templates advertised)")
            else:
                for idx, template in enumerate(templates, 1):
                    name = template.get("name", f"template_{idx}")
                    desc = template.get("description", "")
                    ctx.renderer.info(f"  [r{idx}] {name} - {desc}")
            ctx.renderer.info("Tools:")
            if not tools:
                ctx.renderer.info("  (no tools available)")
            else:
                for idx, tool in enumerate(tools, 1):
                    name = tool.get("name", f"tool_{idx}")
                    desc = tool.get("description", "")
                    ctx.renderer.info(f"  [t{idx}] {name} - {desc}")

    def _build_tool_registry(self, ctx: CLIContext) -> ToolRegistry:
        registry = ToolRegistry()

        for connection in self._connections:
            tools = self.safe_list_tools(ctx, connection)
            for tool in tools:
                name = str(tool.get("name") or "").strip()
                if not name:
                    continue
                safe_tool = normalize_tool_name(name)
                tool_name = f"mcp_{normalize_tool_name(connection.name)}_{safe_tool}"
                desc = str(tool.get("description") or "")
                input_schema = tool.get("inputSchema")
                if not isinstance(input_schema, dict):
                    input_schema = {"type": "object", "properties": {}}

                def _invoke(
                    args: Dict[str, Any],
                    _ctx: Optional[CLIContext],
                    *,
                    _client=connection.client,
                    _tool=name,
                ) -> Any:
                    return _client.call_tool(_tool, args or {})

                registry.register(
                    ToolSpec(
                        name=tool_name,
                        description=desc or f"MCP tool {name}",
                        input_schema=input_schema,
                        source="mcp",
                        tags={normalize_tool_name(connection.name)},
                        metadata={
                            "server": connection.name,
                            "tool": name,
                            "tool_alias": safe_tool,
                        },
                        invoke=_invoke,
                    )
                )
        return registry

    def _agent_enabled(self) -> bool:
        return self.agent_config is not None

    def _ensure_agent_config(self) -> AgentConfig:
        if self.agent_config is None:
            raise RuntimeError("Agent config is required for agent mode.")
        if not self.agent_config.app_id:
            self.agent_config.app_id = self.app_name
        return self.agent_config

    def _cmd_execute(self, ctx: CLIContext, args: List[str]) -> None:
        if len(args) < 2:
            ctx.renderer.warn("Usage: /execute <server_name> <tool_name>")
            return
        server_name, tool_name = args[0], args[1]
        connection = self.connection_by_name(server_name)
        if connection is None:
            ctx.renderer.warn(f"Unknown server '{server_name}'.")
            return
        tools = self.safe_list_tools(ctx, connection)
        tool = self.find_tool(tool_name, tools)
        if tool is None:
            ctx.renderer.warn(f"Tool '{tool_name}' not found on {connection.name}.")
            return
        try:
            arguments = self.collect_tool_arguments(tool)
        except ValueError as exc:
            ctx.renderer.error(str(exc))
            return
        ctx.renderer.info(f"Invoking {tool.get('name')} on {connection.name} ...")
        try:
            result = connection.client.call_tool(tool.get("name", ""), arguments)
        except MCPClientError as exc:
            ctx.renderer.error(f"Tool invocation failed: {exc}")
            return
        ctx.renderer.info("Tool result:")
        ctx.renderer.code(json.dumps(result, indent=2), lang="json")

    @staticmethod
    def find_tool(name: str, tools: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        lowered = name.lower()
        for tool in tools:
            tool_name = str(tool.get("name") or "").lower()
            if tool_name == lowered:
                return tool
        return None

    def collect_tool_arguments(self, tool: Dict[str, Any]) -> Dict[str, Any]:
        schema = tool.get("inputSchema")
        if isinstance(schema, dict):
            argument_defs = self._build_arguments_from_json_schema(schema)
            if argument_defs:
                return self._collect_prompt_inputs({"arguments": argument_defs})
        raw = input(
            "Enter tool arguments as JSON object (leave blank for {}): "
        ).strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON payload: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        return parsed

    def _collect_prompt_inputs(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        collected: Dict[str, Any] = {}
        arg_schema = self._resolve_argument_schema(schema)
        for argument in arg_schema:
            name = argument.get("name")
            if not isinstance(name, str):
                continue
            arg_type = argument.get("type", "string")
            required = bool(argument.get("required"))
            default = argument.get("default")
            description = argument.get("description", "")
            while True:
                prompt_text = f"Enter {name}"
                if description:
                    prompt_text += f" ({description})"
                if default is not None:
                    prompt_text += f" [default: {default}]"
                prompt_text += ": "
                raw = input(prompt_text).strip()
                if not raw and default is not None:
                    collected[name] = default
                    break
                if not raw and not required:
                    break
                if not raw and required:
                    print("This field is required.")
                    continue
                try:
                    collected[name] = self._coerce_argument_value(raw, arg_type)
                    break
                except ValueError as exc:
                    print(f"Invalid value: {exc}")
        return collected

    def _resolve_argument_schema(self, schema: Dict[str, Any]) -> List[Dict[str, Any]]:
        if "arguments" in schema:
            args = schema.get("arguments")
            if isinstance(args, list):
                return [arg for arg in args if isinstance(arg, dict)]
        content = schema.get("content")
        if isinstance(content, dict):
            text = content.get("text")
            parsed = None
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
            elif isinstance(text, dict):
                parsed = text
            if isinstance(parsed, dict):
                args = parsed.get("arguments")
                if isinstance(args, list):
                    return [arg for arg in args if isinstance(arg, dict)]
        return []

    def _coerce_argument_value(self, value: str, arg_type: str) -> Any:
        if arg_type == "number":
            if "." in value:
                return float(value)
            return int(value)
        if arg_type == "boolean":
            lowered = value.lower()
            if lowered in {"true", "yes", "1"}:
                return True
            if lowered in {"false", "no", "0"}:
                return False
            raise ValueError("enter true/false")
        if arg_type == "array":
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"enter JSON array ({exc})") from exc
            if not isinstance(parsed, list):
                raise ValueError("value must be a JSON array")
            return parsed
        return value

    def _build_arguments_from_json_schema(
        self, schema: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        if not isinstance(schema, dict):
            return []
        props = schema.get("properties")
        if not isinstance(props, dict):
            return []
        explicit_type = schema.get("type")
        if explicit_type not in (None, "object"):
            return []
        required_raw = schema.get("required")
        required = (
            {entry for entry in required_raw if isinstance(entry, str)}
            if isinstance(required_raw, list)
            else set()
        )
        arguments: List[Dict[str, Any]] = []
        for name, prop_schema in props.items():
            if not isinstance(name, str) or not isinstance(prop_schema, dict):
                continue
            argument: Dict[str, Any] = {
                "name": name,
                "type": self._normalize_schema_type(prop_schema.get("type")),
                "description": prop_schema.get("description", ""),
                "required": name in required,
            }
            if "default" in prop_schema:
                argument["default"] = prop_schema.get("default")
            arguments.append(argument)
        return arguments

    def _normalize_schema_type(self, raw_type: Any) -> str:
        if isinstance(raw_type, str):
            lowered = raw_type.lower()
            if lowered in {"number", "integer"}:
                return "number"
            if lowered == "boolean":
                return "boolean"
            if lowered == "array":
                return "array"
            return "string"
        if isinstance(raw_type, list):
            for entry in raw_type:
                normalized = self._normalize_schema_type(entry)
                if normalized != "string":
                    return normalized
            return "string"
        return "string"

    @staticmethod
    def _extract_headers(raw: Any) -> Optional[Dict[str, str]]:
        if not isinstance(raw, dict):
            return None
        headers: Dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, str):
                headers[key] = value
            elif value is not None:
                headers[key] = str(value)
        return headers or None
