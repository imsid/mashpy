"""Serve the built admin dashboard from packaged static assets.

The dashboard calls ``/api/v1`` with the same-origin ``mash_api_key`` cookie.
Only ``POST /admin/session`` sets that cookie, and only after it has checked
the key the operator typed: serving the dashboard itself never hands out the
key.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .routes.common import (
    API_KEY_COOKIE,
    APIError,
    api_key_matches,
    state_from_request,
    success,
)


# The dashboard runs only its own scripts and loads its fonts from Google
# Fonts. No other site may frame it.
ADMIN_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
ADMIN_HEADERS = {
    "Content-Security-Policy": ADMIN_CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


class AdminSessionRequest(BaseModel):
    api_key: str


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


def mount_admin_ui(app: FastAPI, *, api_prefix: str) -> None:
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

    @app.post("/admin/session", include_in_schema=False)
    async def admin_session(request: Request, body: AdminSessionRequest) -> JSONResponse:
        """Check the operator's API key and keep it in the dashboard's cookie."""
        expected_key = state_from_request(request).api_key
        if expected_key is None:
            # No key configured: the API is open and needs no cookie.
            return JSONResponse(success({"authenticated": True}))
        if not api_key_matches(body.api_key.strip() or None, expected_key):
            raise APIError(
                code="UNAUTHORIZED",
                message="valid API key is required",
                status_code=401,
            )
        response = JSONResponse(success({"authenticated": True}))
        response.set_cookie(
            key=API_KEY_COOKIE,
            value=expected_key,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path=api_prefix,
        )
        return response

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_index() -> FileResponse:
        return FileResponse(index_path, headers=ADMIN_HEADERS)

    @app.get("/admin/{path:path}", include_in_schema=False)
    def admin_spa(path: str) -> FileResponse:
        del path
        return FileResponse(index_path, headers=ADMIN_HEADERS)


__all__ = ["admin_assets_available", "get_admin_static_dir", "mount_admin_ui"]
