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
- New backends (e.g. Postgres, MongoDB) should implement the `MemoryStore` protocol and preserve return shapes.

## Behavioral Invariants
- Preserve `MemoryStore` method signatures and return payload shapes across backends.
- Memory search retrieval must access storage through `MemoryStore.keyword_search()` and `MemoryStore.semantic_search()`.
- Keep search result contracts stable (`turn_id`, `session_id`, `score`, `preview` for store-level search hits).
- `get_turns()` and `get_turn_by_ids()` must require `app_id` scoping so cross-agent reads cannot mix sessions.

## Built-In Backend Notes
- SQLite operations must remain guarded by the store lock for thread safety.
- `save_turn()` uses `trace_id` as `turn_id`.
- `get_turns()` returns chronological order.
- Signal persistence is JSON-compatible across backends.
- `semantic_search()` is currently a stub (`NotImplementedError`) in the built-in backends.
