"""Browser tokens, site matching, the tool catalog, and result shaping."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from mash.browser.catalog import PLAYWRIGHT_MCP_VERSION, RELAYED_TOOLS, load_catalog
from mash.browser.daemon import shape_result
from mash.browser.processes import BrowserLayout, child_commands
from mash.browser.sites import check_sites, origin_of, url_allowed
from mash.browser.tokens import derive_browser_token, make_view_token, read_view_token

REPO = Path(__file__).resolve().parents[3]


class TokenTests(unittest.TestCase):
    def test_browser_tokens_differ_by_user_and_from_sandbox_tokens(self) -> None:
        from mash.sandbox.tokens import derive_sandbox_token

        self.assertNotEqual(derive_browser_token("s", "u-1"), derive_browser_token("s", "u-2"))
        self.assertNotEqual(derive_browser_token("s", "u-1"), derive_sandbox_token("s", "u-1"))

    def test_view_token_reads_back_until_it_expires(self) -> None:
        token = make_view_token("s", "u@1", "req-1", 2000)

        self.assertEqual(read_view_token("s", token, now=1999), ("u@1", "req-1", 2000))
        self.assertIsNone(read_view_token("s", token, now=2000))

    def test_view_token_from_another_secret_or_tampered_is_refused(self) -> None:
        token = make_view_token("s", "u-1", "req-1", 2000)
        claims, signature = token.split(".")

        self.assertIsNone(read_view_token("other", token, now=0))
        self.assertIsNone(read_view_token("s", f"{claims}x.{signature}", now=0))
        self.assertIsNone(read_view_token("s", "garbage", now=0))


class SiteTests(unittest.TestCase):
    def test_exact_origin(self) -> None:
        sites = ["https://example.com"]
        self.assertTrue(url_allowed("https://example.com/a?b=1", sites))
        self.assertFalse(url_allowed("http://example.com/", sites))
        self.assertFalse(url_allowed("https://www.example.com/", sites))
        self.assertFalse(url_allowed("https://example.com:8443/", sites))

    def test_wildcard_covers_the_domain_and_subdomains_only(self) -> None:
        sites = ["https://*.pge.com"]
        self.assertTrue(url_allowed("https://pge.com/", sites))
        self.assertTrue(url_allowed("https://m.www.pge.com/x", sites))
        self.assertFalse(url_allowed("https://notpge.com/", sites))
        self.assertFalse(url_allowed("https://pge.com.evil.io/", sites))

    def test_star_allows_everything_and_blank_is_always_allowed(self) -> None:
        self.assertTrue(url_allowed("https://anything.io/", ["*"]))
        self.assertTrue(url_allowed("about:blank", ["https://example.com"]))
        self.assertFalse(url_allowed("file:///etc/passwd", ["https://example.com"]))
        self.assertFalse(url_allowed("chrome://settings", ["https://example.com"]))

    def test_entries_must_be_origins(self) -> None:
        self.assertEqual(check_sites(["https://a.com", "*"]), ["https://a.com", "*"])
        for bad in ["a.com", "ftp://a.com", "https://a.com/path", "https://a.*.com", "", 5]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_sites([bad])

    def test_origin_of(self) -> None:
        self.assertEqual(origin_of("https://a.com:8443/x?y"), "https://a.com:8443")


class CatalogTests(unittest.TestCase):
    def test_catalog_is_the_pinned_version_and_holds_every_relayed_tool(self) -> None:
        catalog = load_catalog()
        self.assertEqual(catalog["version"], PLAYWRIGHT_MCP_VERSION)
        self.assertEqual([t["name"] for t in catalog["tools"]], list(RELAYED_TOOLS))
        for tool in catalog["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_the_image_pins_the_same_version(self) -> None:
        dockerfile = (REPO / "Dockerfile.browser").read_text()
        pinned = re.search(r"ARG PLAYWRIGHT_MCP_VERSION=(\S+)", dockerfile)
        self.assertEqual(pinned.group(1), PLAYWRIGHT_MCP_VERSION)

    def test_page_javascript_and_raw_uploads_are_not_relayed(self) -> None:
        for name in ("browser_evaluate", "browser_run_code_unsafe", "browser_file_upload"):
            self.assertNotIn(name, RELAYED_TOOLS)


class ShapeResultTests(unittest.TestCase):
    def test_snapshot_files_are_inlined_and_generated_code_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "output").mkdir()
            (Path(home) / "output" / "page-1.yml").write_text('- button "Pay" [ref=e4]\n')
            raw = {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "### Ran Playwright code\n```js\nawait page.goto('x');\n```\n"
                            "### Page\n- Page URL: https://a.com/bill\n- Page Title: Bill\n"
                            "### Snapshot\n- [Snapshot](output/page-1.yml)\n"
                        ),
                    },
                    {"type": "image", "data": "AAAA", "mimeType": "image/jpeg"},
                ],
                "isError": False,
            }

            result = shape_result(raw, output_root=Path(home))

        self.assertNotIn("Ran Playwright code", result.content)
        self.assertIn('```yaml\n- button "Pay" [ref=e4]\n```', result.content)
        self.assertEqual(result.page_url, "https://a.com/bill")
        self.assertEqual(result.images, [{"media_type": "image/jpeg", "data": "AAAA"}])

    def test_snapshot_links_outside_the_output_folder_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            raw = {"content": [{"type": "text", "text": "- [Snapshot](../../etc/x.yml)"}]}
            result = shape_result(raw, output_root=Path(home))
        self.assertEqual(result.content, "- [Snapshot](../../etc/x.yml)")


class ProcessTests(unittest.TestCase):
    def test_children_listen_on_loopback_and_mcp_attaches_to_chromium(self) -> None:
        commands = child_commands(BrowserLayout(home=Path("/home/browser")), chromium="/c")

        self.assertEqual(list(commands), ["xvfb", "chromium", "x11vnc", "playwright-mcp"])
        self.assertIn("--user-data-dir=/home/browser/profile", commands["chromium"])
        self.assertIn("--remote-debugging-address=127.0.0.1", commands["chromium"])
        self.assertIn("-localhost", commands["x11vnc"])
        mcp = commands["playwright-mcp"]
        self.assertEqual(mcp[mcp.index("--cdp-endpoint") + 1], "http://127.0.0.1:9222")
        self.assertNotIn("--host", mcp)


if __name__ == "__main__":
    unittest.main()
