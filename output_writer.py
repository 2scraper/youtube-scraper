"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers used by all three engines and the
HTTP path.

Three modes, two row shapes
---------------------------
    --mode comments  a video's comment threads, and optionally their replies
    --mode video     one video's own metadata — the parent row comments hang off
    --mode search    a query -> the videos that answer it

`comments` writes `Comment`; `video` and `search` both write `Video`,
because a search result and a watch page describe the same kind of thing
with different coverage: a search row states the view count, the channel
and the duration, and a watch row adds the exact like count, the comment
total, the description and the upload date. Neither is a subset of the
other's population, so `data_source` is a COLUMN and `diff_runs.py` reports
a difference that comes with a `data_source` difference as
`source_changed` rather than as the site having changed.

A repo with two row classes owes three things (CLAUDE.md §9), and they are
all here: the family prefix `source, scraped_at, url, sku, title` is
byte-identical and first in BOTH classes, `mode` is recorded in the run
sidecar because the repo no longer implies it, and `diff_runs.py` refuses a
pair whose modes differ rather than producing a diff whose every line is an
artefact.

`sku` is the id in both, as everywhere in this family: the comment id on a
`Comment`, the 11-character video id on a `Video`.

Why a comment's numbers come in pairs
-------------------------------------
`like_count` sits beside `like_count_text`, and `published_at_approx`
beside `published_time_text`. That is not redundancy — it is the whole
honesty of this repo in two columns.

YouTube does not publish an exact like count for a comment to any client.
It publishes "318K". And it publishes no absolute timestamp at all, only
"1 year ago". Measured over 70 comments in four captures on 2026-09-21,
under the WEB, MWEB and TVHTML5 clients. So `like_count` is 318000 — a
magnitude, not a count — and `published_at_approx` is derived from the run
clock and is only as precise as `published_at_precision` says.

The text columns are what the site actually said. A consumer that needs a
fact rather than a magnitude reads those, or reads the official Data API,
and the README says so rather than letting anyone discover it from a
dashboard.

A `Video` row has no such problem: `view_count` and `like_count` there are
exact figures the site publishes in full, and `published_date_text` is a
real date.

Everything below the dataclasses is row-class-agnostic: pass `row_cls` so
an empty CSV still gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The site a row came from. YouTube serves the same content on
# `www.youtube.com`, `m.youtube.com`, `music.youtube.com` and `youtu.be`,
# and all four resolve to one private API; this column names the SITE, and
# which host a URL was given as is recoverable from `url`.
SOURCE_DEFAULT = "youtube.com"


