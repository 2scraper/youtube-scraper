"""
smoke_test.py — the offline suite for youtube-scraper.

One file of plain functions with fixtures loaded from
`fixtures_generated.json`. No pytest, no conftest, no fixtures directory
(CLAUDE.md §10); `tests/test_smoke.py` wraps this as a single pytest test so
`pytest` works as an entry point without a second copy of the checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and fails if that engine's group reports a skip.

The fixtures are cut from real captures taken 2026-09-21 by `make_fixtures.py`,
which also PROVES each trimmed fixture parses identically to its untrimmed
original — every column of every kept row, and the same page state — before
writing anything.

What is not verbatim in them
----------------------------
Every person. A comment capture is not a product grid: it carries a real
individual's display name, their channel id and URL, their avatar and their
own words, and CLAUDE.md §10 is explicit that republishing those is a
separate act from the site showing them on its own page. So a commenter's
name becomes `@fixture_user_N`, their id a fixed-shape placeholder, and
their text filler of the SAME UTF-16 LAYOUT — the layout matters, because
the payload's emoji runs index into that text in UTF-16 code units and one
of the checks below exists to pin exactly that.

What the site generates around a person is untouched: like and reply
counts, relative timestamps, the verified and creator flags, the pinned
badge, the sort menu, and the video's own title, channel and view count. A
public video and its publisher are the site's catalogue, not a private
individual. `check_fixtures_carry_no_personal_names` guards the SHAPE, so a
future capture holding a value this scrubber never knew about is caught.

What the fixtures deliberately reproduce
----------------------------------------
Each is a trap this repo hit or measured, and the fixture exists so that
fixing it stays fixed:

  * the ENTITY form, where the comment text lives in
    `frameworkUpdates.entityBatchUpdate.mutations` and the renderer tree
    holds only keys — a parser that walks the renderers finds twenty empty
    shells;
  * the LEGACY `commentRenderer` form, which the site still serves to its
    MWEB client today, so the fallback path is exercised rather than
    assumed;
  * a comment whose emoji runs index in UTF-16 code units, where the
    obvious Python slice takes the wrong span;
  * a Russian-locale page, where the relative time and the abbreviated
    like count are both localised and both must come back as null rather
    than as a guess;
  * a video whose comments are TURNED OFF, which answers HTTP 200 with a
    healthy-looking 60 KB payload;
  * a video that does not exist, which also answers 200;
  * a watch payload and a `/player` payload, which together are the only
    way to get an exact upload date on this site.
"""

import argparse
import ast
import csv
import inspect
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import types
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import fields

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
# `HERE` is what the family core calls this. Both names are bound because
# the shared checks below were lifted verbatim from a sibling repo, and
# renaming inside them would stop the two copies being comparable — which
# is the whole point of sharing them.
HERE = REPO_ROOT
sys.path.insert(0, REPO_ROOT)

import product_parser                                        # noqa: E402
import page_flow                                             # noqa: E402
import output_writer                                         # noqa: E402
import proxy_pool                                            # noqa: E402
import env_config                                            # noqa: E402
from output_writer import Comment, Video                     # noqa: E402

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False

ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")

DRIVER_IMPORTS = {
    "playwright_scraper": "playwright",
    "selenium_scraper": "selenium",
    "puppeteer_scraper": "pyppeteer",
}

# The family's CLI contract (CLAUDE.md §9). Re-derived rather than copied:
#
#   grep -ohE '"--[a-z0-9-]+"' */playwright_scraper.py | sort | uniq -c
#
# across the 28 sibling repos on 2026-09-21 — 23 flags in 27 of 27 repos
# that have the engine, `--mode`/`--fingerprint`/`--fp-country`/`--fp-tags`
# in 26 and `--locale` in 24. A flag in 27 of 27 IS the contract.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless",
    "--headful", "--mode", "--fingerprint", "--fp-country", "--fp-tags",
    "--locale",
}

# This site's own additions. Each is here because YouTube has a thing no
# sibling does: two orderings of one comment thread, replies that cost a
# request each, and an InnerTube `gl` that is separate from `hl`.
SITE_FLAGS = {"--sort", "--replies", "--reply-pages", "--region"}

# CLAUDE.md §12, and ASSEMBLED from pieces rather than written out — which
# is what lets the scan cover this file too. Three sibling repos exempted
# their own suite from the scan, so the one file most likely to acquire a
# stray phrase or a pasted credential was the one file nobody scanned
# (§22).
BANNED_WORDING = (
    "cloud " + "browser", "anti" + "detect browser",
    "2scraper Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)

BANNED_FLAGS = ("--anti" + "detect", "--country-code", "--country")

_FIXTURE_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")


def _load_fixtures():
    if not os.path.exists(_FIXTURE_PATH):
        raise SystemExit(
            f"fixtures_generated.json is missing from {REPO_ROOT}.\n"
            f"Regenerate it with `python3 make_fixtures.py`, which needs "
            f"your own captures in ../captures/youtube/ — see that file's "
            f"docstring. If it is present but ignored, check .gitignore's "
            f"`!fixtures_generated.json` exception.")
    with open(_FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


FIX = _load_fixtures()

SCRAPED_AT = "2026-09-21T12:00:00Z"
VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_TITLE = "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)"


def comments(name, **kwargs):
    kwargs.setdefault("video_id", VIDEO_ID)
    kwargs.setdefault("video_title", VIDEO_TITLE)
    kwargs.setdefault("sort", "top")
    kwargs.setdefault("page", 1)
    kwargs.setdefault("scraped_at", SCRAPED_AT)
    return product_parser.parse_comments(FIX[name], row_cls=Comment, **kwargs)


class _NullContext:
    """Stands in for a driver's lifetime while the browser is stubbed.

    Each engine wraps its driver in `_driver_context`; Playwright's opens a
    real `sync_playwright()`, so the concurrency checks below swap in this
    no-op to drive the queue with no browser anywhere (CLAUDE.md §10).
    """

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False

def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))

def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))

def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))

def check_row_schema():
    """The family prefix is byte-identical and in order (§9), and the
    columns this site cannot fill are ABSENT rather than null."""
    from output_writer import Comment, Video, SOURCE_DEFAULT
    names = [f.name for f in fields(Comment)]
    video_names = [f.name for f in fields(Video)]
    equal("the family prefix, in order", names[:5],
          ["source", "scraped_at", "url", "sku", "title"])
    equal("BOTH row classes carry the same prefix, in the same order",
          video_names[:5], names[:5])
    equal("source names the site, which answers on four hosts",
          SOURCE_DEFAULT, "youtube.com")
    # A comment has no price, stock, brand or rating. A column null on
    # every row of every run is what §9 forbids.
    for absent in ("price", "currency", "discount_pct", "in_stock", "brand",
                   "original_price", "rating", "dislike_count"):
        check("no `%s` column on a comment" % absent, absent not in names)
    # What stands in their place, including the paired text columns that
    # are this repo's whole honesty about what the site publishes.
    for present in ("text", "author_name", "like_count", "like_count_text",
                    "published_time_text", "published_at_approx",
                    "published_at_precision", "reply_level", "parent_id",
                    "sort", "data_source"):
        check("the column `%s` is present" % present, present in names)
    # Every count on a comment row is APPROXIMATE, so every one of them has
    # the site's own text beside it. A number with no text column would be
    # a magnitude presented as a fact (§8).
    for number in ("like_count", "reply_count"):
        check("`%s` has a `%s_text` beside it" % (number, number),
              number + "_text" in names)
    # A video row states its figures exactly, which is the asymmetry
    # `--mode video` exists for.
    for present in ("published_at", "duration_seconds", "category",
                    "comments_enabled", "view_count"):
        check("the video column `%s` is present" % present,
              present in video_names)
    doc = open(os.path.join(HERE, "output_writer.py"), encoding="utf-8").read()
    check("...and why a comment's numbers come in pairs is written down",
          "does not publish an exact like count" in doc)

def check_page_and_position_are_unique_across_pages():
    """§18: `position` restarts at 1 on every page, so without the page
    number beside it a row from page 2 claims a slot page 1 already used."""
    rows = comments("comments_top_p1", page=1) + \
        comments("comments_top_p2", page=2)
    pairs = [(r.page, r.position) for r in rows]
    equal("page+position is unique across a multi-page run",
          len(set(pairs)), len(pairs))
    check("the page number really reaches the row",
          {r.page for r in rows} == {1, 2}, str({r.page for r in rows}))

def check_fixtures_carry_no_personal_names():
    """§10: these captures DO carry people, so the guard is load-bearing.

    Most repos in this family read a catalogue, and this check is a
    precaution there. Here it is not: every comment in a real capture is a
    private individual's display name, channel, avatar and own words, and
    `make_fixtures.py` rewrites all four before anything reaches disk.

    Guarded by PATTERN rather than by the literals that were scrubbed, so
    a FUTURE capture carrying a value this scrubber never knew about is
    caught rather than only today's being clean.
    """
    blob = json.dumps(FIX, ensure_ascii=False)

    # Every author the fixtures name must be a placeholder.
    handles = set(re.findall(r'"displayName":\s*"([^"]*)"', blob))
    handles |= set(re.findall(r'"authorText":\s*\{"runs":\s*\[\{"text":\s*"([^"]*)"',
                              blob))
    for handle in handles:
        check("author %r is a placeholder" % handle,
              handle.startswith("@fixture_user_"),
              "a real commenter's name reached the fixtures")
    check("the fixtures name at least one author", bool(handles))

    # Every COMMENTER's channel and avatar likewise — walked through the
    # comment entities rather than grepped, because a channel id looks
    # identical whether it belongs to a commenter or to the channel that
    # PUBLISHED the video, and the publisher is the site's catalogue.
    seen_authors = 0
    for name, payload in FIX.items():
        for entity in product_parser._entity_index(payload).values():
            author = entity.get("author") or {}
            seen_authors += 1
            check("%s: commenter channel id is a placeholder" % name,
                  str(author.get("channelId", "")).startswith("UCF"),
                  str(author.get("channelId")))
            check("%s: commenter avatar is a placeholder" % name,
                  "FIXTURE_AVATAR" in str(author.get("avatarThumbnailUrl", "")),
                  str(author.get("avatarThumbnailUrl"))[:60])
            check("%s: comment id is a placeholder" % name,
                  str((entity.get("properties") or {}).get("commentId", ""))
                  .startswith(("FIXTURE_CMT_", "REPLY_")),
                  str((entity.get("properties") or {}).get("commentId")))
    check("the fixtures carry commenters to check", seen_authors > 10,
          str(seen_authors))

    # And no session material (§10): a real dump carries the session that
    # fetched it.
    for pattern in (r'"sessionId"', r'anti-csrftoken', r'"SAPISID"',
                    r'\bBearer [A-Za-z0-9._-]{20,}',
                    r'"visitorData":\s*"[A-Za-z0-9%+/=_-]{40,}"'):
        found = re.search(pattern, blob)
        check("the fixtures carry no %s" % pattern, found is None,
              found.group(0)[:60] if found else "")

    # The site's own catalogue is deliberately NOT scrubbed, and that is
    # the other half of the rule: a public video and its publisher are not
    # a private individual, and keeping them is what lets the value checks
    # above assert something real.
    check("the video's own title is kept verbatim", VIDEO_TITLE in blob)
    check("the video's publisher is kept verbatim", "Rick Astley" in blob)

