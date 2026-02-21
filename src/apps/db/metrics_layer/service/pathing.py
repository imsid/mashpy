"""Path and identifier helpers for metrics layer services."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple

from .constants import IDENTIFIER_RE, KIND_TO_SUBDIR, METRICS_LAYER_ROOT
from .context import ToolContext, resolve_workspace_path


def normalize_identifier(value: Any, field_name: str) -> str:
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


def ensure_kind(raw_kind: Any) -> str:
    if not isinstance(raw_kind, str):
        raise ValueError("kind is required")
    kind = raw_kind.strip().lower()
    if kind not in KIND_TO_SUBDIR:
        raise ValueError("kind must be one of: source, metric")
    return kind


def resolve_config_path(
    context: ToolContext, kind: str, dataset_id: Any, name: Any
) -> Tuple[Path, str, str]:
    normalized_dataset = normalize_identifier(dataset_id, "dataset_id")
    normalized_name = normalize_identifier(name, "name")
    subdir = KIND_TO_SUBDIR[kind]
    relative_path = METRICS_LAYER_ROOT / normalized_dataset / subdir / f"{normalized_name}.yml"
    resolved = resolve_workspace_path(relative_path.as_posix(), context["root"])
    return resolved, normalized_dataset, normalized_name


def normalize_identifier_list(
    raw_value: Any, field_name: str, required: bool = False
) -> List[str]:
    if raw_value is None:
        if required:
            raise ValueError(f"{field_name} is required")
        return []
    if not isinstance(raw_value, list):
        raise ValueError(f"{field_name} must be an array")

    result: List[str] = []
    seen = set()
    for value in raw_value:
        normalized = normalize_identifier(value, f"{field_name}[]")
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    if required and not result:
        raise ValueError(f"{field_name} must include at least one value")
    return result
