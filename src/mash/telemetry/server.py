"""Lightweight telemetry server with log tailing via SSE and optional observer UI."""

from __future__ import annotations

import argparse
import errno
import json
import mimetypes
import os
import sys
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from mash.logging.logger import EventLogger

from ..memory.search.service import MemorySearchService
from ..memory.search.types import FusionWeights, RetrievalConfig
from ..memory.store import SQLiteStore
from .ui_loader import UIResolution, resolve_ui_resolution

API_PREFIX = "/api/v1"
DEFAULT_LIMIT = 2000
DEFAULT_SEARCH_LIMIT = 10


class TelemetryHTTPServer(ThreadingHTTPServer):
    """HTTP server with optional memory-search and observer UI context."""

    log_path: Path
    default_limit: int
    search_service: Optional[MemorySearchService]
    memory_db_path: Optional[Path]
    ui_resolution: UIResolution

    def handle_error(self, request, client_address) -> None:  # type: ignore[override]
        """Suppress noisy disconnect tracebacks from browsers/SSE reconnects."""
        _, exc, _ = sys.exc_info()
        if _is_expected_disconnect_error(exc):
            return
        super().handle_error(request, client_address)


class TelemetryHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves telemetry API and optional static observer UI."""

    server_version = "MashTelemetry/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        if parsed.path == API_PREFIX or parsed.path.startswith(f"{API_PREFIX}/"):
            self._dispatch_api(parsed)
            return
        if parsed.path.startswith("/api/"):
            self._send_json_error(
                code="ROUTE_NOT_FOUND",
                message="Route not found",
                status=404,
                details={"path": parsed.path},
            )
            return

        if parsed.path.startswith("/assets/"):
            self._serve_ui_asset(parsed.path)
            return

        if parsed.path == "/" and not self._ui_enabled:
            self._handle_root_api_only()
            return

        if self._ui_enabled:
            self._serve_ui_index()
            return

        self._not_found()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence default http.server logging.
        return

    @property
    def _telemetry_server(self) -> TelemetryHTTPServer:
        return self.server  # type: ignore[return-value]

    @property
    def _ui_enabled(self) -> bool:
        return bool(self._telemetry_server.ui_resolution.enabled)

    def _dispatch_api(self, parsed) -> None:
        if parsed.path == f"{API_PREFIX}/health":
            self._handle_health()
            return
        if parsed.path == f"{API_PREFIX}/logs":
            self._handle_logs(parsed)
            return
        if parsed.path == f"{API_PREFIX}/stream":
            self._handle_stream()
            return
        if parsed.path == f"{API_PREFIX}/search":
            self._handle_search(parsed)
            return
        self._send_json_error(
            code="ROUTE_NOT_FOUND",
            message="Route not found",
            status=404,
            details={"path": parsed.path},
        )

    def _handle_root_api_only(self) -> None:
        ui_resolution = self._telemetry_server.ui_resolution
        self._send_json(
            {
                "service": "mash.telemetry",
                "mode": "api-only",
                "api": {
                    "version": "v1",
                    "base": API_PREFIX,
                    "endpoints": [
                        f"{API_PREFIX}/health",
                        f"{API_PREFIX}/logs",
                        f"{API_PREFIX}/stream",
                        f"{API_PREFIX}/search",
                    ],
                },
                "ui": {
                    "mode": ui_resolution.mode,
                    "available": ui_resolution.available,
                    "enabled": ui_resolution.enabled,
                    "reason": ui_resolution.reason,
                },
            }
        )

    def _handle_health(self) -> None:
        server = self._telemetry_server
        ui = server.ui_resolution
        self._send_json_success(
            {
                "status": "ok",
                "api_version": "v1",
                "log": {
                    "path": str(server.log_path),
                    "exists": server.log_path.exists(),
                    "default_limit": server.default_limit,
                },
                "memory": {
                    "configured": server.memory_db_path is not None,
                    "search_available": server.search_service is not None,
                    "path": str(server.memory_db_path)
                    if server.memory_db_path is not None
                    else None,
                },
                "ui": {
                    "mode": ui.mode,
                    "available": ui.available,
                    "enabled": ui.enabled,
                    "static_dir": str(ui.static_dir)
                    if ui.enabled and ui.static_dir is not None
                    else None,
                    "reason": ui.reason,
                },
            }
        )

    def _handle_logs(self, parsed) -> None:
        server = self._telemetry_server
        params = parse_qs(parsed.query)
        limit = _parse_limit(params.get("limit", [None])[0], default=server.default_limit)

        if not server.log_path.exists():
            self._send_json_error(
                code="LOG_FILE_NOT_FOUND",
                message="Log file not found",
                status=404,
                details={"path": str(server.log_path)},
            )
            return

        events = _read_log(server.log_path, limit)
        self._send_json_success(
            {
                "events": events,
                "path": str(server.log_path),
                "limit": limit,
            }
        )

    def _handle_stream(self) -> None:
        server = self._telemetry_server
        if not server.log_path.exists():
            self._send_json_error(
                code="LOG_FILE_NOT_FOUND",
                message="Log file not found",
                status=404,
                details={"path": str(server.log_path)},
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            with server.log_path.open("r", encoding="utf-8") as handle:
                handle.seek(0, os.SEEK_END)
                while True:
                    line = handle.readline()
                    if not line:
                        time.sleep(0.25)
                        continue
                    payload = line.strip()
                    if not payload:
                        continue
                    try:
                        json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _handle_search(self, parsed) -> None:
        server = self._telemetry_server
        params = parse_qs(parsed.query)
        raw_query = (params.get("q", [""])[0] or "").strip()
        app_id = (params.get("app_id", [""])[0] or "").strip()
        session_id = params.get("session_id", [None])[0] or None
        if isinstance(session_id, str):
            session_id = session_id.strip() or None

        if not raw_query:
            self._send_json_error(
                code="MISSING_QUERY",
                message="q is required",
                status=400,
                details={"param": "q"},
            )
            return
        if not app_id:
            self._send_json_error(
                code="MISSING_APP_ID",
                message="app_id is required",
                status=400,
                details={"param": "app_id"},
            )
            return

        limit = _parse_search_limit(params.get("limit", [None])[0])

        if server.search_service is None:
            self._send_json_error(
                code="MEMORY_SEARCH_UNAVAILABLE",
                message="memory search unavailable (start server with --memory-db)",
                status=503,
            )
            return

        try:
            results = server.search_service.search(
                raw_query,
                limit=limit,
                session_id=session_id,
                app_id=app_id,
            )
        except ValueError as exc:
            self._send_json_error(
                code="SEARCH_VALIDATION_ERROR",
                message=str(exc),
                status=400,
            )
            return
        except (NotImplementedError, RuntimeError) as exc:
            self._send_json_error(
                code="SEARCH_UNAVAILABLE",
                message=str(exc),
                status=503,
            )
            return
        except Exception as exc:  # pragma: no cover - defensive handler guard
            self._send_json_error(
                code="SEARCH_FAILED",
                message=f"search failed: {exc}",
                status=500,
            )
            return

        self._send_json_success(
            {
                "results": [asdict(result) for result in results],
                "app_id": app_id,
                "session_id": session_id,
                "query": raw_query,
                "limit": limit,
            }
        )

    def _serve_ui_index(self) -> None:
        static_dir = self._telemetry_server.ui_resolution.static_dir
        if static_dir is None:
            self._not_found()
            return
        index_path = static_dir / "index.html"
        if not index_path.exists() or not index_path.is_file():
            self._not_found()
            return
        self._serve_file(index_path)

    def _serve_ui_asset(self, request_path: str) -> None:
        static_dir = self._telemetry_server.ui_resolution.static_dir
        if not self._ui_enabled or static_dir is None:
            self._not_found()
            return

        relative = request_path.lstrip("/")
        candidate = (static_dir / relative).resolve()
        try:
            candidate.relative_to(static_dir.resolve())
        except ValueError:
            self._not_found()
            return

        if not candidate.exists() or not candidate.is_file():
            self._not_found()
            return

        self._serve_file(candidate)

    def _serve_file(self, path: Path) -> None:
        content = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)

    def _send_json_success(self, data: Any, status: int = 200) -> None:
        self._send_json({"data": data}, status=status)

    def _send_json_error(
        self,
        *,
        code: str,
        message: str,
        status: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._send_json(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                }
            },
            status=status,
        )

    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b"Not Found")


def _parse_limit(raw: Optional[str], *, default: int) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(value, 20000))


def _parse_search_limit(raw: Optional[str]) -> int:
    if not raw:
        return DEFAULT_SEARCH_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SEARCH_LIMIT
    return max(1, min(value, 50))


def _read_log(path: Path, limit: int) -> List[Dict[str, object]]:
    events: List[Dict[str, object]] = []
    if limit <= 0:
        return events

    with path.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()

    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _is_expected_disconnect_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {errno.EPIPE, errno.ECONNRESET}
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Mash telemetry server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", required=True)
    parser.add_argument("--memory-db")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--ui", choices=("auto", "on", "off"), default="auto")
    args = parser.parse_args()

    log_path = Path(os.path.expanduser(args.log)).resolve()
    event_logger = EventLogger(log_path)

    memory_db_path: Optional[Path] = None
    search_service: Optional[MemorySearchService] = None
    if args.memory_db:
        memory_db_path = Path(os.path.expanduser(args.memory_db)).resolve()
        store = SQLiteStore(memory_db_path)
        search_service = MemorySearchService(
            store,
            event_logger=event_logger,
            retrieval_config=RetrievalConfig(enable_keyword=True, enable_semantic=False),
            fusion_weights=FusionWeights(keyword_weight=1.0, semantic_weight=0.0),
        )

    try:
        ui_resolution = resolve_ui_resolution(args.ui)
    except RuntimeError as exc:
        parser.error(str(exc))

    server = TelemetryHTTPServer((args.host, args.port), TelemetryHandler)
    server.log_path = log_path
    server.default_limit = args.limit
    server.memory_db_path = memory_db_path
    server.search_service = search_service
    server.ui_resolution = ui_resolution

    print(f"Telemetry server listening on http://{args.host}:{args.port}")
    print(f"Log file: {log_path}")
    if memory_db_path is not None:
        print(f"Memory DB: {memory_db_path}")
    else:
        print("Memory DB: not set (pass --memory-db to enable /api/v1/search)")

    if ui_resolution.enabled and ui_resolution.static_dir is not None:
        print(f"UI: enabled ({ui_resolution.mode}), serving static from {ui_resolution.static_dir}")
    else:
        reason = ui_resolution.reason or "UI disabled"
        print(f"UI: not serving observer ({reason})")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
