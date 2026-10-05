"""Shared primitives: archive I/O, host matching, HTTP, item sentinels."""

from __future__ import annotations

import codecs
import contextlib
import contextvars
import ipaddress
import json
import os
import re
import socket
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import charset_normalizer
import urllib3.util.connection

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from curl_cffi import CurlOpt, requests as curl_requests
except Exception:                      # optional; comes with yt-dlp[curl-cffi]
    curl_requests = None

# ---- summary sentinels ------------------------------------------------------
# key missing      -> pending, retried every run
# BLANK_SUMMARY    -> deliberately empty (douban mark, no speech); never retried
# GONE_SUMMARY     -> permanent placeholder (404/410, no archive); never retried
FALLBACK_MARK = "↛"
BLANK_SUMMARY = " "
GONE_SUMMARY = "無法取得頁面內容（原始頁面已移除，且無存檔）" + FALLBACK_MARK
PLACEHOLDER_PREFIX = "無法取得頁面內容"


ID_RE = re.compile(r"[0-9a-f]{40}")          # sha1 from update_news.make_id; names files
LANG_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*")


def valid_id(item_id) -> bool:
    return isinstance(item_id, str) and bool(ID_RE.fullmatch(item_id))


def safe_lang(code, default="und") -> str:
    """A language code fit for a filename; anything else becomes `default`."""
    code = str(code or "").strip()
    return code if LANG_RE.fullmatch(code) and len(code) <= 35 else default


def is_pending(item) -> bool:
    return isinstance(item, dict) and bool(item.get("url")) and not item.get("summary")


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
    doc["total_items"] = len(doc["items"])
    write_atomic(path, doc)


# ---- HTTP -------------------------------------------------------------------
# Every url we fetch comes from third-party feeds, and whatever a page returns
# ends up in a public commit. So: http(s) only, public addresses only (checked
# at connect time on every redirect hop), no env proxies, bounded body size.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
IMPERSONATE = os.environ.get("CURL_IMPERSONATE", "chrome")
MAX_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5


_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_META_CHARSET = re.compile(rb"""<meta[^>]{0,200}?charset\s*=\s*["']?\s*([\w.:-]{2,40})""", re.I)
_WEAK = {codecs.lookup(n).name for n in ("latin-1", "ascii", "cp1252")}     # often a server default, not a fact
_XML_CHARSET = re.compile(rb"""^\s*<\?xml[^>]{0,200}?encoding\s*=\s*["']([\w.:-]{2,40})""", re.I)


def _codec(name) -> str | None:
    try:
        return codecs.lookup((name.decode("ascii", "ignore") if isinstance(name, bytes) else name)
                             .strip()).name
    except (LookupError, AttributeError):
        return None


def decode_body(content: bytes, headers: dict) -> str:
    """Bytes to text: BOM, valid UTF-8, declared charset (header, then
    <meta>/<?xml?>), then a guess over the whole body.

    Valid UTF-8 wins over any label: non-ASCII text in another charset is
    almost never valid UTF-8, while mislabelled UTF-8 pages are common. The
    old path read the header from a plain dict with a case-sensitive key (so
    never) and guessed from a truncated 200 KB prefix, which called Chinese
    pages ascii/cp1256 and stored mojibake."""
    for bom, enc in _BOMS:
        if content.startswith(bom):
            return content[len(bom):].decode(enc, errors="replace")
    ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    m = re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", ctype, re.I)
    head = content[:4096]
    declared = [_codec(m.group(1)) if m else None]
    declared += [_codec(x.group(1)) for x in (_META_CHARSET.search(head), _XML_CHARSET.search(head)) if x]
    declared = [d for d in declared if d and d != "utf-8"]
    try:
        return content.decode("utf-8")      # valid UTF-8 beats any label
    except UnicodeDecodeError:
        pass
    for enc in declared:                    # latin-1/ascii labels decode anything: a guess first
        if enc not in _WEAK:
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
    best = charset_normalizer.from_bytes(content).best()
    guess = str(best) if best else None
    # A multibyte guess (Big5, GBK, Shift_JIS...) overrides a weak label; among
    # single-byte charsets the label is a better bet than the guess.
    if guess is not None and (len(guess) < len(content) or not declared):
        return guess
    return content.decode(next(iter(declared), "utf-8"), errors="replace")


