-- Context tokens --------------------------------------------------------------
--
-- A turn records the size of its context: the input and output tokens of its
-- last model call. A session compacts before a request once its latest turn's
-- context reaches the agent's threshold. This replaces session_total_tokens, a
-- running sum of every model call's tokens, which counted the history once per
-- call. Existing turns start at 0.
--
-- memory_session keeps the latest turn's context_tokens.

ALTER TABLE memory_turns RENAME COLUMN session_total_tokens TO context_tokens;
UPDATE memory_turns SET context_tokens = 0 WHERE context_tokens <> 0;

ALTER TABLE memory_session RENAME COLUMN session_total_tokens TO context_tokens;
UPDATE memory_session SET context_tokens = 0 WHERE context_tokens <> 0;
