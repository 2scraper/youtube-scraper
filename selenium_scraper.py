#!/usr/bin/env python3
"""selenium_scraper.py — YouTube comments, video metadata and video search.

The Selenium twin of `playwright_scraper.py`. Everything from
`_prime_session` downwards is byte-identical to that file on purpose:
CLAUDE.md §6 requires all three engines to agree on exit codes, run status,
and whether a run crashes or spends money, and the only way to keep that
true is for the shared half to BE the same text.

    python3 selenium_scraper.py --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --pages 3
    python3 selenium_scraper.py --url dQw4w9WgXcQ --mode video

Two limits of this driver are stated here rather than left to be
discovered, because both are real and neither is this repo's fault:

* **Selenium cannot use an authenticated remote CDP endpoint.**
  Playwright's `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take
  a full `ws://user:pass@host:port` and authenticate on the WebSocket
  upgrade; chromedriver's `debuggerAddress` takes a bare `host:port` with
  nowhere to put a password. So `--cdp-endpoint` against the 2Captcha
  Scraping Browser API is refused here WITH THAT REASON, rather than
  failing later with an auth error a long way from its cause. Use the
  Playwright or pyppeteer engine for that path.
* **Selenium's `--proxy-server` cannot authenticate at all.** Credentials
  are stripped and a warning is printed; a user must not be left believing
  a `user:pass` URL is doing something (CLAUDE.md §6).

The InnerTube call is made by a `fetch` inside the page, through
`execute_async_script`. Note the dialect: Selenium runs a function BODY
with an explicit `return` and a callback as the last argument, where the
other two drivers take `() => expr`. That difference is exactly why no
JavaScript crosses into a shared module (CLAUDE.md §1).

The run itself lives in `run_core.py` and is shared with the other
two engines and with `youtube_scraper.py`. What stays here is what
touches Selenium: launching, connecting, the fingerprint, and the
session object. Measured on 2026-09-25, before the split: 24 of the
29 definitions the three engines shared were byte-identical.

The driver library is imported at MODULE level on purpose
(CLAUDE.md §10): the suite's skip and CI's `python -c "import
selenium_scraper"` both depend on this module failing to import when
Selenium is absent.
"""

import logging
import sys
from typing import Any, Dict, List, Optional, Tuple
from selenium.common.exceptions import (WebDriverException,
                                        TimeoutException as SETimeout)
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from product_parser import (CLIENT_VERSION_URL, DEFAULT_LOCALE, DEFAULT_REGION,
                            DEFAULT_SORT, FALLBACK_CLIENT_VERSION, SORTS,
                            canonical_video_url, client_version_from_text,
                            continuation_body, innertube_headers,
                            innertube_url, is_supported_url, parse_comments,
                            parse_search, parse_video, player_body,
                            search_body, video_body, video_id_from_url)
