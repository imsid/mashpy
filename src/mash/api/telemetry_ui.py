"""Serve the built telemetry UI from packaged static assets."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

TELEMETRY_API_KEY_COOKIE = "mash_api_key"


def get_telemetry_static_dir() -> Path:
    """Return the directory containing bundled telemetry UI assets."""

    static_dir = Path(__file__).resolve().parent / "static" / "telemetry"
    index_path = static_dir / "index.html"
    if not static_dir.exists() or not index_path.exists():
        raise RuntimeError(
            f"packaged telemetry UI assets are missing at {static_dir}; rebuild mash telemetry web assets"
        )
    return static_dir


def mount_telemetry_ui(app: FastAPI) -> None:
    """Mount the telemetry SPA at /telemetry."""

    static_dir = get_telemetry_static_dir()
    index_path = static_dir / "index.html"

    app.mount(
        "/telemetry/assets",
        StaticFiles(directory=static_dir / "assets"),
        name="telemetry-assets",
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

    @app.get("/telemetry", include_in_schema=False)
    @app.get("/telemetry/", include_in_schema=False)
    def telemetry_index(request: Request) -> FileResponse:
        return _build_index_response(request)

    @app.get("/telemetry/{path:path}", include_in_schema=False)
    def telemetry_spa(path: str, request: Request) -> FileResponse:
        del path
        return _build_index_response(request)


__all__ = ["TELEMETRY_API_KEY_COOKIE", "get_telemetry_static_dir", "mount_telemetry_ui"]
