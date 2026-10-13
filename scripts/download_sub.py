#!/usr/bin/env python3
"""Fetch subtitles for pending YouTube items; fall back to local ASR.
Podcast episodes (小宇宙, Firstory) go straight to ASR on their mp3/m4a.

Files are {item_id}.{orig_lang}.{sub_lang}.vtt (yt-dlp's naming), which is all
summarize_feed looks at (pick_subtitle). ASR = faster-whisper (CPU int8) on
audio from yt-dlp (YouTube) or common.download (podcasts), with a per-run
budget in audio-seconds -- the unit that actually has to fit.

Track priority, shared with summarize_feed: no machine translation beats
everything (MT mangles proper nouns), then manual beats auto (auto tracks lack
punctuation), then language:

    0 manual zh   1 manual orig   2 manual other
    3 auto orig   4 auto zh       5 auto other   6 chained (zh-Hant-en...)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import common
import podcast
from common import (BLANK_SUMMARY, STATUS_FILE_NAME, UTC, is_pending, is_youtube, load_doc,
                    record_status, safe_lang, save_doc, valid_id)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"   # yt-dlp only
# The JS challenge solver comes from the pinned yt-dlp-ejs package (yt-dlp[default]);
# `--remote-components ejs:github` would run unpinned code fetched at runtime.
BASE_ARGS = ["--user-agent", UA, "--no-progress", "--socket-timeout", "30"]
SUB_ARGS = ["--extractor-args", "youtube:player_client=web", "--ignore-no-formats"]
AUDIO_ARGS = ["--extractor-args", "youtube:player_client=tv,web_safari,default",
              "-f", "bestaudio[abr<=64]/bestaudio/bestaudio*/best"]
EXHAUSTED_UNDER = 10 * 60    # remaining ASR budget too small for any real video
PODCAST_MAX_BYTES = 512 * 1024 * 1024


# ---- track priority ------------------------------------------------------------
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


def run(cmd: list[str], timeout: float) -> tuple[int | None, str, str]:
    """(returncode | None on timeout, stdout, stderr). A timeout kills the
    whole process group so no yt-dlp grandchild outlives it."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):     # KILL reaches stragglers
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return None, out or "", err or ""


def ytdlp(cookies: Path, url: str, *args) -> list[str]:
    """`--` ends option parsing: a url can never be read as a flag."""
    return ["yt-dlp", "--cookies", str(cookies), *BASE_ARGS, *args, "--", url]


def cookies_rejected(stderr: str) -> bool:
    """Only yt-dlp's own diagnostics: stdout carries remote metadata, and a
    video titled "cookies" must not end the run."""
    return "cookies" in (stderr or "").lower()


def discard(out_dir: Path, item_id: str) -> list[str]:
    gone = [p for pat in (f"{item_id}*.vtt", f"{item_id}*.part") for p in out_dir.glob(pat)]
    for p in gone:
        p.unlink(missing_ok=True)
    return [p.name for p in gone]


# ---- ASR --------------------------------------------------------------------

