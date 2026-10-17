"""Text: HTML readers and page extraction (HTML -> body / meta), Chinese
script conversion, language detection, mojibake, machine translation (DeepL,
then gtx), caption parsing, boilerplate removal and extractive summaries."""

from __future__ import annotations

import bisect
import html as html_mod
import math
import re
import threading
import time
from collections import Counter
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import trafilatura

import net
import store
from store import BLANK_SUMMARY, FALLBACK_MARK

# ---- HTML helpers -------------------------------------------------------------
# One way to read meta tags and one way to turn feed/page HTML into text.
# Attributes are read per tag, so a value can never pair with another tag's
# name, and nothing here backtracks across the document.
def meta_tags(html: str, keys, limit: int | None = None) -> list[tuple[str, str]]:
    """[(key, content)] of <meta property|name=key content=...>, document order, raw values."""
    keys = {k.lower() for k in keys}
    out = []
    for _, a in store.iter_tags(html, ("meta",), limit):
        key = (a.get("property") or a.get("name") or "").lower()
        if key in keys and (val := (a.get("content") or "").strip()):
            out.append((key, val))
    return out


def meta_first(html: str, keys) -> str:
    """The first value of the first key (in `keys` order) that a page sets."""
    found = {}
    for k, v in meta_tags(html, keys):
        found.setdefault(k, v)
    return next((found[k.lower()] for k in keys if k.lower() in found), "")


def page_title(html: str) -> str:
    """og:title, else <title>, unescaped and on one line."""
    t = meta_first(html, ("og:title",)) or (store.tag_text(html, "title", {}, 2000) or "")
    return re.sub(r"\s+", " ", html_mod.unescape(t)).strip()


# ---- streaming HTML readers ---------------------------------------------------
# One parser core, two outputs: html_to_text (every block, one line each) and
# paragraph_text (<p>/<li> only, scoped to <article>/<main>). Both are single
# passes with no tree: a tree builder's append() walks to the last descendant
# and get_text() per paragraph rescans nested ones, both quadratic on deeply
# nested markup.
class _Stream(HTMLParser):
    """Skip depth for tags whose text is never content; everything else goes
    to open()/shut()."""
    SKIP: frozenset = frozenset()

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        else:
            self.open(tag)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        else:
            self.shut(tag)

    def open(self, tag):
        pass

    def shut(self, tag):
        pass


_BLOCKS = frozenset(("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section"))


class _Text(_Stream):
    """Every block on its own line; a stack of code buffers drops multi-line code."""
    SKIP = frozenset(("script", "style", "pre"))

    def __init__(self):
        super().__init__()
        self.out, self.code = [], []

    def _emit(self, s):
        if not self.skip:
            if self.code:
                self.code[-1].append(s)
            else:
                self._sink(s)

    def _sink(self, s):
        self.out.append(s)

    def open(self, tag):
        if tag == "code":
            self.code.append([])
        elif tag == "br":
            self._emit("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._emit("\n")

    def shut(self, tag):
        if tag == "code" and self.code:
            self._close_code()
        elif tag in _BLOCKS:
            self._emit("\n")

    def _close_code(self):
        text = "".join(self.code.pop())
        if "\n" not in text:                     # multi-line code is dropped
            self._emit(text)
        elif self.code:                           # ...and so is any code around it
            self.code[-1].append("\n")

    def handle_data(self, data):
        self._emit(data)

    def text(self) -> str:
        self.close()
        while self.code:
            self._close_code()
        return "".join(self.out)


def html_to_text(raw: str) -> str:
    """Feed / page HTML to text, one line per block: scripts, styles and
    multi-line code dropped. Linear in the input."""
    p = _Text()
    p.feed(raw or "")
    return _tidy(p.text())


def region_text(raw: str, scopes=("main", "article", "body")) -> str:
    """Like html_to_text, scoped to the first element of the first of `scopes`
    the page has (else the whole page), with nav/header/footer/noscript left
    out. One pass: every scope collects its text as it goes."""
    p = _Region(scopes)
    p.feed(raw or "")
    return _tidy(p.text())


def _tidy(text: str) -> str:
    text = text.replace("\u00a0", " ")
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


class _Region(_Text):
    """_Text, also copying what it emits into the open scope buffers."""
    SKIP = _Text.SKIP | frozenset(("nav", "header", "footer", "noscript"))

    def __init__(self, scopes):
        super().__init__()
        self.scopes = tuple(scopes)
        self.bufs: dict[str, list | None] = dict.fromkeys(self.scopes)
        self.depth = dict.fromkeys(self.scopes, 0)

    def open(self, tag):
        if tag in self.depth and not self.skip:
            if self.bufs[tag] is None:
                self.bufs[tag], self.depth[tag] = [], 1
            elif self.depth[tag]:
                self.depth[tag] += 1
        super().open(tag)

    def shut(self, tag):
        super().shut(tag)
        if self.depth.get(tag) and not self.skip:
            self.depth[tag] -= 1

    def _sink(self, s):
        self.out.append(s)
        for k, d in self.depth.items():
            if d:
                self.bufs[k].append(s)

    def text(self) -> str:
        whole = super().text()
        return next(("".join(b) for k in self.scopes if (b := self.bufs[k]) is not None), whole)


class _Paras(_Stream):
    """<p>/<li> text cut into paragraphs at <p>/<li> boundaries; page chrome is
    skipped; the first <article>, else <main>, scopes the result when it holds
    any paragraph (one inside skipped chrome never counts)."""
    SKIP = frozenset(("script", "style", "nav", "header", "footer", "aside", "form"))
    PARA = frozenset(("p", "li"))

    def __init__(self):
        super().__init__()
        self.depth, self.cur, self.paras = {"article": 0, "main": 0}, None, []
        self.seen = {"article": 0, "main": 0}        # 1 inside / 2 after the first one

    def _flush(self):
        if self.cur is not None:
            text = " ".join(self.cur[0])
            if text:
                self.paras.append((text, *self.cur[1:]))
        self.cur = None

    def open(self, tag):
        if tag in self.depth:
            if self.skip:                            # an <article> card inside <aside>
                return
            self.depth[tag] += 1
            if not self.seen[tag]:
                self.seen[tag] = 1
        elif tag in self.PARA and not self.skip:
            self._flush()
            self.cur = ([], self.seen["article"] == 1, self.seen["main"] == 1)

    def shut(self, tag):
        if tag in self.depth and self.skip:
            return
        if tag in self.depth and self.depth[tag]:
            self.depth[tag] -= 1
            if not self.depth[tag] and self.seen[tag] == 1:
                self.seen[tag] = 2
        if tag in self.PARA or tag in self.depth:
            self._flush()

    def handle_data(self, data):
        if self.cur is not None and not self.skip and (d := data.strip()):
            self.cur[0].append(d)

    def result(self) -> list[str]:
        self.close()
        self._flush()
        for k in (1, 2):                             # article, then main, then all
            if scoped := [p[0] for p in self.paras if p[k]]:
                return scoped
        return [p[0] for p in self.paras]


def paragraph_text(html: str) -> str:
    """The page's paragraphs, one per line: the fallback when trafilatura finds
    too little."""
    p = _Paras()
    p.feed(html or "")
    return "\n".join(t for t in p.result() if len(t) >= 20 and not t.startswith(("©", "Powered by")))


# ---- page extraction ----------------------------------------------------------
# HTML -> (body, meta, has_table, has_code) for summarize.fetch_article.
MIN_BODY = 200               # chars below which a body is only a blurb


def unescape(text: str) -> str:
    return html_mod.unescape(text or "").replace("\u00a0", " ").replace("\u200b", "")


@lru_cache(maxsize=1)
def chrome_re() -> re.Pattern:
    """rules.json `qc_chrome`: interface text (app banners, sign-in bars) that
    is not content. Spaces in a pattern match any whitespace. Used at
    extraction (chrome_only) and by summary QC."""
    pats = (store.rules().get("qc_chrome") or {}).get("patterns") or ()
    return re.compile("|".join(p.replace(" ", r"\s*") for p in pats) or r"(?!x)x", re.I)


def chrome_only(body: str) -> bool:
    """True when `body` has chrome and too little text remains without it.
    A separate verdict from is_junk: chrome means "no body here, try the next
    strategy", never "the host refused us" (which pauses the host)."""
    rx = chrome_re()
    if not body or not rx.search(body):
        return False
    return len(re.sub(r"\s+", " ", rx.sub(" ", body)).strip()) < MIN_BODY


_DESC_KEYS = ("og:description", "description", "twitter:description")


def meta_description(html: str) -> str:
    return re.sub(r"\s+", " ", meta_first(html or "", _DESC_KEYS))


_CODE_TAG = re.compile(r"<(?:pre|samp|kbd)[\s>]|<code[\s>]", re.I)


def strip_code(html: str) -> tuple[str, bool]:
    """Remove code blocks before extraction; the summary notes them instead.
    Outermost <pre>/<samp>/<kbd>/<code> only: a block is cut, and so is a
    <code> that holds one, is long or spans lines; a short inline <code>
    keeps its text. The output is the source sliced around the cuts, never
    re-serialised, so everything else reaches trafilatura byte for byte."""
    if not _CODE_TAG.search(html):
        return html, False
    els = [e for e in elements(html) if e.tag in _CODE_BLOCKS or e.tag == "code"]
    blocks = [e.start for e in els if e.tag in _CODE_BLOCKS]          # sorted: document order
    out, pos, removed = [], 0, 0
    for e in outermost(els):
        out.append(html[pos:e.start])
        pos = e.end
        text = e.text("")
        k = bisect.bisect_right(blocks, e.start)    # the first block opening inside e, if any
        holds_block = k < len(blocks) and blocks[k] < e.end
        if e.tag != "code" or holds_block or len(text.strip()) > 40 or "\n" in text:
            removed += 1
        else:
            out.append(html_mod.escape(text.strip(), quote=False))
    out.append(html[pos:])
    return "".join(out), bool(removed)


_CODE_BLOCKS = frozenset(("pre", "samp", "kbd"))


# ---- element spans ------------------------------------------------------------
# A flat list of elements with their offsets in the source, from one pass of
# the same _Stream core, for callers that need raw HTML slices rather than
# text: enough for "first <main>", "drop every <nav>" and "the text of each
# <a>" without building a tree. Unclosed elements end where an
# enclosing one closes, or at the end of the document; a stray end tag with no
# open element of that name is ignored (counted per name, so never a scan).
VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                   "meta", "param", "source", "track", "wbr"))


