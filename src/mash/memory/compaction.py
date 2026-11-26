"""Conversation compaction: a summary checkpoint replaces a session's history.

The runtime renders the history a request would replay (every turn since the
last checkpoint, tool calls and results included) and passes it here
as text. The summary is saved as a ``summary_checkpoint`` turn; replay starts
from the latest one.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Tuple

from ..core.llm import LLMProvider
from ..core.llm.types import LLMContentBlock, LLMMessage, LLMRequest
from ..logging import bound_session_id, bound_trace_id
from .store import MemoryStore

COMPACTION_SYSTEM_PROMPT = """You are a conversation compactor.
Summarize the conversation so far for future context.

Requirements:
- Keep it concise and factual.
- Capture key decisions, constraints, and open questions.
- Include any user preferences or stated goals.
- State what tool calls and their results established, not their raw output.
- Keep, exactly, the agent id and session id of each agent session, and the
  workflow id and run id of each workflow run, that the user may come back to.
- Output plain text with these sections:
  Summary:
  Decisions:
  Open Questions:
  User Preferences:
  Agent Sessions:
"""


async def compact_conversation(
    store: MemoryStore,
    llm: LLMProvider,
    *,
    agent_id: str,
    session_id: str,
    conversation_text: str,
    turn_ids: List[str],
    max_tokens: int,
    temperature: float,
    reason: str,
) -> Tuple[str, str]:
    """Summarize ``conversation_text`` and save it as a summary checkpoint turn.

    ``turn_ids`` are the trace ids of the turns it replaces. Returns the summary
    and the checkpoint's trace id, or empty strings when there is nothing to
    summarize.
    """
    if not conversation_text.strip():
        return "", ""

    trace_id = str(uuid.uuid4())
    # The summary pass is its own trace in the session; the provider reads
    # both from context.
    with bound_trace_id(trace_id), bound_session_id(session_id):
        response = await llm.send(
            LLMRequest(
                model=llm.model,
                system=COMPACTION_SYSTEM_PROMPT,
                messages=[
                    LLMMessage(
                        role="user",
                        content=[LLMContentBlock.text(conversation_text)],
                    )
                ],
                tools=[],
                max_tokens=max_tokens,
                temperature=temperature,
                use_prompt_caching=False,
            )
        )
    summary_text = response.text.strip()

    metadata: Dict[str, Any] = {
        "type": "summary_checkpoint",
        "reason": reason,
        "turn_ids": list(turn_ids),
        "agent_id": agent_id,
        "token_usage": {"input": 0, "output": 0},
    }
    saved_trace_id = await store.save_turn(
        trace_id=trace_id,
        session_id=session_id,
        agent_id=agent_id,
        user_message=f"[summary checkpoint:{reason}]",
        agent_response=summary_text,
        signals={},
        context_tokens=0,
        metadata=metadata,
    )

    return summary_text, saved_trace_id
