"""Runtime tools for agent memory and preferences.

These tools are automatically available to all Mash agents and provide:
- Conversation memory access
- User preference storage
- App-specific data persistence

All tools are app-scoped for clean isolation between applications.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Callable, Dict, List

from ..logging import EventLogger
from ..memory.search.service import MemorySearchService
from ..memory.search.types import SearchResult
from ..memory.store import MemoryStore
from .base import FunctionTool, Tool, ToolResult


class RuntimeToolBuilder:
    """Builder for runtime tools with app and session context."""

    def __init__(
        self,
        store: MemoryStore,
        app_id: str,
        event_logger: EventLogger,
        session_id: str | None = None,
        session_id_provider: Callable[[], str] | None = None,
    ) -> None:
        """Initialize runtime tool builder.

        Args:
            store: Conversation store for persistence.
            app_id: Application ID for isolation.
            session_id: Fixed session ID for compatibility with direct/unit usage.
            session_id_provider: Callable returning the active session ID.
        """
        self._store = store
        self._app_id = app_id
        if session_id_provider is not None:
            self._session_id_provider = session_id_provider
        elif session_id is not None:
            self._session_id_provider = lambda: session_id
        else:
            raise ValueError("session_id or session_id_provider is required")
        self._search_service = MemorySearchService(
            store=store,
            event_logger=event_logger,
        )

    def _session_id(self) -> str:
        session_id = str(self._session_id_provider() or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        return session_id

    def build_tools(self) -> List[Tool]:
        """Build runtime tools for this app and session."""
        return [
            self._build_search_conversations_tool(),
            self._build_get_full_turn_message_tool(),
            self._build_get_user_preferences_tool(),
            self._build_set_user_preferences_tool(),
            self._build_list_app_data_tool(),
            self._build_set_app_data_tool(),
        ]

    def _build_get_conversation_tool(self) -> Tool:
        """Tool to get conversation history."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            limit = args.get("limit")
            session_id = self._session_id()
            turns = self._store.get_turns(
                session_id=session_id,
                limit=limit,
            )
            # Format as conversation messages
            messages = []
            for turn in turns:
                messages.append(
                    {
                        "role": "user",
                        "content": turn["user_message"],
                    }
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn["agent_response"],
                    }
                )
            return ToolResult(
                content=json.dumps(messages, indent=2),
                is_error=False,
            )

        return FunctionTool(
            name="get_conversation",
            description=(
                "Get the conversation history for this session. "
                "Optionally limit the number of recent turns returned. "
                "Use this to reference earlier parts of the conversation "
                "instead of relying on context window."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of turns to return (optional)",
                    },
                },
            },
            _executor=execute,
        )

    def _build_get_user_preferences_tool(self) -> Tool:
        """Tool to get user preferences."""

        def execute(_args: Dict[str, Any]) -> ToolResult:
            session_id = self._session_id()
            preferences = self._store.get_preferences(
                app_id=self._app_id,
                session_id=session_id,
            )
            if preferences is None:
                return ToolResult(
                    content="No preferences stored.",
                    is_error=False,
                )
            return ToolResult(
                content=json.dumps(preferences, indent=2),
                is_error=False,
            )

        return FunctionTool(
            name="get_user_preferences",
            description=(
                "Get stored user preferences for this session. "
                "Preferences are persistent across conversations. "
                "Check this at the start of conversations to maintain user context."
            ),
            parameters={"type": "object", "properties": {}},
            _executor=execute,
        )

    def _build_search_conversations_tool(self) -> Tool:
        """Tool to search conversation history."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return ToolResult.error(
                    "query is required and must be a non-empty string"
                )
            query = query.strip()

            raw_limit = args.get("limit", 10)
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                return ToolResult.error("limit must be an integer")

            scope = args.get("scope", "session")
            if not isinstance(scope, str):
                return ToolResult.error("scope must be 'session' or 'app'")
            scope = scope.strip().lower() or "session"
            if scope not in {"session", "app"}:
                return ToolResult.error("scope must be 'session' or 'app'")

            search_session_id = self._session_id() if scope == "session" else None
            try:
                normalized_queries = self._normalize_search_queries(query)
                results = self._search_with_prefixes(
                    normalized_queries,
                    app_id=self._app_id,
                    limit=limit,
                    session_id=search_session_id,
                )
            except (ValueError, NotImplementedError, RuntimeError) as exc:
                return ToolResult.error(str(exc))
            except Exception as exc:  # pragma: no cover - defensive guard
                return ToolResult.error(f"search failed: {exc}")

            payload = {
                "query": query,
                "effective_queries": normalized_queries,
                "scope": scope,
                "app_id": self._app_id,
                "session_id": search_session_id,
                "limit": limit,
                "results": [asdict(result) for result in results],
            }
            return ToolResult(
                content=json.dumps(payload, indent=2),
                is_error=False,
            )

        return FunctionTool(
            name="search_conversations",
            description=(
                "Search conversation history for relevant prior turns. "
                "Use scope='session' to search only the current conversation, "
                "or scope='app' to search across all sessions for this app. "
                "Prefix query with @user: to search only user messages "
                "(useful for finding what the user asked for or stated), "
                "or @agent: to search only agent responses "
                "(useful for finding prior answers, explanations, or outputs). "
                "If omitted, the tool searches both and merges ranked results. "
                "Returns ranked results with turn IDs, session IDs, scores, and previews."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Search query. Use @user:<text> to search user messages, "
                            "@agent:<text> to search agent responses, or plain text "
                            "to search both and merge results."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum number of results to return (default: 10)"
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["session", "app"],
                        "description": (
                            "Search scope: current session only ('session') "
                            "or all sessions in this app ('app')"
                        ),
                    },
                },
                "required": ["query"],
            },
            _executor=execute,
        )

    @staticmethod
    def _normalize_search_queries(query: str) -> list[str]:
        """Return one or more prefixed queries accepted by the memory parser."""
        stripped = query.strip()
        lowered = stripped.lower()
        if lowered.startswith("@user:") or lowered.startswith("@agent:"):
            return [stripped]
        return [f"@user:{stripped}", f"@agent:{stripped}"]

    def _search_with_prefixes(
        self,
        queries: list[str],
        *,
        app_id: str,
        limit: int,
        session_id: str | None,
    ) -> list[SearchResult]:
        """Search one or more prefixed queries and merge/dedupe results."""
        merged: dict[tuple[str, str], SearchResult] = {}
        for prefixed_query in queries:
            for result in self._search_service.search(
                prefixed_query,
                app_id=app_id,
                limit=limit,
                session_id=session_id,
            ):
                key = (result.session_id, result.turn_id)
                existing = merged.get(key)
                if (
                    existing is None
                    or result.similarity_score > existing.similarity_score
                ):
                    merged[key] = result
        return sorted(
            merged.values(),
            key=lambda item: item.similarity_score,
            reverse=True,
        )[:limit]

    def _build_get_full_turn_message_tool(self) -> Tool:
        """Tool to fetch full messages for one or more conversation turns."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            raw_pairs = args.get("pairs")
            if not isinstance(raw_pairs, list) or not raw_pairs:
                return ToolResult.error(
                    "pairs is required and must be a non-empty array"
                )

            normalized_pairs: List[Dict[str, str]] = []
            for idx, pair in enumerate(raw_pairs):
                if not isinstance(pair, dict):
                    return ToolResult.error(
                        f"pairs[{idx}] must be an object with session_id and turn_id"
                    )

                session_id = pair.get("session_id")
                turn_id = pair.get("turn_id")
                if not isinstance(session_id, str) or not session_id.strip():
                    return ToolResult.error(
                        f"pairs[{idx}].session_id must be a non-empty string"
                    )
                if not isinstance(turn_id, str) or not turn_id.strip():
                    return ToolResult.error(
                        f"pairs[{idx}].turn_id must be a non-empty string"
                    )

                normalized_pairs.append(
                    {
                        "session_id": session_id.strip(),
                        "turn_id": turn_id.strip(),
                    }
                )

            try:
                turns = self._store.get_turn_by_ids(
                    pairs=normalized_pairs,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                return ToolResult.error(f"failed to fetch turns: {exc}")

            found_turns = turns or []
            found_keys = {
                (str(turn.get("session_id")), str(turn.get("turn_id")))
                for turn in found_turns
            }
            missing_pairs = [
                pair
                for pair in normalized_pairs
                if (pair["session_id"], pair["turn_id"]) not in found_keys
            ]

            return ToolResult(
                content=json.dumps(
                    {
                        "requested_count": len(normalized_pairs),
                        "found_count": len(found_turns),
                        "turns": found_turns,
                        "missing_pairs": missing_pairs,
                    },
                    indent=2,
                ),
                is_error=False,
            )

        return FunctionTool(
            name="get_full_turn_message",
            description=(
                "Fetch full user and assistant messages for one or more turns "
                "using (session_id, turn_id) pairs, typically from "
                "search_conversations results. Returns matched turns with the "
                "full user_message and agent_response text for each result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pairs": {
                        "type": "array",
                        "description": (
                            "List of session/turn pairs from search_conversations "
                            "results to expand into full messages"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "session_id": {
                                    "type": "string",
                                    "description": (
                                        "Session ID from search_conversations results"
                                    ),
                                },
                                "turn_id": {
                                    "type": "string",
                                    "description": (
                                        "Turn ID from search_conversations results"
                                    ),
                                },
                            },
                            "required": ["session_id", "turn_id"],
                        },
                    },
                },
                "required": ["pairs"],
            },
            _executor=execute,
        )

    def _build_set_user_preferences_tool(self) -> Tool:
        """Tool to set user preferences."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            preferences = args.get("preferences")
            if not isinstance(preferences, dict):
                return ToolResult(
                    content="preferences must be a JSON object",
                    is_error=True,
                )
            self._store.set_preferences(
                app_id=self._app_id,
                session_id=self._session_id(),
                preferences=preferences,
            )
            return ToolResult(
                content="Preferences saved successfully.",
                is_error=False,
            )

        return FunctionTool(
            name="set_user_preferences",
            description=(
                "Store user preferences for this session. "
                "Use this to remember user settings, preferences, or context "
                "that should persist across conversations (e.g., depth level, "
                "code style, focus areas, language preferences)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "preferences": {
                        "type": "object",
                        "description": "User preferences as JSON object",
                    },
                },
                "required": ["preferences"],
            },
            _executor=execute,
        )

    def _build_get_app_data_tool(self) -> Tool:
        """Tool to get app-specific data by key."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            key = args.get("key")
            if not key:
                return ToolResult(
                    content="key is required",
                    is_error=True,
                )
            value = self._store.get_app_data(
                app_id=self._app_id,
                session_id=self._session_id(),
                key=str(key),
            )
            if value is None:
                return ToolResult(
                    content=f"No data found for key: {key}",
                    is_error=False,
                )
            return ToolResult(
                content=json.dumps(value, indent=2),
                is_error=False,
            )

        return FunctionTool(
            name="get_app_data",
            description=(
                "Get app-specific data by key. "
                "Use this to retrieve information previously stored "
                "with set_app_data (e.g., file locations, discovered patterns, "
                "configurations)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Data key to retrieve",
                    },
                },
                "required": ["key"],
            },
            _executor=execute,
        )

    def _build_set_app_data_tool(self) -> Tool:
        """Tool to set app-specific data by key."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            key = args.get("key")
            value = args.get("value")
            if not key:
                return ToolResult(
                    content="key is required",
                    is_error=True,
                )
            if value is None:
                return ToolResult(
                    content="value is required",
                    is_error=True,
                )
            self._store.set_app_data(
                app_id=self._app_id,
                session_id=self._session_id(),
                key=str(key),
                value=value,
            )
            return ToolResult(
                content=f"Data stored successfully for key: {key}",
                is_error=False,
            )

        return FunctionTool(
            name="set_app_data",
            description=(
                "Store app-specific data by key. "
                "Use this to persist information that should be available "
                "in future conversations (e.g., discovered file locations, "
                "identified patterns, user-specific configs, important facts). "
                "This helps build up knowledge over time."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Data key (use as a tag/identifier)",
                    },
                    "value": {
                        "description": "Data value (any JSON-serializable type)",
                    },
                },
                "required": ["key", "value"],
            },
            _executor=execute,
        )

    def _build_list_app_data_tool(self) -> Tool:
        """Tool to list all app-specific data."""

        def execute(_args: Dict[str, Any]) -> ToolResult:
            session_id = self._session_id()
            data = self._store.list_app_data(
                app_id=self._app_id,
                session_id=session_id,
            )
            if not data:
                return ToolResult(
                    content="No app data stored.",
                    is_error=False,
                )
            return ToolResult(
                content=json.dumps(data, indent=2),
                is_error=False,
            )

        return FunctionTool(
            name="list_app_data",
            description=(
                "List all app-specific data stored for this session. "
                "Returns all keys and values previously saved with set_app_data. "
                "Use this to see what information you've accumulated."
            ),
            parameters={"type": "object", "properties": {}},
            _executor=execute,
        )

    def _build_delete_app_data_tool(self) -> Tool:
        """Tool to delete app-specific data by key."""

        def execute(args: Dict[str, Any]) -> ToolResult:
            key = args.get("key")
            if not key:
                return ToolResult(
                    content="key is required",
                    is_error=True,
                )
            deleted = self._store.delete_app_data(
                app_id=self._app_id,
                session_id=self._session_id(),
                key=str(key),
            )
            if deleted:
                return ToolResult(
                    content=f"Data deleted successfully for key: {key}",
                    is_error=False,
                )
            else:
                return ToolResult(
                    content=f"No data found for key: {key}",
                    is_error=False,
                )

        return FunctionTool(
            name="delete_app_data",
            description=(
                "Delete app-specific data by key. "
                "Use this to remove information that is no longer needed "
                "or has become outdated."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Data key to delete",
                    },
                },
                "required": ["key"],
            },
            _executor=execute,
        )


__all__ = [
    "RuntimeToolBuilder",
]
