"""youtube.com — comments, video metadata and search results.

The site
========
YouTube renders nothing a parser wants. A `/watch` page is ~1.4 MB of
JavaScript with the player state inlined as `ytInitialData`, and the
comments are NOT in it at all — not as markup, not as JSON. The page ships
a *continuation token* and the browser then asks for the comments over the
site's own private API.

So this repo follows CLAUDE.md §21: ask what the FRONT END calls before
assuming a browser is needed. The answer is

    POST https://www.youtube.com/youtubei/v1/next
    {"context": {"client": {"clientName": "WEB", "clientVersion": "…"}},
     "continuation": "<token>"}

and measured 2026-09-21 from a bare datacentre shell in Finland, that
endpoint answers **HTTP 200 with no API key, no cookies, no proxy and no
browser**: 60 sequential pages, 1,200 comments, zero refusals, ~0.25 s per
page. The 1.4 MB HTML page is needed for nothing.

Two request shapes cover every mode:

    {"videoId": "<11 chars>"}     the watch payload: video metadata, plus
                                  the comment section's first continuation
                                  token and the sort menu's two tokens
    {"continuation": "<token>"}   one page of comments, or one page of one
                                  thread's replies

Where the data actually lives
=============================
Not where a reader of the renderer tree would look. `commentThreadRenderer`
holds only KEYS and ordering:

    {"commentViewModel": {"commentViewModel": {"commentKey": "Ehp…",
                                               "toolbarStateKey": "Ehp…"}}}

and the comment itself — text, author, like count, timestamp — arrives in a
flat side-channel, `frameworkUpdates.entityBatchUpdate.mutations`, as a
`commentEntityPayload` whose `key` matches. A parser that walks the
renderers and never joins the mutations finds twenty empty shells and
reports twenty comments with every column null. So `parse_comments` reads
the mutations FIRST, indexes them by key, and walks the renderer tree only
for the order, the pinned badge and the replies token.

Two shapes, because the site serves both (CLAUDE.md §4)
======================================================
The `WEB` client returns the entity form above. The `MWEB` client returns
the LEGACY `commentRenderer` form — `contentText.runs`, `voteCount.runs`,
`publishedTimeText.runs` — which is what every older scraper for this site
was written against. Measured 2026-09-21: WEB 20 entity payloads and 0
legacy renderers, MWEB 0 and 20.

That is not a hypothetical fallback. YouTube has been migrating clients to
the entity form one at a time, and a client can be moved back. So the
entity path is primary, the legacy path runs only when the entity path
yields nothing, and both are pinned to a real captured fixture. If YouTube
flips the WEB client back tomorrow this repo keeps working and says which
shape it read in `data_source`.

What this site does NOT publish
===============================
Counted over 70 comments in four captures, and worth knowing before
choosing this over the official Data API:

* **No absolute timestamp.** `publishedTime` is the rendered relative
  string — "1 year ago" — and there is no ISO date anywhere in the
  payload, under any client (WEB, MWEB, TVHTML5 all checked; ANDROID and
  IOS answer HTTP 400 without device attestation). `published_at_approx`
  is therefore derived and labelled as such; see `relative_time`.
* **No exact like count.** `likeCountNotliked` is "318K" and the
  accessibility string agrees ("318K likes"). The exact figure is not
  published in any field of any client.

Both ARE exact on a VIDEO row, which is the asymmetry that makes
`--mode video` worth having: `1,818,222,351 views`, `19,384,917` likes out
of the like button's accessibility label, and `Oct 24, 2009`. Read the
number BEFORE the count word there, never by stripping digits out of the
label — CLAUDE.md §10 records a sibling repo shipping `445279961` on every
row for exactly that reason.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

SOURCE = "youtube.com"

# Every host that can carry a video id. `youtu.be` puts the id in the path
# and `music.youtube.com` answers on `/watch?v=` like the main site; both
# resolve to the same InnerTube call, so they are accepted and normalised
# rather than refused.
HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com",
         "music.youtube.com", "youtu.be", "www.youtu.be")
CANONICAL_HOST = "www.youtube.com"

# ---------------------------------------------------------------------------
# The InnerTube endpoint
# ---------------------------------------------------------------------------

INNERTUBE_ORIGIN = "https://www.youtube.com"
INNERTUBE_PATH = "/youtubei/v1/{endpoint}"
INNERTUBE_ENDPOINTS = ("next", "player", "search")

# The client this repo speaks. WEB is the only one of the five tested that
# both answers without device attestation AND returns the current entity
# form. MWEB works and returns the legacy form; ANDROID and IOS answer
# `400 Precondition check failed`; TVHTML5 answers 200 with no comment
# section at all.
CLIENT_NAME = "WEB"
CLIENT_ID = "1"

# A FALLBACK only. The live version is read from `/sw.js_data` (2.8 KB)
# once per run — see `client_version_from_text`. CLAUDE.md §8 says the user
# agent comes from the browser rather than from a literal, and a client
# version is the same kind of claim: a hardcoded one drifts away from what
# the site is actually serving. This constant exists so a run still starts
# when that fetch fails, and the run says which one it used.
FALLBACK_CLIENT_VERSION = "2.20260918.00.00"
CLIENT_VERSION_URL = "https://www.youtube.com/sw.js_data"
_CLIENT_VERSION_RE = re.compile(r"\b(2\.\d{8}\.\d{2}\.\d{2})\b")

# Forced to English, and that is a decision rather than an oversight.
# Measured on one video in two locales: `hl=ru` returns the SAME comments
# with `publishedTime` "1 год назад" and the like count "318 тыс." — the
# abbreviation suffix is localised. Comment TEXT is user-written and is
# identical either way, so English costs nothing and buys a parseable
# relative time and an ASCII count suffix. `--locale` overrides it, and
# `parse_count` returns None rather than guessing when the suffix is not
# one it knows (CLAUDE.md §8: never present a guess as a fact).
DEFAULT_LOCALE = "en"
DEFAULT_REGION = "US"

# The site's own two orderings, in the order its menu lists them.
# `top` is YouTube's relevance ranking and is what the site selects; a run
# under one sort and a run under the other are DIFFERENT SAMPLES of the
# same video, not the same rows in a different order, so `sort` is a column
# and `diff_runs.py` refuses to compare across it (CLAUDE.md §21).
SORTS = ("top", "newest")
DEFAULT_SORT = "top"
_SORT_TITLE_PREFIX = {"top": "top", "newest": "new"}

# Measured 2026-09-21 over 60 consecutive pages: exactly 20 top-level
# comments per page and 10 replies per reply page, without exception.
PAGE_SIZE = 20
REPLY_PAGE_SIZE = 10

# The section identifier the site gives its own comment section. This is
# the anchor for everything below: it is a stable, meaningful name the site
# chose, not a build hash, which is CLAUDE.md §4's rule about anchoring on
# a contract rather than on a class.
COMMENT_SECTION_ID = "comment-item-section"

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

# A YouTube video id is 11 characters of URL-safe base64. Anchored, because
# an unanchored match finds one inside any long token.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# The path forms that carry an id directly rather than in the query string.
_PATH_ID_RE = re.compile(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})")


def video_id_from_url(url: str) -> Optional[str]:
    """The 11-character video id in `url`, or None.

    Accepts every form the site itself hands out: `/watch?v=`, `youtu.be/`,
    `/shorts/`, `/live/`, `/embed/`, and a bare id typed on the command
    line. Returns None for a channel, a playlist or a search URL — those
    are not videos and the caller must say so rather than fetching
    something that will parse to nothing.
    """
    if not url:
        return None
    raw = url.strip()
    if _VIDEO_ID_RE.match(raw):
        return raw
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    try:
        parts = urlparse(raw)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host not in HOSTS:
        return None
    if host.endswith("youtu.be"):
        seg = parts.path.strip("/").split("/")[0] if parts.path else ""
        return seg if _VIDEO_ID_RE.match(seg) else None
    m = _PATH_ID_RE.match(parts.path or "")
    if m:
        return m.group(1)
    vals = parse_qs(parts.query or "").get("v") or []
    if vals and _VIDEO_ID_RE.match(vals[0]):
        return vals[0]
    return None


def is_supported_url(url: str) -> Tuple[bool, str]:
    """(supported, reason). The reason is shown to the user, so it says WHY.

    CLAUDE.md §5: refusing a host with a false reason sends the reader
    hunting for a typo. A YouTube channel URL is a perfectly good YouTube
    URL that this tool cannot take, and saying "not a YouTube URL" would be
    a lie about it.
    """
    if not url or not url.strip():
        return False, "no URL given"
    raw = url.strip()
    if _VIDEO_ID_RE.match(raw):
        return True, ""
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return False, f"{url!r} is not a URL"
    if host not in HOSTS:
        return False, (f"{host or url!r} is not a YouTube host — this "
                       f"scraper reads {', '.join(HOSTS[:3])} and youtu.be")
    if video_id_from_url(raw):
        return True, ""
    return False, (f"{url} is a YouTube URL but carries no video id — a "
                   f"channel, playlist or search page has no comment "
                   f"section of its own. Pass a /watch, /shorts, /live or "
                   f"youtu.be URL, or a bare 11-character video id.")


def canonical_video_url(video_id: str) -> str:
    """The `/watch?v=` form, whichever form the caller passed in."""
    return f"{INNERTUBE_ORIGIN}/watch?v={video_id}"


def comment_permalink(video_id: str, comment_id: str) -> str:
    """The site's own deep link to one comment.

    The payload publishes this itself, under
    `commentSurfaceEntityPayload.publishedTimeCommand…url`, and
    `parse_comments` prefers that value. This function is the fallback for
    the legacy shape, which publishes no permalink at all.
    """
    return f"{INNERTUBE_ORIGIN}/watch?v={video_id}&lc={comment_id}"


# ---------------------------------------------------------------------------
# Building a request — pure data, so every engine can issue it its own way
# ---------------------------------------------------------------------------
#
# CLAUDE.md §1: deliberately let no JavaScript cross this boundary. The
# three browser engines each POST this by a different mechanism —
# Playwright's APIRequestContext, a `fetch` inside the page for pyppeteer,
# an async script for Selenium — and the HTTP path uses `requests`. All
# four get the same url/headers/body out of here, so the site knowledge
# stays in one file and only the dialect differs.


def innertube_url(endpoint: str = "next") -> str:
    if endpoint not in INNERTUBE_ENDPOINTS:
        raise ValueError(f"unknown InnerTube endpoint {endpoint!r}")
    return (INNERTUBE_ORIGIN + INNERTUBE_PATH.format(endpoint=endpoint)
            + "?prettyPrint=false")


def innertube_headers(client_version: str) -> Dict[str, str]:
    """Headers the endpoint expects.

    No API key. The `key=` query parameter every guide for this endpoint
    shows is not required — measured 2026-09-21, the endpoint answers 200
    without it — and leaving it out is also the safer shape: CLAUDE.md §8
    records that `requests` puts the full URL including its query string
    into the text of every error it raises, so a key in a query parameter
    leaks the moment anything goes wrong.
    """
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Youtube-Client-Name": CLIENT_ID,
        "X-Youtube-Client-Version": client_version,
        "Origin": INNERTUBE_ORIGIN,
        "Referer": INNERTUBE_ORIGIN + "/",
    }


def innertube_context(client_version: str, locale: str = DEFAULT_LOCALE,
                      region: str = DEFAULT_REGION) -> Dict[str, Any]:
    return {"client": {"clientName": CLIENT_NAME,
                       "clientVersion": client_version,
                       "hl": locale, "gl": region}}


def video_body(video_id: str, client_version: str,
               locale: str = DEFAULT_LOCALE,
               region: str = DEFAULT_REGION) -> Dict[str, Any]:
    """The watch payload: metadata, the comment token and the sort menu."""
    return {"context": innertube_context(client_version, locale, region),
            "videoId": video_id}


def continuation_body(token: str, client_version: str,
                      locale: str = DEFAULT_LOCALE,
                      region: str = DEFAULT_REGION) -> Dict[str, Any]:
    """One page of comments, one page of replies, or a re-sort."""
    return {"context": innertube_context(client_version, locale, region),
            "continuation": token}


def search_body(query: str, client_version: str,
                locale: str = DEFAULT_LOCALE,
                region: str = DEFAULT_REGION) -> Dict[str, Any]:
    """`--mode search`: a query, filtered to videos.

    `params` is the site's own base64 filter for "type: video", taken from
    its own filter menu. Without it the payload mixes in channels,
    playlists and shelves, and every one of those parses to a row with no
    video id.
    """
    return {"context": innertube_context(client_version, locale, region),
            "query": query, "params": "EgIQAQ%3D%3D"}


def client_version_from_text(text: Optional[str]) -> Optional[str]:
    """Pull the live client version out of `/sw.js_data` or a watch page.

    `/sw.js_data` is 2.8 KB against the watch page's 1.4 MB and states the
    same version, so it is what the engines fetch. Both are accepted here
    because the browser engines already have a page loaded and should not
    pay for a second request.
    """
    if not text:
        return None
    m = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', text)
    if m:
        return m.group(1)
    m = _CLIENT_VERSION_RE.search(text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Walking the payload
# ---------------------------------------------------------------------------


def _walk(node: Any, key: str, out: Optional[List[Any]] = None) -> List[Any]:
    """Every value stored under `key`, anywhere in the tree, in tree order."""
    if out is None:
        out = []
    if isinstance(node, dict):
        if key in node:
            out.append(node[key])
        for value in node.values():
            _walk(value, key, out)
    elif isinstance(node, list):
        for value in node:
            _walk(value, key, out)
    return out


def _text(node: Any) -> Optional[str]:
    """The three ways this payload spells a string, and None for the rest."""
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return None
    if "simpleText" in node:
        return node.get("simpleText")
    if "runs" in node and isinstance(node["runs"], list):
        return "".join(r.get("text", "") for r in node["runs"]
                       if isinstance(r, dict))
    if "content" in node and isinstance(node["content"], str):
        return node["content"]
    return None


# Abbreviation suffixes, ASCII only and deliberately so. A localised
# payload writes "318 тыс." or "318 Mio."; this table does not know those
# and `parse_count` returns None for them rather than inventing a number.
_COUNT_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000,
                 "t": 1_000_000_000_000}
_COUNT_RE = re.compile(r"(\d[\d,.    ]*)\s*([kmbt])?\b",
                       re.IGNORECASE)


def parse_count(text: Optional[str]) -> Optional[int]:
    """"318K" -> 318000, "1,818,222,351" -> 1818222351, "318 тыс." -> None.

    An abbreviated figure is returned as its stated magnitude and is
    APPROXIMATE by construction: "318K" is anything from 317,500 to
    318,499, and the site publishes nothing better for a comment. The raw
    string is kept in its own column beside every number this produces, so
    a consumer can always see what was actually said (CLAUDE.md §8).

    Grouping separators follow the family's price rules: a dot or comma
    with exactly three trailing digits is a thousands group, and the space
    forms include NBSP and narrow NBSP because a rendered page uses a
    no-break variant so the number cannot wrap.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    raw = str(text).strip()
    if not raw:
        return None
    m = _COUNT_RE.search(raw)
    if not m:
        return None
    digits, suffix = m.group(1).strip(), (m.group(2) or "").lower()
    # A localised suffix sits right after the number and is not one of
    # ours: refuse rather than silently returning the mantissa, which would
    # report a 318,000-like comment as having 318 likes.
    tail = raw[m.end():].lstrip()
    if not suffix and tail[:1].isalpha() and not _looks_like_unit_word(tail):
        return None
    body = digits.replace(" ", "").replace(" ", "")
    body = body.replace(" ", "").replace(" ", "")
    if suffix:
        # An abbreviated figure is written "1.8B" or "1,8 Mio" — the
        # separator is decimal, never a thousands group.
        body = body.replace(",", ".")
        try:
            return int(round(float(body) * _COUNT_SUFFIX[suffix]))
        except ValueError:
            return None
    if "," in body and "." in body:
        body = (body.replace(",", "") if body.rfind(".") > body.rfind(",")
                else body.replace(".", "").replace(",", "."))
    elif body.count(",") >= 1:
        parts = body.split(",")
        body = "".join(parts) if all(len(p) == 3 for p in parts[1:]) else \
            body.replace(",", ".")
    elif body.count(".") >= 1:
        parts = body.split(".")
        if all(len(p) == 3 for p in parts[1:]):
            body = "".join(parts)
    try:
        return int(round(float(body)))
    except ValueError:
        return None


