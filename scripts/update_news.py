#!/usr/bin/env python3
"""Collect items from OPML feeds (plus Telegram/Jike bridges)
into data/archive.json: canonical urls, stable ids, dedupe, retention."""

from __future__ import annotations

import argparse
import base64
import copy
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

import common
from common import UA, host_in, host_of, load_doc, make_session, save_doc

UTC = timezone.utc
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
VOCUS_AUTHOR = re.compile(r"^(https?://(?:www\.)?vocus\.cc)/@[^/]+/([0-9A-Za-z]+)(.*)$")
TG_POST = re.compile(r"[A-Za-z0-9_]{1,64}/\d{1,12}")
JIKE_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")
BESTBLOGS = "https://www.bestblogs.dev"
BESTBLOGS_ISSUE = re.compile(r"^https?://(?:www\.)?bestblogs\.dev(?:/[a-z]{2})?/newsletter/issue(\d{1,4})/?$", re.I)


@dataclass
class Raw:
    category: str
    source: str
    title: str
    url: str
    published_at: datetime | None
    feed_url: str = ""
    content: str = ""


# ---- identity -----------------------------------------------------------------

def iso(dt):
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def parse_date(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    s = str(value).strip().removeprefix("$D")
    try:
        if re.fullmatch(r"\d{9,}(?:\.\d+)?", s) or isinstance(value, (int, float)):
            n = float(s)
            return datetime.fromtimestamp(n / 1000 if n > 1e10 else n, tz=UTC)
        dt = dtparser.parse(s, tzinfos={"UT": 0, "UTC": 0, "GMT": 0})
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)
    except Exception:
        return None


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
                           entry_content(e, link)))
    return out


def bridge(xml_url: str, html_url: str) -> tuple[str, str] | None:
    """(parser, page url) for rsshub Telegram/Jike routes: scrape the public page."""
    parts = [p for p in urlparse(xml_url).path.strip("/").split("/") if p]
    if urlparse(xml_url).netloc == "rsshub.app":
        if parts[:2] == ["telegram", "channel"] and len(parts) >= 3:
            return "telegram", f"https://t.me/s/{parts[2]}"
        if parts[:1] == ["jike"] and len(parts) >= 3 and parts[1] in ("topic", "user"):
            return "jike", f"https://m.okjike.com/{parts[1]}s/{parts[2]}"
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
            f["parser"], f["url"] = "rss", FEED_REPLACE.get(xml, xml)
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


def bestblogs_issue(n: int, prev: datetime | None):
    """(title, body html, date) of one issue page, or None if it doesn't exist.

    Date: the first YYYY-MM-DD on the page (the listing shows "Newsletter
    2024-06-12"); else article:published_time, a bare MM-DD, dated with the
    previous issue's year (rolling over when the month goes backwards)."""
    r = common.get(f"{BESTBLOGS}/newsletter/issue{n}", (CONNECT_TIMEOUT, 30), session())
    if r is None or r.status != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    meta = lambda prop: (soup.select_one(f'meta[property="{prop}"]') or {}).get("content") or ""
    title = meta("og:title") or (soup.title.get_text(" ", strip=True) if soup.title else "")
    title = re.sub(r"\s*\|\s*BestBlogs\.dev\s*$", "", title.strip())
    root = copy.copy(soup.select_one("main") or soup.select_one("article") or soup.body or soup)
    for t in root.find_all(("script", "style", "noscript", "nav", "header", "footer")):
        t.decompose()
    text = root.get_text(" ", strip=True)
    date = None
    if m := re.search(r"(20\d{2})-(\d{2})-(\d{2})(?!\d)", text):
        date = parse_date(m[0])
    elif prev and (m := re.search(r"(\d{1,2})-(\d{1,2})", meta("article:published_time"))):
        month, day = int(m[1]), int(m[2])
        # roll into the next year only on a real wrap (Dec -> Jan): the site's
        # own listing has out-of-order dates within a year (issue 101 < 100)
        year = prev.year + (prev.month - month > 6)
        try:
            date = datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            pass
    return title, (str(root) if len(text) >= 500 else ""), date


def fetch_bestblogs(archive: dict) -> list[Raw]:
    """Issues at /newsletter/issueN (the index is a JS shell). Known issues are
    re-emitted from storage so they stay inside retention -- refetched only
    while their date is unknown. New ones are fetched until 3 consecutive
    misses; the issue html becomes the feed copy, so summarize_feed never
    fetches the page again. Runs whether or not an OPML file is given."""
    item = lambda n, title, when, body="": Raw(
        "tech", "BestBlogs", title, f"{BESTBLOGS}/newsletter/issue{n}", when,
        content=body)
    known = {int(m[1]): r for r in archive.values()
             if (m := BESTBLOGS_ISSUE.match(str(r.get("url", "")))) and r.get("title")}
    out, prev, dated, new = [], None, 0, 0
    for n, r in sorted(known.items()):
        when = parse_date(r.get("published_at"))
        if when is None and (got := bestblogs_issue(n, prev)) and got[2]:
            when, dated = got[2], dated + 1
        prev = when or prev
        out.append(item(n, r["title"], when))
    n, misses = max(known, default=0) + 1, 0
    while n <= 500 and misses < 3:
        got = bestblogs_issue(n, prev)
        misses = 0 if got else misses + 1
        if got and got[0]:
            out.append(item(n, got[0], got[2], got[1]))
            prev, new = got[2] or prev, new + 1
        n += 1
    print(f"BestBlogs: {len(known)} known ({dated} newly dated), {new} new")
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
    if moved or len(keep) < len(doc["items"]):
        print(f"Re-canonicalised {moved} url(s), merged {len(doc['items']) - len(keep)} duplicate(s).")
    return doc, {r["id"]: r for r in keep if r.get("id")}


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
        if raw.content and not rec.get("summary") and not rec.get("feed_content"):
            rec["feed_content"] = raw.content


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
    raws += fetch_bestblogs(archive)
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
            r.pop("feed_content", None)
    kept.sort(key=lambda r: parse_date(r.get("last_seen_at")) or datetime.min.replace(tzinfo=UTC),
              reverse=True)
    doc = {"generated_at": iso(now), "items": kept}
    save_doc(path, doc)
    print(f"Wrote {path} ({len(kept)} items, fetched {len(raws)} raw)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
