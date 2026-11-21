-- App and policy replace the user ---------------------------------------------
--
-- Mash has no user. A request carries the app that posted it (app_id), which
-- scopes memory, and the policy it runs under (policy_id), which owns the
-- sandbox and the browser profile. The policy itself is snapshotted into the
-- request's metadata. Clean cutover: user columns are dropped without backfill.

ALTER TABLE runtime_request DROP COLUMN IF EXISTS user_id;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS app_id TEXT;
ALTER TABLE runtime_request ADD COLUMN IF NOT EXISTS policy_id TEXT;

DROP INDEX IF EXISTS idx_memory_turns_user;
ALTER TABLE memory_turns DROP COLUMN IF EXISTS user_id;
ALTER TABLE memory_turns ADD COLUMN IF NOT EXISTS app_id TEXT;

-- search_conversations with scope app: this agent's turns in the app.
CREATE INDEX IF NOT EXISTS idx_memory_turns_app_agent
    ON memory_turns(app_id, agent_id)
    WHERE app_id IS NOT NULL;

-- cortex-build: the app's turns since its last build.
CREATE INDEX IF NOT EXISTS idx_memory_turns_app_created
    ON memory_turns(app_id, created_at)
    WHERE app_id IS NOT NULL AND workflow_id IS NULL;

-- Cortex is one memory per app. A user's memory is not an app's, so the
-- per-user tables are dropped rather than renamed.
DROP TABLE IF EXISTS cortex_entry;
DROP TABLE IF EXISTS cortex_user;

CREATE TABLE IF NOT EXISTS cortex_app (
    app_id         TEXT PRIMARY KEY,
    profile        TEXT NOT NULL DEFAULT '',
    -- created_at of the newest turn the last build covered
    built_through  DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- bumped by every write, from a build or the app API
    version        BIGINT NOT NULL DEFAULT 0,
    updated_at     DOUBLE PRECISION NOT NULL,
    last_build     JSONB
);

CREATE TABLE IF NOT EXISTS cortex_entry (
    entry_id    TEXT PRIMARY KEY,
    app_id      TEXT NOT NULL REFERENCES cortex_app(app_id) ON DELETE CASCADE,
    topic       TEXT NOT NULL,
    content     TEXT NOT NULL,
    -- build | app
    origin      TEXT NOT NULL,
    -- [{"agent_id", "session_id", "trace_id"}]; empty for app entries
    sources     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cortex_entry_app
    ON cortex_entry(app_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_cortex_entry_tsv
    ON cortex_entry
    USING GIN (to_tsvector('simple', topic || ' ' || content));

-- A workflow run acts for an app under a policy; the policy is snapshotted at
-- run start and kept on resume.
ALTER TABLE workflow_runs DROP COLUMN IF EXISTS user_id;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS app_id TEXT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS policy_id TEXT;
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS policy JSONB;
