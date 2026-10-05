"""Get an article's text: direct fetch, browser-fingerprint fetch, the feed's
own copy, a reader proxy, then the Wayback Machine."""

from __future__ import annotations

import html as html_mod
import json
import os
import re
from typing import NamedTuple

import trafilatura
from bs4 import BeautifulSoup

import thumbs
from urllib.parse import quote

import common
import lang
from common import UA, host_in, make_session

TIMEOUT, TIMEOUT_SLOW = 30, 60
MIN_BODY = 200               # chars below which a body is only a blurb
SLOW_HOSTS = ("mckinsey.com", "bcg.com", "deloitte.com", "hbr.org")
READER = os.environ.get("READER_PROXY", "https://r.jina.ai/")
USE_READER = os.environ.get("USE_READER_PROXY", "on").lower() != "off"
USE_WAYBACK = os.environ.get("USE_WAYBACK", "on").lower() != "off"
# Also escalate meta-only pages to the reader proxy (costly: many pages are).
READER_ON_META = os.environ.get("READER_ON_META", "").lower() in ("1", "true", "yes", "on")
LANG = "zh-TW,zh;q=0.9,en;q=0.8"
HEADERS = {
    "User-Agent": UA, "Accept-Language": LANG, "Accept-Encoding": "gzip, deflate, br",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    # Akamai stalls requests whose header set doesn't look like a navigation.
    "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document", "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="126", "Not:A-Brand";v="24", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
}
session = make_session(3, HEADERS)

# Interstitials / challenge pages / generic site blurbs, in both scripts: the
# test runs on the *unconverted* source text.
JUNK_RE = re.compile(
    r"Just a moment|Checking your browser|Verify you are human|Enable JavaScript and cookies"
    r"|Please enable (?:JS|JavaScript|cookies)|(?:Access|Permission) [Dd]enied"
    r"|You don'?t have permission to access|Why have I been blocked|Cloudflare Ray ID"
    r"|Attention Required|Request unsuccessful|cf-challenge|cf_chl"
    r"|needs to review the security of your connection"
    r"|protect itself from (?:online attacks|malicious bots)"
    r"|verif(?:y|ies) (?:that )?(?:you are|you're) not a (?:ro)?bot|[Mm]aking sure you'?re not a bot"
    r"|Anubis (?:to protect|has protected)"
    r"|Comprehensive up-to-date news coverage, aggregated from sources"
    r"|豆瓣[\sa-zA-Z.]{0,24}(?:載入中|载入中|加載中|加载中)"
    r"|安全验证|安全驗證|验证码|驗證碼|禁止访问|禁止訪問|访问异常|異常流量|异常流量"
    r"|正在驗證您的請求|正在验证您的请求|該網站使用安全服務|该网站使用安全服务"
    r"|正在確認你是不是機器人|正在确认你是不是机器人", re.I)
BLOCK_STATUS = {401, 403, 407, 418, 429, 451}


def is_junk(text: str) -> bool:
    return bool(text) and bool(JUNK_RE.search(text[:4000]))


class Fetched(NamedTuple):
    text: str | None
    kind: str | None        # body | meta | blocked | gone | None (transient)
    table: bool = False
    code: bool = False


# ---- small text fixes -------------------------------------------------------

def unescape(text: str) -> str:
    return html_mod.unescape(text or "").replace("\u00a0", " ").replace("\u200b", "")


_LEAK = [(re.compile(p, f), r) for p, f, r in (
    (r"<!--.*?-->", re.S, ""),
    (r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.S | re.I, ""),
    (r"</?(?:script|style|noscript)\b[^>]*>", re.I, ""),
    (r"<\s*(?:meta|link|base|source|track|param|input|img|iframe|col|area|embed|wbr)\b[^>]*/?>", re.I, ""),
    (r"<\s*br\s*/?\s*>", re.I, "\n"),
)]
_ORPHAN_TAIL = re.compile(r'\]\(\s*(?:"[^"\n]{0,120}")?\s*\)')      # `]("permalink")`


