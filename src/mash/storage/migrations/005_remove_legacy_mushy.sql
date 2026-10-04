-- Permanently remove generated GIF data from the retired sharing prototype.
-- Thought summaries remain in runtime_event_log; bundled expression assets
-- used by Admin Thoughts are unaffected.
DROP TABLE IF EXISTS mushy_artifact_legacy;
