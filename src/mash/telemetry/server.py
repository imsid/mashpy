"""Lightweight telemetry server with log tailing via SSE."""

from __future__ import annotations

import argparse
import json
import os
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

DEFAULT_LIMIT = 2000
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_LOG_PATH: Optional[Path] = None
DEFAULT_MEMORY_DB_PATH: Optional[Path] = None


class TelemetryHTTPServer(ThreadingHTTPServer):
    """HTTP server with optional memory-search context."""

    search_service: Optional[MemorySearchService]
    memory_db_path: Optional[Path]


class TelemetryHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves log snapshots and SSE tail."""

    server_version = "MashTelemetry/0.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        if parsed.path == "/api/logs":
            self._handle_logs(parsed)
            return
        if parsed.path == "/api/stream":
            self._handle_stream(parsed)
            return
        if parsed.path == "/api/search":
            self._handle_search(parsed)
            return
        self._not_found()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence default http.server logging.
        return

    def _handle_logs(self, parsed) -> None:
        params = parse_qs(parsed.query)
        try:
            path = _resolve_log_path(params.get("path", [None])[0])
        except ValueError:
            self._send_json({"error": "log path required"}, status=400)
            return
        limit = _parse_limit(params.get("limit", [None])[0])
        if not path.exists():
            self._send_json({"error": "log file not found"}, status=404)
            return

        events = _read_log(path, limit)
        self._send_json({"events": events, "path": str(path)})

    def _handle_stream(self, parsed) -> None:
        params = parse_qs(parsed.query)
        try:
            path = _resolve_log_path(params.get("path", [None])[0])
        except ValueError:
            self._send_json({"error": "log path required"}, status=400)
            return
        if not path.exists():
            self._send_json({"error": "log file not found"}, status=404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            with path.open("r", encoding="utf-8") as handle:
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
        params = parse_qs(parsed.query)
        raw_query = (params.get("q", [""])[0] or "").strip()
        app_id = (params.get("app_id", [""])[0] or "").strip()
        session_id = params.get("session_id", [None])[0] or None
        if isinstance(session_id, str):
            session_id = session_id.strip() or None

        if not raw_query:
            self._send_json({"error": "q is required"}, status=400)
            return
        if not app_id:
            self._send_json({"error": "app_id is required"}, status=400)
            return

        limit = _parse_search_limit(params.get("limit", [None])[0])

        search_service = getattr(self.server, "search_service", None)
        if search_service is None:
            self._send_json(
                {"error": "memory search unavailable (start server with --memory-db)"},
                status=503,
            )
            return

        try:
            results = search_service.search(
                raw_query,
                limit=limit,
                session_id=session_id,
                app_id=app_id,
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except (NotImplementedError, RuntimeError) as exc:
            self._send_json({"error": str(exc)}, status=503)
            return
        except Exception as exc:  # pragma: no cover - defensive handler guard
            self._send_json({"error": f"search failed: {exc}"}, status=500)
            return

        self._send_json(
            {
                "results": [asdict(result) for result in results],
                "app_id": app_id,
                "session_id": session_id,
                "query": raw_query,
                "limit": limit,
            }
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


def _resolve_log_path(raw: Optional[str]) -> Path:
    if raw:
        return Path(os.path.expanduser(raw)).resolve()
    if DEFAULT_LOG_PATH is not None:
        return DEFAULT_LOG_PATH
    raise ValueError("log path not provided")


def _parse_limit(raw: Optional[str]) -> int:
    if not raw:
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LIMIT
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


def main() -> None:
    global DEFAULT_LIMIT, DEFAULT_LOG_PATH, DEFAULT_MEMORY_DB_PATH
    parser = argparse.ArgumentParser(description="Mash telemetry server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", required=True)
    parser.add_argument("--memory-db")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    memory_db_path: Optional[Path] = None
    search_service: Optional[MemorySearchService] = None

    log_path = Path(os.path.expanduser(args.log)).resolve()
    event_logger = EventLogger(log_path)
    DEFAULT_LOG_PATH = log_path
    if args.memory_db:
        memory_db_path = Path(os.path.expanduser(args.memory_db)).resolve()
        DEFAULT_MEMORY_DB_PATH = memory_db_path
        store = SQLiteStore(memory_db_path)
        search_service = MemorySearchService(
            store,
            event_logger=event_logger,
            retrieval_config=RetrievalConfig(
                enable_keyword=True, enable_semantic=False
            ),
            fusion_weights=FusionWeights(keyword_weight=1.0, semantic_weight=0.0),
        )
    DEFAULT_LIMIT = args.limit

    server = TelemetryHTTPServer((args.host, args.port), TelemetryHandler)
    server.memory_db_path = memory_db_path
    server.search_service = search_service
    print(f"Telemetry server listening on http://{args.host}:{args.port}")
    if log_path is not None:
        print(f"Log file: {log_path}")
    else:
        print("Log file: not set (pass --log or ?path=...)")
    if memory_db_path is not None:
        print(f"Memory DB: {memory_db_path}")
    else:
        print("Memory DB: not set (pass --memory-db to enable /api/search)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
