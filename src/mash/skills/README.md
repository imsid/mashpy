# Skills

`src/mash/skills` defines optional capability bundles that agents can load
on demand through a meta-`Skill` tool.

A skill is a named piece of markdown content. When the model invokes the
`Skill` tool with a skill name, the content is loaded into the conversation as
instructions the agent should follow.

Skills come in two flavors:

- **Filesystem-backed**: an agent directory containing a `SKILL.md` file with
  YAML frontmatter. Loaded lazily when the model invokes the tool.
- **Inline / dynamic**: the markdown is supplied as a string at registration
  time. Useful when the host generates skill content from another source.

Skills can be attached statically at host build time, or registered/removed
dynamically at runtime through the host API and HTTP API.

## Public Surface

```python
from mash.skills import Skill, SkillRegistry, SkillTool
```

## The `Skill` Dataclass

`Skill` is a frozen dataclass defined in [base.py](./base.py):

```python
@dataclass(frozen=True)
class Skill:
    type: str
    name: str
    description: str = ""
    location: str | None = None
    content: str | None = None
```

Invariants:

- `name` is required.
- At least one of `location` or `content` must be set.

Conventional `type` values:

- `"custom"` — filesystem-backed; `location` points to a directory containing
  `SKILL.md`.
- `"dynamic"` — inline; `content` holds the skill markdown.

## Filesystem-Backed Skills (`SKILL.md` Format)

A filesystem-backed skill lives in a directory containing a `SKILL.md` file
with YAML frontmatter. Frontmatter is parsed by
[`_parse_skill_frontmatter`](./registry.py); only `name` and `description`
are recognized.

```markdown
---
name: trace-digest-workflow
description: Run Masher's diagnostic trace digest workflow.
---

# Trace Digest Workflow

Use this skill only for workflow id `masher-trace-digest` and task id
`digest-traces`.

(Markdown body follows…)
```

When the model invokes the `Skill` tool with this skill's name, `SkillTool`
reads `<location>/SKILL.md` at invocation time and returns a JSON payload
shaped like:

```json
{
  "base_path": "/abs/path/to/skill-dir",
  "skill_path": "/abs/path/to/skill-dir/SKILL.md",
  "skill_name": "trace-digest-workflow",
  "skill_md": "---\nname: ...\n---\n\n# Trace Digest Workflow\n..."
}
```

## Registering Skills At Build Time

Inside `AgentSpec.build_skills()`, populate a `SkillRegistry`:

```python
from mash.skills import Skill, SkillRegistry


class PrimaryAgent(AgentSpec):
    def build_skills(self) -> SkillRegistry:
        registry = SkillRegistry()
        registry.register(
            Skill(
                type="custom",
                name="trace-digest-workflow",
                description="Run Masher's diagnostic trace digest workflow.",
                location="/path/to/skills/trace-digest-workflow",
            )
        )
        return registry
```

A skill discovered by `SkillRegistry.get_custom_skills(skills_dir)` is
registered with `type="custom"` and `location` set to the discovered
directory.

## Registering Skills Dynamically

`AgentPool` exposes runtime registration. If the agent runtime is already
started, the live `SkillTool` is refreshed in place (see
[`_register_runtime_skill`](../runtime/host/host.py) and
[`_refresh_runtime_skill_tool`](../runtime/host/host.py)). If the runtime has
not started yet, the skill is queued and installed when the runtime opens.

```python
from mash.skills import Skill

host.register_agent_skill(
    "data",
    Skill(
        type="dynamic",
        name="workflow:experiment-readout:v1",
        description="Execute Experiment Readout workflow v1.",
        content="# Experiment Readout\n\nLoad and run the workflow…",
    ),
)

# Later:
host.unregister_agent_skill("data", "workflow:experiment-readout:v1")
```

Re-registering the same `name` for the same `agent_id` raises `ValueError`.
Unregister first if you want to replace inline content.

## HTTP API

`POST /api/v1/agent/{agent_id}/skill` accepts a `RegisterAgentSkillRequest`
body (see
[`src/mash/api/routes/common.py`](../api/routes/common.py)):

```json
{
  "type": "dynamic",
  "name": "workflow:experiment-readout:v1",
  "description": "Execute Experiment Readout workflow v1.",
  "location": null,
  "content": "# Experiment Readout\n\nLoad and run the workflow…"
}
```

`location` and `content` are both optional in the schema but at least one
must be set (Skill validation rule).

```bash
curl -X POST http://127.0.0.1:8000/api/v1/agent/data/skill \
  -H "Authorization: Bearer $MASH_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "dynamic",
    "name": "workflow:experiment-readout:v1",
    "description": "Execute Experiment Readout workflow v1.",
    "content": "# Experiment Readout\n\nLoad and run the workflow…"
  }'
```

Response: `{"data": {"agent_id": "data", "skill_name": "workflow:experiment-readout:v1"}}`.

There is no DELETE endpoint yet; dynamic unregistration is only available
through the in-process host API.

## How Skills Are Surfaced To The Model

The host installs a meta-tool named `Skill` on the agent. The tool's
description lists every registered skill name plus its `description`, and the
tool's input schema enumerates the available skill names. The model invokes
the tool with `{"name": "<skill_name>"}`, and the tool's result is the JSON
payload above. The agent then reads `skill_md` as instructions and proceeds.

## Persistence Notes

- Dynamic skills are live host state. Applications that own authoring must
  republish dynamic skills on host restart.
- Filesystem-backed skills persist with the codebase / agent package.

## What This Package Does Not Do

- It does not own skill content authoring or storage.
- It does not enforce a SKILL.md schema beyond the `name` / `description`
  frontmatter keys.
- It is distinct from the built-in tool system; skills are not tools, they
  are instruction bundles surfaced through one shared meta-tool.