def check_state_policy():
    import page_flow
    equal("every state has a policy",
          sorted(page_flow.STATE_POLICY),
          ["challenge", "comments_disabled", "content", "empty", "error",
           "parse_error", "unknown", "video_unavailable"])
    check("content: parsed, not retried, not blocked",
          page_flow.should_parse("content")
          and not page_flow.should_retry("content")
          and not page_flow.counts_as_blocked("content"))
    # ONE blocked state, and on this site it has never been reached: 60
    # consecutive InnerTube pages from a bare datacentre address drew no
    # refusal on 2026-09-21. Solving is True so that a challenge appearing
    # tomorrow is met with the tools this repo already has rather than with
    # a code change; retrying is True because on every sibling site that
    # DOES refuse, a different exit clears it far more cheaply than a
    # solve. Both are cautious settings rather than measured ones here, and
    # page_flow says so.
    check("challenge: retried, solvable, counts as blocked",
          page_flow.should_retry("challenge")
          and page_flow.should_solve("challenge")
          and page_flow.counts_as_blocked("challenge"))
    # A served page we could not read is OUR bug, never "0 jobs" (§20), so
    # it neither counts as blocked nor buys a solve.
    check("parse_error: retried once, never solved, NOT blocked",
          page_flow.should_retry("parse_error")
          and not page_flow.should_solve("parse_error")
          and not page_flow.counts_as_blocked("parse_error")
          and not page_flow.should_parse("parse_error"))
    # The two states that answer HTTP 200 with a large, healthy-looking
    # payload and are REAL ANSWERS rather than faults. Retrying either
    # re-asks a question the site already answered; calling either a block
    # sends a user rotating proxies over a video that simply has its
    # comments switched off.
    for answered in ("comments_disabled", "video_unavailable"):
        check("%s: not retried, not blocked, never solved" % answered,
              not page_flow.should_retry(answered)
              and not page_flow.counts_as_blocked(answered)
              and not page_flow.should_solve(answered))
    check("unknown: retried, not solved, NOT blocked",
          page_flow.should_retry("unknown")
          and not page_flow.should_solve("unknown")
          and not page_flow.counts_as_blocked("unknown"))
    check("an unrecognised state falls back to unknown's policy",
          page_flow.should_retry("something-new")
          and not page_flow.should_solve("something-new"))
    equal("at most one solve per page", page_flow.SOLVES_PER_PAGE, 1)

def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code.

    `RETRY_ON_BLOCKED` carried a paragraph of justification in a sibling repo
    and no engine consulted it, so setting it False changed nothing.
    """
    import page_flow
    sources = []
    for name in ("playwright_scraper.py", "selenium_scraper.py",
                 "puppeteer_scraper.py", "scraper_api_client.py"):
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            sources.append(open(path, encoding="utf-8").read())
    joined = "\n".join(sources)
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                     "SOLVES_PER_PAGE"):
        check("page_flow.%s is CONSULTED by an engine" % constant,
              constant in joined,
              "defined in page_flow and read by nothing")
    for fn in ("pages_to_plan", "ready_selector", "min_matches",
               "content_timeout_ms", "wait_for_count", "classify",
               "should_retry", "should_solve", "counts_as_blocked",
               "should_parse", "concurrency_limit",
               "pagination_is_addressable"):
        check("page_flow.%s has a caller outside its own module" % fn,
              fn in joined, "unused policy")

def check_csv_and_json_writers():
    from output_writer import Comment as JobPosting, write_csv, write_json
    import product_parser as P
    rows = comments("comments_top_p1")
    # `mentions` is the only list column on a comment row, and it is rare
    # in real data — 1 of 70 captured comments carried an @-mention. So it
    # is set here explicitly: this check is about the WRITER, and waiting
    # for a fixture that happens to contain one would leave the list path
    # untested most of the time.
    rows[0].mentions = ["UCFFFFFFFFFFFFFFFFFFFF01", "UCFFFFFFFFFFFFFFFFFFFF02"]
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(rows, csv_path, row_cls=JobPosting)
        with open(csv_path, encoding="utf-8") as f:
            reader = list(csv.reader(f))
        equal("CSV header matches the dataclass, in order",
              reader[0], [f.name for f in fields(JobPosting)])
        equal("CSV holds every row", len(reader) - 1, len(rows))
        badges_col = reader[0].index("mentions")
        joined = [r[badges_col] for r in reader[1:] if r[badges_col]]
        check("a list column is joined readably rather than repr()'d",
              any(" | " in v for v in joined), repr(joined[:2]))
        check("no Python list repr leaked into the CSV",
              not any(cell.startswith("[") for row in reader[1:] for cell in row))

        empty_csv = os.path.join(tmp, "empty.csv")
        write_csv([], empty_csv, row_cls=JobPosting)
        with open(empty_csv, encoding="utf-8") as f:
            header = list(csv.reader(f))
        equal("an EMPTY csv still carries its header", len(header), 1)
        equal("...and it is the right one", header[0],
              [f.name for f in fields(JobPosting)])

        json_path = os.path.join(tmp, "out.json")
        write_json(rows, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        equal("JSON holds every row", len(loaded), len(rows))
        equal("JSON keys are the dataclass fields, in order",
              list(loaded[0].keys()), [f.name for f in fields(JobPosting)])
        with_list = next(r for r in loaded if r["mentions"])
        check("a list column stays a real list in JSON",
              isinstance(with_list["mentions"], list),
              repr(with_list["mentions"]))
        # An empty list and a null both mean "no restriction stated", so
        # both are written as null rather than putting a distinction in the
        # data that is not in the site.
        check("...and an unrestricted listing carries null, never []",
              all(r["mentions"] is None
                  or r["mentions"] for r in loaded))

def check_exit_codes():
    import output_writer as O
    equal("0 ok / 1 crash / 2 usage / 3 blocked / 4 empty / 5 api / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_API_ERROR, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    check("page_cap_reached is a COMPLETE stop reason",
          "page_cap_reached" in O.COMPLETE_STOP_REASONS)
    # `/explore` and `/careers` are each served at ONE address holding
    # their whole result set — measured, not assumed: every pagination
    # parameter tried returned a byte-identical payload — so a run that
    # stopped after one fetch fetched the whole route.
    check("single_page_route is complete by construction AND by measurement",
          "single_page_route" in O.COMPLETE_STOP_REASONS)
    # Carried for the family's shared vocabulary and unreachable here: this
    # site cannot clamp an out-of-range page back, having only one.
    check("page_echo_mismatch is complete",
          "page_echo_mismatch" in O.COMPLETE_STOP_REASONS)
    check("...and an enumeration that yielded nothing is NOT complete",
          "enumeration_empty" not in O.COMPLETE_STOP_REASONS)
    check("no_new_products is complete",
          "no_new_products" in O.COMPLETE_STOP_REASONS)

def check_a_run_that_finds_nothing_writes_nothing():
    """Never replace last night's good output with []."""
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        code = save([], prefix, "json", allow_empty=False)
        equal("an empty run exits 4", code, 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(),
              '[{"sku": "yesterday"}]')
        code = save([], prefix, "json", allow_empty=True)
        equal("--allow-empty WRITES the empty file...", 
              json.load(open(prefix + ".json", encoding="utf-8")), [])
        # ...and still reports exit 4. Pinned deliberately (§10: pin a known
        # behaviour rather than half-guarding it): "zero businesses" is true
        # whether or not the file was written, and a caller that wanted the
        # file still wants to know the result was empty.
        equal("...and still reports exit 4, because it IS empty", code, 4)

def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="single_page_route",
                    pages_requested=1, pages_completed=1, pages_failed=[],
                    products=390, mode="listings", source="mercor.com",
                    start_url="https://work.mercor.com/explore",
                    final_url="https://work.mercor.com/explore",
                    extra={"records_in_payload": 390, "urls_in_itemlist": 326,
                           "pages_available": 1, "route_is_paginated": False})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source"):
        check("the sidecar records %r" % key, key in meta)
    equal("the sidecar carries how many records the payload held",
          meta["records_in_payload"], 390)
    # Both views, because the response has two and they disagree. Without
    # the second number a reader cannot tell that the site's own structured
    # index is 64 entries short of its own payload — which is the whole
    # reason this scraper does not read that index.
    equal("...and how many the site's own ItemList indexed",
          meta["urls_in_itemlist"], 326)
    equal("...and whether this route is addressable page by page",
          meta["route_is_paginated"], False)
    equal("pages_failed is a LIST of numbers, not a count",
          isinstance(meta["pages_failed"], list), True)

def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None

def check_engines_import_their_driver_at_module_level():
    """For the guarded imports above to MEAN anything.

    A sibling repo imported `launch`/`connect` inside the launch path, so the
    module imported cleanly with no pyppeteer installed: the group never
    skipped, and the CI job that exists to fail on unexpected skips could not
    have caught a broken import. It also let CI run against a stub version
    for a while without anything noticing. This drifts back silently, so it
    is asserted with an `ast` walk rather than trusted.
    """
    for module, driver in DRIVER_IMPORTS.items():
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            check("%s exists" % module, False)
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:          # module level ONLY
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver),
              driver in top_level,
              "top-level imports: %s" % sorted(top_level))

def check_shared_calls_bind_against_the_real_signature():
    """§17's check #1, and the one that earns its keep.

    A sibling repo shipped `classify(html, url=…)` in two of three engines
    against a callee taking `status` second, and BOTH crashed on their first
    fetch — invisible to import, --help, compileall, the undefined-name walk
    and 400+ green assertions, because none of those calls a function the way
    a live run does.

    This walks every engine's AST for calls into the shared modules and binds
    each one against the callee's real signature.
    """
    # EVERY shared module, not the three that are easy. A sibling repo
    # widened this after a key arrived and five calls into functions that
    # never existed came out of the credential-gated paths — the ones
    # nobody runs, for the obvious reason (CLAUDE.md §16). Two of the
    # modules below are reachable only with a key.
    import captcha_solver
    import diff_runs
    import env_config
    import fingerprint_client
    import output_writer
    import page_flow
    import product_parser
    import proxy_pool
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer, "proxy_pool": proxy_pool,
               "fingerprint_client": fingerprint_client,
               "captcha_solver": captcha_solver, "env_config": env_config,
               "diff_runs": diff_runs}
    bound = 0
    for module in ENGINES + ("scraper_api_client",):
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        # Which shared names this file imported directly (`from x import y`).
        direct = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (
                        targets[node.module], alias.name)

        # A name bound ANYWHERE in this file shadows a same-named module
        # (CLAUDE.md §22). An engine that takes `proxy_pool` as a parameter
        # is calling a method on an object, not a module attribute, and
        # without this rule that reported twenty-one false positives on a
        # clean repo in a sibling. Parameters count whether or not they
        # carry a type annotation — an annotation is not what makes a name
        # a local.
        shadowed = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spec = node.args
                for arg in (list(spec.args) + list(spec.posonlyargs)
                            + list(spec.kwonlyargs)
                            + [a for a in (spec.vararg, spec.kwarg) if a]):
                    shadowed.add(arg.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                shadowed.add(node.id)
        local_targets = {name: mod for name, mod in targets.items()
                         if name not in shadowed}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            owner = attr = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in local_targets:
                    owner, attr = local_targets[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in direct:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            # A name that is NOT THERE is the loudest possible failure and
            # this check used to swallow it: `getattr(..., None)` returned
            # None, `not callable(None)` was true, and the call was skipped.
            # Three calls into a page_flow API that does not exist in this
            # repo -- comparable(), next_page_selector(),
            # next_page_candidates(), all of them Tokopedia's, all arriving
            # with copied code -- sat in two engines under a green run of
            # this very function. Absent is not "nothing to bind".
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)"
                      % (getattr(owner, "__name__", owner), attr,
                         module + ".py", node.lineno),
                      False,
                      "the engine calls a name the shared module does not "
                      "define; a live run reaches this as AttributeError")
                continue
            callee = getattr(owner, attr)
            if not callable(callee):
                continue
            if inspect.isclass(callee):
                # A CONSTRUCTOR is a call like any other, and skipping it
                # is how `SolveBudget(limit=…)` or `ProxyPool(rotate=…)`
                # with a wrong keyword reaches a live run untested. Bind
                # against `__init__` with `self` already supplied.
                try:
                    signature = inspect.signature(callee.__init__)
                    signature = signature.replace(
                        parameters=list(signature.parameters.values())[1:])
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    signature = inspect.signature(callee)
                except (TypeError, ValueError):
                    continue
            positional = [inspect.Parameter.empty] * len(node.args)
            keywords = {}
            for kw in node.keywords:
                if kw.arg is None:
                    break
                keywords[kw.arg] = inspect.Parameter.empty
            else:
                try:
                    signature.bind(*positional, **keywords)
                    bound += 1
                except TypeError as exc:
                    check("%s:%d %s.%s(...) binds against its real signature"
                          % (module, node.lineno,
                             getattr(owner, "__name__", owner), attr),
                          False,
                          "%s; signature is %s" % (exc, signature))
    check("every shared-module call in every engine binds (%d checked)" % bound,
          bound > 40, "only %d calls were checked — is the walk finding them?"
          % bound)

