#!/usr/bin/env python3
"""youtube_scraper.py — the CLI that needs no browser.

This is the entry point the README's quick start uses, and it exists
because that quick start did not work. The repo told a reader to install
`requirements.txt` — no engine, by design, since the endpoint this reads
answers plain HTTPS — and then to run `playwright_scraper.py`, which
imports Playwright at module level and therefore died with
`ModuleNotFoundError` on their first command. Found by the 2026-09-25
audit; CLAUDE.md §23 says to run your own README verbatim in a fresh
clone, and nobody had.

The module-level import in the engines is CORRECT and stays (§10): the
offline suite's skip and CI's `python -c "import playwright_scraper"`
both depend on it. What was wrong is that a browser engine was also the
only CLI. The run itself is in `run_core.py`; this file binds it to a
driver that starts nothing.

    youtube_scraper.py --url dQw4w9WgXcQ --pages 3          # no browser
    youtube_scraper.py --url dQw4w9WgXcQ --transport browser --engine selenium

`--transport browser`, `--cdp-endpoint`, and an `auto` run that meets a
refusal all need a real engine. That is imported HERE, lazily, and only
then — so a reader who installed one gets it, and a reader who did not
gets a sentence naming the install command instead of an ImportError
raised from inside a run.
"""

import sys

import run_core
from run_core import logger, scrape, parse_args  # noqa: F401

ENGINES = ("playwright", "selenium", "puppeteer")


def load_driver(engine: str):
    """Import an engine module and hand back its driver.

    Deferred on purpose, and this is the ONE place in the repo where a
    driver import is deferred: everywhere else it is at module level
    because a check depends on it failing loudly.
    """
    if engine not in ENGINES:
        raise SystemExit("unknown --engine %r; pick one of %s"
                         % (engine, ", ".join(ENGINES)))
    try:
        module = __import__("%s_scraper" % engine)
    except ImportError as exc:
        raise SystemExit(
            "--engine %s needs its library, which is not installed (%s).\n"
            "    pip install -r requirements-%s.txt\n"
            "%s"
            "Or drop the flag: the default --transport auto reads YouTube "
            "over plain HTTPS and opens no browser."
            % (engine, exc, engine,
               "    playwright install chromium\n"
               if engine == "playwright" else ""))
    return module._DRIVER


def main(argv=None) -> int:
    args = parse_args(argv)
    must_have_browser = (args.transport == "browser" or bool(args.cdp_endpoint))
    if args.transport == "http":
        # The caller said never start one, so do not even look for it.
        driver = run_core.NoDriver()
    elif must_have_browser:
        driver = load_driver(args.engine)          # raises with the command
    else:
        # `auto`: read over HTTPS and keep a real engine in hand in case
        # the site refuses. The engine's runtime is started on FIRST USE,
        # so holding it costs nothing on a run that never opens a browser.
        try:
            driver = load_driver(args.engine)
        except SystemExit as exc:
            logger.info("No browser engine installed, so a refusal cannot "
                        "be escalated to one. Reading over HTTPS, which is "
                        "what this site answers. (%s)",
                        str(exc).splitlines()[0])
            driver = run_core.NoDriver()
    return run_core.main(driver, argv)


if __name__ == "__main__":
    sys.exit(main())
