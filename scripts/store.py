"""Shared state and contracts: item sentinels, configuration (the only reader
of the environment), rules.json, dates, hosts, archive I/O and the subtitle
file contract. Imports no other module of this project."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
from dateutil import parser as dtparser

# ---- summary sentinels ------------------------------------------------------
# key missing      -> pending, retried every run
# BLANK_SUMMARY    -> deliberately empty (douban mark, no speech); never retried
# GONE_SUMMARY     -> permanent placeholder (404/410, no archive); never retried
FALLBACK_MARK = "↛"
BLANK_SUMMARY = " "
GONE_SUMMARY = "無法取得頁面內容（原始頁面已移除，且無存檔）" + FALLBACK_MARK
PLACEHOLDER_PREFIX = "無法取得頁面內容"


# Scratch fields: the feed copy of a pending item, handed from collect to
# summarize in the same process. In memory only:
# save_doc writes ITEM_FIELDS and nothing else.
SCRATCH = ("feed_content", "feed_images")
ITEM_FIELDS = frozenset(("id", "category", "source", "title", "url", "published_at",
                         "last_seen_at", "thumbnail", "summary", "status", "length"))


def drop_scratch(item) -> None:
    for k in SCRATCH:
        item.pop(k, None)


ID_RE = re.compile(r"[0-9a-f]{40}")          # sha1 from collect.make_id; names files
LANG_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*")


def valid_id(item_id) -> bool:
    return isinstance(item_id, str) and bool(ID_RE.fullmatch(item_id))


def safe_lang(code, default="und") -> str:
    """A language code fit for a filename; anything else becomes `default`."""
    code = str(code or "").strip()
    return code if LANG_RE.fullmatch(code) and len(code) <= 35 else default


def is_pending(item) -> bool:
    return isinstance(item, dict) and bool(item.get("url")) and not item.get("summary")


# ---- configuration: the only place that reads the environment --------------
def env_flag(value: str | None, default: bool = False) -> bool:
    """on / 1 / true / yes -> True; anything else set -> False; unset/empty -> `default`."""
    v = (value or "").strip().lower()
    return default if not v else v in ("on", "1", "true", "yes")


FALLBACK_CHOICES = frozenset(("reader", "wayback", "reader-meta"))


@dataclass
class Config:
    """Every tunable. Read once from the environment by the entry points
    (configure()); modules read store.CFG at call time, never at import."""
    data_dir: Path = Path("data")
    archive_days: int = 60
    rss_max_feeds: int = 400               # 0 = every OPML feed
    scraped_sources: bool = False          # BestBlogs, 澎湃
    max_items: int = 200                   # page fetches per run
    time_budget: int = 0                   # seconds; 0 = unlimited
    translate: bool = True
    summary_ratio: float = 1.0
    summary_max: int = 60000
    # summarize.fetch_article fallbacks after the page and the feed copy:
    # reader (r.jina.ai), wayback, reader-meta (also escalate meta-only pages)
    fallbacks: frozenset = frozenset(("reader", "wayback"))
    deepl_key: str = ""
    cookies_path: Path | None = None
    max_asr_total: float = 8 * 3600        # audio-seconds per run; 0 = unlimited
    max_transcribe: int = 40               # ASR jobs per run; 0 = unlimited
    max_asr_duration: float = 3 * 3600     # longer videos are blanked; 0 = unlimited
    whisper_model: str = "base"
    whisper_compute_type: str = "int8"

    @property
    def archive(self) -> Path:
        return self.data_dir / "archive.json"

    @property
    def subtitles(self) -> Path:
        return self.data_dir / "subtitles"

    @property
    def status_file(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox.json"

    @classmethod
    def from_env(cls, env=None) -> "Config":
        e = os.environ if env is None else env
        d = cls()

        def num(name, default, cast=int):
            try:
                return cast(e.get(name) or default)
            except ValueError:
                print(f"WARNING: {name}={e.get(name)!r} is not a number; using {default}")
                return default
        fb = e.get("FALLBACKS")
        fallbacks = d.fallbacks if fb is None else frozenset(
            x for x in (p.strip().lower() for p in fb.split(",")) if x)
        if bad := fallbacks - FALLBACK_CHOICES:
            print(f"WARNING: unknown FALLBACKS {sorted(bad)} ignored")
            fallbacks -= bad
        cookies = (e.get("COOKIES_PATH") or "").strip()
        return cls(
            data_dir=Path(e.get("DATA_DIR") or d.data_dir),
            archive_days=num("ARCHIVE_DAYS", d.archive_days),
            rss_max_feeds=num("RSS_MAX_FEEDS", d.rss_max_feeds),
            scraped_sources=env_flag(e.get("SCRAPED_SOURCES")),
            max_items=num("MAX_ITEMS", d.max_items),
            time_budget=num("TIME_BUDGET_SECONDS", d.time_budget),
            translate=env_flag(e.get("TRANSLATE"), True),
            summary_ratio=num("SUMMARY_RATIO", d.summary_ratio, float),
            summary_max=num("SUMMARY_MAX", d.summary_max),
            fallbacks=fallbacks,
            deepl_key=(e.get("DEEPL_API_KEY") or "").strip(),
            cookies_path=Path(cookies) if cookies else None,
            max_asr_total=num("MAX_ASR_TOTAL_SECONDS", d.max_asr_total, float),
            max_transcribe=num("MAX_TRANSCRIBE", d.max_transcribe),
            max_asr_duration=num("MAX_ASR_DURATION_SECONDS", d.max_asr_duration, float),
            whisper_model=e.get("WHISPER_MODEL") or d.whisper_model,
            whisper_compute_type=e.get("WHISPER_COMPUTE_TYPE") or d.whisper_compute_type,
        )


# Defaults only: importing a module never reads the environment. Entry points
# (and selftest) call configure(), so a value set after import still applies.
CFG = Config()


def configure(**overrides) -> Config:
    """Read the environment (entry points, tests), then apply overrides."""
    global CFG
    CFG = replace(Config.from_env(), **overrides)
    return CFG


RULES_FILE = Path(__file__).with_name("rules.json")
_rules: dict | None = None


def rules() -> dict:
    """rules.json: `boilerplate` (text), `qc_chrome` (QC) and `sites`. Read
    once per process; unreadable -> empty."""
    global _rules
    if _rules is None:
        try:
            data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
            _rules = data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"WARNING: {RULES_FILE} unreadable ({e}); no rules")
            _rules = {}
    return _rules


# Files whose rules decide what a stored item should look like (text rules,
# thumbnail and QC heuristics). The archive records their hash after a full
# backfill; until one of them changes, re-applying them finds nothing to do.
RULE_SOURCES = ("rules.json", "textproc.py", "summarize.py")


def rules_hash() -> str:
    h = hashlib.sha1()
    for name in RULE_SOURCES:
        h.update(name.encode() + b"\0")
        try:
            h.update(Path(__file__).with_name(name).read_bytes())
        except OSError:
            h.update(b"missing")
    return h.hexdigest()[:16]


def site(name: str, kind=tuple):
    """One table of rules.json `sites`: a tuple of hosts, or a dict (kind=dict).
    Called where the table is used, not at import, so importing a module never
    reads rules.json; rules() reads the file once per process."""
    v = (rules().get("sites") or {}).get(name)
    if kind is dict:
        return dict(v) if isinstance(v, dict) else {}
    return tuple(v) if isinstance(v, list) else ()


# ---- dates / embedded JSON ----------------------------------------------------
UTC = timezone.utc


def iso(dt) -> str | None:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def parse_date(value) -> datetime | None:
    """Feed dates in any common shape (RFC 822, ISO, epoch s/ms) as aware UTC."""
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


# ---- HTML tag scan (linear) ---------------------------------------------------
# Pages are untrusted and up to 8 MB. A regex like `<meta\b[^>]*>` rescans to
# the end of the text for every unclosed `<meta`, which is quadratic: a page of
# repeated `<meta a` would take hours. This scanner looks at every character a
# bounded number of times, to keep a hostile page linear. A tag ends at its
# first `>`, the same boundary a `[^>]*>` pattern uses.
# Possessive quantifiers: no backtracking inside a long attribute either.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def ascii_lower(text: str) -> str:
    """Lower-case A-Z only. Unlike str.lower() the length never changes
    ('İ'.lower() is two code points), so offsets found in the result index
    the original text."""
    return text.translate(_ASCII_LOWER)


_ATTR = re.compile(r"""(?<![\w:-])([\w:-]++)\s*+=\s*+(?:"([^"]*+)"|'([^']*+)'|([^\s"'>]++))""")


