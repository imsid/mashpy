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
- `compaction.py`: compaction. The runtime renders every turn since the last checkpoint as text (each user message, its routes and its reply) and `compact_conversation` saves its summary as a `summary_checkpoint` turn, listing the turns it replaces in `metadata["turn_ids"]`. The prompt asks the summary to keep the agent and session ids of runs the user may return to.
- `failed_turns.py`: failed turns. A request the user started that fails saves its user message as a turn with no reply and `metadata["type"] == "failed_request"`, with the error code and message. Replay puts it in a `<failed_request>` block at the top of the next user message, the error clipped to 300 characters. Compaction reads it the same way; `list_app_turns` (Cortex) leaves it out.
- `routes.py`: routes. When a turn is saved, each `RunSubAgents` item and `RunWorkflow` call the request made becomes an entry in the turn's `metadata["routes"]`: the agent and session, or the workflow and run, with its trace and status, read from the calls' full results. Replay puts them in a `<routes>` block at the top of the user message that follows the turn's reply, where only the model sees them; in the reply itself the model would copy them into new replies.
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