def _argparse_flags(module_name):
    """Every --flag a module's parser defines, without running the CLI."""
    path = os.path.join(HERE, module_name + ".py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    # Only calls on the argparse parser itself. A browser's option object
    # also has `add_argument`, and counting Chrome's own switches
    # (`--no-sandbox`, `--window-size=…`) as CLI flags made this check
    # compare nonsense.
    parsers = {"p"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("add_argument_group",
                                             "add_mutually_exclusive_group")):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    parsers.add(target.id)
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parsers):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.startswith("--"):
                    flags.add(arg.value)
    return flags

def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both ways.

    A missing flag fails; so does closing a difference the README documents.
    """
    sets = {}
    for module in ENGINES:
        if not os.path.exists(os.path.join(HERE, module + ".py")):
            continue
        sets[module] = _argparse_flags(module)
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | SITE_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
    # NO documented differences on this site, and that is a stronger
    # statement than a list: the three engines were generated from one
    # file with only their driver layer swapped, so their flag sets are
    # identical by construction. Growing a difference — in either
    # direction — has to be a decision, and this empty map is what makes
    # it fail the build until someone writes down why.
    DOCUMENTED_DIFFERENCES = {}
    names = sorted(sets)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
        only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
        check("%s and %s define the same flags" % (a, b),
              not only_a and not only_b,
              "only in %s: %s; only in %s: %s"
              % (a, sorted(only_a), b, sorted(only_b)))

def check_the_concurrency_difference_is_documented_in_both_directions():
    """§20: the exception list IS the documentation.

    On most sites in this family only the Playwright engine implements
    `--concurrency` and its twins log that they ignore it. Here all three
    implement it, because the three engines are one file with three driver
    layers — so the thing to pin is that they STAY the same, and that the
    limit which actually matters is the one in the shared policy rather
    than one hidden in an engine.

    The real difference on this site is not between engines at all: it is
    between MODES. A comments run walks a token chain and cannot be split;
    a video run can. Both directions are pinned, so closing the difference
    is a decision rather than a surprise.
    """
    src = {}
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if os.path.exists(path):
            src[module] = open(path, encoding="utf-8").read()

    for module, text in src.items():
        check("%s implements concurrency" % module,
              "_fetch_videos_concurrently" in text,
              "this engine accepts --concurrency and would ignore it")
        check("%s consults the shared policy for it" % module,
              "concurrency_for_mode" in text and "concurrency_limit" in text)
        check("%s gives each worker its own pool" % module,
              "_worker_pool" in text)

    equal("comments cannot be parallelised",
          page_flow.concurrency_for_mode("comments", 4), 1)
    equal("search cannot either",
          page_flow.concurrency_for_mode("search", 4), 1)
    equal("video can", page_flow.concurrency_for_mode("video", 4), 4)

    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    check("the README says which mode parallelises",
          "--mode video" in readme and "concurrency" in readme.lower(),
          "the mode difference is not in the README")

def check_banned_and_removed_flags():
    """Scoped to the ENGINES.

    `--country` is banned on the engines and there is no exception here:
    YouTube serves one site, and a country flag on a scraper could only
    contradict what the URL already says. The InnerTube `gl` this repo
    does take is `--region`, which is a rendering hint to the site rather
    than a claim about where the request comes from, and it is named
    differently for exactly that reason. On `fingerprint_client.py` the
    name `--country` is legitimate — there it picks a fingerprint locale,
    not a target — which is why this check is scoped to the engines rather
    than to the tree (CLAUDE.md §10).

    What the rule is really about is an option that can disagree with
    reality, and the three that CAN on this site are all refused with
    their reason rather than silently absorbed:

      * a URL with no video id in it — a channel or a playlist has no
        comment section of its own, and "not a YouTube URL" would be a lie
        about a YouTube URL;
      * `--proxy` with `--cdp-endpoint`, because the Scraping Browser
        already proxies and stacking two exits is not better cover;
      * `--concurrency` above 1 in a mode that walks a token chain, where
        a second worker would have no address to fetch.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        for flag in BANNED_FLAGS:
            check("%s does not define %s" % (module, flag),
                  '"%s"' % flag not in source)
        check("%s refuses a URL with no video id in it" % module,
              "is_supported_url" in source and "p.error(reason)" in source,
              "a channel URL would be fetched and parse to nothing")
        check("%s refuses --proxy stacked on --cdp-endpoint" % module,
              "already proxies" in source,
              "the Scraping Browser supplies its own exit")
        check("%s clamps --concurrency with the reason" % module,
              "concurrency_for_mode" in source
              and "no address to fetch" in source,
              "a mode that cannot parallelise must say so, not ignore it")

