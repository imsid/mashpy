"""Deterministic local tools for DB roles."""

from __future__ import annotations

import hashlib
import json
import re
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from mash.tools.base import FunctionTool, Tool, ToolResult

METRICS_LAYER_ROOT = Path("src/apps/db/metrics-layer")
SCHEMA_ROOT = METRICS_LAYER_ROOT / "schema"
KIND_TO_SUBDIR = {
    "source": "sources",
    "metric": "metrics",
}
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")

ToolContext = Dict[str, Any]


def _resolve_workspace_path(raw_path: str, root: Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path must be within workspace root: {root}")
    return resolved


def _to_json(payload: Dict[str, Any]) -> ToolResult:
    return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2))


def _normalize_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if normalized.endswith(".yml"):
        normalized = normalized[:-4]
    if "/" in normalized or "\\" in normalized:
        raise ValueError(f"{field_name} must not contain path separators")
    if not IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must match regex {IDENTIFIER_RE.pattern}")
    return normalized


def _ensure_kind(raw_kind: Any) -> str:
    if not isinstance(raw_kind, str):
        raise ValueError("kind is required")
    kind = raw_kind.strip().lower()
    if kind not in KIND_TO_SUBDIR:
        raise ValueError("kind must be one of: source, metric")
    return kind


def _resolve_config_path(
    context: ToolContext, kind: str, dataset_id: Any, name: Any
) -> Tuple[Path, str, str]:
    normalized_dataset = _normalize_identifier(dataset_id, "dataset_id")
    normalized_name = _normalize_identifier(name, "name")
    subdir = KIND_TO_SUBDIR[kind]
    relative_path = (
        METRICS_LAYER_ROOT / normalized_dataset / subdir / f"{normalized_name}.yml"
    )
    resolved = _resolve_workspace_path(relative_path.as_posix(), context["root"])
    return resolved, normalized_dataset, normalized_name


def list_metrics_layer_configs(
    args: Dict[str, Any], context: ToolContext
) -> ToolResult:
    dataset_arg = args.get("dataset_id")
    metrics_root = context["metrics_root"]
    root = context["root"]

    try:
        dataset_filter = None
        if dataset_arg is not None:
            dataset_filter = _normalize_identifier(dataset_arg, "dataset_id")

        configs: List[Dict[str, Any]] = []
        for candidate in metrics_root.rglob("*.yml"):
            relative = candidate.relative_to(metrics_root).as_posix()
            parts = relative.split("/")
            if len(parts) != 3:
                continue
            dataset_id, subdir, filename = parts
            if dataset_filter and dataset_id != dataset_filter:
                continue
            if subdir not in {"sources", "metrics"}:
                continue
            kind = "source" if subdir == "sources" else "metric"
            configs.append(
                {
                    "kind": kind,
                    "dataset_id": dataset_id,
                    "name": Path(filename).stem,
                    "path": candidate.relative_to(root).as_posix(),
                }
            )

        payload = {
            "root": metrics_root.relative_to(root).as_posix(),
            "dataset_id": dataset_filter,
            "count": len(configs),
            "configs": sorted(configs, key=lambda item: item["path"]),
        }
        return _to_json(payload)
    except Exception as exc:
        return ToolResult.error(f"list_metrics_layer_configs failed: {exc}")


def read_metrics_layer_config(args: Dict[str, Any], context: ToolContext) -> ToolResult:
    root = context["root"]
    try:
        kind = _ensure_kind(args.get("kind"))
        path, dataset_id, name = _resolve_config_path(
            context=context,
            kind=kind,
            dataset_id=args.get("dataset_id"),
            name=args.get("name"),
        )
        if not path.exists():
            return ToolResult.error(
                f"config file not found: {path.relative_to(root).as_posix()}"
            )
        if not path.is_file():
            return ToolResult.error(
                f"config path is not a file: {path.relative_to(root).as_posix()}"
            )
        content = path.read_text(encoding="utf-8")
        payload = {
            "kind": kind,
            "dataset_id": dataset_id,
            "name": name,
            "path": path.relative_to(root).as_posix(),
            "size": len(content),
            "content": content,
        }
        return _to_json(payload)
    except Exception as exc:
        return ToolResult.error(f"read_metrics_layer_config failed: {exc}")


