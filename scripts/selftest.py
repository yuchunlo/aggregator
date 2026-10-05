#!/usr/bin/env python3
"""Offline contract checks; CI runs this before touching any data. Each check
guards a failure that actually happened (see ARCHITECTURE.md)."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TRANSLATE", "off")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import common, lang, update_news as un, subtitle_priority as sp, thumbs, textproc, extract, download_sub as ds  # noqa: E402

FAILS: list[str] = []


def eq(got, want, label):
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")


def check_jsonio():
    doc = {"generated_at": "x", "total_items": 2, "items": [{"id": "a"}, {"id": "b", "s": "中"}]}
    out = common.dumps(doc)
    eq(json.loads(out), doc, "round-trip")
    eq(out.count("\n"), 8, "one line per item")
    eq(json.loads(common.dumps([{"a": 1}])), [{"a": 1}], "list payload")
    eq(common.dumps({"items": []}), '{\n "items": [\n ]\n}\n', "empty items")
    eq(common.dumps({"g": 1, "items": [{"a": 1}, {"b": 2}]}),
       '{\n "g": 1,\n "items": [\n  {"a":1},\n  {"b":2}\n ]\n}\n', "exact format")


def check_subtitle_priority():
    c = sp.choose_track
    eq(c({"zh-Hant": {}}, {"en": {}}, "en"), (True, "zh-Hant"), "manual zh beats auto orig")
    eq(c({"en": {}, "fr": {}}, {}, "en"), (True, "en"), "manual orig beats manual other")
    eq(c({}, {"en": {}, "zh-Hant": {}}, "en"), (False, "en"), "auto orig beats auto zh")
    eq(c({}, {"zh-Hant-en": {}, "de": {}}, "en"), (False, "de"), "chained last")
    eq(c({"live_chat": {}}, {}, None), None, "live_chat is not a track")
    eq(c({"live_chat": {}}, {"en": {}}, "en"), (False, "en"), "real track beats live_chat")
    eq(c({"zh-Hant-TW": {}}, {"en": {}}, "en"), (True, "zh-Hant-TW"), "zh-Hant-TW is a locale")
    eq(c({"zh-TW": {}}, {"zh-TW": {}}, "zh-TW"), (True, "zh-TW"), "manual over auto")
    for l in ("zh-CN", "zh-SG", "zh-Hans-CN", "zh-MO"):
        eq(sp.is_zh(l), True, f"{l} is Chinese")
    eq([sp.file_rank(l, "en") for l in ("zh-Hant", "en", "de", "zh-Hant-en")], [0, 1, 2, 3], "file ranks")
    eq(sp.track_rank("ZH_HANT", "en", True), 0, "case/underscore")


def check_vtt_and_process():
    eq(ds.ts(3661.007), "01:01:01.007", "timestamp")
    eq(ds.ts(-3), "00:00:00.000", "negative clamps")
    v = ds.to_vtt([(0.0, 5.0, "a"), (2.0, 1.0, "b"), (9.0, 9.0, "  ")])
    eq(v.startswith("WEBVTT\n\n"), True, "vtt header")
    eq("00:00:05.000 --> 00:00:05.500\nb" in v and v.count("-->") == 2, True, "cue clamping")
    eq(ds.to_vtt([]), "", "no segments")
    eq(ds.run(["sh", "-c", "echo hi; echo boo >&2"], 30), (0, "hi\n", "boo\n"), "run ok")
    marker = f"selftest-{os.getpid()}"
    t0 = time.monotonic()
    rc, _, _ = ds.run(["sh", "-c", f"sh -c 'sleep 30; : {marker}' & sleep 30"], 1)
    eq((rc, time.monotonic() - t0 < 15), (None, True), "timeout returns promptly")
    time.sleep(0.3)
    alive = [p for p in Path("/proc").glob("[0-9]*")
             if marker in (p / "cmdline").read_bytes().decode(errors="replace")] \
        if Path("/proc").is_dir() else []
    eq(alive, [], "process group killed")
    with tempfile.TemporaryDirectory() as d:
        for n in ("abc.en.zh.vtt", "abc.en.zh.vtt.part", "other.en.en.vtt"):
            (Path(d) / n).write_text("WEBVTT")
        eq(sorted(ds.discard(Path(d), "abc")), ["abc.en.zh.vtt", "abc.en.zh.vtt.part"], "discard partial")
        (Path(d) / "keep.en.en.vtt").write_text("WEBVTT")
        eq((ds.remove_orphans(Path(d), set()), ds.remove_orphans(Path(d), {"keep"})), (0, 1),
           "orphans removed; an empty archive removes nothing")
        eq(sorted(p.name for p in Path(d).iterdir()), ["keep.en.en.vtt"], "only orphans removed")
    import importlib.util, types
    real = importlib.util.find_spec
    importlib.util.find_spec = lambda name: object()
    try:
        a = types.SimpleNamespace(no_transcribe=False, max_transcribe=0, max_asr_duration=3600,
                                  max_asr_total=7200)
        b = ds.Budget(a)
        eq(b.refuse(4000, False)[1], True, "too long for ASR is permanent")
        eq(b.refuse(100, True)[1], False, "live is not permanent")
        b.spent = 7000
        eq((b.refuse(1000, False)[1], b.stop()), (False, True), "budget wall is not permanent; stops run")
    finally:
        importlib.util.find_spec = real


def check_language():
    nt = lang.needs_translation
    eq(nt("這是一篇講 Python 的文章，使用 requests 與 asyncio 以及 FastAPI framework and uvicorn"),
       False, "繁中 about code is not foreign")
    eq(nt("This is an English summary with more than eight Latin words in it."), True, "English")
    eq(nt("今日は東京で新しいカフェがオープンしました。とても人気があって、たくさんの人が並んでいます。"),
       True, "Japanese despite kanji")
    eq(nt("오늘 서울에서 새로운 카페가 문을 열었습니다 많은 사람들이 줄을 섰습니다"), True, "Korean")
    eq(nt(common.GONE_SUMMARY), False, "placeholder")
    eq(nt("這篇 about using the requests library with asyncio and FastAPI to build servers quickly"),
       False, "a little Han means Chinese with code")
    eq((common.is_pending({"url": "u"}), common.is_pending({"url": "u", "summary": " "})),
       (True, False), "blank summary is not pending")
    eq(nt(common.BLANK_SUMMARY), False, "blank")
    ja = "今日は東京で新しいカフェがオープンしました。" * 200
    chunks = lang.chunk_text(ja)
    eq(len(chunks) > 1 and all(lang._enc(c) <= lang.GTX_CHUNK_BYTES for c in chunks), True,
       "chunks bounded by encoded bytes and split without spaces")
    eq(lang.short_reason("RetryError: " + "x" * 5000 + " too many 429 error responses")[:40],
       "RetryError: too many 429", "reason on one line")


class FakeResp:
    def __init__(self, code, payload):
        self.status_code, self._p = code, payload

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, deepl_code=200, gtx_code=200):
        self.deepl_code, self.gtx_code, self.posts, self.gets = deepl_code, gtx_code, [], 0

    def post(self, url, json=None, **_):
        self.posts.append(json)
        if self.deepl_code == 413 and len(json["text"]) > 1:
            return FakeResp(413, None)
        if self.deepl_code == "short":
            return FakeResp(200, {"translations": [{"text": "譯"}]})
        return FakeResp(200 if self.deepl_code == 413 else self.deepl_code,
                        {"translations": [{"text": "譯" + t} for t in json["text"]]})

    def get(self, url, params=None, **_):
        self.gets += 1
        return FakeResp(self.gtx_code, [[["譯" + params["q"], params["q"]]]])


def check_translate():
    lang.GTX_SLEEP = 0
    saved = lang.deepl_on
    try:
        lang.deepl_on = True
        s = FakeSession()
        out = lang.translate_many(["a", "", "b"], s)
        eq(out, [("譯a", ""), (None, "empty input"), ("譯b", "")], "deepl keeps order and alignment")
        eq(s.posts[0]["target_lang"], "ZH-HANT", "zh-TW maps to ZH-HANT")
        s = FakeSession(deepl_code=413)
        eq(([o for o, _ in lang.translate_many(["a", "b", "c"], s)], s.gets), (["譯a", "譯b", "譯c"], 0),
           "413 halves the batch and stays on DeepL")
        s = FakeSession(deepl_code="short")
        eq((lang.translate_many(["a", "b"], s)[1][0], s.gets), ("譯b", 2),
           "misaligned DeepL result rejected, gtx used instead")
        s = FakeSession(deepl_code=456, gtx_code=429)
        out = lang.translate_many(list("abcdefgh"), s, stop_after=4)
        eq((lang.deepl_on, s.gets, out[-1][1].startswith("skipped")), (False, 4, True),
           "quota disables DeepL; gtx stops at the Nth failure")
    finally:
        lang.deepl_on = saved


def check_thumbs():
    eq([u for u in thumbs.DENY if u != u.lower()], [], "deny entries are lower-case")
    ok = lambda u: thumbs.usable(u)[0]
    eq(ok("https://ritholtz.com/wp-content/uploads/2025/05/MIB_2025.png?w=300"), False, "deny ignores case/query")
    eq(ok("https://blogger.googleusercontent.com/img/a/AVvXsEabc"), True, "trusted host")
    eq(ok("https://example.com/img/us-inflation-chart-1024x576.png"), True, "chart")
    eq(ok("https://example.com/img/us-inflation-rate-chart-since-1970.png"), True,
       "chart vocabulary beats the descriptive-phrase rule")
    eq(ok("https://img.youtube.com/vi/x/mqdefault.jpg"), True, "mqdefault is not 'default'")
    for bad in ("og-image.png", "logo.png", "line@2x.png", "man-standing-near-building.jpg",
                "shutterstock_123.jpg", "spinner.gif"):
        eq(ok("https://example.com/img/" + bad), False, f"reject {bad}")
    for u in ("https://www.douban.com/note/1/", "https://book.douban.com/subject/2/",
              "https://douban.com/people/x/status/3"):
        eq(thumbs.extract('<meta property="og:image" content="https://img1.doubanio.com/view/note/l/public/p1.jpg">', u),
           None, f"douban never yields a thumbnail ({u})")
        eq(thumbs.still_valid({"url": u, "thumbnail": "https://example.com/img/us-inflation-chart.png"})[0],
           False, "backfill drops existing douban thumbnails")
    eq(thumbs.extract('<meta property="og:image" content="https://example.com/img/us-inflation-chart.png">',
                      "https://notdouban.com/a"), "https://example.com/img/us-inflation-chart.png",
       "hosts merely ending in 'douban.com' are not skipped")


def check_text():
    html = ("<meta content='width=1100' name='viewport'/>"
            "<meta content='the real one' name='description'/>")
    eq(extract.meta_description(html), "the real one", "meta attrs never cross tags")
    eq(extract.meta_description('<meta name="description" content="plain">'
                                '<meta property="og:description" content="og">'), "og",
       "og:description preferred")
    eq(extract.meta_description('<meta name="description" content="first">'
                                '<meta name="description" content="later">'), "first",
       "first tag of a key wins")
    eq(extract.is_junk("豆瓣 载入中"), True, "junk in simplified")
    text = "第一句話在這裡。第二句話也在這裡。第三句。"
    eq(textproc.trim_to_sentences(text, 12), "第一句話在這裡。", "whole sentences only")
    eq(textproc.trim_to_sentences(text, 3), "第一句話在這裡。", "first sentence kept whole")
    vtt = ("WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nhello everyone and welcome\n\n"
           "2\n00:00:02.000 --> 00:00:04.000\nhello everyone and welcome\nto the gadgets that I use\n")
    with tempfile.NamedTemporaryFile("w", suffix=".vtt", delete=False) as f:
        f.write(vtt)
    eq(textproc.vtt_to_text(f.name), "hello everyone and welcome\nto the gadgets that I use",
       "rolling cues stitched, cue numbers dropped")
    os.unlink(f.name)
    eq(textproc.stitch([["my favorite low-tech"], ["Previously, you guys liked"]]),
       ["my favorite low-tech", "Previously, you guys liked"], "wrapped cues kept whole")
    tag = "Only those that risk going too far, can possibly know how far he can go."
    for raw in (tag + "2026年9月21日", f"{tag} 2026年9月21日", f"{tag}\n2026年9月21日", "2026年9月21日"):
        eq(textproc.build(raw, "feed"), "", f"blog tagline/date alone leaves nothing: {raw[:30]!r}")
    real = "2026年9月21日，美股收盤大跌，道瓊指數下跌三百點，市場擔心利率持續偏高。"
    eq(textproc.build(f"{real}{tag}2026年9月21日", "feed"), real, "real content survives, tagline and date go")
    eq(textproc.restrip(f"{real}{tag} ↛"), f"{real} ↛", "backfill re-strip keeps marks")
    eq(textproc.restrip(f"{tag} ↛"), common.BLANK_SUMMARY, "backfill re-strip: nothing left -> blank")
    eq(textproc.restrip(real), real, "backfill re-strip leaves clean summaries alone")
    eq(lang.to_twp("软件开发"), "軟體開發", "s2twp")
    eq(textproc.merge_caption_lines("新しいカフェが\nオープンしたので\n行ってみたいと思います\n"
                                    "でも人がすごく多くて\n並ぶのに一時間かかりました", "ja"),
       "新しいカフェがオープンしたので、行ってみたいと思います。\n"
       "でも人がすごく多くて、並ぶのに一時間かかりました。", "Japanese caption regrouping")


def check_encoding():
    """2026-10: a Chinese page with no charset header was guessed as cp1256
    from a truncated prefix; the mojibake was then 'translated' into nonsense
    and stored as its summary."""
    zh = "考古學家發現一座擁有兩百年歷史的古墓。" * 4
    page = ("<script>" + "x" * 250_000 + "</script><p>" + zh + "</p>").encode()
    eq(zh in common.decode_body(page, {}), True, "utf-8 past a long ascii prefix")
    eq(zh in common.decode_body(page, {"content-type": "text/html; charset=windows-1256"}), True,
       "valid utf-8 beats a wrong label")
    eq(common.decode_body(zh.encode("big5"), {"Content-Type": "text/html; charset=ISO-8859-1"}), zh,
       "big5 behind a default latin-1 header")
    fr = "café déjà vu, très bien. " * 10
    eq(common.decode_body(fr.encode("cp1252"), {"Content-Type": "text/html; charset=iso-8859-1"}), fr,
       "real latin-1 page keeps its label")
    for enc in ("cp1252", "cp1256"):
        bad = "".join(bytes([b]).decode(enc, "ignore") or chr(b) for b in zh.encode())
        eq(lang.fix_mojibake("前言 " + bad + " café"), "前言 " + zh + " café", f"repair {enc} runs in place")
        eq(lang.garbled(bad), True, f"detect {enc} mojibake")
    for ok in (zh, fr, "مرحبا بالعالم هذا نص عربي حقيقي", "哈哈哈哈哈哈，Straße Ñandú"):
        eq((lang.garbled(ok), lang.fix_mojibake(ok)), (False, ok), f"clean text untouched: {ok[:8]}")
    eq(lang.garbled("前言。" + "考查，" * 30), True, "degenerate translation loop")
    eq(lang._accept("考查，" * 30)[0], None, "garbled translation rejected")
    eq(textproc.build("é¦é¦é¦é¦çڑ„ه°±و˜¯" * 30), None, "mojibake never becomes a summary")
    import summarize_feed as sf
    items = [{"url": "https://x.example/a", "summary": "çڑ„ه°±و˜¯é¦é¦é¦é¦" * 5},
             {"url": "https://x.example/b", "summary": "正常的中文摘要。"}]
    sf.backfill(items, translate=False)
    eq(["summary" in it for it in items], [False, True], "backfill re-queues stored mojibake")


def check_inbox():
    """data/inbox.json from the reader: only safe recent urls; a known url
    keeps its title (the id hashes it) and is not fetched again."""
    now = datetime.now(timezone.utc)
    d = Path(tempfile.mkdtemp())
    add = lambda u, days=0: {"url": u, "added": (now - timedelta(days=days)).isoformat()}
    (d / "inbox.json").write_text(json.dumps({"urls": [
        add("https://example.org/p"), add("https://example.org/p"), add("javascript:alert(1)"),
        add("https://u:p@x.org/"), add("https://old.org/", 99), add("https://plain.org/a/")]}))

    class R:
        status, text = 200, "<head><meta property='og:title' content='Hello 世界'><title>x</title></head>"
    real = un.common.get
    try:
        un.common.get = lambda url, *a, **k: R() if "example" in url else None
        archive = {}
        raws = un.fetch_inbox(archive, d / "inbox.json", 60)
        eq([(r.title, r.url) for r in raws], [("Hello 世界", "https://example.org/p"), ("plain.org/a", "https://plain.org/a/")],
           "inbox: filtered, deduped, titled")
        un.ingest(archive, raws, now)
        ids = set(archive)
        un.common.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("refetched"))
        un.ingest(archive, un.fetch_inbox(archive, d / "inbox.json", 60), now)
        eq(set(archive), ids, "inbox: known url keeps its id, no refetch")
        (d / "inbox.json").write_text("[]")
        eq(un.fetch_inbox(archive, d / "inbox.json", 60), [], "inbox: malformed file ignored")
    finally:
        un.common.get = real


def check_offline_only():
    """download_sub's run summarizes fresh subtitles without fetching pages."""
    import summarize_feed as sf
    d = Path(tempfile.mkdtemp())
    yid = "a" * 40
    (d / f"{yid}.zh-TW.zh-TW.vtt").write_text("WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n"
        + "這是一段關於考古發現的影片內容，研究人員在烏克蘭南部發掘古墓。" * 20 + "\n", encoding="utf-8")
    f = d / "a.json"
    f.write_text(json.dumps({"items": [
        {"id": yid, "url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "v", "source": "y", "category": "x"},
        {"id": "b" * 40, "url": "https://example.org/x", "title": "p", "source": "s", "category": "x", "feed_content": "keep"}]}))
    real_dir, real_fetch = sf.SUBTITLES_DIR, extract.fetch
    sf.SUBTITLES_DIR = d
    extract.fetch = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            sf.main(["--items-file", str(f), "--offline-only", "--no-translate"])
    finally:
        sf.SUBTITLES_DIR, extract.fetch = real_dir, real_fetch
    out = json.loads(f.read_text())["items"]
    eq((bool(out[0].get("summary")), "summary" in out[1], out[1].get("feed_content")), (True, False, None),
       "offline-only: subtitle summarized, page not fetched, feed copy dropped")


def check_security():
    for u in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:4416/", "http://localhost/",
              "http://10.1.2.3/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
              "file:///etc/passwd", "ftp://example.com/", "https://user:pw@1.1.1.1/", "javascript:alert(1)"):
        eq(common.safe_url(u), False, f"refuse {u}")
        eq(common.get(u, 1), None, f"never fetched: {u}")
    eq(common.safe_url("https://1.1.1.1/"), True, "public address allowed")
    import socket
    real, answers, dialed = socket.getaddrinfo, iter(["1.1.1.1", "127.0.0.1"]), []

    def rebinding(host, *a, **k):          # passes the check once, then answers loopback
        return real(next(answers) if host == "rebind.test" else host, *a, **k)
    plain, socket.getaddrinfo = common._plain_connect, rebinding
    common._plain_connect = lambda addr, *a, **k: dialed.append(addr[0]) or (_ for _ in ()).throw(OSError())
    tok = common._GUARD.set(True)
    try:
        common._guarded_connect(("rebind.test", 80), 1)
    except OSError:
        pass
    finally:
        common._GUARD.reset(tok)
        socket.getaddrinfo, common._plain_connect = real, plain
    eq(dialed, ["1.1.1.1"], "DNS rebinding: connect only to the address that was checked")
    eq([common.valid_id(x) for x in ("a" * 40, "../" + "a" * 37, "A" * 40, None)],
       [True, False, False, False], "item ids are sha1 hex only")
    eq([common.safe_lang(x) for x in ("zh-Hant", "en", "../../x", "", ".*", None)],
       ["zh-Hant", "en", "und", "und", "und", "und"], "language codes fit for filenames")
    cmd = ds.ytdlp(Path("c"), "--exec=rm -rf ~", "--skip-download")
    eq(cmd[-2:], ["--", "--exec=rm -rf ~"], "url passed after --")
    arch = {}
    un.ingest(arch, [un.Raw("c", "s", "t", u, None) for u in ("javascript:alert(1)", "httpx://a", "https://ok.example/a")],
              un.parse_date("2026-01-01"))
    eq([r["url"] for r in arch.values()], ["https://ok.example/a"], "only http(s) item urls stored")
    eq(thumbs.extract('<meta property="og:image" content="javascript:alert(1)//x.png">', "https://a.example/"),
       None, "non-http thumbnail refused")
    eq(un.html_to_text("<p>a<code>x</code>b</p><pre>c</pre><code>1\n2</code><script>d</script>e"), "axb\ne",
       "feed html to text")


