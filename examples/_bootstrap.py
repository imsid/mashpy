"""Bootstrap helpers for running examples from a repository checkout."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_LOCAL_DATA_DIR = ".mash-data"
DEFAULT_CONTAINER_DATA_DIR = "/var/lib/mash"


def load_example_env() -> None:
    """Load `.env` files commonly used with local examples."""
    repo_root = Path(__file__).resolve().parents[1]
    # Keep existing shell values authoritative (override=False default).
    load_dotenv(repo_root / "examples" / ".env")


def require_anthropic_api_key() -> str:
    """Return ANTHROPIC_API_KEY or raise with an actionable setup error."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        return api_key
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set. Export it in your shell or add it to "
        "repo `.env` or `examples/.env`."
    )


def require_openai_api_key() -> str:
    """Return OPENAI_API_KEY or raise with an actionable setup error."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if api_key:
        return api_key
    raise RuntimeError(
        "OPENAI_API_KEY is not set. Export it in your shell or add it to "
        "repo `.env` or `examples/.env`."
    )
