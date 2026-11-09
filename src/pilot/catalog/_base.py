"""Shared constants and model builders for Pilot's agents."""

from __future__ import annotations

import os
from pathlib import Path

from mash.core.llm import LLMProvider
from mash.core.llm.gemini import GeminiProvider
from mash.core.llm.oss import GemmaProvider

APP_NAME = "Mash Pilot"

# The mashpy checkout, bind-mounted read-only into every sandbox
# (MASH_SANDBOX_MOUNTS) and into the host container at the same path.
REPO_ROOT = Path("/home/agent/mashpy")

# pilot/skills — recipes the builder loads on demand.
PILOT_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

# The pilot guide runs Gemini directly; the other agents run the open Gemma
# model over OpenRouter's Chat Completions gateway. Both models are
# overridable via ``PILOT_GUIDE_MODEL`` / ``PILOT_OPEN_MODEL``.
DEFAULT_GUIDE_MODEL = "gemini-3.5-flash"
DEFAULT_OPEN_MODEL = "google/gemma-4-31b-it"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_guide_llm(agent_id: str) -> LLMProvider:
    """Build the Gemini provider for the pilot guide.

    Reads the key from ``GEMINI_API_KEY`` (via the provider) and the model from
    ``PILOT_GUIDE_MODEL`` (default :data:`DEFAULT_GUIDE_MODEL`)."""
    if not os.getenv("GEMINI_API_KEY", "").strip():
        raise RuntimeError(f"Agent {agent_id!r} requires GEMINI_API_KEY.")
    return GeminiProvider(
        app_id=agent_id,
        model=os.getenv("PILOT_GUIDE_MODEL", DEFAULT_GUIDE_MODEL),
    )


def build_default_llm(agent_id: str) -> LLMProvider:
    """Build the OpenRouter-backed Gemma provider for the builder and the summarizer.

    Reads the OpenRouter key from ``OPENROUTER_API_KEY`` and the model from
    ``PILOT_OPEN_MODEL`` (default :data:`DEFAULT_OPEN_MODEL`)."""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(f"Agent {agent_id!r} requires OPENROUTER_API_KEY.")
    return GemmaProvider(
        app_id=agent_id,
        model=os.getenv("PILOT_OPEN_MODEL", DEFAULT_OPEN_MODEL),
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
    )
