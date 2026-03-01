"""Static observer UI package for Mash telemetry."""

from __future__ import annotations

from pathlib import Path


def get_static_dir() -> Path:
    """Return the directory containing static observer UI assets."""
    return Path(__file__).resolve().parent / "static"


__all__ = ["get_static_dir"]