def check_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE.

    A live run of a sibling repo's pyppeteer engine died with NameError on a
    line reached only while fetching, after an import had been removed — the
    module imported cleanly, --help worked, compileall passed and CI was
    green. Kept COARSE (pooled bindings, no scope tracking) so it
    under-reports rather than inventing problems.
    """
    import builtins
    modules = [f for f in sorted(os.listdir(HERE))
               if f.endswith(".py") and f != "smoke_test.py"]
    for filename in modules:
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        # Module-level dunders exist without being assigned anywhere.
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.alias) and node.asname:
                defined.add(node.asname)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % filename, not unresolved,
              "%s" % unresolved)

def _import_graph(entrypoint):
    """Every local module an entrypoint reaches, transitively."""
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, queue = set(), [entrypoint]
    while queue:
        name = queue.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(open(os.path.join(HERE, name + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                queue.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                queue.append(node.module.split(".")[0])
    return seen

def check_dockerfile_copies_everything_the_entrypoint_imports():
    """§10: all three repos in this family shipped an image that died with
    ModuleNotFoundError on every invocation, --help included, because
    proxy_pool.py was missing from the COPY list. CI never built the image;
    this check needs no Docker."""
    path = os.path.join(HERE, "Dockerfile")
    if not os.path.exists(path):
        check("Dockerfile exists", False)
        return
    dockerfile = open(path, encoding="utf-8").read()
    # Only the COPY instructions, continuations included — a comment above
    # them naming a file is not a file the image carries.
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    entry = re.search(r'(?:CMD|ENTRYPOINT)\s*\[?\s*"?(?:python3?"?,\s*"?)?'
                      r'([A-Za-z_][A-Za-z0-9_]*)\.py', dockerfile)
    entrypoint = entry.group(1) if entry else "playwright_scraper"
    needed = _import_graph(entrypoint)
    missing = sorted(needed - copied)
    check("the Dockerfile COPYs every module %s.py imports" % entrypoint,
          not missing, "missing %s" % missing)
    for unwanted in ("smoke_test", "test_smoke"):
        check("the image does not carry %s.py" % unwanted,
              unwanted not in copied)

def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    path = os.path.join(HERE, ".env.example")
    if not os.path.exists(path):
        check(".env.example exists", False)
        return
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", 
                                open(path, encoding="utf-8").read(), re.M))
    read = set(env_config.ENV_KEYS)
    check("every variable the loader reads is documented",
          not (read - documented), "undocumented: %s" % sorted(read - documented))
    check("every documented variable is actually read",
          not (documented - read), "unread: %s" % sorted(documented - read))

def check_a_copied_env_example_reads_as_UNSET():
    """§17: `cp .env.example .env` followed by a run must not connect.

    The placeholder check was a literal set in a sibling repo, and the two
    credentialled URLs are documented the way the vendor documents them —
    `ws://{login}-zone-…:{password}@cb.2captcha.com:9222` — so neither
    literal matched, the run connected with the string `{login}-zone-…` as
    its username, and got a 401 a long way from its cause.
    """
    import env_config
    example = os.path.join(HERE, ".env.example")
    if not os.path.exists(example):
        check(".env.example exists", False)
        return
    text = open(example, encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    check("the example actually sets every variable",
          set(values) == set(env_config.ENV_KEYS),
          "example has %s, loader reads %s"
          % (sorted(values), sorted(env_config.ENV_KEYS)))
    # Every CREDENTIAL must read as unset. The default TARGET must not: it is
    # a real, usable URL, and blanking it would remove the one setting this
    # file exists to make convenient (§17's check #3 says exactly this — the
    # credentials unset, the non-credential default still usable).
    CREDENTIALS = {"TWOCAPTCHA_KEY", "YOUTUBE_CDP_ENDPOINT",
                   "YOUTUBE_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in CREDENTIALS:
                check("a copied .env.example leaves %s unset" % name,
                      got is None, "got %r" % got)
            else:
                check("...while %s stays a usable default" % name,
                      got == raw.strip(), "got %r" % got)
    finally:
        os.environ.clear()
        os.environ.update(before)
    # And the counter-check: a real credential must still come through, or
    # the placeholder rule would have made the loader useless. Deliberately
    # NOT 32 hex characters — that is the shape of a real 2captcha key, and
    # this repo's own credential scan (rightly) fails on one.
    try:
        os.environ["TWOCAPTCHA_KEY"] = "not-a-real-key-but-a-real-value"
        equal("a real value is still read",
              env_config.env_value("TWOCAPTCHA_KEY"),
              "not-a-real-key-but-a-real-value")
    finally:
        os.environ.clear()
        os.environ.update(before)

def check_ci_greps_for_a_sentinel_this_suite_can_actually_emit():
    """The `engine-smoke` job must be ABLE to fail.

    That job exists for one reason (CLAUDE.md §10): "skipped, engine absent"
    reads identically to a real import error, so CI installs each engine and
    fails if THAT engine's group still reports a skip. It works by grepping
    the suite's own output for a sentinel.

    The inherited version grepped for `"<engine>_scraper could not be
    imported"` — a string no suite in this family emits. Checked 2026-09-18,
    seven sibling repos carry the same dead grep, so in none of them could
    the job ever have failed. It passed for the wrong reason, which is
    §22's "a check that swallows the loudest failure it could report".

    This check is the guard against that coming back: whatever sentinel the
    workflow looks for, this suite has to be capable of printing it. It
    verifies the sentinel against `skip()`'s real output format rather than
    against a copy of the string, so changing either one without the other
    fails here.
    """
    workflow = os.path.join(HERE, ".github", "workflows", "tests.yml")
    if not os.path.isdir(os.path.join(HERE, ".github")):
        # §22: trigger on the WHOLE .github directory being absent — which
        # is the Docker image, where it is deliberately not COPYed — and
        # never on a file inside it going missing, because a check that
        # quietly starts passing once its input disappears is the failure
        # mode this whole function is about.
        skip("ci-sentinel", "no .github/ in this tree (the Docker image)")
        return
    check("tests.yml exists", os.path.exists(workflow))
    if not os.path.exists(workflow):
        return
    text = open(workflow, encoding="utf-8").read()

    # What `skip()` actually prints, derived rather than quoted.
    import io as _io, contextlib as _contextlib
    buf = _io.StringIO()
    before = len(SKIPS)
    with _contextlib.redirect_stdout(buf):
        skip("playwright_scraper", "engine library absent (probe)")
    del SKIPS[before:]          # leave the run's real skip list untouched
    printed = buf.getvalue()
    check("skip() prints a line naming the engine", "playwright_scraper" in printed,
          repr(printed))

    # The sentinel the workflow greps for, with the matrix placeholder
    # resolved the way Actions would resolve it.
    greps = re.findall(r'grep -q(?:E)? "([^"]*matrix\.engine[^"]*)"', text)
    check("the engine-smoke step greps for something", bool(greps),
          "no grep against ${{ matrix.engine }} found in tests.yml")
    for pattern in greps:
        resolved = pattern.replace("${{ matrix.engine }}", "playwright")
        check("the CI sentinel %r is a string this suite can emit" % resolved,
              resolved in printed,
              "the workflow greps for %r but skip() prints %r — the job "
              "cannot fail" % (resolved, printed.strip()))

    # And the other half: the step must confirm the suite RAN, or a crash on
    # line one sails past a grep for an absent string.
    check("the engine-smoke step also asserts the suite ran to completion",
          "checks passed" in text,
          "nothing in tests.yml checks for the suite's summary line")

def check_credential_scan_is_one_implementation_invoked_from_both():
    """§17: two sources of truth, one dead and one holed.

    `.github/ci_checks.py` sat in three repos invoked by NOTHING, while
    tests.yml carried an inline grep doing a narrower version of the same job
    — one that matched only ws:// and wss://, so an http://user:pass@
    credential would have sailed past CI.
    """
    script = os.path.join(HERE, ".github", "ci_checks.py")
    check("the credential scan exists as a script", os.path.exists(script))
    if not os.path.exists(script):
        return
    workflow_dir = os.path.join(HERE, ".github", "workflows")
    workflow = os.path.join(workflow_dir, "tests.yml")
    # Triggered on the whole .github directory being absent, never on this one
    # file being missing: two suites in this family run INSIDE the Docker
    # image, which deliberately COPYs no .github/, and a check that quietly
    # starts passing once its input disappears is the same failure this
    # function is about (CLAUDE.md §22).
    if not os.path.isdir(workflow_dir):
        skip("ci-wiring", "no .github/ in this tree (the Docker image)")
    elif os.path.exists(workflow):
        text = open(workflow, encoding="utf-8").read()
        check("CI INVOKES the script rather than reimplementing it",
              "ci_checks.py" in text)
        # ...and does not ALSO reimplement it. The original version of this
        # check asserted only the first half, and the workflow carried inline
        # `python - <<EOF` copies of the --help and sample checks alongside
        # the call — justified in a comment as keeping the two from drifting
        # apart. They drifted: the inline sample copy still imported the row
        # dataclass under a name this repo renamed, and it failed on the
        # repo's FIRST push while the script it duplicated passed.
        #
        # Scoped to the OFFLINE job, because the docker job legitimately
        # names `sample_output.json` for a different purpose — asserting the
        # image does NOT contain it. A guard that fired there would be wrong,
        # and a guard people have to argue with is one they learn to
        # suppress.
        offline = text.split("  engine-smoke:", 1)[0]
        for marker, what in (("from output_writer import", "the row schema"),
                             ("sample_output.json", "the sample output"),
                             ("subprocess.run([sys.executable", "the --help contract")):
            check("the offline job does not reimplement the check for %s" % what,
                  marker not in offline,
                  "tests.yml's offline job mentions %r — one implementation, "
                  "in ci_checks.py, invoked from both" % marker)
        # And the guard must have had something to read, or it passed for the
        # wrong reason (CLAUDE.md §22).
        check("...and the offline job was actually found to scan",
              "ci_checks.py" in offline, "no offline job in tests.yml")
    result = subprocess.run([sys.executable, script, "--all"], cwd=HERE,
                            capture_output=True, text=True)
    check("the credential scan passes on this repo's own tree",
          result.returncode == 0,
          (result.stdout + result.stderr)[-600:])

def check_the_credential_scan_survives_a_venv_in_the_tree():
    """A guard people have to argue with is one they learn to suppress.

    Found by cloning this repo the way a stranger does and following the
    README: `python3 -m venv` puts a virtualenv in the working tree, and the
    credential scan walked into pip's vendored code and flagged a 32-hex
    string in `_elffile.py` as key-shaped. Correct about the string, wrong
    about the file, and the first thing a new user would have seen.

    The fix is structural rather than a longer list of names — a directory
    holding `pyvenv.cfg` is a virtualenv whatever it is called — and this
    pins BOTH halves, because narrowing a credential scan is exactly how one
    stops catching things. CLAUDE.md §22 records a sibling repo whose scan
    caught an UNTRACKED `.env.bak` holding a live key, so scanning must not
    be reduced to tracked files.
    """
    import importlib.util
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.exists(script):
        skip("credential-scan", "no .github/ in this tree (the Docker image)")
        return
    spec = importlib.util.spec_from_file_location("_ci_checks", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    check("the scan knows a virtualenv structurally, not by name",
          hasattr(mod, "_is_virtualenv"))
    if not hasattr(mod, "_is_virtualenv"):
        return

    with tempfile.TemporaryDirectory() as tmp:
        odd = os.path.join(tmp, "whatever-i-called-it")
        os.makedirs(os.path.join(odd, "lib"))
        open(os.path.join(odd, "pyvenv.cfg"), "w").write("home = /usr\n")
        check("...so a venv under any name is recognised",
              mod._is_virtualenv(pathlib.Path(odd)))
        plain = os.path.join(tmp, "src")
        os.makedirs(plain)
        check("...and an ordinary directory is not",
              not mod._is_virtualenv(pathlib.Path(plain)))

    # The other half: it must still walk files git does not track, because a
    # key pasted into a scratch file is the case this scan exists for.
    scanned = [str(p) for p in mod.scanned_files()]
    check("the scan still reads this repo's own files", len(scanned) > 20,
          "%d file(s)" % len(scanned))
    check("...and is not limited to git's index",
          "git ls-files" not in open(script, encoding="utf-8").read())

def check_banned_wording():
    """§12: enforced by this test rather than by review."""
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", ".pytest_cache", "node_modules")]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            for phrase in BANNED_WORDING:
                if phrase.lower() in text and filename != "smoke_test.py":
                    check("%s contains no %r" % (
                        os.path.relpath(path, HERE), phrase), False)
    check("banned-wording scan ran", True)

def check_concurrency_with_the_browser_stubbed():
    """§10: drive the concurrency machinery with no browser at all.

    A live run cannot always reach it — and on this site it can barely
    reach it at all, because only `--mode video` parallelises and a
    blocked first video would end the run before any worker started. So
    the queue, the ordering and the worker teardown are exercised here
    against stubs, which is the only place they can be exercised
    deterministically.

    What is asserted: every queued video is fetched EXACTLY once, the
    results are restorable to the order they were asked for regardless of
    which worker finished first, and no worker is left running.
    """
    engines = [(m, _import_engine(m)) for m in ENGINES]
    engines = [(m, e) for m, e in engines if e is not None]
    if not engines:
        skip("concurrency", "no engine library is importable here")
        return

    class Args:
        mode = "video"
        concurrency = 4
        headless = True
        cdp_endpoint = None
        proxy = None
        proxy_file = None
        proxy_rotate = "per-run"
        fingerprint = False
        twocaptcha_key = None
        fp_tags = None
        fp_country = None
        locale = "en"
        region = "US"
        delay = 0
        retries = 0
        retry_delay = 0
        solve_captcha = "never"
        dump_html = False
        out = "unused"

    for _module, engine in engines:
        ids = ["id%08d" % n for n in range(1, 18)]
        fetched = []
        lock = threading.Lock()

        def fake_fetch_one(session_box, pw, args, pool, video_id, number,
                           scraped_at):
            with lock:
                fetched.append((number, video_id))
            outcome = engine.PageOutcome(number=number)
            outcome.rows = [object()]
            outcome.state = "content"
            return outcome

        class FakeSession:
            client_version = "0"
            proxy_url = None

            def close(self):
                pass

        originals = (engine._fetch_one_video, engine._open_session,
                     engine._prime_session, engine._driver_context)
        try:
            engine._fetch_one_video = fake_fetch_one
            engine._open_session = lambda pw, args, pool: FakeSession()
            engine._prime_session = lambda session, args, url: 200
            engine._driver_context = _NullContext
            outcomes = engine._fetch_videos_concurrently(
                None, Args(), None, ids, "2026-01-01T00:00:00Z", 4)
        finally:
            (engine._fetch_one_video, engine._open_session,
             engine._prime_session, engine._driver_context) = originals

        equal("%s: every queued video was fetched" % _module, len(fetched), len(ids))
        equal("...exactly once each", len(set(n for n, _ in fetched)), len(ids))
        equal("...and none was invented",
              sorted(v for _, v in fetched), sorted(ids))
        equal("an outcome came back for each", len(outcomes), len(ids))
        outcomes.sort(key=lambda o: o.number)
        equal("outcomes are restorable to the order they were asked in",
              [o.number for o in outcomes], list(range(1, len(ids) + 1)))
        check("no worker thread is left running",
              not any(t.name.startswith("Thread-") and t.is_alive()
                      for t in threading.enumerate()
                      if t is not threading.current_thread()))



def check_a_dead_worker_neither_hangs_nor_loses_its_siblings():
    """§10: a worker that raises must not take the run down with it.

    The failure this pins is the quiet one: a thread that dies holding
    queue items, so the run finishes "successfully" with a third of the
    videos silently missing and nothing in the log about it.
    """
    engines = [(m, _import_engine(m)) for m in ENGINES]
    engines = [(m, e) for m, e in engines if e is not None]
    if not engines:
        skip("concurrency", "no engine library is importable here")
        return

    class Args:
        mode = "video"
        concurrency = 3
        headless = True
        cdp_endpoint = None
        proxy = None
        proxy_file = None
        proxy_rotate = "per-run"
        fingerprint = False
        twocaptcha_key = None
        fp_tags = None
        fp_country = None
        locale = "en"
        region = "US"
        delay = 0
        retries = 0
        retry_delay = 0
        solve_captcha = "never"
        dump_html = False
        out = "unused"

    for _module, engine in engines:
        ids = ["id%08d" % n for n in range(1, 13)]

        def exploding_fetch(session_box, pw, args, pool, video_id, number,
                            scraped_at):
            if number % 4 == 0:
                raise RuntimeError("driver died on %s" % video_id)
            outcome = engine.PageOutcome(number=number)
            outcome.rows = [object()]
            outcome.state = "content"
            return outcome

        class FakeSession:
            client_version = "0"
            proxy_url = None

            def close(self):
                pass

        originals = (engine._fetch_one_video, engine._open_session,
                     engine._prime_session, engine._driver_context)
        try:
            engine._fetch_one_video = exploding_fetch
            engine._open_session = lambda pw, args, pool: FakeSession()
            engine._prime_session = lambda session, args, url: 200
            engine._driver_context = _NullContext
            outcomes = engine._fetch_videos_concurrently(
                None, Args(), None, ids, "2026-01-01T00:00:00Z", 3)
        finally:
            (engine._fetch_one_video, engine._open_session,
             engine._prime_session, engine._driver_context) = originals

        equal("%s: every video still produced an outcome" % _module, len(outcomes), len(ids))
        failed = [o for o in outcomes if not o.rows]
        equal("the ones that raised are reported, not dropped", len(failed), 3)
        check("...and each names what went wrong",
              all("driver died" in (o.error or "") for o in failed),
              str([o.error for o in failed][:1]))
        survivors = [o for o in outcomes if o.rows]
        equal("a sibling's failure loses none of the others", len(survivors), 9)
        check("no worker thread is left running",
              not any(t.name.startswith("Thread-") and t.is_alive()
                      for t in threading.enumerate()
                      if t is not threading.current_thread()))

def check_worker_pools_start_on_different_exits():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    firsts = [engine._worker_pool(pool, i).current for i in range(3)]
    equal("three workers start on three different exits",
          len(set(firsts)), 3)
    equal("a missing pool stays missing", engine._worker_pool(None, 0), None)

def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    """§10: an unknown key in new_context(**kwargs) is a TypeError at launch,
    on the PAID path, at runtime."""
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    try:
        from fingerprint_client import playwright_context_kwargs
    except ImportError as e:
        skip("fingerprint", str(e))
        return
    sample = {"id": "x", "country": "US",
              "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US",
              "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    from playwright.sync_api import sync_playwright  # noqa: F401
    import playwright.sync_api as pw_api
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown,
          "unknown: %s" % unknown)

def check_every_engine_exposes_the_same_public_surface():
    """The three engines are one file with three driver layers.

    So this is not a hope about parallel maintenance — it is a fact that
    can go stale, and this check is what notices. Every name the shared
    half uses must exist in all three, and `PageOutcome` must have the
    same shape everywhere, because that object is what carries a worker's
    result back into page order.
    """
    seen = {}
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        for name in ("scrape", "parse_args", "PageOutcome", "MODES",
                     "_fetch_one_video", "_fetch_videos_concurrently",
                     "_worker_pool", "_run_comments", "_run_video",
                     "_run_search", "_open_session", "_prime_session",
                     "_fetch_with_policy", "handle_captcha_if_present",
                     "_proxy_failure", "_mask_credentials",
                     "_driver_context"):
            check("%s.%s exists" % (module, name), hasattr(engine, name))
        outcome = engine.PageOutcome(number=1)
        for field_name in ("number", "rows", "state", "status", "blocked",
                           "error", "next_token", "payload", "attempted"):
            check("%s.PageOutcome carries %r" % (module, field_name),
                  hasattr(outcome, field_name))
        equal("%s.PageOutcome starts empty" % module, outcome.rows, [])
        equal("%s.PageOutcome starts unblocked" % module, outcome.blocked,
              False)
        equal("%s names the same modes" % module, tuple(engine.MODES),
              ("comments", "video", "search"))
        seen[module] = sorted(n for n in dir(engine)
                              if not n.startswith("__"))

    # And against each OTHER, in both directions: a name that appears in
    # one engine and not its twins is a divergence whichever way it runs.
    names = sorted(seen)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = set(seen[a]) - set(seen[b]) - set(DRIVER_IMPORTS.values())
        only_b = set(seen[b]) - set(seen[a]) - set(DRIVER_IMPORTS.values())
        # Each engine legitimately imports its own driver's names.
        DRIVER_LOCAL = {"launch", "connect", "asyncio", "concurrent",
                        "PyppeteerError", "NetworkError", "PPTimeout",
                        "webdriver", "WebDriverException", "SETimeout",
                        "ChromeOptions", "By", "sync_playwright", "PWError",
                        "PWTimeout", "_Loop", "_FETCH_JS",
                        "RemoteBrowserError"}
        only_a -= DRIVER_LOCAL
        only_b -= DRIVER_LOCAL
        check("%s and %s expose the same names" % (a, b),
              not only_a and not only_b,
              "only in %s: %s; only in %s: %s"
              % (a, sorted(only_a), b, sorted(only_b)))

def check_every_solve_is_counted_against_the_budget():
    """`SOLVES_PER_PAGE` is a MONEY limit, so every call that can buy must
    be counted — CLAUDE.md §23.

    `handle_captcha_if_present` is called TWICE per attempt in every engine
    in this family: once before the response is classified (so a challenge
    is cleared before anything is judged) and once after, for the state
    that says the page really is gated. In every sibling repo only the
    SECOND was counted, so the first bought a solve on every block attempt,
    for free and silently. Measured in a sibling on 2026-09-17 from an
    address where a real Cloudflare challenge rendered on every fetch: one
    page bought THREE Turnstile solves with the cap set to 1.

    This repo fixes it by construction rather than by discipline. The
    budget is an OBJECT created once per page and handed to both call
    sites, and it counts the spend inside itself — so a caller cannot
    forget to, which is the failure mode the sibling had. What this check
    pins is that the object is still the only way to spend.

    Inherited rather than measured here: this site has refused nothing, so
    no run of this repo has ever bought a solve.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        calls = source.count("handle_captcha_if_present(session, args, budget)")
        check("%s calls the solver from two places, as designed" % module,
              calls == 2, "found %d call site(s)" % calls)
        check("%s creates exactly one budget per attempt loop" % module,
              source.count("budget = SolveBudget()") == 1,
              "%d budget object(s)" % source.count("budget = SolveBudget()"))
        check("%s never counts a spend by hand" % module,
              "solves_bought" not in source,
              "a hand-rolled counter is the thing SolveBudget replaces")

    # The object itself: the spend is counted inside, and the second
    # attempt is refused.
    budget = page_flow.SolveBudget()
    equal("the first spend is allowed", budget.spend(), True)
    equal("the second is not", budget.spend(), False)
    equal("and the count is kept by the object", budget.spent, 1)
    check("may_spend agrees with spend", not budget.may_spend())
    equal("a zero budget buys nothing", page_flow.SolveBudget(0).spend(), False)

    # And a solver call must sit behind it, not beside it.
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        handler = source[source.index("def handle_captcha_if_present"):]
        handler = handler[:handler.index("\n\n\n")]
        check("%s checks the budget before paying" % module,
              handler.index("budget.spend()") < handler.index("solve_recaptcha("),
              "the solver is reached before the budget is charged")

    equal("at most one purchase per page", page_flow.SOLVES_PER_PAGE, 1)

