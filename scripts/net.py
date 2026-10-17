"""The one HTTP transport (libcurl through curl_cffi) and body decoding.
Imports no other module of this project."""

from __future__ import annotations

import codecs
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import charset_normalizer
from curl_cffi import CurlOpt
from curl_cffi import requests as curl_requests

# ---- HTTP -------------------------------------------------------------------
# Every url we fetch comes from third-party feeds, and whatever a page returns
# ends up in a public commit. One transport (libcurl through curl_cffi) for
# pages, feeds, podcast audio and translation APIs:
#   * http(s) only, no credentials in the url, no proxy (not even from env);
#   * every hop resolved once, refused unless every address is public, and the
#     connection pinned to exactly those addresses (CURLOPT_RESOLVE), so DNS
#     rebinding has no window;
#   * redirects followed by hand, each hop checked again;
#   * a total deadline per request (not a per-read timeout: a server dripping
#     one byte at a time cannot hold a run) and a byte cap while receiving.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
IMPERSONATE = "chrome"
MAX_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECT = (301, 302, 303, 307, 308)
RETRY_STATUS = (429, 500, 502, 503, 504)
CONNECT_TIMEOUT = 10
# Statuses that mean "this client is refused" (the browser fingerprint may pass).
BLOCK_STATUS = frozenset((401, 403, 407, 418, 429, 451, 503))
PAGE_LANG = "zh-TW,zh;q=0.9,en;q=0.8"
FEED_LANG = "zh-CN,zh;q=0.9,en;q=0.8"
PAGE_HEADERS = {
    "User-Agent": UA, "Accept-Language": PAGE_LANG,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    # Akamai stalls requests whose header set doesn't look like a navigation.
    "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document", "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="126", "Not:A-Brand";v="24", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
}
FEED_HEADERS = {
    "User-Agent": UA, "Accept-Language": FEED_LANG,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
}
API_HEADERS = {"User-Agent": UA, "Accept": "application/json, */*;q=0.5"}


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


def _declared(content: bytes, headers: dict) -> list[str]:
    """Charsets the response declares, in order of authority: the
    Content-Type header, then <meta>, then <?xml?>. UTF-8 is left out (valid
    UTF-8 is tried before any label anyway); unknown names are dropped."""
    ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    head = content[:4096]
    found = [re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", ctype, re.I),
             _META_CHARSET.search(head), _XML_CHARSET.search(head)]
    names = (_codec(m.group(1)) for m in found if m)
    return [n for n in names if n and n != "utf-8"]


def decode_body(content: bytes, headers: dict) -> str:
    """Bytes to text, first rule that applies:
    1. a BOM;
    2. valid UTF-8, whatever the labels say (non-ASCII text in another charset
       is almost never valid UTF-8, while mislabelled UTF-8 pages are common);
    3. a declared charset that decodes strictly, except latin-1/ascii/cp1252,
       which decode anything and are often a server default;
    4. a guess over the body, when it reads as a multibyte charset (the guess
       is shorter than the bytes) or nothing was declared;
    5. the first declared charset (else UTF-8), errors replaced."""
    for bom, enc in _BOMS:
        if content.startswith(bom):
            return content[len(bom):].decode(enc, errors="replace")
    declared = _declared(content, headers)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in declared:
        if enc in _WEAK:
            continue
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    guess = _guess(content)
    if guess is not None and (len(guess) < len(content) or not declared):
        return guess
    return content.decode(next(iter(declared), "utf-8"), errors="replace")


GUESS_PREFIX = 512 * 1024


def _guess(content: bytes) -> str | None:
    """charset_normalizer's best reading. The encoding is guessed from the
    first 512 KB and kept only if it decodes the whole body strictly;
    otherwise the whole body is scanned, because a prefix of mostly ASCII
    markup can make a CJK page look like ascii/cp1256."""
    if len(content) > GUESS_PREFIX:
        best = charset_normalizer.from_bytes(content[:GUESS_PREFIX]).best()
        enc = best.encoding if best else None
        if enc and _codec(enc) not in _WEAK:
            try:
                return content.decode(enc)
            except (UnicodeDecodeError, LookupError):
                pass
    best = charset_normalizer.from_bytes(content).best()
    return str(best) if best else None


@dataclass(eq=False)
class Resp:
    status: int
    content: bytes
    headers: dict
    url: str

    @cached_property
    def text(self) -> str:
        """Decoded once (the charset guess over a large body is not cheap)."""
        return decode_body(self.content, self.headers)

    def json(self):
        return json.loads(self.content)


# Address ranges that embed another IPv4 address (NAT64, 6to4, Teredo): a
# "global" v6 address there can reach a private v4 one.
_EMBEDDING = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32"))


