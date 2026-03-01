"""Optional telemetry observer UI discovery."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class UIResolution:
    """Resolved UI serving settings from the selected ui mode."""

    mode: str
    enabled: bool
    available: bool
    static_dir: Optional[Path]
    reason: Optional[str] = None


def resolve_ui_resolution(mode: str) -> UIResolution:
    """Resolve optional telemetry observer UI availability for the requested mode."""

    normalized = (mode or "auto").strip().lower()
    if normalized not in {"auto", "on", "off"}:
        raise RuntimeError(f"invalid ui mode: {mode!r}")

    if normalized == "off":
        return UIResolution(
            mode=normalized,
            enabled=False,
            available=False,
            static_dir=None,
            reason="disabled by --ui off",
        )

    try:
        provider = import_module("mash_telemetry_web")
    except Exception as exc:
        if normalized == "on":
            raise RuntimeError(
                "--ui on was requested but mash_telemetry_web is unavailable"
            ) from exc
        return UIResolution(
            mode=normalized,
            enabled=False,
            available=False,
            static_dir=None,
            reason="optional package mash_telemetry_web not installed",
        )

    get_static_dir = getattr(provider, "get_static_dir", None)
    if not callable(get_static_dir):
        if normalized == "on":
            raise RuntimeError(
                "--ui on was requested but mash_telemetry_web.get_static_dir() is missing"
            )
        return UIResolution(
            mode=normalized,
            enabled=False,
            available=False,
            static_dir=None,
            reason="optional package mash_telemetry_web is invalid",
        )

    static_dir = Path(get_static_dir()).resolve()
    index_path = static_dir / "index.html"
    if not static_dir.exists() or not index_path.exists():
        if normalized == "on":
            raise RuntimeError(
                f"--ui on was requested but static UI files were not found at {static_dir}"
            )
        return UIResolution(
            mode=normalized,
            enabled=False,
            available=False,
            static_dir=None,
            reason="optional package mash_telemetry_web has no static assets",
        )

    return UIResolution(
        mode=normalized,
        enabled=True,
        available=True,
        static_dir=static_dir,
        reason=None,
    )