# The English words that may follow a count without meaning it is a
# localised abbreviation: "1,234 likes", "962 replies", "20 views".
_UNIT_WORDS = ("like", "repl", "view", "comment", "subscriber", "other",
               "person", "people")


def _looks_like_unit_word(tail: str) -> bool:
    low = tail.lower()
    return any(low.startswith(w) for w in _UNIT_WORDS)


# ---------------------------------------------------------------------------
# Relative time
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(
    r"(?:edited\s*)?(\d+)\s+"
    r"(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE)
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400,
                 "week": 604_800, "month": 2_629_746, "year": 31_556_952}


def relative_time(text: Optional[str], now_iso: Optional[str] = None
                  ) -> Tuple[Optional[str], Optional[str]]:
    """"1 year ago" -> ("2025-09-21T…Z", "year"). Unknown wording -> (None, None).

    The returned instant is the LATEST moment the comment could have been
    written: YouTube floors, so "1 year ago" means an age in [1, 2) years
    and therefore a date in (now-2y, now-1y]. Pairing it with the precision
    is what keeps this from being a guess dressed as a fact — a consumer
    filtering "posted after March" can see that a row precise only to the
    year cannot answer the question.

    Returns (None, None) for any wording it does not recognise, which
    includes every non-English locale. That is the point: a `--locale ru`
    run keeps the verbatim string in `published_time_text` and leaves the
    derived column empty rather than mis-dating every row.
    """
    if not text:
        return None, None
    m = _RELATIVE_RE.search(str(text))
    if not m:
        return None, None
    qty, unit = int(m.group(1)), m.group(2).lower()
    from datetime import datetime, timedelta, timezone
    if now_iso:
        try:
            base = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            base = datetime.now(timezone.utc)
    else:
        base = datetime.now(timezone.utc)
    stamp = base - timedelta(seconds=qty * _UNIT_SECONDS[unit])
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), unit


