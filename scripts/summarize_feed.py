#!/usr/bin/env python3
"""Summarise pending archive items, then re-apply current rules to stored ones.

Pending = no truthy `summary`. Offline work (feed copies, subtitles) runs first
and is not metered; page fetches are capped by --max-items. Pass 3 (backfill)
re-validates thumbnails, converts 簡體, and translates non-Chinese summaries.

    summarize_feed.py                       # normal run (env: ITEMS_FILE, MAX_ITEMS...)
    summarize_feed.py --offline-only          # feed copies + local subtitles only, no fetches (after download_sub)
    summarize_feed.py --backfill-only [--dry-run] [--limit N] [--no-translate]
    summarize_feed.py --mine-boilerplate N  # candidate drop_unit rules from corpus
"""

from __future__ import annotations

import argparse
import html as html_mod
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import extract
import lang
import subtitle_priority
import textproc
import thumbs
from common import (BLANK_SUMMARY, FALLBACK_MARK, GONE_SUMMARY, host_in, host_of,
                    is_pending, is_youtube, load_doc, save_doc, valid_id)

SUBTITLES_DIR = Path(os.environ.get("SUBTITLES_DIR", "data/subtitles"))
SLEEP = 1.5                   # polite delay after each network item
BATCH, MAX_FAIL_STREAK = 25, 4
SKIP_HOSTS = ("news.google.com",)      # feed copy is redirect debris, page is useless
PAUSE_DOMAINS = ("douban.com",)        # same skeleton page on every subdomain
TECHMEME_FETCH = ("Source", "Report", "Documents:")
# Summarised from the feed copy without fetching the page, when one exists.
FEED_FIRST_HOSTS = tuple("""
abei.club aftermath.site ageofinvention.xyz artincontext.org attlin.com beartalking.com
bituzi.com blocktempo.com blogspot.com buttondown.com caffes.me careher.net cashchou.com
chaidarun.com cityofsound.com cocktail4party.com coolshell.cn curtismchale.ca davidoks.blog
devtang.com esence.travel first-cafe.com firstround.com fomosoc.com fs.blog gilifedesigner.com
honest-broker.com huli.tw hunterwalk.com joestudwell.com kopu.chat limboy.me
lipperalpha.refinitiv.com lostmagazine.org louie.lu lutaonan.com matters.town maxjamesread.com
medium.com meiguinfo.com mickzh.com noswag.tw notesbylex.com personaljournal.ca polgeonow.com
pseudoyu.com readtrung.com ruanyifeng.com samaltman.com shenlvmeng.github.com shiuncorner.com
sirupsen.com sive.rs smallbooks.com.tw soidid.tw starrocket.io steveblank.com substack.com
techcabal.com tiaodao.typlog.io travelwithbook.com trensse.com
unchartedterritories.tomaspueyo.com uselessetymology.com vox.com waitbutwhy.com werner.wiki
whogovernstw.org yuanyu.idv.tw zmonster.me bestblogs.dev
""".split())


def _ts(value) -> float:
    s = str(value or "").strip()
    for parse in (lambda: datetime.fromisoformat(s.replace("Z", "+00:00")),
                  lambda: parsedate_to_datetime(s)):
        try:
            return parse().timestamp()
        except Exception:
            pass
    return float("-inf")


def pick_subtitle(item_id: str) -> Path | None:
    """Best local .vtt for an item: {id}.{orig}.{sub}.vtt, by file_rank."""
    best = None
    for p in SUBTITLES_DIR.glob(f"{item_id}.*.vtt") if valid_id(item_id) else ():
        parts = p.name[len(item_id) + 1:].split(".")
        if len(parts) == 3:
            key = (subtitle_priority.file_rank(parts[1], parts[0]), p.name)
            best = min(best or (key, p), (key, p))
    return best[1] if best else None