def check_a_dead_proxy_is_reported_as_a_proxy_failure():
    """CLAUDE.md §8: a proxy failure is not a timeout, and the two want
    opposite responses — another try at the same exit versus a different one.

    The engines all compute the reason (`_proxy_failure`) and, WITH a pool,
    log it on rotation. Without a pool — a single `--proxy`, which is the
    common case — an earlier version dropped it and reported only "gave up
    loading", so a refused proxy read exactly like a slow site. Found by
    running it rather than by reading it: `--proxy http://127.0.0.1:9`
    printed the generic message while `_proxy_failure()` had already
    identified ERR_PROXY_CONNECTION_FAILED.

    Asserted on the SOURCE rather than by launching a browser, because the
    branch only runs when a navigation fails and the suite must pass with no
    engine library installed at all.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        # Anchored on the FUNCTION that carries the decision, not on the
        # first `except _TransportError` in the file — which is the
        # client-version fallback in `_prime_session` and comes earlier.
        # The first version of this check read that block in all three
        # engines and failed on code that was correct. Reading the wrong
        # block is how a check like this passes, or fails, for the wrong
        # reason (CLAUDE.md §22).
        anchor = "def _fetch_with_policy("
        if anchor not in source:
            check("%s has a give-up branch to check" % module, False)
            continue
        start = source.index(anchor)
        end = source.find("\ndef ", start + 1)
        branch = source[start:end if end > 0 else len(source)]
        check("%s handles a transport failure at all" % module,
              "except _TransportError as exc:" in branch)
        check("%s names the proxy when the proxy was the fault" % module,
              "exit_failed = _proxy_failure(exc)" in branch
              and "if exit_failed:" in branch,
              "the failure branch does not distinguish a dead exit")
        check("%s ROTATES on a dead exit rather than retrying it" % module,
              "pool.advance(exit_failed)" in branch,
              "a retry through the same dead exit is repetition, not a "
              "second attempt")
        check("%s rebuilds the browser when it rotates" % module,
              "_open_session(pw, args, pool)" in branch,
              "a rotation is a fresh browser, never a proxy swapped under "
              "a live session")
        check("%s still has a plain message for a non-proxy failure" % module,
              "failed after %d attempt(s)" in branch,
              "the non-proxy branch was lost")
        check("%s masks the exit it names" % module,
              "mask(session.proxy_url)" in branch,
              "host and port are the point of the log; the password is not")
    # ...and the detector the branch depends on must actually match the
    # string Chromium produces. Measured on a SIBLING repo 2026-09-17
    # against a dead
    # local port: `net::ERR_PROXY_CONNECTION_FAILED`.
    engine = _import_engine("playwright_scraper")
    if engine is None:
        skip("proxy-failure", "playwright_scraper not importable here")
    else:
        class _E(Exception):
            pass
        got = engine._proxy_failure(
            _E("Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://x/"))
        equal("the marker list matches what Chromium really raises", got,
              "ERR_PROXY_CONNECTION_FAILED")
        equal("...and a plain timeout is NOT read as a proxy failure",
              engine._proxy_failure(_E("Page.goto: Timeout 25000ms exceeded.")),
              "")

def check_engines_do_not_evaluate_a_string_in_the_browser():
    """§18: a site whose CSP omits `unsafe-eval` kills wait_for_function with
    an EvalError and takes the run down with exit 1, on the site's most
    obvious URL. Mercor has not been measured for that, and the cheap habit
    costs nothing on a site that would have allowed it."""
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor"):
            check("%s never CALLS %s" % (module, banned), banned not in called,
                  "poll through page_flow.wait_for_count instead")

def check_credentials_never_reach_a_log():
    """§8: an EXCEPTION MESSAGE is a log, and the masker must be GLOBAL.

    A Playwright connection error repeats the endpoint five times (the
    message plus a four-line call log), so a masker handling only the first
    occurrence prints the password four times and looks like it is working.
    """
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module,
              "supersecret" not in masked, masked)
        check("%s keeps the host and port, which are the useful half" % module,
              "h1:9222" in masked and "h2:9222" in masked, masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)

def check_sample_output_matches_the_schema():
    from output_writer import Comment as JobPosting
    expected = [f.name for f in fields(JobPosting)]
    json_path = os.path.join(HERE, "sample_output.json")
    csv_path = os.path.join(HERE, "sample_output.csv")
    if not os.path.exists(json_path):
        check("sample_output.json exists", False)
        return
    rows = json.load(open(json_path, encoding="utf-8"))
    check("sample_output.json holds rows", bool(rows))
    equal("sample_output.json keys match the schema, in order",
          list(rows[0].keys()), expected)
    check("sample_output.json is from a real run (youtube.com rows)",
          all(r["source"] == "youtube.com" for r in rows))
    check("...and carries no fabrication markers",
          not any("lorem" in (r.get("title") or "").lower() or
                  "example.com" in (r.get("url") or "").lower()
                  for r in rows))
    # The sample is cut from a real run and then ANONYMISED, which is the
    # same rule the fixtures follow and for the same reason: a comment is
    # a person's own words, and republishing them is a separate act from
    # the site showing them on its own page (CLAUDE.md §10). What stays
    # real is everything the site generates — the counts, the timestamps,
    # the flags, the video — which is what makes the sample worth reading.
    check("the sample's commenters are placeholders",
          all(str(r["author_name"]).startswith("@fixture_user_")
              for r in rows),
          "a real commenter reached sample_output.json")
    check("...and its comment ids are too",
          all(str(r["sku"]).startswith(("FIXTURE_CMT_", "REPLY_"))
              for r in rows))
    check("...while the video it came from is real",
          any("youtube.com/watch" in (r.get("url") or "") for r in rows))
    if os.path.exists(csv_path):
        header = next(csv.reader(open(csv_path, encoding="utf-8")))
        equal("sample_output.csv header matches the schema", header, expected)

def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines()
                  if not line.endswith(".pyc"))

def check_no_test_mutates_the_working_tree():
    """§10: one suite used its own file as a fake chromedriver and chmod'd it
    to 755, leaving a mode change in git status.

    Compares the tree against how it looked when the suite STARTED, not
    against a clean checkout — otherwise this is permanently red while
    anyone is editing, and a check that is always red teaches everyone to
    ignore checks.
    """
    if _TREE_BEFORE is None:
        skip("git status", "not a git repository")
        return
    after = _tree_state()
    changed = sorted(set(after) - set(_TREE_BEFORE))
    check("the suite itself changed nothing in the working tree",
          not changed, "%s" % changed)

def check_no_statement_is_unreachable():
    """A statement sitting after a return/raise/break/continue in the SAME
    block, which therefore can never run.

    Narrow on purpose: it makes no claim about reachability in general, only
    about a block whose control flow has already left. Measured across the
    eighteen repos of this family on 2026-09-16 it reported six problems and
    zero false positives.

    `check_undefined_names_in_every_module` cannot see this class at all, by
    design -- it pools every binding in the file rather than tracking scopes,
    so a name used inside dead code passes as long as anything else in the
    module binds it. What was hiding in that blind spot here, and in five
    sibling repos, byte for byte: a function whose `def` line had been lost,
    leaving its docstring and body absorbed into the end of the function
    above it. Present since this repo's first commit, invisible to import,
    `--help`, `compileall`, and every green run of this suite.
    """
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename),
                              encoding="utf-8").read())
        dead = []
        for node in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise,
                                         ast.Continue, ast.Break)):
                        dead.append(block[i + 1].lineno)
                        break
        check("%s: no statement the control flow can never reach" % filename,
              not dead, "first at line %d" % min(dead) if dead else "")

def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="mercor-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose

    global _TREE_BEFORE
    _TREE_BEFORE = _tree_state()

    for fn in CHECKS:
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()

    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0


# ---------------------------------------------------------------------------
# The two payload shapes
# ---------------------------------------------------------------------------


def check_a_comment_lives_in_the_mutations_not_the_renderer_tree():
    """The join this parser exists to make.

    `commentThreadRenderer` holds a KEY and nothing else — no text, no
    author, no like count. Those arrive in a flat side channel,
    `frameworkUpdates.entityBatchUpdate.mutations`. A parser that walks the
    renderers and never joins finds six empty shells and reports six
    comments with every column null.
    """
    payload = FIX["comments_top_p1"]
    threads = product_parser._walk(payload, "commentThreadRenderer")
    check("the fixture carries threads", len(threads) >= 6, str(len(threads)))
    blob = json.dumps(threads)
    for absent in ('"content"', '"displayName"', '"likeCountNotliked"'):
        check("renderer tree holds no %s" % absent, absent not in blob,
              "this fixture no longer reproduces the split the parser is "
              "built around")
    rows = comments("comments_top_p1")
    equal("rows parsed from the joined payload", len(rows), 6)
    check("every joined row has text", all(r.text for r in rows))


def check_entity_comment_values():
    """Values, not coverage. A column can be 100% populated and wrong."""
    rows = comments("comments_top_p1")
    first = rows[0]
    equal("pinned comment like_count", first.like_count, 318000)
    equal("pinned comment like_count_text", first.like_count_text, "318K")
    equal("pinned comment reply_count", first.reply_count, 962)
    equal("published_time_text", first.published_time_text, "1 year ago")
    equal("reply_level of a top-level comment", first.reply_level, 0)
    equal("the pinned comment is pinned", first.is_pinned, True)
    equal("the pinned comment is hearted", first.creator_hearted, True)
    equal("verified author", first.author_is_verified, True)
    equal("data_source", first.data_source, "innertube.entity")
    equal("source", first.source, "youtube.com")
    equal("video_id", first.video_id, VIDEO_ID)
    equal("the row's title is the VIDEO's title", first.title, VIDEO_TITLE)
    check("only one comment is pinned", not rows[1].is_pinned)
    for row in rows:
        if row.like_count is not None:
            check("a count always comes with the text beside it",
                  bool(row.like_count_text), row.sku)


def check_the_legacy_shape_parses_to_the_same_columns():
    """The MWEB client still serves `commentRenderer` today.

    So the fallback path is exercised against a real capture rather than
    assumed. CLAUDE.md §4 wants two paths; this is the second, and it is
    live-testable rather than speculative.
    """
    legacy = comments("comments_legacy_mweb")
    entity = comments("comments_top_p1")
    check("the legacy fixture parses", len(legacy) >= 4, str(len(legacy)))
    equal("legacy data_source", legacy[0].data_source, "innertube.legacy")
    equal("legacy like_count", legacy[0].like_count, 318000)
    equal("legacy like_count_text", legacy[0].like_count_text, "318K")
    equal("legacy reply_count", legacy[0].reply_count, 962)
    equal("legacy timestamp", legacy[0].published_time_text, "1 year ago")
    equal("legacy pinned badge", legacy[0].is_pinned, True)
    equal("legacy creator heart", legacy[0].creator_hearted, True)
    for field in ("like_count", "reply_count", "published_time_text",
                  "is_pinned", "creator_hearted", "reply_level", "source"):
        equal("legacy and entity agree on %s" % field,
              getattr(legacy[0], field), getattr(entity[0], field))

    both = dict(FIX["comments_top_p1"])
    both["contents"] = FIX["comments_legacy_mweb"].get("contents")
    rows = product_parser.parse_comments(both, video_id=VIDEO_ID,
                                         scraped_at=SCRAPED_AT,
                                         row_cls=Comment)
    equal("a payload carrying both shapes yields the entity one",
          len(rows), 6)
    check("the entity path wins when both are present",
          all(r.data_source == "innertube.entity" for r in rows))


def check_emoji_runs_index_in_utf16_code_units():
    """The trap that makes the obvious slice wrong.

    `attachmentRuns[].startIndex/length` are UTF-16 code units, the way
    JavaScript counts. Every emoji outside the BMP is two of them, so
    slicing the Python string with those numbers takes the wrong span —
    and silently: on a real fixture a run of `length=2` covering exactly
    one emoji slices as TWO emoji, and a later run of the same comment
    slices to the empty string because its start is already past the end.

    Written while this suite's own generator hit the same class of bug
    from the other side: Python's `ast` reports `col_offset` in BYTES, and
    a rewrite that treated it as characters corrupted every line after the
    first em dash.
    """
    text = "ab\U0001F62D\U0001F62Dcd"
    equal("one emoji is two UTF-16 code units",
          product_parser.utf16_span(text, 2, 2), "\U0001F62D")
    equal("the second emoji",
          product_parser.utf16_span(text, 4, 2), "\U0001F62D")
    equal("the text after them", product_parser.utf16_span(text, 6, 2), "cd")
    check("the naive Python slice disagrees, which is the point",
          text[2:4] != product_parser.utf16_span(text, 2, 2))

    payload = FIX["comments_top_p1"]
    runs = [r for entity in product_parser._entity_index(payload).values()
            for r in ((entity.get("properties") or {}).get("content") or {})
            .get("attachmentRuns", [])]
    check("the fixture carries at least one attachment run", bool(runs))
    for entity in product_parser._entity_index(payload).values():
        content = (entity.get("properties") or {}).get("content") or {}
        if not content.get("attachmentRuns"):
            continue
        body = content.get("content") or ""
        for run in content["attachmentRuns"]:
            label = ((run.get("element") or {}).get("properties") or {}) \
                .get("accessibilityProperties", {}).get("label")
            if not label:
                continue
            equal("run at %s covers its own emoji" % run["startIndex"],
                  product_parser.utf16_span(body, run["startIndex"],
                                            run["length"]), label)


def check_a_localised_page_refuses_to_guess():
    """`hl=ru` localises the numbers, and a guess would be worse than null.

    Measured: the same video under `hl=ru` returns the same comments with
    `publishedTime` "1 year ago" written in Russian and the like count as
    "318 thousand" in Russian. The TEXT of a comment is user-written and
    identical either way, so the rows are still worth having — but a
    parser that read the leading digits would report a comment with
    318,000 likes as having 318.
    """
    rows = comments("comments_ru")
    check("the Russian fixture still parses to rows", bool(rows))
    first = rows[0]
    check("the count is localised in this fixture",
          "тыс" in (first.like_count_text or ""),
          repr(first.like_count_text))
    equal("a localised abbreviation reads as null, not as its mantissa",
          first.like_count, None)
    check("the verbatim localised timestamp is kept",
          bool(first.published_time_text) and
          "назад" in first.published_time_text)
    equal("a non-English relative time is not dated",
          first.published_at_approx, None)
    equal("and carries no precision", first.published_at_precision, None)
    equal("an exact reply count is locale-independent", first.reply_count, 962)


def check_count_parsing():
    """The table, including the cases that must refuse."""
    cases = [
        ("318K", 318000), ("1.8B", 1800000000), ("2.4M", 2400000),
        ("1,818,222,351", 1818222351), ("962", 962), ("0", 0),
        ("4.54M subscribers", 4540000), ("164,459 views", 164459),
        ("19,384,917", 19384917), ("1 234", 1234), ("1 234", 1234),
        ("318 тыс.", None), ("318 Mio.", None),
        ("2,4 Mio", None), ("", None), (None, None),
        ("no digits here", None),
    ]
    for raw, want in cases:
        equal("parse_count(%r)" % raw, product_parser.parse_count(raw), want)


def check_relative_time_is_an_upper_bound_with_a_precision():
    """A derived date that says how precise it is, or nothing at all."""
    stamp, precision = product_parser.relative_time("1 year ago",
                                                    "2026-09-21T12:00:00Z")
    equal("precision of '1 year ago'", precision, "year")
    check("'1 year ago' lands in the previous year",
          bool(stamp) and stamp.startswith("2025-"), repr(stamp))
    stamp, precision = product_parser.relative_time("3 days ago (edited)",
                                                    "2026-09-21T12:00:00Z")
    equal("'3 days ago (edited)'", stamp, "2026-09-18T12:00:00Z")
    equal("and its precision", precision, "day")
    equal("an unrecognised wording produces nothing, not a guess",
          product_parser.relative_time("1 год "
                                       "назад",
                                       SCRAPED_AT), (None, None))
    equal("None in, None out",
          product_parser.relative_time(None, SCRAPED_AT), (None, None))
    equal("edited is a SUFFIX on this site",
          product_parser.is_edited("3 days ago (edited)"), True)
    equal("and absent when it is", product_parser.is_edited("3 days ago"),
          False)


def check_a_reply_carries_its_parent_in_its_id():
    """`<parent>.<reply>` — so a thread needs no bookkeeping across calls."""
    rows = comments("replies_p1")
    check("the replies fixture parses", bool(rows))
    for row in rows:
        equal("%s is a reply" % row.sku, row.reply_level, 1)
        check("%s has a parent_id" % row.sku, bool(row.parent_id))
        check("%s begins with its parent" % row.sku,
              row.sku.startswith((row.parent_id or "") + "."))
    equal("parent of a reply id", product_parser.parent_comment_id("ABC.DEF"),
          "ABC")
    equal("a top-level id has no parent",
          product_parser.parent_comment_id("ABC"), None)
    check("a top-level comment has no parent_id",
          all(r.parent_id is None for r in comments("comments_top_p1")))


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def check_the_sort_menu_gives_two_distinct_tokens():
    """Two orderings, two tokens, read from the site's own menu."""
    tokens = product_parser.sort_tokens(FIX["watch"])
    equal("sort names", sorted(tokens), ["newest", "top"])
    check("the two tokens differ", tokens["top"] != tokens["newest"])
    equal("the payload states which ordering it is in",
          product_parser.selected_sort(FIX["comments_top_p1"]), "top")


