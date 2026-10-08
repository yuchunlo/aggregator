"""Text to summary: caption parsing, boilerplate removal, extractive
selection (TF-IDF + entities + lead bias, MMR de-duplication), translation."""

from __future__ import annotations

import html as html_mod
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

import lang
from common import BLANK_SUMMARY, FALLBACK_MARK
from extract import sanitize_markup, session

SUMMARY_RATIO = float(os.environ.get("SUMMARY_RATIO", "1"))
SUMMARY_MAX = int(os.environ.get("SUMMARY_MAX", "60000"))
TRANSLATE = os.environ.get("TRANSLATE", "on").lower() != "off"
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
    src = lang.detect(text) if text else ""
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
BOILER_FILE = Path(os.environ.get("BOILERPLATE_FILE",
                                  Path(__file__).with_name("summary_boilerplate.json")))
KINDS = ("page", "meta", "feed", "subtitle", "bridge")
_rules: dict | None = None


def rules() -> dict:
    """summary_boilerplate.json compiled once. Actions run in a fixed order:
    remove_block, remove_inline, cut_to_end (on source text), drop_unit (on
    繁體-normalised text, per sentence/paragraph and source kind)."""
    global _rules
    if _rules is None:
        try:
            data = json.loads(BOILER_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARNING: {BOILER_FILE} unreadable ({e}); no boilerplate rules")
            data = {}
        blocks, inline, cut, units = [], [], [], {}
        for r in data.get("rules") or []:
            act, pat = r.get("action"), r.get("pattern") or ""
            if act == "remove_block" and r.get("text"):
                blocks.append(r["text"])
            elif act in ("remove_inline", "cut_to_end", "drop_unit") and pat:
                try:
                    rx = re.compile(pat)
                except re.error as e:
                    print(f"WARNING: bad boilerplate regex {pat!r} ({e})")
                    continue
                if act == "drop_unit":
                    units.setdefault((r.get("level") or "sentence", r.get("scope") or "all"), []).append(rx)
                else:
                    (inline if act == "remove_inline" else cut).append(pat)
            else:
                print(f"WARNING: bad boilerplate rule {r!r}")
        _rules = {"blocks": blocks, "units": units,
                  "inline": re.compile("|".join(inline), re.M) if inline else None,
                  "cut": re.compile("|".join(cut)) if cut else None,
                  "min_keep": int(data.get("min_keep_after_cut", 80))}
    return _rules


# Markup in the text is the article's subject (a tutorial's <form> sample), not
# extraction debris. Shared with summary_qc so both agree on what is code.
_CODE_CTX = re.compile(r"HTML|CSS|JavaScript|TypeScript|JSX|React|Vue|Svelte|DOM|XML|SVG|前端|網頁設計|"
                       r"標籤|元素|屬性|範例|程式碼|語法|原始碼|模板|tag|element|attribute|markup|snippet", re.I)
_CODE_TOK = re.compile(r"\bfunction\b|\bconst\b|=>|\bimport\b|\bdef\b|\breturn\b|onClick|useState|"
                       r"\{\s*\}|`[^`]{1,80}`|「\s*<[^」]{1,80}」")


def looks_like_code(s: str) -> bool:
    return CODE_NOTE in s or bool(_CODE_CTX.search(s)) or len(_CODE_TOK.findall(s)) >= 3


def strip_boilerplate(text: str, markup: bool = True) -> str:
    """markup=False keeps HTML-looking text (code samples) untouched."""
    r = rules()
    if markup:
        text = sanitize_markup(text)
    for b in r["blocks"]:
        text = text.replace(b, "")
    if r["inline"]:
        text = r["inline"].sub("", text)
    if r["cut"] and (m := r["cut"].search(text)) and m.start() >= r["min_keep"]:
        text = text[:m.start()]
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
    probe = lang.key_text(text)
    units = rules()["units"]
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


def split_sentences(text: str, cjk: bool) -> list[str]:
    out = []
    for line in re.sub(r"\n{2,}", "\n", _BULLET.sub("\n", text)).split("\n"):
        for s in (_ZH_SPLIT if cjk else _EN_SPLIT).split(line.strip()):
            s = s.strip()
            if s and (len(s) >= 8 if cjk else len(s.split()) >= 5):
                out.append(s)
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


def extractive(text: str, cjk: bool, budget: int, kind: str) -> str:
    sents = split_sentences(text, cjk)
    if not sents:
        return trim_to_sentences(text, budget)
    keep = [s for s in sents if not is_boilerplate(s, kind)]
    if keep and len(keep) < len(sents):
        STATS[f"boiler:{kind}:sentence"] += len(sents) - len(keep)
        sents = keep
    uniq = {}                                   # verbatim repeats: first wins
    for s in sents:
        uniq.setdefault(re.sub(r"\W+", "", s.lower()), s)
    sents = [s for k, s in uniq.items() if k]

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
    return ("" if cjk else " ").join(c[2] for c in sorted(chosen, key=lambda c: c[1]))


# ============================================================== assembly
_CJK_PUNCT = "\u4e00-\u9fff，。！？；：、（）「」"


def translate(text: str) -> str | None:
    """zh-TW translation, recording why it failed."""
    out, reason = lang.translate_one(text, session)
    if not out:
        STATS[f"translate-fail:{lang.short_reason(reason) or 'unknown'}"] += 1
    return out


def build(content: str, kind: str = "page", table=False, code=False, meta=False) -> str | None:
    """Extractive summary of `content`, in 繁中, plus trailing marks.

    kind: page | feed | subtitle | bridge (selects boilerplate scope);
    meta: only a blurb was available (marked ↛).
    Returns "" when only boilerplate remains, None when the text is
    undecodable (mojibake): that is a fetch problem, so the item stays pending.
    """
    content = lang.collapse_loops(lang.fix_mojibake(content))   # Whisper/MT loops: keep one copy
    if lang.garbled(content):
        STATS["garbled"] += 1
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
        STATS[f"boiler:{kind}:paragraph"] += len(paras) - len(kept)
        content = "\n".join(kept)

    src = lang.detect(content)
    budget = max(1, min(SUMMARY_MAX, int(len(content) * SUMMARY_RATIO)) - len(suffix))
    if src.startswith("zh"):
        summary = extractive(content, True, budget, kind)
    else:
        raw = extractive(content, src == "ja", int(budget * FOREIGN_BUDGET), kind)
        summary = translate(raw) if TRANSLATE else None
        if not summary:                   # stored as-is; backfill retries later
            STATS[f"untranslated:{src}"] += 1
            summary = raw
    summary = lang.to_twp(summary)
    if lang.detect(summary) in ("other",):
        summary = re.sub(r"\s+", " ", summary).strip()
    else:
        summary = re.sub(rf"(?<=[{_CJK_PUNCT}])\s+|\s+(?=[{_CJK_PUNCT}])", "", summary)
        summary = re.sub(r"\s+", " ", summary).strip()
    summary = trim_to_sentences(summary, budget)
    return summary + suffix if summary else ""
