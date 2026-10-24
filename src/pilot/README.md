# Mash Pilot

Pilot is a command-line guide to the Mash codebase, built on the Mash SDK and
shipped in this repo at [`src/pilot/`](.). You ask Pilot about Mash and it
answers from the source tree, or asks its builder to build a Mash app for you.

```text
> Summarize how HostBuilder registers pooled agents and host compositions.
> Trace how an accepted request moves through AgentRuntime and RequestEngine.
> When is request.waiting emitted, and what does it mean for a busy session?
```

## Models and keys

The `pilot` guide runs **Gemini** (`gemini-3.5-flash`), read from
`GEMINI_API_KEY`. The `builder` and the changelog summarizer run an **open
Gemma model over OpenRouter** (`google/gemma-4-31b-it`), read from
`OPENROUTER_API_KEY`. Both keys are required. Each model is overridable:

| Variable | Purpose | Default |
|---|---|---|
| `GEMINI_API_KEY` | Key for the guide (required) | — |
| `OPENROUTER_API_KEY` | Key for the builder and the changelog summarizer (required) | — |
| `MASH_HOST_SECRET` | Derives each agent's token (required; `openssl rand -hex 32`) | — |
| `PILOT_PRIMARY_MODEL` | Override the guide's model | `gemini-3.5-flash` |
| `PILOT_SUBAGENT_MODEL` | Override the builder's and summarizer's model | `google/gemma-4-31b-it` |

## Quick start

Pilot runs from a checkout of this repo with Docker Compose. The host runs its
agents as processes inside its container. Through the Docker socket it starts
a sandbox per user for their bash commands, and mounts the checkout read-only
into each one at `/home/agent/mashpy`.

```bash
# 1. Clone the repo and put the keys in .env
git clone https://github.com/imsid/mashpy.git && cd mashpy
printf 'GEMINI_API_KEY=...\nOPENROUTER_API_KEY=sk-or-...\nMASH_HOST_SECRET=%s\n' "$(openssl rand -hex 32)" > .env

# 2. Start Postgres and the host, from the repo root
docker compose -f docker-compose.pilot.yml up -d --build

# 3. Install the CLI, save who you are, and ask
curl -fsSL https://raw.githubusercontent.com/imsid/mashpy/main/install.sh | sh
mash connect --api-base-url http://127.0.0.1:8000 --user-id me
pilot repl --host guide
```

Your sandbox starts with your first bash command; `mash sandbox ls` lists the
sandboxes. The agents read your checkout as it is, so edits show up without a
restart. Pilot's own code is in the image: after changing it, run
`up -d --build`, then remove the sandboxes with
`docker rm -f $(docker ps -aq --filter label=mash.sandbox.user)` so the next
command starts them from the new image. Their volumes, and the apps the
builder made, are kept.

## The guide team

| Agent | Role |
|-------|------|
| `pilot` | The guide. Answers questions about Mash from the mounted source, docs and git history. |
| `builder` | Builds Mash apps in its folder of your sandbox (`/home/agent/workspaces/builder/apps/<name>`), installs mashpy with uv, and checks them by importing the pool and running the app's tests. |

The `guide` host makes `pilot` the primary and `builder` its subagent. The
guide delegates to the builder when you ask for an app.

## A workflow it ships

Pilot registers the `pilot-changelog` workflow. A `CodeStep` reads
`CHANGELOG.md` and an `AgentStep` summarizes the most recent release, scanning
the source when an entry is too terse to explain on its own. It's attached to
the `guide` host, so `/workflow run pilot-changelog` runs it from the REPL. See
[`catalog/workflows/`](catalog/workflows/) for the spec.

## Building your own app

The builder follows the `build-mash-agent`, `build-mash-workflow`, and
`build-mash-host` skills:

```text
> Build me a support agent with a knowledge base search tool and human approval for refunds.
> Scaffold a multi-agent code reviewer with separate agents for security, style, and correctness.
> I need an agent that connects to my MCP server at localhost:3000 and uses Gemini.
```

It cannot serve what it builds, because a sandbox has no Docker. Copy the app
out with `docker cp <container>:/home/agent/workspaces/builder/apps/<name> .`,
using the container name `mash sandbox ls` shows for your user, and run it as
any Mash app. Point the CLI at another Mash deployment with
`--api-base-url`. Treat this package as a reference when structuring your own
multi-agent app.

## Try Mushy with Pilot

Thought capture is always enabled for Pilot's agents when their provider
supports it. Gemini records exposed summaries alongside the existing
request events. No agent configuration, second provider, or model call is needed.

Run the updated host and ask Pilot a question normally. Open
`http://127.0.0.1:8000/admin/thoughts?agent=pilot` to see its latest 50 traces with
nonempty summaries. The full original words remain visible, with clickable
underlined phrases and Mushy alongside. Use **View logs** to inspect a trace.
The Agent dropdown changes the feed, and Agents cards offer **View thoughts**.
The Logs trace sidebar also offers **View thoughts** for that exact trace.

Requests made before capture was enabled may have no usable summaries and are
excluded. The retired public GIF links are no longer served; the prototype tables
and generated GIF data were removed directly from Pilot.
