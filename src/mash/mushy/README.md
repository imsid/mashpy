# Mushy / Admin Thoughts

Mushy accompanies the agent's original exposed thought summaries in Mash Admin.
Open **Activity → Thoughts**, or **View thoughts** on an agent's card or in the
Logs trace sidebar. The sidebar link shows only the selected trace, even outside
the latest 50; **Show latest thoughts** returns to the feed. The Agent
dropdown matches Logs, including **All agents**. The feed shows the latest 50
traces containing at least one nonempty summary, ordered by their latest thought.
Every summary within a trace is shown in chronological order, in full. There is
no per-summary selector, playback control, or automatic loading of older traces.

Underlined phrases are exact substrings, selected by local presentation rules
in `web-admin/src/lib/thoughts.js`. Selecting one changes Mushy's bundled
expression and moves it beside the passage. These accents are a presentation
heuristic, not an inferred personality or a rewritten summary. Unmatched text
stays visible. Code and links retain their Markdown structure. Reduced-motion
preferences use static expression frames and disable movement.

## Automatic capture

The runtime always enables exposed thought summaries on its planning providers,
including request-scoped providers. No agent configuration or `Mushy` object is
needed. Gemini currently supports capture; other providers leave their settings
unchanged. `LLMResponse.thought_summary` is required; use `""` when absent.
Only exposed textual summaries are recorded in `runtime.llm.think.completed`.
Answers, tool results, signatures, and encrypted reasoning never substitute for
missing summaries.

There is no separate interpreter/provider to configure or close, and no model
call when opening or refreshing Thoughts. Existing recorded summaries remain
readable regardless of the current provider.

## Authenticated API

`GET /api/v1/telemetry/thoughts?agent_id=pilot` returns
`data.traces`, `data.agent_id`, `data.trace_id`, and `data.limit` (fixed at 50).
Omit `agent_id` for the shared feed. Add `trace_id` to load a specific trace;
this filter applies before the limit, so older traces are accessible. The runtime
store filters for nonempty string summaries
before limiting traces. All summaries for selected traces are returned without
truncation. Each trace carries agent/session/trace IDs and a latest-thought time;
each summary carries its event ID, request ID, timestamp, and original text.

`GET /api/v1/telemetry/thoughts/expressions/{asset}` serves catalog GIF/PNG
assets. Both endpoints use the normal API authentication, including Admin's
same-origin cookie. Each trace's **View logs** link opens the exact trace drawer.

## Retired GIF prototype

The former generation POST, public `/mushy/{id}` viewer and GIF routes, share /
download controls, GIF renderer, interpreter, and artifact store are removed.
The prototype tables and generated GIF data were removed directly from Pilot,
the only database that used them. Migration `003_admin_thoughts.sql` adds only
the query index on `runtime_event_log`; no new thought storage is introduced.

The expression catalog and bundled GIFs remain under `mash/mushy/`. Each has a
static PNG counterpart for reduced motion. There is no runtime Pillow dependency.
