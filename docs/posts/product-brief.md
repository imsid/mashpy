---
title: Mash Product Brief
description: Mash is a self-hosted, durable runtime for building agentic applications where users and agents work together.
date: 2026-11-21
author: imsid
tags:
  - product
  - overview
---

# Mash Product Brief

Mash is a self-hosted, durable runtime for building agentic applications
where users and agents work together. You write your agents and workflows in
Python, deploy them behind one API on your own Postgres, and connect them to
the places your users already are: a Slack workspace, your product, a
terminal.

## How it works

Your application sends Mash events, one for each thing a user says or does.
A session holds the context of one conversation, so every agent in it knows
what came before. An app holds memory across its sessions, so a new
conversation starts with what earlier ones learned. A policy controls what
agents can access, such as a sandbox to run code in and a browser to work on
websites.

Mash has no concept of a user. The application decides who takes part in a
session, whether one person or a whole team, and everyone in it shares the
same context.

## Example: a team bot in Slack

A team bot is a good fit. Each channel is an app, each thread is a session,
and each message is an event.

```
Slack                                            Mash
───────────────────────────────────────────────  ─────────────────────────────────
#growth                                          app: growth
│                                                (memory for the channel)
│
├─ Thread                                        session
│  │                                             policy: growth
│  │
│  ├─ Alice: @bot pull last week's churn         event → agents
│  ├─ bot:   Run this query on the replica?      approval from an agent
│  │         [Approve] [Deny]
│  ├─ Alice: Approve                             answer to the approval
│  ├─ bot:   Churn was 2.1%, up from 1.8%        response
│  ├─ Bob:   @bot break it down by plan          event in the same session,
│  │                                             with Alice's context
│  └─ bot:   Pro 1.2%, Team 3.4%                 response
│
└─ Thread                                        new session, same app
   ├─ Carol: @bot what drove the Team churn?     event; knows what the
   └─ bot:   ...                                 channel has looked at
```

Alice asks for churn numbers and the bot hands the question to the right
agents. One of them checks with her before it touches the database, and she
approves from the thread. Bob picks up where she left off without repeating
anything. A week later Carol opens a new thread, and the bot already knows
what the channel has been working on.

## What agents can do

Agents in Mash work for people, and they check in with them. An agent can ask
for approval before it acts, ask a question when it needs an answer, and hand
a browser to the user to sign in to a site. The request waits for the person,
through restarts, for as long as the policy allows.

Each policy gives agents a computer to work on. In the sandbox, agents write
and run code: they analyse a file, build a chart or a page, keep a database,
and hand the user a link to what they made. In the browser, they work on the
sites the user has signed in to, such as downloading a bill or checking an
order. Mash never sees the password, and the sandbox holds no secrets.

A host puts several agents and workflows behind one conversation. A built-in
companion reads each message, sends the work to the agents that fit it,
running them side by side when the parts are independent, and writes one
answer. Workflows cover the work that should run the same way every time, as
typed steps in code with agents where they are needed.

## Running Mash

Mash runs on your infrastructure: one host process, your Postgres, and Docker
for the sandboxes and browsers. Every request is durable, so a restart picks
up mid-conversation. Each agent picks its own model, from Anthropic, OpenAI
and Gemini to open-source models like Gemma, Qwen and DeepSeek on your own
hardware.

You work with it over an HTTP API from any stack, or from the `mash` CLI and
REPL, which you can ship as your own branded client. Every request is traced
with OpenTelemetry and can go to Grafana, Datadog or any OTLP backend. Before
launch, synthetic evals generate test scenarios from your host's agents and
score how well it handles them, so you can compare changes on quality and
cost.

## Where to go next

- [**Getting started**](../index.md): install, define agents and a host, and
  send your first event
- [**Mash Under the Hood**](mash-under-the-hood.md): each capability in
  more depth
- [**Code as the Source of Truth**](code-as-the-source-of-truth.md): taking
  a workflow from pure code to an agent step to a deployment
- [**Synthetic Evals**](synthetic-evals.md): datasets, rubrics and
  experiments over the live host
- [**Building an agent CLI**](building-agent-clis.md): ship your own client
  on the REPL
- [**Deployment guide**](how-to-deploy.md): the host, agent processes,
  sandboxes and browsers on a server
