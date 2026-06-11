---
title: "Skills: Instructions on Demand"
description: Skills are markdown instruction bundles loaded on demand through one meta-tool, keeping the prompt small while the capability library grows.
date: 2026-06-10
author: imsid
tags:
  - internals
  - skills
---

# Skills: Instructions on Demand

A skill is a named piece of markdown. When the model invokes the `Skill` tool with a skill's name, that markdown lands in the conversation as instructions to follow. The feature is that small, and the design question it answers is about token cost.

An agent that knows how to write changelogs, run quizzes, and produce trace digests needs detailed instructions for each. Putting them all in the system prompt means every request pays for all of them, on every turn, even though a given request uses at most one. Tool definitions also ride on [every LLM request](one-llm-contract.md), so registering each bundle as a tool carries the same cost. The instructions need to exist somewhere the model can reach while staying out of the per-request payload.

```mermaid
flowchart LR
    subgraph upfront ["Every request carries"]
        T["Skill (meta-tool)\n- changelog: one line\n- mash-quiz: one line\n- trace-digest: one line"]
    end
    subgraph lazy ["Loaded only when invoked"]
        C["changelog SKILL.md\n(full instructions)"]
        Q["mash-quiz SKILL.md"]
        D["trace-digest SKILL.md"]
    end
    T -- "Skill(name='changelog')" --> C
```

## One meta-tool, N skills

Mash surfaces every registered skill through a single tool named `Skill`. The tool's description lists each skill's name and one-line description, and its input schema enumerates the valid names. The per-request cost of a skill is therefore one line of tool description; the full markdown is read only when the model asks for it.

The registration is automatic and gated by config:

```python
# src/mash/core/agent.py: Agent.__init__ (trimmed)
if self.config.skills_enabled and self.skills.list_skills():
    if "Skill" not in self.tools:
        self.tools.register(SkillTool(self.skills))
```

`skills_enabled` defaults to `False`; an agent with no skills never carries the meta-tool at all.

## The skill itself

`Skill` is a frozen dataclass with two flavors distinguished by where the markdown lives:

```python
# src/mash/skills/base.py
@dataclass(frozen=True)
class Skill:
    type: str                    # "custom" (filesystem) or "dynamic" (inline)
    name: str
    description: str = ""
    location: str | None = None  # directory containing SKILL.md
    content: str | None = None   # inline markdown
```

A **filesystem-backed** skill is a directory with a `SKILL.md` whose frontmatter carries `name` and `description`; nothing else is parsed:

```markdown
---
name: changelog
description: Generate a changelog entry from recent commits.
---

# Changelog

Scan the most recent commits, group them by area, and append
an entry to CHANGELOG.md following the existing format…
```

When the model invokes `Skill(name="changelog")`, the tool reads `SKILL.md` at invocation time and returns a JSON payload with the markdown plus its paths (`base_path`, `skill_path`), so instructions can reference files relative to the skill directory. Reading at invocation time also means editing a `SKILL.md` on disk takes effect on the next invocation.

A **dynamic** skill skips the filesystem: the markdown is supplied as `content` at registration. This is the flavor for hosts that generate instructions from another system, like an app that authors workflows elsewhere and publishes them to Mash as skills.

## Registration: build time and runtime

Static skills go in `AgentSpec.build_skills()` and live with the codebase. Dynamic skills can be pushed to a running host:

```python
host.register_agent_skill("pilot", Skill(
    type="dynamic",
    name="workflow:experiment-readout:v1",
    description="Execute Experiment Readout workflow v1.",
    content=generated_markdown,
))
```

or over HTTP with `POST /api/v1/agent/{agent_id}/skill`. If the agent's runtime is already started, the live `SkillTool` is refreshed in place, and the model's next request sees the new skill in the tool's enum. If the runtime hasn't started yet, the skill is queued and installed when it opens.

Dynamic skills are live host state. A host restart forgets them, and the application that owns authoring is expected to republish them on startup. Mash surfaces skills; storing and versioning them is the owning application's job, which is why the example name carries a `:v1`.

## Skills are not tools

A tool invocation makes something happen in the world and returns a result. A `Skill` invocation changes only what the model knows for the rest of the request; the returned markdown is input, not effect. That's why a skill carries only a name, a description, and markdown, while approval gating, parameter schemas, and result types stay with tools. It's also why one executor can serve every skill: executing a skill is just a read.

Skills come back two posts from now, where dynamic workflows ship their task instructions as skills. The next post stays with context. The markdown a skill loads goes into the same conversation context that memory builds, and memory has its own rules about what to keep and what to summarize away.

*Next: [Memory and Compaction](memory-and-compaction.md).*
