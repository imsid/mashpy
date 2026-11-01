"""Runtime tools for agent memory and search."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Callable, Dict, List

from ..memory.search.service import MemorySearchService
from ..memory.search.types import SearchResult
from ..memory.store import MemoryStore
from .base import FunctionTool, Tool, ToolResult


class RuntimeToolBuilder:
    """Builder for runtime tools with app, session and user context.

    Conversation tools reach the current session, or with ``user_id`` the
    user's sessions with this agent. They never reach another user's turns.
    """

    def __init__(
        self,
        store: MemoryStore,
        app_id: str,
        session_id: str | None = None,
        session_id_provider: Callable[[], str] | None = None,
        user_id: str | None = None,
    ) -> None:
        self._store = store
        self._app_id = app_id
        self._user_id = user_id
        if session_id_provider is not None:
            self._session_id_provider = session_id_provider
        elif session_id is not None:
            self._session_id_provider = lambda: session_id
        else:
            raise ValueError("session_id or session_id_provider is required")
        self._search_service = MemorySearchService(store=store)

    def _session_id(self) -> str:
        session_id = str(self._session_id_provider() or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        return session_id

    def build_tools(self) -> List[Tool]:
        return [
            self._build_search_conversations_tool(),
            self._build_get_full_turn_message_tool(),
        ]

    def build_get_latest_session_tool(self) -> Tool:
        async def execute(_args: Dict[str, Any]) -> ToolResult:
            session = await self._store.get_latest_session(app_id=self._app_id)
            if session is None:
                return ToolResult.error("no sessions found for this app")
            return ToolResult(content=json.dumps(session, indent=2), is_error=False)

        return FunctionTool(
            name="get_latest_session",
            description=(
                "Return the most recent session for the target app from the runtime "
                "store. Use this to resolve which session to inspect before fetching logs."
            ),
            parameters={"type": "object", "properties": {}},
            _executor=execute,
        )

    def build_get_latest_trace_tool(self) -> Tool:
        async def execute(args: Dict[str, Any]) -> ToolResult:
            raw_session_id = args.get("session_id")
            if raw_session_id is None:
                latest_session = await self._store.get_latest_session(app_id=self._app_id)
                if latest_session is None:
                    return ToolResult.error("no sessions found for this app")
                session_id = str(latest_session["session_id"])
            elif isinstance(raw_session_id, str) and raw_session_id.strip():
                session_id = raw_session_id.strip()
            else:
                return ToolResult.error("session_id must be a non-empty string if provided")

            trace = await self._store.get_latest_trace(
                app_id=self._app_id,
                session_id=session_id,
            )
            if trace is None:
                return ToolResult.error(f"no traces found for session: {session_id}")
            return ToolResult(content=json.dumps(trace, indent=2), is_error=False)

        return FunctionTool(
            name="get_latest_trace",
            description=(
                "Return the latest trace in a session from the runtime store. If "
                "session_id is omitted, resolve the latest session first."
            ),
            parameters={"type": "object", "properties": {"session_id": {"type": "string"}}},
            _executor=execute,
        )

    def build_list_recent_traces_tool(self) -> Tool:
        async def execute(args: Dict[str, Any]) -> ToolResult:
            raw_session_id = args.get("session_id")
            raw_limit = args.get("limit", 5)
            if raw_session_id is None:
                latest_session = await self._store.get_latest_session(app_id=self._app_id)
                if latest_session is None:
                    return ToolResult.error("no sessions found for this app")
                session_id = str(latest_session["session_id"])
            elif isinstance(raw_session_id, str) and raw_session_id.strip():
                session_id = raw_session_id.strip()
            else:
                return ToolResult.error("session_id must be a non-empty string if provided")

            try:
                limit = max(1, int(raw_limit))
            except (TypeError, ValueError):
                return ToolResult.error("limit must be an integer")

            traces = await self._store.list_recent_traces(
                app_id=self._app_id,
                session_id=session_id,
                limit=limit,
            )
            payload = {"session_id": session_id, "limit": limit, "traces": traces}
            return ToolResult(content=json.dumps(payload, indent=2), is_error=False)

        return FunctionTool(
            name="list_recent_traces",
            description=(
                "List recent traces in a session from the runtime store. If session_id "
                "is omitted, resolve the latest session first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
            },
            _executor=execute,
        )

    def _build_get_conversation_tool(self) -> Tool:
        async def execute(args: Dict[str, Any]) -> ToolResult:
            turns = await self._store.get_turns(
                session_id=self._session_id(),
                app_id=self._app_id,
                limit=args.get("limit"),
            )
            messages = []
            for turn in turns:
                messages.append({"role": "user", "content": turn["user_message"]})
                messages.append({"role": "assistant", "content": turn["agent_response"]})
            return ToolResult(content=json.dumps(messages, indent=2), is_error=False)

        return FunctionTool(
            name="get_conversation",
            description="Get the conversation history for this session.",
            parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
            _executor=execute,
        )

    def _build_search_conversations_tool(self) -> Tool:
        async def execute(args: Dict[str, Any]) -> ToolResult:
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return ToolResult.error("query is required and must be a non-empty string")
            query = query.strip()

            raw_limit = args.get("limit", 10)
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                return ToolResult.error("limit must be an integer")

            scopes = self._search_scopes()
            scope_error = "scope must be " + " or ".join(f"'{value}'" for value in scopes)
            scope = args.get("scope", "session")
            if not isinstance(scope, str):
                return ToolResult.error(scope_error)
            scope = scope.strip().lower() or "session"
            if scope not in scopes:
                return ToolResult.error(scope_error)

            search_session_id = self._session_id() if scope == "session" else None
            try:
                normalized_queries = self._normalize_search_queries(query)
                results = await self._search_with_prefixes(
                    normalized_queries,
                    app_id=self._app_id,
                    limit=limit,
                    session_id=search_session_id,
                    user_id=self._user_id,
                )
            except (ValueError, NotImplementedError, RuntimeError) as exc:
                return ToolResult.error(str(exc))
            except Exception as exc:  # pragma: no cover
                return ToolResult.error(f"search failed: {exc}")

            payload = {
                "query": query,
                "effective_queries": normalized_queries,
                "scope": scope,
                "session_id": search_session_id,
                "limit": limit,
                "results": [asdict(result) for result in results],
            }
            return ToolResult(content=json.dumps(payload, indent=2), is_error=False)

        return FunctionTool(
            name="search_conversations",
            description=(
                "Search stored conversation turns and return ranked previews plus "
                "the session_id and trace_id needed to retrieve full messages. "
                + (
                    "Scope 'session' searches this conversation; 'user' searches all "
                    "of this user's conversations with you."
                    if self._user_id
                    else "Searches this conversation."
                )
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "scope": {"type": "string", "enum": self._search_scopes()},
                },
                "required": ["query"],
            },
            _executor=execute,
        )

    def _search_scopes(self) -> list[str]:
        return ["session", "user"] if self._user_id else ["session"]

    @staticmethod
    def _normalize_search_queries(query: str) -> list[str]:
        stripped = query.strip()
        lowered = stripped.lower()
        if lowered.startswith("@user:") or lowered.startswith("@agent:"):
            return [stripped]
        return [f"@user:{stripped}", f"@agent:{stripped}"]

    async def _search_with_prefixes(
        self,
        queries: list[str],
        *,
        app_id: str,
        limit: int,
        session_id: str | None,
        user_id: str | None,
    ) -> list[SearchResult]:
        merged: dict[tuple[str, str], SearchResult] = {}
        for prefixed_query in queries:
            for result in await self._search_service.search(
                prefixed_query,
                app_id=app_id,
                limit=limit,
                session_id=session_id,
                user_id=user_id,
            ):
                key = (result.session_id, result.trace_id)
                existing = merged.get(key)
                if existing is None or result.similarity_score > existing.similarity_score:
                    merged[key] = result
        return sorted(
            merged.values(),
            key=lambda item: item.similarity_score,
            reverse=True,
        )[:limit]

    def _build_get_full_turn_message_tool(self) -> Tool:
        async def execute(args: Dict[str, Any]) -> ToolResult:
            raw_pairs = args.get("pairs")
            if not isinstance(raw_pairs, list) or not raw_pairs:
                return ToolResult.error("pairs is required and must be a non-empty array")

            normalized_pairs: List[Dict[str, str]] = []
            for idx, pair in enumerate(raw_pairs):
                if not isinstance(pair, dict):
                    return ToolResult.error(
                        f"pairs[{idx}] must be an object with session_id and trace_id"
                    )
                session_id = pair.get("session_id")
                trace_id = pair.get("trace_id")
                if not isinstance(session_id, str) or not session_id.strip():
                    return ToolResult.error(f"pairs[{idx}].session_id must be a non-empty string")
                if not isinstance(trace_id, str) or not trace_id.strip():
                    return ToolResult.error(f"pairs[{idx}].trace_id must be a non-empty string")
                normalized_pairs.append(
                    {"session_id": session_id.strip(), "trace_id": trace_id.strip()}
                )

            # Without a user, only this session's turns are reachable; pairs
            # from other sessions come back as missing.
            lookup_pairs = normalized_pairs
            if self._user_id is None:
                current_session_id = self._session_id()
                lookup_pairs = [
                    pair for pair in normalized_pairs
                    if pair["session_id"] == current_session_id
                ]
            try:
                turns = (
                    await self._store.get_turn_by_ids(
                        pairs=lookup_pairs,
                        app_id=self._app_id,
                        user_id=self._user_id,
                    )
                    if lookup_pairs
                    else []
                )
            except Exception as exc:  # pragma: no cover
                return ToolResult.error(f"failed to fetch turns: {exc}")

            found_turns = turns or []
            found_keys = {
                (str(turn.get("session_id")), str(turn.get("trace_id")))
                for turn in found_turns
            }
            missing_pairs = [
                pair
                for pair in normalized_pairs
                if (pair["session_id"], pair["trace_id"]) not in found_keys
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
            description="Expand one or more search results into full turn text.",
            parameters={
                "type": "object",
                "properties": {
                    "pairs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string"},
                                "trace_id": {"type": "string"},
                            },
                            "required": ["session_id", "trace_id"],
                        },
                    }
                },
                "required": ["pairs"],
            },
            _executor=execute,
        )

__all__ = ["RuntimeToolBuilder"]
