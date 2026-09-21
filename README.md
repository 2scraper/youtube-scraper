# youtube-scraper

[![tests](https://github.com/2scraper/youtube-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/youtube-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/youtube-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/youtube-scraper/actions/workflows/canary.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.13-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)](#engines)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-success)](#do-you-need-any-of-this)

YouTube comment threads and replies, video metadata, and video search —
read from the endpoint YouTube's own front end calls.

```bash
git clone https://github.com/2scraper/youtube-scraper.git
cd youtube-scraper
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
./.venv/bin/playwright install chromium
./.venv/bin/python playwright_scraper.py --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --pages 3
```

```
Page 1: 20 comments (20 total so far)
Page 2: 20 comments (40 total so far)
Page 3: 20 comments (60 total so far)
Collected 60 of the video's 2,457,616 comments (0.0024%). A run that fetched
every page it asked for is COMPLETE; it is not exhaustive.
[+] Saved 60 rows -> youtube_comments.json
```

---

## Do you need any of this?

Probably not, and saying so is more useful than a pitch.

**Measured 2026-09-21** from a bare datacentre address in Finland, with no
API key, no proxy, no cookies and no account: **60 consecutive pages,
1,200 comments, zero refusals, about 0.25 s per page.** YouTube publishes
its comments through `POST /youtubei/v1/next`, and that endpoint is not
gated. The 1.4 MB watch page is needed for nothing.

So this repo's paid integrations — captcha solving, proxies, fingerprints,
the Scraping Browser API — are **readiness rather than routine here**. What
they buy, if you need it: volume from many addresses, a specific exit
country, and somewhere to run a browser that is not your machine.

**And before you use this at all, consider the official API.** The YouTube
Data API's `commentThreads.list` costs 1 quota unit per call and returns up
to 100 comments; the default allocation is 10,000 units a day, so roughly a
million comments daily, free, with a Google account. It also gives two
things this scraper **cannot** give you, because YouTube does not publish
them to the web client at all — see the next section.

Use this scraper when you want no account and no quota ceiling, when you
want to control which of the site's two orderings you sample, or when you
want the fields the official API does not expose (pinned, creator-hearted,
verified, the sort a row came from).

---

## What this cannot tell you

Two limits, both properties of the site rather than of this code, both
measured over 70 comments in four captures under three different InnerTube
clients:

| | comment row | video row |
|---|---|---|
| like count | **approximate** — the site publishes `"318K"` and nothing better, under any client | **exact** — `19,384,917`, read from the like button's own accessibility label |
| timestamp | **relative only** — `"1 year ago"`. There is no ISO date in the payload | **exact** — `2009-10-24T23:57:33-07:00` |

So a `Comment` row carries `like_count` **and** `like_count_text`, and
`published_at_approx` **and** `published_time_text` **and**
`published_at_precision`. The number is a magnitude; the text is what the
site actually said; the precision says how much the derived date is worth.
A row precise only to the year cannot answer "was this posted after March",
and the column says so rather than letting you find out from a chart.

`published_at_approx` is the **latest** instant the comment could have been
written, because YouTube floors: `"1 year ago"` is an age in [1, 2) years.

If you need exact like counts or real timestamps, use the official API.
That sentence is in this README because the alternative is you discovering
it from a dashboard.

---

## Modes

```bash
# comments (default) — a video's threads, newest first, with replies
python3 playwright_scraper.py --url dQw4w9WgXcQ --pages 5 --sort newest --replies

# video — one video's metadata, including the exact upload date
python3 playwright_scraper.py --url dQw4w9WgXcQ --mode video

# search — a query to videos, to feed --mode comments
python3 playwright_scraper.py --url "web scraping tutorial" --mode search --pages 3
```

**A page is a continuation, not a screen.** In `--mode comments` one page
is 20 top-level comments; a reply page is 10. `--pages 3` is 60 comments.

**`--replies` costs a request per thread that has any** — about 20 more
requests per page of comments. A one-page run with `--replies` returned 192
rows from 20 threads on 2026-09-21.

**The two orderings are two different samples**, not the same rows
reordered. `top` is YouTube's relevance ranking (and is what the site
selects); `newest` is chronological. Which one a row came from is the
`sort` column, and `diff_runs.py` refuses to compare runs that disagree on
it.

**`--mode video` makes two calls per video** — `next` for the rendered
figures and `player` for the exact upload date, duration, category and
keywords. It is the only mode where `--concurrency` above 1 does anything:
a video has its own address, while a comment page is addressed by a token
the *previous* page handed out, so a comments run is strictly sequential
and says so rather than silently ignoring the flag.

---

## Output

One row per comment, in the order the site sent them. The first five
columns — `source`, `scraped_at`, `url`, `sku`, `title` — are identical
across every scraper in this family; `sku` is the comment id and `title` is
the **video's** title, repeated so a CSV of ten thousand comments reads
without a join.

```json
{
  "source": "youtube.com",
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=Ugz…",
  "sku": "Ugz…",
  "title": "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
  "text": "…",
  "author_name": "@someone",
  "author_is_verified": true,
  "like_count": 318000,
  "like_count_text": "318K",
  "reply_count": 962,
  "published_time_text": "1 year ago",
  "published_at_approx": "2025-09-21T02:10:14Z",
  "published_at_precision": "year",
  "edited": false,
  "reply_level": 0,
  "parent_id": null,
  "is_pinned": true,
  "pinned_by": "Pinned by @RickAstleyYT",
  "creator_hearted": true,
  "sort": "top",
  "data_source": "innertube.entity"
}
```

`sample_output.json` and `sample_output.csv` are cut from a real run. **The
commenters in them are placeholders** — names, channel ids, avatars and the
comment text are rewritten, because a comment is a person's own words and
republishing them is a separate act from YouTube showing them on its own
page. Everything the site generates is untouched: the counts, the
timestamps, the flags, the video.

**A reply's id contains its parent's**, separated by a dot, so threading
needs no bookkeeping: `parent_id` is derived from `sku`.

**Exit codes**: `0` ok · `1` crash · `2` bad usage · `3` blocked · `4` no
comments · `5` remote API error · `6` partial. Every run writes
`<out>.meta.json` beside its output; a failed run writes none, so a
`"failed"` sidecar can never sit next to good data.

**A run that finds nothing writes nothing** — last night's good output is
never replaced with `[]`. Pass `--allow-empty` when an empty result is the
answer you want.

---

## Traps that look like bugs

* **"Comments are turned off" is exit 4, not exit 3.** It is a complete
  answer to the question you asked, so no proxy will help. Same for a video
  that has been deleted or made private. Both answer HTTP 200 with a large,
  healthy-looking payload.
* **`total_comments` in the sidecar can be approximate.** The exact figure
  is in the comment section's own header, which the site sends only on the
  first page of its DEFAULT ordering. A `--sort newest` run falls back to
  the watch page's abbreviated panel total (`"2.4M"` → 2400000) and says so
  by the roundness of the number.
* **A long run can see the same comment twice.** `--sort top` is a live
  ranking, and a comment that gains likes between page 3 and page 30 can be
  served again. The run logs the drop rather than absorbing it silently.
  This is also why a long run is a sample, not a snapshot.
* **`--locale` other than `en` empties two columns on purpose.** A Russian
  payload writes the timestamp and the abbreviated like count in Russian;
  this parser returns `null` for both rather than reading `318` out of
  "318 thousand". The comment TEXT is unaffected — it is user-written and
  identical in every locale.
* **Emoji are in the text, but the payload's run offsets are not Python
  offsets.** They are UTF-16 code units. This matters only if you read
  `attachmentRuns` yourself; `product_parser.utf16_span` is the correct
  reader.

---

## Engines

| | comments | video | search | notes |
|---|---|---|---|---|
| `playwright_scraper.py` | yes | yes | yes | primary |
| `puppeteer_scraper.py` | yes | yes | yes | pyppeteer is effectively unmaintained |
| `selenium_scraper.py` | yes | yes | yes | cannot use `--cdp-endpoint`, see below |
| `scraper_api_client.py` | **no** | yes | no | measured, see below |

The three browser engines are one file with three driver layers; everything
above the transport is the same text, so they cannot drift on exit codes or
run status. Verified live on 2026-09-21: the same video and ordering
through all three gave **40 rows each, 40 ids in common, and zero
disagreeing columns**.

**Install exactly one engine, in its own virtualenv.** Playwright and
pyppeteer pin incompatible `pyee` versions and pyppeteer and Selenium
collide on `urllib3`; `pip install` will "resolve" that by quietly
downgrading something you wanted.

```bash
python3 -m venv .venv-selenium
./.venv-selenium/bin/pip install -r requirements.txt -r requirements-selenium.txt
```

**Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
`connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
`ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
put a password. The engine refuses `--cdp-endpoint` with that reason
instead of failing later. Selenium also cannot authenticate a `--proxy` at
all: the credentials are stripped and a warning is printed.

**The Scraper API path cannot read comments on this site, and that is a
fact about YouTube.** The service renders a page and returns its HTML;
YouTube renders no comments into its HTML and fetches them over a POST the
service does not make. Measured 2026-09-21 against `/watch?v=dQw4w9WgXcQ`:

| request | result |
|---|---|
| no `waitFor` | HTTP 200, 1,354,420 bytes, **0 comments** |
| `waitFor {"state":"networkidle"}` | HTTP 200, 1,386,808 bytes, **0 comments** |
| `waitFor {"element":"ytd-comment-thread-renderer"}` | **HTTP 408** — it never appears |
| `waitFor {"text":"Top comments"}` | **HTTP 408** |

A deliberately wrong key answered HTTP 401 in 0.1 s against the real key's
200 in 6.7 s, so those 200s are real work rather than a cached refusal.
What the client does do is `--mode video`, at $0.0005 per task, which is
what the watch document is genuinely good for.

---

## Configuration

Credentials go in `.env` beside the scripts, never on a command line — a
secret in `argv` is readable by anything that can run `ps` and lands in
shell history. Copy `.env.example`, and check what was picked up without
printing any of it:

```bash
cp .env.example .env
python3 env_config.py
```

Precedence, highest first: an explicit flag → an exported environment
variable → `.env` → the default.

`TWOCAPTCHA_KEY` is one key for four separately billed 2Captcha products:
captcha solving, the Scraping Browser API, proxies and fingerprints.

---

## Comparing two runs

```bash
python3 diff_runs.py yesterday.json today.json
```

It refuses to compare runs whose `mode`, `source` or `sort` differ, and
refuses a pair that is not both `complete` — a partial run's un-fetched
pages would otherwise read as comments that were deleted.

---

## Development

```bash
python3 smoke_test.py          # the offline suite; no engine library needed
python3 smoke_test.py -v       # print every check as it passes
pytest                          # the same checks, wrapped as one test
python3 make_fixtures.py       # regenerate fixtures from your own captures
```

`make_fixtures.py` proves every trimmed fixture parses identically to its
untrimmed original — every column of every row, matched by id — before
writing anything, and refuses to write if any scrubbed value survived.

See `CONTRIBUTING.md`, and `TROUBLESHOOTING.md` for what a given exit code
means and what to do about it.

---

## Legal

This tool reads publicly visible pages. Comments are written by real
people: think about what you are collecting, keep it lawful where you are,
and read YouTube's Terms of Service. Nothing here circumvents
authentication, and the endpoint it uses is the one the site's own front
end calls.

MIT licensed. Not affiliated with, endorsed by, or connected to YouTube or
Google.