def is_edited(text: Optional[str]) -> Optional[bool]:
    """The site marks an edited comment inside its timestamp string.

    Measured on the captures: the marker is a SUFFIX — "3 days ago
    (edited)", "6 years ago (edited)" — not a prefix. A prefix check reads
    False on every edited comment and looks like it works, because the
    column is False on most rows either way.
    """
    if not text:
        return None
    return "(edited)" in str(text).lower()


# ---------------------------------------------------------------------------
# UTF-16 offsets
# ---------------------------------------------------------------------------


def utf16_span(text: str, start: int, length: int) -> str:
    """The substring `start..start+length` in UTF-16 CODE UNITS.

    The payload's `attachmentRuns`, `commandRuns` and `styleRuns` index
    into the comment text the way JavaScript does — in UTF-16 code units —
    and every emoji outside the BMP counts as two. Slicing the Python
    string with those numbers is wrong the moment a comment contains one
    emoji, and silently so: measured on a real fixture, a run of
    `start=42 length=2` covering exactly one crying-face emoji slices as
    TWO emoji in Python, and the third run of the same comment slices to
    the empty string because its start is already past the end.

    Nine of seventy captured comments carry such a run, so this is the
    normal case rather than an edge one.
    """
    if not text:
        return ""
    units = text.encode("utf-16-le")
    return units[start * 2:(start + length) * 2].decode("utf-16-le", "replace")


# ---------------------------------------------------------------------------
# Finding things in a watch payload
# ---------------------------------------------------------------------------


def comments_section(payload: Any) -> Optional[Dict[str, Any]]:
    """The video's own comment section, by the name the site gives it."""
    for section in _walk(payload, "itemSectionRenderer"):
        if isinstance(section, dict) and \
                section.get("sectionIdentifier") == COMMENT_SECTION_ID:
            return section
    return None


def comments_token(payload: Any) -> Optional[str]:
    """The first continuation token for the comment section, or None.

    None has exactly one meaning on a payload that HAS a comment section:
    the video's comments are turned off. The site puts a `messageRenderer`
    saying so where the token would be. See `detect_page_state`.
    """
    section = comments_section(payload)
    if section is None:
        return None
    for command in _walk(section, "continuationCommand"):
        if isinstance(command, dict) and command.get("token"):
            return command["token"]
    return None


