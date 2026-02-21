"""YAML schema loading and validation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

import yaml

from .context import ToolContext


def load_metrics_layer_schema_text(
    context: ToolContext, schema_kind: str
) -> Tuple[Path, str]:
    schema_root = context["schema_root"]
    root = context["root"]
    schema_path = schema_root / f"{schema_kind}.schema.yml"
    if not schema_path.exists() or not schema_path.is_file():
        raise ValueError(
            f"schema not found: {schema_path.relative_to(root).as_posix()}"
        )
    content = schema_path.read_text(encoding="utf-8")
    return schema_path, content


def validate_yaml_text(
    document_text: str, schema_text: str
) -> Tuple[bool, List[str], Optional[str]]:
    try:
        document = yaml.safe_load(document_text)
        schema = yaml.safe_load(schema_text)
    except Exception as exc:
        return False, [], str(exc)

    errors: List[str] = []
    _validate_against_schema(value=document, schema=schema, path="$", errors=errors)
    return len(errors) == 0, errors, None


def _validate_against_schema(
    value: Any, schema: Any, path: str, errors: List[str]
) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{path}: schema must be an object")
        return

    expected_type = schema.get("type")
    if expected_type:
        if not _matches_type(value, expected_type):
            errors.append(
                f"{path}: expected type '{expected_type}', got '{type(value).__name__}'"
            )
            return

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        errors.append(f"{path}: value '{value}' is not in enum {enum_values}")

    if expected_type == "object" and isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"{path}: missing required key '{key}'")

        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, prop_schema in properties.items():
                if key in value:
                    _validate_against_schema(
                        value=value[key],
                        schema=prop_schema,
                        path=f"{path}.{key}",
                        errors=errors,
                    )

        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            for key in value.keys():
                if key not in properties:
                    errors.append(f"{path}: unexpected key '{key}'")

    if expected_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema is not None:
            for idx, item in enumerate(value):
                _validate_against_schema(
                    value=item,
                    schema=item_schema,
                    path=f"{path}[{idx}]",
                    errors=errors,
                )


def _matches_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(
            value, bool
        )
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True
