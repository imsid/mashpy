"""Tool-facing public entrypoints for metrics layer services."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from mash.tools.base import ToolResult

from ...config import BIGQUERY_PROJECT_ID
from .config_repo import (
    list_configs,
    load_metric_entries_by_dataset,
    read_config,
)
from .context import ToolContext
from .pathing import ensure_kind, normalize_identifier, normalize_identifier_list, resolve_config_path
from .query_args import (
    normalize_date_range,
    normalize_filters,
    normalize_limit,
    normalize_order_by,
)
from .sql_compiler import compile_metric_plan
from .yaml_schema import load_metrics_layer_schema_text, validate_yaml_text


def _to_json(payload: Dict[str, Any]) -> ToolResult:
    return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2))


def list_metrics_layer_configs(
    args: Dict[str, Any], context: ToolContext
) -> ToolResult:
    dataset_arg = args.get("dataset_id")

    try:
        dataset_filter = None
        if dataset_arg is not None:
            dataset_filter = normalize_identifier(dataset_arg, "dataset_id")
        payload = list_configs(context=context, dataset_filter=dataset_filter)
        return _to_json(payload)
    except Exception as exc:
        return ToolResult.error(f"list_metrics_layer_configs failed: {exc}")


def read_metrics_layer_config(args: Dict[str, Any], context: ToolContext) -> ToolResult:
    try:
        kind = ensure_kind(args.get("kind"))
        payload = read_config(
            context=context,
            kind=kind,
            dataset_id=args.get("dataset_id"),
            name=args.get("name"),
        )
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
    path = None
    try:
        kind = ensure_kind(args.get("kind"))
        path, dataset_id, name = resolve_config_path(
            context=context,
            kind=kind,
            dataset_id=args.get("dataset_id"),
            name=args.get("name"),
        )
        schema_path, schema_text = load_metrics_layer_schema_text(
            context=context, schema_kind=kind
        )
        valid, validation_errors, parse_error = validate_yaml_text(
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
        schema_path, content = load_metrics_layer_schema_text(
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

    valid, errors, parse_error = validate_yaml_text(
        document_text=document_text,
        schema_text=schema_text,
    )
    if parse_error:
        return ToolResult.error(f"invalid yaml: {parse_error}")
    payload = {"valid": valid, "errors": errors}
    return ToolResult.success(json.dumps(payload, ensure_ascii=True, indent=2))


def compile_metric_configs_to_sql(
    args: Dict[str, Any], context: ToolContext
) -> ToolResult:
    dataset_id: Optional[str] = None
    try:
        dataset_id = normalize_identifier(args.get("dataset_id"), "dataset_id")
        metric_names = normalize_identifier_list(
            args.get("metric_names"), field_name="metric_names", required=True
        )
        dimensions = normalize_identifier_list(args.get("dimensions"), "dimensions")
        filters = normalize_filters(args.get("filters"))
        date_range = normalize_date_range(args.get("date_range"))
        order_by = normalize_order_by(args.get("order_by"))
        limit = normalize_limit(args.get("limit"))

        metric_entries = load_metric_entries_by_dataset(
            context=context, dataset_id=dataset_id
        )
        source_cache: Dict[str, Dict[str, Any]] = {}
        plans: List[Dict[str, Any]] = []
        errors: List[Dict[str, Optional[str]]] = []

        for metric_name in metric_names:
            try:
                plan = compile_metric_plan(
                    context=context,
                    dataset_id=dataset_id,
                    requested_metric_name=metric_name,
                    metric_entries=metric_entries,
                    source_cache=source_cache,
                    requested_dimensions=dimensions,
                    filters=filters,
                    date_range=date_range,
                    order_by=order_by,
                    limit=limit,
                    bigquery_project_id=BIGQUERY_PROJECT_ID,
                )
                plans.append(plan)
            except Exception as exc:
                errors.append({
                    "metric_name": metric_name,
                    "error": str(exc),
                })

        if errors:
            return ToolResult.error(
                json.dumps(
                    {
                        "status": "compile_failed",
                        "dataset_id": dataset_id,
                        "errors": errors,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )

        payload = {
            "dataset_id": dataset_id,
            "count": len(plans),
            "plans": plans,
        }
        return _to_json(payload)
    except Exception as exc:
        return ToolResult.error(
            json.dumps(
                {
                    "status": "compile_failed",
                    "dataset_id": dataset_id,
                    "errors": [{"metric_name": None, "error": str(exc)}],
                },
                ensure_ascii=True,
                indent=2,
            )
        )
