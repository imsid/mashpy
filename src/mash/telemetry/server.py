"""Lightweight telemetry server with log tailing via SSE."""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_LIMIT = 2000


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
        self._not_found()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence default http.server logging.
        return

    def _handle_logs(self, parsed) -> None:
        params = parse_qs(parsed.query)
        path = _resolve_log_path(params.get("path", [None])[0])
        limit = _parse_limit(params.get("limit", [None])[0])
        if not path.exists():
            self._send_json({"error": "log file not found"}, status=404)
            return

        events = _read_log(path, limit)
        self._send_json({"events": events, "path": str(path)})

    def _handle_stream(self, parsed) -> None:
        params = parse_qs(parsed.query)
        path = _resolve_log_path(params.get("path", [None])[0])
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

    def _send_json(self, payload: Dict[str, object], status: int = 200) -> None:
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


def _resolve_log_path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def _parse_limit(raw: Optional[str]) -> int:
    if not raw:
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LIMIT
    return max(1, min(value, 20000))


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
    global DEFAULT_LIMIT
    parser = argparse.ArgumentParser(description="Mash telemetry server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    log_path = Path(os.path.expanduser(args.log)).resolve()
    DEFAULT_LIMIT = args.limit

    server = ThreadingHTTPServer((args.host, args.port), TelemetryHandler)
    print(f"Telemetry server listening on http://{args.host}:{args.port}")
    print(f"Log file: {log_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
