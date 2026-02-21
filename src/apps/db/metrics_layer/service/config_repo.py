"""Repository helpers for metrics layer configs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .context import ToolContext
from .pathing import normalize_identifier, resolve_config_path


def list_configs(context: ToolContext, dataset_filter: Optional[str]) -> Dict[str, Any]:
    metrics_root = context["metrics_root"]
    root = context["root"]

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

    return {
        "root": metrics_root.relative_to(root).as_posix(),
        "dataset_id": dataset_filter,
        "count": len(configs),
        "configs": sorted(configs, key=lambda item: item["path"]),
    }


def read_config(
    context: ToolContext, kind: str, dataset_id: Any, name: Any
) -> Dict[str, Any]:
    root = context["root"]
    path, normalized_dataset_id, normalized_name = resolve_config_path(
        context=context,
        kind=kind,
        dataset_id=dataset_id,
        name=name,
    )
    if not path.exists():
        raise ValueError(f"config file not found: {path.relative_to(root).as_posix()}")
    if not path.is_file():
        raise ValueError(f"config path is not a file: {path.relative_to(root).as_posix()}")
    content = path.read_text(encoding="utf-8")

    return {
        "kind": kind,
        "dataset_id": normalized_dataset_id,
        "name": normalized_name,
        "path": path.relative_to(root).as_posix(),
        "size": len(content),
        "content": content,
    }


def read_yaml_config(path: Path, expected_kind: str) -> Dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise ValueError(f"{expected_kind} config file not found: {path.as_posix()}")
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to parse YAML file {path.as_posix()}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"config file must parse to object: {path.as_posix()}")
    kind = parsed.get("kind")
    if kind != expected_kind:
        raise ValueError(
            f"expected kind '{expected_kind}' but found '{kind}' in {path.as_posix()}"
        )
    return parsed


def load_metric_entries_by_dataset(
    context: ToolContext, dataset_id: str
) -> Dict[str, Dict[str, Any]]:
    metrics_dir = context["metrics_root"] / dataset_id / "metrics"
    if not metrics_dir.exists() or not metrics_dir.is_dir():
        raise ValueError(f"metrics directory not found for dataset '{dataset_id}'")

    entries: Dict[str, Dict[str, Any]] = {}
    for path in sorted(metrics_dir.glob("*.yml")):
        config = read_yaml_config(path=path, expected_kind="metric")
        metric_id = normalize_identifier(config.get("id"), "metric.id")
        entry = {"id": metric_id, "name": path.stem, "path": path, "config": config}
        for alias in {metric_id, path.stem}:
            if alias in entries:
                raise ValueError(
                    f"duplicate metric alias '{alias}' in dataset '{dataset_id}'"
                )
            entries[alias] = entry
    return entries


def load_source_config(
    context: ToolContext,
    dataset_id: str,
    source_id: str,
    source_cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    if source_id in source_cache:
        return source_cache[source_id]

    source_path, _, _ = resolve_config_path(
        context=context,
        kind="source",
        dataset_id=dataset_id,
        name=source_id,
    )
    source_cfg = read_yaml_config(path=source_path, expected_kind="source")
    source_cache[source_id] = source_cfg
    return source_cfg
