#!/usr/bin/env python3
"""Cut the offline suite's fixtures out of real captures, and PROVE they
parse the same.

Its output is `fixtures_generated.json`, which `smoke_test.py` loads. This
script is shipped because two files point at it — `smoke_test.py`'s own
docstring and TROUBLESHOOTING.md — and an instruction pointing at a file
that does not exist is worse than no instruction.

WHAT YOU NEED TO RUN IT
-----------------------
Your own captures, in `../captures/youtube/` relative to the repo, named as
`SOURCES` below expects. They are deliberately NOT in the repository: one
InnerTube page of this site is 200-500 KB.

Take them with `--dump-html` on any engine, which writes exactly the bytes
the parser was given. Take BOTH shapes: a `WEB` capture (the entity form)
and an `MWEB` one (the legacy `commentRenderer` form). They are the repo's
two read paths and only one of them is exercised by a default run.

WHAT IS NOT VERBATIM, and why
-----------------------------
Everything that is a PERSON is rewritten before anything is written to
disk. A comment capture is not like a product grid: it carries a real
individual's display name, their channel id and URL, their avatar, and
their own words. CLAUDE.md §10 is explicit that republishing those is a
separate act from the site showing them on its own page, and a sibling
repo in this family committed exactly that before anyone noticed.

    the commenter's display name       -> "@fixture_user_N"
    their channel id and channel URL   -> a fixed-shape placeholder
    their avatar URL                   -> a placeholder on the real host
    the comment's own text             -> filler of the SAME UTF-16 LAYOUT
    the comment id, and every entity
      key derived from it              -> "FIXTURE_CMT_N" / "FIXTURE_KEY_N"
    the continuation tokens            -> "FIXTURE_TOKEN_N"

The filler is not arbitrary. Emoji, whitespace and punctuation are kept
exactly where they were and only letters and digits are replaced, because
the payload's `attachmentRuns` index into that text in UTF-16 CODE UNITS
and the whole point of one of these fixtures is to pin that. A filler that
changed the layout would make the test pass against a text that cannot
occur.

Everything the SITE generates around a person is left untouched: the like
and reply counts, the relative timestamps, the verified and creator flags,
the pinned badge's shape, the sort menu, the video's own title, channel
and view count. A public video and its publisher are the site's catalogue,
not a private individual.

WHAT IT ENFORCES
----------------
  * every fixture is CUT from a real capture, never hand-written;
  * each one parses IDENTICALLY to the untrimmed original for the rows it
    keeps — every column that was not deliberately scrubbed, not just a
    count;
  * each one CLASSIFIES the same way, which is what catches a trim that
    dropped the structure `detect_page_state` reads and turned a good page
    into an `unknown` one.
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import product_parser as parser                            # noqa: E402
from output_writer import Comment, Video                   # noqa: E402

# name in the fixture file -> capture filename, and how many threads to keep
SOURCES = {
    "comments_top_p1": ("comments_top_p1_en.json", 6),
    "comments_top_p2": ("comments_top_p2_en.json", 4),
    "comments_newest_p1": ("comments_newest_p1_en.json", 4),
    "replies_p1": ("replies_p1_en.json", 4),
    "comments_legacy_mweb": ("comments_legacy_mweb_p1_en.json", 4),
    "comments_ru": ("comments_top_p1_ru.json", 3),
    "watch": ("next_video_en.json", 0),
    "player": ("player_en.json", 0),
    "comments_off": ("comments_off.json", 0),
    "video_unavailable": ("video_unavailable.json", 0),
    "search": ("search_en.json", 5),
}

# Columns this script deliberately rewrites. The parse-identity check below
# compares every OTHER column between the original and the fixture, so this
# list is the exact boundary of what is not verbatim — and shrinking it by
# accident makes the check stricter, never weaker.
SCRUBBED_COLUMNS = {"sku", "url", "text", "author_name", "author_channel_id",
                    "author_channel_url", "author_avatar_url", "parent_id",
                    "pinned_by", "mentions"}

# Columns that necessarily move when a page is cut down, and which say
# nothing about whether the parse is faithful.
POSITIONAL_COLUMNS = {"position"}

_CAPTURE_DIRS = ("../captures/youtube", "captures/youtube", "../captures")


def _find_captures() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    for candidate in _CAPTURE_DIRS:
        path = (here / candidate).resolve()
        if path.is_dir():
            return path
    raise SystemExit(
        "No captures directory found. Put your own captures in "
        "../captures/youtube/ (see this file's docstring) — they are not "
        "shipped with the repo.")


# ---------------------------------------------------------------------------
# Anonymising
# ---------------------------------------------------------------------------

# Nothing shorter than this is ever used as a substring rename.
_MIN_SWEEP_LEN = 8


class _Anonymiser:
    """A consistent rename table for one fixture.

    Consistent matters more than clever: the renderer tree points at the
    mutations by key, so renaming a key in one place and not the other
    produces a fixture whose comments cannot be joined — which would make
    the suite pass on a payload shape that never occurs.
    """

    def __init__(self):
        self.map = {}
        self.counts = {}

    def alias(self, value: str, kind: str) -> str:
        if not isinstance(value, str) or not value:
            return value
        if value not in self.map:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            n = self.counts[kind]
            self.map[value] = {
                "cmt": f"FIXTURE_CMT_{n}",
                "key": f"FIXTURE_KEY_{n}",
                "chan": f"UC{'F' * 20}{n:02d}",
                "name": f"@fixture_user_{n}",
                "token": f"FIXTURE_TOKEN_{n}",
            }[kind]
        return self.map[value]


# An entity key is a long base64url blob, usually percent-encoded. The
# field name alone is not enough to recognise one: `responseContext`
# carries tracking parameters shaped `{"key": "c", "value": "WEB"}`, and
# the first version of this script put "c" in the rename table. The final
# sweep then replaced every letter "c" in the document — including the one
# in `onResponseReceivedEndpoints` — and the fixture classified as
# `unknown`. The parse-identity check caught it, which is the argument for
# having one.
_ENTITY_KEY_RE = re.compile(r"^[A-Za-z0-9_%\-]{16,}=*$")


def _is_entity_key(value) -> bool:
    return (isinstance(value, str) and value not in ("N/A",)
            and bool(_ENTITY_KEY_RE.match(value)))


def _filler(text: str) -> str:
    """Replace the words, keep the layout.

    Letters and digits become a repeating filler; every space, newline,
    punctuation mark and emoji stays exactly where it was. So the string's
    UTF-16 length and every run offset into it survive, which is what the
    `attachmentRuns` fixture exists to pin.
    """
    if not isinstance(text, str):
        return text
    source = "fixturetextnotwrittenbyaperson"
    out, i = [], 0
    for ch in text:
        if ch.isalnum() and ch.isascii():
            out.append(source[i % len(source)])
            i += 1
        else:
            out.append(ch)
    return "".join(out)


def _avatar(_url) -> str:
    return ("https://yt3.ggpht.com/FIXTURE_AVATAR=s88-c-k-c0x00ffffff-no-rj")


# The subtrees that describe a PERSON. Everything inside one of these is
# somebody who left a comment; everything outside is the site's catalogue —
# a video, its publisher, its view count — and is left verbatim.
#
# That line is drawn structurally rather than by field name on purpose. A
# channel id looks identical whether it belongs to a commenter or to the
# channel that published the video, and scrubbing by shape renamed Rick
# Astley out of the `search` fixture, which made a real value assertion
# impossible to write. A publisher of a public video is not the private
# individual CLAUDE.md §10 is about.
_PERSON_SUBTREES = ("commentEntityPayload", "commentSurfaceEntityPayload",
                    "commentRenderer", "commentViewModel",
                    "commentThreadRenderer")


def _scrub(node, anon: _Anonymiser, video_id: str, in_person: bool = False):
    """Walk the payload and rewrite every person in it, in place.

    Two passes in one walk: entity KEYS and continuation TOKENS are
    renamed everywhere, because the renderer tree joins to the mutations
    through them and a half-renamed key produces a fixture whose comments
    cannot be joined at all. Everything else is renamed only inside a
    person's subtree.
    """
    if isinstance(node, list):
        for item in node:
            _scrub(item, anon, video_id, in_person)
        return
    if not isinstance(node, dict):
        return
    in_person = in_person or any(k in node for k in _PERSON_SUBTREES)

    for key in ("commentId",):
        if isinstance(node.get(key), str):
            raw = node[key]
            if "." in raw:
                parent, child = raw.split(".", 1)
                node[key] = (anon.alias(parent, "cmt") + "." +
                             anon.alias(raw, "cmt").replace("FIXTURE_CMT_",
                                                            "REPLY_"))
            else:
                node[key] = anon.alias(raw, "cmt")

    for key in ("key", "commentKey", "toolbarStateKey", "toolbarSurfaceKey",
                "commentSurfaceKey", "sharedKey", "sharedSurfaceKey",
                "translateButtonEntityKey", "composerDraftEntityKey",
                "inlineRepliesKey", "searchVideoResultEntityKey"):
        if _is_entity_key(node.get(key)):
            node[key] = anon.alias(node[key], "key")

    if isinstance(node.get("token"), str):
        node["token"] = anon.alias(node["token"], "token")

    if in_person:
        for key in ("channelId", "browseId", "externalChannelId"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith("UC"):
                node[key] = anon.alias(value, "chan")

        for key in ("displayName", "authorButtonA11y", "innerBadgeA11y",
                    "accessibilityText"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith("@"):
                node[key] = anon.alias(value.split(",")[0], "name")

        for key in ("avatarThumbnailUrl", "creatorThumbnailUrl"):
            if isinstance(node.get(key), str):
                node[key] = _avatar(node[key])

        if isinstance(node.get("url"), str) and node["url"].startswith("/@"):
            node["url"] = "/" + anon.alias(node["url"][1:], "name").lstrip("@")
        if isinstance(node.get("canonicalBaseUrl"), str) and \
                node["canonicalBaseUrl"].startswith("/@"):
            node["canonicalBaseUrl"] = "/" + anon.alias(
                node["canonicalBaseUrl"][1:], "name").lstrip("@")

        thumbs = node.get("authorThumbnail")
        if isinstance(thumbs, dict):
            for thumb in thumbs.get("thumbnails", []):
                if isinstance(thumb.get("url"), str):
                    thumb["url"] = _avatar(thumb["url"])
        sources = node.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and \
                        isinstance(source.get("url"), str) and \
                        "ggpht" in source["url"]:
                    source["url"] = _avatar(source["url"])

    # `/watch?v=…&lc=<comment id>` — the site's own permalink, which
    # carries the id that everything else here has just been renamed away
    # from.
    if isinstance(node.get("url"), str) and "&lc=" in node["url"]:
        head, _, tail = node["url"].partition("&lc=")
        node["url"] = head + "&lc=" + anon.alias(tail.split("&")[0], "cmt")

    content = node.get("content")
    if isinstance(content, dict) and isinstance(content.get("content"), str):
        content["content"] = _filler(content["content"])
    if isinstance(node.get("contentText"), dict):
        for run in node["contentText"].get("runs", []):
            if isinstance(run.get("text"), str):
                run["text"] = _filler(run["text"])
    if in_person:
        # A composed accessibility string carries the author's name AND
        # the whole comment text in one field — "Pinned by X. @Y. 1 year
        # ago. <the comment>". It is the field a walk over named keys
        # misses, and it leaked a real person's words into the first
        # version of these fixtures.
        #
        # Scoped to `accessibilityData`, NOT to every `label` in a
        # person's subtree. An emoji run's label is the emoji itself, and
        # blanking it broke the one check that proves those run offsets
        # are UTF-16 code units — a scrub that silently retires a check is
        # worse than one that misses a field, because the suite still
        # passes.
        inner = node.get("accessibilityData")
        if isinstance(inner, dict) and isinstance(inner.get("label"), str):
            inner["label"] = "FIXTURE_A11Y"

    if isinstance(node.get("authorText"), dict):
        for run in node["authorText"].get("runs", []):
            if isinstance(run.get("text"), str) and run["text"].startswith("@"):
                run["text"] = anon.alias(run["text"], "name")

    for value in node.values():
        _scrub(value, anon, video_id, in_person)


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------


def _trim(payload, keep: int):
    """Keep the first `keep` comment threads and only their mutations.

    A whole page is 200-500 KB, most of it the nineteen threads a test does
    not need. Trimming has to keep the renderer items and the mutations
    they point at IN STEP: drop a mutation whose view model survives and
    the fixture proves only that the parser skips unjoinable rows.
    """
    data = copy.deepcopy(payload)
    # The watch payload is 530 KB and four fifths of it is the
    # related-videos rail, which nothing here reads. Dropping it keeps the
    # comment section, the sort menu, the engagement panel and both info
    # renderers — and `_verify` proves the video row still parses the same.
    two_column = ((data.get("contents") or {})
                  .get("twoColumnWatchNextResults"))
    if isinstance(two_column, dict) and "secondaryResults" in two_column:
        two_column.pop("secondaryResults", None)
    # The comment section's header ships the site's entire EMOJI PICKER —
    # every emoji, every skin-tone label, every search keyword. It is the
    # single biggest thing in a first-page payload (158 KB against a second
    # page's 41 KB) and nothing here reads a byte of it. The header itself
    # stays, because `total_comment_count` is read out of it.
    for holder in parser._walk(data, "commentsHeaderRenderer"):
        if isinstance(holder, dict):
            holder.pop("customEmojis", None)
    # The picker itself, not the composer around it. The composer carries
    # `disabledText` — "Comments are turned off." — which ships on EVERY
    # served first page and is the sharpest marker trap this site has
    # (CLAUDE.md §18). The suite asserts that string is present here and
    # is not in the marker set, so dropping the composer would quietly
    # retire the check that proves the trap is real.
    for key in ("emojiPickerRenderer", "emojiPickerUpsellRenderer",
                "emojis", "categories"):
        _drop_everywhere(data, key)

    # Whole top-level sections the parser never opens. Listed rather than
    # guessed at: each was removed, `_verify` re-run, and kept only
    # because every column of every row still matched. That check is what
    # makes a list like this safe to grow.
    for key in ("topbar", "pageVisualEffects", "playerOverlays", "cards",
                "adBreakHeartbeatParams", "header"):
        data.pop(key, None)

    # Session material. `responseContext` carries `visitorData`, which is
    # the identifier the session that took this capture was using —
    # anonymous and long expired, and still not something to publish
    # (CLAUDE.md §10). Nothing here reads it.
    context = data.get("responseContext")
    if isinstance(context, dict):
        for key in ("visitorData", "serviceTrackingParams",
                    "mainAppWebResponseContext", "webResponseContextExtensionData",
                    "consistencyTokenJar"):
            context.pop(key, None)
    _drop_everywhere(data, "visitorData")
    for key in ("thumbnail", "richThumbnail", "channelThumbnailSupportedRenderers",
                "thumbnailOverlays", "menu", "expandableMetadata",
                "inlinePlaybackEndpoint", "avatar"):
        if keep:                       # only where the rows are the point
            _drop_everywhere(data, key)

    if not keep:
        return data

    kept_keys = set()
    for envelope in ("onResponseReceivedEndpoints", "onResponseReceivedCommands"):
        for endpoint in data.get(envelope, []) or []:
            for command_key in ("reloadContinuationItemsCommand",
                                "appendContinuationItemsAction"):
                command = endpoint.get(command_key)
                if not isinstance(command, dict):
                    continue
                items = command.get("continuationItems") or []
                threads = [i for i in items if "commentThreadRenderer" in i
                           or "commentViewModel" in i or "videoRenderer" in i]
                others = [i for i in items if i not in threads]
                kept_threads = threads[:keep]
                # One of the kept comments must carry an `attachmentRuns`
                # emoji run, because a whole check exists to pin that those
                # offsets are UTF-16 code units. Left to chance, a trim to
                # the first six threads produced a fixture with none, and
                # the check passed on an empty loop.
                if _has_emoji_run(data, kept_threads) is False:
                    for extra in threads[keep:]:
                        if _has_emoji_run(data, [extra]):
                            kept_threads = kept_threads[:-1] + [extra]
                            break
                command["continuationItems"] = kept_threads + others
                threads = kept_threads
                for item in kept_threads:
                    for view in parser._walk(item, "commentViewModel"):
                        if isinstance(view, dict):
                            for field in ("commentKey", "toolbarStateKey",
                                          "commentSurfaceKey", "sharedKey",
                                          "toolbarSurfaceKey",
                                          "sharedSurfaceKey"):
                                if view.get(field):
                                    kept_keys.add(view[field])

    # Legacy shape: the renderers are inline, so trim them where they sit.
    section = None
    for candidate in parser._walk(data, "itemSectionRenderer"):
        if isinstance(candidate, dict) and candidate.get("contents"):
            section = candidate
            break
    if section is not None:
        contents = section.get("contents") or []
        legacy = [c for c in contents if "commentThreadRenderer" in c]
        if legacy:
            section["contents"] = legacy[:keep] + \
                [c for c in contents if c not in legacy]

    if kept_keys:
        for batch_holder in parser._walk(data, "entityBatchUpdate"):
            if not isinstance(batch_holder, dict):
                continue
            batch_holder["mutations"] = [
                m for m in batch_holder.get("mutations") or []
                if not _mutation_key(m) or _mutation_key(m) in kept_keys]
    return data


def _sweep(fixture, anon: _Anonymiser):
    """Final pass: replace every renamed value wherever it still occurs.

    A walk over named keys cannot catch a value that has been COMPOSED
    into another string, and this payload does that in at least two
    places: `targetId` is literally
    `"comment-replies-item-" + <the comment id>`, and an accessibility
    label concatenates the author and the text. Both leaked in the first
    version of this script.

    So the walk renames, and this sweep enforces. It runs on the serialised
    fixture, which is the only representation where a substring is a
    substring.
    """
    text = json.dumps(fixture, ensure_ascii=False)
    for original, alias in sorted(anon.map.items(), key=lambda kv: -len(kv[0])):
        # Longest first, so a value that contains another is replaced
        # before its substring is. And never a short one: a rename table
        # is only safe to apply as a substring when the substring is
        # distinctive, which is why `_is_entity_key` exists above.
        if len(original) < _MIN_SWEEP_LEN:
            raise SystemExit(
                f"refusing to sweep {original!r}: too short to be a safe "
                f"substring. Narrow what goes into the rename table.")
        if original in text:
            text = text.replace(original, alias)
    return json.loads(text)


def _assert_clean(name, fixture, anon: _Anonymiser):
    """Prove no renamed value survived anywhere in the fixture.

    CLAUDE.md §10 asks for a guard so the NEXT capture is scrubbed too.
    This is that guard at generation time; `smoke_test.py` carries the
    pattern-based half, which catches a value this table never knew about.
    """
    text = json.dumps(fixture, ensure_ascii=False)
    leaked = sorted(o for o in anon.map if o and o in text)
    if leaked:
        raise SystemExit(
            f"{name}: {len(leaked)} scrubbed value(s) survived into the "
            f"fixture, e.g. {leaked[:3]!r}. Nothing is written.")


def _has_emoji_run(payload, items) -> bool:
    """Whether any of these threads' comments carries an emoji run."""
    keys = set()
    for item in items:
        for view in parser._walk(item, "commentViewModel"):
            if isinstance(view, dict) and view.get("commentKey"):
                keys.add(view["commentKey"])
    for entity in parser._entity_index(payload).values():
        if entity.get("key") not in keys:
            continue
        content = (entity.get("properties") or {}).get("content") or {}
        if content.get("attachmentRuns"):
            return True
    return False


