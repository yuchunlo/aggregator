#!/usr/bin/env python3
"""Entry point 1 (updatenews.yml). Collect items from OPML feeds (plus
Telegram/Jike bridges, YouTube channels, the reader's inbox and, when
SCRAPED_SOURCES is on, BestBlogs and 澎湃) into DATA_DIR/archive.json:
canonical urls, stable ids, dedupe, retention. Then summarize.run in this same
process (unless --no-summarize), so the feed copies (feed_content /
feed_images) are handed over in memory, never on disk."""

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
import xml.etree.ElementTree as ET
import html as html_mod
from html.parser import HTMLParser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import net
import store
import summarize
import textproc
from net import BLOCK_STATUS, FEED_HEADERS, FEED_LANG
from store import FALLBACK_MARK, UTC, host_in, host_of, iso, load_doc, next_data, parse_date, save_doc, site, walk
from textproc import html_to_text

FEED_TIMEOUT = 17                         # seconds per feed, in total
PAGE_TIMEOUT = 35
FEED_WORKERS = 32                         # feeds fetched at the same time
FEED_CONTENT_MAX = 60000                  # feed copy kept for summarize
TRACKING = {"ref", "spm", "fbclid", "gclid", "igshid", "mkt_tok", "mc_cid", "mc_eid", "_hsenc", "_hsmi"}

# Site tables live in rules.json (`sites`), read where they are used:
#   feed_replace       OPML xmlUrl -> the feed actually fetched
#   feed_skip          OPML xmlUrls never fetched (feed_skip_prefix: by prefix)
#   feed_content_skip  media hosts: no feed copy kept
#   origin_fixups      source -> origin for feeds publishing article links on a
#                      dev origin (http://localhost:8000/...); the explicit
#                      table wins over the feed's own origin, which can be a proxy
DEV_ORIGIN = re.compile(r"^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|[^.]+\.local)(?::\d+)?$", re.I)
# Same Firstory episode under two hosts; one spelling, so one id.
HOST_ALIASES = {"open.firstory.me": "open.firstory.fm"}
VOCUS_AUTHOR = re.compile(r"^(https?://(?:www\.)?vocus\.cc)/@[^/]*/([0-9A-Za-z]+)(.*)$")
TG_POST = re.compile(r"[A-Za-z0-9_]{1,64}/\d{1,12}")
JIKE_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")

