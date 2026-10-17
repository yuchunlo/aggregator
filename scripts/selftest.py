#!/usr/bin/env python3
"""Offline contract checks; CI runs this before touching any data. Each check
pins one behaviour that keeps a known failure out (see ARCHITECTURE.md)."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import traceback
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TRANSLATE", "off")
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Offline: never run the scraped sources from a test, whatever the workflow sets.
os.environ["SCRAPED_SOURCES"] = "off"
import net, store, textproc, summarize, collect as un, media as ds  # noqa: E402

FAILS: list[str] = []


def eq(got, want, label):
    if got != want:
        FAILS.append(f"{label}: got {repr(got)[:300]}, want {repr(want)[:300]}")


@contextlib.contextmanager
def env(**kw):
    """Set environment variables for a block (None removes one)."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, str(v))
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def collect(d, *args, opml=b'<opml><body/></opml>', **kw):
    """collect.main on data dir `d` with an OPML file (default: no feeds)."""
    (Path(d) / "f.opml").write_bytes(opml)
    with env(DATA_DIR=d, **kw), contextlib.redirect_stdout(io.StringIO()):
        return un.main(["--rss-opml", str(Path(d) / "f.opml"), *args])


def subs(archive, d, *args):
    """media.main on d/archive.json (no cookies file, subtitles under d/subtitles)."""
    assert Path(archive) == Path(d) / "archive.json"
    with env(DATA_DIR=d, COOKIES_PATH=Path(d) / "c.txt"), \
            contextlib.redirect_stdout(io.StringIO()):
        return ds.main(list(args))


