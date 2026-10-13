#!/usr/bin/env python3
"""Collect items from OPML feeds (plus Telegram/Jike bridges)
into data/archive.json: canonical urls, stable ids, dedupe, retention."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import lzma
import os
import re
import sys
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
from bs4 import BeautifulSoup
import common
import thumbs
from common import UA, UTC, host_in, host_of, iso, load_doc, make_session, next_data, parse_date, save_doc, walk

CONNECT_TIMEOUT, READ_TIMEOUT = 5, 12     # a dead host fails the handshake fast
WORKERS = 32
LANG = "zh-CN,zh;q=0.9,en;q=0.8"
BLOCK_STATUS = {401, 403, 429, 451, 503}  # retried once with a browser fingerprint
FEED_CONTENT_MAX = 60000                  # feed copy kept for summarize_feed
FEED_CONTENT_SKIP = ("youtube.com", "youtu.be", "soundon.fm", "firstory.me", "xiaoyuzhoufm.com")
TRACKING = {"ref", "spm", "fbclid", "gclid", "igshid", "mkt_tok", "mc_cid", "mc_eid", "_hsenc", "_hsmi"}

FEED_REPLACE = {
    "https://rsshub.app/infoq/recommend": "https://www.infoq.cn/feed",
    "https://rsshub.app/huggingface/blog-zh": "https://huggingface.co/blog/feed.xml",
    "https://rsshub.app/readhub/daily": "https://readhub.cn/rss",
    "https://rsshub.app/36kr/hot-list": "https://36kr.com/feed",
    "https://rsshub.app/sspai/index": "https://sspai.com/feed",
    "https://rsshub.app/sspai/matrix": "https://sspai.com/feed",
    "https://rsshub.app/meituan/tech": "https://tech.meituan.com/feed",
    "https://mjg59.dreamwidth.org/data/rss": "http://mjg59.dreamwidth.org/data/rss",
}
FEED_SKIP_PREFIX = (
    "https://rsshub.app/telegram/channel/", "https://rsshub.app/jike/",
    "https://rsshub.app/bilibili/", "https://rsshub.app/zhihu/",
    "https://rsshub.app/xiaoyuzhou/podcast/", "https://rsshub.app/xyzrank",
    "https://rsshub.app/mittrchina/hot", "https://wechat2rss.bestblogs.dev/",
    "https://werss.bestblogs.dev/", "http://47.122.94.119:18080/",
)
FEED_SKIP = {"https://rachelbythebay.com/w/atom.xml", "https://flak.tedunangst.com/rss"}
# Feeds publishing article links on a dev origin (http://localhost:8000/...).
# The explicit table wins over the feed's own origin, which can be a proxy.
ORIGIN_FIXUPS = {"notesbylex.com": "https://notesbylex.com"}
DEV_ORIGIN = re.compile(r"^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|[^.]+\.local)(?::\d+)?$", re.I)
# Same Firstory episode under two hosts; one spelling, so one id.
HOST_ALIASES = {"open.firstory.me": "open.firstory.fm"}
VOCUS_AUTHOR = re.compile(r"^(https?://(?:www\.)?vocus\.cc)/@[^/]*/([0-9A-Za-z]+)(.*)$")
TG_POST = re.compile(r"[A-Za-z0-9_]{1,64}/\d{1,12}")
JIKE_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")
BESTBLOGS = "https://www.bestblogs.dev"
BESTBLOGS_ISSUE = re.compile(r"^https?://(?:www\.)?bestblogs\.dev(?:/[a-z]{2})?/newsletter/issue(\d{1,4})/?$", re.I)
BESTBLOGS_MAX, BESTBLOGS_MISSES, BESTBLOGS_BACKLOG = 2000, 3, 30
# The Paper (澎湃) channel pages: Next.js, no RSS. Scraped like BestBlogs.
THEPAPER = "https://www.thepaper.cn"
THEPAPER_CHANNELS = {"channel_168153": ("opmlrss", "澎湃·上海书评")}   # path -> (category, source)
THEPAPER_ARTICLE = re.compile(r"^(?:https?://(?:www|m)\.thepaper\.cn)?/newsDetail_forward_(\d{5,12})(?:[/?#]|$)", re.I)
CN_REL_TIME = re.compile(r"(\d{1,3})\s*(分钟|小时|天)前")


@dataclass
class Raw:
    category: str
    source: str
    title: str
    url: str
    published_at: datetime | None
    feed_url: str = ""
    content: str = ""
    images: tuple = ()          # image urls from the feed entry (html <img>, media:*, enclosures)


# ---- identity -----------------------------------------------------------------

def normalize_url(raw: str) -> str:
    """Tracking params stripped. Feeds item ids: changing it forks the archive."""
    raw = (raw or "").strip()
    try:
        p = urlparse(raw)
        if not p.scheme:
            return raw
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
        return urlunparse(p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(),
                                     fragment="", query=urlencode(q, doseq=True))).rstrip("/")
    except Exception:
        return raw


def canonical_url(raw: str, source: str = "", feed_url: str = "") -> str:
    url = normalize_url(raw)
    p = urlparse(url)
    if (alias := HOST_ALIASES.get(p.netloc.lower())):
        p = p._replace(netloc=alias)
        url = urlunparse(p)
    if p.netloc and DEV_ORIGIN.match(p.netloc):
        f = urlparse(feed_url or "")
        origin = ORIGIN_FIXUPS.get(source.strip().casefold()) or (
            f"{f.scheme}://{f.netloc}" if f.scheme in ("http", "https") and f.netloc
            and not DEV_ORIGIN.match(f.netloc) else "")
        if origin:            # never guess: a wrong host files articles under another site
            o = urlparse(origin)
            return urlunparse(p._replace(scheme=o.scheme, netloc=o.netloc)).rstrip("/")
        return url
    if m := VOCUS_AUTHOR.match(url):
        return f"{m[1]}/article/{m[2]}{m[3]}"
    if m := BESTBLOGS_ISSUE.match(url):        # one spelling per issue, any language
        return f"{BESTBLOGS}/newsletter/issue{int(m[1])}"
    return url


def make_id(category: str, source: str, title: str, url: str) -> str:
    """Unchanged formula (the first field used to be called site_id): ids also
    name subtitle files, so a different hash would orphan them all."""
    key = "||".join([category.strip().lower(), source.strip().lower(),
                     title.strip().lower(), normalize_url(url)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# ---- feed parsing -------------------------------------------------------------
_BLOCKS = ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section")


def html_to_text(raw: str) -> str:
    """Feed html to text: scripts, styles and multi-line code dropped."""
    soup = BeautifulSoup(raw or "", "html.parser")
    for t in soup(["script", "style", "pre"]):
        t.decompose()
    for t in soup("code"):
        if "\n" in t.get_text():
            t.decompose()
    for t in soup("br"):
        t.replace_with("\n")
    for t in soup(_BLOCKS):
        t.append("\n")
    text = soup.get_text().replace("\u00a0", " ")
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def entry_content(entry, link: str) -> str:
    """Longest body-ish payload of a feed entry, so a page that later refuses
    to be fetched can still be summarised."""
    if host_in(link, FEED_CONTENT_SKIP):
        return ""
    cands = [str(b.get("value") or "") for b in entry.get("content") or [] if isinstance(b, dict)]
    cands += [str(entry[k]) for k in ("summary", "description", "subtitle") if entry.get(k)]
    return html_to_text(max(cands, key=len))[:FEED_CONTENT_MAX] if cands else ""


FEED_IMAGES_MAX = 12


def entry_images(entry, link: str) -> tuple:
    """Image urls a feed entry carries, best first: <img>s in its html (the
    article's own images), then media:content / media:thumbnail / image
    enclosures (often small renditions). Plain-text feed_content loses these,
    so they are kept beside it for the thumbnail."""
    if host_in(link, FEED_CONTENT_SKIP):
        return ()
    html = [str(b.get("value") or "") for b in entry.get("content") or [] if isinstance(b, dict)]
    html += [str(entry[k]) for k in ("summary", "description") if entry.get(k)]
    urls = [u for h in sorted(html, key=len, reverse=True) for u in thumbs.candidates(h, link)]
    for key in ("media_content", "media_thumbnail"):
        urls += [str(m.get("url") or "") for m in entry.get(key) or [] if isinstance(m, dict)
                 if str(m.get("medium") or m.get("type") or "image").startswith("image")]
    urls += [str(l.get("href") or "") for l in entry.get("links") or [] if isinstance(l, dict)
             and l.get("rel") == "enclosure" and str(l.get("type") or "").startswith("image/")]
    out = []
    for u in urls:
        u = u.strip()
        if u.lower().startswith(("http://", "https://")) and len(u) <= 2048 and u not in out:
            out.append(u)
    return tuple(out[:FEED_IMAGES_MAX])


def compact(text: str, limit: int = 96) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def parse_telegram(html: str, feed: dict) -> list[Raw]:
    out = []
    for msg in BeautifulSoup(html, "html.parser").select(".tgme_widget_message"):
        post = str(msg.get("data-post") or "").strip()
        node = msg.select_one(".tgme_widget_message_text") or \
            msg.select_one(".tgme_widget_message_link_preview_title")
        text = node.get_text(" ", strip=True) if node else ""
        t = msg.select_one("time[datetime]")
        when = parse_date(t.get("datetime")) if t else None
        if TG_POST.fullmatch(post) and text and when:     # attribute text becomes a url path
            out.append(Raw(feed["category"], feed["title"], compact(text), f"https://t.me/{post}", when))
    return out


def parse_jike(html: str, feed: dict) -> list[Raw]:
    script = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    try:
        posts = json.loads(script.string)["props"]["pageProps"].get("posts") or []
    except Exception:
        return []
    out = []
    for p in posts:
        pid, text = str(p.get("id") or "").strip(), str(p.get("content") or "").strip()
        when = parse_date(p.get("createdAt") or p.get("actionTime"))
        if JIKE_ID.fullmatch(pid) and text and when:
            out.append(Raw(feed["category"], feed["title"], compact(text),
                           f"https://m.okjike.com/originalPosts/{pid}", when))
    return out


def parse_rss(content: bytes, feed: dict) -> list[Raw]:
    parsed = feedparser.parse(content)
    source = feed["title"] or parsed.feed.get("title") or host_of(feed["url"])
    out = []
    for e in parsed.entries:
        title, link = str(e.get("title", "")).strip(), str(e.get("link", "")).strip()
        when = parse_date(e.get("published")) or parse_date(e.get("updated")) or parse_date(e.get("pubDate"))
        if title and link and when:
            out.append(Raw(feed["category"], source, title, link, when, feed["url"],
                           entry_content(e, link), entry_images(e, link)))
    return out


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml"
YT_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
YT_PLAYLIST_ID = re.compile(r"^[\w-]{10,64}$")


def youtube_feed_url(page_url: str) -> str | None:
    """The RSS url for a YouTube channel (/channel/UC...) or playlist (?list=...) page, which needs no
    network because the id is in the url. None for anything else (including @handle pages)."""
    u = urlparse(page_url)
    if u.scheme not in ("http", "https") or u.netloc.lower() not in YOUTUBE_HOSTS:
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "channel" and YT_CHANNEL_ID.match(parts[1]):
        return f"{YOUTUBE_FEED}?channel_id={parts[1]}"
    if parts[:1] == ["playlist"] or parts == []:
        pl = dict(parse_qsl(u.query)).get("list", "")
        if YT_PLAYLIST_ID.match(pl):
            return f"{YOUTUBE_FEED}?playlist_id={pl}"
    return None


def is_youtube_channel_page(url: str) -> bool:
    """/@handle, /c/name or /user/name: the page has to be fetched to learn the channel id."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or u.netloc.lower() not in YOUTUBE_HOSTS:
        return False
    parts = [p for p in u.path.split("/") if p]
    return bool(parts) and (parts[0].startswith("@") and len(parts[0]) > 1 or parts[0] in ("c", "user") and len(parts) >= 2)


def extract_youtube_channel_id(html: str) -> str | None:
    """The page's own channel id: its RSS <link>, then the canonical <link>, then channel metadata. The
    page is untrusted input, so only a strictly shaped id is ever returned."""
    for pattern in (r'<link[^>]+type="application/rss\+xml"[^>]+channel_id=(UC[\w-]{22})',
                    r'<link[^>]+rel="canonical"[^>]+youtube\.com/channel/(UC[\w-]{22})',
                    r'"externalId":"(UC[\w-]{22})"'):
        if m := re.search(pattern, html):
            return m.group(1)
    return None


def resolve_youtube_feed(page_url: str) -> str | None:
    r = common.get(page_url, (CONNECT_TIMEOUT, READ_TIMEOUT), session(), lang="en")
    if r is None or r.status >= 400:
        return None
    cid = extract_youtube_channel_id(r.text)
    return f"{YOUTUBE_FEED}?channel_id={cid}" if cid else None


TG_HOSTS = {"t.me", "telegram.me"}
TG_CHANNEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
TG_NOT_CHANNELS = {"joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "login", "iv", "setlanguage", "bg", "c"}


def telegram_page_url(url: str) -> str | None:
    """The public preview page (https://t.me/s/NAME) for a Telegram channel url: t.me/NAME, t.me/s/NAME or a
    post link t.me/NAME/123. None for anything else (invite links, private t.me/c/... links, other hosts).
    NAME is matched strictly because it becomes part of the page url that gets fetched."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or u.netloc.lower() not in TG_HOSTS:
        return None
    parts = [p for p in u.path.split("/") if p]
    if parts[:1] == ["s"]:
        parts = parts[1:]
    if not parts or parts[0].lower() in TG_NOT_CHANNELS or not TG_CHANNEL.fullmatch(parts[0]):
        return None
    if len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
        return None
    return f"https://t.me/s/{parts[0]}"


def bridge(xml_url: str, html_url: str) -> tuple[str, str] | None:
    """(parser, page url) for rsshub Telegram/Jike routes: scrape the public page."""
    parts = [p for p in urlparse(xml_url).path.strip("/").split("/") if p]
    if urlparse(xml_url).netloc == "rsshub.app":
        # the route's last segment becomes part of a fetched url: same whitelist as everywhere
        if parts[:2] == ["telegram", "channel"] and len(parts) >= 3:
            page = telegram_page_url(f"https://t.me/{parts[2]}")
            return ("telegram", page) if page else None
        if parts[:1] == ["jike"] and len(parts) >= 3 and parts[1] in ("topic", "user"):
            return ("jike", f"https://m.okjike.com/{parts[1]}s/{parts[2]}") if JIKE_ID.fullmatch(parts[2]) else None
    for candidate in (xml_url, html_url):
        if page := telegram_page_url(candidate):
            return "telegram", page
    for prefix, kind in (("https://t.me/s/", "telegram"), ("https://m.okjike.com/topics/", "jike"),
                         ("https://m.okjike.com/users/", "jike")):
        if html_url.startswith(prefix):
            return kind, html_url
    return None


OPML_SECRETS = ["FOLLOW_OPML_B64"] + [f"FOLLOW_OPML_B64_{i}" for i in range(2, 6)]


def opml_from_env(env=os.environ) -> bytes:
    """OPML packed by tools/opml_secret.html: FOLLOW_OPML_B64, _2, ... joined,
    base64-decoded, then gunzipped (xz and uncompressed also accepted).
    Stays in memory: the decoded list never touches the runner's disk."""
    blob = "".join((env.get(n) or "").strip() for n in OPML_SECRETS)
    if not blob:
        return b""
    raw = base64.b64decode(blob, validate=True)
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:6] == b"\xfd7zXZ\x00":
        return lzma.decompress(raw)
    return raw


def read_opml(raw: bytes, limit: int) -> list[dict]:
    feeds, seen = [], set()
    for o in ET.fromstring(raw).iter("outline"):
        xml = (o.get("xmlUrl") or "").strip()
        if not xml or xml in seen:
            continue
        seen.add(xml)
        html_url = (o.get("htmlUrl") or "").strip()
        f = {"title": (o.get("title") or o.get("text") or host_of(xml) or xml).strip(),
             "category": (o.get("category") or "").strip() or "opmlrss", "home": html_url}
        if b := bridge(xml, html_url):
            f["parser"], f["url"] = b
        elif xml in FEED_SKIP or xml.startswith(FEED_SKIP_PREFIX):
            continue
        else:
            f["parser"], f["url"] = "rss", youtube_feed_url(xml) or FEED_REPLACE.get(xml, xml)
            f["youtube_page"] = is_youtube_channel_page(xml)   # resolved when fetched (fetch_feeds)
        feeds.append(f)
    return feeds[:limit] if limit > 0 else feeds


_local = threading.local()


def session():
    """One session per worker: its own pools, so same-host feeds reuse a connection.
    Minimal retries: a feed that is down now is simply re-fetched next run."""
    if not hasattr(_local, "s"):
        _local.s = make_session(1, {
            "User-Agent": UA, "Accept-Language": LANG, "Accept-Encoding": "gzip, deflate",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                      "text/xml, text/html;q=0.9, */*;q=0.8"}, read_retry=False, status=(), pool=WORKERS)
    return _local.s


def fetch_feeds(feeds: list[dict]) -> list[Raw]:
    groups: dict[str, list[dict]] = {}
    for f in feeds:                  # several OPML entries may resolve to one url
        groups.setdefault(f["url"], []).append(f)

    def one(url, group):
        via = "requests"
        if group[0].get("youtube_page"):
            if not (url := resolve_youtube_feed(url)):
                return [], "could not find the YouTube channel id on this page", via
        r = common.get(url, (CONNECT_TIMEOUT, READ_TIMEOUT), session())
        if r and r.status in BLOCK_STATUS and (alt := common.get(
                url, CONNECT_TIMEOUT + READ_TIMEOUT, impersonate=True, lang=LANG)):
            r, via = alt, "curl_cffi"
        if r is None or r.status >= 400:
            return [], f"HTTP {r.status}" if r else "fetch failed or refused", via
        parsers = {"rss": lambda f: parse_rss(r.content, f),
                   "telegram": lambda f: parse_telegram(r.text, f), "jike": lambda f: parse_jike(r.text, f)}
        try:
            return [x for f in group for x in parsers[f["parser"]](f)], None, via
        except Exception as e:
            return [], f"{type(e).__name__}: {e}", via

    out, errors, rescued = [], [], []
    with ThreadPoolExecutor(max_workers=min(WORKERS, max(4, len(groups)))) as ex:
        futures = {ex.submit(one, u, g): (u, g[0]["title"]) for u, g in groups.items()}
        for fut, (url, title) in futures.items():
            items, err, via = fut.result()
            out += items
            if err:
                errors.append(f"  - [{title}] {url} : {err}")
            elif via == "curl_cffi":
                rescued.append(f"  - [{title}] {url}")
    if rescued:
        print(f"{len(rescued)} feed(s) needed curl_cffi:\n" + "\n".join(rescued))
    if errors:
        print(f"WARNING: {len(errors)} feed(s) failed:\n" + "\n".join(errors))
    return out


def bestblogs_issue(n: int, now: datetime) -> tuple[str, str, datetime | None] | None:
    """(title, body text, date) of issue n, or None when the page isn't there."""
    r = common.get(f"{BESTBLOGS}/newsletter/issue{n}", (CONNECT_TIMEOUT, 30), session())
    if r is None or r.status != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    meta = lambda prop: (soup.select_one(f'meta[property="{prop}"]') or {}).get("content") or ""
    title = meta("og:title") or (soup.title.get_text(" ", strip=True) if soup.title else "")
    title = re.sub(r"\s*\|\s*BestBlogs\.dev\s*$", "", title.strip())
    root = soup.select_one("main") or soup.select_one("article") or soup.body or soup
    for t in root.find_all(("nav", "header", "footer", "noscript")):
        t.decompose()
    text = html_to_text(str(root))
    date = None
    if m := re.search(r"(20\d{2})-(\d{2})-(\d{2})(?!\d)", text):
        date = parse_date(m[0])
    elif m := re.search(r"(\d{1,2})-(\d{1,2})", meta("article:published_time")):
        try:                     # a bare MM-DD: this year, or last year if that is in the future
            date = datetime(now.year, int(m[1]), int(m[2]), tzinfo=UTC)
            date = date.replace(year=now.year - 1) if date > now + timedelta(days=1) else date
        except ValueError:
            pass
    return title, (text[:FEED_CONTENT_MAX] if len(text) >= 500 else ""), date


def fetch_bestblogs(archive: dict, now: datetime, days: int) -> list[Raw]:
    """New issues only. With issues in the archive: scan up from the highest
    one. Without: find the latest issue (doubling, then bisecting), then walk
    down while issues are inside retention."""
    got: dict[int, tuple | None] = {}
    probe = lambda n: got[n] if n in got else got.setdefault(n, bestblogs_issue(n, now))
    known = [int(m[1]) for r in archive.values() if (m := BESTBLOGS_ISSUE.match(str(r.get("url", ""))))]
    found = []
    if known:
        n, misses = max(known) + 1, 0
        while n <= BESTBLOGS_MAX and misses < BESTBLOGS_MISSES:
            misses = 0 if probe(n) else misses + 1
            found += [n] if got[n] else []
            n += 1
    else:
        hi = 1
        while hi <= BESTBLOGS_MAX and probe(hi):
            hi *= 2
        lo = hi // 2                     # lo exists (or is 0), hi does not
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (mid, hi) if probe(mid) else (lo, mid)
        cutoff = now - timedelta(days=days)
        for n in range(lo, max(0, lo - BESTBLOGS_BACKLOG), -1):
            if not probe(n):
                continue
            if got[n][2] and got[n][2] < cutoff:
                break
            found.append(n)
    out = [Raw("tech", "BestBlogs", got[n][0], f"{BESTBLOGS}/newsletter/issue{n}", got[n][2], content=got[n][1])
           for n in sorted(found) if got[n][0]]
    print(f"BestBlogs: {len(known)} known, {len(out)} new ({len(got)} page(s) fetched)")
    return out


def _thepaper_date(d: dict, now: datetime) -> datetime | None:
    for k in ("pubTimeLong", "publishTime", "pubTime", "pubTimeNew"):
        v = d.get(k)
        if v in (None, ""):
            continue
        if isinstance(v, str) and (m := CN_REL_TIME.search(v)):
            unit = {"分钟": "minutes", "小时": "hours", "天": "days"}[m[2]]
            return now - timedelta(**{unit: int(m[1])})
        if isinstance(v, str) and not re.search(r"\d{4}", v) and not v.isdigit():
            continue
        if when := parse_date(v):
            # bare "2026-10-12 08:30" is Beijing time
            if isinstance(v, str) and not re.search(r"(?:z|[+-]\d{2}:?\d{2})$", v.strip(), re.I):
                when -= timedelta(hours=8)
            return when
    return None


def thepaper_listing(html: str, now: datetime) -> list[tuple[str, str, datetime | None]]:
    """(article id, title, date) from a channel page: the __NEXT_DATA__ JSON,
    else the server-rendered links (whose text repeats the title)."""
    out: dict[str, tuple[str, datetime | None]] = {}
    for d in walk(next_data(html)):
        cid, name = d.get("contId"), d.get("name") or d.get("title")
        link = str(d.get("link") or d.get("url") or "")
        if not (cid and isinstance(name, str) and name.strip()):
            continue
        if link and not THEPAPER_ARTICLE.match(link):     # collections, ads, videos elsewhere
            continue
        cid = str(cid)
        if cid.isdigit() and cid not in out:
            out[cid] = (name.strip(), _thepaper_date(d, now))
    if not out:
        soup = BeautifulSoup(html or "", "html.parser")
        for a in soup.find_all("a", href=True):
            if not (m := THEPAPER_ARTICLE.match(a["href"])) or "commTag" in a["href"]:
                continue
            text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            half = len(text) // 2                  # "标题 标题" (img alt + caption)
            if half and text[:half].strip() == text[half:].strip():
                text = text[:half].strip()
            if text and len(text) > len(out.get(m[1], ("",))[0]):
                out[m[1]] = (text, None)
    return [(cid, t, w) for cid, (t, w) in out.items()]


def thepaper_article(cid: str, now: datetime):
    """(body html, date, images) of one article page, or None."""
    url = f"{THEPAPER}/newsDetail_forward_{cid}"
    r = common.get(url, (CONNECT_TIMEOUT, 30), session(), lang=LANG)
    if r is None or r.status != 200:
        return None
    data, body, other, when = next_data(r.text), "", "", None
    for d in walk(data):
        own = str(d.get("contId", "")) == cid
        c = d.get("content")
        if isinstance(c, str) and "<" in c:      # this article's own body first; any other only as fallback
            if own and len(c) > len(body):
                body = c
            elif not own and len(c) > len(other):
                other = c
        when = when or (_thepaper_date(d, now) if own else None)
    body = body or other
    soup = BeautifulSoup(r.text, "html.parser")
    if not body:
        node = (soup.select_one("[class*=cententWrap]") or soup.select_one("[class*=contentWrap]")
                or soup.select_one("article") or soup.select_one("main"))
        body = str(node) if node else ""
    if when is None:
        meta = soup.select_one('meta[property="article:published_time"]')
        when = parse_date(meta.get("content")) if meta else None
    if when is None and (m := re.search(r"(20\d{2}-\d{2}-\d{2} \d{2}:\d{2})", soup.get_text(" "))):
        when = parse_date(m[1] + " +08:00")
    text = html_to_text(body)                     # plain text, like every other feed copy
    images = tuple(thumbs.candidates(body, url)) + tuple(thumbs.candidates(r.text[:60000], url)[:2])
    return (text[:FEED_CONTENT_MAX] if len(text) >= 300 else ""), when, images


def fetch_thepaper(archive: dict) -> list[Raw]:
    """澎湃 channel pages (no RSS). Like fetch_bestblogs, the article body is
    fetched once, when the item is new, and kept as the feed copy, so
    summarize_feed never fetches the page itself. Items still listed are
    re-emitted (refreshing last_seen_at) without refetching."""
    now = datetime.now(tz=UTC)
    known = {m[1]: r for r in archive.values()
             if (m := THEPAPER_ARTICLE.match(str(r.get("url", ""))))}
    out = []
    for path, (category, source) in THEPAPER_CHANNELS.items():
        r = common.get(f"{THEPAPER}/{path}", (CONNECT_TIMEOUT, 30), session(), lang=LANG)
        if r is None or r.status != 200:
            print(f"WARNING: thepaper {path}: {'HTTP %d' % r.status if r else 'fetch failed'}")
            continue
        listing = thepaper_listing(r.text, now)
        fresh = [cid for cid, _, _ in listing
                 if not (k := known.get(cid)) or not (k.get("summary") or k.get("feed_content"))]
        with ThreadPoolExecutor(max_workers=8) as ex:
            pages = dict(zip(fresh, ex.map(lambda c: thepaper_article(c, now), fresh)))
        for cid, title, when in listing:
            body, images = "", ()
            if got := pages.get(cid):
                body, images = got[0], got[2]
                when = got[1] or when
            if cid in known and when is None:
                when = parse_date(known[cid].get("published_at"))
            out.append(Raw(category, source, title, f"{THEPAPER}/newsDetail_forward_{cid}",
                           when, content=body, images=images))
        print(f"thepaper {path}: {len(listing)} listed, {len(fresh)} fetched")
    return out


# ---- inbox ---------------------------------------------------------------------
# data/inbox.json is written by the reader (feed-github.js) through the GitHub
# API: {"urls": [{"url", "added"}]}. Read-only here -- the reader prunes it --
# so a run never conflicts with a push made while it was running.
INBOX_SOURCE = "URL"
INBOX_LEGACY_SOURCES = {INBOX_SOURCE, "Reader"}     # items added before the rename
INBOX_MAX = 500


def page_title(url: str) -> str:
    r = common.get(url, (CONNECT_TIMEOUT, READ_TIMEOUT), session(), lang=LANG)
    if r is None or r.status >= 400:
        return ""
    soup = BeautifulSoup(r.text[:500_000], "html.parser")
    og = (soup.select_one('meta[property="og:title"]') or {}).get("content") or ""
    return compact(og or (soup.title.string if soup.title and soup.title.string else ""))


def fetch_inbox(archive: dict, path: Path, days: int) -> list[Raw]:
    """Urls added in the reader. A known url keeps its stored title (the id
    hashes the title); a new one gets the page's title, else host + path."""
    try:
        urls = json.loads(path.read_text(encoding="utf-8")).get("urls") or []
    except (OSError, ValueError, AttributeError):
        return []
    known = {r.get("url"): r for r in archive.values() if r.get("source") in INBOX_LEGACY_SOURCES}
    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    out, seen = [], set()
    for e in urls[-INBOX_MAX:] if isinstance(urls, list) else []:
        url = str(e.get("url") or "").strip() if isinstance(e, dict) else ""
        when = parse_date(e.get("added")) if url else None
        p = urlparse(url)
        if (len(url) > 2048 or p.scheme not in ("http", "https") or not p.hostname
                or p.username or p.password or url in seen or not when or when < cutoff):
            continue
        seen.add(url)
        rec = known.get(canonical_url(url, INBOX_SOURCE, ""))
        title = (rec or {}).get("title") or page_title(url) or (p.hostname + p.path.rstrip("/"))
        # A known record keeps its own source too: the id hashes it.
        out.append(Raw("inbox", (rec or {}).get("source") or INBOX_SOURCE, title, url, when))
    print(f"Inbox: {len(out)} url(s), {sum(canonical_url(r.url, INBOX_SOURCE, '') not in known for r in out)} new")
    return out


# ---- archive ------------------------------------------------------------------

def blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (
        isinstance(v, (list, dict)) and not v)


def better_summary(a, b):
    """Real summary over a ↛-marked one; else the longer."""
    if blank(a) or blank(b):
        return b if blank(a) else a
    if ("↛" in a) != ("↛" in b):
        return b if "↛" in a else a
    return b if len(b) > len(a) else a


def absorb(keep: dict, other: dict) -> None:
    """Fold a duplicate into the record kept, never overwriting known values."""
    if not blank(s := better_summary(keep.get("summary"), other.get("summary"))):
        keep["summary"] = s
    if vals := [v for v in (keep.get("last_seen_at"), other.get("last_seen_at")) if v]:
        keep["last_seen_at"] = max(vals)
    for f, v in other.items():
        if f not in ("id", "summary", "last_seen_at") \
                and blank(keep.get(f)) and not blank(v):
            keep[f] = v


def load_archive(path: Path) -> tuple[dict, dict]:
    """(doc, {id: record}). Re-canonicalises stored urls (re-keying the changed
    ones; ids name subtitle files, so only then) and folds records sharing
    published_at + source + url -- ids hash the title, which feeds edit."""
    doc = load_doc(path)
    moved = 0
    for r in doc["items"]:
        new = canonical_url(r.get("url") or "", str(r.get("source") or ""))
        if new and new != r.get("url"):
            r["url"] = new
            r["id"] = make_id(str(r.get("category") or ""), str(r.get("source") or ""),
                              str(r.get("title") or ""), new)
            moved += 1
    first, keep = {}, []
    for r in doc["items"]:                     # file is newest-first: first one wins
        key = (r.get("published_at"), r.get("source"), r.get("url"))
        if all(key) and key in first:
            absorb(first[key], r)
            continue
        if all(key):
            first[key] = r
        keep.append(r)
    archive: dict = {}
    for r in keep:                             # re-keying can land on an id already present
        if not r.get("id"):
            continue
        if r["id"] in archive:
            absorb(archive[r["id"]], r)
        else:
            archive[r["id"]] = r
    if merged := len(doc["items"]) - len(archive):
        print(f"Re-canonicalised {moved} url(s), merged {merged} duplicate(s).")
    elif moved:
        print(f"Re-canonicalised {moved} url(s).")
    return doc, archive


def ingest(archive: dict, raws: list[Raw], now: datetime) -> None:
    """Fold fetched items into the archive: create new records, refresh known
    ones (a feed-supplied date always wins: feeds fix their dates)."""
    for raw in raws:
        title = raw.title.strip()
        url = canonical_url(raw.url, raw.source, raw.feed_url)
        if not title or urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
            continue
        iid = make_id(raw.category, raw.source, title, url)
        rec = archive.get(iid)
        if rec is None:
            rec = archive[iid] = {"id": iid, "category": "", "source": "",
                                  "title": "", "url": "", "published_at": iso(raw.published_at)}
        elif raw.published_at:
            rec["published_at"] = iso(raw.published_at)       # feeds fix their dates
        rec.update(category=raw.category, source=raw.source,
                   title=title, url=url, last_seen_at=iso(now))
        if not rec.get("summary"):
            if raw.content and not rec.get("feed_content"):
                rec["feed_content"] = raw.content
            if raw.images and not rec.get("feed_images"):
                rec["feed_images"] = list(raw.images)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output-dir", default="data")
    ap.add_argument("--archive-days", type=int, default=int(os.environ.get("ARCHIVE_DAYS") or 60),
                    help="drop items whose last_seen_at is older than this (env ARCHIVE_DAYS, default 60)")
    ap.add_argument("--rss-opml", default="", help="OPML file; without it, the FOLLOW_OPML_B64* env vars")
    ap.add_argument("--require-opml", action="store_true", help="exit with an error when no OPML is found")
    ap.add_argument("--rss-max-feeds", type=int, default=0, help="0 = all")
    a = ap.parse_args(argv)

    now = datetime.now(tz=UTC)
    path = Path(a.output_dir) / "archive.json"
    doc, archive = load_archive(path)

    raws: list[Raw] = []
    if a.rss_opml:
        src = Path(a.rss_opml).expanduser()
        raw, where = (src.read_bytes() if src.is_file() else b""), str(src)
    else:
        raw, where = opml_from_env(), "FOLLOW_OPML_B64 secret"
    if raw.strip():
        feeds = read_opml(raw, a.rss_max_feeds)
        print(f"OPML: {len(feeds)} feeds from {where}")
        raws = fetch_feeds(feeds)
    elif a.require_opml:
        sys.exit(f"ERROR: no OPML ({where}); make the secret with tools/opml_secret.html")
    else:
        print(f"WARNING: no OPML ({where}); RSS sources skipped.")
    raws += fetch_bestblogs(archive, now, a.archive_days)
    raws += fetch_thepaper(archive)
    raws += fetch_inbox(archive, Path(a.output_dir) / "inbox.json", a.archive_days)

    ingest(archive, raws, now)

    # Retention: last_seen_at (refreshed whenever a feed still lists the item);
    # records written before that field existed fall back to published_at.
    cutoff = now - timedelta(days=a.archive_days)
    stamp = lambda r: parse_date(r.get("last_seen_at")) or parse_date(r.get("published_at")) or now
    kept = [r for r in archive.values() if stamp(r) >= cutoff]
    if dropped := len(archive) - len(kept):
        print(f"Retention: dropped {dropped} item(s) not seen for {a.archive_days}+ days")
    for r in kept:
        if r.get("summary"):
            common.drop_scratch(r)
    kept.sort(key=lambda r: parse_date(r.get("last_seen_at")) or datetime.min.replace(tzinfo=UTC),
              reverse=True)
    doc = {"generated_at": iso(now), "items": kept}
    print(f"Feed copies for {common.save_scratch(kept)} pending item(s) -> {common.scratch_path()}")
    save_doc(path, doc)
    print(f"Wrote {path} ({len(kept)} items, fetched {len(raws)} raw)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