def squeeze(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def sanitize_markup(text: str) -> str:
    """HTML and markdown fragments that survived extraction."""
    if not text:
        return text
    for rx, rep in _LEAK:
        text = rx.sub(rep, text)
    while True:
        new = _ORPHAN_TAIL.sub("", text)
        if new == text:
            break
        text = new
    return squeeze(text)


_MD_LINK = re.compile(r"\[([^\]\n]*?)\]\((?:https?:|/|#|mailto:)[^)\s]*\)")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)"
                     r"|\[\s*!?\s*\[?\s*(?:Image|圖片|图片|圖像|图像|Figure|插圖|插图)"
                     r"\s*\d*\s*[:：]?[^\]]*\](?:\([^)]*\))?\s*\]?", re.I)
_MD = [(re.compile(p, f), r) for p, f, r in (
    (r"^\s*(?:[-*_]\s*){3,}$", re.M, ""),
    (r"^\s{0,3}#{1,6}\s*", re.M, ""),
    (r"^\s{0,6}(?:[-*+]|\d{1,2}[.)])\s+", re.M, ""),
    (r"<?https?://[^\s)\]<>，。）]+>?", 0, ""),
    (r"\*\*|__|\*|`|~~", 0, ""),
    (r"^[\s\]\[)(|:-]+|[\s\[(|]+$", re.M, ""),
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
    (r"^\s*(?:作者|標題|時間|看板)[\s:：].*$", re.M, ""),
    (r"^\s*※\s*(?:發信站|文章網址|編輯\s*[:：]|伸謝)[^\n]*$", re.M, ""),
    (r"(?:^|(?<=[\s。！？]))\s*(?:推|噓|嘘|→)\s*[A-Za-z0-9_]{2,20}\s*[:：]\s*", 0, "\n"),
    # ip + date + time on every push line reads as fact-dense to the scorer
    (r"\s*(?:\d{1,3}(?:\.\d{1,3}){3})?\s*\d{2}/\d{2}\s+\d{2}:\d{2}\s*", 0, "\n"),
    (r"\s*\d{1,3}(?:\.\d{1,3}){3}\s*", 0, " "),
    (r"[ \t]{2,}", 0, " "),
    (r"\n{2,}", 0, "\n"),
)]


def clean_ptt(text: str) -> str:
    for rx, rep in _PTT:
        text = rx.sub(rep, text)
    return text.strip()