def check_the_next_page_token_is_not_a_reply_threads_token():
    """Every thread carries a token of its own; only one moves forward.

    A tree-wide search for a continuation returns a REPLY thread's token,
    and the run then walks sideways into one conversation instead of
    forward through the listing.
    """
    payload = FIX["comments_top_p1"]
    forward = product_parser.next_page_token(payload)
    check("page 1 offers a next page", bool(forward))
    replies = dict(product_parser.reply_tokens(payload))
    check("the fixture carries reply threads", bool(replies))
    check("the next-page token is not one of the reply tokens",
          forward not in replies.values(),
          "the run would walk into a thread instead of onward")
    known = {r.sku for r in comments("comments_top_p1")}
    for parent in replies:
        check("reply token belongs to a comment on this page",
              parent in known, parent)
    equal("the last page of replies offers nothing further",
          product_parser.next_page_token(FIX["replies_p1"]), None)


def check_search_paginates_from_its_own_section():
    """A search response's first page carries no `onResponseReceived*`.

    Its token sits inside the results section itself. A run that looked
    only in the envelope stopped after page one and reported
    `pagination_exhausted` — a complete-looking run holding a fifth of
    what was asked for.
    """
    payload = FIX["search"]
    check("this fixture still reproduces the envelope-less first page",
          not payload.get("onResponseReceivedEndpoints") and
          not payload.get("onResponseReceivedCommands"))
    check("search page 1 offers a next page",
          bool(product_parser.search_page_token(payload)))
    check("the site's own estimate comes through",
          bool(product_parser.search_total(payload)))