# Scraped sources (no RSS): BestBlogs newsletter issues and 澎湃 channel pages.
# Off unless SCRAPED_SOURCES is on (set in updatenews.yml); the url rule below
# (canonical_url) applies either way, so ids stay the same when it is toggled.
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
        origin = site("origin_fixups", dict).get(source.strip().casefold()) or (
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
    """Fixed formula: ids also name subtitle files, so a different hash would
    orphan every subtitle already stored."""
    key = "||".join([category.strip().lower(), source.strip().lower(),
                     title.strip().lower(), normalize_url(url)])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# ---- feed parsing -------------------------------------------------------------
def _entry_html(entry, keys) -> list[str]:
    """The entry's content blocks, then its `keys` fields, as strings."""
    out = [str(b.get("value") or "") for b in entry.get("content") or [] if isinstance(b, dict)]
    return out + [str(entry[k]) for k in keys if entry.get(k)]


def entry_content(entry, link: str) -> str:
    """Longest body-ish payload of a feed entry, so a page that later refuses
    to be fetched can still be summarised."""
    if host_in(link, site("feed_content_skip")):
        return ""
    cands = _entry_html(entry, ("summary", "description", "subtitle"))
    return html_to_text(max(cands, key=len))[:FEED_CONTENT_MAX] if cands else ""


FEED_IMAGES_MAX = 12


def entry_images(entry, link: str) -> tuple:
    """Image urls a feed entry carries, best first: <img>s in its html (the
    article's own images), then media:content / media:thumbnail / image
    enclosures (often small renditions). Plain-text feed_content loses these,
    so they are kept beside it for the thumbnail."""
    if host_in(link, site("feed_content_skip")):
        return ()
    html = _entry_html(entry, ("summary", "description"))
    urls = [u for h in sorted(html, key=len, reverse=True) for u in summarize.thumb_candidates(h, link)]
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


# Telegram preview page (t.me/s/NAME), read in one streaming pass (no tree).
# Each .tgme_widget_message[data-post] gives one post:
#   title  the first .tgme_widget_message_text, get_text(" ", strip=True) then
#          compact(): unchanged from the earlier tree-parser version, because the id
#          hashes it (a different title would fork every stored post)
#   text   the post's own text (a quoted reply left out), line breaks kept,
#          plus the link preview's title and description: the feed copy
#   images photo / video thumbnail / link preview image, from the inline
#          background-image style
_TG_BLOCK = frozenset("div p li blockquote pre h1 h2 h3 h4 h5 h6 tr".split())
_TG_IMAGE = ("tgme_widget_message_photo_wrap", "tgme_widget_message_video_thumb",
             "link_preview_image", "link_preview_right_image")
_TG_PREVIEW = ("link_preview_title", "link_preview_description")
_TG_STYLE_URL = re.compile(r"""url\(\s*+['"]?+([^'")\s]++)""")


class _TelegramPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, frozenset]] = []
        self.posts: list[dict] = []
        self.post = None            # the post being read
        self.post_depth = 0
        self.cap = []               # open captures: [depth, kind, parts]
        self.open = Counter()       # tag name -> open count (a stray end tag costs O(1))
        self.reply_at = 0           # depth of the quoted reply being read, 0 = none

    @staticmethod
    def _classes(attrs) -> frozenset:
        return frozenset((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag, attrs):
        cls = self._classes(attrs)
        a = dict(attrs)
        if tag not in textproc.VOID:
            self.stack.append((tag, cls))
            self.open[tag] += 1
        depth = len(self.stack)
        if self.post is None:
            if "tgme_widget_message" in cls and tag not in textproc.VOID:
                self.post = {"post": (a.get("data-post") or "").strip(), "title": None, "text": [],
                             "preview": [], "images": [], "when": None}
                self.post_depth = depth
            return
        p = self.post
        if tag == "br":
            for c in self.cap:
                c[2].append("\n")
            return
        if not self.reply_at and "tgme_widget_message_reply" in cls and tag not in textproc.VOID:
            self.reply_at = depth
        in_reply = bool(self.reply_at)
        if tag == "time" and p["when"] is None and not in_reply and a.get("datetime"):
            p["when"] = a["datetime"]
        if not in_reply and any(k in cls for k in _TG_IMAGE):
            if m := _TG_STYLE_URL.search((a.get("style") or "")[:2000]):
                p["images"].append(urljoin("https://t.me/", html_mod.unescape(m[1])))
        if "tgme_widget_message_text" in cls:
            if p["title"] is None:
                self.cap.append([depth, "title", []])
            if not in_reply and not p["text"]:
                self.cap.append([depth, "text", []])
        elif not in_reply and any(k in cls for k in _TG_PREVIEW):
            self.cap.append([depth, "preview", []])

    def handle_endtag(self, tag):
        if not self.open[tag]:
            return                                  # stray end tag
        while self.stack:
            t, _ = self.stack.pop()
            self.open[t] -= 1
            self._closed(len(self.stack) + 1, t)
            if t == tag:
                break

    def _closed(self, depth, tag):
        if depth == self.reply_at:
            self.reply_at = 0
        for c in [c for c in self.cap if c[0] == depth]:
            self.cap.remove(c)
            p = self.post
            if c[1] == "title":
                p["title"] = " ".join(x.strip() for x in c[2] if x.strip() and x != "\n")
            elif c[1] == "text":
                p["text"] = ["".join(c[2])]
            else:
                p["preview"].append("".join(c[2]))
        if tag in _TG_BLOCK:                        # a block inside a capture ended
            for c in self.cap:
                if c[0] < depth:
                    c[2].append("\n")
        if self.post is not None and depth == self.post_depth:
            self.posts.append(self.post)
            self.post, self.cap = None, []

    def handle_data(self, data):
        for c in self.cap:
            c[2].append(data)


def _tg_lines(text: str) -> str:
    lines = (re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def parse_telegram(html: str, feed: dict) -> list[Raw]:
    p = _TelegramPage()
    p.feed(html or "")
    p.close()
    out = []
    for m in p.posts:
        title = m["title"] or ""
        if not title and m["preview"]:              # no text: the link preview's title, as before
            title = " ".join(m["preview"][0].split())
        when = parse_date(m["when"])
        if not (TG_POST.fullmatch(m["post"]) and title and when):    # attribute text becomes a url path
            continue
        body = _tg_lines("\n".join(m["text"] + m["preview"]))[:FEED_CONTENT_MAX]
        images = tuple(dict.fromkeys(u for u in m["images"] if u.lower().startswith(("http://", "https://"))))
        out.append(Raw(feed["category"], feed["title"], compact(title), f"https://t.me/{m['post']}", when,
                       content=body, images=images[:FEED_IMAGES_MAX]))
    return out


def parse_jike(html: str, feed: dict) -> list[Raw]:
    try:
        posts = next_data(html)["props"]["pageProps"].get("posts") or []
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
    parsed = store.parse_feed(content)
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


def _youtube_path(url: str):
    """(parsed url, path segments) of a YouTube web url, else None."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or u.netloc.lower() not in YOUTUBE_HOSTS:
        return None
    return u, [p for p in u.path.split("/") if p]


def youtube_feed_url(page_url: str) -> str | None:
    """The RSS url for a YouTube channel (/channel/UC...) or playlist (?list=...) page, which needs no
    network because the id is in the url. None for anything else (including @handle pages)."""
    if not (yt := _youtube_path(page_url)):
        return None
    u, parts = yt
    if len(parts) >= 2 and parts[0] == "channel" and YT_CHANNEL_ID.match(parts[1]):
        return f"{YOUTUBE_FEED}?channel_id={parts[1]}"
    if parts[:1] == ["playlist"] or parts == []:
        pl = dict(parse_qsl(u.query)).get("list", "")
        if YT_PLAYLIST_ID.match(pl):
            return f"{YOUTUBE_FEED}?playlist_id={pl}"
    return None


def is_youtube_channel_page(url: str) -> bool:
    """/@handle, /c/name or /user/name: the page has to be fetched to learn the channel id."""
    parts = (_youtube_path(url) or (None, []))[1]
    return bool(parts) and (parts[0].startswith("@") and len(parts[0]) > 1 or parts[0] in ("c", "user") and len(parts) >= 2)


YT_EXTERNAL_ID = re.compile(r'"externalId":"(UC[\w-]{22})"')


def extract_youtube_channel_id(html: str) -> str | None:
    """The page's own channel id: its RSS <link>, then the canonical <link>, then channel metadata. The
    page is untrusted input, so only a strictly shaped id is ever returned."""
    rss = canon = None
    for _, a in store.iter_tags(html, ("link",)):
        href, rel = a.get("href") or "", (a.get("rel") or "").lower()
        if rss is None and (a.get("type") or "").lower() == "application/rss+xml":
            rss = m[1] if (m := re.search(r"channel_id=(UC[\w-]{22})(?![\w-])", href)) else None
        if canon is None and rel == "canonical":
            canon = m[1] if (m := re.search(r"youtube\.com/channel/(UC[\w-]{22})(?![\w-])", href)) else None
    if rss or canon:
        return rss or canon
    m = YT_EXTERNAL_ID.search(html or "")
    return m[1] if m else None


def resolve_youtube_feed(page_url: str) -> str | None:
    r = fetch(page_url, lang="en")
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
    skip, skip_prefix = frozenset(site("feed_skip")), site("feed_skip_prefix")
    replace = site("feed_replace", dict)
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
        elif xml in skip or xml.startswith(skip_prefix):
            continue
        else:
            f["parser"], f["url"] = "rss", youtube_feed_url(xml) or replace.get(xml, xml)
            f["youtube_page"] = is_youtube_channel_page(xml)   # resolved when fetched (fetch_feeds)
        feeds.append(f)
    return feeds[:limit] if limit > 0 else feeds


def fetch(url: str, timeout=PAGE_TIMEOUT, lang: str = FEED_LANG):
    """A feed or a page of a scraped source. One retry on a transport error;
    a feed that is down now is simply re-fetched next run."""
    return net.get(url, timeout, headers=FEED_HEADERS, lang=lang, retries=1, retry_status=())


def fetch_feeds(feeds: list[dict]) -> list[Raw]:
    groups: dict[str, list[dict]] = {}
    for f in feeds:                  # several OPML entries may resolve to one url
        groups.setdefault(f["url"], []).append(f)

    def one(url, group):
        via = "direct"
        if group[0].get("youtube_page"):
            if not (url := resolve_youtube_feed(url)):
                return [], "could not find the YouTube channel id on this page", via
        r = fetch(url, FEED_TIMEOUT)
        if r and r.status in BLOCK_STATUS and (alt := net.get(
                url, FEED_TIMEOUT, impersonate=True, lang=FEED_LANG)):
            r, via = alt, "fingerprint"
        if r is None or r.status >= 400:
            return [], f"HTTP {r.status}" if r else "fetch failed or refused", via
        parsers = {"rss": lambda f: parse_rss(r.content, f),
                   "telegram": lambda f: parse_telegram(r.text, f), "jike": lambda f: parse_jike(r.text, f)}
        try:
            return [x for f in group for x in parsers[f["parser"]](f)], None, via
        except Exception as e:
            return [], f"{type(e).__name__}: {e}", via

    out, errors, rescued = [], [], []
    with ThreadPoolExecutor(max_workers=min(FEED_WORKERS, max(4, len(groups) or 1))) as ex:
        futures = {ex.submit(one, u, g): (u, g[0]["title"]) for u, g in groups.items()}
        for fut, (url, title) in futures.items():
            items, err, via = fut.result()
            out += items
            if err:
                errors.append(f"  - [{title}] {url} : {err}")
            elif via == "fingerprint":
                rescued.append(f"  - [{title}] {url}")
    if rescued:
        print(f"[FEEDS] {len(rescued)} feed(s) needed the browser fingerprint:\n" + "\n".join(rescued))
    if errors:
        print(f"WARNING: {len(errors)} feed(s) failed:\n" + "\n".join(errors))
    return out


# ---- inbox ---------------------------------------------------------------------
# data/inbox.json is written by the reader (feed-github.js) through the GitHub
# API: {"urls": [{"url", "added"}]}. Read-only here -- the reader prunes it --
# so a run never conflicts with a push made while it was running.
INBOX_SOURCE = "URL"
INBOX_LEGACY_SOURCES = {INBOX_SOURCE, "Reader"}     # items added before the rename
INBOX_MAX = 500


def fetch_title(url: str) -> str:
    r = fetch(url, FEED_TIMEOUT)
    return compact(textproc.page_title(r.text[:500_000])) if r is not None and r.status < 400 else ""


def bestblogs_issue(n: int, now: datetime) -> tuple[str, str, datetime | None] | None:
    """(title, body text, date) of issue n, or None when the page isn't there."""
    r = fetch(f"{BESTBLOGS}/newsletter/issue{n}")
    if r is None or r.status != 200:
        return None
    page = r.text
    meta = lambda prop: html_mod.unescape(textproc.meta_first(page, (prop,)))
    title = textproc.page_title(page)
    title = re.sub(r"\s*\|\s*BestBlogs\.dev\s*$", "", title.strip())
    text = textproc.region_text(page)
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
    print(f"[BESTBLOGS] {len(known)} known, {len(out)} new ({len(got)} page(s) fetched)")
    return out


EPOCH_RE = re.compile(r"\d{9,}(?:\.\d+)?")
TZ_SUFFIX = re.compile(r"(?:z|[+-]\d{2}:?\d{2}|\b(?:utc|gmt))$", re.I)


def _thepaper_date(d: dict, now: datetime) -> datetime | None:
    for k in ("pubTimeLong", "publishTime", "pubTime", "pubTimeNew"):
        v = d.get(k)
        if v in (None, "") or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) or (isinstance(v, str) and EPOCH_RE.fullmatch(v.strip())):
            if when := parse_date(v):          # epoch s/ms: already UTC
                return when
            continue
        if not isinstance(v, str):
            continue
        if m := CN_REL_TIME.search(v):
            unit = {"分钟": "minutes", "小时": "hours", "天": "days"}[m[2]]
            return now - timedelta(**{unit: int(m[1])})
        if not re.search(r"\d{4}", v):
            continue
        if when := parse_date(v):
            if not TZ_SUFFIX.search(v.strip()):  # bare "2026-10-12 08:30" is Beijing time
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
        for a in textproc.elements(html or ""):
            href = a.attrs.get("href") if a.tag == "a" else None
            if not href or not (m := THEPAPER_ARTICLE.match(href)) or "commTag" in href:
                continue
            text = re.sub(r"\s+", " ", a.text(" "))
            half = len(text) // 2                  # "标题 标题" (img alt + caption)
            if half and text[:half].strip() == text[half:].strip():
                text = text[:half].strip()
            if text and len(text) > len(out.get(m[1], ("",))[0]):
                out[m[1]] = (text, None)
    return [(cid, t, w) for cid, (t, w) in out.items()]


def thepaper_article(cid: str, now: datetime):
    """(body html, date, images) of one article page, or None."""
    url = f"{THEPAPER}/newsDetail_forward_{cid}"
    r = fetch(url)
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
    page = r.text
    doc = textproc.elements(page)
    if not body:
        node = doc.first(lambda e: e.has_class("cententWrap"), lambda e: e.has_class("contentWrap"),
                         lambda e: e.tag == "article", lambda e: e.tag == "main")
        body = page[node.start:node.end] if node else ""
    if when is None:
        meta = textproc.meta_first(page, ("article:published_time",))
        when = parse_date(html_mod.unescape(meta)) if meta else None
    if when is None and (m := re.search(r"(20\d{2}-\d{2}-\d{2} \d{2}:\d{2})",
                                        doc.text())):
        when = parse_date(m[1] + " +08:00")
    text = html_to_text(body)                     # plain text, like every other feed copy
    images = tuple(summarize.thumb_candidates(body, url)) + tuple(summarize.thumb_candidates(r.text[:60000], url)[:2])
    return (text[:FEED_CONTENT_MAX] if len(text) >= 300 else ""), when, images


def fetch_thepaper(archive: dict) -> list[Raw]:
    """澎湃 channel pages (no RSS). Like fetch_bestblogs, the article body is
    fetched while the item is pending and handed over as the feed copy, so
    summarize never fetches the page itself. Items still listed are
    re-emitted (refreshing last_seen_at) without refetching."""
    now = datetime.now(tz=UTC)
    known = {m[1]: r for r in archive.values()
             if (m := THEPAPER_ARTICLE.match(str(r.get("url", ""))))}
    out = []
    for path, (category, source) in THEPAPER_CHANNELS.items():
        r = fetch(f"{THEPAPER}/{path}")
        if r is None or r.status != 200:
            print(f"WARNING: thepaper {path}: {'HTTP %d' % r.status if r else 'fetch failed'}")
            continue
        listing = thepaper_listing(r.text, now)
        # New or still pending: feed copies live in memory only, so a pending
        # item's body is fetched again each run (then summarised in this process).
        fresh = [cid for cid, _, _ in listing if not (known.get(cid) or {}).get("summary")]
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
        print(f"[THEPAPER] {path}: {len(listing)} listed, {len(fresh)} fetched")
    return out


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
        title = (rec or {}).get("title") or fetch_title(url) or (p.hostname + p.path.rstrip("/"))
        # A known record keeps its own source too: the id hashes it.
        out.append(Raw("inbox", (rec or {}).get("source") or INBOX_SOURCE, title, url, when))
    print(f"[INBOX] {len(out)} url(s), {sum(canonical_url(r.url, INBOX_SOURCE, '') not in known for r in out)} new")
    return out


# ---- archive ------------------------------------------------------------------

def blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (
        isinstance(v, (list, dict)) and not v)


def better_summary(a, b):
    """Real summary over a ↛-marked one; else the longer."""
    if blank(a) or blank(b):
        return b if blank(a) else a
    if (FALLBACK_MARK in a) != (FALLBACK_MARK in b):
        return b if FALLBACK_MARK in a else a
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
        print(f"[ARCHIVE] re-canonicalised {moved} url(s), merged {merged} duplicate(s)")
    elif moved:
        print(f"[ARCHIVE] re-canonicalised {moved} url(s)")
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
            # The key itself means "the feed listed this entry in this process",
            # even with no text: summarize tells "no copy" (wait) from
            # "copy without text" (e.g. a douban mark without a note).
            if not rec.get("feed_content"):
                rec["feed_content"] = raw.content
            if raw.images and not rec.get("feed_images"):
                rec["feed_images"] = list(raw.images)


def main(argv=None) -> int:
    """Collect, then summarize and backfill in this process (feed copies stay in
    memory). Tuning comes from the environment: DATA_DIR, ARCHIVE_DAYS,
    RSS_MAX_FEEDS, SCRAPED_SOURCES, MAX_ITEMS, TIME_BUDGET_SECONDS, TRANSLATE..."""
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--rss-opml", default="", help="OPML file (local runs); default: the FOLLOW_OPML_B64* env vars")
    ap.add_argument("--no-summarize", action="store_true", help="collect only: no summaries, no backfill")
    a = ap.parse_args(argv)

    now = datetime.now(tz=UTC)
    cfg = store.configure()
    days = cfg.archive_days
    path = cfg.archive
    if a.rss_opml:
        src = Path(a.rss_opml).expanduser()
        raw, where = (src.read_bytes() if src.is_file() else b""), str(src)
    else:
        raw, where = opml_from_env(), "FOLLOW_OPML_B64 secret"
    if not raw.strip():          # before touching the archive: nothing is written
        sys.exit(f"ERROR: no OPML ({where}); make the secret with tools/opml_secret.html")
    doc, archive = load_archive(path)

    feeds = read_opml(raw, cfg.rss_max_feeds)
    print(f"[OPML] {len(feeds)} feeds from {where}")
    raws = fetch_feeds(feeds)
    if cfg.scraped_sources:
        raws += fetch_bestblogs(archive, now, days)
        raws += fetch_thepaper(archive)
    else:
        print("[SCRAPED] BestBlogs, 澎湃 off; set SCRAPED_SOURCES=on to run them")
    raws += fetch_inbox(archive, cfg.inbox, days)

    ingest(archive, raws, now)

    # Retention: last_seen_at (refreshed whenever a feed still lists the item);
    # records written before that field existed fall back to published_at.
    cutoff = now - timedelta(days=days)
    stamp = lambda r: parse_date(r.get("last_seen_at")) or parse_date(r.get("published_at")) or now
    kept = [r for r in archive.values() if stamp(r) >= cutoff]
    if dropped := len(archive) - len(kept):
        print(f"[RETENTION] dropped {dropped} item(s) not seen for {days}+ days")
    for r in kept:
        if r.get("summary"):
            store.drop_scratch(r)
    kept.sort(key=lambda r: parse_date(r.get("last_seen_at")) or datetime.min.replace(tzinfo=UTC),
              reverse=True)
    doc = {"generated_at": iso(now), **({"rules_hash": doc["rules_hash"]} if doc.get("rules_hash") else {}),
           "items": kept}
    save_doc(path, doc)
    print(f"[ARCHIVE] wrote {path} ({len(kept)} items, fetched {len(raws)} raw)")
    if not a.no_summarize:
        summarize.run(path, doc, cfg.max_items, cfg.time_budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
