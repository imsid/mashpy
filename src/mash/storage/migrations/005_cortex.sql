-- Cortex: per-user memory managed by the host -------------------------------
--
-- One row per user, with the profile injected into agents' context and the
-- watermark of the last build, plus the entries behind the profile. Every
-- write bumps cortex_user.version, so a build applies only if nothing else
-- wrote since it read the user's Cortex.

CREATE TABLE IF NOT EXISTS cortex_user (
    user_id        TEXT PRIMARY KEY,
    profile        TEXT NOT NULL DEFAULT '',
    -- created_at of the newest turn the last build covered
    built_through  DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- bumped by every write, from a build or the app API
    version        BIGINT NOT NULL DEFAULT 0,
    updated_at     DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS cortex_entry (
    entry_id    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES cortex_user(user_id) ON DELETE CASCADE,
    topic       TEXT NOT NULL,
    content     TEXT NOT NULL,
    -- build | app
    origin      TEXT NOT NULL,
    -- [{"agent_id", "session_id", "trace_id"}]; empty for app entries
    sources     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cortex_entry_user
    ON cortex_entry(user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_cortex_entry_tsv
    ON cortex_entry
    USING GIN (to_tsvector('simple', topic || ' ' || content));
