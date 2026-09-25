#!/usr/bin/env python3
"""puppeteer_scraper.py — YouTube comments, video metadata and video search.

The pyppeteer twin of `playwright_scraper.py`. Everything from
`_prime_session` downwards is byte-identical to that file on purpose:
CLAUDE.md §6 requires all three engines to agree on exit codes, run status,
and whether a run crashes or spends money, and the only way to keep that
true is for the shared half to BE the same text. What differs is confined
to the driver layer below — the imports, `_BrowserSession`, the two launch
paths, and the driver's lifetime.

    python3 puppeteer_scraper.py --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --pages 3
    python3 puppeteer_scraper.py --url dQw4w9WgXcQ --mode video

Two things are genuinely different here, and both are stated rather than
discovered:

* **pyppeteer has no request API.** Playwright hands its context an
  `APIRequestContext` that shares the browser's cookies and proxy; here the
  InnerTube call is made by a `fetch` INSIDE the page, which shares them
  for the same reason — it runs on the site's own origin. That JavaScript
  lives in this file and never crosses into a shared module, because the
  three drivers spell it three ways (CLAUDE.md §1).
* **pyppeteer cannot authenticate a proxy on the command line.** The
  credentials are stripped out of `--proxy-server` and applied through
  `page.authenticate`, which is the only place they belong: a
  `--proxy-server=user:pass@host` is part of the browser's argv and
  readable by anything that can run `ps` (CLAUDE.md §8).

pyppeteer is effectively unmaintained and its own README points at
Playwright. It is kept for parity, and it is demoted in priority rather
than in correctness.

The run itself lives in `run_core.py` and is shared with the other
two engines and with `youtube_scraper.py`. What stays here is what
touches pyppeteer: launching, connecting, the fingerprint, and the
session object. Measured on 2026-09-25, before the split: 24 of the
29 definitions the three engines shared were byte-identical.

The driver library is imported at MODULE level on purpose
(CLAUDE.md §10): the suite's skip and CI's `python -c "import
puppeteer_scraper"` both depend on this module failing to import when
pyppeteer is absent.
"""

import logging
import sys
from typing import Any, Dict, List, Optional, Tuple
from product_parser import (CLIENT_VERSION_URL, DEFAULT_LOCALE, DEFAULT_REGION,
                            DEFAULT_SORT, FALLBACK_CLIENT_VERSION, SORTS,
                            canonical_video_url, client_version_from_text,
                            continuation_body, innertube_headers,
                            innertube_url, is_supported_url, parse_comments,
                            parse_search, parse_video, player_body,
                            search_body, video_body, video_id_from_url)