class Element:
    __slots__ = ("tag", "attrs", "start", "end", "_data", "_d0", "_d1")

    def __init__(self, tag, attrs, start, data):
        self.tag, self.attrs, self.start, self.end = tag, attrs, start, start
        self._data, self._d0, self._d1 = data, len(data), len(data)

    def text(self, sep: str = " ") -> str:
        """Every text node inside, stripped and joined by `sep` (empty
        nodes dropped); sep "" keeps the nodes as they are."""
        parts = self._data[self._d0:self._d1]
        return "".join(parts) if sep == "" else sep.join(t for t in (x.strip() for x in parts) if t)

    def has_class(self, part: str) -> bool:
        """Substring of the class attribute, on purpose: CSS-module names
        such as `index_contentWrap__Jv8jK` carry the part inside a token."""
        return part in (self.attrs.get("class") or "")


class _Spans(_Stream):
    """The _Stream core (nothing skipped), recording where each element sits."""

    def __init__(self, html: str):
        super().__init__()
        self.html, self.els, self.data, self.stack = html, [], [], []
        self.open_count: Counter = Counter()
        self.lines = [0] + [m.end() for m in re.finditer("\n", html)]

    def _pos(self) -> int:
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def _close(self, e: Element, end: int):
        e.end, e._d1 = end, len(self.data)
        self.open_count[e.tag] -= 1

    def handle_starttag(self, tag, attrs):
        self._attrs = attrs
        super().handle_starttag(tag, attrs)          # -> open()

    def open(self, tag):
        start = self._pos()
        e = Element(tag, {k: v or "" for k, v in self._attrs}, start, self.data)
        self.els.append(e)
        if tag in VOID:
            e.end = start + len(self.get_starttag_text() or "")
        else:
            self.stack.append(e)
            self.open_count[tag] += 1

    def handle_startendtag(self, tag, attrs):
        start = self._pos()
        e = Element(tag, {k: v or "" for k, v in attrs}, start, self.data)
        e.end = start + len(self.get_starttag_text() or "")
        self.els.append(e)

    def shut(self, tag):
        if self.open_count[tag] <= 0:
            return
        start = self._pos()
        gt = self.html.find(">", start)
        end = gt + 1 if gt >= 0 else len(self.html)
        while self.stack:
            e = self.stack.pop()
            self._close(e, start if e.tag != tag else end)
            if e.tag == tag:
                break

    def handle_data(self, data):
        self.data.append(data)

    def finish(self) -> list[Element]:
        self.close()
        while self.stack:
            self._close(self.stack.pop(), len(self.html))
        return self.els


class Doc:
    """The elements of one document, in order, and its text nodes."""
    __slots__ = ("els", "_data")

    def __init__(self, els: list[Element], data: list[str]):
        self.els, self._data = els, data

    def __iter__(self):
        return iter(self.els)

    def first(self, *tests) -> Element | None:
        """The first element passing the first test any element passes."""
        return next((e for t in tests for e in self.els if t(e)), None)

    def text(self) -> str:
        """Every text node of the document, joined by spaces."""
        return " ".join(self._data)


def elements(html: str) -> Doc:
    """Every element of `html` in document order (see Element)."""
    p = _Spans(html or "")
    p.feed(html or "")
    return Doc(p.finish(), p.data)


def outermost(els) -> list[Element]:
    """Those of `els` (in document order) not inside another of them."""
    out, end = [], -1
    for e in els:                                   # elements() order: by start
        if e.start >= end:
            out.append(e)
            end = e.end
    return out


def _extract(html: str, **kw) -> str:
    """trafilatura on untrusted markup: any failure is "found nothing"."""
    try:
        return trafilatura.extract(html, include_comments=False, favor_recall=True, **kw) or ""
    except Exception:
        return ""


def from_html(html: str) -> tuple[str, str, bool, bool]:
    """(body, meta, has_table, has_code). has_table is scoped to the article
    region trafilatura identifies, not to any <table> in the page chrome."""
    html = html or ""
    table = "<table" in store.ascii_lower(html) and "<table" in store.ascii_lower(
        _extract(html, include_tables=True, output_format="xml"))
    html, code = strip_code(html)
    body = fix_mojibake(unescape(_extract(html, include_tables=False)))
    if len(body) < MIN_BODY:
        alt = fix_mojibake(unescape(paragraph_text(html)))
        if len(alt) > len(body):
            body = alt
    if chrome_only(body):                       # site chrome is not content: next strategy
        body = ""
    meta = fix_mojibake(unescape(meta_description(html)))
    # Text that is still mojibake is not content: drop it so the next
    # strategy (reader proxy, Wayback) gets a chance.
    return ("" if garbled(body) else body), ("" if garbled(meta) else meta), table, code


