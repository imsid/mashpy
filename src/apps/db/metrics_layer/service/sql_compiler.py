"""Metric config to SQL compiler helpers."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .config_repo import load_source_config
from .constants import AGG_FUNCTION_RE, AGGREGATED_EXPR_RE
from .context import ToolContext
from .pathing import normalize_identifier


def compile_metric_plan(
    context: ToolContext,
    dataset_id: str,
    requested_metric_name: str,
    metric_entries: Dict[str, Dict[str, Any]],
    source_cache: Dict[str, Dict[str, Any]],
    requested_dimensions: List[str],
    filters: List[str],
    date_range: Optional[Dict[str, str]],
    order_by: List[Dict[str, str]],
    limit: int,
    bigquery_project_id: Optional[str],
) -> Dict[str, Any]:
    metric_entry = _resolve_metric_entry(metric_entries, requested_metric_name)
    metric_cfg = metric_entry["config"]
    metric_id = metric_entry["id"]

    source_id = normalize_identifier(metric_cfg.get("base_source"), "base_source")
    source_cfg = load_source_config(
        context=context,
        dataset_id=dataset_id,
        source_id=source_id,
        source_cache=source_cache,
    )
    source_dim_map = _build_source_dimension_map(source_cfg=source_cfg)

    for dimension in requested_dimensions:
        if dimension not in source_dim_map:
            raise ValueError(f"dimension '{dimension}' not found in source '{source_id}'")

    metric_allowed_dimensions = metric_cfg.get("dimensions")
    if isinstance(metric_allowed_dimensions, list):
        allowed = {
            normalize_identifier(raw, "metric.dimensions[]")
            for raw in metric_allowed_dimensions
        }
        for dimension in requested_dimensions:
            if dimension not in allowed:
                raise ValueError(
                    f"dimension '{dimension}' is not allowed by metric '{metric_id}'"
                )

    date_clauses = _build_date_clauses(
        date_range=date_range,
        source_id=source_id,
        source_dim_map=source_dim_map,
    )
    _validate_order_by(order_by=order_by, dimensions=requested_dimensions)
    metric_expr, warnings = _compile_metric_sql_expr(
        metric_id=metric_id,
        metric_entries=metric_entries,
        source_cfg=source_cfg,
        expected_source_id=source_id,
        stack=[],
    )

    where_clauses = filters + date_clauses
    select_items = [
        f"{source_dim_map[dimension]} AS {dimension}"
        for dimension in requested_dimensions
    ]
    select_items.append(f"{metric_expr} AS metric_value")

    source_dataset = normalize_identifier(source_cfg.get("dataset"), "source.dataset")
    source_table = normalize_identifier(source_cfg.get("table"), "source.table")
    table_ref = _build_table_ref(
        source_dataset=source_dataset,
        source_table=source_table,
        bigquery_project_id=bigquery_project_id,
    )
    sql = _build_metric_sql(
        select_items=select_items,
        table_ref=table_ref,
        dimensions=requested_dimensions,
        where_clauses=where_clauses,
        order_by=order_by,
        limit=limit,
    )

    return {
        "metric_name": requested_metric_name,
        "source_id": source_id,
        "table_ref": table_ref,
        "sql": sql,
        "dimensions": requested_dimensions,
        "filters": filters,
        "order_by": order_by,
        "limit": limit,
        "warnings": warnings,
    }


def _build_table_ref(
    source_dataset: str, source_table: str, bigquery_project_id: Optional[str]
) -> str:
    if bigquery_project_id:
        return f"`{bigquery_project_id}.{source_dataset}.{source_table}`"
    return f"`{source_dataset}.{source_table}`"


def _build_metric_sql(
    select_items: List[str],
    table_ref: str,
    dimensions: List[str],
    where_clauses: List[str],
    order_by: List[Dict[str, str]],
    limit: int,
) -> str:
    lines = ["SELECT"]
    for idx, item in enumerate(select_items):
        suffix = "," if idx < len(select_items) - 1 else ""
        lines.append(f"  {item}{suffix}")

    lines.append(f"FROM {table_ref}")

    if where_clauses:
        lines.append("WHERE")
        for idx, clause in enumerate(where_clauses):
            prefix = "  " if idx == 0 else "  AND "
            lines.append(f"{prefix}{clause}")

    if dimensions:
        lines.append("GROUP BY " + ", ".join(dimensions))

    if order_by:
        order_clause = ", ".join(
            f"{item['field']} {item['direction']}" for item in order_by
        )
        lines.append(f"ORDER BY {order_clause}")

    lines.append(f"LIMIT {limit}")
    return "\n".join(lines)


def _compile_metric_sql_expr(
    metric_id: str,
    metric_entries: Dict[str, Dict[str, Any]],
    source_cfg: Dict[str, Any],
    expected_source_id: str,
    stack: List[str],
) -> Tuple[str, List[str]]:
    if metric_id in stack:
        chain = " -> ".join(stack + [metric_id])
        raise ValueError(f"cyclic metric reference detected: {chain}")

    metric_entry = metric_entries.get(metric_id)
    if metric_entry is None:
        raise ValueError(f"metric id '{metric_id}' was not found in metrics_layer configs")

    metric_cfg = metric_entry["config"]
    metric_source_id = normalize_identifier(metric_cfg.get("base_source"), "base_source")
    if metric_source_id != expected_source_id:
        raise ValueError(
            "cross-source metric references are not supported in the compiler; "
            f"metric '{metric_id}' uses source '{metric_source_id}' "
            f"but expected '{expected_source_id}'"
        )

    metric_type = metric_cfg.get("type")
    if metric_type == "simple":
        expr_raw = metric_cfg.get("expr")
        if not isinstance(expr_raw, str) or not expr_raw.strip():
            raise ValueError(f"metric '{metric_id}' must define a non-empty expr")
        return _compile_simple_metric_expr(expr_raw, source_cfg=source_cfg)

    if metric_type == "ratio":
        numerator_raw = metric_cfg.get("numerator")
        denominator_raw = metric_cfg.get("denominator")
        if not isinstance(numerator_raw, str) or not numerator_raw.strip():
            raise ValueError(f"metric '{metric_id}' must define numerator")
        if not isinstance(denominator_raw, str) or not denominator_raw.strip():
            raise ValueError(f"metric '{metric_id}' must define denominator")

        next_stack = stack + [metric_id]
        numerator_expr, numerator_warnings = _compile_ratio_component_expr(
            component_expr=numerator_raw,
            metric_entries=metric_entries,
            source_cfg=source_cfg,
            expected_source_id=expected_source_id,
            stack=next_stack,
        )
        denominator_expr, denominator_warnings = _compile_ratio_component_expr(
            component_expr=denominator_raw,
            metric_entries=metric_entries,
            source_cfg=source_cfg,
            expected_source_id=expected_source_id,
            stack=next_stack,
        )
        ratio_expr = f"SAFE_DIVIDE(({numerator_expr}), ({denominator_expr}))"
        return ratio_expr, numerator_warnings + denominator_warnings

    raise ValueError(
        f"metric '{metric_id}' type must be one of: simple, ratio; got '{metric_type}'"
    )


def _compile_ratio_component_expr(
    component_expr: str,
    metric_entries: Dict[str, Dict[str, Any]],
    source_cfg: Dict[str, Any],
    expected_source_id: str,
    stack: List[str],
) -> Tuple[str, List[str]]:
    normalized_component = component_expr.strip()
    if normalized_component in metric_entries:
        return _compile_metric_sql_expr(
            metric_id=normalized_component,
            metric_entries=metric_entries,
            source_cfg=source_cfg,
            expected_source_id=expected_source_id,
            stack=stack,
        )

    metric_ids = sorted(metric_entries.keys(), key=len, reverse=True)
    if not metric_ids:
        return _compile_simple_metric_expr(normalized_component, source_cfg=source_cfg)

    token_pattern = re.compile(r"\b(" + "|".join(re.escape(mid) for mid in metric_ids) + r")\b")
    replacement_count = 0
    warnings: List[str] = []
    compiled_cache: Dict[str, str] = {}

    def _replace(match: re.Match[str]) -> str:
        nonlocal replacement_count
        metric_id = match.group(1)
        replacement_count += 1
        if metric_id not in compiled_cache:
            compiled_expr, nested_warnings = _compile_metric_sql_expr(
                metric_id=metric_id,
                metric_entries=metric_entries,
                source_cfg=source_cfg,
                expected_source_id=expected_source_id,
                stack=stack,
            )
            compiled_cache[metric_id] = compiled_expr
            warnings.extend(nested_warnings)
        return f"({compiled_cache[metric_id]})"

    substituted = token_pattern.sub(_replace, normalized_component)
    if replacement_count == 0:
        return _compile_simple_metric_expr(normalized_component, source_cfg=source_cfg)
    return substituted, warnings


def _compile_simple_metric_expr(
    expr: str, source_cfg: Dict[str, Any]
) -> Tuple[str, List[str]]:
    normalized_expr = expr.strip()
    if _looks_aggregated_expression(normalized_expr):
        return normalized_expr, []

    by_name, by_expr = _build_source_measure_indexes(source_cfg=source_cfg)
    measure = by_name.get(normalized_expr) or by_expr.get(normalized_expr)
    if measure is None:
        return (
            f"SUM({normalized_expr})",
            [
                "simple metric expression did not match a source measure; "
                "used SUM(expr) fallback"
            ],
        )

    agg = measure["agg"]
    measure_expr = measure["expr"]
    return _render_aggregate_expr(expr=measure_expr, agg=agg), []


def _build_source_measure_indexes(
    source_cfg: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    measures_raw = source_cfg.get("measures")
    if not isinstance(measures_raw, list):
        raise ValueError("source.measures must be an array")

    by_name: Dict[str, Dict[str, str]] = {}
    by_expr: Dict[str, Dict[str, str]] = {}
    for index, measure_raw in enumerate(measures_raw):
        if not isinstance(measure_raw, dict):
            raise ValueError(f"source.measures[{index}] must be an object")
        name = normalize_identifier(measure_raw.get("name"), "source.measures[].name")
        expr = measure_raw.get("expr")
        agg = measure_raw.get("agg")
        if not isinstance(expr, str) or not expr.strip():
            raise ValueError(f"source.measures[{index}].expr must be a non-empty string")
        if not isinstance(agg, str) or not agg.strip():
            raise ValueError(f"source.measures[{index}].agg must be a non-empty string")

        entry = {"name": name, "expr": expr.strip(), "agg": agg.strip().upper()}
        by_name[name] = entry
        by_expr[entry["expr"]] = entry
    return by_name, by_expr


def _render_aggregate_expr(expr: str, agg: str) -> str:
    normalized_agg = agg.strip().upper()
    if not AGG_FUNCTION_RE.fullmatch(normalized_agg):
        raise ValueError(f"unsupported measure aggregation '{agg}'")

    normalized_expr = expr.strip()
    if normalized_agg == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {normalized_expr})"
    if normalized_agg == "COUNT":
        return f"COUNT({normalized_expr})"
    return f"{normalized_agg}({normalized_expr})"


def _looks_aggregated_expression(expr: str) -> bool:
    if AGGREGATED_EXPR_RE.match(expr):
        return True
    return bool(re.match(r"^\s*COUNT\s*\(\s*DISTINCT\b", expr, re.IGNORECASE))


def _build_source_dimension_map(source_cfg: Dict[str, Any]) -> Dict[str, str]:
    dimensions_raw = source_cfg.get("dimensions")
    if not isinstance(dimensions_raw, list):
        raise ValueError("source.dimensions must be an array")

    mapping: Dict[str, str] = {}
    for idx, dimension_raw in enumerate(dimensions_raw):
        if not isinstance(dimension_raw, dict):
            raise ValueError(f"source.dimensions[{idx}] must be an object")
        name = normalize_identifier(
            dimension_raw.get("name"), "source.dimensions[].name"
        )
        expr = dimension_raw.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            raise ValueError(f"source.dimensions[{idx}].expr must be a non-empty string")
        mapping[name] = expr.strip()
    return mapping


def _build_date_clauses(
    date_range: Optional[Dict[str, str]],
    source_id: str,
    source_dim_map: Dict[str, str],
) -> List[str]:
    if date_range is None:
        return []

    dimension = date_range["dimension"]
    if dimension not in source_dim_map:
        raise ValueError(f"date_range.dimension '{dimension}' not found in '{source_id}'")

    expr = source_dim_map[dimension]
    clauses: List[str] = []
    if "start" in date_range:
        clauses.append(f"DATE({expr}) >= DATE '{date_range['start']}'")
    if "end" in date_range:
        clauses.append(f"DATE({expr}) <= DATE '{date_range['end']}'")
    return clauses


def _validate_order_by(order_by: List[Dict[str, str]], dimensions: List[str]) -> None:
    allowed_fields = set(dimensions) | {"metric_value"}
    for idx, item in enumerate(order_by):
        field = item["field"]
        if field not in allowed_fields:
            raise ValueError(
                f"order_by[{idx}].field '{field}' must be one of: "
                f"{sorted(allowed_fields)}"
            )


def _resolve_metric_entry(
    metric_entries: Dict[str, Dict[str, Any]], requested_metric_name: str
) -> Dict[str, Any]:
    requested = normalize_identifier(requested_metric_name, "metric_names[]")
    entry = metric_entries.get(requested)
    if entry is None:
        raise ValueError(f"metric '{requested}' not found for requested dataset")
    return entry
