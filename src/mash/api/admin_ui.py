"""Serve the built admin dashboard from packaged static assets."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .telemetry_ui import TELEMETRY_API_KEY_COOKIE


def get_admin_static_dir() -> Path:
    """Return the directory containing bundled admin dashboard assets."""

    static_dir = Path(__file__).resolve().parent / "static" / "admin"
    index_path = static_dir / "index.html"
    if not static_dir.exists() or not index_path.exists():
        raise RuntimeError(
            f"packaged admin UI assets are missing at {static_dir}; rebuild mash admin web assets"
        )
    return static_dir


def admin_assets_available() -> bool:
    """Whether the packaged admin assets are present (built and synced)."""

    static_dir = Path(__file__).resolve().parent / "static" / "admin"
    return static_dir.exists() and (static_dir / "index.html").exists()


def mount_admin_ui(app: FastAPI) -> None:
    """Mount the admin SPA at /admin when its assets are available.

    Mounting is best-effort: a deployment that never built the admin bundle
    simply does not expose the route, matching how the telemetry UI behaves
    when its assets are missing.
    """

    if not admin_assets_available():
        return

    static_dir = get_admin_static_dir()
    index_path = static_dir / "index.html"

    app.mount(
        "/admin/assets",
        StaticFiles(directory=static_dir / "assets"),
        name="admin-assets",
    )

    def _build_index_response(request: Request) -> FileResponse:
        response = FileResponse(index_path)
        runtime_state = getattr(request.app.state, "runtime_state", None)
        api_key = getattr(runtime_state, "api_key", None)
        if api_key:
            response.set_cookie(
                key=TELEMETRY_API_KEY_COOKIE,
                value=api_key,
                httponly=True,
                secure=False,
                samesite="lax",
                path="/",
            )
        return response

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_index(request: Request) -> FileResponse:
        return _build_index_response(request)

    @app.get("/admin/{path:path}", include_in_schema=False)
    def admin_spa(path: str, request: Request) -> FileResponse:
        del path
        return _build_index_response(request)


__all__ = ["admin_assets_available", "get_admin_static_dir", "mount_admin_ui"]
