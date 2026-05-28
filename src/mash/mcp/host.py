"""MCP Host for managing client instances and handling elicitation."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from .client import MCPHTTPClient

# Load environment variables
load_dotenv()

class Host:
    """Host process that manages MCP client instances and interactions.

    The host is responsible for creating/tearing down clients, mediating
    elicitation requests, and enforcing basic policies before handing data off
    to humans.
    """

    def __init__(self) -> None:
        self._clients: Dict[str, MCPHTTPClient] = {}

    def get_client(
        self, url: str, name: str, headers: Optional[Dict[str, str]] = None
    ) -> MCPHTTPClient:
        """Return a singleton MCP client for the given server URL."""
        normalized_headers = (
            {
                str(key): str(value)
                for key, value in headers.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            if headers
            else None
        )
        cache_key = self._client_cache_key(url, normalized_headers)
        client = self._clients.get(cache_key)
        if client is not None:
            return client
        client = MCPHTTPClient(
            url,
            client_name=name,
            default_headers=normalized_headers,
            elicitation_handler=self._handle_elicitation_request,
        )
        self._clients[cache_key] = client
        return client

    def _client_cache_key(self, url: str, headers: Optional[Dict[str, str]]) -> str:
        if not headers:
            return url
        header_blob = "|".join(
            f"{key}:{value}" for key, value in sorted(headers.items())
        )
        return f"{url}|{header_blob}"

    def close(self) -> None:
        """Shut down all managed MCP client connections."""
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    # ------------------------------------------------------------------
    # Elicitation handling
    # ------------------------------------------------------------------
    def _handle_elicitation_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Prompt the operator for more input when servers request it."""

        question = (
            request.get("message")
            or request.get("prompt")
            or "Server requested additional input"
        )
        print(f"[elicitation] {question}")
        answer = input("> ")
        return {
            "elicitationId": request.get("elicitationId") or str(uuid.uuid4()),
            "response": answer,
        }


__all__ = ["Host"]
