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
"""

import argparse
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS,
                            RECAPTCHA_DISCOVERY_JS)
from output_writer import (Comment, Video, dedupe_by_key, finish_run,
                           utc_now, EXIT_API_ERROR, EXIT_NO_PRODUCTS,
                           SOURCE_DEFAULT)
import page_flow
from page_flow import SolveBudget
import product_parser as parser
from product_parser import (CLIENT_VERSION_URL, DEFAULT_LOCALE, DEFAULT_REGION,
                            DEFAULT_SORT, FALLBACK_CLIENT_VERSION, SORTS,
                            canonical_video_url, client_version_from_text,
                            continuation_body, innertube_headers,
                            innertube_url, is_supported_url, parse_comments,
                            parse_search, parse_video, player_body,
                            search_body, video_body, video_id_from_url)
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")

# The one name the shared logic below uses for "the driver failed". Each
# engine binds it to its own library's exception, so everything from
# `_prime_session` downwards is byte-comparable across the three — which is
# what makes "the engines must agree" checkable rather than aspirational.
DriverError = (PWError, PWTimeout)

MODES = ("comments", "video", "search")
DEFAULT_MODE = "comments"

# Every remote call is bounded (CLAUDE.md §8). Playwright's request API
# takes a per-call timeout and does not impose one of its own.
REQUEST_TIMEOUT_MS = 30_000
NAVIGATION_TIMEOUT_MS = 60_000

# How many comments a `--pages N` run expects, used only to make the
# closing log honest about what N meant.
COMMENTS_PER_PAGE = parser.PAGE_SIZE
REPLIES_PER_PAGE = parser.REPLY_PAGE_SIZE


@dataclass
class PageOutcome:
    """One fetch attempt's result, in page order rather than arrival order.

    CLAUDE.md §8: merging by arrival order makes the output depend on which
    worker finished first. Workers return these and the caller sorts.
    """
    number: int
    rows: List[Any] = field(default_factory=list)
    state: str = parser.STATE_UNKNOWN
    status: Optional[int] = None
    blocked: bool = False
    error: Optional[str] = None
    next_token: Optional[str] = None
    payload: Optional[Any] = None
    attempted: bool = True


def _mask_credentials(text: Any) -> str:
    """Mask every credential in a string, not just the first.

    CLAUDE.md §8: a masker that handles the first occurrence prints the
    password the other four times and looks like it is working — Playwright
    repeats a CDP endpoint five times in one error, once in the message and
    four more in its call log.
    """
    import re
    out = str(text)
    out = re.sub(r"(?i)\b((?:client)?key|token|api[_-]?key|password)=[^&\s\"']+",
                 r"\1=***", out)
    out = re.sub(r"(wss?://)([^:/@\s]+):([^@\s]+)@", r"\1\2:***@", out)
    return out


def _chrome_ua(chromium_version: str) -> str:
    """A user agent built from the Chromium actually installed.

    CLAUDE.md §8: a hardcoded version drifts from whatever is installed,
    and claiming an older Chrome than the JS engine and TLS handshake
    report is itself a mismatch.
    """
    major = (chromium_version or "").split(".")[0] or "140"
    return (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def _proxy_failure(exc: Exception) -> str:
    """Name a dead proxy, or "" for anything else.

    CLAUDE.md §8: Chromium reports a dead proxy as a generic error, not a
    timeout, and the two want opposite responses — a timeout deserves
    another try at the SAME exit, a dead proxy a DIFFERENT one. Catching
    only the timeout type let this escape as a traceback in a sibling repo.
    """
    text = str(exc)
    for marker in ("ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
                   "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_UNEXPECTED_PROXY_AUTH",
                   "ERR_PROXY_CERTIFICATE_INVALID"):
        if marker in text:
            return marker
    return ""


class _BrowserSession:
    """A browser, a context on youtube.com, and the request API bound to it.

    A rotation is a FRESH BROWSER (CLAUDE.md §8): cookies a bot manager
    issued against exit A and replayed from exit B are a stronger signal
    than either address alone. So this object is torn down and rebuilt
    rather than having its proxy swapped underneath it.
    """

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


class _TransportError(RuntimeError):
    """A transport-level failure, already masked."""


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


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
    if args.fingerprint:
        from fingerprint_client import (get_fingerprint, fingerprint_user_agent,
                                        playwright_context_kwargs)
        fp = get_fingerprint(args.twocaptcha_key, tags=args.fp_tags,
                             country=args.fp_country)
        context_kwargs.update(playwright_context_kwargs(fp))
        user_agent = fingerprint_user_agent(fp)
    if not user_agent:
        user_agent = _chrome_ua(browser.version)
    context_kwargs["user_agent"] = user_agent

    context = browser.new_context(**context_kwargs)
    context.set_default_timeout(REQUEST_TIMEOUT_MS)
    page = context.new_page()
    return _BrowserSession(browser, context, page, proxy_url,
                           FALLBACK_CLIENT_VERSION, user_agent)


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


def _open_session(pw, args, pool: Optional[ProxyPool]) -> _BrowserSession:
    session = (_connect_remote(pw, args) if args.cdp_endpoint
               else _launch_local(pw, args, pool))
    if args.proxy_rotate == "per-run" or not pool:
        logger.info("Browser up%s", f" via {mask(session.proxy_url)}"
                    if session.proxy_url else "")
    return session


# ---------------------------------------------------------------------------
# Bootstrapping the session on the site's own origin
# ---------------------------------------------------------------------------


def _prime_session(session: _BrowserSession, args, url: str) -> Optional[int]:
    """Load a real page so the context carries the site's own cookies.

    Also where the client version comes from, at no extra cost: the watch
    page states `INNERTUBE_CLIENT_VERSION` and the browser already has it.
    A hardcoded version drifts from what the site is serving, which is the
    same objection CLAUDE.md §8 makes to a hardcoded user agent.

    Falls back to `/sw.js_data` — 2.8 KB against the watch page's 1.4 MB —
    and then to the constant, and says which it used.
    """
    status = None
    try:
        status = session.goto(url)
    except DriverError as exc:
        failure = _proxy_failure(exc)
        if failure:
            raise
        logger.warning("Could not open %s (%s) — the InnerTube calls are "
                       "tried anyway, they do not need the page.",
                       url, _mask_credentials(exc))

    page_flow.wait_for_count(session.count_selector,
                             page_flow.ready_selector(args.mode),
                             page_flow.min_matches(args.mode),
                             timeout_ms=min(10_000,
                                            page_flow.content_timeout_ms(
                                                args.mode)))

    version = client_version_from_text(session.content())
    if not version:
        try:
            _, text = session.get_text(CLIENT_VERSION_URL)
            version = client_version_from_text(text)
        except _TransportError as exc:
            logger.debug("sw.js_data unreachable: %s", exc)
    if version:
        session.client_version = version
        logger.info("InnerTube client version %s (read from the site)", version)
    else:
        logger.warning("Could not read the live InnerTube client version; "
                       "falling back to %s. If calls start failing with 400, "
                       "this is the first thing to check.",
                       FALLBACK_CLIENT_VERSION)
    return status


# ---------------------------------------------------------------------------
# Captcha
# ---------------------------------------------------------------------------


def handle_captcha_if_present(session: _BrowserSession, args,
                              budget: SolveBudget) -> bool:
    """Detect and, if it is worth paying for, solve a challenge.

    Both call sites — before classification and after — go through the same
    `SolveBudget`, which is the CLAUDE.md §23 fix: `SOLVES_PER_PAGE` read
    like an enforced limit in every repo in this family and was not one,
    because only the second of the two calls was counted. One page bought
    three solves on a site where a challenge rendered on every fetch.

    A missing key or a solver error is a WARNING and the run continues
    (CLAUDE.md §8): detection is not the same as blocking, and a run that
    already has data must not die because a solve failed.
    """
    if args.solve_captcha == "never":
        return False
    html = session.content()
    if not html:
        return False
    static = detect_recaptcha_v3(html, session.url)
    live = None
    try:
        live = detect_recaptcha_in_page(session.evaluate, session.url)
    except Exception:                              # noqa: BLE001
        live = None
    challenge = reconcile_detections(static, live)
    if challenge is None:
        return False
    if not budget.may_spend():
        logger.warning("A challenge is present and this page's solve budget "
                       "(%d) is already spent — not paying twice for one "
                       "page.", budget.limit)
        return False
    if not args.twocaptcha_key:
        logger.warning("A captcha is present and no --twocaptcha-key was "
                       "given; continuing unsolved. The run reports exit 3 "
                       "if it really was blocked.")
        return False
    if not budget.spend():
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                min_score=args.min_score,
                                api=args.captcha_api)
    except CaptchaUnsolvable as exc:
        logger.warning("Captcha not solved: %s", _mask_credentials(exc))
        return False
    except Exception as exc:                      # noqa: BLE001
        logger.warning("Captcha solver failed: %s", _mask_credentials(exc))
        return False
    if session.evaluate(INJECT_TOKEN_JS, token) is None:
        logger.warning("Could not inject the solved token into the page.")
        return False
    logger.info("Captcha solved and token injected.")
    return True


# ---------------------------------------------------------------------------
# One InnerTube call, with the family's retry / block policy around it
# ---------------------------------------------------------------------------


def _dump(args, name: str, payload: Any) -> None:
    """Write the exact bytes a call returned.

    On SUCCESS too, not only on failure (CLAUDE.md §9): a run can return
    the right count with a field silently unpopulated, and then the exact
    payload is the only way to tell a parsing bug from a too-early
    snapshot.
    """
    if not args.dump_html:
        return
    path = f"{args.out}_{name}.json"
    try:
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, (dict, list)):
                json.dump(payload, handle, ensure_ascii=False)
            else:
                handle.write(str(payload))
        logger.info("Wrote %s", path)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)


def _call(session: _BrowserSession, args, body: Dict[str, Any],
          endpoint: str, budget: SolveBudget,
          label: str) -> Tuple[Optional[int], Any, str]:
    """POST once and classify the answer. No retries here — that is above."""
    url = innertube_url(endpoint)
    headers = innertube_headers(session.client_version)
    status, payload = session.post_json(url, headers, body)
    state = page_flow.classify(payload, status, url, args.mode, endpoint)
    logger.debug("%s -> http %s, state %s", label, status, state)
    return status, payload, state


def _fetch_with_policy(session_box: Dict[str, Any], pw, args,
                       pool: Optional[ProxyPool], body: Dict[str, Any],
                       endpoint: str, label: str
                       ) -> Tuple[Optional[int], Any, str, bool]:
    """One call plus the retry / rotate / solve policy around it.

    `session_box` holds the live session so a rotation can replace it: a
    rotation is a fresh browser, never a proxy swapped under a live
    session (CLAUDE.md §8).
    """
    budget = SolveBudget()
    attempts = max(1, int(args.retries) + 1)
    blocked_seen = False
    status = payload = None
    state = parser.STATE_UNKNOWN

    for attempt in range(1, attempts + 1):
        session = session_box["session"]
        # First of the two solve call sites: clear a challenge BEFORE the
        # answer is judged, so a gated page is not classified on its
        # interstitial.
        if args.solve_captcha == "always":
            handle_captcha_if_present(session, args, budget)
        try:
            status, payload, state = _call(session, args, body, endpoint,
                                           budget, label)
        except _TransportError as exc:
            state = parser.STATE_ERROR
            payload = str(exc)
            status = None
            exit_failed = _proxy_failure(exc)
            if exit_failed:
                # A dead proxy is NOT a timeout, and the two want opposite
                # responses: a timeout deserves another try at the SAME
                # exit, a dead proxy a DIFFERENT one. Chromium reports it
                # as a generic error rather than as a timeout, which is how
                # this escaped as a traceback in a sibling repo
                # (CLAUDE.md §8).
                logger.warning("%s failed at the EXIT, not at the site: %s "
                               "via %s. Rotating rather than retrying the "
                               "same address.", label, exit_failed,
                               mask(session.proxy_url))
                if pool:
                    pool.advance(exit_failed)
                    session_box["session"].close()
                    session_box["session"] = _open_session(pw, args, pool)
                    _prime_session(session_box["session"], args,
                                   session_box["prime_url"])
            else:
                logger.warning("%s failed after %d attempt(s): %s",
                               label, attempt, exc)

        if page_flow.counts_as_blocked(state):
            blocked_seen = True
            # Second call site, same budget.
            if page_flow.should_solve(state):
                handle_captcha_if_present(session, args, budget)

        if not page_flow.should_retry(state) or attempt >= attempts:
            break
        if page_flow.counts_as_blocked(state):
            if not page_flow.RETRY_ON_BLOCKED:
                break
            budget_left = (args.proxy_block_retries if pool
                           else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
            if attempt > budget_left:
                break
            if pool and pool.rotates_per_page():
                pool.advance(f"state {state}")
                logger.info("Rotating exit and rebuilding the browser — a "
                            "rotation is a fresh browser, never a proxy "
                            "swapped under a live session.")
                session_box["session"].close()
                session_box["session"] = _open_session(pw, args, pool)
                _prime_session(session_box["session"], args,
                               session_box["prime_url"])
        logger.info("%s: state %s, retrying (%d/%d) in %.1fs",
                    label, state, attempt, attempts - 1, args.retry_delay)
        time.sleep(args.retry_delay)

    return status, payload, state, blocked_seen


# ---------------------------------------------------------------------------
# --mode comments
# ---------------------------------------------------------------------------


def _run_comments(session_box, pw, args, pool) -> Tuple[List[Any], Dict[str, Any]]:
    """Walk the continuation chain, page by page, in the order it is given."""
    video_id = video_id_from_url(args.url)
    session = session_box["session"]
    scraped_at = utc_now()

    status, watch, state, blocked = _fetch_with_policy(
        session_box, pw, args, pool,
        video_body(video_id, session_box["session"].client_version,
                   args.locale, args.region),
        "next", f"watch {video_id}")
    _dump(args, "watch", watch)

    meta: Dict[str, Any] = {"video_id": video_id, "sort": args.sort,
                            "replies": bool(args.replies),
                            "client_version": session_box["session"].client_version}

    if state == parser.STATE_VIDEO_UNAVAILABLE:
        logger.error("Video %s is unavailable — private, deleted, or never "
                     "there. That is the site's answer, not a block.", video_id)
        return [], dict(meta, stop_reason="video_unavailable", pages_completed=0,
                        blocked=False)
    if state == parser.STATE_COMMENTS_DISABLED:
        reason = parser.disabled_reason(watch) or "comments are turned off"
        logger.error("Comments are turned off for %s (%s). The site answered "
                     "the question; there is nothing to fetch.", video_id, reason)
        return [], dict(meta, stop_reason="comments_disabled", pages_completed=0,
                        blocked=False)
    if state != parser.STATE_CONTENT:
        return [], dict(meta, stop_reason=f"watch_{state}", pages_completed=0,
                        blocked=blocked)

    video_row = parse_video(watch, video_id=video_id, scraped_at=scraped_at,
                            row_cls=Video)
    video_title = video_row.title if video_row else None
    meta["video_title"] = video_title

    # The site hands out TWO kinds of first token and they are not
    # interchangeable. The comment SECTION's token is what the page itself
    # uses on load, and its response carries the `commentsHeaderRenderer`
    # with the video's exact comment total in it. A token taken from the
    # SORT MENU fetches the same comments and omits that header — measured,
    # and it is why the first live run recorded `total_comments: null` on a
    # video with two and a half million of them. So the section token is
    # used for the site's own default ordering, and the menu token only
    # when a different one was asked for.
    tokens = parser.sort_tokens(watch)
    section_token = parser.comments_token(watch)
    if args.sort == DEFAULT_SORT and section_token:
        token = section_token
    else:
        token = tokens.get(args.sort) or section_token
        if args.sort not in tokens:
            logger.warning("The site's sort menu did not offer %r on this "
                           "video; using its default ordering and recording "
                           "that in the rows.", args.sort)
    if not token:
        return [], dict(meta, stop_reason="no_comment_token", pages_completed=0,
                        blocked=blocked)

    if not page_flow.pagination_is_addressable("", args.mode):
        # Said once, in the run's own log, rather than left to be inferred
        # from the absence of workers: page 5's address is inside page 4,
        # so this walk cannot be planned ahead or split (CLAUDE.md §7).
        logger.info("This listing is not independently addressable — each "
                    "page's token comes from the page before it, so the "
                    "walk is sequential.")

    rows: List[Any] = []
    seen: set = set()
    pages_failed: List[int] = []
    total_comments = None
    pending_replies: List[Tuple[str, str]] = []
    pages = page_flow.pages_to_plan(args.pages, None)
    completed = 0
    stop_reason = "page_cap_reached"

    for page_no in range(1, pages + 1):
        status, payload, state, blocked_here = _fetch_with_policy(
            session_box, pw, args, pool,
            continuation_body(token, session_box["session"].client_version,
                              args.locale, args.region),
            "next", f"comments page {page_no}")
        blocked = blocked or blocked_here
        if page_no == 1:
            _dump(args, "comments_p1", payload)

        if state == parser.STATE_EMPTY:
            stop_reason = "pagination_exhausted"
            break
        if not page_flow.should_parse(state):
            pages_failed.append(page_no)
            stop_reason = f"page_{state}"
            break

        page_rows = parse_comments(
            payload, video_id=video_id, video_title=video_title,
            sort=parser.selected_sort(payload) or args.sort, page=page_no,
            scraped_at=scraped_at, row_cls=Comment)
        if not page_rows:
            # A payload the site plainly served that parsed to nothing is
            # OUR bug, and it gets its own name so it cannot be reported as
            # "this video has no comments" (CLAUDE.md §20).
            logger.error("Page %d was served (%d bytes) and parsed to zero "
                         "rows. That is a parser failure, not an empty "
                         "video. Re-run with --dump-html and read the "
                         "payload.", page_no, len(json.dumps(payload or {})))
            pages_failed.append(page_no)
            stop_reason = "parse_error"
            break

        if total_comments is None:
            # Exact, from the comment section's own header. The watch
            # payload's panel states an ABBREVIATED total ("2.4M") and is
            # the fallback, so the sidecar is never simply blank about how
            # big a sample this run is.
            total_comments = parser.total_comment_count(payload)
            if total_comments is None:
                total_comments = parser.panel_comment_count(watch)[0]
        kept = dedupe_by_key(page_rows, seen, key="sku")
        if len(kept) != len(page_rows):
            logger.info("Page %d: %d of %d comments were already seen — the "
                        "ranking moved under the run.", page_no,
                        len(page_rows) - len(kept), len(page_rows))
        rows.extend(kept)
        completed = page_no
        if args.replies:
            pending_replies.extend(parser.reply_tokens(payload))

        token = parser.next_page_token(payload)
        logger.info("Page %d: %d comments (%d total so far)%s", page_no,
                    len(kept), len(rows), "" if token else " — last page")
        if not token:
            stop_reason = "pagination_exhausted"
            break
        if args.delay:
            time.sleep(args.delay)

    if pending_replies:
        reply_rows, reply_failed = _run_replies(
            session_box, pw, args, pool, pending_replies, video_id,
            video_title, scraped_at, seen)
        rows.extend(reply_rows)
        pages_failed.extend(reply_failed)

    meta.update({
        "stop_reason": stop_reason,
        "pages_completed": completed,
        "pages_failed": pages_failed,
        "blocked": blocked,
        "total_comments": total_comments,
        "comments_collected": len(rows),
        "sample_share_pct": page_flow.sample_share(len(rows), total_comments),
        "reply_threads_expanded": len(pending_replies),
        "video": video_row.__dict__ if video_row else None,
    })
    return rows, meta


def _run_replies(session_box, pw, args, pool, threads, video_id, video_title,
                 scraped_at, seen) -> Tuple[List[Any], List[int]]:
    """Expand each thread that has replies, `--reply-pages` pages deep."""
    rows: List[Any] = []
    failed: List[int] = []
    logger.info("Expanding %d reply threads, up to %d page(s) each.",
                len(threads), args.reply_pages)
    for index, (parent, token) in enumerate(threads, start=1):
        depth = 0
        while token and depth < args.reply_pages:
            depth += 1
            status, payload, state, _ = _fetch_with_policy(
                session_box, pw, args, pool,
                continuation_body(token, session_box["session"].client_version,
                                  args.locale, args.region),
                "next", f"replies {parent} page {depth}")
            if not page_flow.should_parse(state) or state == parser.STATE_EMPTY:
                if state != parser.STATE_EMPTY:
                    failed.append(index)
                break
            batch = parse_comments(payload, video_id=video_id,
                                   video_title=video_title, sort=args.sort,
                                   page=depth, scraped_at=scraped_at,
                                   row_cls=Comment)
            rows.extend(dedupe_by_key(batch, seen, key="sku"))
            token = parser.next_page_token(payload)
            if args.delay:
                time.sleep(args.delay)
    logger.info("Replies: %d rows from %d threads.", len(rows), len(threads))
    return rows, failed


# ---------------------------------------------------------------------------
# --mode video
# ---------------------------------------------------------------------------


def _video_ids(args) -> List[str]:
    """The videos a `--mode video` run covers.

    `--url` takes one video, or several separated by commas — which is the
    unit that HAS independent addresses on this site and therefore the one
    `--concurrency` can actually use.
    """
    out: List[str] = []
    for chunk in str(args.url or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        vid = video_id_from_url(chunk)
        if vid and vid not in out:
            out.append(vid)
        elif not vid:
            logger.warning("Skipping %r: no video id in it.", chunk)
    return out


def _fetch_one_video(session_box, pw, args, pool, video_id: str,
                     number: int, scraped_at: str) -> PageOutcome:
    version = session_box["session"].client_version
    status, watch, state, blocked = _fetch_with_policy(
        session_box, pw, args, pool,
        video_body(video_id, version, args.locale, args.region),
        "next", f"watch {video_id}")
    if state == parser.STATE_VIDEO_UNAVAILABLE:
        return PageOutcome(number=number, state=state, status=status,
                           error="video unavailable")
    if state not in (parser.STATE_CONTENT, parser.STATE_COMMENTS_DISABLED):
        return PageOutcome(number=number, state=state, status=status,
                           blocked=blocked, error=f"state {state}")

    row = parse_video(watch, video_id=video_id, scraped_at=scraped_at,
                      row_cls=Video)
    if row is None:
        return PageOutcome(number=number, state="parse_error", status=status,
                           error="watch payload parsed to no row")

    # The second call, and the only reason this mode makes two: `/player`
    # is where the exact ISO upload date, the duration in seconds, the
    # category and the keyword list live. `next` publishes none of them.
    status2, player, state2, _ = _fetch_with_policy(
        session_box, pw, args, pool,
        player_body(video_id, version, args.locale, args.region),
        "player", f"player {video_id}")
    if isinstance(player, dict):
        parser.apply_player(player, row)
    else:
        logger.warning("/player did not answer for %s (state %s) — the row "
                       "keeps its rendered date and loses the exact one.",
                       video_id, state2)
    if number == 1:
        _dump(args, "watch", watch)
        _dump(args, "player", player)
    return PageOutcome(number=number, rows=[row], state=parser.STATE_CONTENT,
                       status=status, blocked=blocked)


def _run_video(session_box, pw, args, pool) -> Tuple[List[Any], Dict[str, Any]]:
    ids = _video_ids(args)
    if not ids:
        return [], {"stop_reason": "no_video_id", "pages_completed": 0,
                    "blocked": False}
    scraped_at = utc_now()
    planned = page_flow.pages_to_plan(max(args.pages, len(ids)), len(ids))
    ids = ids[:planned]

    workers = page_flow.concurrency_for_mode(args.mode, args.concurrency)
    limit = page_flow.concurrency_limit(args.cdp_endpoint)
    if limit:
        workers = min(workers, limit)

    outcomes: List[PageOutcome] = []
    if workers <= 1 or len(ids) == 1:
        for number, vid in enumerate(ids, start=1):
            outcomes.append(_fetch_one_video(session_box, pw, args, pool, vid,
                                             number, scraped_at))
            if args.delay and number < len(ids):
                time.sleep(args.delay)
    else:
        outcomes = _fetch_videos_concurrently(pw, args, pool, ids, scraped_at,
                                              workers)

    outcomes.sort(key=lambda o: o.number)      # page order, not arrival order
    rows = [row for outcome in outcomes for row in outcome.rows]
    failed = [o.number for o in outcomes if not o.rows and o.attempted]
    blocked = any(o.blocked for o in outcomes)
    completed = len([o for o in outcomes if o.rows])
    stop_reason = "single_page_route" if len(ids) == 1 and rows else (
        "completed" if not failed else "partial")
    return rows, {"stop_reason": stop_reason, "pages_completed": completed,
                  "pages_failed": failed, "blocked": blocked,
                  "videos_requested": len(ids),
                  "client_version": session_box["session"].client_version}


def _fetch_videos_concurrently(pw, args, pool, ids, scraped_at, workers):
    """One browser per worker, each worker owning ONE exit for its lifetime.

    Workers start on DIFFERENT exits — each gets its own pool object holding
    the same exits rotated to a different offset — so no thread needs a
    lock: the concurrency is safe by construction rather than by discipline
    (CLAUDE.md §7).
    """
    work: "queue.Queue[Tuple[int, str]]" = queue.Queue()
    for number, vid in enumerate(ids, start=1):
        work.put((number, vid))
    results: List[PageOutcome] = []
    lock = threading.Lock()

    def worker(index: int):
        worker_pool = _worker_pool(pool, index)
        with _driver_context() as own_pw:
            box = {"session": _open_session(own_pw, args, worker_pool),
                   "prime_url": canonical_video_url(ids[0])}
            try:
                _prime_session(box["session"], args, box["prime_url"])
                while True:
                    try:
                        number, vid = work.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        outcome = _fetch_one_video(box, own_pw, args,
                                                   worker_pool, vid, number,
                                                   scraped_at)
                    except Exception as exc:               # noqa: BLE001
                        # A worker that raises must neither hang the run nor
                        # lose its siblings' pages (CLAUDE.md §10).
                        outcome = PageOutcome(number=number, state="error",
                                              error=_mask_credentials(exc))
                    with lock:
                        results.append(outcome)
            finally:
                box["session"].close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def _worker_pool(pool: Optional[ProxyPool], worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to
    a different offset, so workers start on distinct addresses and no
    thread needs a lock — the concurrency is safe by construction rather
    than by discipline (CLAUDE.md §7).
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


# ---------------------------------------------------------------------------
# --mode search
# ---------------------------------------------------------------------------


def _run_search(session_box, pw, args, pool) -> Tuple[List[Any], Dict[str, Any]]:
    query = (args.url or "").strip()
    if not query:
        return [], {"stop_reason": "no_query", "pages_completed": 0,
                    "blocked": False}
    scraped_at = utc_now()
    version = session_box["session"].client_version
    body = search_body(query, version, args.locale, args.region)
    rows: List[Any] = []
    seen: set = set()
    pages_failed: List[int] = []
    estimated = None
    completed = 0
    blocked = False
    stop_reason = "page_cap_reached"

    for page_no in range(1, max(1, args.pages) + 1):
        status, payload, state, blocked_here = _fetch_with_policy(
            session_box, pw, args, pool, body, "search",
            f"search page {page_no}")
        blocked = blocked or blocked_here
        if page_no == 1:
            _dump(args, "search_p1", payload)
            estimated = parser.search_total(payload)
        if state == parser.STATE_EMPTY:
            stop_reason = "pagination_exhausted"
            break
        if state not in (parser.STATE_CONTENT, parser.STATE_COMMENTS_DISABLED):
            pages_failed.append(page_no)
            stop_reason = f"page_{state}"
            break
        page_rows = parse_search(payload, query=query, scraped_at=scraped_at,
                                 page=page_no, row_cls=Video)
        if not page_rows:
            stop_reason = "pagination_exhausted" if page_no > 1 else "parse_error"
            if page_no == 1:
                pages_failed.append(page_no)
            break
        rows.extend(dedupe_by_key(page_rows, seen, key="sku"))
        completed = page_no
        logger.info("Search page %d: %d videos (%d total).", page_no,
                    len(page_rows), len(rows))
        token = parser.search_page_token(payload)
        if not token:
            stop_reason = "pagination_exhausted"
            break
        body = continuation_body(token, version, args.locale, args.region)
        if args.delay:
            time.sleep(args.delay)

    return rows, {"stop_reason": stop_reason, "pages_completed": completed,
                  "pages_failed": pages_failed, "blocked": blocked,
                  "query": query, "estimated_results": estimated,
                  "client_version": version}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

_RUNNERS = {"comments": _run_comments, "video": _run_video,
            "search": _run_search}


class _driver_context:
    """The driver's own lifetime, as a context manager.

    Playwright needs one (`sync_playwright()`); the other two engines do
    not, and theirs is a no-op holding the same shape. Keeping it here means
    `scrape()` and the worker loop are identical in all three files.
    """

    def __enter__(self):
        self._pw = sync_playwright()
        return self._pw.__enter__()

    def __exit__(self, *exc):
        return self._pw.__exit__(*exc)


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.concurrency > 1 and args.mode == "video":
        logger.info("%d workers over %d exit(s).", args.concurrency, len(pool))
    elif args.concurrency > 1 and not pool:
        # Warn, do not refuse (CLAUDE.md §7).
        logger.warning("--concurrency %d with no proxy pool sends %dx the "
                       "traffic from one address, which is a faster way to "
                       "get it scored than to gather data.",
                       args.concurrency, args.concurrency)

    prime_url = (canonical_video_url(video_id_from_url(args.url) or "")
                 if args.mode != "search" else
                 "https://www.youtube.com/results?search_query=" +
                 (args.url or "").replace(" ", "+"))

    rows: List[Any] = []
    meta: Dict[str, Any] = {}
    with _driver_context() as pw:
        session_box = {"session": _open_session(pw, args, pool),
                       "prime_url": prime_url}
        try:
            _prime_session(session_box["session"], args, prime_url)
            rows, meta = _RUNNERS[args.mode](session_box, pw, args, pool)
        finally:
            session_box["session"].close()

    video = meta.pop("video", None)
    extra = {k: v for k, v in meta.items()
             if k not in ("stop_reason", "pages_completed", "pages_failed",
                          "blocked")}
    extra["engine"] = "playwright"
    extra["category"] = args.category
    if video:
        extra["video_title"] = video.get("title")

    total = extra.get("total_comments")
    if args.mode == "comments" and total:
        logger.info("Collected %d of the video's %s comments (%.4f%%). A run "
                    "that fetched every page it asked for is COMPLETE; it is "
                    "not exhaustive.", len(rows), f"{total:,}",
                    extra.get("sample_share_pct") or 0.0)

    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=bool(meta.get("blocked")),
        stop_reason=meta.get("stop_reason", "completed"),
        pages_requested=args.pages,
        pages_completed=int(meta.get("pages_completed") or 0),
        pages_failed=meta.get("pages_failed") or None,
        start_url=prime_url, final_url=prime_url,
        mode=args.mode, source=SOURCE_DEFAULT, extra=extra)


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(
        description="Scrape YouTube comments, video metadata or video search "
                    "results through the site's own InnerTube endpoint.")
    p.add_argument("--url", default=None,
                   help="A YouTube video URL (/watch, /shorts, /live, "
                        "youtu.be) or a bare 11-character video id. In "
                        "--mode video, a comma-separated list of them. In "
                        "--mode search, the SEARCH QUERY instead. Falls back "
                        "to YOUTUBE_URL from the environment or .env.")
    p.add_argument("--mode", choices=MODES, default=DEFAULT_MODE,
                   help="comments (default): a video's comment threads. "
                        "video: one video's metadata, including the exact "
                        "upload date that the comment rows cannot have. "
                        "search: a query -> videos, to feed --mode comments.")
    p.add_argument("--sort", choices=SORTS, default=DEFAULT_SORT,
                   help="Which of the site's two orderings to read. 'top' is "
                        "YouTube's relevance ranking and is what the site "
                        "selects; 'newest' is chronological. These are "
                        "DIFFERENT SAMPLES of the same video, not the same "
                        "rows reordered, so the ordering is a column and "
                        "diff_runs.py refuses to compare across it.")
    p.add_argument("--replies", action="store_true",
                   help="Also fetch each thread's replies. Costs one extra "
                        "request per thread that has any — about 20 more "
                        "requests per page of comments.")
    p.add_argument("--reply-pages", type=int, default=1,
                   help="How deep to follow one thread's replies, 10 per "
                        "page. Default 1.")
    p.add_argument("--pages", type=int, default=1,
                   help="Continuations to fetch. In --mode comments a page "
                        "is 20 top-level comments; in --mode search it is a "
                        "page of results; in --mode video it is capped by "
                        "how many videos --url named.")
    p.add_argument("--category", default=None,
                   help="Label to tag the run with in the sidecar. Defaults "
                        "to the video id or the query. YouTube has no "
                        "category route, so this names the RUN rather than a "
                        "site concept; the site's own category for a video "
                        "is the `category` column on a --mode video row.")
    p.add_argument("--locale", default=DEFAULT_LOCALE,
                   help="InnerTube `hl`. Default 'en', and that is load "
                        "bearing: a localised payload writes '1 год назад' "
                        "and '318 тыс.', which this parser refuses to guess "
                        "at rather than mis-dating every row. Comment TEXT "
                        "is unaffected either way.")
    p.add_argument("--region", default=DEFAULT_REGION,
                   help="InnerTube `gl`. Default 'US'.")
    p.add_argument("--format", choices=("json", "csv", "both"), default="json")
    p.add_argument("--out", default="youtube_comments",
                   help="Output file prefix.")
    p.add_argument("--delay", type=float, default=0.0,
                   help="Seconds to wait between requests.")
    p.add_argument("--retries", type=int, default=2,
                   help="Retries per request for a transient fault.")
    p.add_argument("--retry-delay", type=float, default=2.0)
    p.add_argument("--concurrency", type=int, default=1,
                   help="Workers. Usable only in --mode video, where a page "
                        "is one video and every video has its own address. "
                        "A comments run walks a token chain and is refused "
                        "more than one worker, with that reason.")
    p.add_argument("--proxy", default=None,
                   help="One proxy URL. Credentials go through the driver's "
                        "own fields, never onto a command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File of proxy URLs, one per line.")
    p.add_argument("--proxy-rotate", choices=ROTATE_MODES, default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="Exits to try when a request is refused.")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2Captcha API key. Also read from TWOCAPTCHA_KEY.")
    p.add_argument("--captcha-api", choices=("v1", "v2"), default="v2")
    p.add_argument("--solve-captcha", choices=("never", "when-blocked", "always"),
                   default="when-blocked",
                   help="when-blocked (default) pays only for a page that is "
                        "actually gated. No challenge has ever been observed "
                        "on this site from the address this repo was built "
                        "on, so this path is readiness rather than routine.")
    p.add_argument("--min-score", type=float, default=0.3,
                   help="Minimum reCAPTCHA v3 score to accept.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="ws:// endpoint of the 2Captcha Scraping Browser API. "
                        "Also read from YOUTUBE_CDP_ENDPOINT.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a device fingerprint from the 2Captcha "
                        "Fingerprint API and apply it.")
    p.add_argument("--fp-country", default=None)
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag. The API rejects a list, and "
                        "rejects 'Chrome' and 'Desktop' — measured, and the "
                        "reason this default is a single word.")
    p.add_argument("--dump-html", action="store_true",
                   help="Write the exact payloads a run received, on success "
                        "too. A run can return the right count with a field "
                        "silently unpopulated.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write an empty result instead of leaving the "
                        "previous good output in place.")
    headless = p.add_mutually_exclusive_group()
    headless.add_argument("--headless", dest="headless", action="store_true",
                          default=True)
    headless.add_argument("--headful", dest="headless", action="store_false")

    args = p.parse_args(argv)
    env_config.apply(args)

    if args.mode == "search":
        if not args.url:
            p.error("--mode search needs a query in --url.")
    else:
        if not args.url:
            p.error("no --url given, and YOUTUBE_URL is not set in the "
                    "environment or .env.")
        first = str(args.url).split(",")[0].strip()
        ok, reason = is_supported_url(first)
        if not ok:
            p.error(reason)

    if args.cdp_endpoint and (args.proxy or args.proxy_file):
        p.error("--cdp-endpoint already proxies; attaching --proxy to it "
                "stacks two exits and is refused on purpose.")
    if args.concurrency > 1:
        allowed = page_flow.concurrency_for_mode(args.mode, args.concurrency)
        if allowed < args.concurrency:
            logger.warning("--concurrency %d clamped to %d in --mode %s: a "
                           "page here is addressed by a token the PREVIOUS "
                           "page handed out, so a second worker would have "
                           "no address to fetch.",
                           args.concurrency, allowed, args.mode)
            args.concurrency = allowed
        limit = page_flow.concurrency_limit(args.cdp_endpoint)
        if limit and args.concurrency > limit:
            p.error("the Scraping Browser API allows one live connection per "
                    "profile, so workers collide (profile_locked). Use "
                    "several pids, one run each.")
    if args.reply_pages < 1:
        p.error("--reply-pages must be at least 1.")
    if args.category is None:
        args.category = (args.url if args.mode == "search"
                         else (video_id_from_url(str(args.url).split(",")[0])
                               or args.mode))
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint "
                     "API uses the same key, though it is a separate "
                     "subscription from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second creates a mismatch rather than "
                       "better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as exc:
        logger.error("%s", exc)
        sys.exit(2)
    except PWError as exc:
        text = _mask_credentials(exc)
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
