"""Serialization helpers for request-scoped structured output."""

from __future__ import annotations

from typing import Any


def serialize_structured_output(value: Any) -> dict[str, Any] | None:
    """Serialize Pydantic models; pass caller-provided JSON schemas through."""
    if value is None:
        return None

    if isinstance(value, dict):
        return dict(value)

    model_json_schema = getattr(value, "model_json_schema", None)
    if callable(model_json_schema):
        schema = model_json_schema()
        if not isinstance(schema, dict):
            raise ValueError("structured_output Pydantic model produced an invalid schema")
        return dict(schema)

    raise TypeError("structured_output must be a Pydantic model or JSON-schema payload")


__all__ = ["serialize_structured_output"]