class Run:
    def __init__(self, path, doc, max_items: int, deadline: float | None):
        self.path, self.doc, self.max_items, self.deadline = path, doc, max_items, deadline
        self.n = Counter()
        self.paused: dict[str, int] = {}          # host -> items skipped since

    # ---- bookkeeping ----
    def touch(self):
        save_doc(self.path, self.doc)          # every change reaches disk at once

    def done(self, it, summary, how):
        it["summary"] = summary
        it.pop("feed_content", None)
        self.n["ok"] += 1
        print(f"    ok ({how})")
        self.touch()

    def thumb(self, it, html):
        if not it.get("thumbnail") and html and (t := thumbs.extract(html, it["url"])):
            it["thumbnail"] = t
            self.touch()

    def title_fallback(self, it) -> bool:
        tr = textproc.translate(it.get("title") or "")
        if tr:
            self.done(it, lang.to_twp(tr) + " " + FALLBACK_MARK, "translated title")
        return bool(tr)

    # ---- routing ----
    @staticmethod
    def route(it) -> str | None:
        url = it["url"]
        if host_in(url, SKIP_HOSTS):
            return None
        if is_youtube(url):
            return "youtube"
        if host_in(url, FEED_FIRST_HOSTS) and (it.get("feed_content") or "").strip():
            return "feed"
        if host_in(url, PAUSE_DOMAINS) and (it.get("title") or "").strip().startswith(("想读", "想看", "想听")):
            return "blank"
        return "techmeme" if "techmeme.com" in url else "fetch"

    def process(self, pending: list, offline_only=False):
        routed = [(r, it) for it in pending if (r := self.route(it))]
        offline = [x for x in routed if x[0] in ("feed", "youtube", "blank")]
        online = [] if offline_only else [x for x in routed if x[0] not in ("feed", "youtube", "blank")]
        print(f"pending={len(pending)}: offline={len(offline)} network={len(online)} "
              f"(fetch cap {self.max_items})")
        for r, it in offline + online:
            if self.deadline and time.monotonic() > self.deadline:
                self.n["time_cut"] = 1
                print("Time budget reached; the rest stay pending.")
                break
            metered = r == "fetch" or (r == "techmeme" and it.get("title", "").startswith(TECHMEME_FETCH))
            if metered and self.n["attempted"] >= self.max_items:
                continue
            key = next((d for d in PAUSE_DOMAINS if host_in(it["url"], (d,))), host_of(it["url"]))
            if metered and key in self.paused:
                self.paused[key] += 1
                continue
            if metered:
                self.n["attempted"] += 1
            if r != "youtube":                # printed only once a subtitle exists
                self.head(it, r)
            try:
                getattr(self, "do_" + r)(it, key)
            except Exception as e:           # one bad item must not end the run
                self.n["failed"] += 1
                print(f"    error ({type(e).__name__}: {e}), kept pending")
            if r not in ("feed", "youtube", "blank"):
                time.sleep(SLEEP)

    @staticmethod
    def head(it, r):
        print(f"[{r}] ({it.get('published_at') or 'no date'}) {(it.get('title') or '')[:60]}\n"
              f"    {it['url']}")

    # ---- handlers ----
    def do_blank(self, it, _key):
        self.done(it, BLANK_SUMMARY, "douban mark, blank")

    def do_feed(self, it, _key):
        html = it["feed_content"]
        self.thumb(it, html_mod.unescape(html))          # even if the summary fails
        text = html.strip()
        s = textproc.build(text, "feed", meta=len(text) < extract.MIN_BODY)
        if s:
            self.done(it, s, f"feed, {len(text)} chars")
        elif s is None:             # undecodable feed copy: drop it, the page gets fetched next run
            it.pop("feed_content", None)
            self.n["failed"] += 1
            self.touch()
        else:                       # feed copy held only boilerplate / images: nothing to say
            self.done(it, BLANK_SUMMARY, "only boilerplate in feed copy, blank")

    def do_youtube(self, it, _key):
        if not it.get("thumbnail"):
            m = re.search(r"(?:[?&]v=|/shorts/|/live/|youtu\.be/)([\w-]{6,20})(?![\w-])", it["url"])
            if m:
                it["thumbnail"] = f"https://img.youtube.com/vi/{m.group(1)}/mqdefault.jpg"
                self.touch()
        path = pick_subtitle(it.get("id", ""))
        if not path:                          # download_sub.py hasn't fetched one yet
            return
        self.head(it, "youtube")
        text = textproc.vtt_to_text(path)
        if len(text) < textproc.MIN_CAPTION_CHARS:
            return self.done(it, BLANK_SUMMARY, f"{path.name}: no speech, blank")
        s = textproc.build(text, "subtitle")
        if s:
            self.done(it, s, f"{path.name}, {len(text)} chars")
        else:
            self.n["failed"] += 1

    def do_techmeme(self, it, key):
        # Sources:/Report:/Documents: headlines rest on obtained reporting:
        # read the page; others get the translated headline.
        if not it["title"].startswith(TECHMEME_FETCH):
            self.title_fallback(it)
            return
        self.do_fetch(it, key, kind="bridge", fallback=self.title_fallback)

    def do_fetch(self, it, key, kind="page", fallback=None):
        feed = it.get("feed_content") or ""
        found = {}
        f = extract.fetch(it["url"], feed, found)
        if not it.get("thumbnail"):
            if t := found.get("thumbnail") or thumbs.extract(html_mod.unescape(feed), it["url"]):
                it["thumbnail"] = t
                self.touch()
        s = textproc.build(f.text, kind, f.table, f.code, f.kind == "meta") if f.text else ""
        if s:
            return self.done(it, s, f"{f.kind}, {len(f.text)} chars")
        if fallback and fallback(it):
            return
        if f.text and s is not None and f.kind in ("body", "meta"):    # page read fine, but only boilerplate in it
            return self.done(it, BLANK_SUMMARY, f"{f.kind}: only boilerplate, blank")
        if f.kind == "blocked":              # site-wide refusal: pause host, stay pending
            self.paused.setdefault(key, 0)
            self.n["blocked"] += 1
            print(f"    blocked -> kept pending, {key} paused for this run")
        elif f.kind == "gone":               # permanent answer for this url
            it["summary"] = GONE_SUMMARY
            it.pop("feed_content", None)
            self.n["gone"] += 1
            self.touch()
            print("    gone (404/410, no archive) -> placeholder")
        else:
            self.n["failed"] += 1
            print("    failed -> kept pending")


