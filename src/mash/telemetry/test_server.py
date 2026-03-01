"""Tests for telemetry server v1 API and optional UI behavior."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from mash.telemetry.server import TelemetryHTTPServer, TelemetryHandler
from mash.telemetry.ui_loader import UIResolution


@contextmanager
def running_server(
    *,
    log_path: Path,
    ui_resolution: UIResolution,
    memory_db_path: Optional[Path] = None,
) -> Iterator[tuple[str, TelemetryHTTPServer]]:
    server = TelemetryHTTPServer(("127.0.0.1", 0), TelemetryHandler)
    server.log_path = log_path
    server.default_limit = 2000
    server.search_service = None
    server.memory_db_path = memory_db_path
    server.ui_resolution = ui_resolution

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    try:
        yield base_url, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def fetch_json(base_url: str, path: str) -> tuple[int, dict]:
    url = f"{base_url}{path}"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TelemetryServerAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.log_path = self.tmp_path / "events.jsonl"
        self.log_path.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_health_route_reports_runtime_state(self) -> None:
        with running_server(
            log_path=self.log_path,
            ui_resolution=UIResolution(
                mode="off",
                enabled=False,
                available=False,
                static_dir=None,
                reason="disabled by --ui off",
            ),
        ) as (base_url, _server):
            status, payload = fetch_json(base_url, "/api/v1/health")

        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["api_version"], "v1")
        self.assertTrue(data["log"]["exists"])
        self.assertFalse(data["memory"]["search_available"])

    def test_logs_route_returns_structured_snapshot(self) -> None:
        self.log_path.write_text(
            "\n".join(
                [
                    json.dumps({"event_type": "ok.one", "session_id": "s1"}),
                    "not-json",
                    json.dumps({"event_type": "ok.two", "session_id": "s1"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with running_server(
            log_path=self.log_path,
            ui_resolution=UIResolution(
                mode="off",
                enabled=False,
                available=False,
                static_dir=None,
            ),
        ) as (base_url, _server):
            status, payload = fetch_json(base_url, "/api/v1/logs?limit=10")

        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(data["path"], str(self.log_path))
        self.assertEqual(len(data["events"]), 2)
        self.assertEqual(data["events"][0]["event_type"], "ok.one")
        self.assertEqual(data["events"][1]["event_type"], "ok.two")

    def test_logs_route_missing_file_returns_404_envelope(self) -> None:
        missing_path = self.tmp_path / "missing.jsonl"
        with running_server(
            log_path=missing_path,
            ui_resolution=UIResolution(
                mode="off",
                enabled=False,
                available=False,
                static_dir=None,
            ),
        ) as (base_url, _server):
            status, payload = fetch_json(base_url, "/api/v1/logs")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "LOG_FILE_NOT_FOUND")

    def test_search_missing_parameters_returns_400_envelope(self) -> None:
        with running_server(
            log_path=self.log_path,
            ui_resolution=UIResolution(
                mode="off",
                enabled=False,
                available=False,
                static_dir=None,
            ),
        ) as (base_url, _server):
            status_missing_q, payload_missing_q = fetch_json(base_url, "/api/v1/search?app_id=demo")
            status_missing_app, payload_missing_app = fetch_json(base_url, "/api/v1/search?q=hello")

        self.assertEqual(status_missing_q, 400)
        self.assertEqual(payload_missing_q["error"]["code"], "MISSING_QUERY")
        self.assertEqual(status_missing_app, 400)
        self.assertEqual(payload_missing_app["error"]["code"], "MISSING_APP_ID")

    def test_search_returns_503_when_memory_db_not_configured(self) -> None:
        with running_server(
            log_path=self.log_path,
            ui_resolution=UIResolution(
                mode="off",
                enabled=False,
                available=False,
                static_dir=None,
            ),
        ) as (base_url, _server):
            status, payload = fetch_json(base_url, "/api/v1/search?q=hello&app_id=demo")

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "MEMORY_SEARCH_UNAVAILABLE")

    def test_stream_route_sends_only_valid_jsonl_lines(self) -> None:
        with running_server(
            log_path=self.log_path,
            ui_resolution=UIResolution(
                mode="off",
                enabled=False,
                available=False,
                static_dir=None,
            ),
        ) as (base_url, _server):
            response = urllib.request.urlopen(f"{base_url}/api/v1/stream", timeout=5)
            try:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Content-Type"), "text/event-stream")

                def delayed_append() -> None:
                    # Let the server open and seek to EOF before appending.
                    time.sleep(0.3)
                    with self.log_path.open("a", encoding="utf-8") as handle:
                        handle.write("not-json\n")
                        handle.write(json.dumps({"event_type": "stream.ok"}) + "\n")
                        handle.flush()

                writer = threading.Thread(target=delayed_append, daemon=True)
                writer.start()

                deadline = time.time() + 5
                streamed_payload = None
                while time.time() < deadline:
                    line = response.readline().decode("utf-8")
                    if line.startswith("data: "):
                        streamed_payload = line[len("data: ") :].strip()
                        break

                self.assertIsNotNone(streamed_payload)
                parsed = json.loads(streamed_payload)
                self.assertEqual(parsed["event_type"], "stream.ok")
                writer.join(timeout=1)
            except socket.timeout as exc:  # pragma: no cover - defensive timeout guard
                self.fail(f"Timed out waiting for SSE data: {exc}")
            finally:
                response.close()


class TelemetryServerUITests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.log_path = self.tmp_path / "events.jsonl"
        self.log_path.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_root_returns_api_landing_when_ui_disabled(self) -> None:
        with running_server(
            log_path=self.log_path,
            ui_resolution=UIResolution(
                mode="off",
                enabled=False,
                available=False,
                static_dir=None,
                reason="disabled",
            ),
        ) as (base_url, _server):
            status, payload = fetch_json(base_url, "/")

        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "mash.telemetry")
        self.assertEqual(payload["mode"], "api-only")

    def test_root_and_assets_are_served_when_ui_enabled(self) -> None:
        static_dir = self.tmp_path / "static"
        assets_dir = static_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (static_dir / "index.html").write_text("<html><body>Telemetry UI</body></html>", encoding="utf-8")
        (assets_dir / "app.js").write_text("console.log('ok');", encoding="utf-8")

        with running_server(
            log_path=self.log_path,
            ui_resolution=UIResolution(
                mode="on",
                enabled=True,
                available=True,
                static_dir=static_dir,
            ),
        ) as (base_url, _server):
            with urllib.request.urlopen(f"{base_url}/", timeout=3) as response:
                self.assertEqual(response.status, 200)
                body = response.read().decode("utf-8")
                self.assertIn("Telemetry UI", body)

            with urllib.request.urlopen(f"{base_url}/assets/app.js", timeout=3) as response:
                self.assertEqual(response.status, 200)
                body = response.read().decode("utf-8")
                self.assertIn("console.log('ok')", body)

            with urllib.request.urlopen(f"{base_url}/trace/abc", timeout=3) as response:
                self.assertEqual(response.status, 200)
                body = response.read().decode("utf-8")
                self.assertIn("Telemetry UI", body)


if __name__ == "__main__":
    unittest.main()