# ---- shared text primitives -------------------------------------------------
# Used by page extraction, summary building and summary QC (summarize.py).
# Interstitials / challenge pages / generic site blurbs, in either script: the
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
def is_junk(text: str) -> bool:
    return bool(text) and bool(JUNK_RE.search(text[:4000]))


# `[^<>]*+` stops at the next `<`, so every scan is bounded by the distance to
# the next tag start: linear however many tags are left unclosed.
_LEAK = [(re.compile(p, f), r) for p, f, r in (
    (r"</?(?:script|style|noscript)\b[^<>]*+>", re.I, ""),
    (r"<\s*+(?:meta|link|base|source|track|param|input|img|iframe|col|area|embed|wbr)\b[^<>]*+>", re.I, ""),
    (r"<\s*+br\s*+/?\s*+>", re.I, "\n"),
)]
_ORPHAN_TAIL = re.compile(r'\]\(\s*+(?:"[^"\n]{0,120}")?\s*+\)')      # `](\"permalink\")`


def _cut_spans(text: str, start: str, end: str) -> str:
    """Remove every `start`...`end` span (ASCII case-insensitive): linear. An
    end tag reaches its `>`. An unclosed `start` is left as it is. Offsets come
    from `text` itself: str.lower() can change the length ('İ')."""
    low, out, pos = store.ascii_lower(text), [], 0
    while (i := low.find(start, pos)) >= 0:
        j = low.find(end, i + len(start))
        if j < 0:
            break
        if end.startswith("</"):
            k = low.find(">", j)
            k = len(text) if k < 0 else k + 1
        else:
            k = j + len(end)
        out.append(text[pos:i])
        pos = k
    return "".join(out) + text[pos:]


def squeeze(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def sanitize_markup(text: str) -> str:
    """HTML and markdown fragments that survived extraction."""
    if not text:
        return text
    text = _cut_spans(text, "<!--", "-->")
    for tag in ("script", "style", "noscript"):
        text = _cut_spans(text, "<" + tag, "</" + tag)
    for rx, rep in _LEAK:
        text = rx.sub(rep, text)
    while True:
        new = _ORPHAN_TAIL.sub("", text)
        if new == text:
            break
        text = new
    return squeeze(text)


# ---- OpenCC -----------------------------------------------------------------
try:
    import opencc
    _S2TWP, _T2S = opencc.OpenCC("s2twp"), opencc.OpenCC("t2s")
except Exception:                      # degrade to identity, never to 簡體
    opencc = _S2TWP = _T2S = None


def _doubled_repairs():
    """s2twp maps e.g. 程序->程式 even inside text that already reads 程式,
    producing 程式式. Build the inverse fix from OpenCC's own phrase table."""
    if not opencc:
        return []
    try:
        path = Path(opencc.__file__).parent / "dictionary" / "TWPhrases.txt"
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                src, dst = parts[0], parts[1].split(" ")[0]
                if src and src != dst and src in dst:
                    out.append((dst.replace(src, dst), dst))
        return out
    except Exception:
        return []


_REPAIRS = _doubled_repairs()


def _repair(text: str) -> str:
    for bad, good in _REPAIRS:
        while bad in text:
            text = text.replace(bad, good)
    return text


def variant(text: str) -> str:
    """'hant' or 'hans', judged by how much t2s would change the Han chars."""
    if not _T2S:
        return "hant"
    sample = "".join(c for c in text or "" if "\u4e00" <= c <= "\u9fff")[:400]
    if not sample:
        return "hant"
    changed = sum(a != b for a, b in zip(sample, _T2S.convert(sample)))
    return "hant" if changed / len(sample) > 0.05 else "hans"


def to_twp(text: str) -> str:
    if not text or not _S2TWP:
        return text
    return _repair(text if variant(text) == "hant" else _S2TWP.convert(text))


_FOLD = str.maketrans({"臺": "台", "着": "著", "裏": "裡"})


@lru_cache(maxsize=8192)
def key_text(text: str) -> str:
    """Normalised form for rule matching: always 繁體, common variants folded."""
    if not text:
        return text
    return (_repair(_S2TWP.convert(text)) if _S2TWP else text).translate(_FOLD)


# ---- detection --------------------------------------------------------------
# Kana share among CJK separates Japanese from Chinese cleanly: measured real
# Japanese summaries sit at 0.515-0.710, the highest Chinese one at 0.171.
SCRIPTS = {
    "han": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]"), "kana": re.compile(r"[\u3040-\u30ff]"),
    "hangul": re.compile(r"[\uac00-\ud7af]"), "cyrillic": re.compile(r"[\u0400-\u04ff]"),
    "greek": re.compile(r"[\u0370-\u03ff]"), "hebrew": re.compile(r"[\u0590-\u05ff]"),
    "arabic": re.compile(r"[\u0600-\u06ff]"), "devanagari": re.compile(r"[\u0900-\u097f]"),
    "thai": re.compile(r"[\u0e00-\u0e7f]"),
}
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
KANA_SHARE_JA = 0.35
MIN_KANA_JA = 8
ZH_MIN_HAN = 0.25            # below this Han share a text is not Chinese prose
ZH_CODE_HAN = 0.02           # above this a summary is Chinese that contains code
MIN_LATIN_WORDS = 8
MIN_SCRIPT_CHARS = 20


def profile(text: str) -> dict:
    p = {k: len(rx.findall(text)) for k, rx in SCRIPTS.items()}
    p["alpha"] = sum(c.isalpha() for c in text)
    return p


def _is_ja(p: dict, min_kana: int) -> bool:
    cjk = p["han"] + p["kana"]
    return cjk > 0 and p["kana"] >= min_kana and p["kana"] / cjk >= KANA_SHARE_JA


def detect(text: str) -> str:
    """'ja' | 'zh-hant' | 'zh-hans' | 'other'."""
    p = profile(text or "")
    if _is_ja(p, MIN_KANA_JA):
        return "ja"
    if not p["alpha"] or p["han"] / p["alpha"] < ZH_MIN_HAN:
        return "other"
    return "zh-" + variant(text)


def needs_translation(text) -> bool:
    """Is a stored summary in a language that still needs translating?

    Strict on purpose: 繁中 posts about code are mostly Latin characters and
    must not be re-translated. The same predicate is used to *accept* a
    translation, so selection and acceptance can never disagree.
    """
    if not text or not text.strip():
        return False
    p = profile(text)
    cjk = p["han"] + p["kana"]
    if cjk and p["kana"] / cjk >= KANA_SHARE_JA:
        return p["kana"] >= MIN_SCRIPT_CHARS
    if p["alpha"] and p["han"] / p["alpha"] > ZH_CODE_HAN:
        return False
    if len(LATIN_WORD_RE.findall(text)) >= MIN_LATIN_WORDS:
        return True
    return any(p[k] >= MIN_SCRIPT_CHARS for k in
               ("hangul", "cyrillic", "greek", "hebrew", "arabic", "devanagari", "thai"))


# ---- mojibake ---------------------------------------------------------------
# UTF-8 read as a single-byte charset: 'çš„' (cp1252) or 'çڑ"' (cp1256) for 的.
def _byte_map(enc: str) -> dict:
    m = {chr(b): b for b in range(0x80, 0xA0)}          # holes come through as C1 controls
    for b in range(0x80, 0x100):
        try:
            m[bytes([b]).decode(enc)] = b
        except UnicodeDecodeError:
            pass
    return m


_MOJI = {}
for _enc in ("cp1252", "cp1256"):
    _m = _byte_map(_enc)
    _MOJI[_enc] = (_m, re.compile("[" + re.escape("".join(_m)) + "]+"))
_LEAD, _CONT = range(0xC2, 0xF5), range(0x80, 0xC0)
_REPEAT = re.compile(r"(.{1,12}?)\1{9,}", re.S)