def _drop_everywhere(node, key: str):
    """Remove `key` wherever it appears. Used only for subtrees nothing reads."""
    if isinstance(node, dict):
        node.pop(key, None)
        for value in node.values():
            _drop_everywhere(value, key)
    elif isinstance(node, list):
        for value in node:
            _drop_everywhere(value, key)


def _mutation_key(mutation):
    payload = (mutation or {}).get("payload") or {}
    for value in payload.values():
        if isinstance(value, dict) and value.get("key"):
            return value["key"]
    return None


# ---------------------------------------------------------------------------
# Proving the fixture parses the same
# ---------------------------------------------------------------------------


def _rows(payload, kind: str):
    if kind == "video":
        row = parser.parse_video(payload, video_id="dQw4w9WgXcQ",
                                 scraped_at="T", row_cls=Video)
        return [row] if row else []
    if kind == "search":
        return parser.parse_search(payload, query="q", scraped_at="T",
                                   row_cls=Video)
    return parser.parse_comments(payload, video_id="dQw4w9WgXcQ",
                                 video_title="T", sort="top", page=1,
                                 scraped_at="2026-01-01T00:00:00Z",
                                 row_cls=Comment)


def _verify(name, original, trimmed, kind):
    """Every kept row, every column, matched BY ID against the original.

    Run before anonymising, and matched on `sku` rather than on position:
    trimming may substitute one thread for another (see `_has_emoji_run`),
    and a positional comparison then reports every column of that row as
    changed. Matching by id says the real thing — this comment, as the
    site sent it, parses the same out of the cut-down payload.
    """
    state_before = parser.detect_page_state(original)
    state_after = parser.detect_page_state(trimmed)
    if state_before != state_after:
        raise SystemExit(f"{name}: trimming changed the classification, "
                         f"{state_before} -> {state_after}. The fixture no "
                         f"longer represents the page it came from.")

    before = {r.sku: r for r in _rows(original, kind)}
    after = _rows(trimmed, kind)
    if len(after) > len(before):
        raise SystemExit(f"{name}: fixture parsed MORE rows than the "
                         f"original ({len(after)} > {len(before)}).")
    for row in after:
        source = before.get(row.sku)
        if source is None:
            raise SystemExit(f"{name}: row {row.sku!r} is not in the "
                             f"original — the trim invented one.")
        for field in row.__dataclass_fields__:
            if field in POSITIONAL_COLUMNS:
                continue
            if getattr(row, field) != getattr(source, field):
                raise SystemExit(
                    f"{name}: {row.sku} column {field!r} changed during "
                    f"trimming: {getattr(source, field)!r} -> "
                    f"{getattr(row, field)!r}")
    return len(after)


