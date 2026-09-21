# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) as
closely as a CLI toolkit can. A **patch** release means fixes; it does not
mean every flag is frozen. Where a patch changes a default that costs money
or changes what a column means, the entry leads with that in a blockquote
rather than leaving it to be discovered from a bill or a chart.

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

[0.1.0]: https://github.com/2scraper/youtube-scraper/releases/tag/v0.1.0
