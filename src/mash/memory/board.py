"""The board: a conversation's final entries, and its one summary.

A conversation's board is the ``memory_turns`` rows of the companion session
in ``seq`` order. Every agent the companion routes to in the conversation
reads the latest summary and every entry after it, each labelled with its
number and author. When the last entry's ``board_tokens`` reaches the
companion's threshold, the companion writes a summary entry at the next
``seq`` before it routes the next message. Entries before a summary stay
readable by number.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.llm import LLMProvider
from ..core.llm.types import LLMContentBlock, LLMMessage, LLMRequest
from ..logging import bound_session_id, bound_trace_id
from .failed_turns import format_failed_request, is_failed_turn
from .store import BOARD_SUMMARY, WORKFLOW_RESULT, MemoryStore

BOARD_COMPACTION_PROMPT = """You summarize a conversation board for the agents that read it next.
Each entry is numbered and names the agent that wrote it.

Requirements:
- Keep it concise and factual.
- Say which agent did what, with the entry numbers, so a reader can open an
  entry in full.
- Capture key decisions, results, constraints and open questions.
- Include any user preferences or stated goals.
- Keep names, paths, links and ids the user may come back to exactly.
- Output plain text with these sections:
  Summary:
  Decisions:
  Open Questions:
  User Preferences:
"""


def board_label(entry: Dict[str, Any]) -> str:
    """An entry's label: its number and its author."""
    meta = entry.get("metadata") or {}
    seq, author = entry.get("seq"), entry.get("agent_id")
    if meta.get("type") == BOARD_SUMMARY:
        return f"[entry {seq} · {author} · summary of the entries before it]"
    if meta.get("type") == WORKFLOW_RESULT:
        return f"[entry {seq} · {author} · workflow run]"
    if is_failed_turn(entry):
        return f"[entry {seq} · {author} · failed]"
    return f"[entry {seq} · {author}]"


def render_board(entries: List[Dict[str, Any]]) -> str:
    """The board as plain text: each entry's label, message and reply."""
    parts: List[str] = []
    for entry in entries:
        meta = entry.get("metadata") or {}
        lines = [board_label(entry)]
        user_text = str(entry.get("user_message") or "")
        if is_failed_turn(entry):
            lines.append(format_failed_request(user_text, meta))
        elif user_text and meta.get("type") != BOARD_SUMMARY:
            lines.append(f"User: {user_text}")
        reply = str(entry.get("agent_response") or "")
        if reply:
            author = "Summary" if meta.get("type") == BOARD_SUMMARY else str(entry.get("agent_id"))
            lines.append(f"{author}: {reply}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def compact_board(
    store: MemoryStore,
    llm: LLMProvider,
    *,
    session_id: str,
    agent_id: str,
    trace_id: str,
    threshold: int,
    max_tokens: int,
    temperature: float,
    reason: str = "auto",
) -> Optional[Dict[str, Any]]:
    """Write a summary entry when the board has reached ``threshold`` tokens.

    It covers the previous summary and every entry after it, and goes on the
    board at the next ``seq`` with ``agent_id`` as its author. ``reason``
    ``manual`` writes it whatever the size, when the board has entries since
    its last summary. ``trace_id`` is the entry's, so a step that runs again
    stores it once. Returns the summary entry, or None when nothing was written.
    """
    last = await store.last_entry(session_id)
    if last is None or (last.get("metadata") or {}).get("type") == BOARD_SUMMARY:
        return None
    if reason != "manual" and (threshold <= 0 or int(last["board_tokens"]) < threshold):
        return None
    entries = await store.read_board(session_id)
    # The model call runs in the summary's own trace; the provider reads both
    # ids from context.
    with bound_trace_id(trace_id), bound_session_id(session_id):
        response = await llm.send(
            LLMRequest(
                model=llm.model,
                system=BOARD_COMPACTION_PROMPT,
                messages=[
                    LLMMessage(
                        role="user",
                        content=[LLMContentBlock.text(render_board(entries))],
                    )
                ],
                tools=[],
                max_tokens=max_tokens,
                temperature=temperature,
                use_prompt_caching=False,
            )
        )
    summary = response.text.strip()
    if not summary:
        raise RuntimeError("the board summary came back empty")
    first, final = int(entries[0]["seq"]), int(entries[-1]["seq"])
    # The summary's size on the board: what agents read from it.
    tokens = int(getattr(response.usage, "output_tokens", None) or 0)
    await store.append_entry(
        trace_id=trace_id,
        session_id=session_id,
        agent_id=agent_id,
        user_message=f"[summary of entries {first}-{final}]",
        agent_response=summary,
        signals={},
        context_tokens=0,
        tokens=tokens,
        metadata={"type": BOARD_SUMMARY, "reason": reason, "covers": [first, final]},
    )
    return await store.last_entry(session_id)


__all__ = ["BOARD_COMPACTION_PROMPT", "board_label", "compact_board", "render_board"]