def iter_tags(html: str, names, limit: int | None = None):
    """(name, {attr: value}) for each start tag whose lower-case name is in
    `names`, in document order. Values are raw (not unescaped)."""
    html = (html or "")[:limit] if limit else (html or "")
    names = {n.lower() for n in names}
    n, i, gt = len(html), html.find("<"), -1
    while 0 <= i < n:
        if gt <= i:
            gt = html.find(">", i + 1)
            if gt < 0:
                return                          # nothing after can close either
        nxt = html.find("<", i + 1)
        if 0 <= nxt < gt:                       # unclosed: the next `<` starts over
            i = nxt
            continue
        j = i + 1
        while j < gt and (html[j].isalnum() or html[j] in "-:_"):
            j += 1
        name = html[i + 1:j].lower()
        if name in names:
            yield name, {m[1].lower(): m[2] if m[2] is not None else m[3] if m[3] is not None else m[4]
                         for m in _ATTR.finditer(html, j, gt)}
        i = nxt


def tag_text(html: str, name: str, attrs: dict, limit: int = 2_000_000) -> str | None:
    """The text inside the first <name ...> whose attributes include `attrs`
    (case-insensitive values), up to its closing tag; None if absent.
    Linear like iter_tags: the `>` that closes a candidate is searched once
    and reused while later candidates start before it."""
    html = html or ""
    low = ascii_lower(html)
    pos, gt, open_, close = 0, -1, "<" + ascii_lower(name), f"</{ascii_lower(name)}"
    while (i := low.find(open_, pos)) >= 0:
        j = i + len(open_)
        if j < len(low) and not (low[j].isspace() or low[j] in "/>"):
            pos = j                             # <scripts>, <titlebar>: another tag
            continue
        if gt <= i:
            gt = low.find(">", j)
            if gt < 0:
                return None                     # nothing after can close either
        nxt = low.find("<", i + 1, gt)
        if nxt >= 0:                            # unclosed: the next `<` starts over
            pos = nxt
            continue
        pos = gt + 1
        a = {m[1].lower(): (m[2] if m[2] is not None else m[3] if m[3] is not None else m[4]) or ""
             for m in _ATTR.finditer(html, j, gt)}
        if all(a.get(k.lower(), "").lower() == v.lower() for k, v in attrs.items()):
            end = low.find(close, gt, gt + 1 + limit)
            return html[gt + 1:end] if end >= 0 else None
    return None