class Net:
    """Stands in for net.request: canned replies by url substring."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, timeout, **kw):
        self.calls.append((method, url, kw))
        for key, reply in self.routes.items():
            if key in url:
                return reply(method, url, kw) if callable(reply) else reply
        return None


def check_jsonio():
    doc = {"generated_at": "x", "total_items": 2, "items": [{"id": "a"}, {"id": "b", "s": "中"}]}
    out = store.dumps(doc)
    eq(json.loads(out), doc, "round-trip")
    eq(out.count("\n"), 8, "one line per item")
    eq(json.loads(store.dumps([{"a": 1}])), [{"a": 1}], "list payload")
    eq(store.dumps({"items": []}), '{\n "items": [\n ]\n}\n', "empty items")
    eq(store.dumps({"g": 1, "items": [{"a": 1}, {"b": 2}]}),
       '{\n "g": 1,\n "items": [\n  {"a":1},\n  {"b":2}\n ]\n}\n', "exact format")


def check_subtitle_priority():
    sp = store                      # the subtitle-file contract lives in store
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
    eq(ds.run_cmd(["sh", "-c", "echo hi; echo boo >&2"], 30), (0, "hi\n", "boo\n"), "run ok")
    marker = f"selftest-{os.getpid()}"
    t0 = time.monotonic()
    rc, _, _ = ds.run_cmd(["sh", "-c", f"sh -c 'sleep 30; : {marker}' & sleep 30"], 1)
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
    nt = textproc.needs_translation
    eq(nt("這是一篇講 Python 的文章，使用 requests 與 asyncio 以及 FastAPI framework and uvicorn"),
       False, "繁中 about code is not foreign")
    eq(nt("This is an English summary with more than eight Latin words in it."), True, "English")
    eq(nt("今日は東京で新しいカフェがオープンしました。とても人気があって、たくさんの人が並んでいます。"),
       True, "Japanese despite kanji")
    eq(nt("오늘 서울에서 새로운 카페가 문을 열었습니다 많은 사람들이 줄을 섰습니다"), True, "Korean")
    eq(nt(store.GONE_SUMMARY), False, "placeholder")
    eq(nt("這篇 about using the requests library with asyncio and FastAPI to build servers quickly"),
       False, "a little Han means Chinese with code")
    eq((store.is_pending({"url": "u"}), store.is_pending({"url": "u", "summary": " "})),
       (True, False), "blank summary is not pending")
    eq(nt(store.BLANK_SUMMARY), False, "blank")
    ja = "今日は東京で新しいカフェがオープンしました。" * 200
    chunks = textproc.chunk_text(ja)
    eq(len(chunks) > 1 and all(textproc._enc(c) <= textproc.GTX_CHUNK_BYTES for c in chunks), True,
       "chunks bounded by encoded bytes and split without spaces")
    eq(textproc.short_reason("RetryError: " + "x" * 5000 + " too many 429 error responses")[:40],
       "RetryError: too many 429", "reason on one line")


def check_translate():
    textproc.GTX_SLEEP = 0
    saved, real = textproc.deepl_disabled, net.request
    store.CFG.deepl_key = "test-key"
    ok = lambda payload: net.Resp(200, json.dumps(payload).encode(), {}, "u")

    def deepl(code):
        def reply(method, url, kw):
            texts = kw["json_body"]["text"]
            if code == 413 and len(texts) > 1:
                return net.Resp(413, b"", {}, url)
            if code == "short":
                return ok({"translations": [{"text": "譯"}]})
            if code not in (200, 413):
                return net.Resp(code, b"", {}, url)
            return ok({"translations": [{"text": "譯" + t} for t in texts]})
        return reply

    def gtx(code):
        return lambda m, url, kw: (net.Resp(code, b"", {}, url) if code != 200 else
                                   ok([[["譯" + kw["params"]["q"], kw["params"]["q"]]]]))
    try:
        textproc.deepl_disabled = False
        net.request = n = Net({"deepl": deepl(200), "translate_a": gtx(200)})
        out = textproc.translate_many(["a", "", "b"])
        eq(out, [("譯a", ""), (None, "empty input"), ("譯b", "")], "deepl keeps order and alignment")
        eq(n.calls[0][2]["json_body"]["target_lang"], "ZH-HANT", "zh-TW maps to ZH-HANT")
        net.request = n = Net({"deepl": deepl(413), "translate_a": gtx(200)})
        eq(([o for o, _ in textproc.translate_many(["a", "b", "c"])], sum("translate_a" in c[1] for c in n.calls)),
           (["譯a", "譯b", "譯c"], 0), "413 halves the batch and stays on DeepL")
        net.request = n = Net({"deepl": deepl("short"), "translate_a": gtx(200)})
        eq((textproc.translate_many(["a", "b"])[1][0], sum("translate_a" in c[1] for c in n.calls)), ("譯b", 2),
           "misaligned DeepL result rejected, gtx used instead")
        net.request = n = Net({"deepl": deepl(456), "translate_a": gtx(429)})
        out = textproc.translate_many(list("abcdefgh"), stop_after=4)
        eq((textproc.deepl_active(), sum("translate_a" in c[1] for c in n.calls), out[-1][1].startswith("skipped")),
           (False, 4, True), "quota disables DeepL; gtx stops at the Nth failure")
        net.request = Net({})
        eq(textproc.translate_many(["a"])[0][0], None, "no response is a failure, not an exception")
    finally:
        textproc.deepl_disabled, net.request = saved, real


def check_translate_partial_and_refusals():
    """A DeepL half that succeeded keeps its (billed) results; only transport
    refusals count toward gtx's stop_after, not results that weren't Chinese."""
    textproc.GTX_SLEEP = 0
    saved, real = textproc.deepl_disabled, net.request
    ok = lambda payload: net.Resp(200, json.dumps(payload).encode(), {}, "u")
    try:
        store.CFG.deepl_key = "test-key"
        textproc.deepl_disabled = False

        def deepl(m, url, kw):
            texts = kw["json_body"]["text"]
            if len(texts) > 1:
                return net.Resp(413, b"", {}, url)
            return ok({"translations": [{"text": "譯" + texts[0]}]}) if texts[0] == "a" else net.Resp(500, b"", {}, url)
        net.request = n = Net({"deepl": deepl, "translate_a": lambda m, url, kw: ok([[["譯" + kw["params"]["q"], ""]]])})
        out = textproc.translate_many(["a", "b"])
        eq(([o for o, _ in out], sum("translate_a" in c[1] for c in n.calls)), (["譯a", "譯b"], 1),
           "deepl 413: the half that translated is kept, only the failed half goes to gtx")
        store.CFG.deepl_key = ""
        net.request = n = Net({"translate_a": lambda m, url, kw: ok([[["still english words here " * 3, ""]]])})
        out = textproc.translate_many(list("abcdef"), stop_after=2)
        eq((sum("translate_a" in c[1] for c in n.calls), any(w == textproc.SKIPPED for _, w in out)), (6, False),
           "gtx answering with non-Chinese is not a refusal: nothing skipped")
    finally:
        textproc.deepl_disabled, net.request = saved, real


def check_deepl_fatal_by_status():
    """Only 401/403/456 switch DeepL off; a transient failure whose message
    happens to contain those digits does not."""
    saved, real = textproc.deepl_disabled, net.request
    try:
        store.CFG.deepl_key = "test-key"
        for status, fatal in ((403, True), (456, True), (500, False)):
            net.request = lambda *a, **k: net.Resp(status, b"", {}, "u")
            _, err = textproc._deepl(["a"])
            eq(err.kind, "fatal" if fatal else "transient", f"deepl HTTP {status} kind")
        net.request = lambda *a, **k: net.Resp(200, json.dumps({"translations": [{"text": "x"}] * 401}).encode(), {}, "u")
        _, err = textproc._deepl(["a"])
        eq((err.kind, "401" in str(err)), ("transient", True), "deepl: '401' in a message is not an auth error")
    finally:
        store.CFG.deepl_key = ""
        textproc.deepl_disabled, net.request = saved, real
    eq((isinstance(textproc.SKIPPED, textproc.Skipped), isinstance(str(textproc.SKIPPED), textproc.Skipped)),
       (True, False), "SKIPPED is told apart by type, not text")


def check_strip_code_edges():
    """Sliced, never re-serialised; a <code> holding a block goes whole; an
    unclosed block runs to the end, as a browser would render it."""
    got, cut = textproc.strip_code('<p a="x&amp;y">t <code>k</code></p><code><pre>long\nblock</pre></code><!--c-->z')
    eq((got, cut), ('<p a="x&amp;y">t k</p><!--c-->z', True), "strip_code: block inside code")
    eq(textproc.strip_code("<p>a</p><pre>x\ny"), ("<p>a</p>", True), "strip_code: unclosed pre")


def check_offsets_survive_unicode_case():
    """Offsets found in a lower-cased copy must index the original. The copy
    lowers A-Z only: 'İ'.lower() is two code points and would shift every cut
    after it."""
    eq(textproc.sanitize_markup("İstanbul <script>x</script> keep"), "İstanbul keep", "cut_spans: İ before a script")
    eq(textproc.sanitize_markup("İİ<!-- c -->tail"), "İİtail", "cut_spans: İ before a comment")
    eq(store.tag_text("<p>İİİ</p><title>T</title>", "title", {}), "T", "tag_text: İ before the tag")
    eq(store.ascii_lower("İA"), "İa", "ascii_lower keeps length")


def check_paragraph_scopes():
    body = "".join(f"<p>正文第{i}段的內容寫得夠長足以成為一個段落</p>" for i in range(3))
    got = textproc.paragraph_text(f"<main>{body}</main><aside><article><div>card</div></article></aside>")
    eq(got.count("\n"), 2, "paragraph_text: an <article> inside skipped chrome does not scope the page")
    got = textproc.paragraph_text(f"<article><div>no paragraphs</div></article><main>{body}</main>")
    eq(got.count("\n"), 2, "paragraph_text: an empty <article> falls back to <main>")


def check_boilerplate_rules_compile_alone():
    rules = textproc._compile_rules({"min_keep_after_cut": 10, "rules": [
        {"action": "remove_inline", "pattern": "(x)y"},
        {"action": "remove_inline", "pattern": r"(a)\1"},
        {"action": "remove_inline", "pattern": "(?i)zz"},
        {"action": "cut_to_end", "pattern": "END"},
    ]})
    saved = textproc._rules
    textproc._rules = rules
    try:
        eq(textproc.strip_boilerplate("xy ab aa ZZ keep", markup=False), "ab   keep",
           "boilerplate: backreference and inline flag work per rule")
        eq(textproc.strip_boilerplate("END title, then the body text END footer", markup=False),
           "END title, then the body text", "boilerplate: an early marker does not hide the real one")
    finally:
        textproc._rules = saved


def check_redirect_drops_credentials():
    seen, real = [], (net._once, net.public_addrs)
    hops = {"https://api.example/a": "https://other.example/b", "https://other.example/b": None}

    def once(meth, url, ips, deadline, sink, limit, *, headers, impersonate, data):
        seen.append((url, dict(headers)))
        return (302, {}, hops[url]) if hops[url] else (200, {}, None)
    try:
        net._once, net.public_addrs = once, lambda *a: ["93.184.216.34"]
        net._fetch("GET", "https://api.example/a", 5, lambda c: None, 100,
                   headers={"Authorization": "secret", "Cookie": "c", "User-Agent": "u"})
    finally:
        net._once, net.public_addrs = real
    eq([sorted(h) for _, h in seen], [["Authorization", "Cookie", "User-Agent"], ["User-Agent"]],
       "redirect to another origin drops credentials")


def check_thumbs():
    eq([u for u in summarize.deny() if u != u.lower()], [], "deny entries are lower-case")
    ok = lambda u: summarize.thumb_usable(u)[0]
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
        eq(summarize.thumb_from_page('<meta property="og:image" content="https://img1.doubanio.com/view/note/l/public/p1.jpg">', u),
           None, f"douban never yields a thumbnail ({u})")
        eq(summarize.thumb_still_valid({"url": u, "thumbnail": "https://example.com/img/us-inflation-chart.png"})[0],
           False, "backfill drops existing douban thumbnails")
    eq(summarize.thumb_from_page('<meta property="og:image" content="https://example.com/img/us-inflation-chart.png">',
                      "https://notdouban.com/a"), "https://example.com/img/us-inflation-chart.png",
       "hosts merely ending in 'douban.com' are not skipped")


def check_text():
    html = ("<meta content='width=1100' name='viewport'/>"
            "<meta content='the real one' name='description'/>")
    eq(textproc.meta_description(html), "the real one", "meta attrs never cross tags")
    eq(textproc.meta_description('<meta name="description" content="plain">'
                                '<meta property="og:description" content="og">'), "og",
       "og:description preferred")
    eq(textproc.meta_description('<meta name="description" content="first">'
                                '<meta name="description" content="later">'), "first",
       "first tag of a key wins")
    eq(summarize.is_junk("豆瓣 载入中"), True, "junk in simplified")
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
    eq(textproc.restrip(f"{tag} ↛"), store.BLANK_SUMMARY, "backfill re-strip: nothing left -> blank")
    eq(textproc.restrip(real), real, "backfill re-strip leaves clean summaries alone")
    eq(textproc.to_twp("软件开发"), "軟體開發", "s2twp")
    eq(textproc.merge_caption_lines("新しいカフェが\nオープンしたので\n行ってみたいと思います\n"
                                    "でも人がすごく多くて\n並ぶのに一時間かかりました", "ja"),
       "新しいカフェがオープンしたので、行ってみたいと思います。\n"
       "でも人がすごく多くて、並ぶのに一時間かかりました。", "Japanese caption regrouping")


def check_encoding():
    """A Chinese page with no charset header and a long ASCII prefix must
    decode as Chinese: guessed from the prefix alone it reads as cp1256, and
    that mojibake would be 'translated' into nonsense and stored as its
    summary."""
    zh = "考古學家發現一座擁有兩百年歷史的古墓。" * 4
    page = ("<script>" + "x" * 250_000 + "</script><p>" + zh + "</p>").encode()
    eq(zh in net.decode_body(page, {}), True, "utf-8 past a long ascii prefix")
    eq(zh in net.decode_body(page, {"content-type": "text/html; charset=windows-1256"}), True,
       "valid utf-8 beats a wrong label")
    eq(net.decode_body(zh.encode("big5"), {"Content-Type": "text/html; charset=ISO-8859-1"}), zh,
       "big5 behind a default latin-1 header")
    fr = "café déjà vu, très bien. " * 10
    eq(net.decode_body(fr.encode("cp1252"), {"Content-Type": "text/html; charset=iso-8859-1"}), fr,
       "real latin-1 page keeps its label")
    for enc in ("cp1252", "cp1256"):
        bad = "".join(bytes([b]).decode(enc, "ignore") or chr(b) for b in zh.encode())
        eq(textproc.fix_mojibake("前言 " + bad + " café"), "前言 " + zh + " café", f"repair {enc} runs in place")
        eq(textproc.garbled(bad), True, f"detect {enc} mojibake")
    for ok in (zh, fr, "مرحبا بالعالم هذا نص عربي حقيقي", "哈哈哈哈哈哈，Straße Ñandú"):
        eq((textproc.garbled(ok), textproc.fix_mojibake(ok)), (False, ok), f"clean text untouched: {ok[:8]}")
    eq(textproc.garbled("前言。" + "考查，" * 30), True, "degenerate translation loop")
    eq(textproc._accept("考查，" * 30)[0], None, "garbled translation rejected")
    eq(textproc.build("é¦é¦é¦é¦çڑ„ه°±و˜¯" * 30), None, "mojibake never becomes a summary")
    items = [{"url": "https://x.example/a", "summary": "çڑ„ه°±و˜¯é¦é¦é¦é¦" * 5},
             {"url": "https://x.example/b", "summary": "正常的中文摘要。"}]
    summarize.backfill(items, translate=False)
    eq(["summary" in it for it in items], [False, True], "backfill re-queues stored mojibake")


def check_summary_qc():
    """Stored-summary QC: each category gets its fix, and the known false
    positives (HTML in a tutorial, news about gambling, real Russian) pass."""
    zh = "研究人員在報告中指出，這項技術可以大幅降低資料中心的耗電量，並預計明年開始量產。"
    act = lambda s, title="t", url="https://x.example/a": (
        (f := summarize.qc_inspect({"summary": s, "title": title, "url": url})) and (f.category, f.action))
    eq(act(zh * 3), None, "clean summary")
    eq(act("嘿嘿嘿，" + zh), None, "three 嘿 is speech, not a loop")
    eq(act(zh + "小" * 20 + zh), ("loop", "repair"), "char loop collapsed")
    eq(act(zh + "在那裡，它是，" * 8 + zh), ("loop", "repair"), "clause cycle collapsed")
    eq(act("前言。" + "考查，" * 30), ("loop", "refetch"), "loop-only summary rebuilt")
    f = summarize.qc_inspect({"summary": zh + "小" * 20 + zh, "title": "t", "url": "u"})
    eq("小小" in f.fixed, False, "collapse leaves no run")
    bad = "".join(bytes([b]).decode("cp1252", "ignore") or chr(b) for b in (zh * 2).encode())
    eq(act(bad), ("garbled", "repair"), "latin-1 mojibake repaired in place")
    eq(act("前言" + "\ufffd" * 10 + zh), ("garbled", "refetch"), "replacement chars")
    cyr = (zh[:10] + "ђѓѕјљњћџ") * 3
    eq(act(cyr), ("garbled", "refetch"), "cp1251 mojibake")
    eq(act("Президент заявил, что правительство рассмотрит новый закон о бюджете в следующем месяце."),
       None, "real Russian is not mojibake")
    eq(act("Sign up Sign in Open in app Write"), ("scrape", "refetch"), "Medium chrome")
    eq(act("Subscribe Sign in Claim my free post"), ("scrape", "refetch"), "Substack paywall")
    eq(act("Please enable JavaScript to view this page."), ("scrape", "refetch"), "JS wall")
    tut = "這篇教學示範如何用 HTML 建立表單：<form><input type=\"text\"><button>送出</button></form>，再用 CSS 調整 <div class=\"row\"> 的排版。" + zh
    eq(act(tut), None, "HTML sample in a tutorial is content")
    eq(act(zh + "<div class=\"post\"><span>a</span><img src=x><p>b</p><br>" + zh * 2),
       ("scrape", "repair"), "tag debris stripped")
    eq(act("澳門博彩收入上月成長，娛樂城業者看好下半年。" + zh), None, "gambling news is not spam")
    eq(act("KEBAYA4D slot gacor daftar sekarang deposit minimal Rp 10rb"), ("spam", "blank"), "promo spam")
    eq(act("頂尖成長工作室，SEO, AEO 全包，品牌曝光一次到位，加 LINE 立即諮詢免費健檢。"), ("spam", "blank"),
       "growth-studio ad (agency + contact)")
    eq(act("頂尖成長工作室，SEO, AEO 全包，品牌曝光一次到位，立即諮詢免費健檢。"), None,
       "agency words alone: one kind, not a verdict")
    eq(act("手遊《某某》推出首儲雙倍活動，玩家儲值 300 元即可獲得限定角色，公司表示本季營收成長三成。"), None,
       "game news about top-ups is not spam")
    eq(act("Indonesian police raided an online casino ring that laundered Rp 2 trillion through shell "
           "companies, officials said on Monday."), None, "rupiah in casino news: auxiliary only")
    eq(act("The growth studio model is spreading: agencies now sell SEO, AEO and paid social as one bundle."),
       None, "industry piece on agencies is not spam")
    eq(act("警方指出，該集團以首儲送、儲值 100 送 200 招攬，並要求加 LINE、代儲點數，最後再刷粉衝人氣。"),
       ("spam", "blank"), "reporting voice: three kinds still a verdict")
    body = "台積電公布第三季財報，營收創下新高，毛利率也優於市場預期，" + zh * 2
    pair = [{"title": "台積電第三季營收創新高", "url": "https://a.example/news/111", "summary": body},
            {"title": "台積電營收亮眼 法人喊進", "url": "https://b.example/zz/finance/9", "summary": body}]
    eq(summarize.check_shared(pair), {}, "retitled copy at 1/3 title overlap is not a mismatch")
    yt = "https://www.youtube.com/watch?v=abcdefghijk"
    it = {"summary": "Sign up Sign in", "title": "t", "url": yt}
    summarize.qc_apply([it]); eq(it["summary"], store.BLANK_SUMMARY, "youtube: refetch is pointless")
    items = [{"title": "蘋果發表新款手機", "url": "https://a.example/1", "summary": "蘋果今天發表新款手機，" + zh * 2},
             {"title": "颱風明天登陸", "url": "https://b.example/zz/2", "summary": "蘋果今天發表新款手機，" + zh * 2},
             {"title": "蘋果發表新款手機（更新）", "url": "https://a.example/1-update", "summary": "蘋果今天發表新款手機，" + zh * 2}]
    summarize.qc_apply(items[:2], fix=True)
    eq(["summary" in it for it in items[:2]], [True, False],
       "shared summary: keeper stays, mismatch refetched")
    eq(summarize.check_shared([items[0], items[2]]), {}, "retitled copy of the same article")
    eq(summarize.qc_gate({"title": "t", "url": "https://x.example/a"}, "Sign up Sign in", {})[0],
       store.BLANK_SUMMARY, "fresh summary failing QC is never stored: no retry state needed")
    eq(summarize.qc_gate({"title": "t", "url": "u"}, zh + "小" * 20 + zh, {})[0], zh + "小" + zh, "gate repairs")
    eq(summarize.qc_gate({"title": "颱風", "url": "https://c.example/9"}, items[0]["summary"], summarize.shared_index([items[0]]))[0],
       store.BLANK_SUMMARY, "gate: new item copying another title's summary")
    eq(summarize.qc_gate({"title": "t", "url": "u"}, zh * 2, summarize.shared_index(items))[0], zh * 2, "gate passes clean text")
    s = "教學：用 HTML 的 <input type=\"text\"> 建立欄位，" + zh
    eq(textproc.restrip(s), s, "restrip keeps HTML samples in code articles")
    eq("<img" in textproc.restrip(zh + "<img src=x>" + zh), False, "restrip still cleans debris")
    eq(textproc._accept(zh + "小" * 20 + zh)[0], zh + "小" + zh, "translation loop collapsed, rest kept")
    out = textproc.build(zh + "在那裡，在那裡，" * 12 + "後來大家都回家了。", "subtitle")
    eq((out is not None, (out or "").count("在那裡") <= 1), (True, True), "build collapses ASR loops, not drops")


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
    real = un.net.get
    try:
        un.net.get = lambda url, *a, **k: R() if "example" in url else None
        archive = {}
        raws = un.fetch_inbox(archive, d / "inbox.json", 60)
        eq([(r.title, r.url) for r in raws], [("Hello 世界", "https://example.org/p"), ("plain.org/a", "https://plain.org/a/")],
           "inbox: filtered, deduped, titled")
        un.ingest(archive, raws, now)
        ids = set(archive)
        un.net.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("refetched"))
        un.ingest(archive, un.fetch_inbox(archive, d / "inbox.json", 60), now)
        eq(set(archive), ids, "inbox: known url keeps its id, no refetch")
        (d / "inbox.json").write_text("[]")
        eq(un.fetch_inbox(archive, d / "inbox.json", 60), [], "inbox: malformed file ignored")
    finally:
        un.net.get = real


def check_routes_douban_blogspot():
    """Every douban mark (想X, 看过/读过/听过) uses the feed note or goes
    blank, never the page: the page is a login skeleton that reads as
    'blocked' and would leave the item pending forever. blogspot.com is
    feed-first, with thumbnails from the entry's image urls (feed_images),
    so image-only posts (mcclin) keep their picture."""
    db = "https://movie.douban.com/subject/1/"
    eq([summarize.route({"url": db, "title": t, "feed_content": ""}) for t in ("想看 X", "想读 X", "看过 X", "读过 X", "听过 X", "看過 X")],
       ["douban"] * 6, "every douban mark with a feed copy takes the douban route")
    eq(summarize.route({"url": db, "title": "看过 X"}), None, "douban mark without a feed copy waits (no route)")
    eq(summarize.route({"url": "https://www.douban.com/note/1/", "title": "想法"}), "fetch", "other douban pages still fetched")
    eq(summarize.route({"url": "https://mcclin.blogspot.com/x.html", "title": "t", "feed_images": ["https://a/b.png"]}),
       "feed", "feed images alone are enough for the feed route")
    eq([summarize.route({"url": "https://www.techmeme.com/1", "title": t, "feed_content": "x"}) for t in ("Source: X", "X")],
       ["feed", "feed"], "techmeme: plain feed-first")
    eq(summarize.route({"url": "https://www.techmeme.com/1", "title": "X"}), "fetch", "techmeme without feed copy: fetch")
    eq(summarize.douban_note("推荐: 力荐\n标签: 科幻\n备注: 很好看"), "很好看", "douban note: rating/tags dropped")
    d = Path(tempfile.mkdtemp())
    f = d / "a.json"
    tag = "Only those that risk going too far, can possibly know how far he can go.\n2026年10月8日"
    feed_img = "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvY/w485-h640/shot.png"
    img = "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvX/w1200-h630-p-k-no-nu/chart.png"
    f.write_text(json.dumps({"items": [
        {"id": "a" * 40, "url": "https://mcclin.blogspot.com/2026/10/x.html", "title": "t", "feed_content": tag},
        {"id": "b" * 40, "url": db, "title": "看过 X", "feed_content": "推荐: 力荐\n备注: 很好看的电影，结局令人意外。"},
        {"id": "c" * 40, "url": db, "title": "想看 Y", "feed_content": "备注: 朋友说这部电影的配乐非常出色。"},
        {"id": "d" * 40, "url": db, "title": "看过 Z", "feed_content": "推荐: 还行"},
        {"id": "e" * 40, "url": db, "title": "想读 W"},
        {"id": "8" * 40, "url": db, "title": "读过 V", "feed_content": ""},
        {"id": "f" * 40, "url": "https://fs.blog/x", "title": "t", "feed_content": tag},
        {"id": "9" * 40, "url": "https://mcclin.blogspot.com/2026/10/y.html", "title": "u", "feed_content": tag,
         "feed_images": [feed_img]}]}))
    page = f'<html><head><meta property="og:image" content="{img}"></head><body><p>{tag}</p></body></html>'
    fetched = []
    real_get, real_sleep = summarize.http_get, summarize.SLEEP
    summarize.http_get, summarize.SLEEP = (lambda url, t, imp=False: (fetched.append(url), (page, "ok"))[1]), 0
    try:
        summarize.run(f, store.load_doc(f), 50, do_backfill=False, translate=False)
    finally:
        summarize.http_get, summarize.SLEEP = real_get, real_sleep
    out = json.loads(f.read_text())["items"]
    eq((out[0].get("thumbnail"), out[0].get("summary")), (None, store.BLANK_SUMMARY),
       "feed-first, only boilerplate, no feed image: blank, page not read")
    eq((out[7].get("thumbnail"), out[7].get("summary"), "feed_images" in out[7]),
       (feed_img, store.BLANK_SUMMARY, False), "feed image used, blank, scratch dropped")
    eq(out[1].get("summary"), "很好看的電影，結局令人意外。", "看过: feed note")
    eq(out[2].get("summary"), "朋友說這部電影的配樂非常出色。", "想看: feed note, same flow")
    eq((out[3].get("summary"), "summary" in out[4], out[5].get("summary")), (store.BLANK_SUMMARY, False, store.BLANK_SUMMARY),
       "douban: rating only / empty copy -> blank; no copy -> stays pending")
    eq(fetched, [], "feed-first items never fetch the page")
    eq([k for it in out for k in store.SCRATCH if k in it], [], "no scratch left after a run")
    eq(summarize.thumb_from_page(f'<meta property="og:image" content="{img}">', "https://www.granitefirm.com/blog/x"),
       None, "granitefirm.com never yields a thumbnail")


def check_feed_images():
    """collect keeps an entry's image urls beside its plain-text copy."""
    rss = b"""<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>b</title>
<item><title>t</title><link>https://mcclin.blogspot.com/2026/10/x.html</link><pubDate>Thu, 08 Oct 2026 01:08:00 +0000</pubDate>
<description>&lt;div&gt;&lt;a href="https://blogger.googleusercontent.com/img/b/X/s1600/a.png"&gt;&lt;img height="640" src="https://blogger.googleusercontent.com/img/b/X/w485-h640/a.png" width="485" /&gt;&lt;/a&gt;&lt;img src="/icon.gif" width="16"&gt;&lt;/div&gt;</description>
<media:thumbnail url="https://blogger.googleusercontent.com/img/b/X/s72-c/a.png" height="72" width="72"/></item></channel></rss>"""
    raws = un.parse_rss(rss, {"category": "c", "title": "b", "url": "https://x/feed"})
    eq(list(raws[0].images), ["https://blogger.googleusercontent.com/img/b/X/w485-h640/a.png",
                              "https://blogger.googleusercontent.com/img/b/X/s72-c/a.png"],
       "feed images: body <img> first, icons skipped, media:thumbnail last")
    eq(raws[0].content, "", "image-only entry has no text")
    archive = {}
    un.ingest(archive, raws, datetime.now(timezone.utc))
    eq(next(iter(archive.values())).get("feed_images"), list(raws[0].images), "ingest stores feed_images")


