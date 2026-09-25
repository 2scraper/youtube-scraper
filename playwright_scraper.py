#!/usr/bin/env python3
"""playwright_scraper.py — YouTube comments, video metadata and video search.

    python3 playwright_scraper.py --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --pages 3
    python3 playwright_scraper.py --url dQw4w9WgXcQ --mode comments --replies --sort newest
    python3 playwright_scraper.py --url dQw4w9WgXcQ --mode video
    python3 playwright_scraper.py --url "web scraping tutorial" --mode search --pages 2

What this engine actually does, and why it looks unlike its siblings
====================================================================
Every other repo in this family drives a browser to RENDER a page and then
parses the markup. That is impossible here and also unnecessary: YouTube
puts no comments in the document at all, and the endpoint its own front end
calls answers a bare HTTP POST with no key, no cookies and no browser
(CLAUDE.md §21, and see `product_parser`'s docstring for the measurements).

So the browser has one job: be a real session on the site's origin. This
engine opens the watch page — which sets the visitor cookies, runs the
site's own consent handling and states the live client version — and then
issues the InnerTube calls through the CONTEXT's request API, which shares
those cookies, that proxy and that user agent.

That is deliberate rather than incidental. `context.request.post` is a
protocol call, not an evaluated string, so it works under YouTube's
Content-Security-Policy, which has no `unsafe-eval`; CLAUDE.md §18 records
a sibling repo whose readiness wait died with `EvalError` on exactly that.

The three modes
===============
    comments  a video's comment threads, `--replies` to expand them.
              STRICTLY SEQUENTIAL: a page is addressed by a token the
              previous page handed out, so `--concurrency` above 1 is
              refused with that reason rather than silently ignored.
    video     one video's metadata. Two InnerTube calls — `next` for the
              rendered figures and `player` for the exact upload date,
              duration, category and keywords.
    search    a query -> videos. Also a token chain, also sequential.

A page is not a page
====================
In `--mode comments` one "page" is one continuation — 20 top-level
comments, or 10 replies. `--pages 3` is therefore 60 comments, not three
screens of anything, and the run's closing line says what fraction of the
video that was. On a video with two and a half million comments it is not a
large fraction, and saying so is the point (CLAUDE.md §21).

The run itself lives in `run_core.py` and is shared with the other
two engines and with `youtube_scraper.py`. What stays here is what
touches Playwright: launching, connecting, the fingerprint, and the
session object. Measured on 2026-09-25, before the split: 24 of the
29 definitions the three engines shared were byte-identical.

The driver library is imported at MODULE level on purpose
(CLAUDE.md §10): the suite's skip and CI's `python -c "import
playwright_scraper"` both depend on this module failing to import when
Playwright is absent.
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
from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
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

logger = logging.getLogger("playwright_scraper")
DriverError = (PWError, PWTimeout)
REQUEST_TIMEOUT_MS = 30_000
NAVIGATION_TIMEOUT_MS = 60_000


class _BrowserSession:
    """A browser, a context on youtube.com, and the request API bound to it.

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

    def __init__(self, browser, context, page, proxy_url: Optional[str],
                 client_version: str, user_agent: Optional[str],
                 owns_context: bool = True):
        self.browser = browser
        self.context = context
        self.page = page
        self.proxy_url = proxy_url
        self.client_version = client_version
        self.user_agent = user_agent
        # False when we adopted the remote browser's existing context
        # instead of creating one. Closing a context we did not create
        # ends a session somebody else's run may still be using.
        self.owns_context = owns_context

    # -- transport ---------------------------------------------------------
    #
    # Two primitives, and the engines' only real difference from each
    # other. Playwright gets an APIRequestContext for free off the browser
    # context; pyppeteer and Selenium have to reach for their own
    # mechanisms. Everything above this line is shared logic.

    def get_text(self, url: str) -> Tuple[Optional[int], Optional[str]]:
        try:
            response = self.context.request.get(
                url, timeout=REQUEST_TIMEOUT_MS)
            return response.status, response.text()
        except PWError as exc:
            raise _TransportError(_mask_credentials(exc)) from exc

    def post_json(self, url: str, headers: Dict[str, str],
                  body: Dict[str, Any]) -> Tuple[Optional[int], Any]:
        try:
            response = self.context.request.post(
                url, headers=headers, data=body, timeout=REQUEST_TIMEOUT_MS)
        except PWError as exc:
            raise _TransportError(_mask_credentials(exc)) from exc
        status = response.status
        try:
            return status, response.json()
        except Exception:
            # A refusal is not JSON. Hand the body back as text so the
            # classifier can name it rather than the run dying on a decode.
            try:
                return status, response.text()
            except Exception:
                return status, None

    def goto(self, url: str) -> Optional[int]:
        response = self.page.goto(url, timeout=NAVIGATION_TIMEOUT_MS,
                                  wait_until="domcontentloaded")
        return response.status if response else None

    def content(self) -> str:
        try:
            return self.page.content()
        except PWError:
            return ""

    def count_selector(self, selector: str) -> int:
        try:
            return len(self.page.query_selector_all(selector))
        except PWError:
            return 0

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run a function EXPRESSION in the page.

        A function object, never an evaluated string: YouTube's
        Content-Security-Policy has no `unsafe-eval`, and a string-eval
        wait is what killed a sibling repo's run with `EvalError`
        (CLAUDE.md §18). Playwright routes this through
        `Runtime.callFunctionOn`, which works under any CSP.
        """
        try:
            return (self.page.evaluate(js, arg) if arg is not None
                    else self.page.evaluate(js))
        except DriverError:
            return None

    @property
    def url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    def close(self):
        """Close what this session created, and nothing else.

        `browser.close()` on a `connect_over_cdp` browser disconnects
        rather than shutting the remote one down, so it is safe either
        way. The CONTEXT is not: over CDP we adopt the one the remote
        browser already has, and closing it ends a session that is not
        ours to end.
        """
        if self.owns_context:
            try:
                self.context.close()
            except Exception:
                pass
        try:
            self.browser.close()
        except Exception:
            pass

def _launch_local(pw, args, pool: Optional[ProxyPool]) -> _BrowserSession:
    """A local Chromium, optionally behind one exit from the pool."""
    proxy_url = pool.current if pool else (args.proxy or None)
    launch_kwargs: Dict[str, Any] = {"headless": args.headless}
    proxy_conf = to_playwright(proxy_url)
    if proxy_conf:
        # Credentials go through Playwright's own fields, never onto the
        # command line: `--proxy-server=` becomes part of the browser's
        # argv, readable by anything that can run `ps` (CLAUDE.md §8).
        launch_kwargs["proxy"] = proxy_conf
    browser = pw.chromium.launch(**launch_kwargs)

    context_kwargs: Dict[str, Any] = {
        "locale": "en-US",
        "viewport": {"width": 1366, "height": 900},
    }
    user_agent = None
    fingerprint = None
    if args.fingerprint:
        from fingerprint_client import (get_fingerprint, fingerprint_user_agent,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fingerprint = get_fingerprint(args.twocaptcha_key, tags=args.fp_tags,
                                      country=args.fp_country)
        context_kwargs.update(playwright_context_kwargs(fingerprint))
        user_agent = fingerprint_user_agent(fingerprint)
    if not user_agent:
        user_agent = _chrome_ua(browser.version)
    context_kwargs["user_agent"] = user_agent

    context = browser.new_context(**context_kwargs)
    context.set_default_timeout(REQUEST_TIMEOUT_MS)
    if fingerprint is not None:
        context.add_init_script(playwright_init_script(fingerprint))
    page = context.new_page()
    if fingerprint is not None:
        _apply_fingerprint(context, page, fingerprint, user_agent)
    return _BrowserSession(browser, context, page, proxy_url,
                           FALLBACK_CLIENT_VERSION, user_agent)

def _apply_fingerprint(context, page, fingerprint, user_agent) -> None:
    """Give the identity everything the fingerprint states, not just a UA.

    Named the same as its twins in the other two engines, and that is not
    cosmetic: it was `_apply_client_hints` here and `_apply_fingerprint`
    there — one job under two names, which is exactly the drift the
    cross-engine surface check exists to catch. It did not catch it,
    because the check only compared engines that IMPORTED, and a supported
    virtualenv holds one. A third-party audit found it by installing all
    three, which the README tells people not to do.

    `user_agent=` on a context sets `navigator.userAgent` and leaves
    `navigator.userAgentData` reporting the REAL browser. Measured
    2026-09-21 by reading both back out of a live page: the UA said
    `Chrome/150` while the brands said `HeadlessChrome/153`. That is a
    contradiction rather than cover, and it is the half-identity CLAUDE.md
    §24 measured being refused where a complete one was served.

    Applied TOGETHER with the user agent and never alone, for the same
    reason: a bare override is the thing that failed there.

    The parameters differ from the twins' because the drivers do — this
    one takes the context that owns the CDP session, pyppeteer takes its
    event loop, Selenium takes the driver. The NAME is what has to match,
    so a reader looking for "where does this engine apply a fingerprint"
    finds the same word in all three.

    Best effort. If the protocol call is refused, the run continues with
    the identity it has and says so — a fingerprint is cover, and no run
    should die because cover was imperfect.
    """
    from fingerprint_client import user_agent_metadata, accept_language

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
    platform = metadata.get("platform")
    if platform:
        payload["platform"] = (fingerprint.get("navigator") or {}).get(
            "platform") or platform
    try:
        session = context.new_cdp_session(page)
        session.send("Network.setUserAgentOverride", payload)
        # NOT detached, and that is the whole of it. Detaching the session
        # REVERTS the override: measured 2026-09-21, a run that detached
        # reported `HeadlessChrome/153` in the brands while one that kept
        # the session reported the fingerprint's `Google Chrome/150`. The
        # call succeeded either way, so this failed silently in exactly the
        # way CLAUDE.md §16 describes — the tool doing less than it says
        # while reporting success. The session is parked on the page so it
        # lives as long as the browser does.
        page._2captcha_cdp_session = session
    except PWError as exc:
        logger.warning("Could not apply the fingerprint's client hints (%s) — "
                       "the run continues, but navigator.userAgentData will "
                       "disagree with the user agent.",
                       _mask_credentials(exc))

def _connect_remote(pw, args) -> _BrowserSession:
    """Connect to the 2Captcha Scraping Browser API over CDP.

    Never sets a user agent, a fingerprint or a proxy on top: the remote
    browser brings its own, and stacking a second creates a contradiction
    rather than better cover (CLAUDE.md §8). The engine refuses those
    combinations in `parse_args` rather than quietly dropping them.
    """
    # The upgrade is RETRIED, because a rejection here is usually the
    # service rather than the request. Measured 2026-09-21 against a live
    # Scraping Browser endpoint: three raw WebSocket upgrades seconds
    # apart gave `HTTP 500` instantly on the first and connected on the
    # other two. Un-retried, that is exit 5 on roughly a third of runs for
    # a condition that clears by itself — and CLAUDE.md §20 names it: a
    # managed browser answering 500 on the upgrade is the SERVICE failing
    # to raise its own exit, not this code.
    #
    # Deliberately NOT the same thing as a dead proxy (§8). There is no
    # other exit to move to here, so the right response is to ask the same
    # endpoint again after a moment rather than to rotate.
    browser = None
    attempts = max(1, int(getattr(args, "retries", 2)) + 1)
    for attempt in range(1, attempts + 1):
        try:
            browser = pw.chromium.connect_over_cdp(
                args.cdp_endpoint, timeout=NAVIGATION_TIMEOUT_MS)
            break
        except PWError as exc:
            text = _mask_credentials(exc)
            if attempt >= attempts or "profile_locked" in text:
                # `profile_locked` is not transient: another run holds
                # this pid, and asking again cannot help.
                raise PWError(f"could not connect to --cdp-endpoint: "
                              f"{text}") from exc
            logger.warning("Scraping Browser refused the WebSocket upgrade "
                           "(%s) — attempt %d/%d, retrying in %.1fs. This is "
                           "usually the service, not the request.",
                           text.strip()[:120], attempt, attempts,
                           args.retry_delay)
            time.sleep(args.retry_delay)
    adopted = bool(browser.contexts)
    context = browser.contexts[0] if adopted else browser.new_context()
    page = context.pages[0] if context.pages else context.new_page()
    return _BrowserSession(browser, context, page, None,
                           FALLBACK_CLIENT_VERSION, None,
                           owns_context=not adopted)


class _Driver(run_core.Driver):
    """Playwright, behind the four operations `run_core` needs."""

    name = "playwright"

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
        self._pw = None
        self.handle = None
        return self

    def __exit__(self, *exc):
        runtime, self._pw = self._pw, None
        self.handle = None
        return runtime.__exit__(*exc) if runtime is not None else False

    def _runtime(self):
        """Start Playwright on FIRST USE, not on entry.

        `scrape()` opens a driver context unconditionally, so starting the
        runtime in `__enter__` meant a `--transport http` run paid for a
        Playwright runtime it never touched — the second half of the
        2026-09-25 audit's F01. Nothing below this line runs unless a
        browser is really being opened.
        """
        if self._pw is None:
            self._pw = sync_playwright()
            self.handle = self._pw.__enter__()
        return self.handle

    def launch_local(self, args, pool):
        return _launch_local(self._runtime(), args, pool)

    def connect_remote(self, args):
        return _connect_remote(self._runtime(), args)


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