def check_only_video_mode_is_independently_addressable():
    """CLAUDE.md §18 — ask per route, not per site."""
    equal("a comment page is addressed by the previous page's token",
          page_flow.pagination_is_addressable(mode="comments"), False)
    equal("search likewise",
          page_flow.pagination_is_addressable(mode="search"), False)
    equal("a video has its own address",
          page_flow.pagination_is_addressable(mode="video"), True)
    equal("comments clamp to one worker",
          page_flow.concurrency_for_mode("comments", 8), 1)
    equal("search clamps to one worker",
          page_flow.concurrency_for_mode("search", 8), 1)
    equal("video does not clamp",
          page_flow.concurrency_for_mode("video", 8), 8)
    equal("one live connection per Scraping Browser profile",
          page_flow.concurrency_limit("ws://x"), 1)
    equal("no limit imposed otherwise",
          page_flow.concurrency_limit(None), None)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def check_url_parsing_accepts_every_form_the_site_hands_out():
    cases = [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/@RickAstleyYT", None),
        ("https://www.youtube.com/playlist?list=PL123", None),
        ("https://example.com/watch?v=dQw4w9WgXcQ", None),
        ("", None),
    ]
    for url, want in cases:
        equal("video_id_from_url(%r)" % url,
              product_parser.video_id_from_url(url), want)
    equal("canonical form", product_parser.canonical_video_url("dQw4w9WgXcQ"),
          "https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def check_unsupported_urls_are_refused_with_a_reason():
    """A refusal that is false sends the reader hunting for a typo."""
    ok, reason = product_parser.is_supported_url(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    check("a watch URL is supported", ok and not reason)

    ok, reason = product_parser.is_supported_url(
        "https://www.youtube.com/@RickAstleyYT")
    check("a channel URL is refused", not ok)
    check("a channel URL is not called a non-YouTube host",
          "not a YouTube host" not in reason, repr(reason))
    check("the refusal says why", "channel" in reason.lower(), repr(reason))

    ok, reason = product_parser.is_supported_url("https://example.com/x")
    check("a non-YouTube host is named as one",
          not ok and "not a YouTube host" in reason, repr(reason))


def check_the_permalink_drops_tracking_parameters():
    """The same comment must have the same URL under either ordering.

    The site appends its own tracking tail to the permalink on some
    payloads and not others, so a URL that kept it would differ between
    two runs of the same video and produce a diff line about nothing.
    """
    top = {r.sku: r.url for r in comments("comments_top_p1")}
    newest = {r.sku: r.url for r in comments("comments_newest_p1",
                                             sort="newest")}
    shared = set(top) & set(newest)
    check("the two orderings share at least one comment", bool(shared))
    for sku in shared:
        equal("%s has one URL under both orderings" % sku, top[sku],
              newest[sku])
    for url in top.values():
        check("no tracking parameter survived",
              "pp=" not in url and "&si=" not in url, url)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def check_page_states_on_real_captures():
    """Six answers, and three of them are HTTP 200 with a healthy payload."""
    equal("watch", product_parser.detect_page_state(FIX["watch"]), "content")
    equal("a comments continuation",
          product_parser.detect_page_state(FIX["comments_top_p1"]), "content")
    equal("a video whose comments are off",
          product_parser.detect_page_state(FIX["comments_off"]),
          "comments_disabled")
    equal("a video that is not there",
          product_parser.detect_page_state(FIX["video_unavailable"]),
          "video_unavailable")
    equal("a search response",
          product_parser.detect_page_state(FIX["search"],
                                           expect_comments=False), "content")
    equal("no payload at all", product_parser.detect_page_state(None), "error")
    equal("a 429 is a refusal",
          product_parser.detect_page_state({}, 429), "challenge")
    equal("a 500 is not", product_parser.detect_page_state({}, 500), "error")


def check_comments_turned_off_is_not_a_block():
    """It is a complete answer to the question, and costs exit 4 not 3."""
    state = product_parser.detect_page_state(FIX["comments_off"])
    equal("not blocked", page_flow.counts_as_blocked(state), False)
    equal("not retried", page_flow.should_retry(state), False)
    equal("and never buys a solve", page_flow.should_solve(state), False)
    reason = product_parser.disabled_reason(FIX["comments_off"])
    check("the site's own sentence comes through",
          bool(reason) and "turned off" in reason.lower(), repr(reason))
    row = product_parser.parse_video(FIX["comments_off"],
                                     scraped_at=SCRAPED_AT, row_cls=Video)
    equal("the video row says comments are off", row.comments_enabled, False)
    equal("and True where they are on",
          product_parser.parse_video(FIX["watch"], scraped_at=SCRAPED_AT,
                                     row_cls=Video).comments_enabled, True)


def check_no_marker_matches_a_page_youtube_serves():
    """CLAUDE.md §18 — count every candidate on a page you know is good.

    Doing that here killed most of an obvious marker list. The sharpest
    one was the site's own "comments are turned off" sentence, which is
    `disabledText` on the comment COMPOSER widget and ships on every
    served first page: a scraper using it as a marker would report every
    video that HAS comments as having them switched off. This check is
    that count, kept.
    """
    served = ["watch", "comments_top_p1", "comments_top_p2",
              "comments_newest_p1", "replies_p1", "comments_ru",
              "comments_legacy_mweb", "search"]
    for name in served:
        blob = json.dumps(FIX[name], ensure_ascii=False)
        equal("no marker fires on %s, a page the site served" % name,
              product_parser.detect_bot_challenge(blob), None)
        state = product_parser.detect_page_state(
            FIX[name], expect_comments=(name != "search"))
        check("%s is not classified as blocked" % name,
              not page_flow.counts_as_blocked(state), state)

    composer = sum(json.dumps(FIX[n], ensure_ascii=False)
                   .count("Comments are turned off")
                   for n in ("comments_top_p1", "comments_newest_p1"))
    check("the served fixtures still carry the composer's disabledText",
          composer >= 1,
          "this check has stopped proving the trap is real")
    markers = " ".join(product_parser.BOT_CHALLENGE_MARKERS)
    for measured_on_a_good_page in ("Comments are turned off", "cf-turnstile",
                                    "consent.youtube.com", "botguard",
                                    "recaptcha"):
        check("%r is not a marker" % measured_on_a_good_page,
              measured_on_a_good_page not in markers,
              "it was counted on a GOOD page of this site")

    equal("a 429 is named", product_parser.detect_bot_challenge("", 429),
          "http 429")
    check("the documented sign-in wording is named",
          bool(product_parser.detect_bot_challenge(
              "Sign in to confirm you're not a bot")))


def check_the_player_payload_is_not_a_classifier():
    """`playabilityStatus` says UNPLAYABLE on a video that plays.

    The WEB client cannot obtain a playback stream without a
    proof-of-origin token, and `/player` serves the metadata anyway. A
    scraper that read that field as "this video is gone" would report
    every video as gone — measured on this repo's own fixture video, which
    is public and playing.
    """
    player = FIX["player"]
    equal("the fixture still reproduces the trap",
          (player.get("playabilityStatus") or {}).get("status"), "UNPLAYABLE")
    row = product_parser.parse_video(FIX["watch"], scraped_at=SCRAPED_AT,
                                     row_cls=Video)
    row = product_parser.apply_player(player, row)
    check("the row survives an UNPLAYABLE player payload", row is not None)
    equal("and gains the exact upload date from it", row.published_at,
          "2009-10-24T23:57:33-07:00")
    equal("and the duration in seconds", row.duration_seconds, 213)
    equal("and the site's own category", row.category, "Music")
    check("apply_player says in its own text why it ignores that field",
          "playabilityStatus" in inspect.getsource(product_parser.apply_player))


def check_the_video_row_carries_what_a_comment_row_cannot():
    """The asymmetry that makes `--mode video` worth having.

    A comment has no exact like count and no absolute date under any
    client. A video has both, and the numbers are read from the places
    that state them in full.
    """
    row = product_parser.parse_video(FIX["watch"], scraped_at=SCRAPED_AT,
                                     row_cls=Video)
    equal("sku is the video id", row.sku, VIDEO_ID)
    equal("title", row.title, VIDEO_TITLE)
    equal("channel", row.channel_name, "Rick Astley")
    equal("channel id", row.channel_id, "UCuAXFkgsw1L7xaCfnd5JJOw")
    equal("the EXACT view count, not the abbreviated one", row.view_count,
          1818222351)
    equal("and what was written", row.view_count_text, "1,818,222,351 views")
    equal("the exact like count, from the button's a11y label",
          row.like_count, 19384917)
    equal("the rendered date", row.published_date_text, "Oct 24, 2009")
    equal("the panel's abbreviated comment total", row.comment_count, 2400000)
    equal("and its text", row.comment_count_text, "2.4M")
    equal("provenance", row.data_source, "innertube.watch")

    equal("the a11y label yields the number, not every digit in it",
          product_parser._exact_from_a11y(
              "like this video along with 19,384,917 other people"), 19384917)
    equal("a label holding two numbers yields the FIRST, not both joined",
          product_parser._exact_from_a11y(
              "4.4 out of 5 stars, 279,961 ratings"), 4)

    equal("the exact comment total, from the section header",
          product_parser.total_comment_count(FIX["comments_top_p1"]), 2457619)
    equal("a later page states no total and must not invent one",
          product_parser.total_comment_count(FIX["comments_top_p2"]), None)


def check_search_rows_are_videos_and_nothing_else():
    rows = product_parser.parse_search(FIX["search"], query="web scraping",
                                       scraped_at=SCRAPED_AT, row_cls=Video)
    check("the search fixture parses", bool(rows))
    for row in rows:
        check("row %r carries a video id" % row.sku,
              bool(row.video_id) and len(row.video_id) == 11,
              "a channel or shelf leaked in")
        equal("sku is the video id", row.sku, row.video_id)
        equal("the query is recorded on the row", row.query, "web scraping")
        equal("provenance", row.data_source, "innertube.search")
    equal("no duplicate videos", len({r.sku for r in rows}), len(rows))


def check_the_sample_share_says_how_small_a_run_is():
    """CLAUDE.md §21 — complete and exhaustive are different words."""
    equal("60 comments of 2.4 million",
          page_flow.sample_share(60, 2457619), 0.0024)
    equal("nothing collected", page_flow.sample_share(0, 100), 0.0)
    equal("no total to divide by", page_flow.sample_share(10, None), None)
    equal("and no division by zero", page_flow.sample_share(10, 0), None)


def check_the_scraping_browsers_own_extension_does_not_read_as_a_challenge():
    """The marker check, run against a page fetched the way a paid run
    fetches — which is the only place this trap can appear.

    CLAUDE.md §21 records a guard that passed for the WRONG REASON: it ran
    only against captures taken with a plain HTTP client, which carry no
    extension injection at all. Every other fixture in this repo is such a
    capture. This one is a page YouTube SERVED, pulled over the 2Captcha
    Scraping Browser, and its auto-solve extension injected sixteen script
    tags into it.

    Counted on it 2026-09-21 — and every line is a marker some repo in
    this family has carried at some point:

        chrome-extension://          16
        hunter.js                     4
        cf-turnstile                  1     <- the trap
        cf-turnstile-response         1
        data-ts-input                 1
        challenges.cloudflare.com     0

    So `cf-turnstile` would report a blocked run on every good page over
    `--cdp-endpoint`, exactly as it did in a sibling repo on a 1.8 MB page
    holding a full catalogue. This repo does not carry it, and this check
    is what keeps that true rather than accidental.
    """
    html = FIX.get("cdp_served_watch")
    check("the CDP-served fixture is present", bool(html),
          "run make_fixtures.py with a capture taken over --cdp-endpoint")
    if not html:
        return

    # First: the fixture really is the one that can catch this. A check
    # whose input lost the thing it tests for passes silently.
    equal("the fixture still carries the extension's turnstile hunter",
          html.count("cf-turnstile"), 1)
    check("...and its injected script tags",
          html.count("chrome-extension://") >= 10,
          str(html.count("chrome-extension://")))

    # Then: nothing in our set fires on it.
    equal("no marker fires on a page served over the Scraping Browser",
          product_parser.detect_bot_challenge(html), None)
    equal("...and it classifies as served, not blocked",
          page_flow.counts_as_blocked(
              product_parser.detect_page_state(html)), False)

    # And the specific strings, named, so re-adding one fails HERE with
    # the reason rather than on someone's bill.
    markers = " ".join(product_parser.BOT_CHALLENGE_MARKERS)
    for injected in ("cf-turnstile", "hunter.js", "data-ts-input",
                     "chrome-extension://"):
        check("%r is injected by the extension and is not a marker"
              % injected,
              injected not in markers,
              "it appears on pages YouTube serves normally")


def check_a_fingerprint_is_applied_whole_or_not_at_all():
    """CLAUDE.md §24: a HALF identity is measured worse than none.

    Four defects lived on this path, every one of them a SILENT success —
    the call was accepted, the log said nothing, and the page disagreed
    with the fingerprint. All four were found by reading the values back
    out of a live page on 2026-09-21, which is what §24 tells you to do
    and which no amount of reading this code would have produced:

      1. `navigator.languages` reported `["en-US"]` against the
         fingerprint's `["en-US", "en"]`, because Playwright's `locale=`
         sets the PRIMARY language only.
      2. `navigator.userAgentData.brands` reported `HeadlessChrome/153`
         while the user agent claimed `Chrome/150` — the client hints are
         the half a `user_agent=` option leaves behind.
      3. Detaching the CDP session REVERTED the override, and the protocol
         reported success either way.
      4. In pyppeteer the init script never ran: `evaluateOnNewDocument`
         wraps its argument as a function expression, and
         `Page.addScriptToEvaluateOnNewDocument` silently does nothing
         until `Page.enable` has been sent — it answers
         `{"identifier": "1"}` regardless.

    These are asserted on the SOURCE and on the pure functions, because
    the branch needs a live browser and a paid key, and the suite must
    pass with neither.
    """
    import fingerprint_client as F

    # A fingerprint shaped like the ones the live API returns.
    fp = {
        "userAgent": {
            "value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/150.0.0.0 Safari/537.36",
            "brandVersionList": [{"brand": "Not;A=Brand", "version": "8"},
                                 {"brand": "Chromium", "version": "150"},
                                 {"brand": "Google Chrome", "version": "150"}],
            "brandFullVersionList": [{"brand": "Chromium",
                                      "version": "150.0.0.0"}],
            "platform": "Windows", "platformVersion": "19.0.0",
            "architecture": "x86", "bitness": "64", "model": "",
            "mobile": False, "fullVersion": "150.0.0.0",
        },
        "navigator": {"platform": "Win32", "hardwareConcurrency": 32,
                      "deviceMemory": 32},
        "intl": {"languages": ["en-US", "en"], "contentLocale": "en-US",
                 "timeZone": "America/New_York"},
        "screen": {"width": 2560, "height": 1440, "deviceScaleFactor": 1.5},
        "webgl": {"vendor": "Google Inc.", "renderer": "ANGLE (NVIDIA)"},
    }

    metadata = F.user_agent_metadata(fp)
    check("a complete fingerprint yields client hints", bool(metadata))
    equal("the brands come from the fingerprint, not the browser",
          [b["brand"] for b in metadata["brands"]],
          ["Not;A=Brand", "Chromium", "Google Chrome"])
    equal("platform", metadata["platform"], "Windows")
    equal("platformVersion", metadata["platformVersion"], "19.0.0")
    equal("bitness", metadata["bitness"], "64")
    equal("mobile is a real bool", metadata["mobile"], False)

    # The refusal half: no brand list means no metadata, so the caller
    # leaves the hints alone rather than applying a fragment of one.
    equal("an incomplete fingerprint yields NO metadata",
          F.user_agent_metadata({"userAgent": {"platform": "Windows"}}), None)
    equal("...and neither does an empty one",
          F.user_agent_metadata({}), None)

    # Accept-Language carries no q-values, and the reason is written down.
    equal("Accept-Language is built without q-values",
          F.accept_language(fp), "en-US,en")
    check("...and why is recorded beside it",
          "q-value" in inspect.getdoc(F.accept_language),
          "Chromium derives navigator.languages from this string and keeps "
          "the qualifier, which no real browser reports")
    equal("no languages, no header", F.accept_language({}), None)

    # The init script carries the language list, which `locale=` cannot.
    script = F.playwright_init_script(fp)
    check("the init script carries navigator.languages",
          "'languages'" in script and "en-US" in script)
    check("...and the platform and WebGL strings with it",
          "'platform'" in script and "37446" in script)

    # And every engine applies the two TOGETHER.
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        check("%s applies the client hints beside the user agent" % module,
              '"userAgent": user_agent, "userAgentMetadata": metadata'
              in source,
              "a bare override is the thing §24 measured being refused")
        check("%s refuses to apply a partial identity" % module,
              "HALF identity is worse than" in source,
              "no brand list must mean no override at all")
        check("%s never detaches the session that carries it" % module,
              ".detach()" not in source,
              "detaching reverts the override, and the call succeeds anyway")
        # The timezone reaches the browser by a DIFFERENT route in each
        # engine, and that is legitimate rather than drift: Playwright
        # takes `timezone_id` as a context option, which is what
        # `playwright_context_kwargs` sets; the other two have no such
        # option and send `Emulation.setTimezoneOverride`. Naming the
        # route per engine is what keeps a missing one visible — all three
        # were verified against a live page reporting America/New_York on
        # 2026-09-21.
        route = ("playwright_context_kwargs" if module == "playwright_scraper"
                 else "Emulation.setTimezoneOverride")
        check("%s applies the fingerprint's timezone (via %s)"
              % (module, route), route in source,
              "a browser reporting UTC under a New York fingerprint "
              "contradicts itself on an axis any script reads")

    puppeteer = os.path.join(HERE, "puppeteer_scraper.py")
    if os.path.exists(puppeteer):
        source = open(puppeteer, encoding="utf-8").read()
        check("pyppeteer enables the Page domain before adding the script",
              'send("Page.enable"' in source,
              "without it the protocol answers success and runs nothing")
        check("...and does not use the wrapper that mangles the source",
              "evaluateOnNewDocument(\n" not in source
              and "page.evaluateOnNewDocument(" not in source,
              "that wrapper emits `(<source>)()` and Chromium drops the "
              "syntax error in silence")
        check("...and installs it through the raw protocol command instead",
              "Page.addScriptToEvaluateOnNewDocument" in source)


def check_the_credential_scan_covers_the_files_it_most_needs_to():
    """The repo's own guard was blind to its biggest files, twice over.

    Both were found by PLANTING a real-shaped key and running the scan
    rather than by reading it (CLAUDE.md §23), and both reported
    "nothing credential-shaped" over a file that held one:

      1. `.json` and `.csv` were not in `SCANNED_SUFFIXES` at all, so
         `fixtures_generated.json` — 500-odd KB of captured page payload,
         which is precisely where a front-end key or a session token
         arrives — was never opened.
      2. With the suffixes added it STILL passed, because the allowlists
         were applied per LINE and that fixture is a single line. It
         contains "sha" 69 times and "hash" 36 times, so one allowlisted
         token anywhere in it exempted every match in the whole file. A
         line-scoped allowlist becomes a FILE-scoped one the moment a file
         is one line.

    Added with no new allowlist entries, which is the point: the real
    fixtures and sample carry zero 32-hex strings and zero credentialled
    URLs, so the strictest rule now covers the largest files instead of
    acquiring an exception a real key could hide behind (CLAUDE.md §24).
    """
    sys.path.insert(0, os.path.join(HERE, ".github"))
    import ci_checks

    for suffix in (".json", ".csv"):
        check("the scan opens %s files" % suffix,
              suffix in ci_checks.SCANNED_SUFFIXES,
              "the generated fixtures and the committed sample are these")

    scanned = {str(p.relative_to(ci_checks.REPO))
               for p in ci_checks.scanned_files()}
    for name in ("fixtures_generated.json", "sample_output.json",
                 "sample_output.csv"):
        check("the scan reaches %s" % name, name in scanned,
              "it is committed, and it is captured payload")

    # The window, and that it is narrower than a one-line fixture.
    check("the allowlist is scoped to a window, not to a line",
          hasattr(ci_checks, "_allowed_near"),
          "a per-line allowlist exempts a whole one-line file")
    equal("a token beside the match still excuses it",
          ci_checks._allowed_near("md5 " + "a" * 32, 4, 36,
                                  ci_checks.HEX32_ALLOWED, lower=True), True)
    far = "md5" + " " * 400 + "b" * 32
    equal("a token 400 characters away does not",
          ci_checks._allowed_near(far, len(far) - 32, len(far),
                                  ci_checks.HEX32_ALLOWED, lower=True), False)
    check("the window is narrower than the fixture is long",
          ci_checks.ALLOWLIST_WINDOW * 2 <
          len(json.dumps(FIX, ensure_ascii=False)),
          "otherwise the fixture is one window and nothing is scoped")

    # And the escaped-quote half: a fixture stored as JSON escapes every
    # quote inside it, so a pattern with plain quotes matches nothing.
    #
    # The sample is ASSEMBLED from pieces rather than written out, and that
    # is not fussiness — the scan now reads this file, and a literal
    # credentialled URL here would make the check fail on its own test
    # data. CLAUDE.md §22: a note about a banned string is a use of it, and
    # assembling is what lets the scan cover the suite instead of
    # exempting the one file most likely to acquire a pasted secret.
    sample = "ws:" + "//" + "acct7" + ":" + "s3cr3tpw" + "@" + "host:9222"
    check("the credentialled-URL pattern tolerates an escaped quote",
          ci_checks.CREDENTIALLED_URL.search('{"e": \\"%s\\"}' % sample)
          is not None)
    check("...and the bare form too",
          ci_checks.CREDENTIALLED_URL.search('{"e": "%s"}' % sample)
          is not None)
    # A documented placeholder must still be allowed, or the check becomes
    # one people switch off.
    placeholder = "ws:" + "//" + "user" + ":" + "pass" + "@" + "host:9222"
    check("a documented placeholder is still allowed",
          ci_checks._allowed_near(placeholder, 0, len(placeholder),
                                  ci_checks.CREDENTIAL_ALLOWED))


CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")
          and callable(v) and k != "check"]

if __name__ == "__main__":
    sys.exit(main())
