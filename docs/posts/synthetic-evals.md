---
title: Synthetic Evals
description: Generate a dataset and rubric from a host's declared capabilities, run experiments that snapshot the live host, and compare quality and cost at read time.
date: 2026-07-05
author: imsid
tags:
  - evals
---

# Synthetic Evals

Before an agent sees any real traffic, there are no traces to learn from. A developer who has just defined a host composition has declared what the agent is supposed to do (its capabilities, its tools, its subagents) but has no empirical signal on whether it actually does those things well. This is the cold-start problem: you want to evaluate your agent before rolling it out, and you have nothing to evaluate against.

Synthetic evals address this directly. Given a host composition and some developer guidance, the system generates a representative dataset of test cases and a scoring rubric derived from the host's declared capabilities. The developer can then run that dataset through the host at any point and get scored results, before the first user ever sends a message.

Synthetic evals are also useful beyond cold start. As a host evolves, there will always be scenarios it has not yet encountered in production. A synthetic dataset can be deliberately seeded to cover edge cases, capability boundaries, and multi-agent paths that organic traffic may take weeks to surface.

This is not a substitute for evaluating against live traces, which carry their own value: real distribution, real user intent, real failure modes. Synthetic and live evals are complementary; this post covers the synthetic side.

The system lives entirely inside Mash. Generation runs as a masher workflow; scoring runs as a durable workflow that uses masher only as an LLM judge. Evals persist to a dedicated Postgres store that ships with Mash. No external eval infrastructure is required.

---

## Concepts

### Host

A host is a named composition of agents: one primary and zero or more subagents. It is the unit of deployment and the unit of evaluation. When a user request arrives, the primary agent handles it and can delegate to subagents via the `InvokeSubagent` tool.

```
Host: travel-assistant
  primary:   TravelConcierge
  subagents: [FlightSearch, HotelSearch]
```

### AgentSpec

An AgentSpec is the implementation of one agent: its system prompt, tools, skills, and LLM model. AgentSpecs evolve: developers change system prompts, add or remove tools, swap models. The host composition can evolve too: a subagent gets added, another gets retired. Each experiment records the state of both at the moment it ran.

### Dataset

A dataset is a set of synthetic test cases generated for a host. Its size is an explicit input to generation: the developer asks for a specific number of rows (default 20, max 100) and gets exactly that many. Each row describes one scenario: what the user input is, which capability it exercises, which agents should be involved, and what good behavior looks like. The dataset is not a list of expected outputs; it is a structured description of intent that the scoring rubric operates against.

### Scoring Rubric

A rubric is a set of weighted criteria derived from the host's declared capabilities and the developer's guidance. Each criterion has a name, a description of what it measures, a weight, and a specific scoring prompt for the LLM judge. The rubric is the stable reference for what "good" means for this host: every experiment on an eval is scored against the same rubric.

### Eval

An eval is the test definition: a dataset and a scoring rubric, bundled under one ID. It records which host it was generated for, but it carries no snapshot of that host's state; the eval defines *what to measure*, not *what is being measured*.

An eval is created by running the `gen-synthetic-evals` workflow. After generation the developer can adjust it, rebalancing rubric weights and tuning criteria, up until the first experiment is created. From that point the eval is locked: experiments are only comparable if they ran the exact same dataset against the exact same rubric, so once results exist against an eval, the eval can never change. To measure something different, generate a new eval.

### Experiment

An experiment is one execution of an eval against the host as it exists right now. Creating and running an experiment are one action: the system snapshots the live host composition and the AgentSpec of every agent in it, then runs every dataset input through the host and scores each output with an LLM judge. Rows run in parallel, each under its own host session, and each row also captures operational metrics (tokens, steps, tool calls, latency) folded from that session's runtime events.