def _verify_scrubbed(name, trimmed, fixture, kind):
    """The scrub must change only what it is allowed to change."""
    if parser.detect_page_state(trimmed) != parser.detect_page_state(fixture):
        raise SystemExit(f"{name}: anonymising changed the classification.")
    before, after = _rows(trimmed, kind), _rows(fixture, kind)
    if len(before) != len(after):
        raise SystemExit(f"{name}: anonymising changed the row count, "
                         f"{len(before)} -> {len(after)}.")
    for source, row in zip(before, after):
        for field in row.__dataclass_fields__:
            if field in SCRUBBED_COLUMNS or field in POSITIONAL_COLUMNS:
                continue
            if getattr(row, field) != getattr(source, field):
                raise SystemExit(
                    f"{name}: anonymising changed {field!r}, which is not "
                    f"in SCRUBBED_COLUMNS: {getattr(source, field)!r} -> "
                    f"{getattr(row, field)!r}")


def main() -> int:
    captures = _find_captures()
    out = {}
    for name, (filename, keep) in SOURCES.items():
        path = captures / filename
        if not path.is_file():
            print(f"  SKIP {name}: {path} not found")
            continue
        original = json.loads(path.read_text(encoding="utf-8"))
        trimmed = _trim(original, keep)
        kind = ("search" if name == "search" else
                "video" if name in ("watch", "comments_off") else "comments")
        # Faithfulness first, against the real values; anonymity second.
        rows = _verify(name, original, trimmed, kind)
        fixture = copy.deepcopy(trimmed)
        anon = _Anonymiser()
        _scrub(fixture, anon, "dQw4w9WgXcQ")
        fixture = _sweep(fixture, anon)
        _assert_clean(name, fixture, anon)
        _verify_scrubbed(name, trimmed, fixture, kind)
        out[name] = fixture
        print(f"  {name:22} {len(json.dumps(fixture)):8,} bytes, "
              f"{rows:3} row(s) verified identical")

    target = pathlib.Path(__file__).resolve().parent / "fixtures_generated.json"
    target.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {target} ({target.stat().st_size:,} bytes, "
          f"{len(out)} fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