def check_scratch_never_left():
    """feed_content / feed_images never reach archive.json: save_doc writes
    ITEM_FIELDS only, and collect hands them to summarize in memory."""
    d = Path(tempfile.mkdtemp())
    f = d / "a.json"
    yt = {"id": "a" * 40, "url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "v"}
    eq(summarize.route(dict(yt, feed_content="x", feed_images=["https://a/b.png"])), "youtube",
       "youtube items never take the feed route")
    items = [{"id": "b" * 40, "url": "https://fs.blog/x", "title": "t",
              "feed_content": "這是一段足夠長的部落格文章內容，討論市場與利率的關係。" * 3,
              "feed_images": ["https://example.com/img/rate-chart.png"], "site_name": "stray", "status": "read"},
             {"id": "c" * 40, "url": "https://example.org/x", "title": "p", "feed_content": "keep"}]
    store.save_doc(f, {"items": items})
    saved = json.loads(f.read_text())["items"]
    eq([sorted(it) for it in saved], [["id", "status", "title", "url"], ["id", "title", "url"]],
       "save_doc writes ITEM_FIELDS only (scratch and stray keys dropped)")
    eq("feed_content" in items[0], True, "save_doc leaves the in-memory copy alone")
    left = lambda: [k for it in json.loads(f.read_text())["items"] for k in store.SCRATCH if k in it]
    with contextlib.suppress(RuntimeError):
        real = summarize.Run.process
        summarize.Run.process = lambda self, p: (_ for _ in ()).throw(RuntimeError("crash"))
        try:
            summarize.run(f, {"items": [dict(it) for it in items]}, 0, do_backfill=False)
        finally:
            summarize.Run.process = real
    eq(left(), [], "no scratch on disk after a crash")
    summarize.run(f, {"items": items}, 0, do_backfill=False, translate=False)
    out = json.loads(f.read_text())["items"]
    eq((bool(out[0].get("summary")), out[0].get("thumbnail")),
       (True, "https://example.com/img/rate-chart.png"), "in-memory feed copy feeds the feed route")
    eq(left(), [], "no scratch on disk after a run")


def check_collect_then_summarize():
    """collect collects and summarizes in one process: the feed copy reaches summarize without any file."""
    rss = ("<rss version=\"2.0\"><channel><title>b</title><item><title>t</title><link>https://fs.blog/x</link>"
           "<pubDate>" + datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000") + "</pubDate>"
           "<description>" + "這是一段足夠長的部落格文章內容，討論市場與利率的關係。" * 3 + "</description>"
           "</item></channel></rss>").encode()
    d = Path(tempfile.mkdtemp())
    real_get, real_sleep = net.get, summarize.SLEEP
    net.get = lambda url, *a, **k: net.Resp(200, rss, {}, url)
    summarize.SLEEP = 0
    files_before = set(Path(tempfile.gettempdir()).iterdir())
    try:
        collect(d, opml=b'<opml><body><outline title="B" xmlUrl="https://fs.blog/feed"/></body></opml>')
    finally:
        net.get, summarize.SLEEP = real_get, real_sleep
    out = store.load_doc(d / "archive.json")["items"]
    eq((len(out), bool(out and out[0].get("summary")), [k for it in out for k in store.SCRATCH if k in it]),
       (1, True, []), "collect + summarize in one process; feed copy used, never saved")
    eq([p.name for p in set(Path(tempfile.gettempdir()).iterdir()) - files_before
        if "scratch" in p.name], [], "no sidecar file written")


def check_throttled_save():
    """Saves are checkpointed, not per item; the end of a run always writes."""
    d = Path(tempfile.mkdtemp())
    f = d / "a.json"
    db = "https://movie.douban.com/subject/1/"
    items = [{"id": f"{i:040x}", "url": db, "title": f"想看 {i}", "feed_content": "推荐: 还行"} for i in range(5)]
    writes = []
    real = summarize.save_doc
    summarize.save_doc = lambda p, doc: (writes.append(p), real(p, doc))
    try:
        summarize.run(f, {"items": items}, 0, do_backfill=False, translate=False)
    finally:
        summarize.save_doc = real
    eq((len(writes), [it.get("summary") for it in json.loads(f.read_text())["items"]]),
       (1, [store.BLANK_SUMMARY] * 5), "5 items, one write at the end")


def check_index_gate():
    """gate looks peers up in the index: same verdict as a full scan."""
    zh = "蘋果今天發表新款手機，研究人員在報告中指出，這項技術可以大幅降低資料中心的耗電量，並預計明年開始量產。" * 2
    items = [{"title": "蘋果發表新款手機", "url": "https://a.example/1", "summary": zh}] + \
            [{"title": f"其他新聞 {i}", "url": f"https://o.example/{i}", "summary": f"不相關的摘要內容第 {i} 則。" * 10}
             for i in range(200)]
    idx = summarize.shared_index(items)
    eq(len(idx.get(summarize._share_key(zh), [])), 1, "index holds the summary once")
    eq(summarize.qc_gate({"title": "颱風明天登陸", "url": "https://c.example/9"}, zh, idx)[0], store.BLANK_SUMMARY,
       "copied summary caught via index")
    it = {"title": "新的文章", "url": "https://n.example/1", "summary": "全新的摘要內容。" * 20}
    summarize.index_add(idx, it)
    eq(summarize.qc_gate({"title": "另一篇無關", "url": "https://z.example/q/9"}, it["summary"], idx)[0], store.BLANK_SUMMARY,
       "index_add keeps the index current within a run")


def check_child_env():
    """yt-dlp children never see secrets."""
    old = dict(os.environ)
    os.environ.update({"DEEPL_API_KEY": "k", "FOLLOW_OPML_B64": "o", "GITHUB_TOKEN": "t", "YT_COOKIES": "c",
                       "HF_HOME": "/hf", "KEEP_ME": "1"})
    try:
        rc, out, _ = ds.run_cmd(["sh", "-c", "env"], 30)
    finally:
        os.environ.clear()
        os.environ.update(old)
    eq((rc, "HF_HOME=/hf" in out and "PATH=" in out, "KEEP_ME" in out, [k for k in ("DEEPL", "OPML", "TOKEN", "YT_COOKIES") if k in out]), (0, True, False, []),
       "subprocess env: allowlist only, no secrets")


def check_offline_only():
    """media's run summarizes fresh subtitles without fetching pages."""
    d = Path(tempfile.mkdtemp())
    store.configure(data_dir=d)
    (d / "subtitles").mkdir()
    yid = "a" * 40
    (d / "subtitles" / f"{yid}.zh-TW.zh-TW.vtt").write_text("WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n"
        + "這是一段關於考古發現的影片內容，研究人員在烏克蘭南部發掘古墓。" * 20 + "\n", encoding="utf-8")
    f = d / "a.json"
    f.write_text(json.dumps({"items": [
        {"id": yid, "url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "v", "source": "y", "category": "x"},
        {"id": "b" * 40, "url": "https://example.org/x", "title": "p", "source": "s", "category": "x", "feed_content": "keep"}]}))
    real_fetch = summarize.fetch_article
    summarize.fetch_article = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
    try:
        summarize.run(f, store.load_doc(f), 50, offline_only=True, do_backfill=False, translate=False)
    finally:
        summarize.fetch_article = real_fetch
    out = json.loads(f.read_text())["items"]
    eq((bool(out[0].get("summary")), "summary" in out[1], out[1].get("feed_content")), (True, False, None),
       "offline-only: subtitle summarized, page not fetched, feed copy dropped")


def check_cookies_status():
    """An expired cookies file must leave a trace the reader can show; a healthy
    run must not commit every day; an idle run must not claim cookies are fine."""
    with tempfile.TemporaryDirectory() as d:
        status = Path(d) / "status.json"
        eq(ds.record_status(status, "cookies", "ok", "t1"), True, "status: first write")
        eq(ds.record_status(status, "cookies", "ok", "t2"), False, "status: unchanged ok is not rewritten")
        eq(json.loads(status.read_text())["cookies"]["checked_at"], "t1", "status: ok keeps its first time")
        eq(ds.record_status(status, "cookies", "expired", "t3"), True, "status: ok -> expired")
        eq(ds.record_status(status, "cookies", "expired", "t4"), True, "status: expired is refreshed every run")
        eq(json.loads(status.read_text())["cookies"], {"state": "expired", "checked_at": "t4"}, "status: expired content")
        status.write_text("{broken", encoding="utf-8")
        eq(ds.record_status(status, "cookies", "ok", "t5"), True, "status: damaged file starts over")

        archive = Path(d) / "archive.json"
        yid = "c" * 40
        archive.write_text(json.dumps({"items": [{"id": yid, "url": "https://www.youtube.com/watch?v=abcdefghijk",
                                                   "title": "v", "source": "y", "category": "x"}]}))
        real_run = ds.run_cmd
        try:
            status.unlink()
            ds.run_cmd = lambda cmd, timeout: (1, "", "ERROR: [youtube] x: The provided YouTube account cookies are no longer valid")
            subs(archive, d)
            eq(json.loads(status.read_text())["cookies"]["state"], "expired", "media: rejected cookies are recorded")
            ds.run_cmd = lambda cmd, timeout: (1, "", "ERROR: some other failure")
            subs(archive, d)
            eq(json.loads(status.read_text())["cookies"]["state"], "ok", "media: a run that was not rejected clears it")
            status.unlink()
            archive.write_text(json.dumps({"items": []}))
            subs(archive, d)
            eq(status.exists(), False, "media: nothing probed means no claim about cookies")
        finally:
            ds.run_cmd = real_run


def check_security():
    for u in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:4416/", "http://localhost/",
              "http://10.1.2.3/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/",
              "file:///etc/passwd", "ftp://example.com/", "https://user:pw@1.1.1.1/", "javascript:alert(1)"):
        eq(net.safe_url(u), False, f"refuse {u}")
        eq(net.get(u, 1), None, f"never fetched: {u}")
    eq(net.safe_url("https://1.1.1.1/"), True, "public address allowed")
    import socket
    real, answers, pinned = socket.getaddrinfo, iter(["1.1.1.1", "127.0.0.1"]), []

    def rebinding(host, *a, **k):          # passes the check once, then answers loopback
        return real(next(answers) if host == "rebind.test" else host, *a, **k)

    def once(method, url, ips, *a, **k):
        pinned.append(list(ips))
        raise OSError("stop")
    real_once, socket.getaddrinfo, net._once = net._once, rebinding, once
    try:
        net.get("http://rebind.test/", 5)
    finally:
        socket.getaddrinfo, net._once = real, real_once
    eq(pinned, [["1.1.1.1"]], "DNS rebinding: one lookup per hop, the connection pinned to the checked address")
    eq(net.public_addrs("64:ff9b::7f00:1", 80), [], "NAT64 address embedding loopback refused")
    eq([store.valid_id(x) for x in ("a" * 40, "../" + "a" * 37, "A" * 40, None)],
       [True, False, False, False], "item ids are sha1 hex only")
    eq([store.safe_lang(x) for x in ("zh-Hant", "en", "../../x", "", ".*", None)],
       ["zh-Hant", "en", "und", "und", "und", "und"], "language codes fit for filenames")
    cmd = ds.ytdlp(Path("c"), "--exec=rm -rf ~", "--skip-download")
    eq(cmd[-2:], ["--", "--exec=rm -rf ~"], "url passed after --")
    arch = {}
    un.ingest(arch, [un.Raw("c", "s", "t", u, None) for u in ("javascript:alert(1)", "httpx://a", "https://ok.example/a")],
              un.parse_date("2026-01-01"))
    eq([r["url"] for r in arch.values()], ["https://ok.example/a"], "only http(s) item urls stored")
    eq(summarize.thumb_from_page('<meta property="og:image" content="javascript:alert(1)//x.png">', "https://a.example/"),
       None, "non-http thumbnail refused")
    eq(un.html_to_text("<p>a<code>x</code>b</p><pre>c</pre><code>1\n2</code><script>d</script>e"), "axb\ne",
       "feed html to text")


def check_telegram_channels():
    page = "https://t.me/s/madchuwin"
    for url in ("https://t.me/s/madchuwin", "https://t.me/madchuwin", "http://t.me/madchuwin/", "https://t.me/madchuwin/1234",
                "https://t.me/s/madchuwin/", "https://telegram.me/madchuwin", "https://T.ME/madchuwin"):
        eq(un.telegram_page_url(url), page, f"channel url normalised: {url}")
    for url in ("https://t.me/joinchat/AAAA", "https://t.me/+abcdEFGH", "https://t.me/c/123456/789", "https://t.me/addstickers/pack",
                "https://t.me/s/", "https://t.me/", "https://t.me/ab", "https://t.me/1channel", "https://t.me/name/notanumber",
                "https://t.me/name/1/2", "https://evil.example/madchuwin", "https://t.me.evil.example/madchuwin",
                "https://t.me/joinchat", "https://t.me/share/123", "https://t.me/addstickers", "https://t.me/proxy",
                "ftp://t.me/madchuwin"):
        eq(un.telegram_page_url(url), None, f"not a public channel: {url}")
    eq(un.telegram_page_url("https://t.me/name?x=%2e%2e#frag"), "https://t.me/s/name", "query and fragment are dropped, never passed through")
    eq(un.bridge("https://t.me/madchuwin", ""), ("telegram", page), "xmlUrl t.me/NAME bridges to the preview page")
    eq(un.bridge("https://t.me/s/madchuwin", ""), ("telegram", page), "xmlUrl t.me/s/NAME bridges to the preview page")
    eq(un.bridge("https://example.com/rss", "https://t.me/madchuwin"), ("telegram", page), "htmlUrl t.me/NAME bridges to the preview page")
    eq(un.bridge("https://rsshub.app/telegram/channel/madchuwin", ""), ("telegram", page), "rsshub telegram route bridges to the preview page")
    eq(un.bridge("https://example.com/rss", "https://example.com/"), None, "ordinary feeds are not bridged")
    opml = (b'<opml><body><outline title="T" xmlUrl="https://t.me/madchuwin" category="news"/>'
            b'<outline title="J" xmlUrl="https://t.me/joinchat/AAAA"/></body></opml>')
    feeds = un.read_opml(opml, 0)
    eq((feeds[0]["parser"], feeds[0]["url"]), ("telegram", page), "OPML telegram entry uses the telegram parser")
    eq(feeds[1]["parser"], "rss", "an invite link is not treated as a channel")
    html = """<div class="tgme_widget_messages_list">
      <div class="tgme_widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="madchuwin/101">
        <div class="tgme_widget_message_text js-message_text" dir="auto">第一則 <b>重點</b><br/>第二行</div>
        <div class="tgme_widget_message_info"><a class="tgme_widget_message_date" href="https://t.me/madchuwin/101"><time datetime="2026-10-05T08:00:00+00:00" class="time">08:00</time></a></div></div></div>
      <div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="../evil/1">
        <div class="tgme_widget_message_text">不該收的</div><time datetime="2026-10-05T09:00:00+00:00"></time></div></div>
      <div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="madchuwin/102">
        <div class="tgme_widget_message_photo_wrap"></div><time datetime="2026-10-05T10:00:00+00:00"></time></div></div>
    </div>"""
    raw = un.parse_telegram(html, {"category": "news", "title": "T"})
    eq([x.url for x in raw], ["https://t.me/madchuwin/101"], "only well-formed posts that have text and a date are kept")
    eq(len(raw), 1, "post with attribute-built path or no text is dropped")
    seen = []
    def fake_get(url, *a, **k):
        seen.append(url)
        return net.Resp(200, html.encode(), {"content-type": "text/html"}, url)
    real_get = net.get
    net.get = fake_get
    try:
        fetched = un.fetch_feeds([feeds[0]])
    finally:
        net.get = real_get
    eq(seen, [page], "the preview page is the only thing fetched")
    eq(len(fetched), 1, "items come out of the fetched preview page")
    long_text = "長" * 300
    page2 = f"""<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="chan_x/201">
      <a class="tgme_widget_message_reply" href="https://t.me/chan_x/150"><i class="tgme_widget_message_reply_thumb" style="background-image:url('https://cdn1.telesco.pe/file/reply.jpg')"></i>
        <div class="tgme_widget_message_text js-message_reply_text">被引用的舊貼文</div></a>
      <a class="tgme_widget_message_photo_wrap" style="width:800px;background-image:url('https://cdn4.telesco.pe/file/photo.jpg')"></a>
      <div class="tgme_widget_message_text js-message_text">台積電 &amp; 輝達 <b>合作</b><br/>第二行<br/><br/>{long_text}</div>
      <a class="tgme_widget_message_link_preview"><i class="link_preview_right_image" style="background-image:url('https://cdn4.telesco.pe/file/preview.jpg')"></i>
        <div class="link_preview_title">預覽標題</div><div class="link_preview_description">預覽描述。</div></a>
      <time datetime="2026-10-15T08:00:00+00:00"></time></div></div>"""
    r = un.parse_telegram(page2, {"category": "news", "title": "T"})[0]
    eq(r.content, f"台積電 & 輝達 合作\n第二行\n{long_text}\n預覽標題\n預覽描述。",
       "telegram: feed copy is the full post text (lines kept, reply left out) plus the link preview")
    eq(r.images, ("https://cdn4.telesco.pe/file/photo.jpg", "https://cdn4.telesco.pe/file/preview.jpg"),
       "telegram: photo and preview images, not the reply thumbnail")
    eq((r.title, raw[0].title), ("被引用的舊貼文", "第一則 重點 第二行"),
       "telegram: title computed as before (the id hashes it), first text block, get_text(' ', strip)")
    eq(summarize.route({"url": r.url, "feed_content": r.content}), "feed", "telegram: summarized from the feed copy")
    eq(summarize.thumb_pick(r.images, r.url), "https://cdn4.telesco.pe/file/photo.jpg", "telegram: telesco.pe is a trusted image host")
    nested = '<div class="tgme_widget_message" data-post="a_bc/1">' * 20000
    t = time.monotonic()
    un.parse_telegram(nested, {"category": "c", "title": "T"})
    eq(time.monotonic() - t < 3, True, "telegram: nested messages parse in linear time")


def check_youtube_channel_urls():
    uc = "UC" + "a" * 22
    feed = "https://www.youtube.com/feeds/videos.xml"
    eq(un.youtube_feed_url(f"https://www.youtube.com/channel/{uc}"), f"{feed}?channel_id={uc}", "channel url to feed")
    eq(un.youtube_feed_url(f"https://youtube.com/channel/{uc}/videos"), f"{feed}?channel_id={uc}", "channel subpage to feed")
    eq(un.youtube_feed_url("https://www.youtube.com/playlist?list=PLabcdefghijk"), f"{feed}?playlist_id=PLabcdefghijk", "playlist to feed")
    eq(un.youtube_feed_url("https://www.youtube.com/@name"), None, "handle needs the network")
    eq(un.youtube_feed_url("https://evil.example/channel/" + uc), None, "other hosts are not YouTube")
    eq(un.youtube_feed_url("https://www.youtube.com/channel/UCshort"), None, "malformed channel id refused")
    eq([un.is_youtube_channel_page(u) for u in ("https://www.youtube.com/@name", "https://www.youtube.com/c/name",
        "https://www.youtube.com/user/name", "https://www.youtube.com/@", "https://evil.example/@name",
        "https://www.youtube.com/watch?v=x")], [True, True, True, False, False, False], "which pages need resolving")
    eq(un.extract_youtube_channel_id(f'<link rel="alternate" type="application/rss+xml" title="RSS" href="{feed}?channel_id={uc}">'),
       uc, "channel id from the RSS link")
    eq(un.extract_youtube_channel_id(f'<link rel="canonical" href="https://www.youtube.com/channel/{uc}">'), uc, "channel id from canonical")
    eq(un.extract_youtube_channel_id(f'{{"externalId":"{uc}"}}'), uc, "channel id from metadata")
    eq(un.extract_youtube_channel_id('<link type="application/rss+xml" href="x?channel_id=UC"><script>alert(1)</script>'), None,
       "page content that is not a well-formed id is never returned")
    eq(un.extract_youtube_channel_id('{"externalId":"UCshort"}'), None, "metadata id must be exactly 24 characters")
    eq(un.extract_youtube_channel_id('{"externalId":"UC' + "a" * 40 + '"}'), None, "over-long id refused")
    opml = (f'<opml><body><outline title="A" xmlUrl="https://www.youtube.com/channel/{uc}"/>'
            f'<outline title="B" xmlUrl="https://www.youtube.com/@handle"/>'
            f'<outline title="C" xmlUrl="https://c.example/rss"/></body></opml>').encode()
    feeds = {f["title"]: f for f in un.read_opml(opml, 0)}
    eq((feeds["A"]["url"], feeds["A"]["youtube_page"]), (f"{feed}?channel_id={uc}", False), "OPML channel url converted without fetching")
    eq((feeds["B"]["url"], feeds["B"]["youtube_page"]), ("https://www.youtube.com/@handle", True), "OPML handle url marked for resolving")
    eq(feeds["C"]["youtube_page"], False, "ordinary feeds are not resolved")
    # fetch_feeds: the handle page is fetched, then the resolved feed is parsed
    page = f'<link rel="alternate" type="application/rss+xml" href="{feed}?channel_id={uc}">'.encode()
    rss = b'<rss version="2.0"><channel><title>T</title><item><title>Video</title><link>https://www.youtube.com/watch?v=abc</link><pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>'
    seen = []
    def fake_get(url, *a, **k):
        seen.append(url)
        return net.Resp(200, page if "@handle" in url else rss, {"content-type": "text/html" if "@handle" in url else "application/xml"}, url)
    real_get = net.get
    net.get = fake_get
    try:
        raw = un.fetch_feeds([feeds["B"]])
    finally:
        net.get = real_get
    eq(seen, ["https://www.youtube.com/@handle", f"{feed}?channel_id={uc}"], "handle page fetched, then its resolved feed")
    eq(len(raw), 1, "items parsed from the resolved feed")


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
            with env(DATA_DIR=d):
                try:
                    un.main([])
                    eq("no exit", "exit", "without OPML the run fails")
                except SystemExit as e:
                    eq(bool(e.code), True, "without OPML the run fails")
            eq((Path(d) / "archive.json").exists(), False, "nothing written when the OPML is missing")
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
    eq(un.canonical_url("https://vocus.cc/@/6aba2fc9fd897800019e270a"),
       "https://vocus.cc/article/6aba2fc9fd897800019e270a", "vocus: empty author slot")
    now = un.parse_date("2026-09-29")
    arch = {}
    raw = un.Raw("tech", "S", "T", "https://a.com/p", un.parse_date("2026-09-01"), content="body")
    un.ingest(arch, [raw], now)
    rec = next(iter(arch.values()))
    eq(list(rec), ["id", "category", "source", "title", "url", "published_at", "last_seen_at",
                   "feed_content"], "new record fields (no site_name / first_seen_at)")
    with tempfile.TemporaryDirectory() as d:
        store.save_doc(Path(d) / "a.json", {"items": [dict(rec, feed_images=["https://a/b.png"])]})
        saved = json.loads((Path(d) / "a.json").read_text(encoding="utf-8"))["items"][0]
    eq(set(saved) <= store.ITEM_FIELDS, True, f"saved item fields within ITEM_FIELDS: {sorted(saved)}")
    import dataclasses
    un.ingest(arch, [dataclasses.replace(raw, published_at=un.parse_date("2026-09-02"))], now)
    eq(rec["published_at"], "2026-09-02T00:00:00Z", "feed date overwrites any category")
    with tempfile.TemporaryDirectory() as d:
        now = datetime.now(timezone.utc)
        ago = lambda days: un.iso(now - timedelta(days=days))
        store.save_doc(Path(d) / "archive.json", {"items": [
            {"id": "a" * 40, "title": "old", "url": "https://x.example/1", "last_seen_at": ago(61)},
            {"id": "b" * 40, "title": "new", "url": "https://x.example/2", "last_seen_at": ago(59)},
            {"id": "c" * 40, "title": "legacy", "url": "https://x.example/3", "published_at": ago(10)}]})
        collect(d, "--no-summarize", ARCHIVE_DAYS=None)
        kept = lambda: sorted(r["title"] for r in store.load_doc(Path(d) / "archive.json")["items"])
        eq(kept(), ["legacy", "new"], "default retention: 60 days of last_seen_at")
        collect(d, "--no-summarize", ARCHIVE_DAYS=5)
        eq(kept(), [], "retention days configurable (ARCHIVE_DAYS)")


def check_thumb_clean():
    c = summarize.thumb_clean
    eq(c("https://a.cn/x/chart.png?x-oss-process=image/resize,w_1024"), "https://a.cn/x/chart.png", "oss resize dropped")
    eq(c("https://a.cn/x.jpg?v=2&x-oss-process=image/resize,w_1024/format,webp#f"), "https://a.cn/x.jpg?v=2#f", "other params kept")
    eq(c("https://a.cn/x.jpg?imageMogr2/thumbnail/600x"), "https://a.cn/x.jpg", "qiniu dropped")
    eq(c("https://a.cn/x.jpg?w=5"), "https://a.cn/x.jpg?w=5", "unrelated query untouched")


def check_firstory_host():
    c = un.canonical_url
    eq(c("https://open.firstory.me/story/clwysgs8128dq01uk5tt621ha"),
       "https://open.firstory.fm/story/clwysgs8128dq01uk5tt621ha", "firstory .me -> .fm")
    eq(c("https://OPEN.FIRSTORY.ME/story/abc?x=1"), c("https://open.firstory.fm/story/abc?x=1"), "firstory host case")
    eq(c("https://open.firstory.fm/story/abc"), "https://open.firstory.fm/story/abc", "firstory .fm unchanged")
    eq(c("https://feed.firstory.me/rss/user/abc"), "https://feed.firstory.me/rss/user/abc", "other firstory hosts untouched")
    with tempfile.TemporaryDirectory() as d:
        old = "https://open.firstory.me/story/abc"
        rec = {"id": un.make_id("opmlrss", "S", "T", old), "category": "opmlrss", "source": "S", "title": "T",
               "url": old, "published_at": "2026-10-01T00:00:00Z", "last_seen_at": "2026-10-01T00:00:00Z"}
        store.save_doc(Path(d) / "archive.json", {"items": [rec]})
        _, arch = un.load_archive(Path(d) / "archive.json")
        new_id = un.make_id("opmlrss", "S", "T", "https://open.firstory.fm/story/abc")
        eq([(r["id"], r["url"]) for r in arch.values()], [(new_id, "https://open.firstory.fm/story/abc")],
           "stored .me item migrated and re-keyed on load")


def check_podcast():
    eq([store.is_episode(u) for u in (
        "https://www.xiaoyuzhoufm.com/episode/6ab2546493d5eb3bdc794b18",
        "https://open.firstory.fm/story/clwysgs8128dq01uk5tt621ha",
        "https://open.firstory.me/story/clwysgs8128dq01uk5tt621ha",
        "https://www.xiaoyuzhoufm.com/podcast/61358d971c5d56efe5bcb5d2",
        "https://open.firstory.fm/user/decosmos")], [True, True, True, False, False], "podcast episode urls")
    eq((ds.hms("2588"), ds.hms("43:08"), ds.hms("00:43:08"), ds.hms("x")),
       (2588.0, 2588.0, 2588.0, 0.0), "itunes durations")
    page = '<meta property="og:audio" content="https://media.xyzcdn.net/a/b.m4a">'
    eq(ds._audio_in_page(page), "https://media.xyzcdn.net/a/b.m4a", "og:audio")
    eq(ds._audio_in_page('{"u":"https:\\/\\/cdn.x\\/r\\/a.mp3"}'), "https://cdn.x/r/a.mp3", "escaped json audio url")
    real = ds._xiaoyuzhou
    try:
        for audio, want in (("http://127.0.0.1/a.m4a", False), ("http://169.254.169.254/a.mp3", False),
                            ("file:///etc/passwd.mp3", False)):
            ds._xiaoyuzhou = lambda u, e, audio=audio: ds.Episode(audio)
            eq(ds.resolve_episode("https://www.xiaoyuzhoufm.com/episode/6ab2546493d5eb3bdc794b18") is not None,
               want, f"audio url guarded: {audio}")
    finally:
        ds._xiaoyuzhou = real
    ep = {"id": "e" * 40, "url": "https://www.xiaoyuzhoufm.com/episode/6ab2546493d5eb3bdc794b18"}
    eq(summarize.route(ep), None, "podcast waits for ASR")
    # No usable audio is the episode's own nature -> blank. Not reaching the
    # page is transient -> pending, untouched, retried every run.
    class A:
        no_transcribe, max_transcribe, max_asr_duration, max_asr_total = True, 0, 0, 0
    url = ep["url"]
    page_no_audio = "<html><head><title>單集</title></head><body>付費內容</body></html>"
    page_private = '<meta property="og:audio" content="http://127.0.0.1/a.m4a">'
    cases = [("page loads, no audio", net.Resp(200, page_no_audio.encode(), {}, url), store.BLANK_SUMMARY),
             ("page gone (404)", net.Resp(404, b"", {}, url), store.BLANK_SUMMARY),
             ("audio on a private address", net.Resp(200, page_private.encode(), {}, url), store.BLANK_SUMMARY),
             ("no response", None, None),
             ("HTTP 503", net.Resp(503, b"", {}, url), None),
             ("HTTP 403", net.Resp(403, b"", {}, url), None)]
    real_get = net.get
    try:
        for label, resp, want in cases:
            net.get = lambda *a, resp=resp, **k: resp
            it = {"id": "f" * 40, "url": url, "title": "t"}
            n = __import__("collections").Counter()
            changed = [ds.podcast_asr(it, A, ds.Budget(A), Path("."), n) for _ in range(3)]
            eq((it.get("summary"), changed[0]), (want, want is not None), f"podcast: {label}")
            if want is None:
                eq((changed, sorted(it)), ([False] * 3, ["id", "title", "url"]), f"podcast: {label} -> untouched, retried")
    finally:
        net.get = real_get
    real_resolve = ds.resolve_episode
    try:
        ds.resolve_episode = lambda u: (_ for _ in ()).throw(ValueError("parser bug"))
        it = {"id": "f" * 40, "url": url, "title": "t"}
        eq((ds.podcast_asr(it, A, ds.Budget(A), Path("."), __import__("collections").Counter()), "summary" in it),
           (False, False), "podcast: a parser error is not the episode's fault -> pending")
    finally:
        ds.resolve_episode = real_resolve
    eq(summarize.route(dict(ep, published_at="2020-01-01T00:00:00Z")), None, "old podcast still waits: no time limit")
    eq(summarize.route(dict(ep, feed_content="show notes " * 50)), None, "podcast never falls back to show notes")


def check_status_ignores_podcasts():
    """A run that only handled podcasts says nothing about the YouTube cookies."""
    with tempfile.TemporaryDirectory() as d:
        archive, status = Path(d) / "archive.json", Path(d) / "status.json"
        archive.write_text(json.dumps({"items": [{"id": "e" * 40, "title": "t", "source": "s", "category": "c",
                                                  "url": "https://www.xiaoyuzhoufm.com/episode/6ab2546493d5eb3bdc794b18"}]}))
        real = ds.resolve_episode
        ds.resolve_episode = lambda u: (_ for _ in ()).throw(ds.EpisodeUnreachable("no response"))
        try:
            subs(archive, d)
        finally:
            ds.resolve_episode = real
        eq(status.exists(), False, "subs: podcast-only run leaves the cookies flag alone")


def check_accept_language():
    """lang reaches the request; the browser fingerprint keeps only its own headers."""
    sent = []
    real = net._fetch
    net._fetch = lambda method, url, timeout, sink, limit, **kw: sent.append(kw["headers"])
    try:
        net.get("https://example.com/", 5, lang="zh-TW")
        net.get("https://example.com/", 5)
        net.get("https://example.com/", 5, impersonate=True, lang="ja")
    finally:
        net._fetch = real
    eq([h.get("Accept-Language") for h in sent], ["zh-TW", net.PAGE_LANG, "ja"],
       "Accept-Language: given lang sent, else the page default")
    eq((sent[0]["User-Agent"], sorted(sent[2])), (net.UA, ["Accept-Language"]),
       "plain requests carry the page headers; impersonated ones only the language")


def check_summary_lines():
    """Line breaks in the source survive into the summary (not for captions,
    whose lines are cue units); translation and QC repair keep them too."""
    a = "研究團隊在新的論文中指出，大型語言模型的推理能力在特定基準上有明顯提升。成本仍然很高，部署也不容易。"
    b = "工程師表示，部署這些模型需要大量的運算資源與電力，資料中心正在擴建。"
    c = "業界預計明年會有更便宜的推理晶片上市，價格可能下降一半以上。"
    s = textproc.build("\n".join([a, b, c]) * 1, "page")
    eq(s.count("\n") >= 1 and all(ln.strip() == ln and ln for ln in s.split("\n")), True,
       "summary: source line breaks kept, lines trimmed, no blank lines")
    eq(textproc.extractive("\n".join([a, b, c]), True, 10_000, "page", lines=True), "\n".join([a, b, c]),
       "summary: break exactly where the source had one, sentences of a line stay together")
    eq("\n" in textproc.build("\n".join([a, b, c]), "subtitle"), False, "summary: caption lines are not kept")
    eq(textproc.tidy("中文 ， 句子 。 \n\n  English   text  \n"), "中文，句子。\nEnglish text", "tidy keeps lines")

    calls = []

    def fake_many(texts, stop_after=0):
        calls.append(list(texts))
        return [(None, "empty input") if not t else (None, "result still not Chinese") if t == "OK"
                else ("譯:" + t, "") for t in texts]
    real = textproc.translate_many
    textproc.translate_many = fake_many
    try:
        got = textproc.translate_keep_lines(["one\ntwo", "single", "bad\nOK", ""])
    finally:
        textproc.translate_many = real
    eq(got[:3], [("譯:one\n譯:two", ""), ("譯:single", ""), ("譯:bad OK", "")],
       "translate_keep_lines: per-line, single, whole-text retry")
    eq(got[3][0], None, "translate_keep_lines: empty stays empty")
    eq(calls, [["one", "two", "single", "bad", "OK"], ["bad OK"]],
       "translate_keep_lines: one batch, one retry, nothing sent for an empty text")
    calls.clear()
    textproc.translate_many = lambda texts, stop_after=0: (calls.append(list(texts)) or
                                                          [(None, textproc.SKIPPED)] * len(texts))
    try:
        got = textproc.translate_keep_lines(["a\nb"], stop_after=1)
    finally:
        textproc.translate_many = real
    eq((got, len(calls)), ([(None, textproc.SKIPPED)], 1), "translate_keep_lines: no retry while gtx refuses")
    body = ("<p>研究人員在報告中指出，這項技術可以大幅降低資料中心的耗電量。</p>\n"
            "<p>預計明年開始量產，價格也會比現有方案便宜三成以上，業界相當期待。</p>") * 2
    f = summarize.qc_inspect({"summary": body, "title": "t"})
    eq(bool(f) and f.action == "repair" and "\n" in f.fixed and "<" not in f.fixed, True,
       "qc repair: tags stripped, line breaks kept")


def check_asr_permanence():
    """Only the item's own nature ends it: no speech, or a video over the cap.
    Download failures and timeouts keep it pending (a podcast over the cap too)."""
    import types, importlib.util
    a = types.SimpleNamespace(no_transcribe=False, max_transcribe=0, max_asr_duration=0, max_asr_total=0,
                              audio_timeout=1, whisper_model="x", whisper_compute_type="int8")
    real_t, real_spec = ds.transcribe, importlib.util.find_spec
    importlib.util.find_spec = lambda name: object()
    try:
        for exc, podcast_, want in ((RuntimeError("download timed out"), False, None),
                                    (ImportError("no whisper"), False, None),
                                    (ds.NoSpeech("x"), False, store.BLANK_SUMMARY),
                                    (ds.NoSpeech("x"), True, store.BLANK_SUMMARY),
                                    (ds.TooLong("x"), False, store.BLANK_SUMMARY),
                                    (ds.TooLong("x"), True, None)):
            def boom(*_a, exc=exc):
                raise exc
            ds.transcribe = boom
            it, n = {"id": "a" * 40}, __import__("collections").Counter()
            with tempfile.TemporaryDirectory() as d:
                ds.asr(it, None, 60, False, a, ds.Budget(a), Path(d), n, None, is_podcast=podcast_)
            eq(it.get("summary"), want, f"asr: {type(exc).__name__} podcast={podcast_}")
    finally:
        ds.transcribe, importlib.util.find_spec = real_t, real_spec


def check_download_guard():
    """Podcast audio goes through net.download: unsafe urls never reach the
    network and leave no file behind; size and the 200 status are enforced."""
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "a.mp3"
        for u in ("http://127.0.0.1/a.mp3", "http://169.254.169.254/a.mp3", "file:///etc/passwd", "https://u:p@1.1.1.1/a"):
            eq((net.download(u, dest, 5, 1000), dest.exists()), (False, False), f"download refused: {u}")

        def fake_once(method, url, ips, deadline, sink, limit, **kw):
            status, chunks, headers = replies.pop(0)
            total = 0
            for c in chunks:
                total += len(c)
                if total > limit:
                    raise net._TooLarge()
                sink(c)
            return status, headers, headers.get("location")
        real = net._once
        net._once = fake_once
        try:
            replies = [(302, [], {"location": "http://10.0.0.1/x.mp3"})]
            eq(net.download("https://1.1.1.1/a.mp3", dest, 5, 1000), False, "download: redirect to private refused")
            replies = [(200, [b"x" * 600, b"x" * 600], {})]
            eq((net.download("https://1.1.1.1/a.mp3", dest, 5, 1000), dest.exists()), (False, False),
               "download: size cap, partial file removed")
            replies = [(404, [b"nope"], {})]
            eq((net.download("https://1.1.1.1/a.mp3", dest, 5, 1000), dest.exists()), (False, False),
               "download: non-200 refused, no file left")
            replies = [(302, [b"moved"], {"location": "https://1.1.1.1/b.mp3"}), (200, [b"ab", b"cd"], {})]
            eq((net.download("https://1.1.1.1/a.mp3", dest, 5, 1000), dest.read_bytes()), (True, b"abcd"),
               "download: body of the final hop only")
        finally:
            net._once = real


def check_archive_rekey_collision():
    """A stored url that re-canonicalises onto an id already in the archive is
    merged into it, not silently overwritten."""
    with tempfile.TemporaryDirectory() as d:
        new_url = "https://open.firstory.fm/story/abc"
        base = {"category": "c", "source": "S", "title": "T", "published_at": None}
        a = dict(base, id=un.make_id("c", "S", "T", new_url), url=new_url, summary="摘要" * 40,
                 last_seen_at="2026-10-02T00:00:00Z")
        b = dict(base, id=un.make_id("c", "S", "T", "https://open.firstory.me/story/abc"),
                 url="https://open.firstory.me/story/abc", thumbnail="https://x.example/chart.png",
                 last_seen_at="2026-10-03T00:00:00Z")
        store.save_doc(Path(d) / "archive.json", {"items": [a, b]})
        _, arch = un.load_archive(Path(d) / "archive.json")
        r = arch.get(a["id"], {})
        eq((len(arch), r.get("summary") == a["summary"], r.get("thumbnail"), r.get("last_seen_at")),
           (1, True, b["thumbnail"], b["last_seen_at"]), "re-keyed duplicate merged, fields kept")


def check_bridge_whitelist():
    eq(un.bridge("https://rsshub.app/telegram/channel/madchuwin", ""), ("telegram", "https://t.me/s/madchuwin"),
       "rsshub telegram route")
    eq(un.bridge("https://rsshub.app/telegram/channel/..%2Fx", ""), None, "rsshub telegram name validated")
    eq(un.bridge("https://rsshub.app/jike/user/AB-12", ""), ("jike", "https://m.okjike.com/users/AB-12"), "rsshub jike route")
    eq(un.bridge("https://rsshub.app/jike/topic/a%2F..", ""), None, "rsshub jike id validated")


def check_index_within_run():
    """Two items in one run getting the same summary: the second is caught
    (the index is updated as summaries are written, not only at start)."""
    d = Path(tempfile.mkdtemp())
    body = ("研究人員在報告中指出，這項技術可以大幅降低資料中心的耗電量，並預計明年開始量產。"
            "工程師表示，部署這些模型需要大量的運算資源與電力，資料中心正在擴建。"
            "業界預計明年會有更便宜的推理晶片上市，價格可能下降一半以上。")
    items = [{"id": "1" * 40, "url": "https://fs.blog/apple-phone", "title": "蘋果發表新款手機", "feed_content": body,
              "published_at": "2026-10-02T00:00:00Z"},
             {"id": "2" * 40, "url": "https://x.substack.com/p/typhoon-landing", "title": "颱風明天登陸",
              "feed_content": body, "published_at": "2026-10-01T00:00:00Z"}]
    summarize.run(d / "a.json", {"items": items}, 0, do_backfill=False, translate=False)
    eq([bool(it["summary"].strip()) for it in items], [True, False], "same summary, other title -> blank, in one run")


def check_refusal_is_not_retried():
    """A refused address is turned away before the session's Retry policy can
    back off and try again."""
    calls = []
    real = net.public_addrs
    net.public_addrs = lambda host, port: calls.append(host) or []
    try:
        net.get("http://internal.example/", 5, retries=3)
    finally:
        net.public_addrs = real
    eq(len(calls), 1, "one address check, no retries")


def check_douban_waits_for_a_copy():
    """A douban mark is blanked only when this process holds its feed copy and
    the copy has no note. Without a copy (media's offline run, a later
    run) it stays pending, so the next collect can still deliver it."""
    d = Path(tempfile.mkdtemp())
    f = d / "a.json"
    db = "https://movie.douban.com/subject/1/"
    f.write_text(json.dumps({"items": [{"id": "a" * 40, "url": db, "title": "看过 X"}]}))
    summarize.run(f, store.load_doc(f), 0, offline_only=True, do_backfill=False, translate=False)
    summarize.run(f, store.load_doc(f), 0, do_backfill=False, translate=False)
    eq("summary" in json.loads(f.read_text())["items"][0], False, "no copy: still pending after offline and plain runs")
    arch = {}
    raw = lambda content: un.Raw("c", "S", "看过 X", db, datetime.now(timezone.utc), content=content)
    un.ingest(arch, [raw("")], datetime.now(timezone.utc))
    rec = next(iter(arch.values()))
    eq(rec.get("feed_content"), "", "feed listed it with no text: an empty copy is still a copy")
    un.ingest(arch, [raw("备注: 好看")], datetime.now(timezone.utc))
    eq(rec.get("feed_content"), "备注: 好看", "a later copy with text replaces the empty one")
    summarize.run(f, {"items": [rec]}, 0, do_backfill=False, translate=False)
    eq(json.loads(f.read_text())["items"][0].get("summary"), "好看", "the note arrives via the next collect")


def check_podcast_blank_is_saved():
    """The blank reaches archive.json (podcast_asr reports the change)."""
    with tempfile.TemporaryDirectory() as d:
        archive = Path(d) / "archive.json"
        archive.write_text(json.dumps({"items": [{"id": "e" * 40, "title": "t", "source": "s", "category": "c",
                                                  "url": "https://www.xiaoyuzhoufm.com/episode/5e280fb8418a84a0461fd08c"}]}))
        real = net.get
        net.get = lambda url, *a, **k: net.Resp(200, b"<html><body>no audio here</body></html>", {}, url)
        try:
            subs(archive, d)
        finally:
            net.get = real
        eq(json.loads(archive.read_text())["items"][0].get("summary"), store.BLANK_SUMMARY,
           "no audio on the episode page -> blank written to the archive")


def check_bestblogs():
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    body = "".join(f"<p>第{i}篇：人工智慧模型在推理基準上取得進步，研究團隊公布了評測方法與資料集。</p>" for i in range(20))
    page = lambda n, d: (f'<html><head><title>第{n}期 | BestBlogs.dev</title></head><body><nav>NAV</nav>'
                         f'<main><h1>Newsletter {d}</h1>{body}<script>x()</script></main></body></html>')
    dates = {n: (now - timedelta(days=7 * (40 - n))).strftime("%Y-%m-%d") for n in range(1, 41)}
    seen = []

    def fake_get(url, *a, **k):
        seen.append(url)
        n = int(url.rsplit("issue", 1)[1])
        return net.Resp(200, page(n, dates[n]).encode(), {"content-type": "text/html"}, url) if n in dates \
            else net.Resp(404, b"", {}, url)
    real = un.net.get
    un.net.get = fake_get
    try:
        raws = un.fetch_bestblogs({}, now, 60)
        eq([int(r.url.rsplit("issue", 1)[1]) for r in raws], list(range(32, 41)),
           "bestblogs: empty archive takes the latest issues inside retention only")
        eq(len(seen) < 25, True, "bestblogs: latest issue found by bisection, not by walking from 1")
        r = raws[-1]
        eq((r.title, r.category, r.source, un.iso(r.published_at)), ("第40期", "tech", "BestBlogs", dates[40] + "T00:00:00Z"),
           "bestblogs: title, labels, date from the page")
        eq(("<" in r.content, "NAV" in r.content, "x()" in r.content, "第0篇" in r.content), (False, False, False, True),
           "bestblogs: feed copy is plain text without chrome")
        arch = {}
        un.ingest(arch, raws, now)
        seen.clear()
        eq(un.fetch_bestblogs(arch, now, 60), [], "bestblogs: known issues are not emitted again")
        eq(len(seen), un.BESTBLOGS_MISSES, "bestblogs: with issues known, only the next ones are probed")
        dates[41] = now.strftime("%Y-%m-%d")
        eq([r.url for r in un.fetch_bestblogs(arch, now, 60)], [f"{un.BESTBLOGS}/newsletter/issue41"],
           "bestblogs: new issue found")
    finally:
        un.net.get = real
    s = textproc.build(raws[-1].content, "feed")
    eq(bool(s) and "<" not in s, True, "bestblogs: summary has no markup")
    eq(un.canonical_url("https://bestblogs.dev/en/newsletter/issue007/"), f"{un.BESTBLOGS}/newsletter/issue7",
       "bestblogs url canonical")
    eq(summarize.route({"url": f"{un.BESTBLOGS}/newsletter/issue7", "feed_content": "x"}), "feed",
       "bestblogs summarized from its copy")


def check_thepaper_body():
    """澎湃 article pages: the article's own body, as plain text, never another article's."""
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    own = "<p>" + "上海書評這一期討論城市記憶與出版史，作者回顧了三十年來的書店變遷。" * 12 + "</p>"
    other = "<p>" + "推薦閱讀：另一篇完全不同的文章內容，篇幅更長。" * 40 + "</p>"
    data = {"props": {"pageProps": {"detailData": {"contId": 123456, "content": own, "pubTimeLong": 1760300000000},
                                    "recommend": [{"contId": 999999, "content": other}]}}}
    page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'
    real = un.net.get
    un.net.get = lambda url, *a, **k: net.Resp(200, page.encode(), {"content-type": "text/html"}, url)
    try:
        body, when, _ = un.thepaper_article("123456", now)
    finally:
        un.net.get = real
    eq(("城市記憶" in body, "推薦閱讀" in body, "<p>" in body), (True, False, False), "thepaper: own contId body, plain text")
    eq(when is not None, True, "thepaper: date from the article data")


def check_scraped_sources_switch():
    """BestBlogs and 澎湃 run only when SCRAPED_SOURCES is on (default off)."""
    eq([store.env_flag(v) for v in (None, "", "off", "0", "false", "no", "maybe", "on", "ON", " 1 ", "true", "Yes")],
       [False] * 7 + [True] * 5, "SCRAPED_SOURCES values; unset means off")
    eq([store.env_flag(v, True) for v in (None, "", "off", "on")], [True, True, False, True], "on-by-default flags")
    d = tempfile.mkdtemp()
    calls = []
    real = un.fetch_bestblogs, un.fetch_thepaper
    un.fetch_bestblogs = lambda *a: calls.append("bestblogs") or []
    un.fetch_thepaper = lambda *a: calls.append("thepaper") or []
    try:
        for on in ("off", "on"):
            calls.clear()
            collect(d, "--no-summarize", SCRAPED_SOURCES=on)
            eq(calls, ["bestblogs", "thepaper"] if on == "on" else [], f"scraped sources with the switch {on}")
    finally:
        un.fetch_bestblogs, un.fetch_thepaper = real
    eq(summarize.route({"url": f"{un.THEPAPER}/newsDetail_forward_123456", "feed_content": "x"}), "feed",
       "thepaper summarized from its copy, page never fetched by summarize")
    eq(un.canonical_url("https://bestblogs.dev/zh/newsletter/issue12"), f"{un.BESTBLOGS}/newsletter/issue12",
       "the BestBlogs url rule holds with the switch off too (ids do not change when toggled)")


def check_thepaper_pending_refetched():
    """A 澎湃 item still pending is fetched again (feed copies are not saved);
    one with a summary is not, and is re-emitted only to refresh last_seen_at."""
    listing_page = ('<html><body><a href="/newsDetail_forward_111111">甲</a>'
                    '<a href="/newsDetail_forward_222222">乙</a></body></html>')
    fetched = []

    def fake_get(url, *a, **k):
        if "channel_" in url:
            return net.Resp(200, listing_page.encode(), {}, url)
        fetched.append(url.rsplit("_", 1)[1])
        return net.Resp(200, b"<html></html>", {}, url)
    url = lambda cid: f"{un.THEPAPER}/newsDetail_forward_{cid}"
    archive = {"a": {"url": url("111111"), "summary": "已完成的摘要"}, "b": {"url": url("222222")}}
    real = un.net.get
    un.net.get = fake_get
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            raws = un.fetch_thepaper(archive)
    finally:
        un.net.get = real
    eq((sorted(fetched), sorted(r.url for r in raws)), (["222222"], [url("111111"), url("222222")]),
       "thepaper: pending item refetched, summarised one only re-listed")


def check_transport():
    """The real transport against a local server (the address check is told
    that t.test is public): pinning, no proxy even from env, total deadline,
    byte cap, redirect hops re-checked, POST bodies."""
    import http.server, socket, threading

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def handle(self):
            with contextlib.suppress(OSError):
                super().handle()

        def reply(self, code, body=b"", headers=()):
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/echo":
                self.reply(200, json.dumps(dict(self.headers)).encode())
            elif self.path == "/big":
                self.send_response(200)
                self.end_headers()
                for _ in range(40):
                    self.wfile.write(b"x" * 65536)
            elif self.path == "/drip":
                self.send_response(200)
                self.end_headers()
                for _ in range(40):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.25)
            elif self.path == "/to-private":
                self.reply(302, headers=[("Location", "http://10.0.0.1/x")])
            elif self.path == "/to-echo":
                self.reply(302, headers=[("Location", "/echo")])

        def do_POST(self):
            self.reply(200, self.rfile.read(int(self.headers.get("Content-Length") or 0)))

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen(4)
    proxy.settimeout(0.2)
    hits = []
    base = f"http://t.test:{srv.server_address[1]}"
    real = net.public_addrs
    net.public_addrs = lambda h, p: ["127.0.0.1"] if h == "t.test" else real(h, p)
    purl = f"http://127.0.0.1:{proxy.getsockname()[1]}"
    try:
        with env(http_proxy=purl, HTTP_PROXY=purl, https_proxy=purl, ALL_PROXY=purl, all_proxy=purl):
            r = net.get(base + "/echo", 5)
        with contextlib.suppress(OSError):
            hits.append(proxy.accept())
        eq((r and r.status, r and r.json().get("Host"), hits), (200, base[7:], []),
           "pinned to the checked address, Host kept, env proxy never used")
        eq(net.get(base + "/to-echo", 5).url, base + "/echo", "redirect followed by hand")
        eq(net.get(base + "/to-private", 5), None, "redirect to a private address refused")
        eq(net.request("GET", base + "/big", 5, limit=1_000_000), None, "body over the byte cap refused")
        t0 = time.monotonic()
        eq(net.get(base + "/drip", 2), None, "a dripping server is cut at the deadline")
        eq(time.monotonic() - t0 < 5, True, "total deadline, not a per-read timeout")
        r = net.request("POST", base + "/p", 5, json_body={"q": "中文"})
        eq(r and r.json(), {"q": "中文"}, "POST json body")
    finally:
        net.public_addrs = real
        srv.shutdown()
        proxy.close()