def next_data(html: str):
    """The __NEXT_DATA__ JSON of a Next.js page, or None."""
    raw = tag_text(html, "script", {"id": "__NEXT_DATA__"})
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def walk(node):
    """Every dict inside a JSON tree, in document order."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            yield n
            stack.extend(reversed(list(n.values())))
        elif isinstance(n, list):
            stack.extend(reversed(n))


def parse_feed(data: bytes):
    """feedparser on a response body. Always a stream: given a str or bytes,
    feedparser first tries the value as a local file name, and a str that
    looks like a url (http, file, ...) is fetched with urllib -- a body could
    make it read runner files or reach addresses net refuses."""
    return feedparser.parse(io.BytesIO(data if isinstance(data, bytes) else str(data).encode("utf-8")))


# ---- hosts ------------------------------------------------------------------
def host_of(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return ""


def host_in(url: str, suffixes) -> bool:
    h = host_of(url)
    return any(h == s or h.endswith("." + s) for s in suffixes)


def is_youtube(url: str) -> bool:
    return host_in(url, ("youtube.com", "youtu.be"))


# Podcast episode pages (小宇宙, Firstory): transcribed by media,
# summarised from that transcript only.
EPISODE_RE = re.compile(
    r"^https?://(?:www\.)?xiaoyuzhoufm\.com/episode/([0-9a-f]{16,32})"
    r"|^https?://open\.firstory\.(?:fm|me)/story/([0-9a-z]{16,40})", re.I)


def is_episode(url: str) -> bool:
    return bool(EPISODE_RE.match(url or ""))



# ---- archive I/O: one line per top-level key, one line per item -------------
# Every writer of archive.json goes through here; a second format would make
# each script reflow the whole file and every commit a full-file diff.
_C = (",", ":")


def dumps(payload) -> str:
    j = lambda v: json.dumps(v, ensure_ascii=False, separators=_C)
    rows = lambda items, pad: ",\n".join(pad + j(it) for it in items)
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        head = [f" {j(k)}: {j(v)}," for k, v in payload.items() if k != "items"]
        parts = ["{", *head, ' "items": [', rows(payload["items"], "  "), " ]", "}"]
    elif isinstance(payload, list):
        parts = ["[", rows(payload, " "), "]"]
    else:
        return j(payload) + "\n"
    return "\n".join(p for p in parts if p) + "\n"


def write_atomic(path, payload) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dumps(payload))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_doc(path) -> dict:
    """Load an archive, refusing to continue on a corrupt file (writing now
    would discard everything)."""
    p = Path(path)
    if not p.exists():
        return {"items": []}
    raw = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(raw)
    except Exception as e:
        raise SystemExit(f"ERROR: {p} is not valid JSON ({e}); restore it "
                         f"(`git checkout -- {p}`) or delete it deliberately.")
    if isinstance(doc, list):
        doc = {"items": doc}
    doc["items"] = [it for it in doc.get("items") or [] if isinstance(it, dict)]
    return doc


def save_doc(path, doc: dict) -> None:
    """Items are written with ITEM_FIELDS only (existing key order kept): scratch
    fields and stray keys can never reach a commit."""
    items = [{k: v for k, v in it.items() if k in ITEM_FIELDS} for it in doc["items"]]
    write_atomic(path, {**{k: v for k, v in doc.items() if k not in ("items", "total_items")},
                        "total_items": len(items), "items": items})


# ---- subtitle files: {id}.{orig_lang}.{sub_lang}.vtt -------------------------
# A contract between media (writer) and summarize (reader), like the
# archive format above. Track priority -- no machine translation beats
# everything, then manual beats auto, then language:
#   0 manual zh  1 manual orig  2 manual other  3 auto orig  4 auto zh  5 auto other  6 chained
NON_LANG = {"live-chat", "rechat"}          # yt-dlp lists chat replays as tracks
ZH_REGIONS = {"tw", "hk", "cn", "mo", "sg"}
ZH_BASE = {"zh", "zh-hant", "zh-hans", "zh-chs", "zh-cht"}
RANKS = {(True, "zh"): 0, (True, "orig"): 1, (True, "other"): 2,
         (False, "orig"): 3, (False, "zh"): 4, (False, "other"): 5, "chained": 6}
# Filenames don't record manual/auto; zh on disk is probably manual anyway.
FILE_RANKS = {"zh": 0, "orig": 1, "other": 2, "chained": 3}


def norm(lang) -> str:
    return (lang or "").strip().lower().replace("_", "-")


def is_chained(lang) -> bool:
    """zh-Hant-en is YouTube re-translating an auto track; zh-Hant-TW is a locale."""
    l = norm(lang)
    return any(l.startswith(b) and l[len(b):] not in ZH_REGIONS for b in ("zh-hant-", "zh-hans-"))


def is_zh(lang) -> bool:
    l = norm(lang)
    return bool(l) and not is_chained(l) and (
        l in ZH_BASE or (l.split("-")[0] == "zh" and l.split("-")[-1] in ZH_REGIONS))


def lang_class(lang, orig) -> str:
    if is_chained(lang):
        return "chained"
    if is_zh(lang):
        return "zh"
    return "orig" if norm(lang) and norm(lang) == norm(orig) else "other"


def track_rank(lang, orig, manual: bool) -> int:
    c = lang_class(lang, orig)
    return RANKS["chained"] if c == "chained" else RANKS[(bool(manual), c)]


def file_rank(sub_lang, orig) -> int:
    return FILE_RANKS[lang_class(sub_lang, orig)]


def choose_track(manual, auto, orig):
    """(is_manual, lang) from yt-dlp's subtitles / automatic_captions, or None.
    Ties: manual first, then language code, so the choice is stable."""
    cands = [(track_rank(l, orig, m), not m, l, m)
             for m, tracks in ((True, manual), (False, auto))
             for l in (tracks or {}) if norm(l) not in NON_LANG]
    if not cands:
        return None
    _, _, lang, is_manual = min(cands)
    return is_manual, lang


def pick_subtitle(sub_dir, item_id: str) -> Path | None:
    """Best local .vtt for an item: {id}.{orig}.{sub}.vtt, by file_rank."""
    best = None
    for p in Path(sub_dir).glob(f"{item_id}.*.vtt") if valid_id(item_id) else ():
        parts = p.name[len(item_id) + 1:].split(".")
        if len(parts) == 3:
            key = (file_rank(parts[1], parts[0]), p.name)
            best = min(best or (key, p), (key, p))
    return best[1] if best else None


def prune_subtitles(sub_dir, item_id: str) -> list[str]:
    """Keep only pick_subtitle's choice among an item's .vtt files, so each id
    has at most one subtitle on disk. Nothing is deleted when no file is a
    valid choice. Returns the names removed."""
    best = pick_subtitle(sub_dir, item_id)
    if best is None:
        return []
    gone = [p for p in Path(sub_dir).glob(f"{item_id}.*.vtt") if p != best]
    for p in gone:
        p.unlink(missing_ok=True)
    return [p.name for p in gone]
