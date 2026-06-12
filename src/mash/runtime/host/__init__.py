"""Pool and host composition exports.

Lazy exports: submodules here import back into ``mash.runtime.service`` and
``mash.runtime.factory``, which themselves import ``host.subagents``. Keeping
this package init lazy breaks the cycle.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .builder import HostBuilder
    from .host import AgentPool
    from .subagents import AgentMetadata
    from .types import Host

__all__ = ["AgentMetadata", "AgentPool", "Host", "HostBuilder"]

_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentMetadata": (".subagents", "AgentMetadata"),
    "AgentPool": (".host", "AgentPool"),
    "Host": (".types", "Host"),
    "HostBuilder": (".builder", "HostBuilder"),
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
