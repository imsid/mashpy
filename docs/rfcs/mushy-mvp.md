# Mushy: original thoughts in Admin

Status: Implemented design, replacing the standalone GIF-sharing prototype.

## Experience

- Activity contains Logs and Thoughts. Agents link to both with their filter preset.
- Thoughts uses the same Agent dropdown as Logs, including All agents.
- A continuous feed shows the latest 50 traces with nonempty exposed summaries.
  Newest thought-bearing traces come first; summaries within each trace are chronological.
- Preserve original wording and Markdown. Highlight exact personality-bearing
  phrases with local rules; selecting one moves Mushy beside it and selects an expression.
- No summary dropdown, reading playback, rewritten dialogue, or public share link.
- Each trace links directly to its Admin Logs drawer. Reduced motion uses static art.

## Runtime and API

`AgentSpec.build_mushy()` returns `Mushy()` to enable provider summary capture;
`None` preserves existing behavior. Only Gemini currently supports enablement.
The normalized `LLMResponse.thought_summary` passes through the existing
`runtime.llm.think.completed` event. Nothing fabricates missing summaries.

The authenticated `GET /api/v1/telemetry/thoughts` reads a single SQL snapshot
from the shared runtime store. Optional `agent_id` scopes both trace selection
and returned summaries. Apply the nonempty-string condition before the 50-trace
limit; return every qualifying summary for selected traces. No inference, new
summary table, generated artifact, background job, or polling is required.
Expression assets are also served under the authenticated telemetry API.

## Retirement and verification

Remove the previous interpreter, rendering, storage, generation and public GET
surfaces. Keep capture, the expression catalog and existing animation clips.
Remove prototype tables and generated GIF data directly from Pilot, their only
installation. Consolidate the unshipped migrations into `003_admin_thoughts.sql`,
which adds only the query index on the existing runtime event table.

Verify full-text preservation, Markdown safety, exact phrase accents, agent
isolation, 50 qualifying traces despite newer blank events, chronological
summaries, authenticated access, retired route 404s, and a fresh schema without prototype tables.
Run runtime request/stream, tool, subagent, history and interaction regressions,
then build and exercise the packaged Admin UI against real Pilot traces.