def sort_tokens(payload: Any) -> Dict[str, str]:
    """{"top": token, "newest": token} from the site's own sort menu.

    Read from the menu rather than constructed, because the token encodes
    the video id and a sort index in a protobuf this repo deliberately does
    not build by hand: a hand-built token is a guess about a private
    encoding, and when the encoding changes it fails by returning the WRONG
    ordering rather than by erroring.
    """
    out: Dict[str, str] = {}
    for menu in _walk(payload, "sortFilterSubMenuRenderer"):
        for item in (menu.get("subMenuItems") or []):
            title = (item.get("title") or "").strip().lower()
            tokens = [c.get("token") for c in _walk(item, "continuationCommand")
                      if isinstance(c, dict) and c.get("token")]
            if not tokens:
                continue
            for name, prefix in _SORT_TITLE_PREFIX.items():
                if title.startswith(prefix) and name not in out:
                    out[name] = tokens[0]
    return out


def selected_sort(payload: Any) -> Optional[str]:
    """Which ordering the site says this payload is in."""
    for menu in _walk(payload, "sortFilterSubMenuRenderer"):
        for item in (menu.get("subMenuItems") or []):
            if item.get("selected"):
                title = (item.get("title") or "").strip().lower()
                for name, prefix in _SORT_TITLE_PREFIX.items():
                    if title.startswith(prefix):
                        return name
    return None


def _continuation_items(payload: Any) -> List[Any]:
    """The items a continuation response actually appended, in order.

    A first page arrives under `reloadContinuationItemsCommand` and later
    pages under `appendContinuationItemsAction`; both live in
    `onResponseReceivedEndpoints`. Walking the whole tree for
    `commentThreadRenderer` instead would also sweep up the threads quoted
    inside unrelated panels, which is CLAUDE.md §4's tile-scoping rule in
    JSON form: stop at the container that covers exactly this page.
    """
    items: List[Any] = []
    # Three spellings of the same envelope, because the site uses a
    # different one per endpoint: `next` answers under
    # `onResponseReceivedEndpoints`, `search` under
    # `onResponseReceivedCommands`. Reading only the first returned an
    # empty list for every search page after the first, which looked
    # exactly like a two-page result set.
    for key in ("onResponseReceivedEndpoints", "onResponseReceivedCommands",
                "onResponseReceivedActions"):
        for endpoint in (payload or {}).get(key, []) or []:
            if not isinstance(endpoint, dict):
                continue
            for command_key in ("reloadContinuationItemsCommand",
                                "appendContinuationItemsAction"):
                command = endpoint.get(command_key)
                if isinstance(command, dict):
                    items.extend(command.get("continuationItems") or [])
    return items


def next_page_token(payload: Any) -> Optional[str]:
    """The token for the page after this one, or None at the end.

    Taken from the LAST item of the appended list, which is where the site
    puts it. A tree-wide search would return a reply thread's token — every
    thread carries one — and the run would walk sideways into one
    conversation instead of forward through the listing.
    """
    items = _continuation_items(payload)
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        command = (item.get("continuationItemRenderer", {})
                   .get("continuationEndpoint", {})
                   .get("continuationCommand", {}))
        if command.get("token"):
            return command["token"]
    return None


def reply_tokens(payload: Any) -> List[Tuple[str, str]]:
    """[(parent_comment_id, token)] for every thread on this page that has
    replies.

    A thread with no replies carries no token, so the list is shorter than
    the page. Each token is worth 10 replies and its own continuation.
    """
    out: List[Tuple[str, str]] = []
    for item in _continuation_items(payload):
        thread = item.get("commentThreadRenderer") if isinstance(item, dict) else None
        if not isinstance(thread, dict):
            continue
        view = (thread.get("commentViewModel") or {}).get("commentViewModel") or {}
        parent = view.get("commentId")
        replies = thread.get("replies")
        if not parent or not isinstance(replies, dict):
            continue
        for command in _walk(replies, "continuationCommand"):
            if isinstance(command, dict) and command.get("token"):
                out.append((parent, command["token"]))
                break
    return out


def panel_comment_count(payload: Any) -> Tuple[Optional[int], Optional[str]]:
    """(count, text) from the watch payload's comment panel header.

    ABBREVIATED — the panel says "2.4M" where the comment section's own
    header, one request later, says "2,457,619". Both are returned so the
    row can carry the number and what was actually written, the same
    pairing every count in this repo uses.
    """
    for panel in _walk(payload, "engagementPanelSectionListRenderer"):
        if not isinstance(panel, dict):
            continue
        identifier = panel.get("panelIdentifier") or panel.get("targetId") or ""
        if "comment" not in str(identifier).lower():
            continue
        for header in _walk(panel, "engagementPanelTitleHeaderRenderer"):
            text = _text(header.get("contextualInfo"))
            if text:
                return parse_count(text), text
    return None, None


def total_comment_count(payload: Any) -> Optional[int]:
    """The site's own total, from the comment section header.

    Present on a first page and absent on the pages after it. Recorded in
    the run sidecar so a consumer can see what fraction of the video a run
    holds — a 5-page run of a video with 2,457,619 comments is `complete`
    in the sense that every requested page was fetched, and is also a
    0.004% sample. CLAUDE.md §21: "complete" and "exhaustive" are different
    words, and a sidecar that says only the first is lying by omission.
    """
    for header in _walk(payload, "commentsHeaderRenderer"):
        value = parse_count(_text(header.get("countText")))
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

STATE_CONTENT = "content"
STATE_COMMENTS_DISABLED = "comments_disabled"
STATE_VIDEO_UNAVAILABLE = "video_unavailable"
STATE_EMPTY = "empty"
STATE_CHALLENGE = "challenge"
STATE_ERROR = "error"
STATE_UNKNOWN = "unknown"
# The site demands a signed-in account for a reason that is NOT a bot
# challenge: an age-restricted, private or members-only video. A real
# answer ABOUT THE VIDEO rather than about us, so it must never buy a
# solve (CLAUDE.md §8: detected != blocking != paying) and must never
# rotate an exit, which cannot change how old the viewer is.
STATE_AUTH_REQUIRED = "auth_required"

