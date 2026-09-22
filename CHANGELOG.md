# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) as
closely as a CLI toolkit can. A **patch** release means fixes; it does not
mean every flag is frozen. Where a patch changes a default that costs money
or changes what a column means, the entry leads with that in a blockquote
rather than leaving it to be discovered from a bill or a chart.

## [0.2.0] — 2026-09-22

Written after a third-party audit. Four of its findings were correctness
defects that reported SUCCESS, which is this codebase's most expensive bug
class — no test failed, no run crashed, and the output looked right.

> **If you branch on `status` or on the exit code, read this.** A run that
> lost reply threads, or whose `--mode video` second call failed, used to
> report `status: complete` and exit `0`. It now reports `partial` and
> exit `6`. Pipelines that treated `complete` as "everything arrived" were
> being told something untrue; pipelines that treat `6` as a hard error
> will now see runs they previously saw as clean.

### Fixed

- **A run with failed pages called itself complete.** `finish_run` decided
  completeness from `stop_reason` alone, and a named list of reasons
  cannot cover a failure recorded anywhere else. Reproduced directly:
  `stop_reason="page_cap_reached", pages_failed=[3, 7]` returned exit 0
  and `status: complete` with both failures listed in the same sidecar.
  Completeness now consults the evidence as well as the reason. Measured
  across the family by CALLING each repo's `finish_run` rather than
  grepping: 28 of 32 behaved this way.
- **Lost reply threads were invisible.** A failed reply fetch put a thread
  INDEX into `pages_failed`, a field holding top-level page NUMBERS — so
  `[3, 7]` could mean either and nothing said which. Replies are now
  accounted for separately: `reply_threads_requested`, `…_completed`,
  `…_failed`, and a `reply_failures` list naming each thread by its parent
  comment id, depth and state. Any failure makes the run partial;
  `pagination_stop_reason` keeps the loop's own reason beside it, because
  "we reached the page cap" and "three threads failed" are two facts.
- **A half-built video row reported success.** When `/player` did not
  answer, `--mode video` logged a warning and returned a successful
  outcome with `published_at`, `duration_seconds`, `category` and
  `keywords` all null — indistinguishable from a video that genuinely has
  none. The row now records which sources built it (`innertube.watch`
  against `innertube.watch+player`), the sidecar names the missing
  columns, and the run is partial.
- **`--proxy-rotate per-page` did not rotate per page.** `pool.advance()`
  was reached only from a dead exit or a refusal, so a run whose pages all
  succeeded stayed on one address for its entire life. It now takes a new
  exit between pages, in all three modes — and exactly N-1 times for N
  pages, not N: the first version rotated after the last page too and
  built a browser for a request that never came.
- **The Scraper API client wrote no sidecar**, while the README promised
  one beside every run that wrote output. It now calls `finish_run` like
  the engines, and records `player_fields_unreachable` — those columns are
  missing by ROUTE there (the service issues a GET; `/player` is a POST),
  which is a different fact from the engines' failure and should not read
  as one.
- **Output files are written atomically.** A kill or a full disk during a
  write used to leave a truncated file where a complete one had been, with
  a sidecar beside it still describing the old run. Writes now go to a
  temporary file in the same directory, are flushed and fsynced, and are
  renamed over the destination — so a reader sees the whole previous file
  or the whole new one.
- **The cross-engine surface check never ran.** It compared engines that
  IMPORTED, and a supported virtualenv holds exactly one (§6 says install
  one), so it compared one engine against nothing and reported itself
  passed — 0 pairs, suite green. The audit found real divergence only by
  installing all three, a configuration the README tells people not to
  create. The comparison now reads the source, so it runs everywhere
  including with no engine installed, and it immediately found the drift:
  one method named `_apply_client_hints` in one engine and
  `_apply_fingerprint` in the others.

### Added

- **`--transport auto|http|browser`**, defaulting to `auto`. The endpoint
  this repo reads answers plain HTTPS, which the README has said since
  0.1.0 while every run started Chromium anyway. Measured end to end, two
  pages, median of three: **2.0 s over HTTP against 3.4 s through a
  browser**, identical rows. `auto` starts a browser the first time a
  response classifies as a challenge and stays on it for the rest of the
  run. A browser is no longer needed to install or to use.
- Fault-injection checks for all four correctness fixes above, each
  driving the engine with the transport stubbed out, and each verified by
  planting the fault back and watching the suite go red.

### Notes

