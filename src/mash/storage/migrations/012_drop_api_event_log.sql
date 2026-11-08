-- The API request log is gone ----------------------------------------------------
--
-- The host used to write an api_event_log row for every /api/v1 call, with up
-- to 8 KB of its request body, and never pruned the table. Nothing reads or
-- writes it now.

DROP TABLE IF EXISTS api_event_log;
