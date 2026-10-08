"""Thumbnail selection: keep charts, maps and topical images; reject photos,
stock/wire images, screenshots and site furniture. Judged from the url alone.

Rule order matters: explicit deny beats the trusted-host allowance.
"""

from __future__ import annotations

import html as html_mod
import re
from urllib.parse import urljoin

from common import host_in

# Specific files the filename heuristics cannot judge (sponsor logos, reused
# covers). Compared lower-cased and without query string.
DENY = frozenset(u.lower() for u in (
    "https://image.blocktempo.com/2025/03/foresight-ventures.png",
    "https://image.blocktempo.com/2025/03/foresight-news.png",
    "https://image.blocktempo.com/2026/04/mexc-logo-v2.png",
    "https://ibw.bwnet.com.tw/file/img/smart-white.png",
    "https://storage.ghost.io/c/30/f5/30f5b1bb-84ee-4c26-b446-fb9a5e512994"
    "/content/images/size/w30/2025/08/ghostop.png",
    "https://storage.ghost.io/c/a0/4c/a04c7225-d919-4d78-9b7c-a3fdd071349b"
    "/content/images/size/w1200/2024/01/1500x500-1.jpeg",
    "https://ritholtz.com/wp-content/uploads/2016/01/barry02-1-1.png",
    "https://ritholtz.com/wp-content/uploads/2025/05/mib_2025.png",
    "https://maxjamesread.com/wp-content/uploads/2021/04/S__46555156-scaled.jpg",
    "https://gmhjohnny.wordpress.com/wp-content/uploads/2020/09/cropped-j102.png",
    "https://gmhjohnny.wordpress.com/wp-content/uploads/2020/09/j102.png",
))
DENY_RE = re.compile(r"^https?://kottke\.org/.*/images/\d{4}/logo-colors/", re.I)
SKIP_HOSTS = ("finance.technews.tw", "douban.com", "granitefirm.com")   # never take a thumbnail from these (incl. subdomains)
# Hosts that serve only article uploads with meaningless filenames (Blogger).
TRUSTED_HOSTS = ("blogger.googleusercontent.com", "bp.blogspot.com")
TRUSTED_PATH_RE = re.compile(r"^https?://lh\d+\.googleusercontent\.com/blogger_img_proxy/", re.I)
# Google image-proxy rendition suffix; strip it to get the underlying image.
GOOGLE_SIZE_RE = re.compile(r"=w\d+-h\d+-p-k-no-nu$", re.I)

IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|avif|svg)(?:$|[?#])", re.I)
ASSET_DIR_RE = re.compile(r"/(?:themes?|wp-includes|plugins?|icons?|css|skin|sprites?|emoji|ui)/", re.I)
STOCK_RE = re.compile(
    r"shutterstock[_-]?\d*|istock(?:photo)?|gettyimages?[-_]?\d*|gyi\d{6,}"
    r"|depositphotos|adobestock|dreamstime|alamy|123rf|bigstock|stockphoto|unsplash"
    r"|photo-\d{10,}-[0-9a-f]{8,}|pexels(?:-photo)?[-_]?\d*|\bap[-_]?photo\b|associated[-_]press"
    # wire services: Reuters timestamps / RTS ids, AFP, EPA
    r"|\d{4}-?\d{2}-?\d{2}t\d{6}z|\brt[sxr][a-z0-9]{5,}|\bafp[-_]?\d{6,}|\bepa[-_]?(?:efe[-_]?)?\d{6,}",
    re.I)
TEMPLATE_RE = re.compile(
    r"og[-_]?image|og[-_]?default|social[-_]?(?:card|share|image|preview)"
    r"|twitter[-_]?card|share[-_]?(?:image|card)|card[-_]?bg"
    r"|cover[-_]?template|template[-_]?cover|[-_]template\b|^template"
    r"|(?:^|[-_])default(?:[-_](?:image|thumb|cover|banner))?(?:[-_]|$)"   # not mqdefault.jpg
    r"|placeholder|fallback|no[-_]?image|dummy|generic[-_]?(?:image|cover)"
    r"|\blogo\b|wordmark|favicon|avatar|profile[-_]?pic|headshot|portrait[-_]?shot"
    r"|\bbanner\b|\bheader[-_]?(?:image|bg)?\b|hero[-_]?(?:image|bg)"
    r"|watermark|spacer|pixel|blank|transparent|1x1|use[-_]this(?:[-_]|$)"
    # screenshots, demos, collages
    r"|screen[-_ ]?shot|screencap|scrn|\bcapture\b|\bdemo\b|\bpreview\b|\bmockup\b|\bui[-_]"
    r"|collage|montage|grid[-_]?of|poster[-_]?(?:grid|collage|set)"
    r"|line[-_]?up\b|\bcombo\b|side[-_]by[-_]side[-_]?photos?",
    re.I)

_w = lambda s: frozenset(s.split())
CHART = _w("""axis axes chart charts graph graphs plot plotted map maps mapped mapping
diagram schematic figure fig table matrix index indices ratio ratios rate rates share
shares percent percentage pct scale scaled distribution breakdown composition split
spread trend trends trending trajectory curve curves growth decline change delta monitor
monitoring tracker tracking dashboard scorecard comparison compare compared versus vs flip
gap gaps timeline history historical forecast projection projected outlook heatmap treemap
sankey waterfall scatter histogram bubble radar donut quarterly annual monthly yearly ytd
yoy qoq cagr bloomberg reuters-graphics ft economist statista ourworldindata visualcapitalist""")
PHOTO = _w("""photo photograph photographed picture pic image img shot shots snapshot close
closeup up view viewing views viewed angle aerial overhead portrait portraits standing
sitting seated walking holding wearing smiling posing poses posed looking facing gesturing
speaking talking waving exterior interior facade storefront skyline streetscape landscape
location located site situated near outside inside man woman men women people person crowd
worker workers employee background backdrop foreground blurred bokeh attends attending
arrives arriving during ceremony conference press generic stock illustrative illustration
decorative""")
CHROME = _w("""loader spinner throbber skeleton holder empty icon icons sprite sprites qrcode
opengraph ogimg arrow arrows divider separator overlay texture btn button nav navbar badge
ribbon follow subscribe scrolling""")
GRAPHIC = _w("""line lines rule hr dot dots bar bars bg background spacer pixel sep divider
shadow mask gradient texture blank dummy placeholder empty none null loader spinner""")
LAYOUT = _w("""content contents main top bottom left right inner outer wrap wrapper box block
section area panel col row grid cell border edge corner middle center centre side foot base
common default style theme layout title text head header footer heading caption label item
list nav menu sub""")


def skipped_host(page_url: str) -> bool:
    return host_in(page_url, SKIP_HOSTS)


def usable(url: str) -> tuple[bool, str]:
    """(verdict, reason) -- the reason is logged so rules stay tunable."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False, "not an absolute url"
    bare = url.split("?", 1)[0].split("#", 1)[0].strip().lower()
    if bare in DENY or DENY_RE.match(bare):
        return False, "explicitly denied url"
    if bare.endswith(".svg"):
        return False, "svg (usually a logo or icon)"
    if host_in(url, TRUSTED_HOSTS) or TRUSTED_PATH_RE.match(url):
        return True, "trusted image host"
    path = url.split("?", 1)[0].split("#", 1)[0]
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if not IMAGE_EXT_RE.search(path):
        return False, "no image extension"
    stem = re.sub(r"\.[a-z0-9]+$", "", name, flags=re.I)
    stem = re.sub(r"[-_]\d{2,4}x\d{2,4}$", "", stem)          # name-1024x576
    stem = re.sub(r"[-_@][123]x$", "", stem, flags=re.I)       # retina suffix
    if len(stem) < 3:
        return False, "filename too short to judge"
    if ASSET_DIR_RE.search(path):
        return False, "served from an asset/theme directory"
    if STOCK_RE.search(f"{stem} {url}"):
        return False, "stock / wire-service filename"
    if TEMPLATE_RE.search(stem):
        return False, "template / furniture / screenshot"
    tokens = [t for t in re.split(r"[^a-z0-9]+", stem.lower()) if t]
    toks = set(tokens)
    if not tokens:
        return False, "no readable filename"
    if len(tokens) == 1 and (len(tokens[0]) >= 16 or tokens[0].isdigit()):
        return False, "opaque id / hash filename"
    if CHART & toks:
        return True, "chart vocabulary in filename"
    if len(tokens) <= 4 and CHROME & toks:
        return False, f"interface chrome ({', '.join(sorted(CHROME & toks))})"
    if toks <= (GRAPHIC | LAYOUT) and GRAPHIC & toks:
        return False, f"decoration filename ({'-'.join(tokens)})"
    if PHOTO & toks:
        return False, f"photographic wording ({', '.join(sorted(PHOTO & toks))})"
    if len(tokens) >= 5:
        return False, f"descriptive phrase ({len(tokens)} tokens)"
    return True, "short topical filename"


META_IMAGE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image(?::url)?|twitter:image(?::src)?)["']"""
    r"""[^>]+content\s*=\s*["']([^"']+)["']"""
    r"""|<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]+(?:property|name)\s*=\s*"""
    r"""["'](?:og:image(?::url)?|twitter:image(?::src)?)["']""", re.I)
BODY_IMG_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
IMG_DIM_RE = re.compile(r"""\b(?:width|height)\s*=\s*["']?(\d+)""", re.I)


def candidates(html: str, page_url: str, limit: int = 24) -> list[str]:
    """Image urls worth judging, in order: og:image / twitter:image, then body
    <img>s that are not declared icon-sized. Absolute, unescaped, de-duplicated."""
    raws = [m.group(1) or m.group(2) for m in META_IMAGE_RE.finditer((html or "")[:60000])]
    for m in BODY_IMG_RE.finditer(html or ""):
        dims = [int(d) for d in IMG_DIM_RE.findall(html[m.start():m.end() + 120])]
        if not (dims and max(dims) < 200):         # icon or tracking pixel
            raws.append(m.group(1))
        if len(raws) > limit:
            break
    out = []
    for raw in raws:
        url = GOOGLE_SIZE_RE.sub("", urljoin(page_url, html_mod.unescape((raw or "").strip())))
        if url and url not in out:
            out.append(url)
    return out


def pick(urls, page_url: str, where: str = "page") -> str | None:
    """First usable url among `urls` (already in preference order)."""
    if skipped_host(page_url):
        return None
    for url in urls or ():
        ok, reason = usable(url)
        if ok:
            print(f"    thumbnail ({where}): {url}  [{reason}]")
            return url
    return None


def extract(html: str, page_url: str) -> str | None:
    """First usable image of a page: og:image / twitter:image, then body <img>s."""
    if not html or skipped_host(page_url):
        return None
    return pick(candidates(html, page_url), page_url)


def still_valid(item: dict) -> tuple[bool, str]:
    if skipped_host(item.get("url") or ""):
        return False, "host opted out"
    return usable(item["thumbnail"])