ADVERSARIAL = ("<script a", "<meta a", "<img src=", "<link a", "<title", "<p>", "<code>", "<!--", "<",
               "a-", "![", "[Image", "](", "\n", " ", "推 abc: ", "1.2.3.4 ", "çš„", "x，y，")


def check_no_backtracking():
    """Every parser that reads remote text is linear. Unclosed `<meta a`,
    `<script a`, `<img`, nested `<p>`, runs of spaces... each makes a naive
    regex or tree parser quadratic or worse (hours on an 8 MB page). 200 KB of
    each pattern must take a few seconds at most per parser."""
    parsers = {
        "meta_tags": lambda s: textproc.meta_tags(s, ("og:title",)),
        "page_title": textproc.page_title, "next_data": store.next_data,
        "thumb_candidates": lambda s: summarize.thumb_candidates(s, "https://a.example/"),
        "youtube_channel_id": un.extract_youtube_channel_id, "audio_in_page": ds._audio_in_page,
        "html_to_text": textproc.html_to_text, "from_html": textproc.from_html,
        "sanitize_markup": textproc.sanitize_markup, "clean_markdown": summarize.clean_markdown,
        "clean_ptt": summarize.clean_ptt, "check_scrape": summarize.check_scrape,
        "garbled": textproc.garbled, "find_loops": textproc.find_loops,
    }
    slow = []
    for name, f in parsers.items():
        for unit in ADVERSARIAL:
            s = unit * (200_000 // len(unit))
            t0 = time.monotonic()
            f(s)
            if (dt := time.monotonic() - t0) > 3:
                slow.append(f"{name} {unit!r} {dt:.0f}s")
    eq(slow, [], "remote-text parsers stay linear on adversarial input")
    eq(ds._audio_in_page('<a href="https://x/a.mp3x">x</a> "https://cdn/b.MP3?t=1"'), "https://cdn/b.MP3?t=1",
       "audio url: extension before the query, case-insensitive")
    eq(ds._audio_in_page("url(https://x/a.mp3)"), "", "audio url must end at a delimiter")
    eq(store.next_data('<script type="application/json" ID="__NEXT_DATA__">{"a":1}</script>'), {"a": 1},
       "next_data: attribute case and order")
    eq(textproc.page_title("<title>a &amp;\n b</title>"), "a & b", "page_title from <title>")
    eq(summarize.thumb_candidates('<img src="/a.jpg" width="50"><img src="/b.jpg" width="600">', "https://x/"),
       ["https://x/b.jpg"], "icon-sized <img> skipped")


def check_feed_body_is_data():
    """feedparser treats a str/bytes argument as a file name first, and a str
    url as something to fetch with urllib (no net checks). A feed body that
    is a path or a url must stay data."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "x.xml"
        f.write_text('<rss><channel><item><title>LOCAL</title><link>https://a/b</link>'
                     '<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></channel></rss>')
        eq(store.parse_feed(str(f).encode()).entries, [], "a body naming a local file is not opened")
        eq(store.parse_feed(f"file://{f}").entries, [], "a body that is a file:// url is not fetched")
        feed = {"category": "c", "title": "t", "url": "https://a/feed"}
        eq(un.parse_rss(str(f).encode(), feed), [], "parse_rss: body path stays data")
        eq([r.title for r in un.parse_rss(f.read_bytes(), feed)], ["LOCAL"], "parse_rss still parses a feed")


def check_backfill_once_per_rules():
    """The full backfill (QC, re-strip, thumbnails) runs when rules.json,
    textproc.py or summarize.py changed since the last full pass; otherwise a
    run only retries missing translations."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.json"
        chrome = {"id": "e" * 40, "url": "https://x.example/a", "title": "t", "summary": "Sign up Sign in Open in app"}
        summarize.run(f, {"items": [dict(chrome)]}, 0, translate=False)
        doc = store.load_doc(f)
        eq(doc.get("rules_hash"), store.rules_hash(), "full pass records the rules hash")
        eq("summary" in doc["items"][0], False, "first pass applies QC")
        doc["items"].append(dict(chrome, id="d" * 40))
        summarize.run(f, doc, 0, translate=False)
        eq("summary" in store.load_doc(f)["items"][1], True, "unchanged rules: no second full pass")
        doc = store.load_doc(f)
        doc["rules_hash"] = "stale"
        summarize.run(f, doc, 0, translate=False)
        eq("summary" in store.load_doc(f)["items"][1], False, "changed rules: full pass again")
        eq(store.load_doc(f).get("rules_hash"), store.rules_hash(), "hash refreshed")
        collect(d)
        eq(store.load_doc(f).get("rules_hash"), store.rules_hash(), "collect keeps the rules hash")


def check_rules_file():
    """rules.json: three sections, compilable patterns, consistent site tables."""
    r = store.rules()
    eq(sorted(k for k in r if not k.startswith("_")), ["boilerplate", "qc_chrome", "sites", "version"], "sections")
    sites = r["sites"]
    bad = [p for p in r["qc_chrome"]["patterns"] if not isinstance(p, str)]
    eq(bad, [], "qc_chrome patterns are strings")
    for rule in r["boilerplate"]["rules"]:
        if rule.get("pattern"):
            re.compile(rule["pattern"])
    hosts = [h for k, v in sites.items() if k.endswith(("hosts", "domains", "skip")) and isinstance(v, list) for h in v]
    eq([h for h in hosts if h != h.lower() or "/" in h and not h.startswith("http")], [], "hosts lower-case, no paths")
    clash = set(sites["feed_first_hosts"]) & set(sites["skip_hosts"])
    eq(clash, set(), "no host is both feed-first and skipped")
    eq(list(sites["origin_fixups"]), [k.casefold() for k in sites["origin_fixups"]], "origin_fixups keyed by casefolded source")


def check_cookies_rejected():
    """Only yt-dlp's refusal of the account ends the run; hints for age-gated
    or members-only videos mention --cookies too and must not."""
    rej = ds.cookies_rejected
    eq([rej(x) for x in (
        "WARNING: [youtube] The provided YouTube account cookies are no longer valid. They have likely been rotated",
        "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies",
        "ERROR: [youtube] abc: Sign in to confirm your age. Use --cookies-from-browser or --cookies",
        "ERROR: [youtube] abc: Join this channel to get access to members-only content. Use --cookies",
        "")], [True, True, False, False, False], "cookies rejection is matched narrowly")


def check_subtitle_from_info_json():
    """One extraction per video: the subtitle comes from the probe's info JSON;
    the url is tried again only when that fails for another reason."""
    calls = []
    with tempfile.TemporaryDirectory() as d:
        out_dir, info = Path(d) / "subs", Path(d) / "info.json"
        out_dir.mkdir()
        info.write_text("{}")
        item = "a" * 40

        def fake(results):
            def run(cmd, timeout):
                calls.append(cmd)
                rc, write = results.pop(0)
                if write:
                    (out_dir / f"{item}.en.en.vtt").write_text("WEBVTT")
                return rc, "", "ERROR: other" if rc else ""
            return run
        real = ds.run_cmd
        try:
            ds.run_cmd = fake([(0, True)])
            ds.fetch_track(None, "https://youtu.be/x", out_dir, item, "en", True, "en", info, 5)
            eq((len(calls), "--load-info-json" in calls[0], calls[0][-2:] != ["--", "https://youtu.be/x"],
                "--sleep-interval" in calls[0]), (1, True, True, False), "subtitle from the info JSON, no sleep")
            calls.clear()
            (out_dir / f"{item}.en.en.vtt").unlink()
            ds.run_cmd = fake([(1, False), (0, True)])
            ds.fetch_track(None, "https://youtu.be/x", out_dir, item, "en", True, "en", info, 5)
            eq((len(calls), calls[1][-2:]), (2, ["--", "https://youtu.be/x"]), "failed info-json run retried from the url")
        finally:
            ds.run_cmd = real


def check_parallel_fetch():
    """Network items: several hosts at once, one request at a time per host,
    the fetch cap counted in date order, results written on the main thread."""
    import threading
    lock, active, peak, per_host, seen = threading.Lock(), Counter(), [0], Counter(), []
    body = "這是一段足夠長的網頁內容，研究人員在報告中指出這項技術可以大幅降低耗電量。" * 8

    def fake_fetch(url, feed, found):
        host = store.host_of(url)
        with lock:
            active[host] += 1
            per_host[host] = max(per_host[host], active[host])
            peak[0] = max(peak[0], sum(active.values()))
            seen.append(url)
        time.sleep(0.05)
        with lock:
            active[host] -= 1
        return summarize.Fetched(body + url, "body")
    items = [{"id": f"{i:040x}", "url": f"https://h{i % 3}.example/p{i}", "title": f"文章 {i}",
              "published_at": f"2026-10-{20 - i:02d}T00:00:00Z"} for i in range(9)]
    real, real_sleep = summarize.fetch_article, summarize.SLEEP
    summarize.fetch_article, summarize.SLEEP = fake_fetch, 0.01
    try:
        with tempfile.TemporaryDirectory() as d:
            r = summarize.run(Path(d) / "a.json", {"items": items}, 7, do_backfill=False, translate=False)
    finally:
        summarize.fetch_article, summarize.SLEEP = real, real_sleep
    eq((max(per_host.values()), peak[0] > 1), (1, True), "one request per host at a time, hosts in parallel")
    eq((r.n["attempted"], len(seen), sorted(seen) == sorted(it["url"] for it in items[:7])), (7, 7, True),
       "fetch cap: the newest 7 items")
    eq(sum(bool(it.get("summary")) for it in items), 7, "every fetched item summarized")


def check_blocked_host_paused():
    """A refusal pauses that host for the run: its other items are not fetched
    (they stay pending), other hosts carry on."""
    seen = []

    def fake_fetch(url, feed, found):
        seen.append(url)
        if "bad." in url:
            return summarize.Fetched(None, "blocked")
        return summarize.Fetched("這是一段足夠長的網頁內容，研究人員在報告中指出這項技術可以大幅降低耗電量。" * 8 + url, "body")
    items = [{"id": f"{i:040x}", "url": f"https://{'bad' if i % 2 else 'ok'}.example/p{i}", "title": f"文章 {i}",
              "published_at": f"2026-10-{20 - i:02d}T00:00:00Z"} for i in range(6)]
    real, real_sleep = summarize.fetch_article, summarize.SLEEP
    summarize.fetch_article, summarize.SLEEP = fake_fetch, 0
    try:
        with tempfile.TemporaryDirectory() as d:
            r = summarize.run(Path(d) / "a.json", {"items": items}, 50, do_backfill=False, translate=False)
    finally:
        summarize.fetch_article, summarize.SLEEP = real, real_sleep
    eq(sum("bad." in u for u in seen), 1, "blocked host: one request, then paused")
    eq((r.paused, r.n["blocked"]), ({"bad.example": 2}, 1), "paused host counts the items it skipped")
    eq([bool(it.get("summary")) for it in items], [True, False] * 3, "other hosts fetched; blocked ones stay pending")


def check_transcript_without_content_is_final():
    """A local transcript that yields nothing (undecodable, or only boilerplate)
    is blanked: the same file would give the same result every run."""
    with tempfile.TemporaryDirectory() as d:
        yid = "d" * 40
        store.configure(data_dir=Path(d))
        (Path(d) / "subtitles").mkdir()
        (Path(d) / "subtitles" / f"{yid}.zh.zh.vtt").write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n" + "é¦é¦é¦é¦çڑ„ه°±و˜¯" * 30 + "\n", encoding="utf-8")
        item = {"id": yid, "url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "v"}
        summarize.run(Path(d) / "a.json", {"items": [item]}, 0, offline_only=True, do_backfill=False,
               translate=False)
    eq(item.get("summary"), store.BLANK_SUMMARY, "undecodable transcript -> blank, not retried forever")


def check_backfill_qc_only():
    """--backfill --qc-only applies summary QC and nothing else."""
    zh = "研究人員在報告中指出，這項技術可以大幅降低資料中心的耗電量，並預計明年開始量產。"
    items = [{"url": "https://x.example/a", "summary": "Sign up Sign in Open in app",
              "thumbnail": "https://example.com/img/logo.png"},
             {"url": "https://x.example/b", "summary": "This is an English summary that is long enough to translate it."},
             {"url": "https://x.example/c", "summary": zh + "小" * 20 + zh}]
    summarize.backfill(items, translate=False, qc_only=True)
    eq(("summary" in items[0], items[0].get("thumbnail"), items[1]["summary"][:4], items[2]["summary"]),
       (False, "https://example.com/img/logo.png", "This", zh + "小" + zh), "QC fixes only: thumbnails and text rules untouched")


def check_tls_verified():
    """A certificate the system does not trust is refused, impersonated or not."""
    import http.server, shutil, ssl, subprocess, threading
    if not shutil.which("openssl"):
        return
    d = Path(tempfile.mkdtemp())
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "k.pem"),
                    "-out", str(d / "c.pem"), "-days", "1", "-subj", "/CN=t.test",
                    "-addext", "subjectAltName=DNS:t.test"], capture_output=True, check=True)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "c.pem", d / "k.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = net.public_addrs
    net.public_addrs = lambda h, p: ["127.0.0.1"] if h == "t.test" else real(h, p)
    try:
        url = f"https://t.test:{srv.server_address[1]}/"
        eq((net.get(url, 5), net.get(url, 5, impersonate=True)), (None, None),
           "untrusted certificate refused (plain and impersonated)")
    finally:
        net.public_addrs = real
        srv.shutdown()



