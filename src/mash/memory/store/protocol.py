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
        agent_id: str,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: Optional[int] = None,
        after_log_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return structured log records for one app/session/trace scope."""
        ...

    async def get_latest_log_trace(
        self,
        agent_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the latest trace summary from persisted logs."""
        ...

    async def list_recent_log_traces(
        self,
        agent_id: str,
        session_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """List recent trace summaries from persisted logs."""
        ...

    async def save_turn(
        self,
        trace_id: str,
        session_id: str,
        agent_id: str,
        user_message: str,
        agent_response: str,
        signals: Dict[str, Any],
        context_tokens: int,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        workflow_id: Optional[str] = None,
        workflow_run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        replayable: bool = True,
        app_id: Optional[str] = None,
    ) -> str:
        """Save a conversation turn with signals.

        ``context_tokens`` is the size of the turn's context: the input and
        output tokens of its last model call. Saving a ``trace_id`` that is
        already stored is a no-op, so a replayed persist step never fails on
        the existing row.

        Workflow task turns carry their ``workflow_id`` / ``workflow_run_id`` /
        ``task_id`` and are marked ``replayable=False`` so they are excluded from
        the conversation history replayed into the model.
        """
        ...

    async def get_turns(
        self,
        session_id: str,
        agent_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get conversation turns for a session."""
        ...

    async def get_turn(
        self,
        trace_id: str,
        agent_id: str,
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """One turn by its trace id (the primary key), or None.

        Only a turn of ``agent_id``, and of ``session_id`` when given. The row has
        ``trace_id``, ``session_id``, ``user_message``, ``agent_response``,
        ``metadata`` and ``created_at``.
        """
        ...

    async def get_replay_turns(
        self,
        session_id: str,
        agent_id: str,
        *,
        trace_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """The turns a request replays into its context, oldest first.

        The latest replayable ``summary_checkpoint`` turn, if any, then every
        replayable turn after it. With ``trace_ids``, those turns of the session
        instead: the ones a request recorded at its context load, so a recovered
        request replays the same turns. Turns carry no signals.
        """
        ...

    async def list_app_turns(
        self,
        app_id: str,
        *,
        after: float = 0.0,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """The app's turns after ``after``, oldest first, across agents and sessions.

        Leaves out workflow and subagent turns, and turns of failed requests:
        these are the turns where the app spoke to the pool and got a reply. Each row has ``trace_id``, ``session_id``,
        ``agent_id``, ``user_message``, ``agent_response`` and ``created_at``.
        """
        ...

    async def count_app_turns(self, app_id: str, *, after: float = 0.0) -> int:
        """How many turns ``list_app_turns`` would return without a limit."""
        ...

    async def list_workflow_turns(
        self,
        agent_id: str,
        *,
        workflow_id: str,
        workflow_run_id: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        sort_desc: bool = True,
    ) -> List[Dict[str, Any]]:
        """List workflow task turns for one application and workflow.

        Filters on the ``workflow_id`` column (optionally scoped to a single
        ``workflow_run_id``); each turn carries its ``workflow_run_id`` and
        ``task_id``.
        """
        ...

    async def get_session_signals(
        self,
        session_id: str,
        agent_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get chronological per-turn signal payloads for a session."""
        ...

    async def list_sessions(
        self,
        agent_id: str,
        *,
        limit: int,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """The application's ``limit`` most recently active sessions, newest first.

        With ``app_id``, only sessions with that app's turns. Each row carries
        ``first_message``, the start of the session's first user message.
        """
        ...

    async def get_latest_session(
        self,
        agent_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent persisted session for one application."""
        ...

    async def get_latest_trace(
        self,
        agent_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent trace for a session in one application."""
        ...

    async def list_recent_traces(
        self,
        agent_id: str,
        session_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """List recent traces for a session in one application."""
        ...

    async def get_turn_by_ids(
        self,
        pairs: List[Dict[str, str]],
        agent_id: str,
        app_id: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Get turns by exact session/turn identifier pairs in one lookup.

        With ``app_id``, only that app's turns are returned.
        """
        ...

    async def keyword_search(
        self,
        column: SearchColumn,
        query_term: str,
        limit: int,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
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
        agent_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search turns semantically in a single column."""
        ...
