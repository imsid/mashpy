"""Backend-agnostic async memory store protocol."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from ..search.types import SearchColumn


class MemoryStore(Protocol):
    """Async protocol for conversation storage."""

    async def open(self) -> None:
        """Initialize backend resources."""
        ...

    async def close(self) -> None:
        """Close backend resources."""
        ...

    async def save_logs(
        self,
        logs: List[Dict[str, Any]],
    ) -> None:
        """Persist one or more structured log records."""
        ...

    async def get_logs(
        self,
        app_id: str,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: Optional[int] = None,
        after_log_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return structured log records for one app/session/trace scope."""
        ...

    async def get_latest_log_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the latest trace summary from persisted logs."""
        ...

    async def list_recent_log_traces(
        self,
        app_id: str,
        session_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """List recent trace summaries from persisted logs."""
        ...

    async def save_turn(
        self,
        trace_id: str,
        session_id: str,
        app_id: str,
        user_message: str,
        agent_response: str,
        signals: Dict[str, Any],
        session_total_tokens: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Save a conversation turn with signals."""
        ...

    async def get_turns(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get conversation turns for a session."""
        ...

    async def list_sessions(
        self,
        app_id: str,
    ) -> List[Dict[str, Any]]:
        """List persisted sessions for one application."""
        ...

    async def get_latest_session(
        self,
        app_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent persisted session for one application."""
        ...

    async def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent trace for a session in one application."""
        ...

    async def list_recent_traces(
        self,
        app_id: str,
        session_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """List recent traces for a session in one application."""
        ...

    async def get_turn_by_ids(
        self,
        pairs: List[Dict[str, str]],
    ) -> Optional[List[Dict[str, Any]]]:
        """Get turns by exact session/turn identifier pairs in one lookup."""
        ...

    async def keyword_search(
        self,
        column: SearchColumn,
        query_term: str,
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search turns by keyword in a single column."""
        ...

    async def semantic_search(
        self,
        column: SearchColumn,
        query_term: str,
        query_embedding: Optional[List[float]],
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search turns semantically in a single column."""
        ...

    async def get_preferences(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get user preferences for app and session."""
        ...

    async def get_latest_preferences(
        self,
        app_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get latest user preferences for app."""
        ...

    async def set_preferences(
        self,
        app_id: str,
        session_id: str,
        preferences: Dict[str, Any],
    ) -> None:
        """Set user preferences for app and session."""
        ...

    async def get_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
    ) -> Optional[Any]:
        """Get app-specific data by key."""
        ...

    async def set_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
        value: Any,
    ) -> None:
        """Set app-specific data by key."""
        ...

    async def list_app_data(
        self,
        app_id: str,
        session_id: str,
    ) -> List[Dict[str, Any]]:
        """List all app-specific data for session."""
        ...

    async def delete_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
    ) -> bool:
        """Delete app-specific data by key."""
        ...