def backfill(items, *, translate=True, deadline=None, save=None, limit=0) -> Counter:
    """Bring stored items up to the current rules: thumbnails, 簡體, language."""
    n, targets = Counter(), []
    for it in items:
        if it.get("thumbnail"):
            ok, why = thumbs.still_valid(it)
            if not ok:
                del it["thumbnail"]
                n["thumbnail"] += 1
                n[f"thumb: {why}"] += 1
        s = it.get("summary")
        if not s or not s.strip() or s == GONE_SUMMARY:
            continue
        if lang.garbled(s):                     # stored mojibake / translated garbage
            del it["summary"]                   # pending again: re-fetched with the fixed decoder
            n["garbled"] += 1
            continue
        if (t := textproc.restrip(s)) != s:              # rules added since it was written
            it["summary"] = s = t
            n["boilerplate"] += 1
            if s == BLANK_SUMMARY:
                continue
        if lang.variant(s) == "hans" and (t := lang.to_twp(s)) != s:
            it["summary"] = s = t
            n["simplified"] += 1
        if lang.needs_translation(s):
            targets.append(it)
    targets = targets[:limit] if limit else targets
    print(f"Backfill: thumbnails dropped={n['thumbnail']}, boilerplate={n['boilerplate']}, "
          f"simplified={n['simplified']}, garbled reset={n['garbled']}, "
          f"non-Chinese={len(targets)}"
          + "".join(f"\n  {k}×{v}" for k, v in n.items() if k.startswith("thumb: ")))
    if not (targets and translate):
        return n
    print("Backfill: DeepL " + ("on" + (" (%s/%s chars used)" % u if (u := lang.deepl_usage(extract.session)) else "")
                                if lang.deepl_on else "not configured (DEEPL_API_KEY) -- gtx only"))
    reasons, streak = Counter(), 0
    for pos in range(0, len(targets), BATCH):
        if deadline and time.monotonic() > deadline:
            print(f"Backfill: time budget reached after {pos}.")
            break
        batch = targets[pos:pos + BATCH]
        marks = [FALLBACK_MARK if it["summary"].rstrip().endswith(FALLBACK_MARK) else "" for it in batch]
        bodies = [it["summary"].rstrip().rstrip(FALLBACK_MARK).strip() for it in batch]
        for it, mark, (out, why) in zip(batch, marks, lang.translate_many(
                bodies, extract.session, stop_after=MAX_FAIL_STREAK - streak)):
            if out:
                it["summary"] = (lang.to_twp(out) + " " + mark).rstrip()
                n["translated"] += 1
                streak = 0
            else:
                n["failed"] += 1
                reasons[lang.short_reason(why) or "unknown"] += 1
                streak += 1
        if save and n["translated"]:
            save()
        if streak >= MAX_FAIL_STREAK:
            print(f"Backfill: {streak} consecutive failures; rest left for a later run.")
            break
        time.sleep(SLEEP)
    print(f"Backfill: translated={n['translated']} failed={n['failed']} "
          f"providers={dict(lang.PROVIDERS)}"
          + (f"\n  reasons: {dict(reasons.most_common(5))}" if reasons else "")
          + (f"\n  DeepL failures (fell back to gtx): {dict(lang.FAILURES)}" if lang.FAILURES else ""))
    return n


