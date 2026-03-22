# Memory Store

`src/mash/memory/store` defines the backend-agnostic storage contract used by runtime memory, session persistence, preferences, app data, and memory-search retrieval.

## What This Package Exposes
- `MemoryStore`: protocol contract that all storage backends must implement.
- `SQLiteStore`: the currently supported concrete backend, exported as the default built-in implementation.

## Supported Backends
- `SQLiteStore` in `src/mash/memory/store/backends/sqlite/store.py`

Backend expectations:
- Backends must preserve the `MemoryStore` method signatures.
- Backends must preserve return shapes so callers and agents can switch backends without changing logic.
- Search methods must return store-level hit dictionaries with:
  - `turn_id`
  - `session_id`
  - `score`
  - `preview`

Current backend status:
- Keyword search is implemented in `SQLiteStore` via SQLite FTS5.
- Semantic search is part of the protocol, but `SQLiteStore.semantic_search()` currently raises `NotImplementedError`.

## Protocol Methods

### Turn Persistence

`save_turn(...) -> str`
- Persists one conversation turn.
- Inputs:
  - `trace_id`: used as the stored `turn_id`
  - `session_id`
  - `app_id`
  - `user_message`
  - `agent_response`
  - `signals`
  - `session_total_tokens`
  - `metadata`
- Returns:
  - the persisted `turn_id`

Notes:
- `signals` are backend-defined numeric signals associated with the turn.
- In the SQLite backend, non-numeric signal values are skipped.

`get_turns(session_id, limit=None) -> list[dict]`
- Returns conversation turns for one session.
- Expected shape per item:
  - `turn_id`
  - `user_message`
  - `agent_response`
  - `session_total_tokens`
  - `signals`
  - `metadata`
  - `created_at`

Behavior:
- The contract is chronological turn order.
- In SQLite, `limit=None` returns all turns ascending by time; limited reads still return ascending order after an internal reverse query.

`get_turn_by_ids(pairs) -> list[dict] | None`
- Bulk lookup by exact `{session_id, turn_id}` pairs.
- Expected shape per returned item:
  - `turn_id`
  - `session_id`
  - `user_message`
  - `agent_response`

Behavior:
- Missing pairs are omitted.
- Returns `None` when no requested pairs are valid or found.

### Session Listing And Trace Lookup

`list_sessions(app_id) -> list[dict]`
- Lists persisted sessions for one app.
- Expected shape per item:
  - `session_id`
  - `turn_count`
  - `last_activity_at`
  - `session_total_tokens`

`get_latest_session(app_id) -> dict | None`
- Convenience wrapper returning the first item from `list_sessions(app_id)`.

`list_recent_traces(app_id, session_id, limit=5) -> list[dict]`
- Returns recent turns/traces for a specific session within an app.
- Expected shape per item:
  - `trace_id`
  - `session_id`
  - `user_message`
  - `agent_response`
  - `metadata`
  - `created_at`

`get_latest_trace(app_id, session_id) -> dict | None`
- Convenience wrapper over `list_recent_traces(..., limit=1)`.

### Search Methods

`keyword_search(column, query_term, limit, session_id=None, app_id=None) -> list[dict]`
- Searches one text column by keyword.
- Inputs:
  - `column`: currently expected to be `user_message` or `agent_response`
  - `query_term`
  - `limit`
  - optional `session_id`
  - optional `app_id`
- Required hit shape:
  - `turn_id`
  - `session_id`
  - `score`
  - `preview`

SQLite behavior:
- Uses FTS5 with token-AND semantics.
- Returns normalized scores in `(0, 1]` based on result rank, not raw BM25.

`semantic_search(column, query_term, query_embedding, limit, session_id=None, app_id=None) -> list[dict]`
- Protocol hook for semantic/vector retrieval over one text column.
- Required hit shape matches `keyword_search`.

SQLite behavior:
- Not implemented yet.

### Preferences

`get_preferences(app_id, session_id) -> dict | None`
- Reads app-scoped preferences for one session.

`get_latest_preferences(app_id) -> dict | None`
- Reads the most recently updated preferences row for one app.

`set_preferences(app_id, session_id, preferences) -> None`
- Upserts preferences for one app/session pair.

Expected payload shape:
- Arbitrary JSON object.

### App Data

`get_app_data(app_id, session_id, key) -> Any | None`
- Reads one app-defined data value.

`set_app_data(app_id, session_id, key, value) -> None`
- Upserts one app-defined data value.

`list_app_data(app_id, session_id) -> list[dict]`
- Lists all app-defined key/value entries for a session.
- Expected shape per item:
  - `key`
  - `value`
  - `updated_at`

`delete_app_data(app_id, session_id, key) -> bool`
- Deletes one app-defined value.
- Returns `True` if a row was deleted.

## Return Shape Notes
- Store methods return plain dictionaries/lists, not typed model objects.
- `metadata`, `preferences`, and `app_data.value` are intended to be JSON-compatible.
- Callers should not assume backend-specific extra keys beyond the documented shapes.

## SQLite-Specific Notes
- Thread safety is enforced with a store-level lock around DB operations.
- `trace_id` is persisted as the canonical `turn_id`.
- FTS index rows are rebuilt automatically if the main `turns` table has data but the FTS table is empty.
- Database files are created on demand; parent directories are also created automatically.
