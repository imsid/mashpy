"""Smoke test of the browser image: the container, the host's provider, the live view.

Builds nothing; run it against an image that is already built:

    docker build -f Dockerfile.browser -t mash-browser:dev .
    uv run --extra browser python scripts/smoke_browser_image.py mash-browser:dev

Needs Docker, network access (it browses example.com), and an installed Chrome
for the live-view check (``MASH_SMOKE_VIEWER_CHANNEL``, default ``chrome``).
Exits 1 when any check fails. CI runs it when the image's inputs change
(``.github/workflows/browser-image.yml``), and the release runs it before
publishing the image.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import uvicorn
import websockets
from fastapi import FastAPI
from playwright.async_api import async_playwright

from mash.api.routes.browser import build_browser_link_router, view_url
from mash.browser.manager import BrowserManager
from mash.browser.provider import DockerBrowserProvider, browser_name

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "mash-browser:dev"
VIEWER_CHANNEL = os.environ.get("MASH_SMOKE_VIEWER_CHANNEL", "chrome")
NAME = "mash-browser-smoke"
PORT = 19000
TOKEN = "smoke-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = f"http://127.0.0.1:{PORT}"
SITE = "http://127.0.0.1:8765"
SITES = [SITE]
NAMESPACE = "mashsmoke"
# A test site, served inside the container next to Chromium. The "Leave" link
# goes to another origin, localhost, on the same server.
PAGES = {
    "index.html": (
        "<title>Bills</title><h1>Your bills</h1>"
        "<script>document.cookie='session=abc; max-age=86400; path=/'</script>"
        '<a href="bill.pdf" download>Download October bill</a> '
        '<a href="http://localhost:8765/away.html">Leave</a> '
        '<label>Meter file <input type="file" id="meter"></label>'
    ),
    "bill.pdf": "%PDF-1.7 october",
    "away.html": "<title>Away</title>",
}
LIST_PROCESSES = (
    "import os,pwd\n"
    "for p in os.listdir('/proc'):\n"
    "  if p.isdigit():\n"
    "    try: print(pwd.getpwuid(os.stat('/proc/'+p).st_uid).pw_name, open('/proc/'+p+'/comm').read().strip())\n"
    "    except Exception: pass\n"
)

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail and not ok else ""), flush=True)


def sh(*args: str, check_rc: bool = True) -> str:
    out = subprocess.run(args, capture_output=True, text=True)
    if check_rc and out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {out.stderr}")
    return out.stdout


def ref(snapshot: str, label: str) -> str:
    for line in snapshot.splitlines():
        if label in line and "[ref=" in line:
            return line.split("[ref=")[1].split("]")[0]
    raise AssertionError(f"no ref for {label!r} in the snapshot")


async def wait_health(base: str, timeout: float = 180) -> float:
    start = time.monotonic()
    async with httpx.AsyncClient() as client:
        while time.monotonic() - start < timeout:
            with contextlib.suppress(httpx.HTTPError):
                if (await client.get(f"{base}/health")).status_code == 200:
                    return time.monotonic() - start
            await asyncio.sleep(0.5)
    raise RuntimeError("the daemon never became healthy")


async def container_stage() -> None:
    """The image on its own: processes, every daemon route, the view socket, CDP."""
    sh("docker", "rm", "-f", NAME, check_rc=False)
    sh(
        "docker", "run", "-d", "--name", NAME, "--shm-size=1g",
        "-e", f"MASH_BROWSER_TOKEN={TOKEN}", "-p", f"127.0.0.1:{PORT}:9000", IMAGE,
    )
    took = await wait_health(BASE)
    check("daemon healthy", True, f"{took:.1f}s")
    write_site = "; ".join(f"open('/tmp/site/{n}','w').write({json.dumps(b)})" for n, b in PAGES.items())
    sh("docker", "exec", NAME, "python3", "-c", f"import os; os.makedirs('/tmp/site', exist_ok=True); {write_site}")
    sh("docker", "exec", "-d", NAME, "python3", "-m", "http.server", "8765", "--directory", "/tmp/site", "--bind", "127.0.0.1")
    await asyncio.sleep(1)

    procs = sh("docker", "exec", NAME, "python3", "-c", LIST_PROCESSES)
    for proc in ("Xvfb", "chrome", "x11vnc", "node"):
        check(f"process {proc} running", proc in procs)
    users = {line.split()[0] for line in procs.splitlines() if line.strip()}
    check("processes run as browser, not root", users == {"browser"}, str(users))

    async with httpx.AsyncClient() as anonymous:
        check("no token is refused", (await anonymous.get(f"{BASE}/sites")).status_code == 401)

    async with httpx.AsyncClient(base_url=BASE, headers=AUTH, timeout=120) as http:
        async def call(request_id: str, tool: str, **arguments):
            response = await http.post(
                f"/requests/{request_id}/call",
                json={"tool": tool, "arguments": arguments, "sites": SITES},
            )
            return response.json()

        # No retry: /health promises the first call works.
        page = await call("r-1", "browser_navigate", url=f"{SITE}/index.html")
        check("navigate", not page.get("is_error"), str(page.get("content"))[:300])
        check("snapshot inlined", "```yaml" in page["content"] and "Download October bill" in page["content"])
        check("page wrapped as web content", page["content"].startswith("<web-content>"))

        clicked = await call("r-1", "browser_click", target=ref(page["content"], "Download October bill"), element="Download")
        downloaded = "file: bill.pdf, state: completed" in clicked["content"]
        check("download recorded", downloaded, clicked["content"][-300:])
        if downloaded:
            download_id = clicked["content"].split("download_id: ")[1].split(",")[0]
            check("download served", (await http.get(f"/downloads/{download_id}")).content == b"%PDF-1.7 october")

        uploaded = await http.post(
            "/requests/r-1/upload",
            params={
                "name": "meter.csv", "target": ref(page["content"], "Meter file"),
                "element": "Meter file", "sites": json.dumps(SITES),
            },
            content=b"kwh\n9\n",
        )
        check("upload", not uploaded.json()["is_error"], uploaded.json()["content"][:300])

        left = await call("r-1", "browser_click", target=ref(page["content"], "Leave"), element="Leave")
        check("leaving the sites is refused", left["is_error"] and "outside the sites" in left["content"], left["content"][:200])
        back = await call("r-1", "browser_snapshot")
        check("went back to the allowed page", back.get("page_url") == f"{SITE}/index.html", str(back.get("page_url")))

        shot = await call("r-1", "browser_take_screenshot")
        check("screenshot image", bool(shot.get("images")))

        other = await call("r-2", "browser_navigate", url=f"{SITE}/away.html")
        mine = await call("r-1", "browser_snapshot")
        check(
            "requests keep their own tabs",
            mine.get("page_url") == f"{SITE}/index.html" and other.get("page_url") == f"{SITE}/away.html",
        )

        sites = (await http.get("/sites")).json()
        check("cookie domains listed", any(s["domain"] == "127.0.0.1" for s in sites["sites"]), json.dumps(sites))

        vnc = await http.get("/view/vnc.html")
        check("noVNC client served", vnc.status_code == 200 and "noVNC" in vnc.text, str(vnc.status_code))

    async with websockets.connect(
        f"ws://127.0.0.1:{PORT}/view/websockify", additional_headers=AUTH, subprotocols=["binary"]
    ) as ws:
        banner = await asyncio.wait_for(ws.recv(), 10)
        check("VNC handshake through the view socket", bytes(banner).startswith(b"RFB 003."), repr(banner))

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"ws://127.0.0.1:{PORT}/cdp", headers=AUTH)
        context = browser.contexts[0]
        page = await context.new_page()
        await page.goto(f"{SITE}/index.html")
        user_agent = await page.evaluate("navigator.userAgent")
        cookies = await context.cookies()
        await page.close()
        await browser.close()
    check("CDP bridge drives the same profile", any(c["name"] == "session" for c in cookies))
    check("Chromium runs headed", "Headless" not in user_agent, user_agent)

    logs = sh("docker", "logs", NAME, check_rc=False)
    restarts = [line for line in logs.splitlines() if "restarting" in line]
    check("no child restarts", not restarts, "\n".join(restarts)[:400])


async def provider_stage() -> None:
    """The host's provider and manager: start, browse, persist, sign out, delete."""
    provider = DockerBrowserProvider(host_secret="smoke-secret", image=IMAGE, namespace=NAMESPACE, in_container=False)
    manager = BrowserManager(provider, host_secret="smoke-secret")
    user = "smoke@user"
    name = browser_name(user, NAMESPACE)
    example = ["https://example.com"]
    try:
        endpoint = await manager.endpoint(user)
        check("provider starts the user's browser", True)
        result = await manager.call(
            user, request_id="p-1", tool="browser_navigate",
            arguments={"url": "https://example.com/"}, sites=example,
        )
        check("browses the internet", "Example Domain" in result["content"], result["content"][:300])
        # Stands in for a site signing the user in.
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(
                endpoint.base_url.replace("http", "ws", 1) + "/cdp",
                headers={"Authorization": f"Bearer {endpoint.token}"},
            )
            await browser.contexts[0].add_cookies(
                [{"name": "signed_in", "value": "yes", "domain": "example.com", "path": "/", "expires": time.time() + 86400}]
            )
            await browser.close()
        await manager.stop(user)
        check("stop stops the container", json.loads(sh("docker", "inspect", name))[0]["State"]["Running"] is False)
        sites = await manager.sites(user)
        check("profile survives a restart", any(s["domain"] == "example.com" for s in sites), json.dumps(sites))
        cleared = await manager.clear_site(user, "example.com")
        sites = await manager.sites(user)
        check(
            "clearing a site signs out of it",
            cleared["cookies"] >= 1 and not any(s["domain"] == "example.com" for s in sites),
        )
        networks = json.loads(sh("docker", "inspect", name))[0]["NetworkSettings"]["Networks"]
        check("on the browsers network only", list(networks) == [f"{NAMESPACE}-browsers"], str(list(networks)))
    finally:
        await manager.delete_profile(user)
        await provider.close()
    check("profile deleted", sh("docker", "volume", "ls", "-q", "--filter", f"name={name}").strip() == "")


