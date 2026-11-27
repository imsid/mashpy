# AGENTS Guide for `src/mash/memory/store`

## Scope
Memory store protocol and backend implementations.

## Public API Boundary
- Callers should import store types from `mash.memory.store`.
- `MemoryStore` is the backend-agnostic protocol contract.
- Backend implementation details should remain internal to this package.

## Backend Organization
- Backend code belongs under `src/mash/memory/store/backends/<backend>/`.
- Keep backend internals isolated from callers; expose only stable store types from `store/__init__.py`.
- The only built-in backend is `PostgresStore`. Custom backends must implement the `MemoryStore` protocol and preserve return shapes.

## Behavioral Invariants
- Preserve `MemoryStore` method signatures and return payload shapes across backends.
- Memory search retrieval must access storage through `MemoryStore.keyword_search()` and `MemoryStore.semantic_search()`.
- Keep search result contracts stable (`trace_id`, `session_id`, `score`, `preview` for store-level search hits).
- `get_turns()` and `get_turn_by_ids()` must require `agent_id` scoping so cross-agent reads cannot mix sessions.
- The board reads (`read_entries`, `search_entries`, `last_entry`, `read_board`) take one session and return every agent's entries in it; none takes an agent filter.

## Built-In Backend Notes
- `append_entry()` stores the `trace_id` argument as the row PK (`trace_id` column) and takes the session's next `seq`; `(session_id, seq)` stays unique.
- `board_tokens` on an entry is the board's size since its last `summary` entry, that entry included.
- `get_turns()` returns chronological order.
- Signal persistence is JSON-compatible across backends.
- `semantic_search()` is currently a stub (`NotImplementedError`) in the built-in backend.
