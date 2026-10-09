#!/usr/bin/env python3
"""Summarise pending archive items, then re-apply current rules to stored ones.

Pending = no truthy `summary`. Offline work (feed copies, subtitles) runs first
and is not metered; page fetches are capped by --max-items. Pass 3 (backfill)
re-validates thumbnails, converts 簡體, and translates non-Chinese summaries.

    summarize_feed.py                       # normal run (env: ITEMS_FILE, MAX_ITEMS...)
    summarize_feed.py --offline-only          # feed copies + local subtitles only, no fetches (after download_sub)
    summarize_feed.py --backfill-only [--dry-run] [--limit N] [--no-translate]
    (summary QC runs first in every backfill; standalone: summary_qc.py [--fix])
    summarize_feed.py --mine-boilerplate N  # candidate drop_unit rules from corpus
"""

from __future__ import annotations

import argparse
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
import summary_qc
import textproc
import thumbs
from common import (BLANK_SUMMARY, drop_scratch, load_scratch, FALLBACK_MARK, GONE_SUMMARY, host_in, host_of,
                    is_pending, is_youtube, load_doc, save_doc, valid_id)

SUBTITLES_DIR = Path(os.environ.get("SUBTITLES_DIR", "data/subtitles"))
SLEEP = 1.5                   # polite delay after each network item
BATCH, MAX_FAIL_STREAK = 25, 4
SKIP_HOSTS = ("news.google.com",)      # feed copy is redirect debris, page is useless
PAUSE_DOMAINS = ("douban.com",)        # same skeleton page on every subdomain: pause as one
# Douban marks (想看 / 看过 ...): the page is a login skeleton, so the feed copy
# is all there is. Its 备注 is the user's own note; ratings and tags are not.
DOUBAN_MARK_RE = re.compile(r"^(?:想[看读听讀聽]|[看读听讀聽][过過])")
DOUBAN_DROP_RE = re.compile(r"^\s*(?:推荐|推薦|评分|評分|标签|標籤|tags?)\s*[:：]", re.I)
DOUBAN_LABEL_RE = re.compile(r"^\s*(?:备注|備註|短评|短評)\s*[:：]\s*")
# Summarised from the feed copy without fetching the page, when one exists.
# Their feed images (feed_images) supply the thumbnail. YouTube is routed
# before this and update_news stores no feed copy for it.
FEED_FIRST_HOSTS = tuple("""
abei.club aftermath.site ageofinvention.xyz artincontext.org attlin.com beartalking.com
bituzi.com blocktempo.com buttondown.com caffes.me careher.net cashchou.com
chaidarun.com cityofsound.com cocktail4party.com coolshell.cn curtismchale.ca davidoks.blog
devtang.com esence.travel first-cafe.com firstround.com fomosoc.com fs.blog gilifedesigner.com
honest-broker.com huli.tw hunterwalk.com joestudwell.com kopu.chat limboy.me
lipperalpha.refinitiv.com lostmagazine.org louie.lu lutaonan.com matters.town maxjamesread.com
mcclin.blogspot.com medium.com meiguinfo.com mickzh.com noswag.tw notesbylex.com personaljournal.ca polgeonow.com
pseudoyu.com readtrung.com ruanyifeng.com samaltman.com shenlvmeng.github.com shiuncorner.com
sirupsen.com sive.rs smallbooks.com.tw soidid.tw starrocket.io steveblank.com substack.com
techcabal.com techmeme.com tiaodao.typlog.io travelwithbook.com trensse.com
unchartedterritories.tomaspueyo.com uselessetymology.com vox.com waitbutwhy.com werner.wiki
whogovernstw.org yuanyu.idv.tw zmonster.me bestblogs.dev
""".split())

# route -> (needs network, counts against --max-items)
ROUTES = {
    "douban":  (False, False),
    "feed":    (False, False),   # feed copy only, never the page
    "youtube": (False, False),
    "fetch":   (True, True),
}


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


