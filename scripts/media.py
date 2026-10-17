#!/usr/bin/env python3
"""Entry point 2 (downloadsub.yml). Fetch subtitles for pending YouTube items; fall back to local ASR.
Podcast episodes (小宇宙, Firstory) go straight to ASR on their mp3/m4a.
Then, in this same process, summarize summarizes the new transcripts
(offline: no page fetches, no backfill) and the archive is written once.

Configuration is the environment (all set in downloadsub.yml): DATA_DIR,
COOKIES_PATH, MAX_ASR_TOTAL_SECONDS, WHISPER_MODEL,
WHISPER_COMPUTE_TYPE, DEEPL_API_KEY / TRANSLATE (for the summaries).

Files are {item_id}.{orig_lang}.{sub_lang}.vtt (yt-dlp's naming), which is all
summarize looks at (pick_subtitle). ASR = faster-whisper (CPU int8) on
audio from yt-dlp (YouTube) or download (podcasts), with a per-run
budget in audio-seconds -- the unit that actually has to fit.

Track priority (choose_track, shared with summarize): no machine
translation beats everything, then manual beats auto, then language.
"""

from __future__ import annotations

import argparse
import html as html_mod
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


import net
import store
import summarize
import textproc
from store import (BLANK_SUMMARY, UTC, choose_track, prune_subtitles, is_episode, is_pending, is_youtube, load_doc,
                   next_data, safe_lang, save_doc, track_rank, valid_id, walk, write_atomic)

# The JS challenge solver comes from the pinned yt-dlp-ejs package (yt-dlp[default]);
# `--remote-components ejs:github` would run unpinned code fetched at runtime.
BASE_ARGS = ["--user-agent", net.UA, "--no-progress", "--socket-timeout", "30"]
SUB_ARGS = ["--extractor-args", "youtube:player_client=web", "--ignore-no-formats"]
AUDIO_ARGS = ["--extractor-args", "youtube:player_client=tv,web_safari,default",
              "-f", "bestaudio[abr<=64]/bestaudio/bestaudio*/best"]
EXHAUSTED_UNDER = 10 * 60    # remaining ASR budget too small for any real video
PODCAST_MAX_BYTES = 512 * 1024 * 1024


@dataclass
class Limits:
    """Per-run limits; the configurable ones come from store.CFG."""
    max_items: int = 400            # pending media items looked at per run
    # The next four come from store.Config (environment); defaults live there.
    max_transcribe: int = store.Config.max_transcribe
    max_asr_total: float = store.Config.max_asr_total
    max_asr_duration: float = store.Config.max_asr_duration
    probe_timeout: float = 90
    download_timeout: float = 240
    audio_timeout: float = 300
    whisper_model: str = store.Config.whisper_model
    whisper_compute_type: str = store.Config.whisper_compute_type
    no_transcribe: bool = False

    @classmethod
    def from_cfg(cls, cfg, **kw):
        return cls(max_asr_total=cfg.max_asr_total, max_transcribe=cfg.max_transcribe,
                   max_asr_duration=cfg.max_asr_duration, whisper_model=cfg.whisper_model,
                   whisper_compute_type=cfg.whisper_compute_type, **kw)


def record_status(path, key: str, state: str, now: str) -> bool:
    """Record one health flag (cookies "ok" / "expired") in the status file the
    reader shows warnings from. Returns True when the file was written. A flag
    is rewritten only when its state changes, or while it is "expired" (the
    reader needs a fresh `checked_at` to tell a new, still rejected cookies
    upload from an old one): a healthy run makes no daily commit. A missing or
    damaged file starts over: it is only a hint, never the archive."""
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            doc = {}
    except (OSError, ValueError):
        doc = {}
    current = doc.get(key) if isinstance(doc.get(key), dict) else {}
    if current.get("state") == state and state != "expired":
        return False
    doc[key] = {"state": state, "checked_at": now}
    write_atomic(p, doc)
    return True


# yt-dlp runs untrusted extractors against remote pages, so it gets an
# allowlisted environment (what a Python console script, deno and the runner
# need), and even from that anything that looks like a secret is dropped.
CHILD_ENV = re.compile(r"PATH|HOME|USER|LANG|LC_\w+|TZ|TERM|TMPDIR|TMP|TEMP|HF_HOME|XDG_\w+|DENO_\w+"
                       r"|LD_LIBRARY_PATH|PYTHONPATH|PYTHONHOME|pythonLocation|VIRTUAL_ENV|SSL_CERT_\w+")
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|OPML|COOKIE|CREDENTIAL|AUTH", re.I)