def validate_and_write_metrics_layer_config(
    args: Dict[str, Any], context: ToolContext
) -> ToolResult:
    content = args.get("content")
    create_dirs = bool(args.get("create_dirs", False))
    root = context["root"]
    if not isinstance(content, str):
        return ToolResult.error("content must be a string")

    kind: Optional[str] = None
    dataset_id: Optional[str] = None
    name: Optional[str] = None
    path: Optional[Path] = None
    try:
        kind = _ensure_kind(args.get("kind"))
        path, dataset_id, name = _resolve_config_path(
            context=context,
            kind=kind,
            dataset_id=args.get("dataset_id"),
            name=args.get("name"),
        )
        schema_path, schema_text = _load_metrics_layer_schema_text(
            context=context, schema_kind=kind
        )
        valid, validation_errors, parse_error = _validate_yaml_text(
            document_text=content,
            schema_text=schema_text,
        )
        if parse_error:
            return ToolResult.error(
                json.dumps(
                    {
                        "status": "validation_failed",
                        "stage": "parse",
                        "kind": kind,
                        "dataset_id": dataset_id,
                        "name": name,
                        "schema_path": schema_path.relative_to(root).as_posix(),
                        "error": parse_error,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
        if not valid:
            return ToolResult.error(
                json.dumps(
                    {
                        "status": "validation_failed",
                        "stage": "schema_validation",
                        "kind": kind,
                        "dataset_id": dataset_id,
                        "name": name,
                        "schema_path": schema_path.relative_to(root).as_posix(),
                        "errors": validation_errors,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
        if not path.parent.exists() and not create_dirs:
            return ToolResult.error(
                json.dumps(
                    {
                        "status": "write_failed",
                        "stage": "write",
                        "kind": kind,
                        "dataset_id": dataset_id,
                        "name": name,
                        "path": path.relative_to(root).as_posix(),
                        "error": "parent directory does not exist; set create_dirs=true",
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
        if create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        payload = {
            "status": "written",
            "kind": kind,
            "dataset_id": dataset_id,
            "name": name,
            "path": path.relative_to(root).as_posix(),
            "schema_path": schema_path.relative_to(root).as_posix(),
            "validation": {"valid": True, "errors": []},
            "bytes_written": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        return _to_json(payload)
    except Exception as exc:
        return ToolResult.error(
            json.dumps(
                {
                    "status": "write_failed",
                    "stage": "write",
                    "kind": kind,
                    "dataset_id": dataset_id,
                    "name": name,
                    "path": path.relative_to(root).as_posix() if path else None,
                    "error": str(exc),
                },
                ensure_ascii=True,
                indent=2,
            )
        )


def get_metrics_layer_schema(args: Dict[str, Any], context: ToolContext) -> ToolResult:
    raw_schema_kind = args.get("schema_kind")
    root = context["root"]
    if not isinstance(raw_schema_kind, str):
        return ToolResult.error("schema_kind is required")
    schema_kind = raw_schema_kind.strip().lower()
    if schema_kind not in {"source", "metric"}:
        return ToolResult.error("schema_kind must be one of: source, metric")

    try:
        schema_path, content = _load_metrics_layer_schema_text(
            context=context, schema_kind=schema_kind
        )
        payload = {
            "schema_kind": schema_kind,
            "path": schema_path.relative_to(root).as_posix(),
            "size": len(content),
            "content": content,
        }
        return _to_json(payload)
    except Exception as exc:
        return ToolResult.error(f"get_metrics_layer_schema failed: {exc}")


def validate_yaml(args: Dict[str, Any], context: ToolContext) -> ToolResult:
    del context
    document_text = args.get("document_text")
    schema_text = args.get("schema_text")
    if not isinstance(document_text, str) or not isinstance(schema_text, str):
        return ToolResult.error("document_text and schema_text must be strings")

    valid, errors, parse_error = _validate_yaml_text(
        document_text=document_text,
        schema_text=schema_text,
    )
    if parse_error:
        return ToolResult.error(f"invalid yaml: {parse_error}")
    payload = {"valid": valid, "errors": errors}
    return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2))


def _load_metrics_layer_schema_text(
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


def _validate_yaml_text(
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


def build_steward_tools(workspace_root: Path) -> List[Tool]:
    """Build tools used by the data steward role."""

    root = workspace_root.resolve()
    context: ToolContext = {
        "root": root,
        "metrics_root": _resolve_workspace_path(METRICS_LAYER_ROOT.as_posix(), root),
        "schema_root": _resolve_workspace_path(SCHEMA_ROOT.as_posix(), root),
    }

    return [
        FunctionTool(
            name="list_metrics_layer_configs",
            description=(
                "List source/metric config files under src/apps/db/metrics-layer."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "dataset_id": {
                        "type": "string",
                        "description": "Optional dataset id filter.",
                    }
                },
            },
            _executor=partial(list_metrics_layer_configs, context=context),
        ),
        FunctionTool(
            name="read_metrics_layer_config",
            description=(
                "Read one deterministic source/metric config by kind, dataset_id, and name."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["source", "metric"],
                    },
                    "dataset_id": {
                        "type": "string",
                    },
                    "name": {
                        "type": "string",
                        "description": "Config name without path; .yml optional.",
                    },
                },
                "required": ["kind", "dataset_id", "name"],
            },
            _executor=partial(read_metrics_layer_config, context=context),
        ),
        FunctionTool(
            name="validate_and_write_metrics_layer_config",
            description=(
                "Validate a source/metric config against schema and write only if valid."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["source", "metric"],
                    },
                    "dataset_id": {
                        "type": "string",
                    },
                    "name": {
                        "type": "string",
                        "description": "Config name without path; .yml optional.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                    "create_dirs": {
                        "type": "boolean",
                        "description": "Create parent directories when missing.",
                    },
                },
                "required": ["kind", "dataset_id", "name", "content"],
            },
            _executor=partial(validate_and_write_metrics_layer_config, context=context),
        ),
        FunctionTool(
            name="get_metrics_layer_schema",
            description=(
                "Read a metrics-layer YAML schema for source or metric config kinds."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "schema_kind": {
                        "type": "string",
                        "enum": ["source", "metric"],
                    }
                },
                "required": ["schema_kind"],
            },
            _executor=partial(get_metrics_layer_schema, context=context),
        ),
        FunctionTool(
            name="validate_yaml",
            description=("Validate a YAML document against a lightweight YAML schema."),
            parameters={
                "type": "object",
                "properties": {
                    "document_text": {
                        "type": "string",
                        "description": "YAML document to validate.",
                    },
                    "schema_text": {
                        "type": "string",
                        "description": "YAML schema definition.",
                    },
                },
                "required": ["document_text", "schema_text"],
            },
            _executor=partial(validate_yaml, context=context),
        ),
    ]


def build_analyst_tools(workspace_root: Path) -> List[Tool]:
    """Build tools for a future analyst role."""
    del workspace_root
    return []


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
