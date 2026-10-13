"""Podcast episode pages -> their audio file (mp3 / m4a), for ASR.

小宇宙  www.xiaoyuzhoufm.com/episode/<id>   og:audio (.m4a) in the page
Firstory open.firstory.fm|me/story/<id>     an audio url in the page, else the
                                             <enclosure> of the show's RSS
                                             (feed.firstory.me/rss/user/<uid>),
                                             which the page links

Only the episode page and the feed it names are fetched. The audio url is
remote data: it must resolve to public addresses here, and download_sub fetches
it through common.download, which re-checks every hop.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass

import feedparser

import common
from common import UA, make_session, next_data, walk

EPISODE_RE = re.compile(
    r"^https?://(?:www\.)?xiaoyuzhoufm\.com/episode/([0-9a-f]{16,32})"
    r"|^https?://open\.firstory\.(?:fm|me)/story/([0-9a-z]{16,40})", re.I)
AUDIO_URL_RE = re.compile(r"""https?://[^\s"'<>\\]+?\.(?:mp3|m4a|aac)(?:\?[^\s"'<>\\]*)?(?=["'<\s\\]|$)""", re.I)
OG_AUDIO_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["']og:audio(?::url|:secure_url)?["'][^>]+content\s*=\s*["']([^"']+)"""
    r"""|<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]+(?:property|name)\s*=\s*["']og:audio(?::url|:secure_url)?["']""",
    re.I)
FIRSTORY_RSS_RE = re.compile(r"https?://feed\.firstory\.me/rss/user/[0-9a-z]{10,40}", re.I)
MINUTES_RE = re.compile(r"(\d{1,4})\s*分钟")
HMS_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2}):(\d{2})(?![\d:])")
TIMEOUT = (5, 30)
LANG = "zh-CN,zh-TW;q=0.9,en;q=0.8"
_session = make_session(1, {"User-Agent": UA, "Accept-Language": LANG})


@dataclass
class Episode:
    audio_url: str
    duration: float = 0.0      # seconds, 0 = unknown
    lang: str | None = None    # hint only; whisper detects


def is_episode(url: str) -> bool:
    return bool(EPISODE_RE.match(url or ""))


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


def _get(url: str) -> str:
    r = common.get(url, TIMEOUT, _session, lang=LANG)
    return r.text if r is not None and r.status == 200 else ""


def _audio_in_page(page: str) -> str:
    for m in OG_AUDIO_RE.finditer(page):
        return html_mod.unescape(m[1] or m[2])
    if m := AUDIO_URL_RE.search(page.replace("\\u002F", "/").replace("\\/", "/")):
        return html_mod.unescape(m[0])
    return ""


def _xiaoyuzhou(url: str, eid: str) -> Episode | None:
    page = _get(url)
    audio = _audio_in_page(page)
    if not audio:
        return None
    duration = 0.0
    for d in walk(next_data(page)):
        if d.get("eid") == eid and isinstance(d.get("duration"), (int, float)):
            duration = float(d["duration"])
            break
    if not duration and (m := MINUTES_RE.search(page)):
        duration = int(m[1]) * 60.0
    return Episode(audio, duration, "zh")


def _firstory(url: str, sid: str) -> Episode | None:
    page = _get(url)
    duration = 0.0
    if m := HMS_RE.search(re.sub(r"<[^>]+>", " ", page)):      # "2024-06-04 00:43:08"
        duration = hms(m[0])
    if audio := _audio_in_page(page):
        return Episode(audio, duration, "zh")
    feed = FIRSTORY_RSS_RE.search(page)
    if not feed:
        return None
    parsed = feedparser.parse(_get(feed[0]))
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
    ep = _xiaoyuzhou(url, m[1]) if m[1] else _firstory(url, m[2])
    if ep and common.safe_url(ep.audio_url):
        return ep
    return None