def ts(sec: float) -> str:
    ms = int(round(max(sec, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def to_vtt(segments) -> str:
    """(start, end, text) -> WebVTT; overlapping/inverted cues are clamped."""
    lines, prev = ["WEBVTT", ""], 0.0
    for start, end, text in segments:
        text = " ".join(str(text).split())
        if not text:
            continue
        start = max(float(start), 0.0, prev)
        end = float(end) if float(end) > start else start + 0.5
        lines += [f"{ts(start)} --> {ts(end)}", text, ""]
        prev = end
    return "\n".join(lines) if len(lines) > 2 else ""


class NoSpeech(RuntimeError):
    """The audio has nothing to transcribe: a fact about the item."""


class TooLong(RuntimeError):
    """Longer than --max-asr-duration: a fact about the item."""


_models = {}


def transcribe(fetch, out_dir: Path, item_id, orig, a, duration) -> tuple[Path, float]:
    """(vtt path, audio seconds). `fetch(dir, seconds)` puts the audio in dir
    and returns its path. NoSpeech / TooLong are permanent; anything else
    (missing whisper, download failure, timeout) is transient."""
    from faster_whisper import WhisperModel
    with tempfile.TemporaryDirectory(prefix=f"asr-{item_id}-") as d:
        # ~1s of download per 4s of audio, floor --audio-timeout, cap 45 min
        timeout = min(max(a.audio_timeout, duration * 0.25), 45 * 60) if duration else a.audio_timeout
        audio = fetch(Path(d), timeout)
        key = (a.whisper_model, a.whisper_compute_type)
        if key not in _models:
            _models[key] = WhisperModel(a.whisper_model, device="cpu", compute_type=key[1],
                                        cpu_threads=os.cpu_count() or 2)
        # PyAV inside faster-whisper decodes the stream as-is; no ffmpeg needed.
        segs, info = _models[key].transcribe(str(audio), beam_size=1, vad_filter=True,
                                             condition_on_previous_text=False)
        if a.max_asr_duration and (info.duration or 0) > a.max_asr_duration:
            raise TooLong(f"too long for ASR: {info.duration / 60:.0f} min")
        detected = safe_lang(info.language)
        vtt = to_vtt((s.start, s.end, s.text) for s in segs)
    if not vtt:
        raise NoSpeech("no speech segments")
    path = out_dir / f"{item_id}.{safe_lang(orig, detected)}.{detected}.vtt"
    path.write_text(vtt, encoding="utf-8")
    return path, float(info.duration or duration or 0)


def youtube_audio(cookies: Path, url: str):
    def fetch(d: Path, timeout: float) -> Path:
        rc, out, err = run(ytdlp(cookies, url, *AUDIO_ARGS, "-o", f"{d}/audio.%(ext)s"), timeout)
        files = sorted(p for p in d.glob("audio.*") if p.suffix != ".part")
        if rc is None:
            raise RuntimeError(f"audio download timed out after {timeout:.0f}s")
        if rc != 0 or not files:
            raise RuntimeError(f"audio download failed: {(out + err).strip()[-1500:]}")
        return files[0]
    return fetch


def direct_audio(url: str):
    """A podcast file, through common.download: every redirect hop and every
    connection is held to public addresses, unlike a yt-dlp download."""
    def fetch(d: Path, timeout: float) -> Path:
        ext = Path(url.split("?", 1)[0]).suffix.lower()
        dest = d / ("audio" + (ext if ext in (".mp3", ".m4a", ".aac") else ".bin"))
        if not common.download(url, dest, timeout, PODCAST_MAX_BYTES):
            raise RuntimeError("audio download failed, timed out or too large")
        return dest
    return fetch


class Budget:
    def __init__(self, a):
        self.a, self.count, self.spent, self.hit_wall = a, 0, 0.0, False

    def left(self) -> float:
        return self.a.max_asr_total - self.spent if self.a.max_asr_total else float("inf")

    def stop(self) -> bool:
        """Turned something away for budget, and what's left fits no video."""
        return self.hit_wall and self.left() <= EXHAUSTED_UNDER

    def refuse(self, duration, live) -> tuple[str, bool] | None:
        """(reason, permanent). Only the video's own length is permanent; the
        rest is about this run and must leave the item pending."""
        a = self.a
        if a.no_transcribe:
            return "transcription disabled", False
        if importlib.util.find_spec("faster_whisper") is None:
            return "faster-whisper not installed", False
        if live:
            return "still live", False
        if a.max_transcribe and self.count >= a.max_transcribe:
            return "per-run item cap reached", False
        if a.max_asr_duration and duration > a.max_asr_duration:
            return f"{duration / 60:.0f} min > {a.max_asr_duration / 60:.0f} min cap", True
        if a.max_asr_total and duration > self.left():
            self.hit_wall = True
            return f"{duration / 60:.0f} min > {self.left() / 60:.0f} min budget left", False
        return None

    def __str__(self):
        return f"asr={self.count} audio={self.spent / 60:.0f}min" + (
            f"/{self.a.max_asr_total / 60:.0f}min" if self.a.max_asr_total else "")


def asr(item, orig, duration, live, a, budget, out_dir, n, fetch, is_podcast=False) -> bool:
    """Returns whether the item changed. Only the item's own nature ends it
    (BLANK): no speech, or -- for a video -- longer than the cap. A podcast
    over the cap, and every run-level failure, stays pending."""
    item_id = item["id"]
    refused = budget.refuse(duration, live)
    if refused:
        why, permanent = refused
        blank = permanent and not is_podcast
        print(f"[ASR-SKIP] {item_id}: {why}" + ("" if blank else " (kept pending)"))
    else:
        try:
            path, secs = transcribe(fetch, out_dir, item_id, orig, a, duration)
            budget.count += 1
            budget.spent += max(secs or duration, 0.0)
            n["succeeded"] += 1
            print(f"[ASR] {item_id}: {path.name} ({(secs or duration) / 60:.0f}min, {budget})")
            return False
        except Exception as e:
            blank = isinstance(e, NoSpeech) or (isinstance(e, TooLong) and not is_podcast)
            print(f"[ASR-FAILED] {item_id}: {type(e).__name__}: {e}" + ("" if blank else " (kept pending)"))
        discard(out_dir, item_id)
    if blank:
        item["summary"] = BLANK_SUMMARY
        n["no_subs"] += 1
        return True
    n["deferred"] += 1
    return False


def podcast_asr(item, a, budget, out_dir, n) -> bool:
    """Resolve the episode page to its mp3/m4a, then the shared ASR path."""
    url, item_id = item["url"], item["id"]
    try:
        ep = podcast.resolve(url)
    except Exception as e:
        ep = None
        print(f"[PODCAST] {item_id}: resolve error {type(e).__name__}: {e}")
    if ep is None:
        n["failed"] += 1
        print(f"[PODCAST] {item_id}: no audio found on {url} (kept pending, retried next run)")
        return False
    dirty = False
    if ep.duration and item.get("length") != int(ep.duration):
        item["length"] = int(ep.duration)          # stored for the frontend
        dirty = True
    print(f"[PODCAST] {item_id}: {ep.audio_url} ({ep.duration / 60:.0f}min)")
    return asr(item, ep.lang, ep.duration, False, a, budget, out_dir, n,
               direct_audio(ep.audio_url), is_podcast=True) or dirty


def remove_orphans(out_dir: Path, ids: set) -> int:
    """Delete .vtt files whose item left the archive. An empty id set (a
    missing or empty archive) deletes nothing."""
    orphans = [p for p in out_dir.glob("*.vtt") if ids and p.name.split(".", 1)[0] not in ids]
    for p in orphans:
        p.unlink()
    return len(orphans)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--archive", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--cookies-path", required=True)
    ap.add_argument("--max-items", type=int, default=70)
    ap.add_argument("--probe-timeout", type=float, default=90)
    ap.add_argument("--download-timeout", type=float, default=240)
    ap.add_argument("--no-transcribe", action="store_true")
    ap.add_argument("--whisper-model", default=os.environ.get("WHISPER_MODEL", "base"))
    ap.add_argument("--whisper-compute-type", default=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"))
    ap.add_argument("--audio-timeout", type=float, default=300)
    ap.add_argument("--max-asr-duration", type=float, default=3 * 3600, help="0 = unlimited")
    ap.add_argument("--max-transcribe", type=int, default=20, help="0 = unlimited")
    ap.add_argument("--max-asr-total", type=float, default=4 * 3600, metavar="SECONDS",
                    help="audio-seconds to transcribe per run (0 = unlimited)")
    ap.add_argument("--status-file", default=None,
                    help=f"health flags for the reader (default: {STATUS_FILE_NAME} next to --archive)")
    a = ap.parse_args(argv)

    status_file = a.status_file or Path(a.archive).with_name(STATUS_FILE_NAME)
    out_dir, cookies = Path(a.output_dir), Path(a.cookies_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = load_doc(a.archive)
    if n := remove_orphans(out_dir, {it.get("id") for it in doc["items"]}):
        print(f"Removed {n} orphaned subtitle file(s).")
    have = {p.name.split(".", 1)[0] for p in out_dir.glob("*.vtt")}
    budget, n, dirty = Budget(a), Counter(), False
    cookies_expired = False

    todo = [it for it in doc["items"] if valid_id(it.get("id")) and is_pending(it)
            and (is_youtube(it["url"]) or podcast.is_episode(it["url"])) and it["id"] not in have]
    for item in todo[:a.max_items]:
        if budget.stop():
            print(f"[BUDGET] {budget.left() / 60:.0f} min of ASR left; rest stay pending")
            break
        url, item_id = item["url"], item["id"]
        n["processed"] += 1
        if podcast.is_episode(url):
            dirty |= podcast_asr(item, a, budget, out_dir, n)
            continue
        n["probed"] += 1                     # a YouTube probe: the only evidence about the cookies
        rc, out, err = run(ytdlp(cookies, url, *SUB_ARGS, "--skip-download", "--dump-json"),
                           a.probe_timeout)
        if cookies_rejected(err):
            print("[EXPIRED] cookies invalid")
            cookies_expired = True
            break
        try:
            info = json.loads(out.strip().splitlines()[-1]) if rc == 0 else None
        except Exception:
            info = None
        if not info:
            n["failed"] += 1
            print(f"[FAILED] {item_id}: probe failed")
            continue
        orig = safe_lang(info.get("language"), None) if info.get("language") else None
        try:
            duration = max(float(info.get("duration") or 0), 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        live = bool(info.get("is_live") or info.get("live_status") == "is_live")
        if duration and item.get("length") != int(duration):
            item["length"] = int(duration)          # stored for the frontend
            dirty = True
        fallback = lambda: asr(item, orig, duration, live, a, budget, out_dir, n, youtube_audio(cookies, url))

        # track keys come from remote metadata and become a --sub-langs regex
        tracks = [{k: v for k, v in (info.get(f) or {}).items() if safe_lang(k, None)}
                  for f in ("subtitles", "automatic_captions")]
        choice = choose_track(*tracks, orig)
        if choice is None:
            dirty |= fallback()
        else:
            manual, lang = choice
            print(f"[TRACK] {item_id}: {lang} ({'manual' if manual else 'auto'}, orig={orig}, "
                  f"rank={track_rank(lang, orig, manual)})")
            rc, out, err = run(ytdlp(cookies, url, *SUB_ARGS, "--skip-download",
                                     "--write-sub" if manual else "--write-auto-sub",
                                     "--sub-langs", lang, "--sub-format", "vtt",
                                     "--sleep-interval", "4", "--max-sleep-interval", "7",
                                     # no remote field in the filename: orig is safe_lang'd
                                     "-o", str(out_dir / f"{item_id}.{orig or 'und'}.%(ext)s")),
                               a.download_timeout)
            log = (out + err).lower()
            if cookies_rejected(err):
                print("[EXPIRED] cookies invalid")
                cookies_expired = True
                break
            if rc is None:
                n["failed"] += 1
                print(f"[TIMEOUT] {item_id}: cleaned {discard(out_dir, item_id)}")
            elif rc == 0 and any(out_dir.glob(f"{item_id}*.vtt")):
                n["succeeded"] += 1
            elif rc == 0 and "no subtitles for the requested languages" in log:
                dirty |= fallback()
            else:
                n["failed"] += 1
                print(f"[FAILED] {item_id} (exit {rc}): {(out + err)[-1500:]}")

    if dirty:
        save_doc(a.archive, doc)
    # No YouTube video was probed (podcasts use no cookies): leave the flag alone.
    if cookies_expired or n["probed"]:
        record_status(status_file, "cookies", "expired" if cookies_expired else "ok",
                      datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    print(f"Done. {dict(n)} ({budget})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
