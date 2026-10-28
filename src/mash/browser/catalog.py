"""The Playwright MCP tools agents get, and their schemas.

The browser image runs ``@playwright/mcp`` at ``PLAYWRIGHT_MCP_VERSION``. The
agent process registers its tools before any browser runs, so mashpy ships
their schemas in ``playwright_tools.json``, written from that version's
``tools/list`` by ``scripts/update_playwright_tools.py``. Change the version
here, in ``Dockerfile.browser``, and in that file together.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

PLAYWRIGHT_MCP_VERSION = "0.0.83"

# Relayed to agents under their own names. Left out: page JavaScript
# (browser_evaluate, browser_run_code_unsafe), browser_file_upload (Mash's
# browser_upload replaces it), browser_close and browser_resize (the daemon
# owns the session and the display), console and network inspection, and raw
# mouse buttons and drags.
RELAYED_TOOLS = (
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_find",
    "browser_click",
    "browser_hover",
    "browser_type",
    "browser_fill_form",
    "browser_select_option",
    "browser_press_key",
    "browser_handle_dialog",
    "browser_wait_for",
    "browser_tabs",
    "browser_take_screenshot",
    "browser_mouse_click_xy",
    "browser_mouse_wheel",
)


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    """``{"version": ..., "tools": [{name, description, inputSchema}, ...]}``."""
    return json.loads(files("mash.browser").joinpath("playwright_tools.json").read_text())


def relayed_tool_schemas() -> list[dict[str, Any]]:
    """The relayed tools' schemas, in ``RELAYED_TOOLS`` order."""
    by_name = {tool["name"]: tool for tool in load_catalog()["tools"]}
    return [by_name[name] for name in RELAYED_TOOLS]


__all__ = [
    "PLAYWRIGHT_MCP_VERSION",
    "RELAYED_TOOLS",
    "load_catalog",
    "relayed_tool_schemas",
]