- One `--transport http` run in nine returned exit 1 during testing, once,
  and did not reproduce in eight further attempts. Recorded rather than
  explained away: it is either a transient network fault or something not
  yet understood.

## [0.1.0] — 2026-09-21

First release. Three modes, three browser engines, and a Scraper API
client, reading YouTube through the endpoint its own front end calls.

### Added

- **`--mode comments`** — a video's comment threads and, with `--replies`,
  their replies. 20 top-level comments per page, 10 replies per page.
  `--sort top|newest` selects between the site's two orderings.
- **`--mode video`** — one video's metadata, from two calls: `next` for
  the rendered figures and `player` for the exact upload date, the
  duration in seconds, the category and the keyword list. The only mode
  that can use `--concurrency`, because a video has its own address and a
  comment page does not.
- **`--mode search`** — a query to videos, to feed `--mode comments`.
- `playwright_scraper.py`, `puppeteer_scraper.py` and
  `selenium_scraper.py`: one file with three driver layers, so the shared
  half cannot drift. Verified live on 2026-09-21 — the same video and
  ordering through all three gave 40 rows each, 40 ids in common and zero
  disagreeing columns.
- `scraper_api_client.py` for `--mode video` only. It cannot read comments
  on this site, and the measurement is in its docstring and the README.
- `diff_runs.py`, which refuses to compare two runs whose ordering differs
  — on this site the ordering decides *which* comments a capped run holds,
  not merely their order.
- An offline suite of 639 checks that passes with no engine library
  installed, and `make_fixtures.py`, which proves every trimmed fixture
  parses identically to its untrimmed original before writing it.

### Measured, and worth knowing before choosing this

- **The comment endpoint is not gated.** 2026-09-21, from a bare
  datacentre address in Finland with no key, no proxy, no cookies and no
  account: 60 consecutive pages, 1,200 comments, zero refusals, ~0.25 s
  per page.
- **YouTube publishes no exact like count for a comment, and no absolute
  timestamp**, to any client — checked under WEB, MWEB and TVHTML5 over 70
  comments in four captures. So `like_count` is a magnitude beside
  `like_count_text`, and `published_at_approx` is derived, labelled with
  `published_at_precision`, and null in any locale this parser does not
  read. A video row has both exactly, which is why `--mode video` exists.
- **The site serves two payload shapes.** The WEB client returns the
  entity form, where the comment text lives in
  `frameworkUpdates.entityBatchUpdate.mutations` and the renderer tree
  holds only keys; the MWEB client still returns the legacy
  `commentRenderer` form. Both are parsed, both are pinned to a real
  capture, and `data_source` says which was read.
- **The Scraper API cannot return comments here**, because YouTube renders
  none into its HTML and fetches them over a POST the service does not
  make. A `waitFor` on the comment section times out (HTTP 408); a
  `networkidle` wait returns 1.39 MB with zero comments. Control: a
  deliberately wrong key answered 401 in 0.1 s against the real key's 200
  in 6.7 s.
- **The Scraping Browser path is live-verified**, on Playwright and
  pyppeteer (Selenium cannot reach an authenticated CDP endpoint and
  refuses it by name). A US profile returned comments, video metadata and
  search normally on 2026-09-21. Its WebSocket upgrade answered `HTTP 500`
  on one of three attempts seconds apart, so both engines now retry it —
  and its auto-solve extension injects `cf-turnstile` and fifteen other
  captcha markers into pages the site served normally, which is why none
  of them is in this repo's block-marker set and why a fixture cut from
  such a page is now in the suite.
- **No challenge has ever been rendered to this scraper**, and the served
  page's own greps mislead: `recaptcha` appears once, in a CSS rule that
  hides a badge, and `botguard` thirteen times, all of them configuration
  flags. The site's actual defence is its own attestation (`bgChallenge`),
  which is not a solvable widget. The captcha machinery is carried as
  readiness and says so.

### Notes on what is not verbatim

`fixtures_generated.json`, `sample_output.json` and `sample_output.csv` are
cut from real runs and then anonymised: a commenter's display name,
channel id, avatar, comment id and own words are replaced with
placeholders. Everything the site generates around them — counts,
timestamps, badges, the video and its publisher — is untouched. A comment
is a person's writing, and republishing it is a separate act from YouTube
showing it on its own page.

[0.2.0]: https://github.com/2scraper/youtube-scraper/releases/tag/v0.2.0
[0.1.0]: https://github.com/2scraper/youtube-scraper/releases/tag/v0.1.0