def child_env() -> dict:
    return {k: v for k, v in os.environ.items() if CHILD_ENV.fullmatch(k) and not SECRET_ENV.search(k)}


def run_cmd(cmd: list[str], timeout: float) -> tuple[int | None, str, str]:
    """(returncode | None on timeout, stdout, stderr). The child gets no secrets
    (child_env); a timeout kills the whole process group so no yt-dlp
    grandchild outlives it."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True, env=child_env())
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


def ytdlp(cookies: Path | None, url: str | None, *args) -> list[str]:
    """`--` ends option parsing: a url can never be read as a flag. url=None
    for --load-info-json runs (the video comes from the info file)."""
    auth = ["--cookies", str(cookies)] if cookies else []
    return ["yt-dlp", *auth, *BASE_ARGS, *args, *(["--", url] if url else [])]


# yt-dlp's own words when the account cookies are refused. Narrow on purpose:
# its hints for age-gated or members-only videos also mention --cookies, and
# one such video must not end the run and flag healthy cookies as expired.
COOKIES_REJECTED = re.compile(r"cookies (?:are|is) no longer valid|cookies? (?:have|has) (?:likely )?been rotated"
                              r"|sign in to confirm you(?:'|’)?re not a bot", re.I)


def cookies_rejected(stderr: str) -> bool:
    """Only yt-dlp's own diagnostics: stdout carries remote metadata, and a
    video titled "cookies are no longer valid" must not end the run."""
    return bool(COOKIES_REJECTED.search(stderr or ""))


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
    """(start, end, text) -> WebVTT; overlapping/inverted cues are clamped.
    A cue with the same text as the one before it extends that cue instead of
    repeating it: Whisper's hallucination loops emit one line many times."""
    lines, prev, last = ["WEBVTT", ""], 0.0, None
    for start, end, text in segments:
        text = " ".join(str(text).split())
        if not text:
            continue
        start = max(float(start), 0.0, prev)
        end = float(end) if float(end) > start else start + 0.5
        if text == last:
            lines[-3] = lines[-3].split(" --> ")[0] + f" --> {ts(end)}"
        else:
            lines += [f"{ts(start)} --> {ts(end)}", text, ""]
        prev, last = end, text
    return "\n".join(lines) if len(lines) > 2 else ""


# ---- podcast episodes -> audio --------------------------------------------------
# Podcast episode pages -> their audio file (mp3 / m4a), for ASR.
#
# 小宇宙  www.xiaoyuzhoufm.com/episode/<id>   og:audio (.m4a) in the page
# Firstory open.firstory.fm|me/story/<id>     an audio url in the page, else the
#                                              <enclosure> of the show's RSS
#                                              (feed.firstory.me/rss/user/<uid>),
#                                              which the page links
#
# Only the episode page and the feed it names are fetched. The audio url is
# remote data: resolve_episode requires public addresses, and the audio is
# fetched through net.download, which re-checks every hop.
AUDIO_EXT = (".mp3", ".m4a", ".aac")
OG_AUDIO_KEYS = ("og:audio", "og:audio:url", "og:audio:secure_url")
_TOKEN_SPLIT = re.compile(r"""[\s"'<>\\]+""")
FIRSTORY_RSS_RE = re.compile(r"https?://feed\.firstory\.me/rss/user/[0-9a-z]{10,40}", re.I)
MINUTES_RE = re.compile(r"(\d{1,4})\s*分钟")
HMS_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2}):(\d{2})(?![\d:])")
POD_TIMEOUT = 35
POD_LANG = "zh-CN,zh-TW;q=0.9,en;q=0.8"


@dataclass
class Episode:
    audio_url: str
    duration: float = 0.0      # seconds, 0 = unknown
    lang: str | None = None    # hint only; whisper detects


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


class EpisodeUnreachable(RuntimeError):
    """The episode page (or the feed it names) could not be read this run:
    transient, the item stays pending. Distinct from a page that loads and has
    no audio, which is the episode's own nature."""


def _pod_get(url: str, raw: bool = False):
    """The page text (bytes when `raw`); empty when it is gone (404/410).
    Anything else that is not a 200 (no answer, refused, 403, 5xx...) raises
    EpisodeUnreachable."""
    r = net.get(url, POD_TIMEOUT, lang=POD_LANG, retries=2)
    if r is not None and r.status == 200:
        return r.content if raw else r.text
    if r is not None and r.status in (404, 410):
        return b"" if raw else ""
    raise EpisodeUnreachable(f"HTTP {r.status}" if r is not None else "no response")