def fix_mojibake(text: str) -> str:
    """Repair each mojibake run in place; text around it is left alone."""
    if not text or text.isascii():
        return text
    for enc, (m, run) in _MOJI.items():
        def fix(mo, m=m, enc=enc):
            try:
                out = bytes(m[c] for c in mo.group()).decode("utf-8")
            except UnicodeDecodeError:
                return mo.group()
            # cp1256 also covers real Arabic: only accept a repair into CJK-range text.
            return out if enc == "cp1252" or max(map(ord, out)) >= 0x800 else mo.group()
        text = run.sub(fix, text)
    return text


# A loop: one unit (1-12 chars, containing a word char) repeated >=10 times over
# >=40 chars, or >=15 times however short ("小小小…"); or a cycle of 2-3 short
# clauses repeated >=5 times ("在那裡，它是，在那裡，它是…"). Machine translation
# and Whisper both fall into these.
LOOP_MIN_SHORT, LOOP_MIN_CYCLE = 15, 5
_CLAUSE_DELIM = re.compile(r"([，,、；;。.！!？?\s]+)")


def _unit_loops(text: str):
    for mo in _REPEAT.finditer(text):
        unit = mo.group(1)
        reps = len(mo.group()) // len(unit)
        if re.search(r"[^\W\d_]", unit) and (len(mo.group()) >= 40 or reps >= LOOP_MIN_SHORT):
            yield mo.start(), mo.start() + reps * len(unit), unit, reps


def _cycle_loops(text: str):
    """Runs of a 2-3 clause cycle, as (start, end, cycle_text, reps)."""
    parts = _CLAUSE_DELIM.split(text)          # clause, delim, clause, delim, ...
    clauses = parts[0::2]
    starts, pos = [], 0
    for i, p in enumerate(parts):
        if i % 2 == 0:
            starts.append(pos)
        pos += len(p)
    i = 0
    while i < len(clauses):
        hit = None
        for p in (2, 3):
            cyc = clauses[i:i + p]
            if len(cyc) < p or len(set(cyc)) < p or not all(0 < len(c) <= 12 for c in cyc):
                continue
            k = 1
            while clauses[i + k * p:i + (k + 1) * p] == cyc:
                k += 1
            if k >= LOOP_MIN_CYCLE:
                hit = (p, k)
                break
        if hit:
            p, k = hit
            end_clause = i + p * k                  # first clause after the run
            end = starts[end_clause] if end_clause < len(starts) else len(text)
            one = starts[i + p] if i + p < len(starts) else end
            yield starts[i], end, text[starts[i]:one], k
            i = end_clause
        else:
            i += 1


def find_loops(text: str) -> list[tuple]:
    """[(start, end, unit, reps)] of degenerate repetition, in text order."""
    if not text:
        return []
    out = sorted([*_unit_loops(text), *_cycle_loops(text)])
    keep, last = [], -1
    for lp in out:                                  # overlapping finds: keep the first
        if lp[0] >= last:
            keep.append(lp)
            last = lp[1]
    return keep


def collapse_loops(text: str) -> str:
    """Each loop reduced to a single occurrence of its unit; the rest untouched."""
    for start, end, unit, _ in reversed(find_loops(text)):
        text = text[:start] + unit + text[end:]
    return text


def _utf8_pairs(text: str, m: dict, run: re.Pattern) -> int:
    """UTF-8 lead bytes followed by a continuation byte, in the runs of
    `text` that read as one single-byte charset."""
    n = 0
    for mo in run.finditer(text):
        b = [m[c] for c in mo.group()]
        n += sum(x in _LEAD and y in _CONT for x, y in zip(b, b[1:]))
    return n


def garbled(text: str) -> bool:
    """Unrepaired mojibake, replacement characters, or a degenerate loop
    (machine translation of garbage repeats one phrase dozens of times)."""
    if not text:
        return False
    chars = max(1, sum(not c.isspace() for c in text))
    if text.count("\ufffd") / chars > 0.02:
        return True
    if max(_utf8_pairs(text, m, run) for m, run in _MOJI.values()) * 2 / chars > 0.05:
        return True
    return next(_unit_loops(text), None) is not None


# Stored-summary diagnosis (QC). Broader than garbled(), which guards
# the pipeline's input and must stay strict: real Russian and Arabic pass it.
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MOJI_LATIN = re.compile(r"[ÃÂâ€¦¤§¨©®±´µ¶¸¹º»¼½¾¿ÐÑØÞßçéèêëìíîïðñòóôõöùúûüýþÿ]")
_MOJI_BOX = re.compile(r"[\u2550-\u256c\u2580-\u259f\u2524\u251c\u252c\u2534\u253c]")
_CYR = re.compile(r"[\u0400-\u04ff]")
# letters CP1251/CP866 mojibake produces but real Russian/Ukrainian almost never uses
_CYR_RARE = re.compile(r"[\u0402\u0403\u0405\u0408-\u040c\u040e\u040f\u0452\u0453\u0455"
                       r"\u0458-\u045c\u045e\u045f\u0460-\u048f\u0492-\u04ff]")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_CLAUSE = re.compile(r"[，。,、！？!?；;：:\s]+")