from proxy_pool import (from_args as proxy_pool_from_args, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import asyncio
import concurrent.futures
from pyppeteer import launch, connect
from pyppeteer.errors import PyppeteerError, NetworkError
from pyppeteer.errors import TimeoutError as PPTimeout
import json
import threading
import time

import run_core
from run_core import (  # noqa: F401 — re-exported, see run_core.py
    MODES, DEFAULT_MODE, PLAYER_ONLY_FIELDS, COMMENTS_PER_PAGE,
    REPLIES_PER_PAGE,
    PageOutcome, _TransportError, _call, _chrome_ua, _dump,
    _fetch_one_video, _fetch_videos_concurrently, _fetch_with_policy,
    _handle_captcha_in_browser, _mask_credentials, _open_http,
    _open_session, _prime_session, _proxy_failure, _rotate_if_per_page,
    _run_comments, _run_replies, _run_search, _run_video, _video_ids,
    _worker_pool, handle_captcha_if_present, parse_args)

logger = logging.getLogger("puppeteer_scraper")
DriverError = (PyppeteerError, NetworkError, PPTimeout,
               concurrent.futures.TimeoutError, TimeoutError)
REQUEST_TIMEOUT_MS = 30_000
NAVIGATION_TIMEOUT_MS = 60_000
CDP_CONNECT_TIMEOUT = 30
_FETCH_JS = """
async (spec) => {
  const init = {method: spec.method, headers: spec.headers,
                credentials: 'include'};
  if (spec.body) { init.body = spec.body; }
  const response = await fetch(spec.url, init);
  return {status: response.status, text: await response.text()};
}
"""


class _Loop:
    """One event loop on a background thread, with enforced timeouts.

    `last_error` is class-level on purpose: the exception that explains a
    failed connect arrives on the loop's exception handler rather than on
    the awaited coroutine, so the two have to meet somewhere.

    The shared policy in `page_flow` is written against plain synchronous
    callables, which is the right shape for two of the three drivers.
    Bridging here keeps that policy in one place rather than growing an
    async copy of it that would drift.

    The second benefit is what this family's rules require: every call gets
    an explicit timeout. `.result(timeout)` returns control even when the
    browser never answers, which pyppeteer's own API does not offer.
    """

    last_error = None

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH keys, not one or the other: asyncio puts its own words in
        # `message` and the library's in `exception`, and an `or` between
        # them looks at the exception and never sees the message — which is
        # why these kept printing after they were "handled". Only teardown
        # noise is swallowed; anything else still reaches the default
        # handler, because silencing the loop wholesale would hide real
        # faults under a successful-looking run.
        message = " | ".join(str(context.get(k)) for k in
                             ("exception", "message") if context.get(k))
        # Remembered, not just filtered. When the CONNECT fails, the real
        # reason lands here in a task nobody awaits, while the caller sits
        # on a coroutine that never returns — so the run would report a
        # 90-second timeout for something the service said instantly.
        exc = context.get("exception")
        if exc is not None:
            _Loop.last_error = f"{type(exc).__name__}: {exc}"
        if any(m in message for m in (
                "Target closed", "Connection closed", "No session with given id",
                "Task was destroyed but it is pending",
                # asyncio uses BOTH spellings and they are not
                # interchangeable: a dead connect surfaces as "Task
                # exception was never retrieved", and a filter carrying
                # only the "Future" wording printed a full traceback under
                # an error the engine had already handled.
                "Future exception was never retrieved",
                "Task exception was never retrieved",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, awaitable, timeout: Optional[float] = 60.0):
        """Run any AWAITABLE on the loop, not only a coroutine.

        `asyncio.run_coroutine_threadsafe` requires a coroutine and rejects
        anything else with "A coroutine object is required" — and pyppeteer
        is not consistent about which it hands back: `page.goto` returns a
        coroutine while `CDPSession.send` returns a Future. That difference
        cost a real bug: the fingerprint's client hints reported as failed
        while the command had in fact been dispatched, so the identity was
        applied HALF and the log said it had not been applied at all. Both
        halves of that are worse than either.
        """
        if asyncio.iscoroutine(awaitable):
            coro = awaitable
        else:
            async def _await(value):
                return await value
            coro = _await(awaitable)
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping it outright leaves pyppeteer's websocket reader and
        keepalive pending, and asyncio then prints "Task was destroyed but
        it is pending!" with a traceback for each — AFTER the output has
        been written. Four tracebacks under a successful run is how a
        reader learns to ignore the log.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            self.loop.stop()
        try:
            self.loop.call_soon_threadsafe(_cancel_and_stop)
            self._thread.join(timeout=5)
        except RuntimeError:
            pass

class _BrowserSession:
    """A browser, a page on youtube.com, and a fetch primitive bound to it.

    A rotation is a FRESH BROWSER (CLAUDE.md §8): cookies a bot manager
    issued against exit A and replayed from exit B are a stronger signal
    than either address alone. So this object is torn down and rebuilt
    rather than having its proxy swapped underneath it.
    """

    # The tuple this engine's library raises. `run_core` reads it off
    # the session, so the shared loop never names a driver.
    # Named so the sidecar can report which transport ran.
    transport = "browser"

    errors = DriverError

    def __init__(self, loop, browser, page, proxy_url: Optional[str],
                 client_version: str, user_agent: Optional[str],
                 owns_browser: bool = True):
        self.loop = loop
        self.browser = browser
        self.context = browser
        self.page = page
        self.proxy_url = proxy_url
        self.client_version = client_version
        self.user_agent = user_agent
        # False when we CONNECTED to somebody else's browser rather than
        # launching one. It decides how this session ends, and getting it
        # wrong is not cosmetic — see `close`.
        self.owns_browser = owns_browser
        self._url = ""

    # -- transport ---------------------------------------------------------

    def _fetch(self, url: str, method: str = "GET",
               headers: Optional[Dict[str, str]] = None,
               body: Optional[str] = None):
        spec = {"url": url, "method": method, "headers": headers or {},
                "body": body}
        try:
            return self.loop.run(self.page.evaluate(_FETCH_JS, spec),
                                 timeout=REQUEST_TIMEOUT_MS / 1000.0)
        except Exception as exc:                       # noqa: BLE001
            raise _TransportError(_mask_credentials(exc)) from exc

    def get_text(self, url: str) -> Tuple[Optional[int], Optional[str]]:
        result = self._fetch(url) or {}
        return result.get("status"), result.get("text")

    def post_json(self, url: str, headers: Dict[str, str],
                  body: Dict[str, Any]) -> Tuple[Optional[int], Any]:
        result = self._fetch(url, "POST", headers,
                             json.dumps(body)) or {}
        status, text = result.get("status"), result.get("text")
        try:
            return status, json.loads(text) if text else None
        except (TypeError, ValueError):
            # A refusal is not JSON. Hand the body back as text so the
            # classifier can name it rather than the run dying on a decode.
            return status, text

    def goto(self, url: str) -> Optional[int]:
        response = self.loop.run(
            self.page.goto(url, {"waitUntil": "domcontentloaded",
                                 "timeout": NAVIGATION_TIMEOUT_MS}),
            timeout=NAVIGATION_TIMEOUT_MS / 1000.0 + 5)
        self._url = url
        return getattr(response, "status", None)

    def content(self) -> str:
        try:
            return self.loop.run(self.page.content(), timeout=30) or ""
        except Exception:                              # noqa: BLE001
            return ""

    def count_selector(self, selector: str) -> int:
        try:
            found = self.loop.run(self.page.querySelectorAll(selector),
                                  timeout=30)
            return len(found or [])
        except Exception:                              # noqa: BLE001
            return 0

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run a function EXPRESSION in the page. See `_FETCH_JS` above."""
        try:
            coro = (self.page.evaluate(js, arg) if arg is not None
                    else self.page.evaluate(js))
            return self.loop.run(coro, timeout=30)
        except Exception:                              # noqa: BLE001
            return None

    @property
    def url(self) -> str:
        try:
            return self.page.url or self._url
        except Exception:
            return self._url

    def close(self):
        """End the session — and over CDP, end OURS rather than theirs.

        `Browser.close()` in pyppeteer sends `Browser.close` over the
        protocol, which tells the browser on the other end to shut down.
        That is right for a Chromium this process launched and WRONG for a
        Scraping Browser profile we merely connected to: it ends a remote
        session somebody is paying for, and the next run against the same
        `pid` meets whatever state that left behind. `disconnect()` closes
        our WebSocket and leaves the browser alone.

        Playwright's `connect_over_cdp` disconnects on `close()` by
        definition, so its engine needs no equivalent — but it must not
        close a CONTEXT it adopted rather than created, which is the same
        mistake one level down.
        """
        try:
            if self.owns_browser:
                self.loop.run(self.browser.close(), timeout=20)
            else:
                self.loop.run(self.browser.disconnect(), timeout=20)
        except Exception:
            pass
        try:
            self.loop.close()
        except Exception:
            pass

class RemoteBrowserError(RuntimeError):
    """The Scraping Browser API refused the connection."""

def _launch_local(pw, args, pool: Optional[ProxyPool]) -> _BrowserSession:
    """A local Chromium, optionally behind one exit from the pool."""
    from proxy_pool import split_credentials

    loop = _Loop()
    proxy_url = pool.current if pool else (args.proxy or None)
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    host_only, username, password = (None, None, None)
    if proxy_url:
        host_only, username, password = split_credentials(proxy_url)
        # Credentials NEVER go into argv — `--proxy-server=` becomes part
        # of the browser's command line, readable by anything that can run
        # `ps` and kept in shell history (CLAUDE.md §8). The host and port
        # stay, because which exit a run used is the point of the log and
        # is not the secret.
        launch_args.append(f"--proxy-server={host_only}")

    browser = loop.run(launch(headless=args.headless, args=launch_args,
                              handleSIGINT=False, handleSIGTERM=False,
                              handleSIGHUP=False), timeout=90)
    page = loop.run(browser.newPage(), timeout=30)

    if username:
        # The one place pyppeteer CAN authenticate a proxy.
        loop.run(page.authenticate({"username": username,
                                    "password": password}), timeout=30)

    user_agent = None
    fingerprint = None
    if args.fingerprint:
        from fingerprint_client import get_fingerprint, fingerprint_user_agent
        fingerprint = get_fingerprint(args.twocaptcha_key, tags=args.fp_tags,
                                      country=args.fp_country)
        user_agent = fingerprint_user_agent(fingerprint)
    if not user_agent:
        version = loop.run(browser.version(), timeout=20) or ""
        user_agent = _chrome_ua(version.split("/")[-1] if "/" in version
                                else version)
    loop.run(page.setUserAgent(user_agent), timeout=20)
    loop.run(page.setViewport({"width": 1366, "height": 900}), timeout=20)
    if fingerprint is not None:
        _apply_fingerprint(loop, page, fingerprint, user_agent)
    return _BrowserSession(loop, browser, page, proxy_url,
                           FALLBACK_CLIENT_VERSION, user_agent)

def _apply_fingerprint(loop, page, fingerprint, user_agent) -> None:
    """Give the identity everything the fingerprint states, not just a UA.

    CLAUDE.md §24 measured a BARE user-agent override being served on the
    first navigation and refused on the next three, while a complete
    identity was served throughout. `page.setUserAgent` on its own is that
    bare override: it changes `navigator.userAgent` and leaves
    `navigator.userAgentData` — and the `Sec-CH-UA` header — reporting the
    real browser.

    So this engine applies the same set its Playwright twin does: the
    client hints beside the user agent, the screen, the timezone, and the
    init script that carries `navigator.languages`, the platform and the
    WebGL strings. Best effort throughout — a fingerprint is cover, and no
    run should die because cover was imperfect.
    """
    from fingerprint_client import (user_agent_metadata, accept_language,
                                    playwright_init_script)

    # The init script goes in through the RAW protocol command, not through
    # `page.evaluateOnNewDocument`. That wrapper treats its argument as a
    # function EXPRESSION and emits `(<arg>)(…)`, so the shared module's
    # ready-to-run `(() => {…})();` becomes a syntax error that Chromium
    # drops in silence — the call reports success and nothing is installed.
    #
    # Measured 2026-09-21 rather than reasoned about: a run reported the
    # fingerprint applied while the page returned `deviceMemory` 8 against
    # the fingerprint's 32, `hardwareConcurrency` 4 against 32, and the
    # real SwiftShader renderer string. CLAUDE.md §1 names this exact
    # hazard — the drivers disagree about what a snippet IS — which is why
    # the shared module emits source and each engine installs it its own
    # way.
    session = None
    try:
        session = loop.run(page.target.createCDPSession(), timeout=20)
        # `Page.enable` FIRST, and it is not a formality. Without it the
        # protocol still answers `{"identifier": "1"}` — a success — and
        # never runs the script. Measured side by side on the same
        # fingerprint: without it the page reported `deviceMemory` 8,
        # with it 32, which is what the fingerprint states.
        loop.run(session.send("Page.enable", {}), timeout=20)
        loop.run(session.send("Page.addScriptToEvaluateOnNewDocument",
                              {"source": playwright_init_script(fingerprint)}),
                 timeout=20)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("Could not install the fingerprint's init script: %s",
                       _mask_credentials(exc))

    screen = (fingerprint.get("screen") or {})
    if screen.get("width") and screen.get("height"):
        try:
            loop.run(page.setViewport({
                "width": int(screen.get("outerWidth") or screen["width"]),
                "height": max(400, int(screen.get("outerHeight")
                                       or screen["height"] - 120)),
                "deviceScaleFactor": float(screen.get("deviceScaleFactor") or 1),
            }), timeout=20)
        except Exception:                              # noqa: BLE001
            pass

    metadata = user_agent_metadata(fingerprint)
    if not metadata:
        logger.warning("The fingerprint carried no brand list, so its client "
                       "hints are left alone: a HALF identity is worse than "
                       "none (CLAUDE.md §24).")
        return
    payload = {"userAgent": user_agent, "userAgentMetadata": metadata}
    language = accept_language(fingerprint)
    if language:
        payload["acceptLanguage"] = language
    platform = (fingerprint.get("navigator") or {}).get("platform")
    if platform:
        payload["platform"] = platform
    try:
        if session is None:
            session = loop.run(page.target.createCDPSession(), timeout=20)
        loop.run(session.send("Network.setUserAgentOverride", payload),
                 timeout=20)
        # Kept, never detached: detaching REVERTS the override, and the
        # call succeeds either way — measured on the Playwright twin.
        page._2captcha_cdp_session = session
        timezone = (fingerprint.get("intl") or {}).get("timeZone")
        if timezone:
            loop.run(session.send("Emulation.setTimezoneOverride",
                                  {"timezoneId": timezone}), timeout=20)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("Could not apply the fingerprint's client hints (%s) — "
                       "the run continues, but navigator.userAgentData will "
                       "disagree with the user agent.",
                       _mask_credentials(exc))

def _connect_remote(pw, args) -> _BrowserSession:
    """Connect to the 2Captcha Scraping Browser API over CDP.

    Never sets a user agent, a fingerprint or a proxy on top: the remote
    browser brings its own, and stacking a second creates a contradiction
    rather than better cover (CLAUDE.md §8). Unlike chromedriver, pyppeteer
    takes a full `ws://user:pass@host:port` and authenticates on the
    WebSocket upgrade, so an authenticated endpoint works here.
    """
    # Retried, and the reason is measured — see the same passage in
    # playwright_scraper.py. Three raw WebSocket upgrades to a live
    # Scraping Browser endpoint on 2026-09-21: `HTTP 500` instantly on the
    # first, connected on the other two.
    loop = _Loop()
    browser = None
    attempts = max(1, int(getattr(args, "retries", 2)) + 1)
    for attempt in range(1, attempts + 1):
        _Loop.last_error = None
        try:
            browser = loop.run(connect(browserWSEndpoint=args.cdp_endpoint),
                               timeout=CDP_CONNECT_TIMEOUT)
            break
        except Exception as exc:                       # noqa: BLE001
            # The awaited call times out; the REAL reason is whatever the
            # loop's handler caught. Preferring it turns "did not return
            # within 30s" into "server rejected WebSocket connection: HTTP
            # 500", which is the difference between checking your network
            # and reading CLAUDE.md §20.
            reason = _mask_credentials(_Loop.last_error or exc)
            if attempt >= attempts or "profile_locked" in reason:
                loop.close()
                raise RemoteBrowserError(
                    f"could not connect to --cdp-endpoint: "
                    f"{reason}") from exc
            logger.warning("Scraping Browser refused the WebSocket upgrade "
                           "(%s) — attempt %d/%d, retrying in %.1fs. This is "
                           "usually the service, not the request.",
                           str(reason).strip()[:120], attempt, attempts,
                           args.retry_delay)
            time.sleep(args.retry_delay)
    pages = loop.run(browser.pages(), timeout=30) or []
    page = pages[0] if pages else loop.run(browser.newPage(), timeout=30)
    return _BrowserSession(loop, browser, page, None,
                           FALLBACK_CLIENT_VERSION, None,
                           owns_browser=False)


class _Driver(run_core.Driver):
    """pyppeteer, behind the four operations `run_core` needs."""

    name = "puppeteer"

    def context(self):
        """A FRESH driver, because a worker gets its own.

        Playwright's sync API ties a browser to the thread that
        created it (CLAUDE.md §7), so the worker loop opens one of
        these per thread. Returning `self` would have the workers
        overwrite each other's handle — and it would have looked
        correct on a single-worker run.
        """
        return type(self)()

    def __enter__(self):
        self.handle = None     # pyppeteer needs no driver-level handle
        return self

    def __exit__(self, *exc):
        return False

    def launch_local(self, args, pool):
        return _launch_local(self.handle, args, pool)

    def connect_remote(self, args):
        return _connect_remote(self.handle, args)


_DRIVER = _Driver()


def scrape(args) -> int:
    """This engine's binding of the shared run."""
    return run_core.scrape(_DRIVER, args)


if __name__ == "__main__":
    try:
        sys.exit(run_core.main(_DRIVER))
    except DriverError as exc:
        text = run_core._mask_credentials(exc)
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(run_core.EXIT_API_ERROR)
        raise