def is_audio_url(url: str) -> bool:
    """http(s) url whose path (before any query) ends in an audio extension."""
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return url[:8].lower().startswith(("http://", "https://")) and path.endswith(AUDIO_EXT)


def _audio_in_page(page: str) -> str:
    """og:audio, else the first audio url anywhere in the page (JSON-escaped
    slashes included). Token scan, linear in the page size: a regex here
    would be quadratic on a long run of url-like text."""
    if tags := textproc.meta_tags(page, OG_AUDIO_KEYS):
        return html_mod.unescape(tags[0][1])
    for tok in _TOKEN_SPLIT.split(page.replace("\\u002F", "/").replace("\\/", "/")):
        if (i := store.ascii_lower(tok).find("http")) >= 0 and is_audio_url(tok[i:]):
            return html_mod.unescape(tok[i:])
    return ""


def _xiaoyuzhou(url: str, eid: str) -> Episode | None:
    page = _pod_get(url)
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
    page = _pod_get(url)
    duration = 0.0
    if m := HMS_RE.search(re.sub(r"<[^<>]*+>", " ", page)):      # "2024-06-04 00:43:08"
        duration = hms(m[0])
    if audio := _audio_in_page(page):
        return Episode(audio, duration, "zh")
    feed = FIRSTORY_RSS_RE.search(page)
    if not feed:
        return None
    parsed = store.parse_feed(_pod_get(feed[0], raw=True))
    for e in parsed.entries:
        where = " ".join(str(e.get(k, "")) for k in ("id", "link", "guid"))
        where += " " + " ".join(str(l.get("href", "")) for l in e.get("links", []))
        if sid.lower() not in where.lower():
            continue
        for l in list(e.get("enclosures", [])) + list(e.get("links", [])):
            href = l.get("href") or l.get("url") or ""
            if href and (str(l.get("type", "")).startswith("audio") or is_audio_url(href)):
                return Episode(href, hms(e.get("itunes_duration")) or duration, "zh")
    return None


def resolve_episode(url: str) -> Episode | None:
    """The episode's audio, or None when the episode has none we may use (no
    audio on the page, page gone, audio on a non-public address). Raises
    EpisodeUnreachable when the page could not be read."""
    m = store.EPISODE_RE.match(url or "")
    if not m:
        return None
    ep = _xiaoyuzhou(url, m[1]) if m[1] else _firstory(url, m[2])
    if ep and net.safe_url(ep.audio_url):
        return ep
    return None


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
        rc, out, err = run_cmd(ytdlp(cookies, url, *AUDIO_ARGS, "-o", f"{d}/audio.%(ext)s"), timeout)
        files = sorted(p for p in d.glob("audio.*") if p.suffix != ".part")
        if rc is None:
            raise RuntimeError(f"audio download timed out after {timeout:.0f}s")
        if rc != 0 or not files:
            raise RuntimeError(f"audio download failed: {(out + err).strip()[-1500:]}")
        return files[0]
    return fetch


def direct_audio(url: str):
    """A podcast file, through net.download: every redirect hop and every
    connection is held to public addresses, unlike a yt-dlp download."""
    def fetch(d: Path, timeout: float) -> Path:
        ext = Path(url.split("?", 1)[0]).suffix.lower()
        dest = d / ("audio" + (ext if ext in (".mp3", ".m4a", ".aac") else ".bin"))
        if not net.download(url, dest, timeout, PODCAST_MAX_BYTES):
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
            prune_subtitles(out_dir, item_id)
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
        ep = resolve_episode(url)
    except Exception as e:      # unreachable or a parser error: not the episode's fault
        n["failed"] += 1
        print(f"[PODCAST] {item_id}: could not read {url}: {type(e).__name__}: {e} (kept pending, retried next run)")
        return False
    if ep is None:              # the page loaded and has no usable audio
        item["summary"] = BLANK_SUMMARY
        n["no_audio"] += 1
        print(f"[PODCAST] {item_id}: no audio found on {url} -> blank")
        return True
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