from proxy_pool import (from_args as proxy_pool_from_args, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import json
from selenium import webdriver

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

logger = logging.getLogger("selenium_scraper")
DriverError = (WebDriverException, SETimeout)
REQUEST_TIMEOUT_MS = 30_000
NAVIGATION_TIMEOUT_MS = 60_000
_FETCH_JS = """
var spec = arguments[0];
var done = arguments[arguments.length - 1];
var init = {method: spec.method, headers: spec.headers,
            credentials: 'include'};
if (spec.body) { init.body = spec.body; }
fetch(spec.url, init).then(function (response) {
  return response.text().then(function (text) {
    done({status: response.status, text: text});
  });
}).catch(function (err) {
  done({status: null, text: null, error: String(err)});
});
"""


class _BrowserSession:
    """A driver, a page on youtube.com, and a fetch primitive bound to it.

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

    def __init__(self, driver, proxy_url: Optional[str],
                 client_version: str, user_agent: Optional[str],
                 owns_driver: bool = True):
        # Always True here: this engine cannot connect to a remote browser
        # at all (see the module docstring), so it only ever ends a driver
        # it started. The flag exists so the three sessions carry the same
        # shape and `check_every_engine_exposes_the_same_public_surface`
        # can say so.
        self.owns_driver = owns_driver
        self.driver = driver
        self.browser = driver
        self.context = driver
        self.page = driver
        self.proxy_url = proxy_url
        self.client_version = client_version
        self.user_agent = user_agent

    # -- transport ---------------------------------------------------------

    def _fetch(self, url: str, method: str = "GET",
               headers: Optional[Dict[str, str]] = None,
               body: Optional[str] = None):
        spec = {"url": url, "method": method, "headers": headers or {},
                "body": body}
        try:
            self.driver.set_script_timeout(REQUEST_TIMEOUT_MS / 1000.0)
            return self.driver.execute_async_script(_FETCH_JS, spec)
        except DriverError as exc:
            raise _TransportError(_mask_credentials(exc)) from exc

    def get_text(self, url: str) -> Tuple[Optional[int], Optional[str]]:
        result = self._fetch(url) or {}
        return result.get("status"), result.get("text")

    def post_json(self, url: str, headers: Dict[str, str],
                  body: Dict[str, Any]) -> Tuple[Optional[int], Any]:
        result = self._fetch(url, "POST", headers, json.dumps(body)) or {}
        status, text = result.get("status"), result.get("text")
        if result.get("error"):
            raise _TransportError(_mask_credentials(result["error"]))
        try:
            return status, json.loads(text) if text else None
        except (TypeError, ValueError):
            # A refusal is not JSON. Hand the body back as text so the
            # classifier can name it rather than the run dying on a decode.
            return status, text

    def goto(self, url: str) -> Optional[int]:
        self.driver.set_page_load_timeout(NAVIGATION_TIMEOUT_MS / 1000.0)
        self.driver.get(url)
        # Selenium reports no HTTP status for a navigation. That is a real
        # gap on sites where the status IS the signal — but not here: every
        # response this engine classifies comes back through `_fetch`,
        # which carries the status from the page's own `fetch`. So nothing
        # is discarded; there is simply nothing to discard at this call.
        return None

    def content(self) -> str:
        try:
            return self.driver.page_source or ""
        except DriverError:
            return ""

    def count_selector(self, selector: str) -> int:
        try:
            return len(self.driver.find_elements(By.CSS_SELECTOR, selector))
        except DriverError:
            return 0

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run a function EXPRESSION in the page.

        Wrapped in `return (…)(arg)` because `execute_script` takes a
        function BODY, where the shared captcha module hands out `() =>
        expr`. Never an evaluated string on the page's own terms: YouTube's
        Content-Security-Policy has no `unsafe-eval` (CLAUDE.md §18), and
        `execute_script` goes through the WebDriver protocol rather than
        through the page's `eval`.
        """
        try:
            if arg is not None:
                return self.driver.execute_script(
                    f"return ({js})(arguments[0]);", arg)
            return self.driver.execute_script(f"return ({js})();")
        except DriverError:
            return None

    @property
    def url(self) -> str:
        try:
            return self.driver.current_url or ""
        except Exception:
            return ""

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass

class RemoteBrowserError(RuntimeError):
    """The remote-browser path is unavailable from this driver."""

def _launch_local(pw, args, pool: Optional[ProxyPool]) -> _BrowserSession:
    """A local Chromium, optionally behind one exit from the pool."""
    from proxy_pool import split_credentials

    proxy_url = pool.current if pool else (args.proxy or None)
    options = ChromeOptions()
    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,900")

    if proxy_url:
        host_only, username, _password = split_credentials(proxy_url)
        if username:
            # Said out loud rather than silently dropped. Selenium cannot
            # authenticate a proxy at all, and a user who passed a
            # `user:pass` URL must not be left believing it is doing
            # something (CLAUDE.md §6).
            logger.warning("Selenium cannot authenticate a proxy: the "
                           "credentials in --proxy have been STRIPPED and "
                           "only %s is in use. If this exit needs a "
                           "password, use the Playwright or pyppeteer "
                           "engine.", mask(proxy_url))
        options.add_argument(f"--proxy-server={host_only}")

    user_agent = None
    fingerprint = None
    if args.fingerprint:
        from fingerprint_client import get_fingerprint, fingerprint_user_agent
        fingerprint = get_fingerprint(args.twocaptcha_key, tags=args.fp_tags,
                                      country=args.fp_country)
        user_agent = fingerprint_user_agent(fingerprint)
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    driver = webdriver.Chrome(options=options)
    if not user_agent:
        version = (driver.capabilities or {}).get("browserVersion", "")
        user_agent = _chrome_ua(version)
    if fingerprint is not None:
        _apply_fingerprint(driver, fingerprint, user_agent)
    return _BrowserSession(driver, proxy_url, FALLBACK_CLIENT_VERSION,
                           user_agent)

def _apply_fingerprint(driver, fingerprint, user_agent) -> None:
    """Give the identity everything the fingerprint states, not just a UA.

    `--user-agent=` on the command line is a BARE override: it changes
    `navigator.userAgent` and leaves `navigator.userAgentData` and the
    `Sec-CH-UA` header reporting the real browser. CLAUDE.md §24 measured
    that half-identity being refused where a complete one was served, so
    this engine applies the same set its twins do.

    Best effort throughout — a fingerprint is cover, and no run should die
    because cover was imperfect.
    """
    from fingerprint_client import (user_agent_metadata, accept_language,
                                    playwright_init_script)

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": playwright_init_script(fingerprint)})
    except DriverError as exc:
        logger.warning("Could not install the fingerprint's init script: %s",
                       _mask_credentials(exc))

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
        driver.execute_cdp_cmd("Network.setUserAgentOverride", payload)
        timezone = (fingerprint.get("intl") or {}).get("timeZone")
        if timezone:
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                   {"timezoneId": timezone})
    except DriverError as exc:
        logger.warning("Could not apply the fingerprint's client hints (%s) — "
                       "the run continues, but navigator.userAgentData will "
                       "disagree with the user agent.",
                       _mask_credentials(exc))

def _connect_remote(pw, args) -> _BrowserSession:
    """Refused, with the reason — see this module's docstring.

    chromedriver's `debuggerAddress` takes a bare `host:port` and has
    nowhere to put a password, so the 2Captcha Scraping Browser endpoint —
    `ws://{login}-zone-…:{password}@cb.2captcha.com:9222` — cannot be used
    from here. Reporting that plainly is the whole point: the alternative
    is an auth failure several steps away from its cause.
    """
    raise RemoteBrowserError(
        "--cdp-endpoint is not usable from the Selenium engine: "
        "chromedriver's debuggerAddress takes a bare host:port and cannot "
        "carry the endpoint's credentials. Use playwright_scraper.py or "
        "puppeteer_scraper.py for the Scraping Browser API.")


class _Driver(run_core.Driver):
    """Selenium, behind the four operations `run_core` needs."""

    name = "selenium"

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
        self.handle = None     # Selenium needs no driver-level handle
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