def public_addrs(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, IPv4 first, or [] if any is not a
    public unicast address (one private answer is enough to refuse)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    out = []
    for *_, addr in infos:
        ip = ipaddress.ip_address(addr[0].split("%", 1)[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast or (ip.version == 6 and any(ip in n for n in _EMBEDDING)):
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


class _TooLarge(Exception):
    pass


def _once(method, url, ips, deadline, sink, limit, *, headers, impersonate, data):
    """One request to `url`, connected only to `ips`. Returns (status, headers,
    location); the body goes to `sink`. Raises on transport errors."""
    host, port = _target(url)
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("deadline")
    pin = f"{host}:{port}:" + ",".join(f"[{i}]" if ":" in i else i for i in ips)
    received = 0

    def take(chunk):
        nonlocal received
        received += len(chunk)
        if received > limit or time.monotonic() > deadline:
            raise _TooLarge()                   # aborts the transfer (CURL_WRITEFUNC_ERROR)
        sink(chunk)
    opts = {CurlOpt.RESOLVE: [pin], CurlOpt.PROXY: "", CurlOpt.NOPROXY: "*",
            CurlOpt.PROTOCOLS_STR: "http,https", CurlOpt.REDIR_PROTOCOLS_STR: "http,https",
            CurlOpt.MAXFILESIZE_LARGE: limit}
    with curl_requests.Session(curl_options=opts, trust_env=False, use_thread_local_curl=False) as s:
        r = s.request(method, url, headers=headers, data=data, allow_redirects=False,
                      timeout=(min(CONNECT_TIMEOUT, left), max(left - min(CONNECT_TIMEOUT, left), 0.001)),
                      impersonate=IMPERSONATE if impersonate else None,
                      default_headers=bool(impersonate), content_callback=take)
        hdrs = {k: v for k, v in r.headers.items()}
        return r.status_code, hdrs, hdrs.get("location") or hdrs.get("Location")


_CREDENTIALS = frozenset(("authorization", "proxy-authorization", "cookie"))


def _origin(url: str) -> tuple:
    u = urlparse(url)
    return u.scheme.lower(), (u.hostname or "").lower(), u.port


def _fetch(method, url, timeout, sink, limit, *, headers, impersonate=False, data=None,
           retries=0, retry_status=RETRY_STATUS):
    """Follow redirects by hand (each hop checked and pinned); retry transport
    failures and `retry_status` with backoff. Returns (status, headers, final
    url) or None when the url is unsafe, too large, too slow or unreachable.
    `sink(None)` resets a partial body before a retry or a redirect."""
    deadline = time.monotonic() + (sum(timeout) if isinstance(timeout, tuple) else timeout)
    attempt = 0
    while True:
        cur, meth, body, hdrs_out = url, method, data, headers   # a redirect rewrites this attempt only
        try:
            for _ in range(MAX_REDIRECTS + 1):
                t = _target(cur)
                ips = public_addrs(*t) if t else []
                if not ips:                     # refused before any connection: never retried
                    return None
                sink(None)
                status, hdrs, loc = _once(meth, cur, ips, deadline, sink, limit,
                                          headers=hdrs_out, impersonate=impersonate, data=body)
                if status in REDIRECT and loc:
                    nxt = urljoin(cur, loc)
                    if _origin(nxt) != _origin(cur):    # credentials never follow to another origin
                        hdrs_out = {k: v for k, v in hdrs_out.items() if k.lower() not in _CREDENTIALS}
                    cur = nxt
                    if status == 303 or (status in (301, 302) and meth == "POST"):
                        meth, body = "GET", None
                    continue
                break
            else:
                return None
        except (_TooLarge, TimeoutError):
            return None
        except Exception as e:                  # curl/transport failure (a sink abort surfaces here too)
            if _TooLarge in {type(x) for x in (e, e.__cause__, e.__context__)} \
                    or time.monotonic() >= deadline:
                return None
            status = None
        if status is not None and (status not in retry_status or attempt >= retries):
            return status, hdrs, cur
        if attempt >= retries:
            return None
        attempt += 1
        wait = 0.8 * 2 ** (attempt - 1)
        if status is not None:
            with_retry_after = (hdrs.get("retry-after") or hdrs.get("Retry-After") or "").strip()
            if with_retry_after.isdigit():
                wait = min(float(with_retry_after), 30.0)
        if time.monotonic() + wait >= deadline:
            return (status, hdrs, cur) if status is not None else None
        time.sleep(wait)


def request(method: str, url: str, timeout, *, headers: dict | None = None, params: dict | None = None,
            data: bytes | str | None = None, json_body=None, impersonate=False, lang: str | None = None,
            retries: int = 0, retry_status=RETRY_STATUS, limit: int = MAX_BYTES) -> Resp | None:
    """One HTTP exchange through the guarded transport, or None (unsafe url,
    too large, past `timeout` seconds in total, or failed)."""
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    h = dict(headers if headers is not None else PAGE_HEADERS)
    if impersonate:                             # the fingerprint brings its own header set
        h = {k: v for k, v in h.items() if k.lower() in ("authorization", "content-type")}
        h["Accept-Language"] = lang or "en"
    elif lang:
        h["Accept-Language"] = lang
    if json_body is not None:
        data, h["Content-Type"] = json.dumps(json_body), "application/json"
    buf = bytearray()
    got = _fetch(method, url, timeout, lambda c: buf.clear() if c is None else buf.extend(c), limit,
                 headers=h, impersonate=impersonate, data=data, retries=retries, retry_status=retry_status)
    return None if got is None else Resp(got[0], bytes(buf), got[1], got[2])


def get(url: str, timeout, *, impersonate=False, lang: str | None = None, headers: dict | None = None,
        retries: int = 0, retry_status=RETRY_STATUS) -> Resp | None:
    """GET a page (at most MAX_BYTES, `timeout` seconds in total), or None."""
    return request("GET", url, timeout, headers=headers, impersonate=impersonate, lang=lang,
                   retries=retries, retry_status=retry_status)


def download(url: str, dest, seconds: float, max_bytes: int) -> bool:
    """Stream a 200 response into `dest` under the same checks as get();
    False (and no file) on any failure, over `max_bytes`, or past `seconds`."""
    dest = Path(dest)
    try:
        with open(dest, "wb") as f:
            def sink(chunk):
                if chunk is None:
                    f.seek(0)
                    f.truncate()
                else:
                    f.write(chunk)
            got = _fetch("GET", url, seconds, sink, max_bytes, headers={"User-Agent": UA}, retries=1)
    except OSError:
        got = None
    if not got or got[0] != 200:
        dest.unlink(missing_ok=True)
        return False
    return True