def diagnose(s: str) -> tuple[str, int, str | None] | None:
    """(what, position, repaired text or None) for a garbled stored summary, or
    None. Repaired text is offered only when the repair leaves nothing garbled."""
    n = len(s)
    if s.count("\ufffd") > 5 or len(_CTRL.findall(s)) > 3:
        return "binary data or replacement characters", 0, None
    han = len(_HAN.findall(s))
    moj = len(_MOJI_LATIN.findall(s))

    def repair(kind, at):
        fixed = fix_mojibake(s)
        ok = fixed != s and not garbled(fixed) and not _CYR_RARE.search(fixed)
        return (f"{kind}, repairable" if ok else kind), at, (fixed if ok else None)
    if moj > 30 and moj > 0.05 * n and han:
        return repair("UTF-8 read as Latin-1", _MOJI_LATIN.search(s).start())
    rare, box, cyr = len(_CYR_RARE.findall(s)), len(_MOJI_BOX.findall(s)), len(_CYR.findall(s))
    if rare >= 5 or (box >= 5 and cyr >= 5):
        return repair("UTF-8 read as CP1251/CP866", (_CYR_RARE.search(s) or _MOJI_BOX.search(s)).start())
    if garbled(s) and not find_loops(s):
        return repair("UTF-8 read as cp1252/cp1256", 0)
    if han > 200:                                   # broken machine translation
        clauses = [c for c in _CLAUSE.split(s) if c]
        hz = [c for c in clauses if _HAN.search(c) and not re.search(r"[0-9A-Za-z=+\-*/^()]", c)]
        if len(hz) > 40 and len(hz) > 0.6 * len(clauses):
            ratio = sum(len(c) <= 3 for c in hz) / len(hz)
            avg = sum(map(len, hz)) / len(hz)
            cyr_runs = len(re.findall(r"[\u0400-\u04ff]+", s))
            if (avg < 4.0 and ratio > 0.45) or (avg < 5.0 and ratio > 0.35 and cyr_runs >= 20):
                return ("broken machine translation" + (" mixed with Cyrillic" if cyr_runs >= 20 else ""),
                        n // 3, None)
    return None


# ---- translation ------------------------------------------------------------
GTX = "https://translate.googleapis.com/translate_a/single"
GTX_CHUNK_BYTES = 2000       # q is in the query string: limit *encoded* bytes
GTX_SLEEP = 1.0
TRANSLATE_TIMEOUT = 30       # seconds per DeepL / gtx request, in total
DEEPL_BATCH, DEEPL_MAX_CHARS = 50, 20_000
_SENT = re.compile(r"(?<=[.!?。！？])\s*")      # zero-width: CJK has no spaces

deepl_disabled = False              # set for good on a quota/auth failure
PROVIDERS: Counter = Counter()       # successful texts per backend
FAILURES: Counter = Counter()        # DeepL failures that fell back to gtx
_count_lock = threading.Lock()       # summaries are built on several threads


def count(counter: Counter, key: str, n: int = 1) -> None:
    """counter[key] += n, safe across worker threads."""
    with _count_lock:
        counter[key] += n


def deepl_active() -> bool:
    return bool(store.CFG.deepl_key) and not deepl_disabled


class TranslateError(Exception):
    """A failed translation. kind "fatal" (DeepL quota or auth: DeepL stays
    off for the rest of the run) or "transient" (anything else). The message
    is for the log and the STATS keys only; nothing branches on it."""

    def __init__(self, reason: str, kind: str = "transient"):
        super().__init__(reason)
        self.kind = kind


class Skipped(str):
    """A result reason meaning gtx was never asked: it is refusing this run.
    Recognised by type, not by its text."""


SKIPPED = Skipped("skipped: gtx refusing this run")
DEEPL_FATAL = {401: "deepl HTTP 401", 403: "deepl HTTP 403", 456: "deepl quota exceeded (456)"}


def short_reason(reason: str, limit: int = 160) -> str:
    """One line. urllib3's RetryError embeds the whole url (and the text)."""
    reason = " ".join((reason or "").split())
    for key in ("too many 429", "429", "403", "timed out", "NameResolution",
                "Connection", "quota", "456"):
        if key in reason:
            head = reason.split(":", 1)[0]
            return (head if key in head else f"{head}: {key}")[:limit]
    return reason[:limit]


def _enc(s: str) -> int:
    return len(quote(s, safe=""))


def chunk_text(text: str, limit: int = GTX_CHUNK_BYTES) -> list[str]:
    out, cur = [], ""
    for sent in (s.strip() for s in _SENT.split(text or "") if s.strip()):
        cand = f"{cur} {sent}" if cur else sent
        if _enc(cand) <= limit:
            cur = cand
            continue
        if cur:
            out.append(cur)
        while _enc(sent) > limit:              # one over-long sentence
            lo, hi = 1, len(sent)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                lo, hi = (mid, hi) if _enc(sent[:mid]) <= limit else (lo, mid - 1)
            out.append(sent[:lo])
            sent = sent[lo:].lstrip()
        cur = sent
    return out + ([cur] if cur else [])


def _call(method, url, **kw):
    """The guarded transport for the two fixed APIs: status 200 or TranslateError."""
    r = net.request(method, url, TRANSLATE_TIMEOUT, headers=dict(net.API_HEADERS, **kw.pop("headers", {})),
                       retries=2, **kw)
    if r is None:
        raise TranslateError("no response (timed out or refused)")
    return r


_gtx_lock, _gtx_next = threading.Lock(), 0.0


def _gtx_wait() -> None:
    """GTX_SLEEP between any two gtx requests of this process. The wait is
    shared, not per thread: summaries are built on several worker threads, and
    a per-thread sleep would multiply the request rate by the thread count."""
    global _gtx_next
    with _gtx_lock:
        now = time.monotonic()
        wait, _gtx_next = _gtx_next - now, max(now, _gtx_next) + GTX_SLEEP
    if wait > 0:
        time.sleep(wait)


def _gtx(text: str) -> str:
    out = []
    for chunk in chunk_text(text):
        _gtx_wait()
        r = _call("GET", GTX, params={"client": "gtx", "sl": "auto", "tl": "zh-TW", "dt": "t", "q": chunk})
        if r.status != 200:
            raise TranslateError(f"HTTP {r.status}")
        try:
            segs = r.json()[0]
            out.append("".join(str(s[0]) for s in segs if isinstance(s, list) and s and s[0]))
        except Exception as e:
            raise TranslateError(f"bad payload: {type(e).__name__}") from e
    return "".join(out).strip()


def _deepl_call(method, path, **kw):
    key = store.CFG.deepl_key
    host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
    return _call(method, host + path, headers={"Authorization": f"DeepL-Auth-Key {key}"}, **kw)


def _deepl(texts: list[str]) -> tuple[list, TranslateError | None]:
    """([translation | None] aligned with `texts`, the error if any is None).
    A 413 halves the batch (DeepL limits bytes, we count chars); a half that
    succeeds keeps its results, which DeepL has already billed. Whether an
    error is fatal is decided here, from the status code."""
    none = [None] * len(texts)
    try:
        r = _deepl_call("POST", "/v2/translate", json_body={"text": texts, "target_lang": "ZH-HANT"})
    except TranslateError as e:
        return none, TranslateError(f"deepl {e}")
    if r.status == 413 and len(texts) > 1:
        mid = len(texts) // 2
        head, err = _deepl(texts[:mid])
        if err and err.kind == "fatal":             # the other half would fail the same way
            return head + [None] * (len(texts) - mid), err
        tail, err2 = _deepl(texts[mid:])
        return head + tail, err or err2
    if r.status in DEEPL_FATAL:
        return none, TranslateError(DEEPL_FATAL[r.status], "fatal")
    if r.status != 200:
        return none, TranslateError(f"deepl HTTP {r.status}")
    try:
        out = [t["text"] for t in r.json()["translations"]]
    except Exception as e:
        return none, TranslateError(f"deepl bad payload: {type(e).__name__}")
    if len(out) != len(texts):          # misaligned results would cross-write items
        return none, TranslateError(f"deepl returned {len(out)} of {len(texts)} segments")
    return out, None


def deepl_usage():
    if not deepl_active():
        return None
    try:
        d = _deepl_call("GET", "/v2/usage").json()
        return int(d["character_count"]), int(d["character_limit"])
    except Exception:
        return None


def _accept(out: str) -> tuple:
    """A translation is usable only if it is Chinese and not garbage."""
    out = (out or "").strip()
    if not out:
        return None, "empty result"
    if find_loops(out):                     # keep what the provider got right
        fixed = collapse_loops(out)
        if len(fixed) < 0.6 * len(out):     # mostly loop: nothing worth keeping
            return None, "garbled result"
        out = fixed
    if garbled(out):
        return None, "garbled result"
    if needs_translation(out):
        return None, "result still not Chinese"
    return out, ""


def translate_many(texts: list[str], stop_after: int = 0) -> list[tuple]:
    """[(translation | None, reason)] aligned with `texts`, into 繁體中文.

    stop_after: after this many consecutive gtx failures, stop sending
    requests; the rest come back as skipped (the endpoint is refusing us).
    """
    global deepl_disabled
    res: list = [(None, "empty input")] * len(texts)
    todo = []
    for i, t in enumerate(texts):
        if (t or "").strip():
            if garbled(t):                          # never pay to translate garbage
                res[i] = (None, "garbled input")
            else:
                todo.append(i)
    streak, pos = 0, 0                              # streak: consecutive gtx refusals
    while pos < len(todo):
        batch, chars = [], 0
        while pos < len(todo) and len(batch) < DEEPL_BATCH and not (
                batch and chars + len(texts[todo[pos]]) > DEEPL_MAX_CHARS):
            chars += len(texts[todo[pos]])
            batch.append(todo[pos])
            pos += 1
        if deepl_active():
            outs, err = _deepl([texts[i].strip() for i in batch])
            done = [(i, o) for i, o in zip(batch, outs) if o is not None]
            for i, o in done:
                res[i] = _accept(o)
            if done:
                count(PROVIDERS, "deepl", len(done))
            if err:
                count(FAILURES, short_reason(str(err)))
                if err.kind == "fatal":
                    deepl_disabled = True
            batch = [i for i, o in zip(batch, outs) if o is None]
        for i in batch:
            if stop_after and streak >= stop_after:
                res[i] = (None, SKIPPED)
                continue
            try:
                res[i] = _accept(_gtx(texts[i]))
                streak = 0                          # gtx answered, whatever the content
            except TranslateError as e:
                res[i] = (None, str(e))
                streak += 1
            if res[i][0]:
                count(PROVIDERS, "gtx")
    return res


def translate_keep_lines(texts: list[str], stop_after: int = 0) -> list[tuple]:
    """translate_many, but each line goes as its own segment so the result has
    the same line breaks (gtx joins chunks with spaces). A text whose lines
    don't all translate is retried whole, without the breaks."""
    parts = [[ln.strip() for ln in (t or "").split("\n") if ln.strip()] for t in texts]
    flat = [ln for p in parts for ln in p]
    got = translate_many(flat, stop_after) if flat else []
    res, pos = [], 0
    for p in parts:
        lines, pos = got[pos:pos + len(p)], pos + len(p)
        if not p:
            res.append((None, "empty input"))
        elif len(p) == 1:
            res.append(lines[0])
        elif all(o for o, _ in lines):
            res.append(("\n".join(o for o, _ in lines), ""))
        elif any(isinstance(why, Skipped) for _, why in lines):
            res.append((None, SKIPPED))              # gtx is refusing: a retry only adds requests
        else:
            res.append(None)                         # whole text below
    if retry := [i for i, r in enumerate(res) if r is None]:
        for i, r in zip(retry, translate_many([" ".join(parts[i]) for i in retry], stop_after)):
            res[i] = r
    return res


FOREIGN_BUDGET = 1.2          # translation shrinks text; select a bit more
TABLE_NOTE = "請參閱所附表格 " + FALLBACK_MARK
CODE_NOTE = "請參閱所附程式碼 " + FALLBACK_MARK
STATS: Counter = Counter()           # boilerplate drops, untranslated, reasons

# ============================================================== captions (VTT)
_TAG = re.compile(r"<[^>]+>")
_WATERMARK = re.compile(r"\[[^\]]*(?:人工智慧翻譯|AI\s*翻譯|criblate\.com)[^\]]*\]", re.I)
_ANNOT = re.compile(r"\[\s*(?:_+|\*+|\s)*\s*\]|\[[^\]\n]{1,30}\]"
                    r"|\(\s*(?:music|applause|laughter|inaudible|crosstalk|silence)\s*\)", re.I)
_SPEAKER = re.compile(r"(?:&gt;\s*){2,}|>{2,}")
_TURN = "\x00"
_LATIN = re.compile(r"[0-9A-Za-z\u00c0-\u024f]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_TERMINAL = re.compile(r"[。！？!?；;]")
_ZH_CONN = re.compile(r"^(但是|但|不過|可是|然而|所以|因為|因此|於是|結果|其實|如果|要是|而且|另外|"
                      r"同時|然後|之後|後來|首先|接著|最後|即是|反而|不然|例如|譬如|比如|總之|換言之)")
_ZH_CONT = re.compile(r"^(的|地|得|了|著|過|嗎|呢|吧|啊|喔|嘛|就|才|也|都|還|再|又|並|和|與|或|至|到|"
                      r"給|把|被|從|對|向|以及)")
# Japanese: clause-opening conjunctions, particles/auxiliaries that can only
# continue a clause, and polite endings that close a sentence (ASR drops 。).
_JA_CONN = re.compile(r"^(しかし|でも|だから|それで|そして|それから|ところで|つまり|例えば|なので|"
                      r"ただ|一方|また|さらに|実は|まず|次に|最後に|要するに|ちなみに|けれども|だけど|じゃあ)")
_JA_CONT = re.compile(r"^(は|が|を|に|で|と|も|の|へ|や|から|まで|より|ね|よ|か|な|って|けど|し|ので|"
                      r"のに|ながら|ます|です|でした|ました|ません|ている|てる|た|だ)")
_JA_END = re.compile(r"(です|ます|ました|でした|ません|ましょう|でしょう|ください|ですね|ますね|ですよ|ますよ)$")
# (conn, cont, sentence end, comma, chars after which a line joins without comma)
_PROFILES = {"zh": (_ZH_CONN, _ZH_CONT, None, "，", ""),
             "ja": (_JA_CONN, _JA_CONT, _JA_END, "、", "がはをにでとものへや")}
MIN_CAPTION_CHARS = 30
MERGE_TARGET, MERGE_MAX, CONN_MIN = 45, 75, 15


def _cues(raw: str):
    """Each cue's text lines; header, NOTE blocks and cue numbers never leak."""
    for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.split("\n")
        idx = next((n for n, l in enumerate(lines) if "-->" in l), None)
        if idx is not None:
            cleaned = [t for t in (_TAG.sub("", l).strip() for l in lines[idx + 1:]) if t]
            if cleaned:
                yield cleaned


def _join(lines: list[str]) -> str:
    out = ""
    for part in lines:                  # space between Latin words, none in CJK
        if out and (_LATIN.search(out[-1]) or _LATIN.search(part[0])):
            out += " "
        out += part
    return out


def _overlap(tail: str, text: str) -> int:
    """Longest k with tail ending in text[:k], not splitting a Latin word."""
    for k in range(min(len(tail), len(text)), 0, -1):
        if k != len(text) and k < (5 if _CJK.search(text[:k]) else 12):
            break
        if tail.endswith(text[:k]) and not (
                k < len(text) and _LATIN.match(text[k]) and _LATIN.match(text[k - 1])):
            return k
    return 0


def stitch(cues) -> list[str]:
    """Rolling cues repeat the previous line; wrapped cues don't. Join each cue
    whole and drop whatever prefix the output already ends with."""
    out, acc = [], ""
    for cue in cues:
        text = _join(cue)
        rest = text[_overlap(acc[-400:], text):].strip(" \t,")
        if rest:
            out.append(rest)
            acc = f"{acc} {rest}" if acc else rest
    return out


def merge_caption_lines(text: str, lang_: str = "zh") -> str:
    """Rebuild ~45-char sentences from unpunctuated CJK cue lines (zh | ja)."""
    conn_re, cont_re, end_re, comma, glue = _PROFILES[lang_]
    units, cur = [], ""

    def flush():
        nonlocal cur
        if cur:
            units.append(cur if _TERMINAL.search(cur[-1]) else cur + "。")
            cur = ""

    for line in (l.strip() for l in text.split("\n")):
        if line.startswith(_TURN):
            line = line.lstrip(_TURN).strip()
            flush()
        if not line:
            continue
        conn = bool(conn_re.match(line))
        cont = not conn and bool(cont_re.match(line))
        if cur and (not cont or len(cur) >= MERGE_MAX) and (
                len(cur) >= MERGE_TARGET or len(cur) + len(line) > MERGE_MAX
                or (conn and len(cur) >= CONN_MIN)):
            flush()
        if not cur:
            cur = line
        elif cont or cur[-1] in "，,、。！？!?；;" or (
                cur[-1] in glue and not cur.endswith(("ので", "のに", "ても", "でも", "ては"))):
            cur += line
        else:
            cur += comma + line
        if _TERMINAL.search(cur[-1]) or (end_re and end_re.search(cur)):
            flush()
    flush()
    return "\n".join(units)


def clean_caption(text: str, turns=False) -> str:
    text = _SPEAKER.sub("\n" + _TURN if turns else "\n", html_mod.unescape(text))
    text = _ANNOT.sub(" ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"^[ \t]+|[ \t]+$", "", text, flags=re.M)
    return re.sub(r"\n{2,}", "\n", text).strip()


def vtt_to_text(path) -> str:
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    text = clean_caption("\n".join(stitch(_cues(_WATERMARK.sub("", raw)))), turns=True)
    src = detect(text) if text else ""
    if src.startswith("zh") or src == "ja":
        mode = "ja" if src == "ja" else "zh"
        n = len(_TERMINAL.findall(text))
        cpt = len(text) / n if n else float("inf")
        if cpt > 60:                                   # unpunctuated ASR
            text = merge_caption_lines(text, mode)
        elif cpt < 28:                                 # a full stop per cue line
            text = merge_caption_lines(re.sub(r"[。．｡]+[ \t]*$", "", text, flags=re.M), mode)
    return text.replace(_TURN, "")


# ============================================================== boilerplate
KINDS = ("page", "meta", "feed", "subtitle")
_rules: dict | None = None
_rules_lock = threading.Lock()


def boiler_rules() -> dict:
    """rules.json `boilerplate`, compiled once. Actions run in a fixed order:
    remove_block, remove_inline, cut_to_end (on source text), drop_unit (on
    繁體-normalised text, per sentence/paragraph and source kind). Each pattern
    is compiled and applied on its own: joined into one alternation, a
    backreference would point at another rule's group, and an inline flag or a
    repeated group name would fail the whole set."""
    global _rules
    with _rules_lock:
        if _rules is None:
            _rules = _compile_rules(store.rules().get("boilerplate") or {})
    return _rules


def _compile_rules(data: dict) -> dict:
    out = {"blocks": [], "inline": [], "cut": [], "units": {},
           "min_keep": int(data.get("min_keep_after_cut", 80))}
    for r in data.get("rules") or []:
        act, pat = r.get("action"), r.get("pattern") or ""
        if act == "remove_block" and r.get("text"):
            out["blocks"].append(r["text"])
        elif act in ("remove_inline", "cut_to_end", "drop_unit") and pat:
            try:
                rx = re.compile(pat, re.M if act == "remove_inline" else 0)
            except re.error as e:
                print(f"WARNING: bad boilerplate regex {pat!r} ({e})")
                continue
            if act == "drop_unit":
                out["units"].setdefault((r.get("level") or "sentence", r.get("scope") or "all"), []).append(rx)
            else:
                out["inline" if act == "remove_inline" else "cut"].append(rx)
        else:
            print(f"WARNING: bad boilerplate rule {r!r}")
    return out


# Markup in the text is the article's subject (a tutorial's <form> sample), not
# extraction debris. Shared with QC so both agree on what is code.
_CODE_CTX = re.compile(r"HTML|CSS|JavaScript|TypeScript|JSX|React|Vue|Svelte|DOM|XML|SVG|前端|網頁設計|"
                       r"標籤|元素|屬性|範例|程式碼|語法|原始碼|模板|tag|element|attribute|markup|snippet", re.I)
_CODE_TOK = re.compile(r"\bfunction\b|\bconst\b|=>|\bimport\b|\bdef\b|\breturn\b|onClick|useState|"
                       r"\{\s*\}|`[^`]{1,80}`|「\s*<[^」]{1,80}」")


def looks_like_code(s: str) -> bool:
    return CODE_NOTE in s or bool(_CODE_CTX.search(s)) or len(_CODE_TOK.findall(s)) >= 3


def strip_boilerplate(text: str, markup: bool = True) -> str:
    """markup=False keeps HTML-looking text (code samples) untouched."""
    r = boiler_rules()
    if markup:
        text = sanitize_markup(text)
    for b in r["blocks"]:
        text = text.replace(b, "")
    for rx in r["inline"]:
        text = rx.sub("", text)
    # cut_to_end: the earliest marker that leaves min_keep chars; one nearer
    # the start (a title that quotes it) does not hide a real one further on.
    cuts = [m.start() for rx in r["cut"]
            if (m := next((m for m in rx.finditer(text) if m.start() >= r["min_keep"]), None))]
    if cuts:
        text = text[:min(cuts)]
    return text.strip()


def restrip(summary: str) -> str:
    """A stored summary under the current text rules; trailing marks kept,
    BLANK when nothing but boilerplate is left."""
    body, marks = summary.strip(), []
    for m in (TABLE_NOTE, CODE_NOTE, FALLBACK_MARK):
        if body.endswith(m):
            body, marks = body[:-len(m)].rstrip(), [m] + marks
    new = strip_boilerplate(body, markup=not looks_like_code(summary))
    if new == body:
        return summary
    return " ".join([new, *marks]) if new else BLANK_SUMMARY


_URL_FRAG = re.compile(r"^(?:https?://)?[\w\-]{2,}[./?#:][\w\-./?#=&%~+]*$", re.A)


def is_boilerplate(text: str, kind: str, level="sentence") -> bool:
    if not text:
        return False
    if level == "sentence" and len(text) <= 40 and " " not in text \
            and not _CJK.search(text) and _URL_FRAG.match(text):
        return True
    probe = key_text(text)
    units = boiler_rules()["units"]
    scopes = ("all", *KINDS) if kind == "*" else ("all", kind)
    return any(p.search(probe) for s in scopes for p in units.get((level, s), ()))


# ============================================================== extraction
_ZH_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*")
_EN_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\u00c0-\u024f\"'(])")
_BULLET = re.compile(r"(?<=[\u4e00-\u9fff%）。，、])\s*[-–—•·]\s*(?=[\u4e00-\u9fff\dA-Za-z])")
# Keeps each terminator (and trailing space) with its sentence: pieces re-join exactly.
SENT_RE = re.compile(r"(?<=[。！？；;])(?![」』”\"'）)])\s*"
                     r"|(?<=[.!?])\s+(?=[A-Z0-9\u00c0-\u024f\u4e00-\u9fff\"'(])")
STOP = frozenset("""a an the and or but if then than that this these those of in on at to
for from with by as is are was were be been being it its it's he she they them his her their
we you your i not no so do does did done have has had will would can could should may might
must about into over under between after before during what which who whom whose when where
why how all any both each few more most other some such only own same very s t just don now
also there here out up""".split())
_NUM = re.compile(r"[0-9０-９][0-9０-９,.:%]*|[一二三四五六七八九十百千萬億兆]{2,}")
_ENT = re.compile(r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+|《[^》]{1,30}》|「[^」]{1,20}」"
                  r"|『[^』]{1,20}』|[0-9.]+\s*(?:%|per cent|percent)")
_LEAD_ZH = re.compile(
    r"^(然而|不過|此外|另外|同時|當然|其實|事實上|值得一提的是|換句話說|也就是說|總而言之|總的來說|"
    r"不僅如此|除此之外|與此同時|与此同时|与此同時|然后|然後|接著|接着|因此|所以|而且|并且|並且|"
    r"首先|其次|最後|最后|再者)[，,、]")
_LEAD_EN = re.compile(
    r"^(However|Moreover|Furthermore|In addition|Additionally|Of course|In fact|Indeed|"
    r"Meanwhile|Nevertheless|Nonetheless|Besides|That said|To be sure|As a result|Therefore|"
    r"Thus|So|Also|And|But|Yet),?\s+", re.I)
_PRONOUN_ZH = re.compile(r"^[我你他她它我們你們他們這那些的了是也都很就會能不沒有和與跟得地嗎呢吧啊，。！？\s]+$")
W_ENT, W_ENT_STEP, W_ENT_CAP = 1.2, 0.08, 5
W_FLUFF, W_LEAD = 0.25, 0.5
MMR_LAMBDA, MMR_DUP = 0.7, 0.65


def split_lines(text: str, cjk: bool) -> list[tuple[int, str]]:
    """[(source line number, sentence)]: the line number lets a summary keep
    the source's line breaks between the sentences it picks."""
    out = []
    lines = re.sub(r"\n{2,}", "\n", _BULLET.sub("\n", text)).split("\n")
    for no, line in enumerate(lines):
        for s in (_ZH_SPLIT if cjk else _EN_SPLIT).split(line.strip()):
            s = s.strip()
            if s and (len(s) >= 8 if cjk else len(s.split()) >= 5):
                out.append((no, s))
    return out


def tokens(s: str, cjk: bool) -> set:
    if cjk:
        chars = re.sub(r"[^\u4e00-\u9fff0-9A-Za-z]", "", s)
        return {chars[i:i + 2] for i in range(len(chars) - 1)} | set(
            re.findall(r"[0-9]+(?:\.[0-9]+)?%?", s))
    return {t for t in re.findall(r"[a-zA-Z][a-zA-Z'-]+|[0-9]+(?:\.[0-9]+)?%?", s.lower())
            if t not in STOP}


def trim_lead(s: str, cjk: bool) -> str:
    rx = _LEAD_ZH if cjk else _LEAD_EN
    while (new := rx.sub("", s).lstrip()) != s:
        s = new
    return s


def trim_to_sentences(text: str, limit: int) -> str:
    """At most `limit` chars by dropping whole sentences from the end; a single
    over-long first sentence is kept whole rather than cut."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    pieces, prev = [], 0
    for m in SENT_RE.finditer(text):
        if m.end() > prev:
            pieces.append(text[prev:m.end()])
            prev = m.end()
    pieces = [p for p in pieces + [text[prev:]] if p.strip()]
    kept, used = [], 0
    for p in pieces:
        if used + len(p.rstrip()) > limit:
            break
        kept.append(p)
        used += len(p)
    return "".join(kept).strip() if kept else pieces[0].strip()


def extractive(text: str, cjk: bool, budget: int, kind: str, lines: bool = False) -> str:
    """lines: picked sentences that came from different source lines are
    joined with a line break, as in the source."""
    pairs = split_lines(text, cjk)
    if not pairs:
        return trim_to_sentences(text, budget)
    keep = [(no, s) for no, s in pairs if not is_boilerplate(s, kind)]
    if keep and len(keep) < len(pairs):
        count(STATS, f"boiler:{kind}:sentence", len(pairs) - len(keep))
        pairs = keep
    uniq = {}                                   # verbatim repeats: first wins
    for no, s in pairs:
        uniq.setdefault(re.sub(r"\W+", "", s.lower()), (no, s))
    pairs = [p for k, p in uniq.items() if k]
    line_of = [no for no, _ in pairs]
    sents = [s for _, s in pairs]

    toks = [tokens(s, cjk) for s in sents]
    df = Counter(t for ts in toks for t in ts)
    n = len(sents)
    cands = []
    for i, (s, ts) in enumerate(zip(sents, toks)):
        if not ts:
            continue
        score = sum(math.sqrt(df[t]) * math.log(1 + n / df[t]) for t in ts) / len(ts) ** 0.5
        ents = len(_ENT.findall(s)) + len(_NUM.findall(s))
        if ents:
            score *= W_ENT + min(ents, W_ENT_CAP) * W_ENT_STEP
        elif s.rstrip().endswith(("？", "?")) or (cjk and _PRONOUN_ZH.match(s)):
            score *= W_FLUFF
        score *= 1 + W_LEAD * max(0.0, 1 - i / n)
        cands.append([score, i, trim_lead(s, cjk), ts, 0.0])     # last: max similarity
    if not cands:
        return sents[0]

    top = max(c[0] for c in cands) or 1.0
    chosen, used = [], 0
    while cands:
        fit = [c for c in cands if c[4] < MMR_DUP and used + len(c[2]) + 1 <= budget]
        if not fit:
            break
        best = max(fit, key=lambda c: c[0] / top - MMR_LAMBDA * c[4])
        cands.remove(best)
        chosen.append(best)
        used += len(best[2]) + 1
        if used >= budget * 0.97:
            break
        for c in cands:
            union = c[3] | best[3]
            if union:
                c[4] = max(c[4], len(c[3] & best[3]) / len(union))
    if not chosen:
        return sents[0]
    out, prev = "", None
    for c in sorted(chosen, key=lambda c: c[1]):
        if prev is not None:
            out += "\n" if lines and line_of[c[1]] != line_of[prev] else ("" if cjk else " ")
        out, prev = out + c[2], c[1]
    return out


# ============================================================== assembly
_CJK_PUNCT = "\u4e00-\u9fff，。！？；：、（）「」"


def tidy(text: str, keep_lines: bool = True) -> str:
    """Collapse spaces (none around CJK punctuation); line breaks survive as
    single newlines unless keep_lines is off."""
    rows = text.split("\n") if keep_lines else [text]
    out = []
    for row in rows:
        if detect(row) != "other":
            row = re.sub(rf"(?<=[{_CJK_PUNCT}])\s+|\s+(?=[{_CJK_PUNCT}])", "", row)
        if row := re.sub(r"\s+", " ", row).strip():
            out.append(row)
    return "\n".join(out)


def _translate(text: str) -> str | None:
    """zh-TW translation, line breaks kept, recording why it failed."""
    out, reason = translate_keep_lines([text])[0]
    if not out:
        count(STATS, f"translate-fail:{short_reason(reason) or 'unknown'}")
    return out


def build(content: str, kind: str = "page", table=False, code=False, meta=False,
          translate: bool | None = None) -> str | None:
    """Extractive summary of `content`, in 繁中, plus trailing marks.

    kind: page | feed | subtitle (selects boilerplate scope);
    meta: only a blurb was available (marked ↛).
    translate: translate non-Chinese text (default: CFG.translate).
    Returns "" when only boilerplate remains, None when the text is
    undecodable (mojibake): that is a fetch problem, so the item stays pending.
    """
    content = collapse_loops(fix_mojibake(content))   # Whisper/MT loops: keep one copy
    if garbled(content):
        count(STATS, "garbled")
        return None
    content = re.sub(r"\((?:\d{1,2}:)?\d{1,2}:\d{2}\)\s*[:：]?", ": ", content)
    content = strip_boilerplate(content)
    if not content:
        return ""
    marks = [m for m, on in ((TABLE_NOTE, table), (CODE_NOTE, code)) if on and not meta]
    if meta:
        marks.append(FALLBACK_MARK)
        kind = "meta"
    suffix = (" " + " ".join(marks)) if marks else ""

    paras = re.split(r"\n\s*\n|\n", content)
    kept = [p for p in paras if not is_boilerplate(p.strip(), kind, "paragraph")]
    if len(kept) < len(paras) and any(p.strip() for p in kept):
        count(STATS, f"boiler:{kind}:paragraph", len(paras) - len(kept))
        content = "\n".join(kept)

    src = detect(content)
    budget = max(1, min(store.CFG.summary_max, int(len(content) * store.CFG.summary_ratio)) - len(suffix))
    lines = kind != "subtitle"             # caption lines are cue/merge units, not the speaker's paragraphs
    if src.startswith("zh"):
        summary = extractive(content, True, budget, kind, lines)
    else:
        raw = extractive(content, src == "ja", int(budget * FOREIGN_BUDGET), kind, lines)
        summary = _translate(raw) if (store.CFG.translate if translate is None else translate) else None
        if not summary:                   # stored as-is; backfill retries later
            count(STATS, f"untranslated:{src}")
            summary = raw
    summary = tidy(to_twp(summary), keep_lines=lines)
    summary = trim_to_sentences(summary, budget)
    return summary + suffix if summary else ""
