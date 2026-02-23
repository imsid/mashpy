"""Backend-agnostic memory store protocol."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from ..search.types import SearchColumn


class MemoryStore(Protocol):
    """Protocol for conversation storage."""

    def save_turn(
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
        """Save a conversation turn with signals.

        Args:
            trace_id: Trace ID for this turn (used as turn_id).
            session_id: Session identifier.
            user_message: User's message.
            agent_response: Agent's response.
            signals: Collected signals for this turn.
            session_total_tokens: Total tokens used in this session after this turn.
            metadata: Optional metadata.
            app_id: Optional application identifier for app-scoped search/filtering.

        Returns:
            Turn ID.
        """
        ...

    def get_turns(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get conversation turns for a session.

        Args:
            session_id: Session identifier.
            limit: Maximum number of turns to return.

        Returns:
            List of turns.
        """
        ...

    def get_turn_by_ids(
        self,
        pairs: List[Dict[str, str]],
    ) -> Optional[List[Dict[str, Any]]]:
        """Get turns by exact session/turn identifier pairs in one lookup.

        Args:
            pairs: List of {"session_id", "turn_id"} pairs to fetch.

        Returns:
            List of dictionaries with full turn text (at least user_message and
            agent_response) for found pairs, or None if no pairs are found.
        """
        ...

    def keyword_search(
        self,
        column: SearchColumn,
        query_term: str,
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search turns by keyword in a single column.

        Returns:
            List of hits ordered by descending score in [0, 1].
            Each hit must include: turn_id, session_id, score, preview.
        """
        ...

    def semantic_search(
        self,
        column: SearchColumn,
        query_term: str,
        query_embedding: Optional[List[float]],
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search turns semantically in a single column.

        Returns:
            List of hits ordered by descending score in [0, 1].
            Each hit must include: turn_id, session_id, score, preview.
        """
        ...

    def get_preferences(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get user preferences for app and session.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.

        Returns:
            User preferences as dictionary, or None if not set.
        """
        ...

    def get_latest_preferences(
        self,
        app_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get latest user preferences for app.

        Args:
            app_id: Application identifier.

        Returns:
            User preferences as dictionary, or None if not set.
        """
        ...

    def set_preferences(
        self,
        app_id: str,
        session_id: str,
        preferences: Dict[str, Any],
    ) -> None:
        """Set user preferences for app and session.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.
            preferences: User preferences dictionary.
        """
        ...

    def get_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
    ) -> Optional[Any]:
        """Get app-specific data by key.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.
            key: Data key.

        Returns:
            Data value, or None if key doesn't exist.
        """
        ...

    def set_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
        value: Any,
    ) -> None:
        """Set app-specific data by key.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.
            key: Data key.
            value: Data value (must be JSON-serializable).
        """
        ...

    def list_app_data(
        self,
        app_id: str,
        session_id: str,
    ) -> List[Dict[str, Any]]:
        """List all app-specific data for session.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.

        Returns:
            List of dictionaries with 'key', 'value', and 'updated_at' fields.
        """
        ...

    def delete_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
    ) -> bool:
        """Delete app-specific data by key.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.
            key: Data key.

        Returns:
            True if data was deleted, False if key didn't exist.
        """
        ...