def route(it) -> str | None:
    url = it["url"]
    if host_in(url, SKIP_HOSTS):
        return None
    if is_youtube(url):
        return "youtube"
    if host_in(url, ("douban.com",)) and DOUBAN_MARK_RE.match((it.get("title") or "").strip()):
        return "douban"
    if host_in(url, FEED_FIRST_HOSTS) and ((it.get("feed_content") or "").strip() or it.get("feed_images")):
        return "feed"
    return "fetch"


def pause_key(url: str) -> str:
    return next((d for d in PAUSE_DOMAINS if host_in(url, (d,))), host_of(url))


def douban_note(feed: str) -> str:
    """The user's own words in a douban mark's feed copy: ratings/tags dropped, labels stripped."""
    lines = (DOUBAN_LABEL_RE.sub("", ln).strip() for ln in feed.splitlines() if not DOUBAN_DROP_RE.match(ln))
    return "\n".join(ln for ln in lines if ln)


class Run:
    def __init__(self, path, doc, max_items: int, deadline: float | None, offline_only=False):
        self.path, self.doc, self.max_items, self.deadline = path, doc, max_items, deadline
        self.offline_only = offline_only
        self.n = Counter()
        self.paused: dict[str, int] = {}          # pause key -> items skipped since

    # ---- bookkeeping ----
    def touch(self):
        save_doc(self.path, self.doc)          # every change reaches disk at once

    def done(self, it, summary, how):
        summary, f = summary_qc.gate(it, summary, self.doc["items"])
        if f:
            how += f"; QC {f.category} ({f.detail}) -> " + ("repaired" if summary.strip() else "blank")
        it["summary"] = summary
        drop_scratch(it)
        self.n["ok"] += 1
        print(f"    ok ({how})")
        self.touch()

    def feed_thumb(self, it) -> bool:
        """Thumbnail from the feed's own image urls; True when the item has one."""
        if not it.get("thumbnail") and (t := thumbs.pick(it.get("feed_images"), it["url"], "feed")):
            it["thumbnail"] = t
            self.touch()
        return bool(it.get("thumbnail"))

    def claim_fetch(self, it) -> bool:
        """One page fetch for `it`, if the cap, the pause list and the mode allow it."""
        if self.offline_only or self.n["attempted"] >= self.max_items:
            return False
        key = pause_key(it["url"])
        if key in self.paused:
            self.paused[key] += 1
            return False
        self.n["attempted"] += 1
        return True

    # ---- main loop ----
    def process(self, pending: list):
        routed = [(r, it) for it in pending if (r := route(it))]
        routed.sort(key=lambda x: ROUTES[x[0]][0])          # offline first; stable keeps date order
        if self.offline_only:
            routed = [x for x in routed if not ROUTES[x[0]][0]]
        n_off = sum(not ROUTES[r][0] for r, _ in routed)
        print(f"pending={len(pending)}: offline={n_off} network={len(routed) - n_off} "
              f"(fetch cap {self.max_items})")
        for r, it in routed:
            if self.deadline and time.monotonic() > self.deadline:
                self.n["time_cut"] = 1
                print("Time budget reached; the rest stay pending.")
                break
            if ROUTES[r][1] and not self.claim_fetch(it):
                continue
            before = self.n["attempted"]
            if r != "youtube":                # printed only once a subtitle exists
                self.head(it, r)
            try:
                getattr(self, "do_" + r)(it)
            except Exception as e:           # one bad item must not end the run
                self.n["failed"] += 1
                print(f"    error ({type(e).__name__}: {e}), kept pending")
            if ROUTES[r][0] or self.n["attempted"] > before:
                time.sleep(SLEEP)

    @staticmethod
    def head(it, r):
        print(f"[{r}] ({it.get('published_at') or 'no date'}) {(it.get('title') or '')[:60]}\n"
              f"    {it['url']}")

    # ---- offline handlers ----
    def do_douban(self, it):
        """想X and X过 alike: the note from the feed copy, else blank. Never fetched."""
        note = douban_note(it.get("feed_content") or "")
        s = textproc.build(note, "feed") if note else ""
        if s:
            self.done(it, s, f"douban mark, feed note {len(note)} chars")
        else:                       # no feed copy / rating only / undecodable: nothing to say
            self.done(it, BLANK_SUMMARY, "douban mark, no note in feed, blank")

    def do_feed(self, it):
        """Feed copy only, never the page: text -> summary, else blank.
        The thumbnail comes from feed_images either way."""
        self.feed_thumb(it)
        text = (it.get("feed_content") or "").strip()
        s = textproc.build(text, "feed", meta=len(text) < extract.MIN_BODY) if text else ""
        if s:
            self.done(it, s, f"feed, {len(text)} chars")
        elif s is None:             # undecodable feed copy: stays pending for a fresh copy
            drop_scratch(it)
            self.n["failed"] += 1
            self.touch()
            print("    undecodable feed copy -> kept pending")
        else:                       # only boilerplate / images: nothing to say
            self.done(it, BLANK_SUMMARY, "no text in feed copy, blank")

    def do_youtube(self, it):
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

    # ---- network handlers ----
    def do_fetch(self, it):
        feed = it.get("feed_content") or ""
        found = {}
        f = extract.fetch(it["url"], feed, found)
        if not it.get("thumbnail") and found.get("thumbnail"):
            it["thumbnail"] = found["thumbnail"]
            self.touch()
        self.feed_thumb(it)                  # page had none: the feed's images may
        s = textproc.build(f.text, "page", f.table, f.code, f.kind == "meta") if f.text else ""
        if s:
            return self.done(it, s, f"{f.kind}, {len(f.text)} chars")
        if f.text and s is not None and f.kind in ("body", "meta"):    # page read fine, but only boilerplate in it
            return self.done(it, BLANK_SUMMARY, f"{f.kind}: only boilerplate, blank")
        key = pause_key(it["url"])
        if f.kind == "blocked":              # site-wide refusal: pause host, stay pending
            self.paused.setdefault(key, 0)
            self.n["blocked"] += 1
            print(f"    blocked -> kept pending, {key} paused for this run")
        elif f.kind == "gone":               # permanent answer for this url
            it["summary"] = GONE_SUMMARY
            drop_scratch(it)
            self.n["gone"] += 1
            self.touch()
            print("    gone (404/410, no archive) -> placeholder")
        else:
            self.n["failed"] += 1
            print("    failed -> kept pending")


