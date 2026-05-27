"""Serialization helpers for request-scoped structured output."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def serialize_structured_output(value: Any) -> dict[str, Any] | None:
    """Serialize Pydantic models and close object schemas for provider portability."""
    if value is None:
        return None

    if isinstance(value, dict):
        return normalize_structured_output_schema(value)

    model_json_schema = getattr(value, "model_json_schema", None)
    if callable(model_json_schema):
        schema = model_json_schema()
        if not isinstance(schema, dict):
            raise ValueError("structured_output Pydantic model produced an invalid schema")
        return normalize_structured_output_schema(schema)

    raise TypeError("structured_output must be a Pydantic model or JSON-schema payload")

def normalize_structured_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively set additionalProperties=false for every object node."""
    normalized = deepcopy(schema)
    _close_object_nodes(normalized)
    return normalized


def _close_object_nodes(value: Any) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _close_object_nodes(child)
        if value.get("type") == "object":
            value["additionalProperties"] = False
    elif isinstance(value, list):
        for item in value:
            _close_object_nodes(item)


__all__ = ["normalize_structured_output_schema", "serialize_structured_output"]
