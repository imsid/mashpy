"""Deprecated telemetry module.

Use the standalone `mash-api` package for runtime and observability APIs.
"""

from .ui_loader import UIResolution, resolve_ui_resolution

__all__ = ["UIResolution", "resolve_ui_resolution"]
