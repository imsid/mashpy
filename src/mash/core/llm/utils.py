"""Shared helpers for provider adapters."""

from __future__ import annotations

from typing import Any, Dict, Optional


def coerce_block_dict(block: Any, block_type: Optional[str]) -> Dict[str, Any]:
    """Convert a provider block to dictionary format."""
    if isinstance(block, dict):
        return block

    if hasattr(block, "model_dump"):
        try:
            return block.model_dump()
        except TypeError:
            pass

    if hasattr(block, "dict"):
        try:
            return block.dict()
        except TypeError:
            pass

    data: Dict[str, Any] = {}
    raw = getattr(block, "__dict__", None)
    if isinstance(raw, dict):
        data.update(raw)

    if block_type:
        data.setdefault("type", block_type)

    if not data:
        data = {"type": block_type or "unknown", "text": str(block)}

    return data


def block_value(block: Any, key: str) -> Any:
    """Extract a value from an SDK block or plain dictionary."""
    if isinstance(block, dict):
        return block.get(key)

    value = getattr(block, key, None)
    if value is not None:
        return value

    if hasattr(block, "model_dump"):
        try:
            return block.model_dump().get(key)
        except TypeError:
            pass

    if hasattr(block, "dict"):
        try:
            return block.dict().get(key)
        except TypeError:
            pass

    return None
