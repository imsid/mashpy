-- The event log is a request's only record ------------------------------------
--
-- A running request keeps its loop state in memory, and each step appends its
-- output as events with dedupe keys. A request recovered after a crash rebuilds
-- the state from its events, so the checkpoint, and the image blobs it pointed
-- at, are no longer written or read.

DROP INDEX IF EXISTS idx_runtime_request_blob_refs;
ALTER TABLE runtime_request DROP COLUMN IF EXISTS blob_refs;
ALTER TABLE runtime_request DROP COLUMN IF EXISTS checkpoint;
DROP TABLE IF EXISTS runtime_blob;
