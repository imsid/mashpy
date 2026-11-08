"""A small MCP server over HTTP for tests, run in a background thread.

It answers ``initialize``, ``tools/list`` and ``tools/call`` with JSON. Its one
tool, ``slow_echo``, sleeps for ``seconds`` and echoes ``text``. It counts the
methods it receives, so a test can check how often the client lists tools.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOOLS = [
    {
        "name": "slow_echo",
        "description": "Sleep, then echo the text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "seconds": {"type": "number"},
            },
        },
    }
]


class FakeMCPServer:
    def __init__(self, *, session_id: str | None = None) -> None:
        self.calls: Counter[str] = Counter()
        self.session_id = session_id
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def do_GET(self) -> None:
                # The client's SSE listener; this server has no stream.
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                message = json.loads(self.rfile.read(length) or b"{}")
                method = str(message.get("method") or "")
                server.calls[method] += 1
                if "id" not in message or not method:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                result = server.result(method, message.get("params") or {})
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"], "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if server.session_id:
                    self.send_header("MCP-Session-ID", server.session_id)
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def result(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"protocolVersion": "2025-03-26", "serverInfo": {"name": "fake"}}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            arguments = params.get("arguments") or {}
            time.sleep(float(arguments.get("seconds") or 0))
            return {"content": [{"type": "text", "text": str(arguments.get("text") or "")}]}
        return {}

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/mcp"

    def __enter__(self) -> "FakeMCPServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def unreachable_url() -> str:
    """A URL on a local port nothing listens on."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/mcp"
