# Skills

`src/mash/skills` defines optional skill capabilities that can be attached to agents.

## What This Package Does
- Defines the skill interface used by optional agent capabilities.
- Provides the registry used to enable skills for an agent.
- Adapts skills into tool-facing behavior when they need to be exposed to the model.
- Supports filesystem-backed skills and inline dynamic skill content.

## Main Components
- `base.py`: skill interface and base behavior.
- `registry.py`: enabled-skill registry.
- `tool.py`: adaptation of skills into tool-facing behavior.

## Role In The System
- Skills are optional extensions layered on top of the core runtime.
- They remain distinct from built-in tools and host composition behavior.
