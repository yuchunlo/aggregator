"""Subtitle track priority, shared by download_sub (chooses what to download)
and summarize_feed (chooses among files on disk).

No machine translation beats everything (MT mangles proper nouns), then
manual beats auto (auto tracks lack punctuation), then language:

    0 manual zh   1 manual orig   2 manual other
    3 auto orig   4 auto zh       5 auto other   6 chained (zh-Hant-en...)
"""

from __future__ import annotations

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
