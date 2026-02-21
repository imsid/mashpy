"""Constants for metrics layer services."""

from __future__ import annotations

import re
from pathlib import Path

METRICS_LAYER_ROOT = Path("src/apps/db/metrics_layer")
SCHEMA_ROOT = METRICS_LAYER_ROOT / "schema"

KIND_TO_SUBDIR = {
    "source": "sources",
    "metric": "metrics",
}

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
DATE_LITERAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AGG_FUNCTION_RE = re.compile(r"^[A-Z_]+$")
AGGREGATED_EXPR_RE = re.compile(
    r"^\s*(SUM|AVG|COUNT|COUNTIF|MIN|MAX|ANY_VALUE|APPROX_COUNT_DISTINCT)\s*\(",
    re.IGNORECASE,
)