# Markers for a refusal. Short, and deliberately so.
#
# CLAUDE.md §18 says to count every candidate on a page you know is good
# before adding it, and doing that here killed most of the obvious list.
# Counted 2026-09-21 across ten served captures and one served watch page:
#
#     consent.youtube.com                 4 on served pages
#     botguard                           13 on the served watch page
#     CONSENT                             5 on the served watch page
#     recaptcha / captcha                 1 each on the served watch page
#     "Comments are turned off"           1 on FOUR served comment pages
#
# That last one is the sharpest trap in this file. It is not a refusal and
# not even a video-level state: it is `disabledText`, the label the comment
# COMPOSER widget ships so it can grey itself out, and the site sends it on
# every first page. A scraper using it as a marker reports "comments are
# turned off" for every video that has comments. The real signal is
# structural and is in `detect_page_state` below.
#
# What is left is the wording YouTube uses when it genuinely refuses, plus
# the HTTP status. Neither of the two text markers has been OBSERVED from
# here — 60 consecutive pages from this address drew no refusal at all — so
# they are carried as documented-but-unverified and are marked as such
# rather than described as measured (CLAUDE.md §19).
# Every marker here is written with an ASCII apostrophe and matched
# against text that `_fold_apostrophes` has normalised, because YOUTUBE
# DOES NOT USE ONE. Captured from `/player` for jNQXAC9IVRw on
# 2026-09-25 from a datacentre address:
#
#     "reason": "Sign in to confirm you\u2019re not a bot"
#
# U+2019, and `json.dumps` with its default `ensure_ascii=True` then
# turned it into the six characters `\u2019` in the text being searched
# — so the shipped ASCII marker missed the real refusal twice over, and
# the response fell through to `content`. This is CLAUDE.md §20's "a
# marker must survive BOTH encodings of the same page", arriving through
# a typographic apostrophe instead of an HTML entity.
BOT_CHALLENGE_MARKERS = (
    "Sign in to confirm you're not a bot",        # measured 2026-09-25
    "Sign in to confirm that you're not a bot",
    "/sorry/index",                               # Google's own refusal path
)

# The apostrophes a site may use where ASCII writes '. Folding is cheaper
# and safer than carrying every spelling of every marker: a marker added
# later inherits the tolerance instead of inheriting the hole.
_APOSTROPHES = "\u2019\u02bc\u2018\u00b4\u02b9"

def _fold_apostrophes(text: str) -> str:
    for ch in _APOSTROPHES:
        text = text.replace(ch, "'")
    return text
BLOCKED_STATUSES = (401, 403, 429)
# The site's structural "this video is not here", measured 0 on ten served
# captures and 2 on the unavailable one.
UNAVAILABLE_MARKER = "backgroundPromoRenderer"


def detect_bot_challenge(text: Optional[str], status: Optional[int] = None
                         ) -> Optional[str]:
    """Name the refusal, or None. Status first, because it proves the most.

    CLAUDE.md §17: order the signals by how much they prove, not by how
    cheap they are. A 429 IS the refusal; a text marker is a guess about
    wording that the site may change.
    """
    if status is not None and status in BLOCKED_STATUSES:
        return f"http {status}"
    if not text:
        return None
    # ensure_ascii=False on purpose: the default turns every non-ASCII
    # character into a `\uXXXX` escape, so a marker written with a real
    # character could never match a serialised payload. Folded afterwards
    # so one ASCII spelling covers every apostrophe the site may use.
    body = text if isinstance(text, str) else json.dumps(text,
                                                         ensure_ascii=False)
    head = _fold_apostrophes(body[:200_000])
    for marker in BOT_CHALLENGE_MARKERS:
        if marker in head:
            return marker
    return None


def playability_refusal(payload: Any) -> Optional[str]:
    """The site's OWN field for "you may not have this", or None.

    Structural, so it survives a reworded sentence and any locale — which
    a text marker does not, and the text marker above had already been
    missing the real one. CLAUDE.md §17: order the signals by how much
    they prove.

    Deliberately narrow, and `UNPLAYABLE` is deliberately absent. See
    `apply_player`: this endpoint answers `UNPLAYABLE / "Video
    unavailable"` for videos that are public and playing, because the WEB
    client cannot get a playback stream without a proof-of-origin token
    — and it serves the metadata anyway. Treating that as a refusal would
    report every video as refused, which is a worse bug than the one this
    function fixes.
    """
    if not isinstance(payload, dict):
        return None
    status_field = (payload.get("playabilityStatus") or {})
    if not isinstance(status_field, dict):
        return None
    if status_field.get("status") != "LOGIN_REQUIRED":
        return None
    reason = status_field.get("reason")
    if not isinstance(reason, str):
        reason = ""
    folded = _fold_apostrophes(reason)
    if "not a bot" in folded:
        return STATE_CHALLENGE
    # A sign-in wall that is not a bot challenge: age-restricted, private
    # or members-only. Named apart so it can never buy a solve.
    return STATE_AUTH_REQUIRED


def detect_page_state(payload: Any, status: Optional[int] = None,
                      *, expect_comments: bool = True) -> str:
    """Which of the six things this response is.

    Ordered by how much each signal proves. The refusal check runs first
    because a refusal is a fact about the response rather than about the
    video; then the structural checks, which are what tell a video with no
    comment section from one whose comments are switched off — a
    distinction three text markers got wrong above.
    """
    if status is not None and status >= 400:
        challenge = detect_bot_challenge(payload, status)
        return STATE_CHALLENGE if challenge else STATE_ERROR
    if payload is None:
        return STATE_ERROR
    if isinstance(payload, str):
        return STATE_CHALLENGE if detect_bot_challenge(payload) else STATE_UNKNOWN
    if not isinstance(payload, dict):
        return STATE_UNKNOWN

    # The site's own field before any wording of ours.
    refusal = playability_refusal(payload)
    if refusal:
        return refusal

    serialized = json.dumps(payload, ensure_ascii=False)[:200_000]
    if detect_bot_challenge(serialized):
        return STATE_CHALLENGE

    # A WATCH response is checked first, and the order matters: a watch
    # payload carries `onResponseReceivedEndpoints` of its own (the site
    # pre-seeds the related-videos rail through one), so testing for that
    # key first classified every watch payload as a continuation with no
    # comments in it — `empty` for a video with two million comments, and
    # `empty` rather than `comments_disabled` for a video whose comments
    # really are off. CLAUDE.md §17: order the signals by how much they
    # prove. `contents` + a comment section is what a watch response IS.
    section = comments_section(payload)
    if section is None and payload.get("onResponseReceivedEndpoints"):
        # A continuation response: it carries comments, or it is the end.
        return STATE_CONTENT if _has_comments(payload) else STATE_EMPTY

    if section is None:
        if UNAVAILABLE_MARKER in serialized:
            return STATE_VIDEO_UNAVAILABLE
        return STATE_UNKNOWN if expect_comments else STATE_CONTENT
    if comments_token(payload):
        return STATE_CONTENT
    # Section present, no token: the site put a message where the comments
    # would be. This is the structural form of "comments are turned off",
    # and it is the only trustworthy form.
    return STATE_COMMENTS_DISABLED


def _has_comments(payload: Any) -> bool:
    return bool(_entity_index(payload)) or bool(_walk(payload, "commentRenderer"))


def disabled_reason(payload: Any) -> Optional[str]:
    """The site's own sentence for why a section is empty, if it gave one."""
    section = comments_section(payload)
    if section is None:
        return None
    for message in _walk(section, "messageRenderer"):
        text = _text(message.get("text"))
        if text:
            return text.strip()
    return None


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------