def check_config():
    """One reader of the environment; FALLBACKS parsed strictly; paths from DATA_DIR."""
    with env(DATA_DIR="/x", FALLBACKS="Wayback, bogus", MAX_ITEMS="7", ARCHIVE_DAYS="oops", TRANSLATE="off"):
        c = store.configure()
    eq((c.archive, c.subtitles, c.status_file, c.inbox), (Path("/x/archive.json"), Path("/x/subtitles"),
       Path("/x/status.json"), Path("/x/inbox.json")), "config: every path derives from DATA_DIR")
    eq((c.fallbacks, c.max_items, c.archive_days, c.translate), (frozenset({"wayback"}), 7, 60, False),
       "config: unknown fallbacks dropped, bad numbers fall back to defaults")
    with env(FALLBACKS=None):
        eq(store.configure().fallbacks, frozenset({"reader", "wayback"}), "config: default fallbacks unchanged")
    with env(FALLBACKS=""):
        eq(store.configure().fallbacks, frozenset(), "config: FALLBACKS= turns every fallback off")


def check_fallback_switch_and_log():
    """FALLBACKS decides which third parties see a url; each try is logged as via=... ok|miss."""
    NAME = {"r.jina.ai": "reader", "archive.org": "wayback"}
    calls, real = [], net.get
    net.get = lambda url, *a, **k: (calls.append(url), None)[1]
    try:
        for fb, want in (("", []), ("wayback", ["archive.org"]), ("reader,wayback", ["r.jina.ai", "archive.org"])):
            calls.clear()
            store.configure(fallbacks=frozenset(filter(None, fb.split(","))))
            summarize._tls.buf = []
            summarize.fetch_article("https://example.org/a")
            lines, summarize._tls.buf = summarize._tls.buf, None
            eq([h for h in ("r.jina.ai", "archive.org") if any(h in c for c in calls)], want,
               f"fallbacks {fb!r}: third parties contacted")
            eq([l.strip() for l in lines if "via=" in l], [f"via={NAME[w]} miss" for w in want],
               f"fallbacks {fb!r}: logged")
    finally:
        net.get = real


