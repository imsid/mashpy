-- The last cortex-build run applied to a user, with its result. A write step
-- retried after its transaction committed finds its own run id here and
-- returns the stored result instead of failing the version check.
ALTER TABLE cortex_user ADD COLUMN IF NOT EXISTS last_build JSONB;