def _entity_index(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Every `commentEntityPayload` in the response, keyed by its own key.

    This is the join table the renderer tree points into. Built once per
    page rather than searched per comment: a page carries 20 comments and
    101 mutations, and a per-comment walk of the whole payload is 20 full
    traversals of 300 KB.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for batch in _walk(payload, "mutations"):
        if not isinstance(batch, list):
            continue
        for mutation in batch:
            if not isinstance(mutation, dict):
                continue
            entity = (mutation.get("payload") or {}).get("commentEntityPayload")
            if isinstance(entity, dict) and entity.get("key"):
                index[entity["key"]] = entity
    return index


def _toolbar_state_index(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Heart state by key. The creator's heart is here and nowhere else."""
    index: Dict[str, Dict[str, Any]] = {}
    for batch in _walk(payload, "mutations"):
        if not isinstance(batch, list):
            continue
        for mutation in batch:
            if not isinstance(mutation, dict):
                continue
            state = (mutation.get("payload") or {}).get(
                "engagementToolbarStateEntityPayload")
            if isinstance(state, dict) and state.get("key"):
                index[state["key"]] = state
    return index


def _surface_index(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Surface payloads by key — the site's own permalink lives here."""
    index: Dict[str, Dict[str, Any]] = {}
    for batch in _walk(payload, "mutations"):
        if not isinstance(batch, list):
            continue
        for mutation in batch:
            if not isinstance(mutation, dict):
                continue
            surface = (mutation.get("payload") or {}).get(
                "commentSurfaceEntityPayload")
            if isinstance(surface, dict) and surface.get("key"):
                index[surface["key"]] = surface
    return index


# Query parameters the site appends for its own tracking. They are not
# part of the address: the same comment came back with `&pp=0gcJCSIA…` on a
# `newest` page and without it on a `top` page, and a URL that changes
# between two runs of the same video is a diff line about nothing.
_TRACKING_PARAMS = ("pp", "playerParams", "feature", "si", "t")


def _strip_tracking(url: Optional[str]) -> Optional[str]:
    if not url or "?" not in url:
        return url
    head, _, query = url.partition("?")
    kept = [part for part in query.split("&")
            if part and part.split("=", 1)[0] not in _TRACKING_PARAMS]
    return head + ("?" + "&".join(kept) if kept else "")


def _channel_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return INNERTUBE_ORIGIN + ("" if path.startswith("/") else "/") + path


def parent_comment_id(comment_id: Optional[str]) -> Optional[str]:
    """A reply's id embeds its parent's: `<parent>.<reply>`.

    So the thread a reply belongs to needs no bookkeeping across requests —
    it is in the id. Returns None for a top-level comment, whose id carries
    no dot.
    """
    if not comment_id or "." not in comment_id:
        return None
    return comment_id.split(".", 1)[0]


def mentions(entity: Dict[str, Any]) -> Optional[List[str]]:
    """Channel ids @-mentioned in the comment body, or None.

    Uses `utf16_span` for the run offsets — see its docstring for why the
    obvious slice is wrong.
    """
    content = ((entity.get("properties") or {}).get("content") or {})
    runs = content.get("commandRuns") or []
    if not runs:
        return None
    out: List[str] = []
    for run in runs:
        browse = ((run.get("onTap") or {}).get("innertubeCommand") or {}).get(
            "browseEndpoint") or {}
        channel = browse.get("browseId")
        if channel and channel not in out:
            out.append(channel)
    return out or None


@dataclass
class _EntityRow:
    """Intermediate shape. `Comment` itself lives in output_writer.py so the
    family's row schema stays in one place (CLAUDE.md §9)."""


def parse_comments(payload: Any, *, video_id: Optional[str] = None,
                   video_title: Optional[str] = None,
                   sort: str = DEFAULT_SORT, page: Optional[int] = None,
                   scraped_at: str = "", row_cls: Any = None,
                   start_position: int = 1) -> List[Any]:
    """One response -> a list of comment rows, in the order the site sent them.

    Primary path: the entity payloads joined to the renderer order.
    Fallback: the legacy `commentRenderer`, which the MWEB client still
    serves today. The fallback runs only when the primary yields nothing,
    and each row records which path produced it in `data_source`.
    """
    if row_cls is None:                       # pragma: no cover - callers pass it
        from output_writer import Comment as row_cls  # noqa: N813

    rows = _parse_entity_comments(
        payload, video_id=video_id, video_title=video_title, sort=sort,
        page=page, scraped_at=scraped_at, row_cls=row_cls,
        start_position=start_position)
    if rows:
        return rows
    return _parse_legacy_comments(
        payload, video_id=video_id, video_title=video_title, sort=sort,
        page=page, scraped_at=scraped_at, row_cls=row_cls,
        start_position=start_position)


def _parse_entity_comments(payload, *, video_id, video_title, sort, page,
                           scraped_at, row_cls, start_position) -> List[Any]:
    entities = _entity_index(payload)
    if not entities:
        return []
    states = _toolbar_state_index(payload)
    surfaces = _surface_index(payload)

    # The renderer order. A page's own items first; if the response has no
    # `onResponseReceivedEndpoints` (a re-sort arrives that way) fall back
    # to a tree walk, which is still ordered.
    views: List[Dict[str, Any]] = []
    for item in _continuation_items(payload):
        if not isinstance(item, dict):
            continue
        thread = item.get("commentThreadRenderer")
        if isinstance(thread, dict):
            view = (thread.get("commentViewModel") or {}).get("commentViewModel")
            if isinstance(view, dict):
                views.append(view)
            continue
        view = item.get("commentViewModel")
        if isinstance(view, dict):
            inner = view.get("commentViewModel")
            views.append(inner if isinstance(inner, dict) else view)
    if not views:
        for view in _walk(payload, "commentViewModel"):
            if isinstance(view, dict) and view.get("commentKey"):
                views.append(view)

    rows: List[Any] = []
    position = start_position
    for view in views:
        entity = entities.get(view.get("commentKey") or "")
        if entity is None:
            continue
        rows.append(_entity_row(
            entity, view=view, state=states.get(view.get("toolbarStateKey") or ""),
            surface=surfaces.get(view.get("commentSurfaceKey") or ""),
            video_id=video_id, video_title=video_title, sort=sort, page=page,
            scraped_at=scraped_at, row_cls=row_cls, position=position))
        position += 1
    return rows


def _entity_row(entity, *, view, state, surface, video_id, video_title, sort,
                page, scraped_at, row_cls, position):
    props = entity.get("properties") or {}
    author = entity.get("author") or {}
    toolbar = entity.get("toolbar") or {}
    view = view or {}
    state = state or {}
    surface = surface or {}

    comment_id = props.get("commentId") or view.get("commentId")
    published_text = props.get("publishedTime")
    approx, precision = relative_time(published_text, scraped_at or None)

    like_text = toolbar.get("likeCountNotliked") or toolbar.get("likeCountLiked")
    reply_text = toolbar.get("replyCount")

    url = None
    command = ((surface.get("publishedTimeCommand") or {})
               .get("innertubeCommand") or {})
    path = (command.get("commandMetadata") or {}).get(
        "webCommandMetadata", {}).get("url")
    if path:
        url = _strip_tracking(_channel_url(path))
    elif video_id and comment_id:
        url = comment_permalink(video_id, comment_id)

    channel_path = ((author.get("channelCommand") or {}).get("innertubeCommand")
                    or {}).get("browseEndpoint", {}).get("canonicalBaseUrl")

    return row_cls(
        source=SOURCE,
        scraped_at=scraped_at,
        url=url,
        sku=comment_id,
        title=video_title,
        video_id=video_id,
        text=(props.get("content") or {}).get("content"),
        author_name=author.get("displayName"),
        author_channel_id=author.get("channelId"),
        author_channel_url=_channel_url(channel_path),
        author_avatar_url=author.get("avatarThumbnailUrl"),
        author_is_verified=author.get("isVerified"),
        author_is_creator=author.get("isCreator"),
        author_is_artist=author.get("isArtist"),
        like_count=parse_count(like_text),
        like_count_text=like_text,
        reply_count=parse_count(reply_text),
        reply_count_text=reply_text if reply_text not in ("", None) else None,
        published_time_text=published_text,
        published_at_approx=approx,
        published_at_precision=precision,
        edited=is_edited(published_text),
        reply_level=props.get("replyLevel"),
        parent_id=parent_comment_id(comment_id),
        # False rather than null: the payload either carries the badge or
        # it does not, and "not pinned" is a fact the site stated by
        # omission rather than something unknown.
        is_pinned=bool(view.get("pinnedText")),
        pinned_by=view.get("pinnedText"),
        # Null ONLY when the toolbar-state payload is missing altogether,
        # which is the one case where the heart is genuinely unknown.
        creator_hearted=(None if not state else
                         state.get("heartState") == "TOOLBAR_HEART_STATE_HEARTED"),
        mentions=mentions(entity),
        sort=sort,
        data_source="innertube.entity",
        page=page,
        position=position,
    )


def _parse_legacy_comments(payload, *, video_id, video_title, sort, page,
                           scraped_at, row_cls, start_position) -> List[Any]:
    """The `commentRenderer` shape, still served to the MWEB client.

    Same columns, different spellings of every one of them. Pinned state is
    a badge rather than a field, the like count is a `runs` list rather
    than a string, and `replyCount` is an exact integer here where the
    entity form gives an abbreviated string — so a legacy row's
    `reply_count_text` is the number written out, and that is honest: it is
    what the site said.
    """
    renderers = [r for r in _walk(payload, "commentRenderer")
                 if isinstance(r, dict) and r.get("commentId")]
    rows: List[Any] = []
    position = start_position
    for renderer in renderers:
        comment_id = renderer.get("commentId")
        published_text = _text(renderer.get("publishedTimeText"))
        approx, precision = relative_time(published_text, scraped_at or None)
        like_text = _text(renderer.get("voteCount"))
        author_endpoint = renderer.get("authorEndpoint") or {}
        channel_path = (author_endpoint.get("browseEndpoint") or {}).get(
            "canonicalBaseUrl")
        channel_id = (author_endpoint.get("browseEndpoint") or {}).get("browseId")
        badge = renderer.get("pinnedCommentBadge")
        pinned_by = None
        if isinstance(badge, dict):
            pinned_by = _text((badge.get("pinnedCommentBadgeRenderer") or {})
                              .get("label"))
        reply_count = renderer.get("replyCount")
        thumbnails = ((renderer.get("authorThumbnail") or {}).get("thumbnails")
                      or [])
        rows.append(row_cls(
            source=SOURCE,
            scraped_at=scraped_at,
            url=comment_permalink(video_id, comment_id) if video_id else None,
            sku=comment_id,
            title=video_title,
            video_id=video_id,
            text=_text(renderer.get("contentText")),
            author_name=_text(renderer.get("authorText")),
            author_channel_id=channel_id,
            author_channel_url=_channel_url(channel_path),
            author_avatar_url=(thumbnails[-1].get("url") if thumbnails else None),
            author_is_verified=None,
            author_is_creator=renderer.get("authorIsChannelOwner"),
            author_is_artist=None,
            like_count=parse_count(like_text),
            like_count_text=like_text,
            reply_count=parse_count(reply_count),
            reply_count_text=(str(reply_count) if reply_count is not None
                              else None),
            published_time_text=published_text,
            published_at_approx=approx,
            published_at_precision=precision,
            edited=is_edited(published_text),
            reply_level=1 if parent_comment_id(comment_id) else 0,
            parent_id=parent_comment_id(comment_id),
            is_pinned=bool(pinned_by),
            pinned_by=pinned_by,
            creator_hearted=bool(renderer.get("creatorHeart")),
            mentions=None,
            sort=sort,
            data_source="innertube.legacy",
            page=page,
            position=position,
        ))
        position += 1
    return rows


# ---------------------------------------------------------------------------
# --mode video
# ---------------------------------------------------------------------------


def _exact_from_a11y(label: Optional[str]) -> Optional[int]:
    """"like this video along with 19,384,917 other people" -> 19384917.

    Reads the NUMBER, not every digit in the string. CLAUDE.md §10 records
    a sibling repo that shipped `445279961` on every row of every run by
    stripping the digits out of a label holding both a rating and a count.
    """
    if not label:
        return None
    m = re.search(r"([\d][\d,.    ]*)", label)
    return parse_count(m.group(1)) if m else None


def parse_video(payload: Any, *, video_id: Optional[str] = None,
                scraped_at: str = "", row_cls: Any = None) -> Optional[Any]:
    """The watch payload -> one video row.

    `video_id` is taken from `currentVideoEndpoint`, never from a tree-wide
    search for "videoId": the payload names a dozen RELATED videos and the
    first hit in tree order is one of them. Same failure as reading a
    neighbouring tile's price.
    """
    if row_cls is None:                       # pragma: no cover
        from output_writer import Video as row_cls  # noqa: N813
    if not isinstance(payload, dict):
        return None

    primary = (_walk(payload, "videoPrimaryInfoRenderer") or [None])[0]
    secondary = (_walk(payload, "videoSecondaryInfoRenderer") or [None])[0]
    if primary is None and secondary is None:
        return None
    primary = primary or {}
    secondary = secondary or {}

    current = ((payload.get("currentVideoEndpoint") or {})
               .get("watchEndpoint") or {})
    vid = current.get("videoId") or video_id

    view_block = (_walk(primary, "videoViewCountRenderer") or [{}])[0]
    view_text = _text(view_block.get("viewCount"))
    short_view_text = _text(view_block.get("shortViewCount"))

    like_labels = [t for t in _walk(primary, "accessibilityText")
                   if isinstance(t, str) and "like this video" in t.lower()]
    like_count = _exact_from_a11y(like_labels[0]) if like_labels else None

    owner = (_walk(secondary, "videoOwnerRenderer") or [{}])[0]
    browse = (owner.get("navigationEndpoint") or {}).get("browseEndpoint") or {}

    description = _text(secondary.get("attributedDescription"))
    panel_count, panel_text = panel_comment_count(payload)

    return row_cls(
        source=SOURCE,
        scraped_at=scraped_at,
        url=canonical_video_url(vid) if vid else None,
        sku=vid,
        title=_text(primary.get("title")),
        video_id=vid,
        channel_name=_text(owner.get("title")),
        channel_id=browse.get("browseId"),
        channel_url=_channel_url(browse.get("canonicalBaseUrl")),
        subscriber_count_text=_text(owner.get("subscriberCountText")),
        subscriber_count=parse_count(_text(owner.get("subscriberCountText"))),
        view_count=parse_count(view_text),
        view_count_text=view_text or short_view_text,
        like_count=like_count,
        like_count_text=(like_labels[0] if like_labels else None),
        comment_count=panel_count,
        comment_count_text=panel_text,
        # The section is present with a token, present with a message, or
        # absent because the video is gone. Only the first two are an
        # answer to "does this video take comments".
        comments_enabled=(None if comments_section(payload) is None
                          else bool(comments_token(payload))),
        published_date_text=_text(primary.get("dateText")),
        published_relative_text=_text(primary.get("relativeDateText")),
        description=description,
        duration_text=None,
        data_source="innertube.watch",
        page=1,
        position=1,
    )


def player_body(video_id: str, client_version: str,
                locale: str = DEFAULT_LOCALE,
                region: str = DEFAULT_REGION) -> Dict[str, Any]:
    """`/youtubei/v1/player` — the only endpoint that states an exact date.

    `--mode video` makes this second call because the watch payload
    publishes the upload date only as the string it renders ("Oct 24,
    2009") while this one publishes `2009-10-24T23:57:33-07:00`, plus the
    duration in seconds, the category and the keyword list.
    """
    return {"context": innertube_context(client_version, locale, region),
            "videoId": video_id}


def apply_player(payload: Any, row: Any) -> Any:
    """Merge a `/player` response into an existing video row.

    Deliberately NOT a classifier. `playabilityStatus` on this endpoint
    said `UNPLAYABLE / "Video unavailable"` for a video that is available,
    public and playing — the WEB client cannot obtain a playback stream
    without a proof-of-origin token, and the metadata is served anyway. So
    a scraper that took that field as "this video is gone" would report
    every video as gone. The unavailable signal stays where it was
    measured: no comment section plus `backgroundPromoRenderer` on the
    watch payload.
    """
    if row is None or not isinstance(payload, dict):
        return row
    micro = (payload.get("microformat") or {}).get(
        "playerMicroformatRenderer") or {}
    details = payload.get("videoDetails") or {}

    published = micro.get("publishDate") or micro.get("uploadDate")
    if published:
        row.published_at = published
    length = details.get("lengthSeconds") or micro.get("lengthSeconds")
    if length is not None:
        try:
            row.duration_seconds = int(length)
        except (TypeError, ValueError):
            pass
    if micro.get("category"):
        row.category = micro["category"]
    if details.get("keywords"):
        row.keywords = list(details["keywords"])
    if micro.get("isFamilySafe") is not None:
        row.is_family_safe = bool(micro["isFamilySafe"])
    if micro.get("isUnlisted") is not None:
        row.is_unlisted = bool(micro["isUnlisted"])
    if details.get("isLiveContent") is not None:
        row.is_live = bool(details["isLiveContent"])
    # `/player` states the view count exactly too, and states it LATER than
    # the watch payload did — so it is preferred when both are present.
    exact_views = details.get("viewCount") or micro.get("viewCount")
    if exact_views:
        parsed = parse_count(exact_views)
        if parsed is not None:
            row.view_count = parsed
    if not row.title and details.get("title"):
        row.title = details["title"]
    if not row.channel_name and details.get("author"):
        row.channel_name = details["author"]
    return row


# ---------------------------------------------------------------------------
# --mode search
# ---------------------------------------------------------------------------


def parse_search(payload: Any, *, query: str = "", scraped_at: str = "",
                 page: Optional[int] = None, row_cls: Any = None,
                 start_position: int = 1) -> List[Any]:
    """A search response -> one row per video, in result order.

    Only `videoRenderer` items become rows. The payload also carries
    channels, playlists and promoted shelves; each of those would parse to
    a row with no video id, which is the one column that makes a search
    result useful — it is what `--mode comments` takes next.
    """
    if row_cls is None:                       # pragma: no cover
        from output_writer import Video as row_cls  # noqa: N813
    rows: List[Any] = []
    position = start_position
    for renderer in _walk(payload, "videoRenderer"):
        if not isinstance(renderer, dict):
            continue
        vid = renderer.get("videoId")
        if not vid:
            continue
        owner = renderer.get("ownerText") or {}
        browse = None
        for run in (owner.get("runs") or []):
            endpoint = (run.get("navigationEndpoint") or {}).get(
                "browseEndpoint")
            if endpoint:
                browse = endpoint
                break
        view_text = _text(renderer.get("viewCountText"))
        rows.append(row_cls(
            source=SOURCE,
            scraped_at=scraped_at,
            url=canonical_video_url(vid),
            sku=vid,
            title=_text(renderer.get("title")),
            video_id=vid,
            channel_name=_text(owner),
            channel_id=(browse or {}).get("browseId"),
            channel_url=_channel_url((browse or {}).get("canonicalBaseUrl")),
            subscriber_count_text=None,
            subscriber_count=None,
            view_count=parse_count(view_text),
            view_count_text=view_text,
            like_count=None,
            like_count_text=None,
            comment_count=None,
            published_date_text=None,
            published_relative_text=_text(renderer.get("publishedTimeText")),
            description=_text((renderer.get("detailedMetadataSnippets") or
                               [{}])[0].get("snippetText")),
            duration_text=_text(renderer.get("lengthText")),
            data_source="innertube.search",
            page=page,
            position=position,
            query=query or None,
        ))
        position += 1
    return rows


def search_total(payload: Any) -> Optional[int]:
    """The site's own `estimatedResults`, which is what it calls an estimate."""
    value = (payload or {}).get("estimatedResults")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def search_page_token(payload: Any) -> Optional[str]:
    """The next page of results, or None at the end.

    Search paginates by continuation only — there is no `?page=N` for it,
    so CLAUDE.md §7's layer 2 does not apply and the chain has to be
    followed one link at a time.

    It also needs its own reader rather than reusing `next_page_token`.
    A search response's FIRST page carries no `onResponseReceived*`
    envelope at all: the token sits inside the results section itself,
    under `twoColumnSearchResultsRenderer/…/sectionListRenderer`. A run
    that looked only in the envelope stopped after page one and reported
    `pagination_exhausted` — a complete-looking run holding a fifth of what
    was asked for, which is CLAUDE.md §7's silent-single-page bug wearing a
    different endpoint.

    Searching the whole tree for a `continuationItemRenderer` is safe HERE
    and would not be on a comments page, where every thread carries one of
    its own. So the two readers stay separate.
    """
    token = next_page_token(payload)
    if token:
        return token
    for renderer in _walk(payload, "continuationItemRenderer"):
        if not isinstance(renderer, dict):
            continue
        command = (renderer.get("continuationEndpoint", {})
                   .get("continuationCommand", {}))
        if command.get("token"):
            token = command["token"]
    return token
