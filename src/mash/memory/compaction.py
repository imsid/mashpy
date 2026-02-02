"""Conversation compaction utilities."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Tuple

from ..core.llm import LLMProvider
from .store import MemoryStore

COMPACTION_SYSTEM_PROMPT = """You are a conversation compactor.
Summarize the conversation so far for future context.

Requirements:
- Keep it concise and factual.
- Capture key decisions, constraints, and open questions.
- Include any user preferences or stated goals.
- Do NOT include tool outputs or raw logs.
- Output plain text with these sections:
  Summary:
  Decisions:
  Open Questions:
  User Preferences:
"""


def compact_conversation(
    store: MemoryStore,
    llm: LLMProvider,
    app_id: str,
    session_id: str,
    model: str,
    max_tokens: int,
    temperature: float,
    turn_limit: int,
    reason: str,
    session_total_tokens_reset: int,
) -> Tuple[str, str]:
    """Summarize conversation history and save a summary checkpoint turn."""
    if turn_limit <= 0:
        raise ValueError("turn_limit must be > 0")

    turns = store.get_turns(session_id=session_id, limit=None)
    if not turns:
        return "", ""

    summary_index = None
    for idx in range(len(turns) - 1, -1, -1):
        meta = turns[idx].get("metadata") or {}
        if meta.get("type") == "summary_checkpoint":
            summary_index = idx
            break

    if summary_index is not None:
        base_turns = turns[summary_index:]
        if len(base_turns) > turn_limit:
            if turn_limit == 1:
                base_turns = [base_turns[0]]
            else:
                base_turns = [base_turns[0]] + base_turns[-(turn_limit - 1) :]
    else:
        base_turns = turns[-turn_limit:]

    lines: List[str] = []
    turn_ids: List[str] = []
    for turn in base_turns:
        turn_ids.append(turn.get("turn_id", ""))
        user_message = turn.get("user_message") or ""
        agent_response = turn.get("agent_response") or ""
        if user_message:
            lines.append(f"User: {user_message}")
        if agent_response:
            lines.append(f"Assistant: {agent_response}")

    if not lines:
        return "", ""

    conversation_text = "\n".join(lines)
    response = llm.create_message(
        model=model,
        system=COMPACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": conversation_text}],
        tools=[],
        max_tokens=max_tokens,
        temperature=temperature,
        betas=None,
        use_prompt_caching=False,
    )
    summary_text, _, _ = llm.parse_response(response)
    summary_text = summary_text.strip()

    metadata: Dict[str, Any] = {
        "type": "summary_checkpoint",
        "reason": reason,
        "turn_limit": turn_limit,
        "turn_ids": turn_ids,
        "app_id": app_id,
        "token_usage": {"input": 0, "output": 0},
    }
    trace_id = str(uuid.uuid4())
    turn_id = store.save_turn(
        trace_id=trace_id,
        session_id=session_id,
        user_message=f"[summary checkpoint:{reason}]",
        agent_response=summary_text,
        signals={},
        session_total_tokens=session_total_tokens_reset,
        metadata=metadata,
    )

    return summary_text, turn_id
