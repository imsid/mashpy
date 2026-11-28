# Memory

`src/mash/memory` provides persistent memory storage, retrieval, and compaction support for Mash agents.

## What This Package Does
- Defines the memory store protocol used by agents.
- Provides a Postgres-backed store implementation.
- Implements retrieval/search services over stored memory.
- Provides compaction and signaling helpers for longer-lived memory flows.

## Main Components
- `store/`: storage protocol and concrete Postgres backend.
- `search/`: retrieval service, parser/rerank logic, and typed search results.
- `board.py`: the board. A conversation's board is the companion session's turns in `seq` order. `render_board` writes it as text, each entry labelled `[entry N · author]`. `compact_board` writes the board's one summary: when the last entry's `board_tokens` reaches the threshold (or on `reason="manual"`), it summarizes the previous summary and every entry after it and appends the result at the next `seq` with `metadata["type"] == "summary"` and the range it covers. Its `tokens` are the summary call's output tokens, so `board_tokens` starts again from there.
- `compaction.py`: compaction of an agent's own session. The runtime renders every turn since the last checkpoint as text (each user message and its reply) and `compact_conversation` saves its summary as a `summary_checkpoint` turn, listing the turns it replaces in `metadata["turn_ids"]`.
- `failed_turns.py`: failed turns. A request the user started that fails (the companion's, or the one it routed) saves its user message as a turn with no reply and `metadata["type"] == "failed_request"`, with the error code and message. Replay puts it in a `<failed_request>` block at the top of the next user message, the error clipped to 300 characters. Compaction reads it the same way; `list_app_turns` (Cortex) leaves it out.
- `signals.py`: memory-related signaling hooks plus typed signal definitions.

## Public Exports
- `MemorySearchService`
- `MemoryStore`
- `RetrievalConfig`
- `SearchResult`

## Role In The System
- Memory is shared infrastructure for hosted agents and built-in specialists.
- Persistence concerns belong in `store/`, while retrieval concerns belong in `search/`.
- Signal values are persisted per turn by the active memory-store backend.
- Signal definitions are runtime-owned metadata returned alongside per-turn signal reads; they are not persisted as store rows.
