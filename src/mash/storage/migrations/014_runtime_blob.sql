-- Checkpoint images out of line -----------------------------------------------
--
-- A request's checkpoint is rewritten after every step and tool call, and it
-- carries the images the model sees (browser screenshots, base64). Each image
-- is now stored once in runtime_blob, keyed by the SHA-256 of its data, and
-- the checkpoint keeps {"media_type", "mash_blob": <sha256>} in its place.
--
-- blob_refs lists the blobs a request's checkpoint references, so retention
-- deletes a blob only when no remaining request needs it. last_used_at is
-- bumped by every checkpoint save that references the blob, so a blob a save
-- is about to reference is never old enough to delete.

CREATE TABLE IF NOT EXISTS runtime_blob (
    sha256       TEXT PRIMARY KEY,
    media_type   TEXT,
    data         TEXT NOT NULL,
    last_used_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_blob_last_used
    ON runtime_blob(last_used_at);

ALTER TABLE runtime_request
    ADD COLUMN IF NOT EXISTS blob_refs TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_runtime_request_blob_refs
    ON runtime_request USING GIN (blob_refs);
