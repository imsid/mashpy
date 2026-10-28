# Browser

`src/mash/browser` holds per-user browsers: a Chromium the agents acting for
one user share, with a profile that keeps the sites the user signed in to. It
runs in a container of its own, next to the user's sandbox. Agents drive it
through the host; code in the sandbox has no route to it.

An agent gets the browser tools when its spec lists sites
(`AgentSpec.browser_sites()`) and the host has browsers. The host starts
browsers only when some agent lists sites.

## Call paths

```
agent process              host                              browser u-42
browser_navigate ──► POST /browser/call ──► BrowserManager ─► POST /requests/<id>/call
browser_save ──────► POST /browser/save  ─┐  ensure(u-42)       daemon ─► Playwright MCP ─► Chromium
browser_upload ────► POST /browser/upload ┼► SandboxManager       (one MCP session and tab
  (agent token,       request identity    │  (workspace files)     per request)
   request_id)        user = u-42, sites  ┘
                       from the spec

browser_handoff ───► engine: POST /browser/handoff ─► view token ─► interaction "browser"
                                                                    {url, expires_at}
user's browser ────► /s/<view token>/browser/ ──────► BrowserManager ─► /view/ (noVNC) ─► x11vnc
workflow code ─────► ctx.browser() ──────────────────► BrowserManager ─► /cdp ─► Chromium
```

## Main components

- `daemon.py`: `BrowserDaemon`, the app `mash browser serve` runs in each
  browser container. It is Playwright MCP's client, with one MCP session per
  request (`PlaywrightMCPSession`), and the first call of a request opens a tab
  for it, since every session sees every tab. Each call's result is shaped for
  the model (`shape_result`): the snapshot file an action names is read in, the
  generated Playwright code is dropped, downloads the call started are listed
  by id and name, and the page goes between `<web-content>` tags. A URL outside
  the call's sites fails before it runs, and a call that lands outside them
  goes back and returns an error instead of the page. A request's tab and
  session close after 10 minutes without a call. Every route but `/health`
  needs the browser token, on HTTP and websockets.
- `processes.py`: the daemon's children, restarted when they exit: Xvfb at
  1280x800, Chromium headed on it with `/home/browser/profile` and CDP on
  `127.0.0.1:9222`, x11vnc on `127.0.0.1:5900`, and Playwright MCP attached
  over CDP on `127.0.0.1:8931` with its output in `/home/browser/output`.
- `cdp.py`: `ChromiumCDP`, the daemon's own browser-level CDP connection. It
  sends downloads to `/home/browser/downloads/<guid>` and records them from
  CDP's download events, and lists and clears cookies by domain. It reconnects
  when Chromium restarts.
- `catalog.py`: the pinned Playwright MCP version and the tools relayed to
  agents. `playwright_tools.json` holds their schemas, written by
  `scripts/update_playwright_tools.py`; change it, the version, and
  `Dockerfile.browser` together.
- `sites.py`: checks `browser_sites()` entries and matches URLs to them.
- `tokens.py`: `derive_browser_token(secret, user_id)`, the HMAC of
  `mash-browser:<user_id>` under `MASH_HOST_SECRET`, and the view tokens in
  live-view links, which carry a user, a request and an expiry.
- `provider.py`: `DockerBrowserProvider`: container and volume
  `<ns>-browser-<key>` (the sandbox's `<key>`), labels `mash.browser.user` and
  `mash.namespace`, network `<ns>-browsers`, 1 GB of shared memory, restart
  policy `unless-stopped`. Browsers run
  `ghcr.io/imsid/mash-browser:<mashpy version>` (`Dockerfile.browser`), or
  `MASH_BROWSER_IMAGE`. The container gets only its token from the host.
- `manager.py`: `BrowserManager`, the host's one way into a browser, on the
  sandboxes' lifecycle (`mash.sandbox.containers`):
  `MASH_BROWSER_IDLE_SECONDS` (default 900) and `MASH_BROWSER_MAX_RUNNING`
  (default 10). It also keeps the open handoffs: `start_handoff` mints a view
  token valid for `MASH_BROWSER_HANDOFF_SECONDS` (default 900) and
  `end_handoff` revokes it.

`mash.testing.browsers` has `FakeBrowser`, an in-memory stand-in for Chromium
and Playwright MCP that keeps their sharing of tabs, snapshot files and
download events, and `LocalBrowserProvider`, which runs daemons over it on
localhost ports. `tests/mash/browser/test_browser_live.py` runs the daemon
against a real Chromium and Playwright MCP when `MASH_BROWSER_LIVE=1`.
