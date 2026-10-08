#!/usr/bin/env python3
"""Quality check for stored summaries, with the fix each problem calls for.

Categories (from the former check_summaries.py):
  garbled    binary / U+FFFD, UTF-8 read as Latin-1 or CP1251/CP866,
               broken machine translation (tiny clauses, Han interleaved with Cyrillic)
  loop       one word repeated, or a 2-3 clause cycle
  scrape     only site chrome (Medium nav, Steam languages, GitHub sign-in,
               Substack paywall, "enable JavaScript"), or leftover HTML tags.
               HTML inside a technical article (code samples) is NOT flagged.
  mismatch   different titles sharing one summary (reposts / retitled
               copies of the same article are excluded)
  spam       promotion only: top-up amounts, member recruiting, growth-studio
               ads. News *about* gambling or adult industries is not spam.

Fixes (what Pass 3 applies; the CLI only prints unless --fix):
  repair   rewrite in place: mojibake repaired, loops collapsed, tags stripped
  refetch  delete summary -> pending (one more try from the source)
  blank    BLANK_SUMMARY: spam, or a YouTube item whose local subtitle would
           only rebuild the same text

No state is stored. The retry is bounded by `gate`, which summarize_feed runs
on every new summary before writing it: a fresh summary with the same problem
is repaired or written as BLANK, never stored, so backfill cannot reset the
same item again (unless the rules themselves change).

No report file is written: problem titles and fix hints go to stdout (the Actions
log), plus `::warning::` annotations when running on GitHub Actions.

    python scripts/summary_qc.py [--items-file data/archive.json] [--fix]
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from collections import defaultdict
from typing import NamedTuple

import lang
import textproc
from common import (BLANK_SUMMARY, FALLBACK_MARK, GONE_SUMMARY, PLACEHOLDER_PREFIX,
                    is_youtube, load_doc, save_doc)

from extract import JUNK_RE            # interstitials / challenge pages: one list for page and summary


class Finding(NamedTuple):
    category: str
    detail: str
    snippet: str
    action: str                 # repair | refetch | blank
    fixed: str | None = None    # new text for repair


def snippet(s: str, start: int = 0, width: int = 60) -> str:
    a = max(0, start - width // 3)
    return re.sub(r"\s+", " ", s[a:a + width])


# ---------------------------------------------------------------- 1 亂碼 ----
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MOJI_LATIN = re.compile(r"[ÃÂâ€¦¤§¨©®±´µ¶¸¹º»¼½¾¿ÐÑØÞßçéèêëìíîïðñòóôõöùúûüýþÿ]")
_MOJI_BOX = re.compile(r"[\u2550-\u256c\u2580-\u259f\u2524\u251c\u252c\u2534\u253c]")
_CYR = re.compile(r"[\u0400-\u04ff]")
# letters CP1251/CP866 mojibake produces but real Russian/Ukrainian almost never uses
_CYR_RARE = re.compile(r"[\u0402\u0403\u0405\u0408-\u040c\u040e\u040f\u0452\u0453\u0455"
                       r"\u0458-\u045c\u045e\u045f\u0460-\u048f\u0492-\u04ff]")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_CLAUSE = re.compile(r"[，。,、！？!?；;：:\s]+")


def check_garbled(s: str) -> Finding | None:
    n = len(s)
    rep, ctl = s.count("\ufffd"), len(_CTRL.findall(s))
    if rep > 5 or ctl > 3:
        return Finding("garbled", "binary data or replacement characters", snippet(s), "refetch")
    han = len(_HAN.findall(s))
    moj = len(_MOJI_LATIN.findall(s))
    if moj > 30 and moj > 0.05 * n and han:
        return _mojibake(s, "UTF-8 read as Latin-1", _MOJI_LATIN.search(s).start())
    rare, box, cyr = len(_CYR_RARE.findall(s)), len(_MOJI_BOX.findall(s)), len(_CYR.findall(s))
    if rare >= 5 or (box >= 5 and cyr >= 5):
        m = _CYR_RARE.search(s) or _MOJI_BOX.search(s)
        return _mojibake(s, "UTF-8 read as CP1251/CP866", m.start())
    if lang.garbled(s) and not lang.find_loops(s):
        return _mojibake(s, "UTF-8 read as cp1252/cp1256", 0)
    if han > 200:                                   # broken machine translation
        clauses = [c for c in _CLAUSE.split(s) if c]
        hz = [c for c in clauses if _HAN.search(c) and not re.search(r"[0-9A-Za-z=+\-*/^()]", c)]
        if len(hz) > 40 and len(hz) > 0.6 * len(clauses):
            ratio = sum(len(c) <= 3 for c in hz) / len(hz)
            avg = sum(map(len, hz)) / len(hz)
            cyr_runs = len(re.findall(r"[\u0400-\u04ff]+", s))
            if (avg < 4.0 and ratio > 0.45) or (avg < 5.0 and ratio > 0.35 and cyr_runs >= 20):
                return Finding("garbled", "broken machine translation" + (" mixed with Cyrillic" if cyr_runs >= 20 else ""),
                               snippet(s, n // 3), "refetch")
    return None


def _mojibake(s: str, kind: str, at: int) -> Finding:
    fixed = lang.fix_mojibake(s)
    if fixed != s and not lang.garbled(fixed) and not _CYR_RARE.search(fixed):
        return Finding("garbled", f"{kind}, repairable", snippet(s, at), "repair", fixed)
    return Finding("garbled", kind, snippet(s, at), "refetch")


# ------------------------------------------------------------ 2 重複迴圈 ----
def check_loop(s: str) -> Finding | None:
    loops = lang.find_loops(s)
    if not loops:
        return None
    start, _, unit, reps = max(loops, key=lambda lp: lp[1] - lp[0])
    fixed = lang.collapse_loops(s)
    detail = f"\"{unit.strip()[:20]}\" repeated"
    if len(fixed.strip()) < 30 or len(fixed) < 0.4 * len(s):   # mostly loop: nothing to keep
        return Finding("loop", detail + ", mostly loop", snippet(s, start), "refetch")
    return Finding("loop", detail, snippet(s, start), "repair", fixed)


# ------------------------------------------------------------ 3 抓取失敗 ----
BOILERPLATE = [
    r"Sign up Sign in", r"Open in app", r"Get the app", r"Sitemap Open in app",
    r"網站地圖 在應用程式中開啟", r"在應用程式中開啟 註冊 登入",
    r"You signed in with another tab", r"You switched accounts on another tab",
    r"你已在另一個分頁或視窗登入", r"Reload to refresh your session", r"Sign in to GitHub",
    r"Get the Steam Mobile App", r"取得 Steam 行動應用程式",
    r"Install Steam .{0,40}(?:Bahasa|Deutsch|Español)", r"簡體中文 繁體中文 日本語 한국어",
    r"This site requires JavaScript", r"本網站需要啟用 JavaScript", r"需要啟用 JavaScript",
    r"You need to enable JavaScript",
    r"Claim my free post", r"領取免費文章", r"Keep reading with a \d+-day free trial",
    r"Subscribe Sign in", r"訂閱 登入", r"This post is for paid subscribers",
    r"403 Forbidden", r"404 Not Found", r"Page not found", r"找不到頁面", r"Sorry, you have been blocked",
    r"Skip to (?:main )?content", r"跳至主要內容", r"Toggle navigation",
]
_BOILER = re.compile("|".join(b.replace(" ", r"\s*") for b in BOILERPLATE), re.I)
_HTML = re.compile(r"</?(?:div|span|script|style|iframe|table|tr|td|img|meta|link|svg|path|button|"
                   r"input|p|br|li|ul|ol|a|h[1-6]|section|article|nav|footer|header)\b[^>\n]{0,200}>"
                   r"|&nbsp;|&amp;|&lt;|&gt;|&#\d+;", re.I)
_HTML_STRONG = re.compile(r"width=device-width|name=[\"']?viewport|OptanonWrapper|crossorigin=|"
                          r"<meta\b|<script\b[^>]*\bsrc=|<link\b[^>]*\brel=|class=\"[\w\- :/\[\]]{20,}\"", re.I)
looks_like_code = textproc.looks_like_code


def check_scrape(s: str) -> Finding | None:
    hits = _BOILER.findall(s) + JUNK_RE.findall(s)
    if hits and (len(s) < 800 or len(hits) >= 3):
        m = _BOILER.search(s) or JUNK_RE.search(s)
        return Finding("scrape", f"site chrome only: \"{m.group(0)}\"",
                       snippet(s, m.start()), "refetch")
    tags = _HTML.findall(s)
    strong = _HTML_STRONG.search(s)
    if not (strong or len(tags) >= 5 or (tags and len(s) < 300)) or looks_like_code(s):
        return None
    m = strong or _HTML.search(s)
    body = s.rstrip()
    mark = FALLBACK_MARK if body.endswith(FALLBACK_MARK) else ""
    body = body[:-1].rstrip() if mark else body
    clean = re.sub(r"<[^>]{0,300}>", " ", body)
    clean = re.sub(r"&(?:nbsp|amp|lt|gt|#\d+);", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    detail = "leftover HTML"
    if len(clean) >= 80 and len(clean) >= 0.6 * len(body) and not _HTML_STRONG.search(clean):
        return Finding("scrape", detail + ", tags stripped", snippet(s, m.start()), "repair",
                       (clean + " " + mark).rstrip())
    return Finding("scrape", detail, snippet(s, m.start()), "refetch")


# ------------------------------------------------------------ 5 垃圾內容 ----
_PROMO = re.compile("|".join([
    r"KEBAYA\w*", r"tebak skor", r"member aktif", r"deposit\s*(?:minimal|Rp)", r"\bRp\s?\d",
    r"slot gacor", r"situs (?:judi|slot|resmi|terpercaya)", r"daftar sekarang", r"link alternatif",
    r"bonus new member", r"growth studio", r"full-stack growth", r"SEO, AEO", r"成長工作室",
    r"代儲", r"儲值\s*\d", r"首儲", r"註冊送", r"加入會員[領送]", r"招募會員", r"刷粉", r"buy followers",
    r"代寫論文", r"加賴", r"加\s*LINE", r"top[\s-]?up\s*\$?\d",
]), re.I)
_PROMO_TOPIC = re.compile(r"\bjudi\b|togel|taruhan|bandar|casino|betting|sportsbook|博彩|娛樂城|百家樂|"
                          r"線上賭|老虎機|porn|onlyfans|escort|成人影片", re.I)


def check_spam(s: str, title: str) -> Finding | None:
    text = s + " " + title
    promo = {m.group(0).lower() for m in _PROMO.finditer(text)}
    if len(promo) >= 2 or (promo and _PROMO_TOPIC.search(text)):
        topic = {m.group(0).lower() for m in _PROMO_TOPIC.finditer(text)}
        return Finding("spam", "promotion: " + ", ".join(sorted(promo))[:60], snippet(s), "blank")
    return None


# ------------------------------------------------------------ 3 標題不符 ----
_BAD_URL = re.compile(r"/(?:undefined|null|NaN|None)(?:/|$|\?)", re.I)
_STOP_EN = set("this that with from have will what when your about into they them their there "
               "were been more than also just like".split())


def keywords(text: str) -> set:
    text = lang.key_text(text or "").lower()
    out = set(re.findall(r"[a-z][a-z0-9]{3,}", text)) - _STOP_EN
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        out.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return out | set(re.findall(r"\d{2,}", text))


def _url_path(u: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", (u or "").lower()).split("?")[0].rstrip("/")
    return u.split("/", 1)[-1] if "/" in u else u


def _share_key(s: str) -> str:
    return re.sub(r"\s+", "", s.rstrip(FALLBACK_MARK))[:300]


def check_shared(items: list) -> dict[int, Finding]:
    """id(item) -> Finding for items whose summary is shared across different titles."""
    groups = defaultdict(list)
    for it in items:
        s = it.get("summary") or ""
        if not s.strip() or s == GONE_SUMMARY or s.startswith(PLACEHOLDER_PREFIX):
            continue
        key = _share_key(s)
        if len(key) >= 60:
            groups[key].append(it)
    out = {}
    for key, g in groups.items():
        if len(g) < 2 or len({re.sub(r"\W", "", (it.get("title") or "").lower())[:40] for it in g}) < 2:
            continue
        broken = [it for it in g if _BAD_URL.search(it.get("url") or "")]
        paths = [_url_path(it.get("url")) for it in g]
        if not broken and all(difflib.SequenceMatcher(None, paths[0], p).ratio() >= 0.6 for p in paths[1:]):
            continue                                    # same article under several urls
        tks = [keywords(it.get("title")) for it in g]
        if not broken and tks[0] and all(t and len(tks[0] & t) / min(len(tks[0]), len(t)) >= 0.4
                                         for t in tks[1:]):
            continue                                    # retitled copy / repost
        sk = keywords(g[0]["summary"][:1500])
        score = [len(t & sk) / max(1, len(t)) for t in tks]
        best = max(range(len(g)), key=score.__getitem__)
        keeper = best if score[best] >= 0.2 and score.count(score[best]) == 1 else None
        names = " / ".join((it.get("title") or "")[:24] for it in g)
        for i, it in enumerate(g):
            if i == keeper and it not in broken:
                continue
            why = "broken url" if it in broken else "title does not match"
            out[id(it)] = Finding("mismatch", f"{why}; summary shared with: {names}",
                                  snippet(key), "refetch")
    return out


# ---------------------------------------------------------------- apply -----
def inspect(it: dict) -> Finding | None:
    """The one finding that decides the fix for this item (spam first, then
    garbled before loops: collapsing a loop inside mojibake fixes nothing)."""
    s = it.get("summary") or ""
    if not s.strip() or s == GONE_SUMMARY:
        return None
    return (check_spam(s, it.get("title") or "") or check_garbled(s)
            or check_loop(s) or check_scrape(s))


def _settle(it: dict, f: Finding) -> str:
    if f.action == "refetch" and is_youtube(it.get("url") or "") and f.category != "loop":
        return "blank"              # the local subtitle would rebuild the same text
    return f.action


def apply(items: list, fix: bool = True) -> list:
    """Check every stored summary; when `fix`, change items in place.
    Returns [(item, finding, action)]."""
    shared = check_shared(items)
    rows = []
    for it in items:
        f = inspect(it) or shared.get(id(it))
        if not f:
            continue
        action = _settle(it, f)
        rows.append((it, f, action))
        if not fix:
            continue
        if action == "repair":
            it["summary"] = f.fixed
        elif action == "blank":
            it["summary"] = BLANK_SUMMARY
        else:
            it.pop("summary", None)
    return rows


def gate(it: dict, summary: str, items: list) -> tuple[str, Finding | None]:
    """The summary to actually write for `it`. A new summary that fails QC is
    repaired, or becomes BLANK: it came fresh from the source, so another try
    would return the same thing."""
    if not summary or not summary.strip() or summary == GONE_SUMMARY:
        return summary, None
    probe = {**it, "summary": summary}
    f = inspect(probe)
    if not f:
        key = _share_key(summary)
        peers = [x for x in items if x is not it and len(key) >= 60
                 and _share_key(x.get("summary") or "") == key] if len(key) >= 60 else []
        f = check_shared(peers + [probe]).get(id(probe)) if peers else None
    if not f:
        return summary, None
    return (f.fixed, f) if f.action == "repair" else (BLANK_SUMMARY, f)


# ---------------------------------------------------------------- hints -----
CATEGORIES = ("garbled", "loop", "scrape", "mismatch", "spam")
HINTS = {
    "garbled": "if one site keeps showing up, check its Content-Type / <meta charset>, add a byte sample "
               "to selftest.check_encoding, then fix common.decode_body; broken translation usually "
               "means the source itself was mojibake.",
    "loop": "collapsed in place, no retranslation needed. If one channel keeps looping, try a larger "
            "Whisper model or beam_size>1 for it.",
    "scrape": "for repeated chrome on one site, mine a rule with `summarize_feed.py --mine-boilerplate 3` "
              "into summary_boilerplate.json; add bot-walled sites to SLOW_HOSTS; paywalled sites whose "
              "feed has full text go in FEED_FIRST_HOSTS.",
    "mismatch": "check whether the feed's links all point to one page (canonical_url, redirects) or "
                "feed_content is filled in twice.",
    "spam": "if one feed keeps producing it, remove the feed from the OPML; already blanked.",
}


def report(rows: list, out=print) -> None:
    """Problem titles by category, with the fix taken and a maintainer hint. stdout only."""
    if not rows:
        out("QC: no problems")
        return
    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    for cat in CATEGORIES:
        rs = [r for r in rows if r[1].category == cat]
        if not rs:
            continue
        out(f"QC {cat}: {HINTS[cat]}")
        for it, f, a in rs:
            title = (it.get("title") or it.get("url") or "").strip()
            out(f"  {a:<7}  {title}  ({f.detail})")
        if gh:
            titles = "; ".join((it.get("title") or "")[:40] for it, _, _ in rs[:5])
            print(f"::warning title=summary QC {cat}::{titles}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--items-file", default=os.environ.get("ITEMS_FILE", "data/archive.json"))
    ap.add_argument("--fix", action="store_true", help="apply fixes and save (default: print only)")
    a = ap.parse_args(argv)
    doc = load_doc(a.items_file)
    rows = apply(doc["items"], fix=a.fix)
    report(rows)
    if a.fix and rows:
        save_doc(a.items_file, doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
