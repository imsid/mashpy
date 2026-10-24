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
  `src/pilot/catalog/` say which agents exist (the `pilot` guide and its
  `builder` subagent, plus masher's eval agents and the changelog workflow's
  agent), which tools each one registers, and which models back them.
  `GET /api/v1/agent`, `/api/v1/hosts` and `/api/v1/workflow` say what is
  actually deployed; the changelog workflow's id is `pilot-changelog`.
- Every agent runs as a subprocess of the host (`mash agent serve`) inside the
  host container, so its tools, MCP connections and logs live there; the host
  log prefixes each agent's lines with `[agent_id]`. Bash is different: every
  `bash` call runs in the sandbox of the user the request acts for, a
  `mash-sandbox-<key>` container the host starts through the Docker socket on
  first use, in `/home/agent/workspaces/<agent_id>`.
- Every request and every workflow run that reaches an agent carries a
  `user_id`. Pick distinct users per case when isolation matters.
- Every pilot agent is named by the `guide` host or a workflow, so disabling
  one returns `409 AGENT_IN_USE`. Testing a successful disable needs a host
  or agent you register for the test.

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

Run compose from the repo root. `.env` needs `GEMINI_API_KEY`,
`OPENROUTER_API_KEY` and `MASH_HOST_SECRET`; compose refuses to start without
the secret. If `.env` lacks it, export one in the shell for the run
(`openssl rand -hex 32`) instead of editing `.env`.

```bash
docker compose -f docker-compose.pilot.yml up -d --build
```

Mash and pilot code is baked into the image, so a source change needs a
rebuild. The agent processes restart with the host, but sandboxes keep the
image they were created from, so remove them too when the change reaches the
sandbox daemon or the image (their volumes, the users' files, are kept):

```bash
docker compose -f docker-compose.pilot.yml up -d --build pilot
docker rm -f $(docker ps -aq --filter label=mash.sandbox.user)
```

The repo itself is mounted read-only at `/home/agent/mashpy` in the host and
every sandbox, so edits to files the agents read need no restart.
**`restart` does not re-read `.env`**: an environment change needs
`up -d pilot`. Agent processes inherit the host's environment; sandboxes get
only their token, so `.env` never reaches bash. `down -v` wipes the database;
use it only when prior state is in the way, and say so, because it destroys
every earlier run you might still want to inspect.

The port opens before the pool is ready, and on a cold start the agent
processes take a while to import the app, so wait on the API, not the
container:

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
  -H 'Content-Type: application/json' -d '{"message":"...","session_id":"s-1","user_id":"u-1"}'
curl -s http://127.0.0.1:8000/api/v1/agent/<agent>/request/<request_id>/status

# CLI — run it inside the host container; the host's `pilot` binary may be a
# stale install. Piped stdin works, so a REPL session is scriptable. Run it from
# /tmp: the REPL writes its history file to the working directory, which the
# `mash` user cannot write in /opt/mash.
printf '/status\n/exit\n' | docker compose -f docker-compose.pilot.yml exec -T -w /tmp -e HOME=/tmp pilot pilot repl --host guide

# Sandboxes — list them, then inspect a user's files the way bash sees them.
# docker exec copies the container's env (the sandbox token included), so check
# what bash's environment holds through an agent's bash call, not docker exec.
docker compose -f docker-compose.pilot.yml exec -T pilot mash sandbox ls
docker exec -u agent -w /home/agent/workspaces/<agent_id> \
  $(docker ps -q --filter label=mash.sandbox.user=<user_id>) ls -la

# Agent processes — the host image has no ps or kill; read /proc and signal
# from python (e.g. to kill one and watch the host restart it)
docker compose -f docker-compose.pilot.yml exec -T pilot sh -c \
  'for f in /proc/[0-9]*/cmdline; do tr "\0" " " <$f | grep -q -- "--agent <agent_id>" && basename $(dirname $f); done'
docker compose -f docker-compose.pilot.yml exec -T pilot python -c "import os,signal;os.kill(<pid>,signal.SIGKILL)"

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
(`docker compose -f docker-compose.pilot.yml logs pilot --tail 100`), the
`[agent_id]` lines in it for one agent's process, the sandbox's own log
(`docker logs mash-sandbox-<key>`), and where
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