class Resp(NamedTuple):
    status: int
    content: bytes
    headers: dict
    url: str

    @property
    def text(self) -> str:
        return decode_body(self.content, self.headers)


def public_addrs(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, IPv4 first, or [] if any is not a
    public unicast address (one private answer is enough to refuse)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    out = []
    for fam, *_, addr in infos:
        ip = ipaddress.ip_address(addr[0].split("%", 1)[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast:
            return []
        if str(ip) not in out:
            out.append(str(ip))
    return sorted(out, key=lambda a: ":" in a)


def _target(url: str):
    """(host, port) of an http(s) url without credentials, else None."""
    try:
        p = urlparse(url)
        if p.scheme in ("http", "https") and p.hostname and not p.username and not p.password:
            return p.hostname, p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        pass
    return None


def safe_url(url: str) -> bool:
    """http(s) to a host that resolves only to public addresses."""
    t = _target(url)
    return bool(t) and bool(public_addrs(*t))


# DNS rebinding: checking a name and then letting the client resolve it again
# leaves a window in which it can answer 127.0.0.1. Inside get(), every
# connection urllib3 opens is resolved and checked here and made to exactly
# the address that passed; curl is pinned with CURLOPT_RESOLVE.
_GUARD: contextvars.ContextVar[bool] = contextvars.ContextVar("public_only", default=False)
_plain_connect = urllib3.util.connection.create_connection


def _guarded_connect(address, *args, **kw):
    if not _GUARD.get():
        return _plain_connect(address, *args, **kw)
    host, port = address
    err = OSError(f"refused non-public address for {host}")
    for ip in public_addrs(host, port):
        try:
            return _plain_connect((ip, port), *args, **kw)
        except OSError as e:
            err = e
    raise err


urllib3.util.connection.create_connection = _guarded_connect


def make_session(retries: int, headers: dict, *, read_retry=True,
                 status=(429, 500, 502, 503, 504), pool=16) -> requests.Session:
    s = requests.Session()
    s.trust_env = False        # an env proxy would resolve the host itself, past the address check
    retry = Retry(total=retries, connect=retries, redirect=0,
                  read=retries if read_retry else False,
                  status=retries if status else 0, status_forcelist=list(status),
                  backoff_factor=0.8 if status else 0.2,
                  allowed_methods=frozenset(["GET", "POST"]),
                  respect_retry_after_header=bool(status), raise_on_status=False)
    ad = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
    s.mount("http://", ad)
    s.mount("https://", ad)
    s.headers.update(headers)
    return s


def _read(r) -> bytes | None:
    if int(r.headers.get("Content-Length") or 0) > MAX_BYTES:
        return None
    buf = bytearray()
    for chunk in r.iter_content(65536):
        buf += chunk
        if len(buf) > MAX_BYTES:
            return None
    return bytes(buf)


_default = requests.Session()
_default.trust_env = False


@contextlib.contextmanager
def _open(url, timeout, session, impersonate, lang):
    if impersonate:
        host, port = _target(url)
        ips = public_addrs(host, port)
        if not ips:
            raise OSError("non-public address")
        pin = [f"{host}:{port}:{','.join(f'[{i}]' if ':' in i else i for i in ips)}"]
        with curl_requests.Session(curl_options={CurlOpt.RESOLVE: pin}) as cs:
            r = cs.get(url, timeout=timeout, impersonate=IMPERSONATE, stream=True,
                       headers={"Accept-Language": lang}, allow_redirects=False)
            try:
                yield r
            finally:
                r.close()
            return
    token = _GUARD.set(True)
    try:
        r = (session or _default).get(url, timeout=timeout, stream=True, allow_redirects=False)
        try:
            yield r
        finally:
            r.close()
    finally:
        _GUARD.reset(token)


def get(url: str, timeout, session: requests.Session | None = None,
        impersonate=False, lang="en") -> Resp | None:
    """GET with redirects followed by hand (each hop re-checked, each
    connection pinned to a checked address), or None when the url is unsafe,
    the body too large, the transport fails, or (with impersonate) curl_cffi
    is missing."""
    if impersonate and curl_requests is None:
        return None
    for _ in range(MAX_REDIRECTS + 1):
        if not _target(url):
            return None
        try:
            with _open(url, timeout, session, impersonate, lang) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                body = _read(r)
                return None if body is None else Resp(r.status_code, body, dict(r.headers), url)
        except Exception:
            return None
    return None
