"""Context helpers for metrics layer tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .constants import METRICS_LAYER_ROOT, SCHEMA_ROOT

ToolContext = Dict[str, Any]


def resolve_workspace_path(raw_path: str, root: Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path must be within workspace root: {root}")
    return resolved


def build_tool_context(workspace_root: Path) -> ToolContext:
    root = workspace_root.resolve()
    return {
        "root": root,
        "metrics_root": resolve_workspace_path(METRICS_LAYER_ROOT.as_posix(), root),
        "schema_root": resolve_workspace_path(SCHEMA_ROOT.as_posix(), root),
    }
