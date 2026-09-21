# Troubleshooting

Every exit code, what it actually means on this site, and what to do.

Start here:

```bash
python3 env_config.py     # what configuration was picked up (no secrets printed)
python3 smoke_test.py     # the offline suite; needs no engine and no network
```

---

## Exit codes

| code | meaning | on this site |
|---|---|---|
| `0` | ok | rows were written |
| `1` | crash | a bug, or a served page that parsed to nothing |
| `2` | bad usage | a URL with no video id, or two flags that contradict |
| `3` | blocked | **never observed here.** See below — this would be news |
| `4` | no comments | usually a real answer, not a fault |
| `5` | remote API error | the Scraping Browser or the Scraper API failed |
| `6` | partial | some pages arrived, then the run stopped |

---

## `exit 4` — "0 rows"

Three different things produce it, and only one is a problem.

**Comments are turned off.** The log says so by name, quoting the site's
own sentence. This is a complete answer: no proxy, key or engine will
change it. `--mode video` on the same id returns a row with
`comments_enabled: false`.

**The video is unavailable** — private, deleted, or never there. Also a
real answer. Note that it answers HTTP 200 with a 60 KB payload, so
"nothing came back" is not the right mental model.

**A served page parsed to nothing.** This one IS a problem, and it is
reported separately: the log says the page was served, gives its size, and
tells you to re-run with `--dump-html`. That combination means the payload
changed shape and this parser needs updating — it is never "the video has
no comments". The dumped JSON is the evidence.

---

## `exit 3` — blocked

This has not happened. Measured 2026-09-21 from a bare datacentre address
with no key and no proxy: 60 consecutive pages, no refusal of any kind.

So if you see exit 3, it is new, and the saved payload is the evidence for
what changed. Before assuming your address is the problem:

- check whether the response is a **429** — that is the clearest statement
  of a rate limit this site can make, and the fix is `--delay`, not a
  proxy;
- read the dumped payload for the documented sign-in interstitial. That is
  an interstitial demanding an **account**, and no captcha solver clears
  it; a different exit is the only remedy;
- try one request by hand. If a plain `curl` of the watch page still
  returns 200, the problem is in this repo rather than at the site.

Adding a residential proxy (`--proxy`) or the Scraping Browser
(`--cdp-endpoint`) is the usual next move, and the README says plainly
that neither has been needed so far.

**What NOT to reach for first: a hand-written `--user-agent`.** There is no
such flag here, and that is deliberate. A claimed user agent with the real
browser's TLS handshake and client hints underneath it is a contradiction,
and on at least one site in this family that contradiction is the whole
reason a request gets refused — served on the first navigation and denied on
the next three.

`--fingerprint` is the opposite case and is safe to try: it supplies a
COMPLETE identity — user agent, client hints, platform, timezone, language
list, screen and WebGL strings together — verified on 2026-09-21 by reading
all of them back out of a live page in each engine. Do not stack it on
`--cdp-endpoint`, which brings its own; the engines refuse that combination.

**And if a solve is refused over `--cdp-endpoint`**: the token is minted
from your machine and installed into a browser that may be on another
continent. The run warns about it. The endpoint's own auto-solve runs where
the browser is and does not have that problem.

---

## `exit 5` — remote API error

**`profile_locked`** from `--cdp-endpoint`: a Scraping Browser profile
allows ONE live connection, and another run still holds this `pid`. Use a
different `pid`, or wait.

**A 401 from the endpoint**: a profile's credentials last about a day.
There is no working endpoint pasted anywhere in this repo for exactly that
reason — get a fresh one rather than reusing the shape.

**`--cdp-endpoint` on the Selenium engine** is refused outright, with the
reason: chromedriver's `debuggerAddress` takes a bare `host:port` and
cannot carry the endpoint's password. Use the Playwright or pyppeteer
engine for that path.

---

## Things that look like bugs and are not

**"I asked for 10 pages and got 200 comments."** A page here is a
continuation, not a screen: 20 top-level comments, or 10 replies. The
closing log line says how small the run is against the video's own total,
which on a popular video is a very small number, and that is honest rather
than alarming.

**The sidecar's `total_comments` is suspiciously round.** Under
`--sort newest` the exact figure is not sent, so the run falls back to the
watch page's abbreviated panel total — `"2.4M"` becomes 2400000. The
roundness is the tell. Run `--sort top` if you need the exact number.

**Two runs a minute apart disagree on timestamps.** They must: the site
renders relative times, so "25 minutes ago" becomes "27 minutes ago" on
its own. Measured directly — two engines 88 seconds apart disagreed on 3
of 40 rows, on that column only. `diff_runs.py` deliberately does not
track it.

**A comment appeared twice in a long run.** `--sort top` is a live
ranking; a comment that gains likes between page 3 and page 30 can be
served again. The run logs the drop rather than hiding it.

**`like_count` did not move even though the comment got likes.** The site
publishes `"318K"`. A comment going from 318,400 to 318,600 real likes
reports no change at all. Read `like_count_text` before believing a delta.

**Every count and date is null on a `--locale ru` run.** On purpose: a
localised payload writes those in Russian, and this parser returns null
rather than reading `318` out of "318 thousand". The comment TEXT is
unaffected. Use the default `--locale en` unless you specifically want the
localised strings.

**`--concurrency 8` says it clamped to 1.** In `--mode comments` and
`--mode search` each page is addressed by a token the *previous* page
handed out, so a second worker would have no address to fetch.
`--mode video` parallelises across videos.

---

## Installing

**`pip check` complains, or an engine stops importing.** Playwright and
pyppeteer pin incompatible `pyee` versions, and pyppeteer and Selenium
collide on `urllib3`. Install exactly one engine per virtualenv:

```bash
python3 -m venv .venv-playwright
./.venv-playwright/bin/pip install -r requirements.txt -r requirements-playwright.txt
./.venv-playwright/bin/playwright install chromium
```

**The suite fails on `fixtures_generated.json` being missing.** It is
committed; if it is absent, regenerate it with `python3 make_fixtures.py`,
which needs your own captures in `../captures/youtube/` (see that file's
docstring — the captures are deliberately not in the repo).

**A `--dump-html` file is what to send with a bug report.** It is the
exact payload the parser was given, and it is written on success too —
a run can return the right count with a column silently unpopulated, and
then the bytes are the only way to tell a parsing bug from a payload
change.