def check_303_does_not_leak_into_retry():
    """A 303 turns this attempt into a GET; a retry starts again from the original POST."""
    seen, real_once, real_pub = [], net._once, net.public_addrs
    replies = iter([(303, {"location": "/b"}, "/b"), (503, {}, None), (200, {}, None)])

    def once(method, url, ips, deadline, sink, limit, **kw):
        seen.append((method, kw["data"]))
        return next(replies)
    net._once, net.public_addrs = once, lambda h, p: ["93.184.216.34"]
    try:
        net.request("POST", "https://example.org/a", 30, data=b"x", retries=1)
    finally:
        net._once, net.public_addrs = real_once, real_pub
    eq(seen, [("POST", b"x"), ("GET", None), ("POST", b"x")], "303 rewrites one attempt only")


def check_large_non_utf8_body():
    """Bodies past the guess prefix still decode right (and quickly)."""
    body = ("中文內容測試，這是一段很長的繁體與简体混合文字。" * 30000).encode("gb18030")
    assert len(body) > net.GUESS_PREFIX
    t = time.monotonic()
    out = net.decode_body(body, {})
    eq((out[:6], time.monotonic() - t < 20), ("中文內容測試", True), "large GBK body decoded from the prefix guess")


def check_cli_subcommands():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "archive.json").write_text(json.dumps({"items": [
            {"id": "f" * 40, "url": "https://x.example/a", "title": "t", "summary": "Sign up Sign in Open in app"}]}))
        with env(DATA_DIR=d):
            eq(summarize.main(["backfill", "--dry-run"]), 0, "cli: backfill --dry-run")
            eq("summary" in store.load_doc(Path(d) / "archive.json")["items"][0], True, "cli: dry run writes nothing")
            eq(summarize.main(["backfill", "--qc-only"]), 0, "cli: backfill --qc-only")
            eq("summary" in store.load_doc(Path(d) / "archive.json")["items"][0], False, "cli: QC refetch applied")
            eq(summarize.main(["mine", "2"]), 0, "cli: mine")
        with env(DATA_DIR=str(Path(d) / "none")), contextlib.redirect_stderr(io.StringIO()):
            eq(summarize.main(["backfill"]), 1, "cli: missing archive is an error")

