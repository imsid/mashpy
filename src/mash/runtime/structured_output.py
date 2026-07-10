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
    """Normalize a schema for strict structured-output providers.

    OpenAI strict JSON schemas require every object property to appear in that
    object's ``required`` array and reject Pydantic's ``default`` annotations.
    Requiring defaulted fields is safe for response generation: the provider
    emits the value explicitly and Pydantic performs the final validation.
    """
    normalized = deepcopy(schema)
    _normalize_schema_nodes(normalized)
    return normalized


def _normalize_schema_nodes(value: Any) -> None:
    if isinstance(value, dict):
        value.pop("default", None)
        for child in value.values():
            _normalize_schema_nodes(child)
        if value.get("type") == "object":
            value["additionalProperties"] = False
            properties = value.get("properties")
            if isinstance(properties, dict):
                value["required"] = list(properties)
    elif isinstance(value, list):
        for item in value:
            _normalize_schema_nodes(item)


__all__ = ["normalize_structured_output_schema", "serialize_structured_output"]
