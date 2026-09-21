"""page_flow.py — the retry / solve / blocked decision, as DATA.

Shared by all three browser engines and the HTTP path so they cannot
quietly disagree about whether a response is worth retrying, worth paying
a solver for, or worth reporting as a block. Three copies of that triage
drift, and the drift is silent: one engine reporting exit 3 where its twin
reports exit 0 on the same video (CLAUDE.md §1).

Policy and pure algorithms only. No JavaScript crosses this boundary —
Selenium's `execute_script` takes a function BODY with an explicit
`return` while Playwright and pyppeteer take `() => expr`, so a shared
module that carried a snippet would acquire one driver's dialect. What the
engines share here is a NAME for an operation and a decision about it.

YouTube answers a request six ways
----------------------------------
and five of them want a different response, which is why this module
exists on this site at all:

    content             a payload with comments in it
    empty               a continuation that returned none — the end
    comments_disabled   the video takes no comments (a real answer)
    video_unavailable   the video is gone (also a real answer)
    challenge           a refusal
    parse_error         a served payload we failed to read — OUR bug

The middle two are the ones a naive engine gets wrong, because both answer
HTTP 200 with a large, healthy-looking payload. Reporting either as a block
sends a user rotating proxies over a video that simply has its comments
switched off.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from product_parser import (STATE_CHALLENGE, STATE_COMMENTS_DISABLED,
                            STATE_CONTENT, STATE_EMPTY, STATE_ERROR,
                            STATE_UNKNOWN, STATE_VIDEO_UNAVAILABLE,
                            detect_page_state)

# ---------------------------------------------------------------------------
# Readiness — for the browser engines only
# ---------------------------------------------------------------------------
#
# The browser engines do not read comments out of the DOM. They load a page
# on the site's own origin and POST the InnerTube call from inside it, so
# what they wait for is not a rendered grid but a document that can issue a
# same-origin request. That is a much weaker requirement than every other
# repo in this family has, and stating it here keeps an engine from growing
# a comment-counting wait that measures the wrong thing.
#
# `#movie_player` is the player container and appears once the watch page's
# own script has run. A run that never sees it still works — the fetch is
# tried anyway — so this is a wait, not a gate.
READY_SELECTOR = "#movie_player, ytd-app, body"
READY_SELECTOR_SEARCH = "ytd-app, body"
MIN_CARD_MATCHES = 1
CONTENT_TIMEOUT_MS = 30_000
READY_POLL_MS = 500


def ready_selector(mode: str = "comments") -> str:
    return READY_SELECTOR_SEARCH if mode == "search" else READY_SELECTOR


def min_matches(mode: str = "comments") -> int:
    return MIN_CARD_MATCHES


def content_timeout_ms(mode: str = "comments") -> int:
    return CONTENT_TIMEOUT_MS


def wait_for_count(count: Callable[[str], int], selector: str, minimum: int,
                   timeout_ms: int = CONTENT_TIMEOUT_MS,
                   poll_ms: int = READY_POLL_MS) -> int:
    """Poll `count(selector)` until it reaches `minimum`, or time out.

    The caller passes a counting primitive rather than a snippet, and the
    primitive must be a protocol call — `querySelectorAll` through the
    driver — never an evaluated STRING. CLAUDE.md §18: a site whose
    Content-Security-Policy lacks `unsafe-eval` kills
    `wait_for_function`-style string evaluation, and YouTube's CSP is
    strict. Polling a real function object goes through
    `Runtime.callFunctionOn` and works under any CSP, in all three drivers.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    seen = 0
    while True:
        try:
            seen = count(selector)
        except Exception:
            seen = 0
        if seen >= minimum or time.time() >= deadline:
            return seen
        time.sleep(poll_ms / 1000.0)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(payload, status: Optional[int] = None, url: str = "",
             mode: str = "comments", endpoint: str = "next") -> str:
    """Name what YouTube answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every caller writes
    `classify(payload, status, url)`. CLAUDE.md §17 records a sibling repo
    whose two engines called `classify(html, url=…)` against a callee
    taking `status` second, and both crashed on their FIRST fetch —
    invisible to import, `--help`, `compileall` and four hundred green
    offline assertions, because none of those calls a function the way a
    live run does. `smoke_test.py` binds every call site against this
    signature for exactly that reason.

    `status` is threaded through rather than dropped. A 429 is the clearest
    statement of a refusal this site can make, and a classifier that never
    receives it has to guess from the body.
    """
    # `endpoint` is not decoration. `/player` and `/search` payloads have
    # no comment section by design, and a classifier that expects one calls
    # a perfectly good `/player` response `unknown` — which the first live
    # `--mode video` run then retried twice before writing the row anyway.
    # Two wasted requests per video, and a log that read like a fault.
    expect = mode != "search" and endpoint == "next"
    return detect_page_state(payload, status, expect_comments=expect)


