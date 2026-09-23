# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

YouTube changing its payload is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is WHICH
source broke — and on this site that is never a CSS selector, because the
parser does not read the DOM at all. It reads the JSON the site's own front
end fetches from `POST /youtubei/v1/next` (and `/youtubei/v1/player` for
`--mode video`).

There are **two** comment shapes here, and a break is usually in one of
them and not the other:

1. **The entity shape** (the `WEB` client). `commentThreadRenderer` holds
   only keys and ordering; the comment itself arrives in
   `frameworkUpdates.entityBatchUpdate.mutations` as a
   `commentEntityPayload`. A parser that stops joining the two finds empty
   shells — rows with every column null — rather than failing loudly.
2. **The legacy `commentRenderer` shape** (the `MWEB` client), which runs
   only when the entity path yields nothing. Each row records which of the
   two it came from in `data_source`.

A run that says the page was served and parsed to nothing is the payload
changing shape. Re-run with `--dump-html`; the dumped JSON is the evidence.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once.
   Unlike most siblings in this family the canary here is **not** gated on a
   secret: YouTube's comment endpoint is served to a bare GitHub runner, so
   it runs a real 3-page scrape daily and is expected to be GREEN. That is
   not a convenience — it is what keeps the README's central claim honest.
   If YouTube ever puts that endpoint behind a challenge or a key, the badge
   goes red the next morning and the claim is retested without anyone
   having to remember to.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Several properties in this repo exist because they were once absent or were
measured against expectation. Tests pin them, so a PR that breaks one will
fail rather than silently regress:

- **A marker that matches every page is worse than no marker.** The
  Scraping Browser API's auto-solve extension injects captcha markers into
  every page it loads — `cf-turnstile` among them — including pages that
  were served perfectly normally. This repo carries none of those as block
  markers, and a fixture cut from such a page is in the offline suite so it
  stays that way. Before adding any marker: count it on a page you know is
  good.

- **"Comments are turned off" and "video unavailable" are answers, not
  faults.** Both come back as HTTP 200 with a large, healthy-looking
  payload. They exit 4, are never retried and never counted as blocked.

- **Approximate stays approximate.** A comment's like count and timestamp
  are published only as `"318K"` and `"1 year ago"`, so a row carries the
  site's text beside the derived number and `published_at_precision` says
  how much the derived date is worth. Do not make them look exact.

- **The two orderings are two different samples.** `top` and `newest`
  decide WHICH comments a capped run holds, so `sort` is a column and
  `diff_runs.py` refuses to compare runs that disagree on it.

- **A comments run cannot be split across workers.** Each page's token
  comes from the page before it, so `--concurrency` above 1 only does
  anything in `--mode video`, and the run says so rather than silently
  ignoring the flag.

- **Never write that a captcha cannot be solved.** Write that this repo does
  not implement X. 2Captcha solves enterprise reCAPTCHA
  (`RecaptchaV2EnterpriseTaskProxyless`) and Cloudflare Turnstile
  (`TurnstileTaskProxyless`), and this repo builds both. A detection without
  a sitekey **refuses to build a task** rather than paying for one the API
  will reject.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including a query that genuinely
  matched nothing, which is a correct answer — `5` the content was never
  obtained, `6` partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A query
  that matched nothing was served exactly as asked.
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so concurrency cannot change
  the output.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha
classifier and the CLI contract against inline fixtures. If yours genuinely
needs youtube.com, say in the PR what you ran, which mode and URL, from
which exit, and what you got — including the sidecar's `total_comments`
and `comments_collected` and the coverage lines the run prints.

Things about running this live that are specific to YouTube:

* **You need nothing.** No key, no proxy, no account. The comment endpoint
  was served to a bare datacentre address on 2026-09-21: 60 consecutive
  pages, no refusal. If a run is refused, that is NEW, and the saved debug
  payload is the finding — say so in the issue rather than reaching for a
  proxy.
* **`--sort top` is a live ranking.** Two runs minutes apart can differ,
  and a long run can see the same comment twice. A small difference between
  two runs is the site, not a regression.
* **Be polite about `--replies`.** It costs a request per thread that has
  any. Use `--delay`, and do not run a large scrape to test a one-line
  change.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification. The bar is that all three engines return identical rows
for the same video and ordering.

Do not add anything that posts, likes, subscribes or signs in. This project
reads; it never acts on the site on anyone's behalf.

## Scope

This repo reads **public data** on youtube.com: a video's comment threads
and replies, its metadata, and search results, exactly as an anonymous
visitor is served them.

Out of scope: anything behind a login, anything that writes to the site,
and anything that defeats a protection rather than passing it the way an
ordinary browser does. Comments are written by real people; a feature whose
point is profiling individual commenters is a product decision about
personal data rather than a bug fix.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.

## The `§` references in the comments

Comments and docstrings here cite section numbers — "CLAUDE.md §18", "§24".
That file is the 2scraper family's internal conventions document and is
deliberately **not** in this repository: it applies to every scraper in the
family and is not specific to this one.

Nothing is lost by not having it. The rule a reference points at is always
written out in full beside the reference, because a comment whose reasoning
lives somewhere else is not a comment. Read the `§` as provenance — "this
paragraph exists because it cost another repo real time" — and the sentence
around it as the whole of the argument.

If you find a reference whose reasoning is NOT spelled out beside it, that
is a bug in the comment; please open an issue.