def utc_now() -> str:
    """The run's timestamp, as a UTC ISO-8601 string with a `Z`.

    One helper so every row in a run can be given the SAME stamp by the
    caller rather than each row calling the clock. Rows from one page that
    disagree in `scraped_at` by a few milliseconds make a diff noisier for
    no information — and on this site the stamp does more work than usual,
    because every derived comment date is measured backwards from it.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Comment:
    """One comment or one reply.

    The family prefix — `source`, `scraped_at`, `url`, `sku`, `title` — is
    byte-identical and in this order across every repo in the family
    (CLAUDE.md §9). `title` is the VIDEO's title: a comment has no title of
    its own, and repeating the parent's is what makes a CSV of ten
    thousand comments readable without a join.
    """

    source: str = SOURCE_DEFAULT
    scraped_at: str = ""
    # The site's own deep link to this comment, taken from the payload's
    # `publishedTimeCommand` rather than built: `/watch?v=…&lc=<id>`.
    url: Optional[str] = None
    # The comment id. A reply's id is `<parent>.<reply>`, so `parent_id`
    # below needs no bookkeeping across requests.
    sku: Optional[str] = None
    # The video's title, not the comment's.
    title: Optional[str] = None

    # ---- what was said ---------------------------------------------------
    video_id: Optional[str] = None
    # Verbatim, emoji included. The payload's `attachmentRuns` do NOT mean
    # the text has holes in it — checked on all 70 captured comments, the
    # emoji are present in the string and the runs only say where to paint
    # an image over them. What the runs' offsets ARE is UTF-16 code units,
    # which is why `product_parser.utf16_span` exists.
    text: Optional[str] = None

    # ---- who said it -----------------------------------------------------
    # The site writes a display name as its handle, "@YouTube".
    author_name: Optional[str] = None
    author_channel_id: Optional[str] = None
    author_channel_url: Optional[str] = None
    author_avatar_url: Optional[str] = None
    author_is_verified: Optional[bool] = None
    # The video's own channel commenting on its own video.
    author_is_creator: Optional[bool] = None
    author_is_artist: Optional[bool] = None

    # ---- engagement ------------------------------------------------------
    # APPROXIMATE, and the column beside it says what the site actually
    # wrote. See the module docstring: YouTube publishes no exact like
    # count for a comment, to any client.
    like_count: Optional[int] = None
    like_count_text: Optional[str] = None
    # Exact on a legacy-shape row (the site sends an integer) and
    # abbreviated on an entity-shape row, so this one is read the same way
    # and kept beside its text for the same reason.
    reply_count: Optional[int] = None
    reply_count_text: Optional[str] = None

    # ---- when ------------------------------------------------------------
    # What the site said: "1 year ago", or "edited 3 months ago".
    published_time_text: Optional[str] = None
    # Derived from `scraped_at` minus that. The LATEST instant the comment
    # could have been written, because YouTube floors — "1 year ago" is an
    # age in [1, 2) years. Null on any locale whose wording is not English.
    published_at_approx: Optional[str] = None
    # second / minute / hour / day / week / month / year. Without it, a
    # date accurate to within a year reads exactly like one accurate to the
    # second.
    published_at_precision: Optional[str] = None
    edited: Optional[bool] = None

    # ---- where it sits in the thread -------------------------------------
    # 0 for a top-level comment, 1 for a reply. YouTube has no deeper
    # nesting: a reply to a reply is still level 1, addressed with an
    # @mention.
    reply_level: Optional[int] = None
    parent_id: Optional[str] = None
    is_pinned: Optional[bool] = None
    # "Pinned by @RickAstleyYT" — the site's own sentence, which names WHO.
    pinned_by: Optional[str] = None
    # The creator's heart. Measured 2 of 70 captured comments, so this
    # column takes both its values on a single page and is genuinely
    # verified, which CLAUDE.md §20 asks for before trusting a boolean.
    creator_hearted: Optional[bool] = None
    # Channel ids @-mentioned in the body, read with UTF-16-correct offsets.
    mentions: Optional[List[str]] = None

    # ---- provenance ------------------------------------------------------
    # Which of the site's two orderings this row was drawn from. NOT a
    # sidecar field: the ordering decides WHICH comments a capped run holds,
    # so two runs under different sorts are different samples of the same
    # video and `diff_runs.py` refuses to compare them (CLAUDE.md §21).
    sort: Optional[str] = None
    # `innertube.entity` or `innertube.legacy` — which of the site's two
    # payload shapes this row was read out of.
    data_source: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None


@dataclass
class Video:
    """One video — from its watch payload, or from a search result.

    Same family prefix, byte-identical, so a consumer reading a comments
    file and a videos file reads the same first five columns.
    """

    source: str = SOURCE_DEFAULT
    scraped_at: str = ""
    url: Optional[str] = None
    # The 11-character video id.
    sku: Optional[str] = None
    title: Optional[str] = None

    video_id: Optional[str] = None
    channel_name: Optional[str] = None
    channel_id: Optional[str] = None
    channel_url: Optional[str] = None
    # "4.54M subscribers" — abbreviated by the site, like a comment's likes.
    subscriber_count_text: Optional[str] = None
    subscriber_count: Optional[int] = None

    # EXACT on a watch row: the site publishes "1,818,222,351 views" in
    # full, and the abbreviated "1.8B" separately. Search rows state it in
    # full too.
    view_count: Optional[int] = None
    view_count_text: Optional[str] = None
    # EXACT on a watch row, and read out of the like button's accessibility
    # label — which states the number in full where the button's own text
    # is abbreviated. Read the NUMBER in that label, never every digit in
    # it (CLAUDE.md §10). Null on a search row: the payload has none.
    like_count: Optional[int] = None
    like_count_text: Optional[str] = None
    # ABBREVIATED, from the watch payload's comment panel ("2.4M"). The
    # exact figure exists one request further in, in the comment section's
    # own header, and a `--mode comments` run records it in the sidecar.
    comment_count: Optional[int] = None
    comment_count_text: Optional[str] = None
    # Whether the video accepts comments at all — the single most useful
    # thing to know before spending requests on it. Never null on a watch
    # row: the section is there with a token, or there with a message.
    comments_enabled: Optional[bool] = None

    # ---- when -------------------------------------------------------------
    # EXACT, and an ISO-8601 instant with a real offset:
    # "2009-10-24T23:57:33-07:00". It comes from a SECOND endpoint —
    # `/youtubei/v1/player` — because the watch payload states only the
    # rendered "Oct 24, 2009". That asymmetry is the reason `--mode video`
    # makes two calls per video, and it is also the sharpest contrast with
    # a comment row, which has no absolute time under any endpoint at all.
    published_at: Optional[str] = None
    # What the watch page rendered — "Oct 24, 2009".
    published_date_text: Optional[str] = None
    published_relative_text: Optional[str] = None

    description: Optional[str] = None
    duration_text: Optional[str] = None
    # From `/player`. The two endpoints disagree by a second on the video
    # used for these fixtures (`videoDetails` 213, `microformat` 214); this
    # column takes `videoDetails`, which matches the rendered running time.
    duration_seconds: Optional[int] = None
    # YouTube's own category ("Music"), its keyword list, and the two flags
    # it publishes about reach. All four are `/player` only.
    category: Optional[str] = None
    keywords: Optional[List[str]] = None
    is_family_safe: Optional[bool] = None
    is_unlisted: Optional[bool] = None
    is_live: Optional[bool] = None

    data_source: Optional[str] = None
    page: Optional[int] = None
    position: Optional[int] = None
    # `--mode search` only: the query that produced this row. Kept because
    # a search result is only meaningful beside the question it answers,
    # and because two search runs of different queries must not be diffed
    # as though rows had appeared and disappeared.
    query: Optional[str] = None


# Row classes by --mode, so an engine maps its mode to a schema in one
# place.
ROW_CLASS_BY_MODE = {"comments": Comment, "video": Video, "search": Video}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py.
#
# All three qualify, for different reasons. A comment id is globally
# unique and a video names each of its comments once — measured over 60
# consecutive pages of one video on 2026-09-21, 1,200 ids and 1,200
# distinct. A `video` run is one row. A search names each video once per
# result set.
#
# Adjacent comment pages do NOT overlap on this site, so any drop during
# dedupe means something unexpected — the site repeating a thread across a
# page boundary while the ranking shifts under a long run, most likely —
# and that is why the drop count is logged rather than silently applied.
UNIQUE_BY_SKU_MODES = ("comments", "video", "search")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a repeated page then re-parses without duplicating its rows into the
    final output.

    On YouTube a drop here is unexpected but not impossible, which is why
    the count is logged rather than quietly applied. Sixty consecutive
    pages of one video returned 1,200 comment ids and 1,200 distinct ones
    on 2026-09-21, so adjacent pages do not overlap by design.

    What CAN produce a duplicate is the ranking moving underneath a long
    run: `--sort top` is a live relevance ordering, and a comment that
    gains likes between page 3 and page 30 can be served twice. That is a
    fact about the site worth seeing in a log rather than silently
    absorbing, and it is also the reason a long run is a sample rather than
    a snapshot.
    The function stays regardless — it is the backstop that keeps the output
    clean, and "should never fire" is a poor reason to remove a guard that
    costs one pass over a list.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    All three of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Comment) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On YouTube this code does NOT cover the two states that look like it and
# are not. A video whose comments are TURNED OFF answers HTTP 200 with a
# comment section holding a message instead of a token: the request was
# served exactly as asked, the answer is that there are no comments, and
# that is EXIT_NO_PRODUCTS. A video that does not exist or has been taken
# down answers 200 with `backgroundPromoRenderer` and no comment section at
# all — also not a block. Reporting either as blocked sends a user hunting
# for a proxy problem that does not exist.
#
# What EXIT_BLOCKED would mean here is largely unmeasured, and saying so is
# more use than inventing a description. Measured 2026-09-21 from a bare
# Finnish datacentre address with no proxy and no key: 60 consecutive
# InnerTube pages, all HTTP 200, no refusal of any kind. Every candidate
# text marker counted on known-good captures fired on GOOD pages —
# `consent.youtube.com` 4 times, `botguard` 13, `recaptcha` once — so none
# of them is carried (CLAUDE.md §18).
#
# What IS carried is the HTTP status (401/403/429) and the two sentences
# YouTube is documented to use when it demands a sign-in. Neither sentence
# has been observed from here, and they are marked unverified in
# product_parser rather than described as measured. If a run reports exit
# 3, the saved debug payload is the evidence, and it is new.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "comments", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listings run, a job run or a
    careers run, and those populate different columns — a listings row has
    the site's `domain` and its slot counters, a job row has the company
    website and a currency, a careers row has a department and an Ashby
    apply URL. `diff_runs.py` refuses a pair whose modes or sources differ,
    which matters more here than on most sites in this family: a careers run
    and a marketplace run have NO ids in common at all, so a diff of the two
    would report every row as both added and removed.

    `source` is `youtube.com` on every row of every run. The site answers
    on four hosts and this column names the SITE rather than the host, so
    one value covers all of them; which host a URL was given as is
    recoverable from `url`. It is kept because consumers read these columns
    by name across the family.

    `extra` carries facts about the run that are not about any single row,
    and on this site the most important one is how small a run is. YouTube
    states its own total in the comment section header — 2,457,619 on the
    video used for this repo's fixtures — so `extra` records
    `total_comments`, `comments_collected` and the percentage between them,
    plus `sort`, the `client_version` the run spoke and whether replies
    were expanded.

    That is the only honest way to say what a run holds, because "complete"
    and "exhaustive" come apart badly here (CLAUDE.md §21). A 5-page run
    fetched every page it was asked for and is genuinely `complete`. It is
    also 100 comments out of two and a half million, which is 0.004% — and
    nothing in the row count reveals that.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        # Named "products" even though these are job listings, and kept that
        # way deliberately: every repo in this family writes this key, and a
        # consumer reading several of them reads one sidecar shape.
        # quora-scraper made the same call for answers. The row TYPE is
        # `mode` plus `source`, which are right beside it.
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Comment) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 rows -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue.
#
# On YouTube the third signal is the strongest one available, and it is
# neither of those: the site hands out the NEXT page's token inside the
# page it just served. There is no `?page=N` to construct and no selector
# to go stale — a response either carries a continuation token or it is the
# last page, and "pagination_exhausted" means the site said so itself.
#
# That is also why this repo cannot plan page URLs ahead (CLAUDE.md §7):
# page 5's token is unknowable until page 4 has been read, so a comments
# run is strictly sequential and `--concurrency` above 1 is refused for it
# with that reason. `--mode video` parallelises across VIDEOS instead,
# which is the unit that actually has independent addresses.
#
# "page_cap_reached" fires when `--pages` runs out with the site still
# offering more, which is the normal end of a run here.
# "page_echo_mismatch" is carried for the family's shared vocabulary and
# cannot fire: this site is never asked for a page by number, so it has no
# number to echo back.
#
# "single_page_route" is what a `--mode video` run reports: one video is
# one response, and there is no second page of it to miss.
#
# Note what it does NOT mean on this site: a `complete` comments run holds
# every page it asked for, which is almost never every comment the video
# has. CLAUDE.md §21 — complete and exhaustive are different words — and
# the sidecar records the site's own total beside the collected count so a
# consumer is not left inferring one from the other.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "page_cap_reached", "page_echo_mismatch",
                         "single_page_route")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "comments", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Comment)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
