"""Argument normalization for metric SQL compilation."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from .constants import DATE_LITERAL_RE
from .pathing import normalize_identifier


def normalize_filters(raw_value: Any) -> List[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise ValueError("filters must be an array")

    filters: List[str] = []
    for idx, value in enumerate(raw_value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"filters[{idx}] must be a non-empty string")
        text = value.strip()
        if ";" in text:
            raise ValueError(f"filters[{idx}] must not contain semicolons")
        filters.append(text)
    return filters


def normalize_date_range(raw_value: Any) -> Optional[Dict[str, str]]:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError("date_range must be an object")

    dimension = normalize_identifier(raw_value.get("dimension"), "date_range.dimension")
    start_raw = raw_value.get("start")
    end_raw = raw_value.get("end")
    if start_raw is None and end_raw is None:
        raise ValueError("date_range must include start or end")

    result: Dict[str, str] = {"dimension": dimension}
    if start_raw is not None:
        result["start"] = _normalize_date_literal(start_raw, "date_range.start")
    if end_raw is not None:
        result["end"] = _normalize_date_literal(end_raw, "date_range.end")
    if "start" in result and "end" in result and result["start"] > result["end"]:
        raise ValueError("date_range.start must be <= date_range.end")
    return result


def normalize_order_by(raw_value: Any) -> List[Dict[str, str]]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise ValueError("order_by must be an array")

    items: List[Dict[str, str]] = []
    for idx, value in enumerate(raw_value):
        if not isinstance(value, dict):
            raise ValueError(f"order_by[{idx}] must be an object")

        field_raw = value.get("field")
        if not isinstance(field_raw, str) or not field_raw.strip():
            raise ValueError(f"order_by[{idx}].field must be a non-empty string")

        field = field_raw.strip()
        if field != "metric_value":
            field = normalize_identifier(field, f"order_by[{idx}].field")

        direction_raw = value.get("direction")
        if not isinstance(direction_raw, str) or not direction_raw.strip():
            raise ValueError(f"order_by[{idx}].direction must be a non-empty string")
        direction = direction_raw.strip().upper()
        if direction not in {"ASC", "DESC"}:
            raise ValueError(f"order_by[{idx}].direction must be one of: ASC, DESC")

        items.append({"field": field, "direction": direction})
    return items


def normalize_limit(raw_value: Any) -> int:
    if raw_value is None:
        return 100
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise ValueError("limit must be an integer")
    if raw_value < 1 or raw_value > 1000:
        raise ValueError("limit must be between 1 and 1000")
    return raw_value


def _normalize_date_literal(raw_value: Any, field_name: str) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError(f"{field_name} must be a non-empty YYYY-MM-DD string")
    value = raw_value.strip()
    if not DATE_LITERAL_RE.fullmatch(value):
        raise ValueError(f"{field_name} must match YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid date") from exc
    return value
