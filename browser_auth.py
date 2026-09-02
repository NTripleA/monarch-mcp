"""Browser-based cookie capture for re-seeding a Monarch session.

Monarch puts programmatic password login behind Cloudflare bot protection, so the
reliable way to authenticate is to reuse a browser session. This module captures that
session out-of-band: cookies travel from the browser to a short-lived loopback server
and straight into the session file, never through tool arguments or results.

Two capture paths, in order of preference:

1. Auto-detect -- read monarch.com cookies from the local browser's cookie store.
   Requires the optional `browser` extra; unavailable or locked stores fall through.
2. Paste -- the user copies the Cookie header from DevTools into a local page.

The loopback server binds to 127.0.0.1 on a random port, is guarded by a single-use
token, and is torn down as soon as capture succeeds or times out.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Literal

from aiohttp import web

MONARCH_DOMAINS = ("monarch.com", "app.monarch.com", ".monarch.com")

# Monarch's API rejects a cookie set lacking either of these.
REQUIRED_COOKIES = ("session_id", "csrftoken")

CaptureMethod = Literal["auto-detected", "pasted"]


def cookie_names(cookie_string: str) -> set[str]:
    """Names present in a Cookie header string, ignoring malformed pairs."""
    return {pair.strip().split("=", 1)[0] for pair in cookie_string.split(";") if "=" in pair}


def missing_required_cookies(cookie_string: str) -> list[str]:
    """Which of the required cookies are absent. Empty list means the string is usable."""
    present = cookie_names(cookie_string)
    return [name for name in REQUIRED_COOKIES if name not in present]


def _extract_with_rookiepy() -> dict[str, str]:
    import rookiepy

    records = rookiepy.load(list(MONARCH_DOMAINS))
    return {r["name"]: r["value"] for r in records if "monarch.com" in str(r.get("domain", ""))}


def _extract_with_browser_cookie3() -> dict[str, str]:
    import browser_cookie3

    jar = browser_cookie3.load(domain_name="monarch.com")
    return {cookie.name: cookie.value for cookie in jar if cookie.value}


def extract_cookies_from_browsers() -> str | None:
    """Read Monarch cookies from the local browser cookie store.

    Returns a Cookie header string, or None when the optional dependency is missing,
    no browser holds a usable Monarch session, or the store cannot be read (Chrome's
    Keychain prompt declined, Safari without Full Disk Access, browser holding a lock).
    Never raises -- callers fall back to the paste flow.
    """
    for loader in (_extract_with_rookiepy, _extract_with_browser_cookie3):
        try:
            cookies = loader()
        except Exception:
            continue
        if cookies and not [name for name in REQUIRED_COOKIES if name not in cookies]:
            return "; ".join(f"{name}={value}" for name, value in cookies.items())
    return None


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Monarch Money</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 40rem; margin: 3rem auto; padding: 0 1.5rem; }}
  h1 {{ font-size: 1.4rem; margin-bottom: .25rem; }}
  p.sub {{ opacity: .7; margin-top: 0; }}
  ol {{ padding-left: 1.2rem; }}
  li {{ margin: .4rem 0; }}
  textarea {{ width: 100%; min-height: 7rem; font-family: ui-monospace, monospace;
              font-size: 12px; padding: .6rem; box-sizing: border-box; }}
  button {{ font-size: 15px; padding: .55rem 1.1rem; cursor: pointer; margin-right: .5rem; }}
  .row {{ margin: 1.2rem 0; }}
  .msg {{ padding: .7rem 1rem; border-radius: 6px; margin: 1rem 0; }}
  .err {{ background: #fdd; color: #900; }}
  .ok {{ background: #dfd; color: #060; }}
  @media (prefers-color-scheme: dark) {{
    .err {{ background: #4a1f1f; color: #ffb4b4; }}
    .ok {{ background: #1f4a26; color: #b4ffc4; }}
  }}
  a.btn {{ display: inline-block; }}
</style>
</head>
<body>
<h1>Connect Monarch Money</h1>
<p class="sub">This page runs locally. Nothing you paste here is sent anywhere except your
own Monarch MCP server.</p>

{message}

<div class="row">
  <a class="btn" href="https://app.monarch.com" target="_blank" rel="noopener">
    <button type="button">1. Open Monarch and sign in &rarr;</button>
  </a>
</div>

<div class="row">
  <form method="post" action="/auth/{token}/detect">
    <button type="submit">2. Detect my session automatically</button>
  </form>
</div>

<details>
  <summary>Automatic detection didn't work &mdash; paste it manually</summary>
  <ol>
    <li>In the Monarch tab, open DevTools and go to <strong>Network</strong></li>
    <li>Filter for <code>graphql</code> and click any request to <code>api.monarch.com</code></li>
    <li>Under <strong>Request Headers</strong>, find <code>Cookie</code> (toggle <strong>Raw</strong>)</li>
    <li>Copy the <strong>entire</strong> value and paste it below</li>
  </ol>
  <form method="post" action="/auth/{token}/submit">
    <textarea name="cookies" placeholder="session_id=...; csrftoken=...; cf_clearance=..."></textarea>
    <div class="row"><button type="submit">Connect</button></div>
  </form>
</details>
</body>
</html>
"""

DONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Connected</title>
<style>:root{color-scheme:light dark}body{font:15px/1.6 -apple-system,sans-serif;
max-width:34rem;margin:5rem auto;padding:0 1.5rem;text-align:center}</style></head>
<body><h1>&#10003; Connected</h1>
<p>Your Monarch session has been saved. You can close this tab and return to your
assistant.</p></body></html>
"""


class CookieCaptureServer:
    """One-shot loopback server that collects a Monarch Cookie header.

    Usage:
        server = CookieCaptureServer()
        url = await server.start()
        try:
            cookie_string, method = await server.wait(timeout=300)
        finally:
            await server.stop()
    """

    def __init__(self) -> None:
        self._token = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self._result: asyncio.Future[tuple[str, CaptureMethod]] | None = None
        self.url = ""

    @property
    def token(self) -> str:
        return self._token

    def _authorized(self, request: web.Request) -> bool:
        supplied = request.match_info.get("token", "")
        return secrets.compare_digest(supplied, self._token)

    def _complete(self, cookie_string: str, method: CaptureMethod) -> bool:
        """Resolve the pending capture. Returns False if already used (single-use)."""
        if self._result is None or self._result.done():
            return False
        self._result.set_result((cookie_string, method))
        return True

    def _render(self, message_html: str = "") -> web.Response:
        return web.Response(
            text=PAGE_TEMPLATE.format(token=self._token, message=message_html),
            content_type="text/html",
        )

    async def _handle_page(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPNotFound
        return self._render()

    async def _handle_detect(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPNotFound

        # Reading a cookie store can block on Keychain or file locks.
        cookie_string = await asyncio.to_thread(extract_cookies_from_browsers)
        if cookie_string and self._complete(cookie_string, "auto-detected"):
            return web.Response(text=DONE_PAGE, content_type="text/html")

        return self._render(
            '<div class="msg err">Could not read a Monarch session from your browser. '
            "This is normal if the optional <code>browser</code> extra isn't installed, or if "
            "your browser keeps its cookies encrypted. Use the manual option below.</div>"
        )

    async def _handle_submit(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            raise web.HTTPNotFound

        form = await request.post()
        raw = form.get("cookies", "")
        cookie_string = raw.strip() if isinstance(raw, str) else ""

        if not cookie_string:
            return self._render('<div class="msg err">Paste the Cookie header first.</div>')

        missing = missing_required_cookies(cookie_string)
        if missing:
            return self._render(
                f'<div class="msg err">That header is missing: <code>{", ".join(missing)}</code>. '
                "Copy the <strong>entire</strong> Cookie value &mdash; it should also include "
                "<code>cf_clearance</code>.</div>"
            )

        if not self._complete(cookie_string, "pasted"):
            return self._render('<div class="msg err">This link was already used.</div>')

        return web.Response(text=DONE_PAGE, content_type="text/html")

    async def start(self) -> str:
        """Bind to a random loopback port and return the single-use capture URL."""
        self._result = asyncio.get_running_loop().create_future()

        app = web.Application()
        app.router.add_get("/auth/{token}", self._handle_page)
        app.router.add_post("/auth/{token}/detect", self._handle_detect)
        app.router.add_post("/auth/{token}/submit", self._handle_submit)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()

        port = self._runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}/auth/{self._token}"
        return self.url

    async def wait(self, timeout: float) -> tuple[str, CaptureMethod]:
        """Block until cookies are captured. Raises asyncio.TimeoutError on expiry."""
        if self._result is None:
            raise RuntimeError("wait() called before start()")
        return await asyncio.wait_for(asyncio.shield(self._result), timeout=timeout)

    async def stop(self) -> None:
        if self._result is not None and not self._result.done():
            self._result.cancel()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