def check_opml_secret():
    import gzip, lzma
    opml = b'<opml version="2.0"><body><outline title="A" xmlUrl="https://a.example/rss"/></body></opml>'
    for enc in (gzip.compress, lzma.compress, lambda b: b):
        blob = base64.b64encode(enc(opml)).decode()
        eq(un.opml_from_env({"FOLLOW_OPML_B64": blob[:10], "FOLLOW_OPML_B64_2": blob[10:]}), opml,
           "split secret joins and decodes")
    eq(un.opml_from_env({}), b"", "no secret, nothing unpacked")
    eq([f["url"] for f in un.read_opml(opml, 0)], ["https://a.example/rss"], "OPML read from bytes")
    with tempfile.TemporaryDirectory() as d:
        old = {n: os.environ.pop(n, None) for n in un.OPML_SECRETS}
        try:
            try:
                un.main(["--output-dir", d, "--require-opml"])
                eq("no exit", "exit", "--require-opml without OPML fails")
            except SystemExit as e:
                eq(bool(e.code), True, "--require-opml without OPML fails")
            eq((Path(d) / "archive.json").exists(), False, "nothing written when OPML is required and missing")
        finally:
            os.environ.update({k: v for k, v in old.items() if v is not None})


def check_merge_and_id():
    keep, other = {"id": "a", "last_seen_at": "2026-01-01"}, {"id": "b", "last_seen_at": "2026-02-01", "x": 1}
    un.absorb(keep, other)
    eq(keep, {"id": "a", "last_seen_at": "2026-02-01", "x": 1}, "absorb")
    keep = {"x": 1, "summary": "long fallback text ↛"}
    un.absorb(keep, {"x": 2, "summary": "real"})
    eq(keep, {"x": 1, "summary": "real"}, "known values kept; real summary beats ↛")
    # id formula must never change: ids name subtitle files
    eq(un.make_id("tech", "Example Blog", "Example post title", "https://example.com/posts/1"),
       "07e86ab84e0206952c6d93c4c0e6e27eb02abc95", "id formula unchanged (frozen value)")
    eq(un.make_id("Tech", "X", "T", "https://a.com"), un.make_id("tech", "x", "t", "https://a.com"),
       "id is case-insensitive")
    eq(un.canonical_url("https://vocus.cc/@who/abc123?utm_source=x"), "https://vocus.cc/article/abc123",
       "vocus canonical + tracking stripped")
    now = un.parse_date("2026-09-29")
    arch = {}
    raw = un.Raw("tech", "S", "T", "https://a.com/p", un.parse_date("2026-09-01"), content="body")
    un.ingest(arch, [raw], now)
    rec = next(iter(arch.values()))
    eq(list(rec), ["id", "category", "source", "title", "url", "published_at", "last_seen_at",
                   "feed_content"], "new record fields (no site_name / first_seen_at)")
    import dataclasses
    un.ingest(arch, [dataclasses.replace(raw, published_at=un.parse_date("2026-09-02"))], now)
    eq(rec["published_at"], "2026-09-02T00:00:00Z", "feed date overwrites any category")
    with tempfile.TemporaryDirectory() as d:
        now = datetime.now(timezone.utc)
        ago = lambda days: un.iso(now - timedelta(days=days))
        common.save_doc(Path(d) / "archive.json", {"items": [
            {"id": "a" * 40, "title": "old", "url": "https://x.example/1", "last_seen_at": ago(61)},
            {"id": "b" * 40, "title": "new", "url": "https://x.example/2", "last_seen_at": ago(59)},
            {"id": "c" * 40, "title": "legacy", "url": "https://x.example/3", "published_at": ago(10)}]})
        # main() also scrapes BestBlogs: on a runner with network that adds live
        # issues to the archive. The test is about retention only, so stay offline.
        real_bb, un.fetch_bestblogs = un.fetch_bestblogs, lambda archive: []
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                un.main(["--output-dir", d])
            kept = lambda: sorted(r["title"] for r in common.load_doc(Path(d) / "archive.json")["items"])
            eq(kept(), ["legacy", "new"], "default retention: 60 days of last_seen_at")
            with contextlib.redirect_stdout(io.StringIO()):
                un.main(["--output-dir", d, "--archive-days", "5"])
            eq(kept(), [], "retention days configurable")
        finally:
            un.fetch_bestblogs = real_bb


if __name__ == "__main__":
    # Checks run the real scripts, which narrate as they go ("no OPML",
    # "Retention: dropped ..."): that is test scaffolding, not the real run,
    # so it is swallowed; only failures are printed.
    for n in un.OPML_SECRETS:                 # never fetch real feeds from a test
        os.environ.pop(n, None)
    for name, fn in list(globals().items()):
        if name.startswith("check_"):
            with contextlib.redirect_stdout(io.StringIO()):
                fn()
    for f in FAILS:
        print("FAIL:", f)
    print("selftest:", "FAILED" if FAILS else "ok")
    sys.exit(1 if FAILS else 0)