STATE_POLICY = {
    # A payload with comments in it.
    STATE_CONTENT: {"retry": False, "solve": False, "blocked": False,
                    "parse": True},
    # A continuation that came back with no comments. On this site that is
    # the END of the thread or of the listing rather than a fault: the site
    # hands out a token for a page that turns out to hold nothing when a
    # run reaches the last one. Not blocked, not retried, and the rows
    # already gathered stand.
    STATE_EMPTY: {"retry": False, "solve": False, "blocked": False,
                  "parse": True},
    # The video takes no comments. A real, complete answer to the question
    # asked — EXIT_NO_PRODUCTS, never EXIT_BLOCKED. Retrying it re-asks a
    # question the site has already answered.
    STATE_COMMENTS_DISABLED: {"retry": False, "solve": False,
                              "blocked": False, "parse": False},
    # The video is private, deleted or was never there. Also a real answer,
    # and also not a block: a user rotating proxies over this would be
    # chasing a video that does not exist.
    STATE_VIDEO_UNAVAILABLE: {"retry": False, "solve": False,
                              "blocked": False, "parse": False},
    # A refusal. Never observed from the address this repo was built on —
    # 60 consecutive InnerTube pages drew none — so `solve` being True is a
    # readiness rather than a measurement: if YouTube starts demanding a
    # sign-in challenge tomorrow, the run meets it with the tools already
    # here instead of needing a code change. `retry` is True because on
    # every site in this family that DOES refuse, a different exit clears
    # it far more cheaply than a solve does.
    STATE_CHALLENGE: {"retry": True, "solve": True, "blocked": True,
                      "parse": False},
    # An HTTP error that is not a recognised refusal — a 500, a gateway's
    # own page, a truncated body. A wait, not a spend.
    STATE_ERROR: {"retry": True, "solve": False, "blocked": False,
                  "parse": False},
    # A payload the site plainly served, with a comment section in it, that
    # parsed to zero rows. That is OUR bug, not an empty video, and it gets
    # its own name so it cannot be reported as "no comments" — which would
    # send the reader to check the video id instead of the parser
    # (CLAUDE.md §20). One retry in case a response was truncated, and
    # always worth a dump.
    "parse_error": {"retry": True, "solve": False, "blocked": False,
                    "parse": False},
    # Neither the site nor a recognised refusal — a proxy's own response,
    # Chromium's network-error page, an upstream error.
    STATE_UNKNOWN: {"retry": True, "solve": False, "blocked": False,
                    "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by the
# engines rather than merely documented — setting it False really does stop
# the retry loop. (CLAUDE.md §17: a policy constant nothing reads is the
# same defect as dead code, and this family shipped one for months.)
RETRY_ON_BLOCKED = True

# Retries to spend on a refusal when there is no pool to rotate through.
# Without a pool every retry leaves from the same address that was just
# refused, so more than one is repetition rather than a second attempt.
BLOCK_RETRIES_WITHOUT_POOL = 1

# ---------------------------------------------------------------------------
# The solve budget
# ---------------------------------------------------------------------------
#
# One paid solve per page. CLAUDE.md §23 records that this constant has
# read like an enforced limit in every repo in this family and was not one:
# every engine calls the captcha handler TWICE per attempt — once before
# the response is classified, so a challenge is cleared before anything is
# judged, and once after, for the state that says the page really is gated
# — and only the second call was counted. Measured from an address where a
# challenge rendered on every fetch, one page bought THREE solves.
#
# So the budget is not a number engines are trusted to respect. It is a
# function both call sites go through, and `smoke_test.py` asserts that the
# number of call sites equals the number of guards equals the number of
# increments.
SOLVES_PER_PAGE = 1


class SolveBudget:
    """One page's paid-solve allowance, shared by both call sites.

    `spend()` returns True at most SOLVES_PER_PAGE times and counts the
    spend itself, so neither caller can forget to.
    """

    def __init__(self, limit: int = SOLVES_PER_PAGE):
        self.limit = max(0, int(limit))
        self.spent = 0

    def may_spend(self) -> bool:
        return self.spent < self.limit

    def spend(self) -> bool:
        if not self.may_spend():
            return False
        self.spent += 1
        return True

    def __repr__(self) -> str:                # pragma: no cover - debugging
        return f"SolveBudget(spent={self.spent}/{self.limit})"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def pagination_is_addressable(url: str = "", mode: str = "comments") -> bool:
    """Whether page N of this run can be fetched without walking to it.

    CLAUDE.md §18 says to ask this per ROUTE rather than per site, and on
    YouTube the three modes answer differently:

      * `comments` — NO, and not by a near miss. A page is addressed by an
        opaque continuation token that the site hands out inside the page
        before it, and page 5's token is unknowable until page 4 has been
        read. There is no `?page=N` to construct. This repo deliberately
        does not build the token itself: it is a protobuf of a private
        encoding, and a hand-built one fails by returning the WRONG
        ordering rather than by erroring.
      * `search` — NO, for the same reason.
      * `video` — YES, and this is where the word earns its keep. A "page"
        in `--mode video` is one VIDEO, and every video has a real,
        independent address. That is what the concurrency machinery needs
        and what makes the sidecar's "which pages failed by number" mean
        something.

    So a comments run is strictly sequential and says why, and a video run
    may use every worker it is given.
    """
    return mode == "video"


def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given what is known to exist.

    YouTube states no page count for a comment thread — only a total
    comment count, from which a page count could be DIVIDED but should not
    be: the site stops serving continuations well before the arithmetic
    says it should, and a run that trusted the division would report
    missing pages that were never available. So `pages_available` is None
    for a comments run and the cap is simply what the caller asked for;
    the run ends when the site stops handing out tokens.

    In `--mode video` it is the number of videos enumerated, and capping at
    it matters: asking for the eleventh of ten videos is not an empty page,
    it is an index error waiting to happen.
    """
    wanted = max(1, int(pages_requested or 1))
    if pages_available and pages_available > 0:
        return min(wanted, int(pages_available))
    return wanted


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (CLAUDE.md §7).
    """
    return 1 if cdp_endpoint else None


def concurrency_for_mode(mode: str, concurrency: int) -> int:
    """Workers this mode can actually use.

    `comments` and `search` walk a token chain, so a second worker would
    have no address to fetch — it would sit waiting for a token the first
    worker has not produced yet. Clamping here rather than starting idle
    threads keeps the run's own log honest about what it did.
    """
    if mode in ("comments", "search"):
        return 1
    return max(1, int(concurrency or 1))


def sample_share(collected: int, total: Optional[int]) -> Optional[float]:
    """What percentage of the video's comments a run actually holds.

    CLAUDE.md §21: "complete" and "exhaustive" are different words, and a
    sidecar that says only the first is lying by omission. A five-page run
    of a video with 2,457,619 comments is complete — every page it asked
    for arrived — and is 0.004% of the comments. This number goes in the
    sidecar and into the engine's closing log so nobody has to work it out.
    """
    if not total or total <= 0 or collected < 0:
        return None
    return round(100.0 * collected / total, 4)
