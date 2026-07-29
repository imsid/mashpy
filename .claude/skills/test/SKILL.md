# SKILL: test

Test local mash changes against a real Mash host — the pilot deployment in
Docker — by driving it the way a user would: the HTTP API, the CLI, and the
runtime tables underneath.

Use this after implementing a change, before opening the PR. The unit suite
says the code does what you wrote; this says the deployment does what the user
asked for.

---

## Phase 1 — Ground yourself

Read the change before designing anything around it.

- If the work is on a feature branch, read the diff (`git diff main...HEAD`)
  and any design doc it implements. If the user named the behavior to test
  instead, read the code that implements it.
- Decide what is *material*: which behaviors a user could observe going wrong.
  Refactors with no observable surface do not need a live test.
- Learn how pilot exposes that surface. `src/pilot/spec.py` and
  `src/pilot/catalog/` say which agents exist, which tools each one registers,
  and which models back them. `GET /api/v1/agent` and `/api/v1/hosts` say what
  is actually deployed.

Model choice is part of grounding: an agent whose model cannot drive the path
you want to test will fail for reasons that have nothing to do with your
change. Check which provider backs each agent, and prefer one you have seen
complete the shape you need (a multi-turn tool loop, an interaction, a subagent
invocation).

---

## Phase 2 — Write the plan and get it approved

Keep it to a handful of cases — the material behaviors, plus the one or two
edge cases most likely to be wrong. A long plan tests the framework, not the
change.

Present, per case:

- **What it exercises** — the behavior, in one line.
- **Verification method** — how you will observe it. Use the surface that
  actually proves the claim, and more than one when they can disagree:
  - **API** — targeted `curl` against `/api/v1/...`, including status codes and
    error bodies, and the SSE event stream where termination is the point.
  - **CLI** — a REPL session for the flows a user reaches by typing.
  - **Tables** — `runtime_event_log` for the request lifecycle, plus
    `memory_turns`, `workflow_runs` / `workflow_steps`, `runtime_feedback`, or
    the `dbos` schema when the case reaches them.
- **Success criteria** — the concrete observation that decides pass or fail,
  written before you run it. "The request completes" is not a criterion; "the
  last lifecycle event is `runtime.request.completed` and exactly one
  `runtime.turn.persisted` row exists" is.

Wait for approval before running anything.

---

## Phase 3 — Bring the stack up

```bash
docker compose -f docker-compose.pilot.yml up -d
```

The repo is bind-mounted and installed editable, so source edits need only
`docker compose -f docker-compose.pilot.yml restart pilot`. **`restart` does
not re-read `.env`** — an environment change needs `up -d pilot`, and it is
worth confirming with `exec -T pilot printenv <VAR>`. `down -v` wipes the
database; use it only when prior state is in the way, and say so, because it
destroys every earlier run you might still want to inspect.

The port opens before the pool is ready, so wait on the API, not the container:

```bash
until [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/v1/agent)" = 200 ]; do sleep 2; done
```

---

## Phase 4 — Run the cases

Run them one at a time and record the actual output of each — you are
collecting evidence, not just watching for green.

The three surfaces:

```bash
# API — every response is wrapped in {"data": ...}; errors are {"error":{"code",...}}
curl -s -X POST http://127.0.0.1:8000/api/v1/agent/<agent>/request \
  -H 'Content-Type: application/json' -d '{"message":"...","session_id":"s-1"}'
curl -s http://127.0.0.1:8000/api/v1/agent/<agent>/request/<request_id>/status

# CLI — run it inside the container; the host's `pilot` binary may be a stale
# install. Piped stdin works, so a REPL session is scriptable.
printf '/status\n/exit\n' | docker compose -f docker-compose.pilot.yml exec -T pilot pilot repl --host guide

# Tables
docker compose -f docker-compose.pilot.yml exec -T db psql -U mash -d mash_pilot -A -F'|' -c \
  "select seq,event_type,left(payload::text,160) from runtime_event_log where request_id='<id>' order by seq"
```

Requests are asynchronous: poll `status` until it leaves `pending`, then read
the events. `status` reports DBOS truth; the event log reports what clients
see. **Check both when a case touches request lifecycle — a divergence between
them is itself a finding.**

Write the shared helpers (submit, poll, psql) into your scratchpad and `source`
them per Bash call, since shell state does not persist between calls.

---

## Phase 5 — Diagnose failures by reading

A failure is a lead, not a verdict. Before reporting anything, find the
mechanism in source: the mash code path, the server log
(`docker compose -f docker-compose.pilot.yml logs pilot --tail 100`), and where
relevant the installed dependency itself, which is importable in the container:

```bash
docker compose -f docker-compose.pilot.yml exec -T pilot python -c \
  "import inspect; from dbos import DBOS; print(inspect.getsource(DBOS.resume_workflow_async))"
```

Separate three things, and never let the first pass as the second:

- **Environment noise** — a flaky or unsuitable model, a missing key, a stale
  container. Re-run on a surface that avoids it.
- **A defect in the change under test** — cite file and line.
- **A pre-existing defect you happened to surface** — check whether the branch
  touched that code (`git log -- <path>`) before blaming the change.

When a fix follows, prove the test has teeth: confirm it fails against the
pre-fix code and passes after.

---

## Phase 6 — Report

Objective and evidence-first. For each case: what was verified, on which
surface, and the observation that settled it — not a restatement of the plan.

If everything passed, say so plainly and note what the tests do *not* cover.
For each finding, give the evidence, the file and line where the mechanism
lives, and what correct behavior would be; recommend a fix direction, and flag
when it is a design decision rather than a patch.

Finally, restore anything you changed to make testing possible — `.env`
overrides, temporary registrations, prompt tweaks — and say so explicitly.
Leave the stack running unless asked otherwise.
