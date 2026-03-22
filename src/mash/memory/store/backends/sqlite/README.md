# SQLite Store Backend

`src/mash/memory/store/backends/sqlite` contains the built-in `SQLiteStore` backend.

This backend persists turns, signals, preferences, and app-scoped data in SQLite, and provides keyword retrieval via SQLite FTS5.

## Files
- `store.py`: `SQLiteStore` implementation
- `__init__.py`: backend export surface

## Database Objects Created
On initialization, `SQLiteStore._init_schema()` creates the following tables and indexes.

### `turns`
Canonical conversation-turn table.

Columns:
- `turn_id TEXT PRIMARY KEY`
- `session_id TEXT NOT NULL`
- `app_id TEXT NOT NULL DEFAULT 'default'`
- `user_message TEXT NOT NULL`
- `agent_response TEXT NOT NULL`
- `session_total_tokens INTEGER NOT NULL DEFAULT 0`
- `metadata TEXT`
- `created_at REAL NOT NULL`

What it stores:
- One row per saved turn.
- `turn_id` is the `trace_id` passed to `save_turn(...)`.
- `metadata` is stored as JSON text.
- `created_at` is a Unix timestamp in seconds.

Main readers:
- `get_turns()`
- `list_sessions()`
- `get_latest_session()`
- `list_recent_traces()`
- `get_latest_trace()`
- `get_turn_by_ids()`
- `keyword_search()` via join from the FTS table

### `signals`
Numeric signal values attached to turns.

Columns:
- `turn_id TEXT NOT NULL`
- `signal_name TEXT NOT NULL`
- `signal_value REAL NOT NULL`

Constraints:
- `PRIMARY KEY (turn_id, signal_name)`
- `FOREIGN KEY (turn_id) REFERENCES turns(turn_id)`

What it stores:
- Zero or more numeric signals per turn.
- Only numeric/coercible signal values are written by `save_turn()`.
- Non-numeric signal values are ignored rather than serialized.

Main readers:
- `_get_signals_for_turn()`
- indirectly `get_turns()`

### `preferences`
Session-scoped preferences per app.

Columns:
- `app_id TEXT NOT NULL`
- `session_id TEXT NOT NULL`
- `value TEXT NOT NULL`
- `updated_at REAL NOT NULL`

Constraints:
- `PRIMARY KEY (app_id, session_id)`

What it stores:
- One JSON object per app/session pair.
- `set_preferences()` uses upsert semantics.

Main readers/writers:
- `get_preferences()`
- `get_latest_preferences()`
- `set_preferences()`

### `app_data`
Arbitrary app-defined key/value storage per app/session.

Columns:
- `app_id TEXT NOT NULL`
- `session_id TEXT NOT NULL`
- `key TEXT NOT NULL`
- `value TEXT NOT NULL`
- `updated_at REAL NOT NULL`

Constraints:
- `PRIMARY KEY (app_id, session_id, key)`

What it stores:
- One JSON value per app/session/key tuple.
- `set_app_data()` upserts by that composite key.
- Values are JSON-encoded when possible; non-serializable values are stringified first.

Main readers/writers:
- `get_app_data()`
- `set_app_data()`
- `list_app_data()`
- `delete_app_data()`

### `fts_turns`
SQLite FTS5 virtual table used for keyword search.

Definition:
- `turn_id UNINDEXED`
- `session_id UNINDEXED`
- `user_message`
- `agent_response`
- tokenizer: `unicode61`

Important notes:
- Table name is fixed by `SQLiteStore._FTS_TABLE = "fts_turns"`.
- This is a virtual table, not a normal row table.
- It indexes searchable text for `user_message` and `agent_response`.
- `turn_id` and `session_id` are stored as unindexed carry-through fields for joins and filtering.

Usage:
- `keyword_search()` runs `MATCH` queries against `fts_turns`.
- Results are joined back to `turns` to recover `app_id`, preview text, and stable ordering.

Rebuild behavior:
- If `turns` already has rows and `fts_turns` is empty at startup, `_rebuild_turns_fts_index_locked()` repopulates the FTS index from `turns`.

## Indexes Created
- `idx_turns_session ON turns(session_id)`
- `idx_turns_app ON turns(app_id)`
- `idx_signals_name ON signals(signal_name)`
- `idx_app_data_session ON app_data(app_id, session_id)`

Why they exist:
- `idx_turns_session`: session-history reads
- `idx_turns_app`: app-scoped session and trace listing
- `idx_signals_name`: signal-based lookups/inspection
- `idx_app_data_session`: app/session-scoped app-data reads

## Method-To-Table Mapping

`save_turn(...)`
- Inserts one row into `turns`
- Inserts one row into `fts_turns`
- Inserts zero or more rows into `signals`

`get_turns(session_id, ...)`
- Reads from `turns`
- Hydrates signals from `signals`

`list_sessions(app_id)`
- Aggregates from `turns`

`list_recent_traces(app_id, session_id, ...)`
- Reads recent rows from `turns`

`get_turn_by_ids(pairs)`
- Reads matching rows from `turns`

`keyword_search(...)`
- Searches `fts_turns`
- Joins to `turns`

`get_preferences(...)`, `get_latest_preferences(...)`, `set_preferences(...)`
- Operate on `preferences`

`get_app_data(...)`, `set_app_data(...)`, `list_app_data(...)`, `delete_app_data(...)`
- Operate on `app_data`

## Search Constraints
- Keyword search supports only:
  - `user_message`
  - `agent_response`
- `keyword_search()` builds a column-scoped FTS5 query with token-AND semantics.
- Returned scores are rank-derived normalized scores, not raw FTS/BM25 values.
- `semantic_search()` is currently not implemented in this backend.

## Operational Notes
- SQLite connections are opened with `check_same_thread=False`.
- A Python lock wraps DB operations for thread safety.
- File-backed database parent directories are created automatically.
- `:memory:` is supported for ephemeral in-memory stores.
- FTS5 support is required; initialization fails with a runtime error if the SQLite build does not support it.