An experiment runs exactly once. Its snapshot is not configuration; it is a record of what was actually evaluated. There is no baseline stored anywhere: an eval can have many experiments, each capturing the system's state at one point in time, and any two of them can be compared. The delta between two experiments (which prompts changed, which tools were added, how scores and costs moved) is computed at read time from their snapshots and their runs, never stored.

---

## How the Concepts Relate

```
eval_id                                   the test definition
 ├── host_id (which host it was generated for)
 ├── dataset (row_count rows; default 20, max 100)
 └── scoring_rubric
      (editable until the first experiment; locked after)

experiment_id                             one execution, run once
 ├── eval_id (FK)
 ├── host_composition (snapshot of the live host at run start)
 ├── agent_spec_snapshot (per-agent spec state at run start)
 ├── status (pending → completed | failed)
 └── experiment_runs (one per dataset row)
      ├── row_id (FK → dataset)
      ├── session_id (host session the row ran under → Logs)
      ├── actual_output
      ├── weighted_score (null if the row errored)
      ├── error (failure reason, null on success)
      ├── scores (per criterion: score + rationale)
      └── metrics (operational: tokens, steps, tool calls, latency)

comparison (read time only, nothing stored)
 └── experiment A vs experiment B
      ├── agent_spec_delta (diff of the two snapshots)
      ├── score delta (aggregate + per criterion + per row)
      └── operational delta (tokens, calls, latency)
```

The key invariants:

- The dataset and rubric are fixed per eval_id once the first experiment exists. Every experiment on an eval ran the same inputs and was scored on the same criteria: that is what makes them comparable.
- An experiment always runs against the live host: whatever composition and AgentSpecs are deployed when it starts. The snapshot records exactly that state.
- Nothing derived is stored. Deltas between experiments, score aggregates, and operational rollups are all computed at read time.

The intended loop: define an eval, run an experiment against the current host state, change something (a system prompt, a tool, a model), run another experiment on the same eval, and compare the two.

---

## Structural Design

Both flows are normal Masher `WorkflowSpec` step pipelines. Generation uses an
eval-agent step backed by a SKILL. Experiment execution uses three CodeSteps;
the latter two drive durable host and judge requests with async Python fan-out.

### Generation: `gen-synthetic-evals`

Triggered from the admin UI Evals tab with a target host, optional user guidance, and a row count (default 20, max 100). This is a single eval-agent task backed by the `gen-synthetic-evals` SKILL. The agent reads the host's declared capabilities and generates the dataset and rubric before a code step persists them.

| Step | What It Does |
|---|---|
| generate (SKILL) | The eval agent reads the host composition and generates exactly `row_count` synthetic rows plus a weighted rubric |
| persist (tool) | `run_gen_synthetic_evals_workflow` validates the rows and rubric, writes dataset, rubric, and eval to Postgres, and returns eval_id |

