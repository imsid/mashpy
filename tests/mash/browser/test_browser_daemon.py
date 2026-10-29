"""The browser daemon over a fake Chromium: tabs, sites, snapshots, files."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from mash.browser.daemon import BrowserDaemon
from mash.testing.browsers import FakeBrowser, FakePage

TOKEN = "browser-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SITES = ["https://*.pge.com"]
PAGES = {
    "https://www.pge.com/": FakePage(
        title="PG&E",
        refs={
            "bill": ("goto", "https://www.pge.com/bill"),
            "evil": ("goto", "https://evil.io/steal"),
            "pdf": ("download", "bill-2026-10.pdf", b"%PDF-1.7"),
            "attach": ("chooser",),
        },
    ),
    "https://www.pge.com/bill": FakePage(title="Your bill"),
}


class BrowserDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.browser = FakeBrowser(Path(self._dir.name), PAGES)
        self.daemon = BrowserDaemon(
            token=TOKEN,
            connect=self.browser.connect,
            cdp=self.browser.cdp,
            home=Path(self._dir.name),
            download_wait_seconds=0.0,
        )
        self.http = TestClient(self.daemon.app)
        self.http.__enter__()

    def tearDown(self) -> None:
        self.http.__exit__(None, None, None)
        self._dir.cleanup()

    def call(self, request_id: str, tool: str, sites=SITES, **arguments):
        response = self.http.post(
            f"/requests/{request_id}/call",
            json={"tool": tool, "arguments": arguments, "sites": sites},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_every_route_but_health_needs_the_token(self) -> None:
        self.assertEqual(self.http.get("/health").status_code, 200)
        self.assertEqual(self.http.post("/requests/r/call", json={}).status_code, 401)
        self.assertEqual(self.http.get("/sites", headers={"Authorization": "Bearer x"}).status_code, 401)

    def test_a_requests_first_call_opens_a_tab_of_its_own(self) -> None:
        self.call("r-1", "browser_navigate", url="https://www.pge.com/")
        self.call("r-2", "browser_navigate", url="https://www.pge.com/bill")

        urls = [tab.url for tab in self.browser.tabs]
        # The tab Chromium started with is untouched; each request has its own.
        self.assertEqual(urls, ["about:blank", "https://www.pge.com/", "https://www.pge.com/bill"])

    def test_action_results_carry_the_page_inline_as_web_content(self) -> None:
        result = self.call("r-1", "browser_navigate", url="https://www.pge.com/")

        self.assertFalse(result["is_error"])
        self.assertEqual(result["page_url"], "https://www.pge.com/")
        self.assertTrue(result["content"].startswith("<web-content>\n"))
        self.assertTrue(result["content"].endswith("\n</web-content>"))
        self.assertIn('link "bill" [ref=bill]', result["content"])
        self.assertNotIn("[Snapshot](", result["content"])
        self.assertNotIn("Ran Playwright code", result["content"])

    def test_navigation_outside_the_sites_is_refused_before_it_runs(self) -> None:
        result = self.call("r-1", "browser_navigate", url="https://evil.io/")

        self.assertTrue(result["is_error"])
        self.assertIn("https://evil.io is outside the sites", result["content"])
        self.assertNotIn(("browser_navigate", {"url": "https://evil.io/"}), self.browser.calls)

    def test_a_click_that_leaves_the_sites_goes_back_and_hides_the_page(self) -> None:
        self.call("r-1", "browser_navigate", url="https://www.pge.com/")
        result = self.call("r-1", "browser_click", target="evil", element="link")

        self.assertTrue(result["is_error"])
        self.assertIn("https://evil.io is outside the sites", result["content"])
        self.assertNotIn("steal", result["content"])
        self.assertEqual(self.browser.tabs[-1].url, "https://www.pge.com/")

    def test_unknown_tools_and_bad_sites_are_refused(self) -> None:
        resp = self.http.post(
            "/requests/r/call", json={"tool": "browser_evaluate", "sites": SITES}, headers=AUTH
        )
        self.assertEqual(resp.status_code, 400)
        resp = self.http.post(
            "/requests/r/call",
            json={"tool": "browser_snapshot", "sites": ["not-an-origin"]},
            headers=AUTH,
        )
        self.assertEqual(resp.status_code, 400)

    def test_downloads_are_listed_in_the_result_and_served_by_id(self) -> None:
        self.call("r-1", "browser_navigate", url="https://www.pge.com/")
        result = self.call("r-1", "browser_click", target="pdf", element="Download PDF")

        self.assertIn("download_id: dl-1, file: bill-2026-10.pdf, state: completed", result["content"])
        download = self.http.get("/downloads/dl-1", headers=AUTH)
        self.assertEqual(download.content, b"%PDF-1.7")
        self.assertEqual(download.headers["x-mash-file-name"], "bill-2026-10.pdf")
        self.assertEqual(self.http.get("/downloads/nope", headers=AUTH).status_code, 404)

    def test_upload_opens_the_chooser_and_hands_it_the_file(self) -> None:
        self.call("r-1", "browser_navigate", url="https://www.pge.com/")
        resp = self.http.post(
            "/requests/r-1/upload",
            params={"name": "../meter.csv", "target": "attach", "sites": json.dumps(SITES)},
            content=b"kwh\n12\n",
            headers=AUTH,
        )

        self.assertFalse(resp.json()["is_error"], resp.json())
        [[path]] = self.browser.uploaded
        self.assertEqual(Path(path).read_bytes(), b"kwh\n12\n")
        self.assertEqual(Path(path).parent, Path(self._dir.name) / "uploads" / "r-1")

    def test_release_closes_the_requests_tab(self) -> None:
        self.call("r-1", "browser_navigate", url="https://www.pge.com/")
        self.assertEqual(len(self.browser.tabs), 2)

        self.assertEqual(self.http.delete("/requests/r-1", headers=AUTH).json(), {"closed": True})
        self.assertEqual([t.url for t in self.browser.tabs], ["about:blank"])
        self.assertEqual(self.http.delete("/requests/r-1", headers=AUTH).json(), {"closed": False})

    def test_idle_sessions_close(self) -> None:
        self.call("r-1", "browser_navigate", url="https://www.pge.com/")
        self.daemon._idle_seconds = 0.0
        closed = self.http.portal.call(self.daemon.reap_idle)

        self.assertEqual(closed, ["r-1"])
        self.assertEqual(len(self.browser.tabs), 1)

    def test_screenshots_come_back_as_images(self) -> None:
        self.call("r-1", "browser_navigate", url="https://www.pge.com/")
        result = self.call("r-1", "browser_take_screenshot")

        self.assertEqual(result["images"][0]["media_type"], "image/png")

    def test_sites_lists_and_clears_cookie_domains(self) -> None:
        self.browser.cdp.cookies = {"pge.com": 3}

        self.assertEqual(
            self.http.get("/sites", headers=AUTH).json(), {"sites": [{"domain": "pge.com", "cookies": 3}]}
        )
        self.assertEqual(
            self.http.delete("/sites/pge.com", headers=AUTH).json(), {"domain": "pge.com", "cookies": 3}
        )


if __name__ == "__main__":
    unittest.main()