def check_refactor_contracts():
    """Import reads no environment; chrome is "no body", never "blocked";
    repeated cues merge; one subtitle per id."""
    import subprocess
    code = ("import os, sys; sys.path.insert(0, %r); os.environ['MAX_ITEMS'] = '7'; "
            "import store; print(store.CFG.max_items, store.configure().max_items)") % str(Path(__file__).resolve().parent)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.split()
    eq(out, [str(store.Config().max_items), "7"], "config: import uses defaults, configure() reads the env")
    rx = textproc.chrome_re()
    pat = (store.rules().get("qc_chrome") or {}).get("patterns") or []
    if pat:
        chrome = pat[0] + " " + pat[-1]
        eq(textproc.chrome_only(chrome), True, "chrome only: no body")
        eq(textproc.chrome_only(chrome + " " + "正文內容很長。" * 40), False, "chrome plus a real body: kept")
        eq(textproc.is_junk(chrome), False, "chrome is not a block (would pause the host)")
        eq(bool(rx.search(chrome)), True, "chrome_re matches its own patterns")
    eq(textproc.chrome_only("短文一則。"), False, "a short body without chrome is not chrome")
    vtt = ds.to_vtt([(0, 1, "a"), (1, 2, "a"), (2, 3, "b"), (3, 4, "a")])
    eq(vtt, "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\na\n\n00:00:02.000 --> 00:00:03.000\nb\n\n"
            "00:00:03.000 --> 00:00:04.000\na\n", "vtt: consecutive repeats merge, later repeats stay")
    with tempfile.TemporaryDirectory() as d:
        i = "a" * 40
        for name in (f"{i}.en.en.vtt", f"{i}.en.zh-Hant.vtt", f"{i}.en.zh-Hant-en.vtt", f"{'b' * 40}.en.en.vtt"):
            (Path(d) / name).write_text("WEBVTT\n")
        best = store.pick_subtitle(d, i)
        store.prune_subtitles(d, i)
        eq(sorted(p.name for p in Path(d).glob("*.vtt")), sorted([best.name, f"{'b' * 40}.en.en.vtt"]),
           "prune keeps the best file of that id only")
        eq(store.prune_subtitles(d, "c" * 40), [], "prune with no file deletes nothing")