def fetch_track(cookies, url, out_dir: Path, item_id, orig, manual, lang, info_file, timeout):
    """Write the chosen subtitle track. From the probe's info JSON first
    (--load-info-json: no second extraction); once more from the url if that
    fails for another reason than "no such track" (e.g. a stale PO token).
    Returns (rc, stdout, stderr) of the last attempt."""
    args = [*SUB_ARGS, "--skip-download", "--write-sub" if manual else "--write-auto-sub",
            "--sub-langs", lang, "--sub-format", "vtt",
            # no remote field in the filename: orig is safe_lang'd
            "-o", str(out_dir / f"{item_id}.{orig or 'und'}.%(ext)s")]
    rc, out, err = run_cmd(ytdlp(cookies, None, "--load-info-json", str(info_file), *args), timeout)
    got = rc == 0 and any(out_dir.glob(f"{item_id}*.vtt"))
    if got or cookies_rejected(err) or "no subtitles for the requested languages" in (out + err).lower():
        return rc, out, err
    discard(out_dir, item_id)
    return run_cmd(ytdlp(cookies, url, *args), timeout)


def main(argv=None, limits: Limits | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-transcribe", action="store_true", help="subtitles only, no ASR")
    ap.add_argument("--no-summarize", action="store_true", help="do not summarize the new transcripts")
    args = ap.parse_args(argv)
    cfg = store.configure()
    a = limits or Limits.from_cfg(cfg, no_transcribe=args.no_transcribe)

    archive, out_dir, cookies = cfg.archive, cfg.subtitles, cfg.cookies_path
    if cookies and not cookies.is_file():
        print(f"WARNING: {cookies} not found; YouTube without cookies")
        cookies = None
    status_file = cfg.status_file
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = load_doc(archive)
    if n := remove_orphans(out_dir, {it.get("id") for it in doc["items"]}):
        print(f"[CLEANUP] removed {n} orphaned subtitle file(s)")
    # One subtitle per id: files left by an earlier track choice go too.
    extra = [x for i in sorted({p.name.split(".", 1)[0] for p in out_dir.glob("*.vtt")})
             for x in prune_subtitles(out_dir, i)]
    if extra:
        print(f"[CLEANUP] removed {len(extra)} lower-ranked subtitle file(s)")
    have = {p.name.split(".", 1)[0] for p in out_dir.glob("*.vtt")}
    budget, n, dirty = Budget(a), Counter(), False
    cookies_expired = False

    todo = [it for it in doc["items"] if valid_id(it.get("id")) and is_pending(it)
            and (is_youtube(it["url"]) or is_episode(it["url"])) and it["id"] not in have]
    todo.sort(key=lambda it: summarize._ts(it.get("published_at")), reverse=True)   # newest first
    for item in todo[:a.max_items]:
        if budget.stop():
            print(f"[BUDGET] {budget.left() / 60:.0f} min of ASR left; rest stay pending")
            break
        url, item_id = item["url"], item["id"]
        n["processed"] += 1
        if is_episode(url):
            dirty |= podcast_asr(item, a, budget, out_dir, n)
            continue
        n["probed"] += 1                     # a YouTube probe: the only evidence about the cookies
        rc, out, err = run_cmd(ytdlp(cookies, url, *SUB_ARGS, "--skip-download", "--dump-json"), a.probe_timeout)
        if cookies_rejected(err):
            print("[EXPIRED] cookies invalid")
            cookies_expired = True
            break
        try:
            line = out.strip().splitlines()[-1] if rc == 0 else ""
            info = json.loads(line) if line else None
        except Exception:
            info = None
        if not isinstance(info, dict):
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
            continue
        manual, lang = choice
        print(f"[TRACK] {item_id}: {lang} ({'manual' if manual else 'auto'}, orig={orig}, "
              f"rank={track_rank(lang, orig, manual)})")
        # The info JSON can carry session cookies: it lives in a private (0700)
        # temp dir outside the repository, never next to the committed files.
        with tempfile.TemporaryDirectory(prefix="ytinfo-") as d:
            info_file = Path(d) / "info.json"
            info_file.write_text(line, encoding="utf-8")
            rc, out, err = fetch_track(cookies, url, out_dir, item_id, orig, manual, lang, info_file,
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
            prune_subtitles(out_dir, item_id)
        elif rc == 0 and "no subtitles for the requested languages" in log:
            dirty |= fallback()
        else:
            n["failed"] += 1
            print(f"[FAILED] {item_id} (exit {rc}): {(out + err)[-1500:]}")

    # No YouTube video was probed (podcasts use no cookies): leave the flag alone.
    if cookies_expired or n["probed"]:
        record_status(status_file, "cookies", "expired" if cookies_expired else "ok",
                      datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    print(f"[MEDIA] done {dict(n)} ({budget})")
    if args.no_summarize:
        if dirty:
            save_doc(archive, doc)
    else:                                   # one archive write for both steps
        summarize.run(archive, doc, 0, offline_only=True, do_backfill=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
