"""Mash public package: authoring surface and package metadata.

Agents and workflows import as peers from the package root:

    from mash import (
        AgentSpec, AgentMetadata, Host, HostBuilder, Pool,
        WorkflowSpec, CodeStep, AgentStep, StepContext,
    )

Exports are lazy so ``import mash`` never drags in DBOS or provider SDKs.
"""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, metadata, version
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mash.runtime import AgentMetadata, AgentSpec, Host, HostBuilder, Pool
    from mash.workflows import AgentStep, CodeStep, StepContext, WorkflowSpec

_DEFAULT_DOCS_URL = "https://github.com/imsid/mashpy#readme"

try:
    __version__ = version("mashpy")
except PackageNotFoundError:  # pragma: no cover - source tree without install metadata
    __version__ = "0.0.0"


def get_docs_url() -> str:
    """Return the package documentation URL from package metadata when available."""
    try:
        project_urls = metadata("mashpy").get_all("Project-URL") or []
    except PackageNotFoundError:
        return _DEFAULT_DOCS_URL

    for entry in project_urls:
        if "," not in entry:
            continue
        name, url = entry.split(",", 1)
        if name.strip().lower() == "documentation":
            return url.strip()
    return _DEFAULT_DOCS_URL


_EXPORTS: dict[str, str] = {
    "AgentSpec": "mash.runtime",
    "AgentMetadata": "mash.runtime",
    "Host": "mash.runtime",
    "HostBuilder": "mash.runtime",
    "Pool": "mash.runtime",
    # The leaf module, not mash.workflows: the package init pulls in the
    # DBOS-backed service, which authoring-time imports must not load.
    "WorkflowSpec": "mash.workflows.spec",
    "CodeStep": "mash.workflows.spec",
    "AgentStep": "mash.workflows.spec",
    "StepContext": "mash.workflows.spec",
}


def __getattr__(name: str) -> Any:
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "AgentSpec",
    "AgentMetadata",
    "Host",
    "HostBuilder",
    "Pool",
    "WorkflowSpec",
    "CodeStep",
    "AgentStep",
    "StepContext",
    "__version__",
    "get_docs_url",
]