The persistence tool does no LLM work and takes no snapshots. It validates the generated rows and rubric (including that the dataset has exactly the requested number of rows, so the size is deterministic rather than the model's judgment) and hands everything to the eval store.

### Running an experiment: `run-experiment`

Triggered from the admin UI with an `eval_id` and `host_id`, each invocation is
a normal three-CodeStep workflow. The experiment tables are the durable handoff
between preparation, host execution, and judging.

| Stage | What It Does |
|---|---|
| prepare-experiment | Validates the eval and host, snapshots the host, AgentSpecs, rubric, and dataset, and atomically seeds the row ledger |
| execute-rows | Runs unfinished rows through the snapshotted host with async fan-out and persists outputs, failures, sessions, and metrics |
| judge-rows | Scores executed rows with `eval-judge-agent`, updates the ledger, and finalizes the experiment aggregate |

Each phase runs its first unfinished row alone before the rest fan out, so both
the host and judge prompt prefixes are warmed once. Row records move through an
explicit state machine and agent task IDs are deterministic, so replay skips
terminal rows and rejoins requests that completed before their row update was
committed. A row error records its stage and reason without failing other rows.

The first `run-experiment` invocation also locks the eval.

### Judge

For each row, `judge-rows` builds a self-contained message from the snapshotted
rubric, input, and host output, then calls `eval-judge-agent` with a structured
output contract. Python validates every criterion and recomputes
`weighted_score`; it never trusts the model's arithmetic. Dynamic criterion
names remain wrapped in a `json_text` string that deterministic code parses.

### Comparison

Comparison is a read-time operation over two experiments of the same eval. Nothing about a comparison is persisted. Given a baseline experiment and a control experiment, the API:

- diffs the two `agent_spec_snapshot`s into a per-agent delta (system prompt changed, tools or skills added and removed, model changed), reusing the same diff that would apply to any two snapshots;
- computes both score aggregates, mean and per-criterion breakdown, from each experiment's runs and returns them side by side;
- computes both operational rollups the same way;
- pairs runs by `row_id` and returns per-row score deltas along with each side's response and judge scores, so the UI can rank rows by movement and open any row to see what drove the change.

Because deltas are computed rather than stored, any experiment can serve as the baseline for any other experiment on the same eval.

### SKILL

Generation has one SKILL, `gen-synthetic-evals`. It gives masher the instructions for reading a host composition and producing a high-quality dataset and rubric: how to infer scenario diversity from declared capabilities, how to spread rows across sampling categories, how to generate inputs that exercise multi-agent routing, and how to construct and weight rubric criteria. Changing the SKILL changes generation behavior without touching workflow code. Scoring has no SKILL; its judge prompt is built in code from the rubric.

---

## Data Model

### Dataset Row

```python
{
    "row_id": str,                  # UUID
    "input": str,                   # synthetic user message
    "scenario_description": str,    # which capability or path this tests
    "sampling_category": str,       # random | multi_tool | multi_agent |
                                    # high_tokens | long_running | short_running
    "expected_behavior": str,       # what good looks like: a behavioral
                                    # contract, not a fixed output
    "target_agents": list[str],     # which agents should be involved
}
```

`expected_behavior` is a prose description, not a string-match target. For example: "The primary agent should delegate flight queries to FlightSearch, synthesize the results, and respond with at least one concrete option including price and dates." The LLM judge evaluates the actual output against this description.

### Scoring Rubric

```python
{
    "rubric_id": str,               # UUID
    "global_scoring_prompt": str,   # overall context given to the LLM judge
    "criteria": [
        {
            "name": str,            # e.g. "task_completion"
            "description": str,     # what this criterion measures
            "weight": float,        # importance weight, sums to 1.0 across criteria
            "scoring_prompt": str,  # specific judge prompt for this criterion
            "scale_min": int,       # default 1
            "scale_max": int,       # default 5
        }
    ],
}
```

Typical criteria for a multi-agent host: `task_completion`, `subagent_coordination`, `tool_selection_accuracy`, `response_quality`, `factual_grounding`. Weights reflect the host's purpose: a research host weights `factual_grounding` higher; a booking host weights `task_completion`. Weights must sum to 1.0 and are editable by the developer after generation, until the eval's first experiment locks them.

### Eval

```python
{
    "eval_id": str,                     # UUID
    "created_at": datetime,
    "host_id": str,                     # host the eval was generated for
    "user_guidance": str,               # developer's seed guidance
    "dataset_id": str,                  # FK → dataset
    "rubric_id": str,                   # FK → rubric
}
```

The eval carries no host or AgentSpec snapshot. Whether an eval is locked is derived: an eval with at least one experiment is locked.

### Experiment

```python
{
    "experiment_id": str,               # UUID
    "eval_id": str,                     # FK → eval
    "created_at": datetime,             # stamped when scoring starts
    "completed_at": datetime | None,    # stamped when scoring finishes
    "status": str,                      # pending | completed | failed
    "host_composition": {               # live composition at run start
        "primary": str,                 # agent_id
        "subagents": list[str],         # agent_ids
    },
    "agent_spec_snapshot": {            # AgentSpec state at run start
        "<agent_id>": {
            "agent_id": str,
            "system_prompt": str,
            "max_steps": int,
            "max_tokens": int,
            "temperature": float,
            "tools": list[str],         # sorted tool names
            "skills": list[str],        # sorted skill names
            "model": str | None,
        }
    },
}
```

`created_at` marks the start of scoring and `completed_at` the end, so their difference is the experiment's wall-clock duration.

Nothing derived is stored on the experiment. The score aggregate (mean and per-criterion breakdown, using the rubric weights of the eval) and the operational aggregate (token, step, tool-call, and latency rollups) are computed on the fly from `experiment_runs` when the developer opens or compares experiments. The delta between two experiments is likewise computed at comparison time from their snapshots.

### ExperimentRun

One row per dataset row per experiment. This is the leaf-level record; all scoring data lives here.

```python
{
    "run_id": str,                      # deterministic per (experiment, row)
    "experiment_id": str,               # FK → experiment
    "row_id": str,                      # FK → dataset row
    "input": str,                       # denormalized from dataset row for query convenience
    "session_id": str | None,           # host session the row ran under; links to Logs
    "actual_output": str | None,        # full agent response; null if the row errored
    "weighted_score": float | None,     # sum(score * weight) across criteria; null if unscored
    "error": str | None,                # failure reason when the row could not be scored
    "scores": {
        "<criterion_name>": {
            "score": int,               # on the criterion's scale (default 1-5)
            "rationale": str,           # judge's explanation
        }
    },
    "metrics": dict | None,             # operational metrics for this row (see below)
}
```

`weighted_score` per run is computed at score time from `scores` and the rubric weights. Experiment-level score aggregates (mean, per-criterion breakdown) are derived from `weighted_score` and per-criterion scores across all `experiment_runs` for a given `experiment_id` at query time. Rows that errored carry a null `weighted_score` and a populated `error`, and are excluded from the score aggregate. `session_id` deep-links a run to the host session it executed under, so the developer can open the full trace in Logs.

### Operational Metrics

Every dataset row runs through the host under a single session id shared by the primary agent and all of its subagents. That session's runtime events are a self-contained record of what the run cost, independent of the qualitative score. After a row finishes, the workflow folds those events into a `metrics` object and stores it on the run:

```python
{
    "latency_ms": float | None,     # wall time across the session
    "llm_calls": int,
    "steps": int,
    "tool_calls": int,
    "tokens": {
        "input": int,
        "output": int,
        "cache_read": int,
        "cache_creation": int,
    },
    "tool_call_breakdown": dict,    # tool name → call count
    "stop_reason": str | None,      # primary agent's terminal stop reason
    "num_subagent_steps": int,
    "subagents": [                  # per-subagent rollup
        {
            "agent_id": str,
            "steps": int,
            "tool_calls": int,
            "llm_calls": int,
            "stop_reason": str | None,
            "tokens": { ... },
        }
    ],
}
```

The fold is a pure function over the event list with no I/O, so it is deterministic and unit-testable against a captured session. It is best-effort: a row whose host request errored still reports the tokens and steps it spent before failing, and a metrics failure never fails the row. Metrics are rolled up at read time, so an experiment's operational aggregate always matches its persisted rows.

---

## Example End-to-End Flow

**Setup.** A developer has built a travel assistant host:

```
Host: travel-assistant
  primary:   TravelConcierge  (system_prompt: "You are a travel planning assistant...")
  subagents: [FlightSearch, HotelSearch]
```

**Step 1: Generate an eval.**

The developer opens the Evals tab in the admin UI, selects `travel-assistant`, sets the row count to 100, and runs `gen-synthetic-evals` with the following guidance:

> "Users typically ask about round trips, multi-city itineraries, and budget constraints. FlightSearch and HotelSearch should both be exercised."

The workflow runs the SKILL and produces:

- 100 synthetic inputs such as:
  - "Find me a round-trip flight from NYC to Paris under $1000 in mid-August"
  - "Plan a 10-day trip covering Rome, Florence, and Venice with hotels under $150/night"
  - "What's the cheapest way to get from London to Tokyo next month?"
- A rubric with four criteria: `task_completion` (0.4), `subagent_coordination` (0.3), `response_quality` (0.2), `factual_grounding` (0.1)

Result: `eval_id = eval-001`. The developer bumps `task_completion` to 0.45 and trims `subagent_coordination` to 0.25; the eval has no experiments yet, so it is still editable.

**Step 2: Run the first experiment.**

The developer runs `run-experiment` with `eval_id = eval-001` and the target
host. The workflow snapshots the live composition, AgentSpecs, rubric, and
dataset, then executes and judges the 100 durable row records. This first
experiment also locks `eval-001`.

Result: `experiment_id = exp-001`. 100 `experiment_runs` written, one per dataset row, each carrying its score and its metrics.

**Step 3: Change the primary agent's system prompt.**

The developer rewrites TravelConcierge's system prompt to explicitly handle budget constraints and multi-city itineraries.

**Step 4: Run a second experiment.**

The developer runs `run-experiment` again with `eval_id = eval-001`. A new
experiment snapshots the host as it is now and runs the same 100 inputs through
it, scored against the same rubric.

Result: `experiment_id = exp-002`. 100 `experiment_runs` written.

**Step 5: Compare in the Evals tab.**

The developer selects `exp-001` as the baseline and `exp-002` as the control. The comparison is computed on the fly: the spec delta from the two snapshots, the score and operational aggregates from each experiment's runs.

```
eval-001  travel-assistant  100 rows  2026-07-04

  baseline: exp-001
  control:  exp-002

  agent changes (exp-001 → exp-002):
    TravelConcierge:  system_prompt changed
    FlightSearch:     (no changes)
    HotelSearch:      (no changes)

  aggregate score         3.6  →  4.1   (+0.5)

  by criterion:
    task_completion       3.4  →  4.2   (+0.8)   weight: 0.45
    subagent_coordination 3.8  →  3.9   (+0.1)   weight: 0.25
    response_quality      3.5  →  4.3   (+0.8)   weight: 0.2
    factual_grounding     3.2  →  3.8   (+0.6)   weight: 0.1

  operational (mean per row):
    tokens (in / out)     2,940 / 610  →  3,510 / 720
    llm calls             4.1  →  4.6
    tool calls            2.3  →  2.5
    latency               8.4s →  9.1s
```

The prompt rewrite raised scores but also raised cost per row. The operational rollup makes that trade-off visible alongside the quality delta.

The developer then expands the row-level view to see which specific inputs drove the delta. Runs are paired by row_id and ranked by absolute score change between the two experiments:

```
  top regressions (baseline → control):
    row-047  "What's the cheapest way to get to Tokyo?"          3.2 → 2.8  (-0.4)
    row-031  "Can I fly business class for under $500?"          2.9 → 2.6  (-0.3)

  top improvements (baseline → control):
    row-003  "Find a round-trip to Paris under $1000"            2.8 → 4.6  (+1.8)
    row-019  "Plan 10 days in Italy with hotels under $150"      3.0 → 4.5  (+1.5)
    row-055  "Multi-city: NYC → London → Rome, cheapest option"  2.5 → 4.1  (+1.6)
```

The row-level view surfaces that the prompt rewrite improved budget and multi-city scenarios significantly but slightly regressed on open-ended "cheapest overall" queries: an actionable signal, not just a number.

Every run links to the host session it executed under, so the trace tooling in [Reading a trace](reading-a-trace.md) applies to eval rows unchanged. What a scoring run pays the token meter, and why the runner serializes the first row, is measured in [Prompt caching and the token meter](prompt-caching-token-meter.md).
