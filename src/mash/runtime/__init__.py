"""Mash runtime package."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import AgentClient, AgentClientError
    from .host import AgentHost, HostBuilder
    from .host.subagents import SubAgentMetadata
    from .server import AgentServer
    from .service import AgentRuntime
    from .spec import AgentSpec

__all__ = [
    "AgentSpec",
    "AgentRuntime",
    "AgentServer",
    "AgentClient",
    "AgentClientError",
    "AgentHost",
    "HostBuilder",
    "SubAgentMetadata",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentSpec": (".spec", "AgentSpec"),
    "AgentRuntime": (".service", "AgentRuntime"),
    "AgentServer": (".server", "AgentServer"),
    "AgentClient": (".client", "AgentClient"),
    "AgentClientError": (".client", "AgentClientError"),
    "AgentHost": (".host", "AgentHost"),
    "HostBuilder": (".host", "HostBuilder"),
    "SubAgentMetadata": (".host.subagents", "SubAgentMetadata"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value