async def view_stage() -> None:
    """A person opens a sign-in link in their own browser and sees the container's."""
    provider = DockerBrowserProvider(host_secret="smoke-secret", image=IMAGE, namespace=NAMESPACE, in_container=False)
    browsers = BrowserManager(provider, host_secret="smoke-secret")
    app = FastAPI()
    app.include_router(build_browser_link_router())
    app.state.runtime_state = SimpleNamespace(
        pool=SimpleNamespace(get_browsers=lambda: browsers),
        host_secret="smoke-secret",
        public_url="http://127.0.0.1:18080",
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=18080, log_level="warning"))
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
    serving = asyncio.create_task(server.serve())
    user = "view@user"
    try:
        await browsers.call(
            user, request_id="r-1", tool="browser_navigate",
            arguments={"url": "https://example.com/"}, sites=["https://example.com"],
        )
        handoff = browsers.start_handoff(user, "r-1")
        await browsers.front(user, "r-1")
        url = view_url("http://127.0.0.1:18080", handoff.token)
        async with async_playwright() as pw:
            viewer = await pw.chromium.launch(channel=VIEWER_CHANNEL)
            page = await viewer.new_page(viewport={"width": 1300, "height": 860})
            await page.goto(url)
            status = ""
            for _ in range(40):
                status = await page.evaluate("document.querySelector('#noVNC_status')?.textContent || ''")
                if status.startswith("Connected"):
                    break
                await asyncio.sleep(0.5)
            check("the live view connects", status.startswith("Connected"), status)
            await viewer.close()
        browsers.end_handoff("r-1")
        async with httpx.AsyncClient() as http:
            ended = await http.get(url.replace("/browser/", "/browser/vnc.html"))
        check("the link stops working when the handoff ends", ended.status_code == 404, str(ended.status_code))
    finally:
        server.should_exit = True
        await serving
        await browsers.delete_profile(user)
        await provider.close()


async def main() -> None:
    try:
        await container_stage()
    finally:
        sh("docker", "rm", "-f", NAME, check_rc=False)
    try:
        await provider_stage()
        await view_stage()
    finally:
        sh("docker", "network", "rm", f"{NAMESPACE}-browsers", check_rc=False)
    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