# ---- HTML -> text -----------------------------------------------------------
_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r"""\b([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_DESC_KEYS = ("og:description", "description", "twitter:description")


def meta_description(html: str) -> str:
    """Attributes are read per tag: a single cross-tag regex once paired one
    tag's content with the next tag's name."""
    found = {}
    for tag in _META_TAG.findall(html or ""):
        a = {m[0].lower(): m[1] or m[2] or m[3] for m in _ATTR.findall(tag)}
        key = (a.get("property") or a.get("name") or "").lower()
        val = a.get("content", "").strip()
        if key in _DESC_KEYS and val and key not in found:
            found[key] = re.sub(r"\s+", " ", val)
    return next((found[k] for k in _DESC_KEYS if k in found), "")


_CODE_TAG = re.compile(r"<(?:pre|samp|kbd)[\s>]|<code[\s>]", re.I)


def strip_code(html: str) -> tuple[str, bool]:
    """Remove code blocks before extraction; the summary notes them instead."""
    if not _CODE_TAG.search(html):
        return html, False
    soup = BeautifulSoup(html, "html.parser")
    removed = 0
    for tag in soup(["pre", "samp", "kbd"]):
        tag.decompose()
        removed += 1
    for tag in soup("code"):
        if tag.parent is None:
            continue
        text = tag.get_text()
        if len(text.strip()) > 40 or "\n" in text:
            tag.decompose()
            removed += 1
        else:
            tag.replace_with(text.strip())
    return str(soup), bool(removed)


def _bs4_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    scope = soup.find("article") or soup.find("main") or soup
    paras = (p.get_text(" ", strip=True) for p in scope.find_all(["p", "li"]))
    return "\n".join(p for p in paras if len(p) >= 20 and not p.startswith(("©", "Powered by")))


def from_html(html: str) -> tuple[str, str, bool, bool]:
    """(body, meta, has_table, has_code). has_table is scoped to the article
    region trafilatura identifies, not to any <table> in the page chrome."""
    table = False
    if "<table" in html.lower():
        try:
            xml = trafilatura.extract(html, include_comments=False, include_tables=True,
                                      favor_recall=True, output_format="xml")
            table = bool(xml) and "<table" in xml.lower()
        except Exception:
            pass
    html, code = strip_code(html)
    body = trafilatura.extract(html, include_comments=False, include_tables=False,
                               favor_recall=True) or ""
    body = lang.fix_mojibake(unescape(body))
    if len(body) < MIN_BODY:
        alt = lang.fix_mojibake(unescape(_bs4_text(html)))
        if len(alt) > len(body):
            body = alt
    meta = lang.fix_mojibake(unescape(meta_description(html)))
    # Text that is still mojibake is not content: drop it so the next
    # strategy (reader proxy, Wayback) gets a chance.
    return ("" if lang.garbled(body) else body), ("" if lang.garbled(meta) else meta), table, code


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
    r = common.get(url, timeout, session, impersonate, LANG)
    if r is None:
        return None, "fail"
    if r.status in BLOCK_STATUS:
        return None, "blocked"
    if r.status in (404, 410):
        return None, "notfound"
    return (r.text, "ok") if r.status < 400 else (None, "fail")


def via_reader(url: str) -> str | None:
    if not USE_READER:
        return None
    text, status = http_get(READER.rstrip("/") + "/" + url, TIMEOUT_SLOW)
    if status != "ok" or not text:
        return None
    text = lang.fix_mojibake(text)
    if "Markdown Content:" in text[:1000]:           # drop the reader's preamble
        text = text.split("Markdown Content:", 1)[1]
    text = clean_markdown(text)
    return None if is_junk(text) or lang.garbled(text) else text or None


def via_wayback(url: str) -> str | None:
    if not USE_WAYBACK:
        return None
    r = common.get("https://archive.org/wayback/available?url=" + quote(url, safe=""), TIMEOUT, session)
    try:
        snap = (json.loads(r.content).get("archived_snapshots") or {}).get("closest") or {}
    except Exception:
        return None
    snap_url = str(snap.get("url") or "")
    if not snap.get("available") or not host_in(snap_url, ("archive.org",)):
        return None
    # id_ = the original bytes, without the archive's banner
    html, status = http_get(re.sub(r"(/web/\d+)/", r"\1id_/", snap_url, count=1), TIMEOUT_SLOW)
    return html if status == "ok" else None


def fetch(url: str, feed_text: str = "", found: dict | None = None) -> Fetched:
    """Try every strategy, cheapest first. `found["thumbnail"]` is filled from
    any page HTML seen on the way."""
    found = {} if found is None else found
    slow = host_in(url, SLOW_HOSTS)
    best = Fetched(None, None)
    blocked = notfound = False

    def consider(html: str) -> Fetched | None:
        nonlocal best, blocked
        if not found.get("thumbnail"):
            found["thumbnail"] = thumbs.extract(html, url)
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
            print(f"    recovered via {how}")
        if res.text and "ptt.cc" in url:
            text = clean_ptt(res.text)
            res = res._replace(text=text, kind="meta" if len(text) < MIN_BODY else res.kind)
        return res

    for imp in ([True] if slow else [False, True]):
        html, status = http_get(url, TIMEOUT_SLOW if slow else TIMEOUT, imp)
        if status == "blocked":
            blocked = True
        elif status == "notfound":
            notfound = True
            break
        elif html and (res := consider(html)):
            return done(res, "curl_cffi" if imp and not slow else "")

    feed_text = lang.fix_mojibake(unescape(feed_text).strip())
    if lang.garbled(feed_text):
        feed_text = ""
    if len(feed_text) >= MIN_BODY:
        return done(Fetched(feed_text, "body"), "feed content")
    if best.kind == "meta" and not READER_ON_META:
        return done(best)
    if (text := via_reader(url)) and len(text) >= MIN_BODY:
        return done(Fetched(text, "body"), "reader proxy")
    if (html := via_wayback(url)) and (res := consider(html)):
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