def mine_boilerplate(items, min_count: int) -> None:
    """Sentences repeated across summaries that no rule removes yet."""
    counts, sources = Counter(), {}
    for it in items:
        for sent in textproc.SENT_RE.split(it.get("summary") or ""):
            sent = sent.strip()
            if 6 <= len(sent) <= 120 and not textproc.is_boilerplate(sent, "*") \
                    and textproc.strip_boilerplate(textproc.clean_caption(sent)):
                counts[sent] += 1
                sources.setdefault(sent, set()).add(it.get("source") or "?")
    rows = sorted(((c, s) for s, c in counts.items() if c >= min_count), reverse=True)
    for label, want in (("single source -> scope page", 1), ("cross-source -> scope all", 2)):
        print(f"── {label} ──")
        for c, s in [r for r in rows if min(len(sources[r[1]]), 2) == want][:60]:
            print(f"{c:>5}× [{len(sources[s])} src] {s[:80]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items-file", default=os.environ.get("ITEMS_FILE", "data/archive.json"))
    ap.add_argument("--max-items", type=int, default=int(os.environ.get("MAX_ITEMS", "50")))
    ap.add_argument("--time-budget-seconds", type=int,
                    default=int(os.environ.get("TIME_BUDGET_SECONDS", "0")),
                    help="fetching stops at half of this; backfill may use the rest")
    ap.add_argument("--no-translate", dest="translate", action="store_false", default=textproc.TRANSLATE)
    ap.add_argument("--no-backfill", dest="backfill", action="store_false")
    ap.add_argument("--backfill-only", action="store_true")
    ap.add_argument("--offline-only", action="store_true",
                    help="no page fetches, no backfill")
    ap.add_argument("--dry-run", action="store_true", help="with --backfill-only: report only")
    ap.add_argument("--limit", type=int, default=0, help="with --backfill-only: translate at most N")
    ap.add_argument("--mine-boilerplate", type=int, default=0, metavar="MIN_COUNT")
    a = ap.parse_args(argv)
    textproc.TRANSLATE = a.translate

    if not os.path.exists(a.items_file):
        print(f"ERROR: {a.items_file} not found.", file=sys.stderr)
        return 1
    doc = load_doc(a.items_file)
    items = doc["items"]
    save = lambda: save_doc(a.items_file, doc)

    if a.mine_boilerplate:
        mine_boilerplate(items, a.mine_boilerplate)
        return 0
    if a.backfill_only:
        if a.dry_run:
            t = [it for it in items if lang.needs_translation(it.get("summary"))]
            print(f"would translate {len(t)}")
            for it in t[:5]:
                print(f"  {it['url'][:70]}  {it['summary'][:60]}")
            return 0
        backfill(items, translate=a.translate, save=save, limit=a.limit)
        save()
        return 0

    start = time.monotonic()
    budget = a.time_budget_seconds
    pending = sorted(filter(is_pending, items), key=lambda it: _ts(it.get("published_at")), reverse=True)
    run = Run(a.items_file, doc, a.max_items, start + budget * 0.5 if budget else None)
    try:
        run.process(pending, offline_only=a.offline_only)
        print(f"Done. {dict(run.n)}; paused hosts: {dict(run.paused) or '-'}")
        if textproc.STATS:
            print("Stats: " + ", ".join(f"{k}×{v}" for k, v in sorted(textproc.STATS.items())))
        if a.backfill and not a.offline_only:
            try:
                backfill(items, translate=a.translate,
                         deadline=start + budget if budget else None, save=save)
            except Exception as e:
                print(f"ERROR: backfill aborted ({type(e).__name__}: {e})")
    finally:
        # feed_content is scratch: update_news writes it, only this reads it.
        for it in items:
            it.pop("feed_content", None)
        save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
