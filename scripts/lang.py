"""Chinese script conversion, language detection, and machine translation.

Translation backends: DeepL (DEEPL_API_KEY, batched, preferred) then Google's
undocumented gtx endpoint (per text, no quota, no SLA).
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

try:
    import opencc
    _S2TWP, _T2S = opencc.OpenCC("s2twp"), opencc.OpenCC("t2s")
except Exception:                      # degrade to identity, never to 簡體
    opencc = _S2TWP = _T2S = None

# ---- OpenCC -----------------------------------------------------------------

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


def key_text(text: str) -> str:
    """Normalised form for rule matching: always 繁體, common variants folded."""
    if not text:
        return text
    return (_repair(_S2TWP.convert(text)) if _S2TWP else text).translate(_FOLD)


# ---- detection --------------------------------------------------------------
# Kana share among CJK separates Japanese from Chinese cleanly: measured real
# Japanese summaries sit at 0.515-0.710, the highest Chinese one at 0.171.
SCRIPTS = {
    "han": r"[\u3400-\u4dbf\u4e00-\u9fff]", "kana": r"[\u3040-\u30ff]",
    "hangul": r"[\uac00-\ud7af]", "cyrillic": r"[\u0400-\u04ff]",
    "greek": r"[\u0370-\u03ff]", "hebrew": r"[\u0590-\u05ff]",
    "arabic": r"[\u0600-\u06ff]", "devanagari": r"[\u0900-\u097f]",
    "thai": r"[\u0e00-\u0e7f]",
}
SCRIPTS = {k: re.compile(v) for k, v in SCRIPTS.items()}
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
LOOP_MIN, LOOP_MIN_SHORT, LOOP_MIN_CYCLE = 10, 15, 5
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


def garbled(text: str) -> bool:
    """Unrepaired mojibake, replacement characters, or a degenerate loop
    (machine translation of garbage repeats one phrase dozens of times)."""
    if not text:
        return False
    chars = max(1, sum(not c.isspace() for c in text))
    if text.count("\ufffd") / chars > 0.02:
        return True
    def pairs(m, run):           # UTF-8 lead byte followed by a continuation byte
        return sum(x in _LEAD and y in _CONT for mo in run.finditer(text)
                   for b in [[m[c] for c in mo.group()]] for x, y in zip(b, b[1:]))
    if max(pairs(m, run) for m, run in _MOJI.values()) * 2 / chars > 0.05:
        return True
    return next(_unit_loops(text), None) is not None


# ---- translation ------------------------------------------------------------
GTX = "https://translate.googleapis.com/translate_a/single"
GTX_CHUNK_BYTES = 2000       # q is in the query string: limit *encoded* bytes
GTX_SLEEP = 1.0
TIMEOUT = 30
DEEPL_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
DEEPL_BATCH, DEEPL_MAX_CHARS = 50, 20_000
_SENT = re.compile(r"(?<=[.!?。！？])\s*")      # zero-width: CJK has no spaces

deepl_on = bool(DEEPL_KEY)           # flips off for good on quota/auth failure
PROVIDERS: Counter = Counter()       # successful texts per backend
FAILURES: Counter = Counter()        # DeepL failures that fell back to gtx


class TranslateError(Exception):
    pass


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


def _gtx(text: str, session) -> str:
    out = []
    for i, chunk in enumerate(chunk_text(text)):
        if i:
            time.sleep(GTX_SLEEP)
        try:
            r = session.get(GTX, params={"client": "gtx", "sl": "auto", "tl": "zh-TW",
                                         "dt": "t", "q": chunk}, timeout=TIMEOUT)
        except Exception as e:
            raise TranslateError(f"{type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise TranslateError(f"HTTP {r.status_code}")
        try:
            segs = r.json()[0]
            out.append("".join(str(s[0]) for s in segs if isinstance(s, list) and s and s[0]))
        except Exception as e:
            raise TranslateError(f"bad payload: {type(e).__name__}") from e
    return "".join(out).strip()


def _deepl_host() -> str:
    return "https://api-free.deepl.com" if DEEPL_KEY.endswith(":fx") else "https://api.deepl.com"


def _deepl(texts: list[str], session) -> list[str]:
    """One POST; a 413 halves the batch (DeepL limits bytes, we count chars)."""
    try:
        r = session.post(_deepl_host() + "/v2/translate", timeout=TIMEOUT,
                         headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}"},
                         json={"text": texts, "target_lang": "ZH-HANT"})
    except Exception as e:
        raise TranslateError(f"deepl {type(e).__name__}: {e}") from e
    if r.status_code == 413 and len(texts) > 1:
        mid = len(texts) // 2
        return _deepl(texts[:mid], session) + _deepl(texts[mid:], session)
    if r.status_code == 456:
        raise TranslateError("deepl quota exceeded (456)")
    if r.status_code != 200:
        raise TranslateError(f"deepl HTTP {r.status_code}")
    try:
        out = [t["text"] for t in r.json()["translations"]]
    except Exception as e:
        raise TranslateError(f"deepl bad payload: {type(e).__name__}") from e
    if len(out) != len(texts):          # misaligned results would cross-write items
        raise TranslateError(f"deepl returned {len(out)} of {len(texts)} segments")
    return out


def deepl_usage(session):
    if not deepl_on:
        return None
    try:
        d = session.get(_deepl_host() + "/v2/usage", timeout=15,
                        headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}"}).json()
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


def translate_many(texts: list[str], session, stop_after: int = 0) -> list[tuple]:
    """[(translation | None, reason)] aligned with `texts`, into 繁體中文.

    stop_after: after this many consecutive gtx failures, stop sending
    requests; the rest come back as skipped (the endpoint is refusing us).
    """
    global deepl_on
    res = [(None, "empty input")] * len(texts)
    todo = [i for i, t in enumerate(texts) if (t or "").strip()]
    for i in [i for i in todo if garbled(texts[i])]:     # never pay to translate garbage
        res[i] = (None, "garbled input")
        todo.remove(i)
    streak = 0
    while todo:
        batch, chars = [], 0
        while todo and len(batch) < DEEPL_BATCH and not (
                batch and chars + len(texts[todo[0]]) > DEEPL_MAX_CHARS):
            chars += len(texts[todo[0]])
            batch.append(todo.pop(0))
        if deepl_on:
            try:
                for i, o in zip(batch, _deepl([texts[i].strip() for i in batch], session)):
                    res[i] = _accept(o)
                PROVIDERS["deepl"] += len(batch)
                continue
            except TranslateError as e:
                FAILURES[short_reason(str(e))] += 1
                if any(k in str(e) for k in ("quota", "403", "401")):
                    deepl_on = False
        for i in batch:
            if stop_after and streak >= stop_after:
                res[i] = (None, "skipped: gtx refusing this run")
                continue
            try:
                res[i] = _accept(_gtx(texts[i], session))
            except TranslateError as e:
                res[i] = (None, str(e))
            if res[i][0]:
                streak = 0
                PROVIDERS["gtx"] += 1
            else:
                streak += 1
            time.sleep(GTX_SLEEP)
    return res


def translate_one(text: str, session) -> tuple:
    return translate_many([text], session)[0]
