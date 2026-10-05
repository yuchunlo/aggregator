#!/usr/bin/env python3
"""Fetch subtitles for pending YouTube items; fall back to local ASR.

Files are {item_id}.{orig_lang}.{sub_lang}.vtt (yt-dlp's naming), which is all
summarize_feed looks at. ASR = yt-dlp audio + faster-whisper (CPU int8), with
a per-run budget in audio-seconds -- the unit that actually has to fit.
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
from pathlib import Path

from common import BLANK_SUMMARY, is_youtube, load_doc, safe_lang, save_doc, valid_id
from subtitle_priority import choose_track, track_rank

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
# The JS challenge solver comes from the pinned yt-dlp-ejs package (yt-dlp[default]);
# `--remote-components ejs:github` would run unpinned code fetched at runtime.
BASE_ARGS = ["--user-agent", UA, "--no-progress", "--socket-timeout", "30"]
SUB_ARGS = ["--extractor-args", "youtube:player_client=web", "--ignore-no-formats"]
AUDIO_ARGS = ["--extractor-args", "youtube:player_client=tv,web_safari,default",
              "-f", "bestaudio[abr<=64]/bestaudio/bestaudio*/best"]
EXHAUSTED_UNDER = 10 * 60    # remaining ASR budget too small for any real video


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


class Unavailable(RuntimeError):
    """Environment problem (whisper missing): retry next run."""


_models = {}


def transcribe(url, cookies, out_dir: Path, item_id, orig, a, duration) -> Path:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise Unavailable("faster-whisper not installed")
    with tempfile.TemporaryDirectory(prefix=f"asr-{item_id}-") as d:
        # ~1s of download per 4s of audio, floor 5 min, cap 45 min
        timeout = min(max(a.audio_timeout, duration * 0.25), 45 * 60) if duration else a.audio_timeout
        rc, out, err = run(ytdlp(cookies, url, *AUDIO_ARGS, "-o", f"{d}/audio.%(ext)s"), timeout)
        files = sorted(p for p in Path(d).glob("audio.*") if p.suffix != ".part")
        if rc is None:
            raise RuntimeError(f"audio download timed out after {timeout:.0f}s")
        if rc != 0 or not files:
            raise RuntimeError(f"audio download failed: {(out + err).strip()[-1500:]}")
        key = (a.whisper_model, a.whisper_compute_type)
        if key not in _models:
            _models[key] = WhisperModel(a.whisper_model, device="cpu", compute_type=key[1],
                                        cpu_threads=os.cpu_count() or 2)
        # PyAV inside faster-whisper decodes the stream as-is; no ffmpeg needed.
        segs, info = _models[key].transcribe(str(files[0]), beam_size=1, vad_filter=True,
                                             condition_on_previous_text=False)
        if a.max_asr_duration and (info.duration or 0) > a.max_asr_duration:
            raise RuntimeError(f"too long for ASR: {info.duration / 60:.0f} min")
        detected = safe_lang(info.language)
        vtt = to_vtt((s.start, s.end, s.text) for s in segs)
    if not vtt:
        raise RuntimeError("no speech segments")
    path = out_dir / f"{item_id}.{safe_lang(orig, detected)}.{detected}.vtt"
    path.write_text(vtt, encoding="utf-8")
    return path


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


def asr(item, url, item_id, orig, duration, live, a, budget, out_dir, cookies, n) -> bool:
    """Returns whether the item changed (marked blank)."""
    refused = budget.refuse(duration, live)
    permanent = False
    if refused:
        why, permanent = refused
        print(f"[ASR-SKIP] {item_id}: {why}" + ("" if permanent else " (kept pending)"))
    else:
        try:
            path = transcribe(url, cookies, out_dir, item_id, orig, a, duration)
            budget.count += 1
            budget.spent += max(duration, 0.0)
            n["succeeded"] += 1
            print(f"[ASR] {item_id}: {path.name} ({duration / 60:.0f}min, {budget})")
            return False
        except Unavailable as e:
            print(f"[ASR-FAILED] {item_id}: {e} (kept pending)")
        except Exception as e:                  # the video itself: don't retry
            print(f"[ASR-FAILED] {item_id}: {e}")
            permanent = True
        discard(out_dir, item_id)
    if permanent:
        item["summary"] = BLANK_SUMMARY
        n["no_subs"] += 1
        return True
    n["deferred"] += 1
    return False


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
    a = ap.parse_args(argv)

    out_dir, cookies = Path(a.output_dir), Path(a.cookies_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = load_doc(a.archive)
    if n := remove_orphans(out_dir, {it.get("id") for it in doc["items"]}):
        print(f"Removed {n} orphaned subtitle file(s).")
    have = {p.name.split(".", 1)[0] for p in out_dir.glob("*.vtt")}
    budget, n, dirty = Budget(a), Counter(), False

    todo = [it for it in doc["items"] if valid_id(it.get("id")) and it.get("summary") is None
            and is_youtube(it.get("url", "")) and it["id"] not in have]
    for item in todo[:a.max_items]:
        url, item_id = item["url"], item["id"]
        n["processed"] += 1
        rc, out, err = run(ytdlp(cookies, url, *SUB_ARGS, "--skip-download", "--dump-json"),
                           a.probe_timeout)
        if cookies_rejected(err):
            print("[EXPIRED] cookies invalid")
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
        fallback = lambda: asr(item, url, item_id, orig, duration, live, a, budget, out_dir, cookies, n)

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
        if budget.stop():
            print(f"[BUDGET] {budget.left() / 60:.0f} min of ASR left; rest stay pending")
            break

    if dirty:
        save_doc(a.archive, doc)
    print(f"Done. {dict(n)} ({budget})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
