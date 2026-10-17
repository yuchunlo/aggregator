#!/usr/bin/env python3
"""Summarise pending archive items, then re-apply current rules to stored ones.

Pending = no truthy `summary`. Offline work (feed copies, local subtitles) runs
first and is not metered; page fetches are capped by MAX_ITEMS and run in
parallel across hosts (one at a time per host). Sections: thumbnails, article
fetch (page, fingerprint, feed copy, reader proxy, Wayback), summary QC, the
run loop, backfill.

collect.py and media.py call `run()` in-process. As a command it is a
maintenance tool on DATA_DIR/archive.json:

    summarize.py backfill [--dry-run | --qc-only] [--limit N] [--no-translate]
    summarize.py mine N        # candidate drop_unit rules from the corpus
"""

from __future__ import annotations

import argparse
import difflib
import html as html_mod
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import SimpleQueue
from typing import NamedTuple
from urllib.parse import quote, urljoin


import net
import store
import textproc
from store import (BLANK_SUMMARY, FALLBACK_MARK, GONE_SUMMARY, PLACEHOLDER_PREFIX, drop_scratch, host_in,
                   host_of, is_pending, is_youtube, load_doc, parse_date, save_doc, site)
from textproc import JUNK_RE, MIN_BODY, from_html, is_junk, meta_tags, sanitize_markup, unescape


# ============================================================== thumbnails
# Site tables in rules.json (`sites`):
#   thumb_deny          specific files the filename heuristics cannot judge
#                       (sponsor logos, reused covers); lower-case, no query
#   thumb_skip_hosts    pages that never yield a thumbnail (incl. subdomains)
#   thumb_trusted_hosts hosts that serve only article uploads with
#                       meaningless filenames (Blogger)
# Read where they are used (store.site), never at import.
DENY_RE = re.compile(r"^https?://kottke\.org/.*/images/\d{4}/logo-colors/", re.I)


@lru_cache(maxsize=1)
def deny() -> frozenset:
    return frozenset(u.lower() for u in site("thumb_deny"))
TRUSTED_PATH_RE = re.compile(r"^https?://lh\d+\.googleusercontent\.com/blogger_img_proxy/", re.I)
# Google image-proxy rendition suffix; strip it to get the underlying image.
GOOGLE_SIZE_RE = re.compile(r"=w\d+-h\d+-p-k-no-nu$", re.I)

IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|avif|svg)(?:$|[?#])", re.I)
ASSET_DIR_RE = re.compile(r"/(?:themes?|wp-includes|plugins?|icons?|css|skin|sprites?|emoji|ui)/", re.I)
STOCK_RE = re.compile(
    r"shutterstock[_-]?\d*|istock(?:photo)?|gettyimages?[-_]?\d*|gyi\d{6,}"
    r"|depositphotos|adobestock|dreamstime|alamy|123rf|bigstock|stockphoto|unsplash"
    r"|photo-\d{10,}-[0-9a-f]{8,}|pexels(?:-photo)?[-_]?\d*|\bap[-_]?photo\b|associated[-_]press"
    # wire services: Reuters timestamps / RTS ids, AFP, EPA
    r"|\d{4}-?\d{2}-?\d{2}t\d{6}z|\brt[sxr][a-z0-9]{5,}|\bafp[-_]?\d{6,}|\bepa[-_]?(?:efe[-_]?)?\d{6,}",
    re.I)
TEMPLATE_RE = re.compile(
    r"og[-_]?image|og[-_]?default|social[-_]?(?:card|share|image|preview)"
    r"|twitter[-_]?card|share[-_]?(?:image|card)|card[-_]?bg"
    r"|cover[-_]?template|template[-_]?cover|[-_]template\b|^template"
    r"|(?:^|[-_])default(?:[-_](?:image|thumb|cover|banner))?(?:[-_]|$)"   # not mqdefault.jpg
    r"|placeholder|fallback|no[-_]?image|dummy|generic[-_]?(?:image|cover)"
    r"|\blogo\b|wordmark|favicon|avatar|profile[-_]?pic|headshot|portrait[-_]?shot"
    r"|\bbanner\b|\bheader[-_]?(?:image|bg)?\b|hero[-_]?(?:image|bg)"
    r"|watermark|spacer|pixel|blank|transparent|1x1|use[-_]this(?:[-_]|$)"
    # screenshots, demos, collages
    r"|screen[-_ ]?shot|screencap|scrn|\bcapture\b|\bdemo\b|\bpreview\b|\bmockup\b|\bui[-_]"
    r"|collage|montage|grid[-_]?of|poster[-_]?(?:grid|collage|set)"
    r"|line[-_]?up\b|\bcombo\b|side[-_]by[-_]side[-_]?photos?",
    re.I)

_w = lambda s: frozenset(s.split())
CHART = _w("""axis axes chart charts graph graphs plot plotted map maps mapped mapping
diagram schematic figure fig table matrix index indices ratio ratios rate rates share
shares percent percentage pct scale scaled distribution breakdown composition split
spread trend trends trending trajectory curve curves growth decline change delta monitor
monitoring tracker tracking dashboard scorecard comparison compare compared versus vs flip
gap gaps timeline history historical forecast projection projected outlook heatmap treemap
sankey waterfall scatter histogram bubble radar donut quarterly annual monthly yearly ytd
yoy qoq cagr bloomberg reuters-graphics ft economist statista ourworldindata visualcapitalist""")
PHOTO = _w("""photo photograph photographed picture pic image img shot shots snapshot close
closeup up view viewing views viewed angle aerial overhead portrait portraits standing
sitting seated walking holding wearing smiling posing poses posed looking facing gesturing
speaking talking waving exterior interior facade storefront skyline streetscape landscape
location located site situated near outside inside man woman men women people person crowd
worker workers employee background backdrop foreground blurred bokeh attends attending
arrives arriving during ceremony conference press generic stock illustrative illustration
decorative""")
CHROME = _w("""loader spinner throbber skeleton holder empty icon icons sprite sprites qrcode
opengraph ogimg arrow arrows divider separator overlay texture btn button nav navbar badge
ribbon follow subscribe scrolling""")
GRAPHIC = _w("""line lines rule hr dot dots bar bars bg background spacer pixel sep divider
shadow mask gradient texture blank dummy placeholder empty none null loader spinner""")
LAYOUT = _w("""content contents main top bottom left right inner outer wrap wrapper box block
section area panel col row grid cell border edge corner middle center centre side foot base
common default style theme layout title text head header footer heading caption label item
list nav menu sub""")


# Cloud image-processing params (resize/crop renditions): Aliyun OSS, Huawei OBS,
# Baidu BOS, Qiniu. Dropped so the stored url is the original image.
PROCESS_PARAM_RE = re.compile(r"^(?:x-oss-process|x-image-process|x-bce-process|imageMogr2|imageView2)(?:[=/]|$)", re.I)


def thumb_clean(url: str) -> str:
    """`url` without image-processing query params; other params kept."""
    if not url or "?" not in url:
        return url
    base, _, rest = url.partition("?")
    query, hash_, frag = rest.partition("#")
    kept = [p for p in query.split("&") if p and not PROCESS_PARAM_RE.match(p)]
    return base + ("?" + "&".join(kept) if kept else "") + hash_ + frag


def thumb_skipped_host(page_url: str) -> bool:
    return host_in(page_url, site("thumb_skip_hosts"))


def thumb_usable(url: str) -> tuple[bool, str]:
    """(verdict, reason) -- the reason is logged so rules stay tunable."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False, "not an absolute url"
    bare = url.split("?", 1)[0].split("#", 1)[0].strip().lower()
    if bare in deny() or DENY_RE.match(bare):
        return False, "explicitly denied url"
    if bare.endswith(".svg"):
        return False, "svg (usually a logo or icon)"
    if host_in(url, site("thumb_trusted_hosts")) or TRUSTED_PATH_RE.match(url):
        return True, "trusted image host"
    path = url.split("?", 1)[0].split("#", 1)[0]
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if not IMAGE_EXT_RE.search(path):
        return False, "no image extension"
    stem = re.sub(r"\.[a-z0-9]+$", "", name, flags=re.I)
    stem = re.sub(r"[-_]\d{2,4}x\d{2,4}$", "", stem)          # name-1024x576
    stem = re.sub(r"[-_@][123]x$", "", stem, flags=re.I)       # retina suffix
    if len(stem) < 3:
        return False, "filename too short to judge"
    if ASSET_DIR_RE.search(path):
        return False, "served from an asset/theme directory"
    if STOCK_RE.search(f"{stem} {url}"):
        return False, "stock / wire-service filename"
    if TEMPLATE_RE.search(stem):
        return False, "template / furniture / screenshot"
    tokens = [t for t in re.split(r"[^a-z0-9]+", stem.lower()) if t]
    toks = set(tokens)
    if not tokens:
        return False, "no readable filename"
    if len(tokens) == 1 and (len(tokens[0]) >= 16 or tokens[0].isdigit()):
        return False, "opaque id / hash filename"
    if CHART & toks:
        return True, "chart vocabulary in filename"
    if len(tokens) <= 4 and CHROME & toks:
        return False, f"interface chrome ({', '.join(sorted(CHROME & toks))})"
    if toks <= (GRAPHIC | LAYOUT) and GRAPHIC & toks:
        return False, f"decoration filename ({'-'.join(tokens)})"
    if PHOTO & toks:
        return False, f"photographic wording ({', '.join(sorted(PHOTO & toks))})"
    if len(tokens) >= 5:
        return False, f"descriptive phrase ({len(tokens)} tokens)"
    return True, "short topical filename"


META_IMAGE_KEYS = ("og:image", "og:image:url", "twitter:image", "twitter:image:src")


def _dim(v) -> int:
    m = re.match(r"\s*(\d{1,6})", v or "")
    return int(m[1]) if m else 0


def thumb_candidates(html: str, page_url: str, limit: int = 24) -> list[str]:
    """Image urls worth judging, in order: og:image / twitter:image, then body
    <img>s that are not declared icon-sized. Absolute, unescaped, de-duplicated."""
    raws = [v for _, v in meta_tags(html or "", META_IMAGE_KEYS, limit=60000)]
    for _, a in store.iter_tags(html, ("img",)):
        dims = [d for d in (_dim(a.get("width")), _dim(a.get("height"))) if d]
        if a.get("src") and not (dims and max(dims) < 200):     # icon or tracking pixel
            raws.append(a["src"])
        if len(raws) > limit:
            break
    out = []
    for raw in raws:
        url = GOOGLE_SIZE_RE.sub("", urljoin(page_url, html_mod.unescape((raw or "").strip())))
        if url and url not in out:
            out.append(url)
    return out


def thumb_pick(urls, page_url: str, where: str = "page") -> str | None:
    """First usable url among `urls` (already in preference order)."""
    if thumb_skipped_host(page_url):
        return None
    for url in urls or ():
        url = thumb_clean(url)
        ok, reason = thumb_usable(url)
        if ok:
            log(f"    thumbnail ({where}): {url}  [{reason}]")
            return url
    return None


def thumb_from_page(html: str, page_url: str) -> str | None:
    """First usable image of a page: og:image / twitter:image, then body <img>s."""
    if not html or thumb_skipped_host(page_url):
        return None
    return thumb_pick(thumb_candidates(html, page_url), page_url)


def thumb_still_valid(item: dict) -> tuple[bool, str]:
    if thumb_skipped_host(item.get("url") or ""):
        return False, "host opted out"
    return thumb_usable(item["thumbnail"])



# ============================================================== article fetch
# Direct fetch, browser-fingerprint fetch, the feed's own copy, a reader proxy,
# then the Wayback Machine (the last two per CFG.fallbacks).
ARTICLE_TIMEOUT = 30                    # seconds per request, in total
SLOW_ARTICLE_TIMEOUT = 60               # slow_hosts, the reader proxy, Wayback snapshots
RETRIES = 2                             # transport errors, 429 and 5xx
# rules.json `slow_hosts`: bot walls, started with the browser fingerprint.
READER = "https://r.jina.ai/"          # renders JS, fetches from its own IPs; sees every url sent
# net.BLOCK_STATUS counts 503 as a refusal: after retries it is a
# Cloudflare/Akamai challenge page, not an outage.


class Fetched(NamedTuple):
    text: str | None
    kind: str | None        # body | meta | blocked | gone | None (transient)
    table: bool = False
    code: bool = False


# ---- small text fixes -------------------------------------------------------
# HTML -> text (from_html, unescape, MIN_BODY) lives in textproc with the
# other HTML readers.

_MD_LINK = re.compile(r"\[([^\[\]\n]*+)\]\((?:https?:|/|#|mailto:)[^()\s]*+\)")
_MD_IMG = re.compile(r"!\[[^\[\]]*+\]\([^()]*+\)"
                     r"|\[[ \t]*+!?[ \t]*+\[?[ \t]*+(?:Image|圖片|图片|圖像|图像|Figure|插圖|插图)"
                     r"[ \t]*+\d*+[ \t]*+[:：]?[^\[\]]*+\](?:\([^()]*+\))?[ \t]*+\]?", re.I)
_MD = [(re.compile(p, f), r) for p, f, r in (
    (r"^[ \t]*+(?:[-*_][ \t]*+){3,}$", re.M, ""),
    (r"^[ \t]{0,3}#{1,6}[ \t]*+", re.M, ""),
    (r"^[ \t]{0,6}(?:[-*+]|\d{1,2}[.)])[ \t]++", re.M, ""),
    (r"<?https?://[^\s)\]<>，。）]+>?", 0, ""),
    (r"\*\*|__|\*|`|~~", 0, ""),
    (r"^[ \t\]\[)(|:-]++|(?<![ \t\[(|])[ \t\[(|]++$", re.M, ""),
)]


def clean_markdown(text: str) -> str:
    """Reader-proxy markdown down to prose, keeping link anchor text."""
    text = html_mod.unescape(text or "")
    text = _MD_IMG.sub("", text)
    for _ in range(3):                         # nested [a](b) inside [c](d)
        text = _MD_LINK.sub(r"\1", text)
    for rx, rep in _MD:
        text = rx.sub(rep, text)
    return sanitize_markup(text)


_PTT = [(re.compile(p, f), r) for p, f, r in (
    (r"^[ \t]*+(?:作者|標題|時間|看板)[ \t:：].*$", re.M, ""),
    (r"^[ \t]*+※[ \t]*+(?:發信站|文章網址|編輯[ \t]*+[:：]|伸謝)[^\n]*$", re.M, ""),
    (r"(?:^|(?<=[\s。！？]))(?:推|噓|嘘|→)[ \t]*+[A-Za-z0-9_]{2,20}[ \t]*+[:：][ \t]*+", 0, "\n"),
    # ip + date + time on every push line reads as fact-dense to the scorer.
    # (?<!\s) + possessive: a run of spaces is scanned once, not once per space.
    (r"(?<!\s)\s*+(?:\d{1,3}(?:\.\d{1,3}){3})?\s*+\d{2}/\d{2}\s++\d{2}:\d{2}\s*+", 0, "\n"),
    (r"(?<![\s\d.])\s*+\d{1,3}(?:\.\d{1,3}){3}\s*+", 0, " "),
    (r"[ \t]{2,}", 0, " "),
    (r"\n{2,}", 0, "\n"),
)]


def clean_ptt(text: str) -> str:
    for rx, rep in _PTT:
        text = rx.sub(rep, text)
    return text.strip()


def classify(body: str, meta: str, table: bool, code: bool) -> Fetched:
    """A real body, or only a blurb (marks only apply to a real body)."""
    if len(body) >= MIN_BODY or (body and re.search(r"[。！？.!?]", body)
                                 and len(body) >= max(80, len(meta))):
        return Fetched(body, "body", table, code)
    if meta or body:
        return Fetched(meta or body, "meta")
    return Fetched(None, None)


# ---- network ----------------------------------------------------------------

def http_get(url: str, timeout: int, impersonate=False) -> tuple[str | None, str]:
    """(html, status) with status ok | blocked | notfound | fail."""
    r = net.get(url, timeout, impersonate=impersonate, lang=net.PAGE_LANG, retries=RETRIES)
    if r is None:
        return None, "fail"
    if r.status in net.BLOCK_STATUS:
        return None, "blocked"
    if r.status in (404, 410):
        return None, "notfound"
    return (r.text, "ok") if r.status < 400 else (None, "fail")


def via_reader(url: str) -> str | None:
    if "reader" not in store.CFG.fallbacks:
        return None
    text, status = http_get(READER.rstrip("/") + "/" + url, SLOW_ARTICLE_TIMEOUT)
    if status != "ok" or not text:
        return None
    text = textproc.fix_mojibake(text)
    if "Markdown Content:" in text[:1000]:           # drop the reader's preamble
        text = text.split("Markdown Content:", 1)[1]
    text = clean_markdown(text)
    return None if is_junk(text) or textproc.garbled(text) else text or None


def via_wayback(url: str) -> str | None:
    if "wayback" not in store.CFG.fallbacks:
        return None
    r = net.get("https://archive.org/wayback/available?url=" + quote(url, safe=""), ARTICLE_TIMEOUT,
                   headers=net.API_HEADERS, retries=RETRIES)
    if r is None or r.status != 200:
        return None
    try:
        snap = (r.json().get("archived_snapshots") or {}).get("closest") or {}
    except Exception:
        return None
    snap_url = str(snap.get("url") or "")
    if not snap.get("available") or not host_in(snap_url, ("archive.org",)):
        return None
    # id_ = the original bytes, without the archive's banner
    html, status = http_get(re.sub(r"(/web/\d+)/", r"\1id_/", snap_url, count=1), SLOW_ARTICLE_TIMEOUT)
    return html if status == "ok" else None


def fetch_article(url: str, feed_text: str = "", found: dict | None = None) -> Fetched:
    """Try every strategy, cheapest first. `found["thumbnail"]` is filled from
    any page HTML seen on the way."""
    found = {} if found is None else found
    slow = host_in(url, site("slow_hosts"))
    best = Fetched(None, None)
    blocked = notfound = False

    def consider(html: str) -> Fetched | None:
        nonlocal best, blocked
        if not found.get("thumbnail"):
            found["thumbnail"] = thumb_from_page(html, url)
        body, meta, table, code = from_html(html)
        if is_junk(body) or is_junk(meta) or (len(body) < MIN_BODY and is_junk(html[:20000])):
            blocked = True
            return None
        res = classify(body, meta, table, code)
        if res.kind == "body":
            return res
        if res.kind and not best.kind:
            best = res                          # keep the blurb, look for a body
        return None

    def done(res: Fetched, how: str = "") -> Fetched:
        if how:
            log(f"    recovered via {how}")
        if res.text and "ptt.cc" in url:
            text = clean_ptt(res.text)
            res = res._replace(text=text, kind="meta" if len(text) < MIN_BODY else res.kind)
        return res

    for imp in ([True] if slow else [False, True]):
        html, status = http_get(url, SLOW_ARTICLE_TIMEOUT if slow else ARTICLE_TIMEOUT, imp)
        if status == "blocked":
            blocked = True
        elif status == "notfound":
            notfound = True
            break
        elif html and (res := consider(html)):
            return done(res, "curl_cffi" if imp and not slow else "")

    feed_text = textproc.fix_mojibake(unescape(feed_text).strip())
    if textproc.garbled(feed_text):
        feed_text = ""
    if len(feed_text) >= MIN_BODY:
        return done(Fetched(feed_text, "body"), "feed content")
    if best.kind == "meta" and "reader-meta" not in store.CFG.fallbacks:
        return done(best)
    if "reader" in store.CFG.fallbacks:
        got = via_reader(url)
        ok = bool(got) and len(got) >= MIN_BODY
        log(f"    via=reader {'ok' if ok else 'miss'}")     # countable in the Actions log
        if ok:
            return done(Fetched(got, "body"), "reader proxy")
    if "wayback" in store.CFG.fallbacks:
        res = (html := via_wayback(url)) and consider(html)
        log(f"    via=wayback {'ok' if res else 'miss'}")
        if res:
            return done(res, "web archive")
    if feed_text:
        return done(Fetched(feed_text, "meta"), "feed content (short)")
    if best.kind:
        return done(best)
    if blocked:
        return Fetched(None, "blocked")
    if notfound:
        return Fetched(None, "gone")
    return Fetched(None, None)




# ============================================================== summary QC
# Quality check for stored summaries, with the fix each problem calls for.
#
# Categories:
#   garbled    binary / U+FFFD, UTF-8 read as Latin-1, CP1251/CP866 or
#                cp1252/cp1256, broken machine translation (lang.diagnose)
#   loop       one word repeated, or a 2-3 clause cycle
#   scrape     only site chrome (rules.json `qc_chrome`, plus common.JUNK_RE),
#                or leftover HTML tags. HTML inside a technical article
#                (code samples) is NOT flagged.
#   mismatch   different titles sharing one summary (reposts / retitled
#                copies of the same article are excluded)
#   spam       promotion only: top-up amounts, member recruiting, growth-studio
#                ads. News *about* gambling or adult industries is not spam.
#
# Fixes:
#   repair   rewrite in place: mojibake repaired, loops collapsed, tags stripped
#   refetch  delete summary -> pending (one more try from the source)
#   blank    BLANK_SUMMARY: spam, or a YouTube item whose local subtitle would
#            only rebuild the same text
#
# No state is stored. The retry is bounded by `gate`, which summarize runs
# on every new summary before writing it: a fresh summary with the same problem
# is repaired or written as BLANK, never stored, so backfill cannot reset the
# same item again (unless the rules themselves change).
#
# Run from summarize: every backfill applies it; report only with
# `summarize.py backfill --dry-run`, QC fixes alone with `backfill --qc-only`.
# No report file: problem titles and fix hints go to stdout (the Actions log),
# plus `::warning::` annotations when running on GitHub Actions.


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
def check_garbled(s: str) -> Finding | None:
    if not (d := textproc.diagnose(s)):
        return None
    what, at, fixed = d
    return Finding("garbled", what, snippet(s, at), "repair" if fixed else "refetch", fixed)


# ------------------------------------------------------------ 2 重複迴圈 ----
def check_loop(s: str) -> Finding | None:
    loops = textproc.find_loops(s)
    if not loops:
        return None
    start, _, unit, reps = max(loops, key=lambda lp: lp[1] - lp[0])
    fixed = textproc.collapse_loops(s)
    detail = f"\"{unit.strip()[:20]}\" repeated"
    if len(fixed.strip()) < 30 or len(fixed) < 0.4 * len(s):   # mostly loop: nothing to keep
        return Finding("loop", detail + ", mostly loop", snippet(s, start), "refetch")
    return Finding("loop", detail, snippet(s, start), "repair", fixed)


# ------------------------------------------------------------ 3 抓取失敗 ----
_HTML = re.compile(r"</?(?:div|span|script|style|iframe|table|tr|td|img|meta|link|svg|path|button|"
                   r"input|p|br|li|ul|ol|a|h[1-6]|section|article|nav|footer|header)\b[^>\n]{0,200}>"
                   r"|&nbsp;|&amp;|&lt;|&gt;|&#\d+;", re.I)
_HTML_STRONG = re.compile(r"width=device-width|name=[\"']?viewport|OptanonWrapper|crossorigin=|"
                          r"<meta\b|<script\b[^<>]{0,300}?\bsrc=|<link\b[^<>]{0,300}?\brel=|class=\"[\w\- :/\[\]]{20,}\"", re.I)


def check_scrape(s: str) -> Finding | None:
    hits = textproc.chrome_re().findall(s) + JUNK_RE.findall(s)
    if hits and (len(s) < 800 or len(hits) >= 3):
        m = textproc.chrome_re().search(s) or JUNK_RE.search(s)
        return Finding("scrape", f"site chrome only: \"{m.group(0)}\"",
                       snippet(s, m.start()), "refetch")
    tags = _HTML.findall(s)
    strong = _HTML_STRONG.search(s)
    if not (strong or len(tags) >= 5 or (tags and len(s) < 300)) or textproc.looks_like_code(s):
        return None
    m = strong or _HTML.search(s)
    body = s.rstrip()
    mark = FALLBACK_MARK if body.endswith(FALLBACK_MARK) else ""
    body = body[:-1].rstrip() if mark else body
    clean = re.sub(r"<[^>]{0,300}>", " ", body)
    clean = re.sub(r"&(?:nbsp|amp|lt|gt|#\d+);", " ", clean)
    clean = "\n".join(ln for ln in (re.sub(r"[ \t\r\f\v]+", " ", x).strip() for x in clean.split("\n")) if ln)
    detail = "leftover HTML"
    if len(clean) >= 80 and len(clean) >= 0.6 * len(body) and not _HTML_STRONG.search(clean):
        return Finding("scrape", detail + ", tags stripped", snippet(s, m.start()), "repair",
                       (clean + " " + mark).rstrip())
    return Finding("scrape", detail, snippet(s, m.start()), "refetch")


# ------------------------------------------------------------ 5 垃圾內容 ----
# Promotion signals, grouped by kind: one ad repeats its own kind ("儲值 300,
# 首儲" or "growth studio, SEO, AEO"), and so does a news story about that
# trade. A verdict needs signals of different kinds.
_PROMO_GROUPS = {
    "slot": [r"KEBAYA\w*", r"tebak skor", r"slot gacor", r"situs (?:judi|slot|resmi|terpercaya)"],
    "signup": [r"member aktif", r"deposit\s*(?:minimal|Rp)", r"daftar sekarang", r"link alternatif",
               r"bonus new member"],
    "agency": [r"growth studio", r"full-stack growth", r"SEO, AEO", r"成長工作室"],
    "topup": [r"代儲", r"儲值\s*\d", r"首儲", r"註冊送", r"加入會員[領送]", r"招募會員", r"top[\s-]?up\s*\$?\d"],
    "followers": [r"刷粉", r"buy followers"],
    "ghostwriting": [r"代寫論文"],
    "contact": [r"加賴", r"加\s*LINE"],
}
_PROMO = {g: re.compile("|".join(ps), re.I) for g, ps in _PROMO_GROUPS.items()}
# Auxiliary: rupiah amounts are everywhere in Indonesian news. Counted only
# next to a real promotion signal, never on its own.
_PROMO_AUX = re.compile(r"\bRp\s?\d", re.I)
_PROMO_TOPIC = re.compile(r"\bjudi\b|togel|taruhan|bandar|casino|betting|sportsbook|博彩|娛樂城|百家樂|"
                          r"線上賭|老虎機|porn|onlyfans|escort|成人影片", re.I)
# Reporting voice: a story *about* promotions quotes the same words. With it,
# only signals of three different kinds still make a verdict.
_NEWS_VOICE = re.compile(r"表示|指出|據悉|報導|报道|記者|记者|警方|調查|调查|官員|官员|發言人|发言人"
                         r"|\b(?:said|says|reported|according to|police|officials?|spokesperson|investigat\w+)\b", re.I)
PROMO_KINDS, PROMO_KINDS_NEWS = 2, 3


def check_spam(s: str, title: str) -> Finding | None:
    text = s + " " + title
    hits = {g: sorted({m.group(0).lower() for m in rx.finditer(text)}) for g, rx in _PROMO.items()}
    hits = {g: h for g, h in hits.items() if h}
    if not hits:
        return None
    kinds = len(hits)
    aux = sorted({m.group(0).lower() for m in _PROMO_AUX.finditer(text)})
    if aux:
        kinds += 1
    if _NEWS_VOICE.search(text):
        spam = kinds >= PROMO_KINDS_NEWS
    else:
        spam = kinds >= PROMO_KINDS or bool(_PROMO_TOPIC.search(text))
    if not spam:
        return None
    words = [w for h in hits.values() for w in h] + aux
    return Finding("spam", "promotion: " + ", ".join(words)[:60], snippet(s), "blank")


# ------------------------------------------------------------ 3 標題不符 ----
RETITLE_OVERLAP = 0.3         # title keyword overlap that marks a retitled copy / repost
_BAD_URL = re.compile(r"/(?:undefined|null|NaN|None)(?:/|$|\?)", re.I)
_STOP_EN = set("this that with from have will what when your about into they them their there "
               "were been more than also just like".split())


def keywords(text: str) -> set:
    text = textproc.key_text(text or "").lower()
    out = set(re.findall(r"[a-z][a-z0-9]{3,}", text)) - _STOP_EN
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        out.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return out | set(re.findall(r"\d{2,}", text))


def _url_path(u: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", (u or "").lower()).split("?")[0].rstrip("/")
    return u.split("/", 1)[-1] if "/" in u else u


def _share_key(s: str) -> str:
    return re.sub(r"\s+", "", s.rstrip(FALLBACK_MARK))[:300]


def _indexed_key(it: dict) -> str | None:
    """The share key of a real summary long enough to compare, else None."""
    s = it.get("summary") or ""
    if s.strip() and s != GONE_SUMMARY and not s.startswith(PLACEHOLDER_PREFIX) and len(k := _share_key(s)) >= 60:
        return k
    return None


def check_shared(items: list) -> dict[int, Finding]:
    """id(item) -> Finding for items whose summary is shared across different titles."""
    out = {}
    for key, g in shared_index(items).items():
        if len(g) < 2 or len({re.sub(r"\W", "", (it.get("title") or "").lower())[:40] for it in g}) < 2:
            continue
        broken = [it for it in g if _BAD_URL.search(it.get("url") or "")]
        paths = [_url_path(it.get("url")) for it in g]
        if not broken and all(difflib.SequenceMatcher(None, paths[0], p).ratio() >= 0.6 for p in paths[1:]):
            continue                                    # same article under several urls
        tks = [keywords(it.get("title")) for it in g]
        if not broken and tks[0] and all(t and len(tks[0] & t) / min(len(tks[0]), len(t)) >= RETITLE_OVERLAP
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
def qc_inspect(it: dict) -> Finding | None:
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


def qc_apply(items: list, fix: bool = True) -> list:
    """Check every stored summary; when `fix`, change items in place.
    Returns [(item, finding, action)]."""
    shared = check_shared(items)
    rows = []
    for it in items:
        f = qc_inspect(it) or shared.get(id(it))
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


def shared_index(items: list) -> dict:
    """share key -> items holding a summary with that key (what `gate` compares against)."""
    idx = defaultdict(list)
    for it in items:
        index_add(idx, it)
    return idx


def index_add(idx: dict, it: dict) -> None:
    """Keep `idx` current after `it` gets a new summary."""
    if k := _indexed_key(it):
        idx[k].append(it)


def qc_gate(it: dict, summary: str, index: dict) -> tuple[str, Finding | None]:
    """The summary to actually write for `it`. A new summary that fails QC is
    repaired, or becomes BLANK: it came fresh from the source, so another try
    would return the same thing. `index` is shared_index(items): one lookup
    instead of a scan of the whole archive per item."""
    if not summary or not summary.strip() or summary == GONE_SUMMARY:
        return summary, None
    probe = {**it, "summary": summary}
    f = qc_inspect(probe)
    if not f and len(key := _share_key(summary)) >= 60:
        peers = [x for x in index.get(key, ()) if x is not it]
        f = check_shared(peers + [probe]).get(id(probe)) if peers else None
    if not f:
        return summary, None
    return (f.fixed, f) if f.action == "repair" else (BLANK_SUMMARY, f)


# ---------------------------------------------------------------- hints -----
CATEGORIES = ("garbled", "loop", "scrape", "mismatch", "spam")
HINTS = {
    "garbled": "if one site keeps showing up, check its Content-Type / <meta charset>, add a byte sample "
               "to selftest.check_encoding, then fix net.decode_body; broken translation usually "
               "means the source itself was mojibake.",
    "loop": "collapsed in place, no retranslation needed. If one channel keeps looping, try a larger "
            "Whisper model or beam_size>1 for it.",
    "scrape": "for repeated chrome on one site, mine a rule with `summarize.py mine 3` "
              "into rules.json `boilerplate`; add bot-walled sites to `sites.slow_hosts`; paywalled sites "
              "whose feed has full text go in `sites.feed_first_hosts`.",
    "mismatch": "check whether the feed's links all point to one page (canonical_url, redirects) or "
                "feed_content is filled in twice.",
    "spam": "if one feed keeps producing it, remove the feed from the OPML; already blanked.",
}


def qc_report(rows: list, out=print) -> None:
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
            # titles are remote text: escape them so they cannot end or start a workflow command
            titles = titles.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace("::", ": :")
            print(f"::warning title=summary QC {cat}::{titles}")



# ============================================================== run
SLEEP = 1.5                   # polite gap between two requests to one host
HOST_WORKERS = 6              # hosts fetched at the same time
SAVE_EVERY = 30.0             # checkpoint interval (s); the run always saves at the end
BATCH, MAX_FAIL_STREAK = 25, 4
# rules.json tables, read where they are used:
#   skip_hosts        feed copy is redirect debris, page is useless
#   pause_domains     same skeleton page on every subdomain: paused as one
#   feed_first_hosts  summarised from the feed copy without fetching the page,
#                     when one exists; their feed images (feed_images) supply
#                     the thumbnail. YouTube is routed before this and collect
#                     stores no feed copy for it.
# Douban marks (想看 / 看过 ...): the page is a login skeleton, so the feed copy
# is all there is. Its 备注 is the user's own note; ratings and tags are not.
DOUBAN_MARK_RE = re.compile(r"^(?:想[看读听讀聽]|[看读听讀聽][过過])")
DOUBAN_DROP_RE = re.compile(r"^\s*(?:推荐|推薦|评分|評分|标签|標籤|tags?)\s*[:：]", re.I)
DOUBAN_LABEL_RE = re.compile(r"^\s*(?:备注|備註|短评|短評)\s*[:：]\s*")

OFFLINE = ("douban", "feed", "youtube", "podcast")      # never fetched, never metered


def _ts(value) -> float:
    return d.timestamp() if (d := parse_date(value)) else float("-inf")


def pick_subtitle(item_id: str) -> Path | None:
    return store.pick_subtitle(store.CFG.subtitles, item_id)


def route(it) -> str | None:
    url = it["url"]
    if host_in(url, site("skip_hosts")):
        return None
    if is_youtube(url):
        return "youtube"
    if store.is_episode(url):
        # Transcript only: summarising the show notes would end the item
        # before ASR runs. No fallback; media retries every run.
        return "podcast" if pick_subtitle(it.get("id", "")) else None
    if host_in(url, ("douban.com",)) and DOUBAN_MARK_RE.match((it.get("title") or "").strip()):
        # The note exists only in the feed copy. No copy in this process
        # (media's run, a later run): wait, because writing BLANK now
        # would lose a note the next collect can deliver.
        return "douban" if "feed_content" in it else None
    if host_in(url, site("feed_first_hosts")) and ((it.get("feed_content") or "").strip() or it.get("feed_images")):
        return "feed"
    return "fetch"


def pause_key(url: str) -> str:
    return next((d for d in site("pause_domains") if host_in(url, (d,))), host_of(url))


def douban_note(feed: str) -> str:
    """The user's own words in a douban mark's feed copy: ratings/tags dropped, labels stripped."""
    lines = (DOUBAN_LABEL_RE.sub("", ln).strip() for ln in feed.splitlines() if not DOUBAN_DROP_RE.match(ln))
    return "\n".join(ln for ln in lines if ln)


# Lines logged on a worker thread are collected and printed by the main thread
# with the item's result, so parallel fetches still log one item at a time.
_tls = threading.local()


def log(line: str) -> None:
    buf = getattr(_tls, "buf", None)
    if buf is None:
        print(line)
    else:
        buf.append(line)


def _fetch_one(it, translate: bool):
    """Worker: the page (or a fallback) and its summary. No archive writes here."""
    _tls.buf = []
    try:
        found = {}
        f = fetch_article(it["url"], it.get("feed_content") or "", found)
        s = textproc.build(f.text, "page", f.table, f.code, f.kind == "meta", translate=translate) if f.text else ""
        return f, s, found.get("thumbnail"), "".join(x + "\n" for x in _tls.buf)
    finally:
        _tls.buf = None


class Run:
    def __init__(self, path, doc, max_items: int, deadline: float | None, offline_only=False, translate=None):
        self.path, self.doc, self.max_items, self.deadline = path, doc, max_items, deadline
        self.offline_only = offline_only
        self.translate = store.CFG.translate if translate is None else translate
        self.n = Counter()
        self.paused: dict[str, int] = {}          # pause key -> items skipped since
        self.index = shared_index(doc["items"])
        self.dirty, self.saved_at = False, time.monotonic()

    # ---- bookkeeping ----
    def touch(self):
        """Checkpoint at most every SAVE_EVERY seconds (each save rewrites the
        whole archive); save() at the end of the run always writes."""
        self.dirty = True
        if time.monotonic() - self.saved_at >= SAVE_EVERY:
            self.save()

    def save(self):
        if self.dirty:
            save_doc(self.path, self.doc)
            self.dirty, self.saved_at = False, time.monotonic()

    def done(self, it, summary, how):
        summary, f = qc_gate(it, summary, self.index)
        if f:
            how += f"; QC {f.category} ({f.detail}) -> " + ("repaired" if summary.strip() else "blank")
        it["summary"] = summary
        index_add(self.index, it)
        drop_scratch(it)
        self.n["ok"] += 1
        print(f"    ok ({how})")
        self.touch()

    def feed_thumb(self, it) -> bool:
        """Thumbnail from the feed's own image urls; True when the item has one."""
        if not it.get("thumbnail") and (t := thumb_pick(it.get("feed_images"), it["url"], "feed")):
            it["thumbnail"] = t
            self.touch()
        return bool(it.get("thumbnail"))

    def timed_out(self) -> bool:
        if self.deadline and time.monotonic() > self.deadline:
            if not self.n["time_cut"]:
                self.n["time_cut"] = 1
                print("[SUMMARIZE] time budget reached; the rest stay pending")
            return True
        return False

    # ---- main loop ----
    def process(self, pending: list):
        routed = [(r, it) for it in pending if (r := route(it))]
        offline = [x for x in routed if x[0] in OFFLINE]
        network = [] if self.offline_only else [it for r, it in routed if r not in OFFLINE]
        print(f"[SUMMARIZE] pending={len(pending)}: offline={len(offline)} network={len(network)} "
              f"(fetch cap {self.max_items})")
        for r, it in offline:
            if self.timed_out():
                return
            if r != "youtube":                # printed only once a subtitle exists
                self.head(it, r)
            try:
                getattr(self, "do_" + r)(it)
            except Exception as e:           # one bad item must not end the run
                self.n["failed"] += 1
                print(f"    error ({type(e).__name__}: {e}), kept pending")
        if network:
            self.fetch_all(network)

    def fetch_all(self, queue: list):
        """The newest max_items items (queue is newest first), grouped by host:
        one task per host fetches its items in order with SLEEP between them,
        HOST_WORKERS hosts at a time. A refusal ends that host's task (the host is
        paused for this run); the time budget ends every task. Results are
        written on this thread only, as they arrive."""
        hosts: dict[str, list] = {}
        for it in queue[:max(self.max_items, 0)]:
            hosts.setdefault(pause_key(it["url"]), []).append(it)
        results: SimpleQueue = SimpleQueue()

        def host_task(key, items):
            try:
                for k, it in enumerate(items):
                    if self.deadline and time.monotonic() > self.deadline:
                        results.put(("late", key, len(items) - k))
                        return
                    if k:
                        time.sleep(SLEEP)
                    try:
                        res = _fetch_one(it, self.translate)
                    except Exception as e:          # reported on the main thread
                        res = e
                    results.put(("item", it, res))
                    if not isinstance(res, Exception) and res[0].kind == "blocked":
                        results.put(("paused", key, len(items) - k - 1))
                        return
            finally:
                results.put(("done", key, 0))

        with ThreadPoolExecutor(max_workers=HOST_WORKERS) as ex:
            for key, items in hosts.items():
                ex.submit(host_task, key, items)
            left = len(hosts)
            while left:
                kind, a, b = results.get()
                if kind == "done":
                    left -= 1
                elif kind == "paused":
                    self.paused[a] = self.paused.get(a, 0) + b
                elif kind == "late":
                    self.timed_out()
                else:
                    self.n["attempted"] += 1
                    self.head(a, "fetch")
                    try:
                        if isinstance(b, Exception):
                            raise b
                        f, summary, thumb, log = b
                        print(log, end="")
                        self.apply_fetch(a, f, summary, thumb)
                    except Exception as e:
                        self.n["failed"] += 1
                        print(f"    error ({type(e).__name__}: {e}), kept pending")

    @staticmethod
    def head(it, r):
        print(f"[{r}] ({it.get('published_at') or 'no date'}) {(it.get('title') or '')[:60]}\n"
              f"    {it['url']}")

    # ---- offline handlers ----
    def do_douban(self, it):
        """想X and X过 alike: the note from the feed copy. Never fetched.
        route() sends only items with a copy here, so a missing note is final."""
        note = douban_note(it["feed_content"])
        self.feed_outcome(it, textproc.build(note, "feed", translate=self.translate) if note else "",
                          f"douban mark, feed note {len(note)} chars", "feed note",
                          "douban mark, no note in feed copy, blank")

    def do_feed(self, it):
        """Feed copy only, never the page: text -> summary, else blank.
        The thumbnail comes from feed_images either way."""
        self.feed_thumb(it)
        text = (it.get("feed_content") or "").strip()
        self.feed_outcome(it, textproc.build(text, "feed", meta=len(text) < MIN_BODY, translate=self.translate)
                          if text else "", f"feed, {len(text)} chars", "feed copy", "no text in feed copy, blank")

    def feed_outcome(self, it, s, ok: str, what: str, blank: str):
        """A summary of a feed copy; None (undecodable) waits for a fresh copy;
        "" (nothing but ratings, tags, boilerplate or images) is final."""
        if s:
            self.done(it, s, ok)
        elif s is None:
            drop_scratch(it)
            self.n["failed"] += 1
            print(f"    undecodable {what} -> kept pending")
        else:
            self.done(it, BLANK_SUMMARY, blank)

    def do_youtube(self, it):
        if not it.get("thumbnail"):
            m = re.search(r"(?:[?&]v=|/shorts/|/live/|youtu\.be/)([\w-]{6,20})(?![\w-])", it["url"])
            if m:
                it["thumbnail"] = f"https://img.youtube.com/vi/{m.group(1)}/mqdefault.jpg"
                self.touch()
        if pick_subtitle(it.get("id", "")):          # else media.py hasn't fetched one yet
            self.head(it, "youtube")
            self.do_podcast(it)

    def do_podcast(self, it):
        """A local transcript (YouTube subtitle or podcast ASR) -> summary. The
        file will not change, so a transcript with nothing usable (only
        boilerplate, or undecodable) ends the item instead of being retried."""
        path = pick_subtitle(it.get("id", ""))
        text = textproc.vtt_to_text(path)
        if len(text) < textproc.MIN_CAPTION_CHARS:
            return self.done(it, BLANK_SUMMARY, f"{path.name}: no speech, blank")
        if s := textproc.build(text, "subtitle", translate=self.translate):
            return self.done(it, s, f"{path.name}, {len(text)} chars")
        self.done(it, BLANK_SUMMARY, f"{path.name}: " + ("undecodable" if s is None else "only boilerplate")
                  + ", blank")

    # ---- network results ----
    def apply_fetch(self, it, f, s, thumb):
        if not it.get("thumbnail") and thumb:
            it["thumbnail"] = thumb
            self.touch()
        self.feed_thumb(it)                  # page had none: the feed's images may
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


def backfill(items, *, translate=True, deadline=None, save=None, limit=0, qc_only=False,
             rules=True) -> Counter:
    """Bring stored items up to the current rules: QC fixes, thumbnails, 簡體,
    language. rules=False: the rules have not changed since the last full
    pass (store.rules_hash), so only the translations still missing are retried."""
    n, targets = Counter(), []
    if not rules:
        targets = [it for it in items if textproc.needs_translation(it.get("summary"))]
        items = ()
        print("[BACKFILL] rules unchanged since the last full pass; translations only")
    else:
        qc_report(qc_apply(items, fix=True))   # mojibake, loops, chrome, mismatch, spam
    if qc_only:
        return n
    for it in items:
        if it.get("thumbnail") and (c := thumb_clean(it["thumbnail"])) != it["thumbnail"]:
            it["thumbnail"] = c
            n["thumb: processing params stripped"] += 1
        if it.get("thumbnail"):
            ok, why = thumb_still_valid(it)
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
        if textproc.variant(s) == "hans" and (t := textproc.to_twp(s)) != s:
            it["summary"] = s = t
            n["simplified"] += 1
        if textproc.needs_translation(s):
            targets.append(it)
    targets = targets[:limit] if limit else targets
    print(f"[BACKFILL] thumbnails dropped={n['thumbnail']}, boilerplate={n['boilerplate']}, "
          f"simplified={n['simplified']}, "
          f"non-Chinese={len(targets)}"
          + "".join(f"\n  {k}×{v}" for k, v in n.items() if k.startswith("thumb: ")))
    if not (targets and translate):
        return n
    print("[BACKFILL] DeepL " + ("on" + (" (%s/%s chars used)" % u if (u := textproc.deepl_usage()) else "")
                                if textproc.deepl_active() else "not configured (DEEPL_API_KEY) -- gtx only"))
    reasons, streak = Counter(), 0
    for pos in range(0, len(targets), BATCH):
        if deadline and time.monotonic() > deadline:
            print(f"[BACKFILL] time budget reached after {pos}")
            break
        batch = targets[pos:pos + BATCH]
        marks = [FALLBACK_MARK if it["summary"].rstrip().endswith(FALLBACK_MARK) else "" for it in batch]
        bodies = [it["summary"].rstrip().rstrip(FALLBACK_MARK).strip() for it in batch]
        for it, mark, (out, why) in zip(batch, marks, textproc.translate_keep_lines(
                bodies, stop_after=MAX_FAIL_STREAK - streak)):
            if out:
                it["summary"] = (textproc.to_twp(out) + " " + mark).rstrip()
                n["translated"] += 1
                streak = 0
            else:
                n["failed"] += 1
                reasons[textproc.short_reason(why) or "unknown"] += 1
                streak += 1
        if save and n["translated"]:
            save()
        if streak >= MAX_FAIL_STREAK:
            print(f"[BACKFILL] {streak} consecutive failures; rest left for a later run")
            break
        time.sleep(SLEEP)
    print(f"[BACKFILL] translated={n['translated']} failed={n['failed']} "
          f"providers={dict(textproc.PROVIDERS)}"
          + (f"\n  reasons: {dict(reasons.most_common(5))}" if reasons else "")
          + (f"\n  DeepL failures (fell back to gtx): {dict(textproc.FAILURES)}" if textproc.FAILURES else ""))
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


def run(path, doc, max_items, budget=0, *, offline_only=False, do_backfill=True, translate=None) -> Run:
    """Summarise pending items of `doc` (feed copies already in memory when
    called from collect), then backfill. Fetching stops at half the budget.
    offline_only (media): local subtitles and feed copies only, no
    fetches, no backfill. The archive is always saved at the end, even on an
    exception."""
    translate = store.CFG.translate if translate is None else translate
    # Compile rules.json here, on the main thread: a bad rule's WARNING then
    # prints once, in order, instead of from inside a worker's buffered log.
    textproc.boiler_rules()
    textproc.chrome_re()
    items, start = doc["items"], time.monotonic()
    pending = sorted(filter(is_pending, items), key=lambda it: _ts(it.get("published_at")), reverse=True)
    r = Run(path, doc, max_items, start + budget * 0.5 if budget else None,
            offline_only=offline_only, translate=translate)
    try:
        r.process(pending)
        print(f"[SUMMARIZE] done {dict(r.n)}; paused hosts: {dict(r.paused) or '-'}")
        if textproc.STATS:
            print("[SUMMARIZE] stats " + ", ".join(f"{k}×{v}" for k, v in sorted(textproc.STATS.items())))
        if do_backfill and not offline_only:
            current = store.rules_hash()
            try:
                backfill(items, translate=translate, deadline=start + budget if budget else None,
                         save=lambda: save_doc(path, doc), rules=doc.get("rules_hash") != current)
                doc["rules_hash"] = current        # only after a pass that finished
            except Exception as e:
                print(f"ERROR: backfill aborted ({type(e).__name__}: {e})")
    finally:
        save_doc(path, doc)                    # save_doc never writes scratch fields
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("backfill", help="re-apply current rules to stored summaries")
    how = bf.add_mutually_exclusive_group()
    how.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    how.add_argument("--qc-only", action="store_true", help="summary QC fixes only")
    bf.add_argument("--limit", type=int, default=0, help="translate at most N")
    bf.add_argument("--no-translate", dest="translate", action="store_false", default=None,
                    help="QC and boilerplate only, no translation (default: the TRANSLATE env var)")
    mn = sub.add_parser("mine", help="candidate drop_unit rules from the corpus")
    mn.add_argument("min_count", type=int, metavar="MIN_COUNT")
    a = ap.parse_args(argv)

    cfg = store.configure()
    path = cfg.archive
    if not path.exists():
        print(f"ERROR: {path} not found (DATA_DIR={cfg.data_dir}).", file=sys.stderr)
        return 1
    doc = load_doc(path)
    items = doc["items"]
    if a.cmd == "mine":
        mine_boilerplate(items, a.min_count)
        return 0
    if a.dry_run:
        qc_report(qc_apply(items, fix=False))
        t = [it for it in items if textproc.needs_translation(it.get("summary"))]
        print(f"would translate {len(t)}")
        for it in t[:5]:
            print(f"  {it['url'][:70]}  {it['summary'][:60]}")
        return 0
    save = lambda: save_doc(path, doc)
    backfill(items, translate=cfg.translate if a.translate is None else a.translate,
             save=save, limit=a.limit, qc_only=a.qc_only)
    if not (a.qc_only or a.limit):          # a full pass: later runs can skip it
        doc["rules_hash"] = store.rules_hash()
    save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
