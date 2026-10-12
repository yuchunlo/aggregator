"""Podcast episode pages -> their audio file (mp3 / m4a), for ASR.

小宇宙  www.xiaoyuzhoufm.com/episode/<id>   og:audio (.m4a) in the page
Firstory open.firstory.fm|me/story/<id>     an audio url in the page, else the
                                             <enclosure> of the show's RSS
                                             (feed.firstory.me/rss/user/<uid>),
                                             which the page links

Only the episode page and the feed it names are fetched; the audio url must
point at a known CDN host or carry an audio extension.
"""

from __future__ import annotations

import html as html_mod
import json
import re
from dataclasses import dataclass

import feedparser

import common
from common import UA, host_in, make_session

EPISODE_RE = re.compile(
    r"^https?://(?:www\.)?xiaoyuzhoufm\.com/episode/([0-9a-f]{16,32})"
    r"|^https?://open\.firstory\.(?:fm|me)/story/([0-9a-z]{16,40})", re.I)
AUDIO_URL_RE = re.compile(r"""https?://[^\s"'<>\\]+?\.(?:mp3|m4a|aac)(?:\?[^\s"'<>\\]*)?(?=["'<\s\\]|$)""", re.I)
OG_AUDIO_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["']og:audio(?::url|:secure_url)?["'][^>]+content\s*=\s*["']([^"']+)"""
    r"""|<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]+(?:property|name)\s*=\s*["']og:audio(?::url|:secure_url)?["']""",
    re.I)
FIRSTORY_RSS_RE = re.compile(r"https?://feed\.firstory\.me/rss/user/[0-9a-z]{10,40}", re.I)
NEXT_DATA_RE = re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)
MINUTES_RE = re.compile(r"(\d{1,4})\s*分钟")
HMS_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2}):(\d{2})(?![\d:])")
TIMEOUT = (5, 30)


@dataclass
class Episode:
    audio_url: str
    duration: float = 0.0      # seconds, 0 = unknown
    lang: str | None = None    # hint only; whisper detects


def is_episode(url: str) -> bool:
    return bool(EPISODE_RE.match(url or ""))


def episode_id(url: str) -> str | None:
    m = EPISODE_RE.match(url or "")
    return (m[1] or m[2]) if m else None


def hms(value) -> float:
    """itunes:duration -- '2588', '43:08' or '00:43:08' -> seconds."""
    s = str(value or "").strip()
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return 0.0
    if not parts or len(parts) > 3:
        return 0.0
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return max(sec, 0.0)


def _session():
    return make_session(1, {"User-Agent": UA, "Accept-Language": "zh-CN,zh-TW;q=0.9,en;q=0.8"})


def _get(url: str, s) -> str:
    r = common.get(url, TIMEOUT, s, lang="zh-CN,zh-TW;q=0.9,en;q=0.8")
    return r.text if r is not None and r.status == 200 else ""


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            yield n
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)


def _next_data(page: str):
    m = NEXT_DATA_RE.search(page)
    try:
        return json.loads(m[1]) if m else None
    except ValueError:
        return None


def _audio_in_page(page: str) -> str:
    for m in OG_AUDIO_RE.finditer(page):
        return html_mod.unescape(m[1] or m[2])
    if m := AUDIO_URL_RE.search(page.replace("\\u002F", "/").replace("\\/", "/")):
        return html_mod.unescape(m[0])
    return ""


def _xiaoyuzhou(url: str, eid: str, s) -> Episode | None:
    page = _get(url, s)
    audio = _audio_in_page(page)
    if not audio:
        return None
    duration = 0.0
    for d in _walk(_next_data(page)):
        if d.get("eid") == eid and isinstance(d.get("duration"), (int, float)):
            duration = float(d["duration"])
            break
    if not duration and (m := MINUTES_RE.search(page)):
        duration = int(m[1]) * 60.0
    return Episode(audio, duration, "zh")


def _firstory(url: str, sid: str, s) -> Episode | None:
    page = _get(url, s)
    duration = 0.0
    if m := HMS_RE.search(re.sub(r"<[^>]+>", " ", page)):      # "2024-06-04 00:43:08"
        duration = hms(m[0])
    if audio := _audio_in_page(page):
        return Episode(audio, duration, "zh")
    feed = FIRSTORY_RSS_RE.search(page)
    if not feed:
        return None
    parsed = feedparser.parse(_get(feed[0], s))
    for e in parsed.entries:
        where = " ".join(str(e.get(k, "")) for k in ("id", "link", "guid"))
        where += " " + " ".join(str(l.get("href", "")) for l in e.get("links", []))
        if sid.lower() not in where.lower():
            continue
        for l in list(e.get("enclosures", [])) + list(e.get("links", [])):
            href = l.get("href") or l.get("url") or ""
            if href and (str(l.get("type", "")).startswith("audio") or AUDIO_URL_RE.match(href)):
                return Episode(href, hms(e.get("itunes_duration")) or duration, "zh")
    return None


def resolve(url: str) -> Episode | None:
    """The episode's audio, or None (not an episode page / nothing found)."""
    m = EPISODE_RE.match(url or "")
    if not m:
        return None
    s = _session()
    ep = _xiaoyuzhou(url, m[1], s) if m[1] else _firstory(url, m[2], s)
    # yt-dlp connects outside common.get: the audio url (remote data) must
    # pass the same public-address check before it is handed over.
    if ep and common.safe_url(ep.audio_url):
        return ep
    return None