def backfill(items, *, translate=True, deadline=None, save=None, limit=0) -> Counter:
    """Bring stored items up to the current rules: QC fixes, thumbnails, 簡體, language."""
    n, targets = Counter(), []
    summary_qc.report(summary_qc.apply(items, fix=True))   # mojibake, loops, chrome, mismatch, spam
    for it in items:
        if it.get("thumbnail"):
            ok, why = thumbs.still_valid(it)
            if not ok:
                del it["thumbnail"]
                n["thumbnail"] += 1
                n[f"thumb: {why}"] += 1
        s = it.get("summary")
        if not s or not s.strip() or s == GONE_SUMMARY:   # QC above may have blanked it
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
          f"simplified={n['simplified']}, "
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
    if not a.mine_boilerplate and not a.backfill_only:
        load_scratch(items)                    # update_news's feed copies, this job only
    save = lambda: save_doc(a.items_file, doc)

    if a.mine_boilerplate:
        mine_boilerplate(items, a.mine_boilerplate)
        return 0
    if a.backfill_only:
        if a.dry_run:
            summary_qc.report(summary_qc.apply(items, fix=False))
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
    run = Run(a.items_file, doc, a.max_items, start + budget * 0.5 if budget else None,
              offline_only=a.offline_only)
    try:
        run.process(pending)
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
        save()                                 # save_doc never writes scratch fields
    return 0


if __name__ == "__main__":
    sys.exit(main())