def check_tag_text_linear():
    html = "<script a" * 200_000 + ">"
    t = time.monotonic()
    store.tag_text(html, "script", {"id": "__NEXT_DATA__"})
    eq(time.monotonic() - t < 2, True, "tag_text: unclosed tags stay linear")
    eq(store.tag_text('<scripts id="__NEXT_DATA__">x</scripts><script id="__NEXT_DATA__">{}</script>',
                      "script", {"id": "__NEXT_DATA__"}), "{}", "tag_text: <scripts> is another tag")
    eq(store.tag_text("<title>A</title>", "title", {}), "A", "tag_text: bare tag")
    eq(store.tag_text('<script a<script id="__NEXT_DATA__">{"x":1}</script>', "script",
                      {"id": "__NEXT_DATA__"}), '{"x":1}', "tag_text: unclosed tag before the real one")


def check_firstory_tag_strip_linear():
    t = time.monotonic()
    ds.HMS_RE.search(__import__("re").sub(r"<[^<>]*+>", " ", "<a" * 200_000))
    eq(time.monotonic() - t < 2, True, "firstory tag strip: unclosed tags stay linear")


def check_thepaper_dates():
    now = un.datetime.now(tz=store.UTC)
    for v in (1760000000000, "1760000000000", "1760000000", 1760000000.0):
        eq(store.iso(un._thepaper_date({"pubTimeLong": v}, now)), "2025-10-09T08:53:20Z", f"thepaper epoch {v!r}: UTC")
    eq(store.iso(un._thepaper_date({"pubTime": "2026-10-12 08:30"}, now)), "2026-10-12T00:30:00Z", "thepaper bare: Beijing")
    eq(store.iso(un._thepaper_date({"pubTime": "2026-10-12T08:30:00+08:00"}, now)), "2026-10-12T00:30:00Z",
       "thepaper offset kept")
    eq(store.iso(un._thepaper_date({"pubTime": "3小时前"}, now)), store.iso(now - un.timedelta(hours=3)),
       "thepaper relative time")


if __name__ == "__main__":
    # Checks run the real scripts, which narrate as they go ("no OPML",
    # "[RETENTION] dropped ..."): that is test scaffolding, not the real run,
    # so it is swallowed; only failures are printed.
    for n in un.OPML_SECRETS:                 # never fetch real feeds from a test
        os.environ.pop(n, None)
    for name, fn in list(globals().items()):
        if name.startswith("check_"):
            store.configure()                 # every check starts from the environment
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    fn()
            except Exception:
                FAILS.append(f"{name} crashed:\n" + traceback.format_exc(limit=4))
    for f in FAILS:
        print("FAIL:", f)
    print("selftest:", "FAILED" if FAILS else "ok")
    sys.exit(1 if FAILS else 0)
