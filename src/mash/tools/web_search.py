"""Web search providers exposed as MCP-backed agent tools.

A web search provider resolves to a single :class:`MCPServerConfig` that the
runtime feeds through the existing remote-tools path. The agent ends up with
plain ``web_search`` and ``web_fetch`` tools; the MCP wiring stays hidden.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import List, Optional

from mash.mcp.types import MCPServerConfig


class WebSearchProvider(ABC):
    """Contract for a web search backend.

    Implementations return the MCP server config that exposes the search and
    fetch tools. Add a new provider (Exa, Tavily, Brave) by subclassing this
    and returning its endpoint; nothing else in the spec surface changes.
    """

    @abstractmethod
    def mcp_server_config(self) -> MCPServerConfig:
        """Return the MCP server config backing this provider's tools."""


class ParallelSearchProvider(WebSearchProvider):
    """Parallel AI web search and fetch.

    Three auth modes, resolved at config time:

    - anonymous: no key, free endpoint, lower rate limits
    - api key: ``api_key`` arg or ``PARALLEL_API_KEY`` env
    - oauth token: ``oauth_token`` arg or ``PARALLEL_OAUTH_TOKEN`` env

    Any token sends ``Authorization: Bearer <token>`` and targets the
    OAuth-capable endpoint. An explicit arg beats the matching env var, and an
    oauth token beats an api key. Pass ``base_url`` to pin a different endpoint.
    """

    FREE_URL = "https://search.parallel.ai/mcp"
    OAUTH_URL = "https://search.parallel.ai/mcp-oauth"
    TOOLS = ["web_search", "web_fetch"]
    SERVER_NAME = "parallel_web_search"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        oauth_token: Optional[str] = None,
        base_url: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
    ) -> None:
        self._api_key = api_key or os.getenv("PARALLEL_API_KEY", "").strip() or None
        self._oauth_token = (
            oauth_token or os.getenv("PARALLEL_OAUTH_TOKEN", "").strip() or None
        )
        self._base_url = base_url
        self._allowed_tools = list(allowed_tools) if allowed_tools else list(self.TOOLS)

    def mcp_server_config(self) -> MCPServerConfig:
        token = self._oauth_token or self._api_key
        url = self._base_url or (self.OAUTH_URL if token else self.FREE_URL)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return MCPServerConfig(
            name=self.SERVER_NAME,
            url=url,
            description="Parallel AI web search and fetch.",
            headers=headers,
            allowed_tools=list(self._allowed_tools),
        )


__all__ = ["WebSearchProvider", "ParallelSearchProvider"]
