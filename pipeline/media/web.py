"""Pictures from the live web pages research cited, for every recording whose liner notes are done.

First pass of docs/plans/media.md, collect only: nothing here is shown in the app. Every picture is kept as an
`unverified` candidate with where it came from (page, caption, credit, nearby text), for the evidence rules and
manual approval to decide later. The Wayback Machine is not used (it is slow and rate-limited; dead pages wait
for a later pass), nor Wikipedia/Wikidata/MusicBrainz/discographies (portraits come from enrich_wiki).

    python -m pipeline.media.web plan --shards 2           # data/media/web/todo.<k>.jsonl, split by site
    python -m pipeline.media.web run --todo data/media/web/todo.0.jsonl [--workers 6]
    python -m pipeline.media.web index                     # data/media/web/recordings/<id>.json + summary

`plan` lists every page the current research results cite, closest-to-done first: recent-death recordings, then
by the recording citing the page that has the fewest steps left (pipeline.readiness), then release order. Each site
goes to one shard, so the two laptops never hit the same site at once. `run` fetches each page once (>= 1.5 s
between requests to a site, 429/503 cool the site down), lists its pictures, downloads up to 8 per page into
blobs/<ab>/<sha256>.<ext> and writes pages/<key>.json. Rerunning is cheap: pages already done are not fetched
again, but recordings that newly cite them are added, and picture downloads that failed for a passing reason
(5xx, timeouts, cooldowns) are retried, up to three tries each, an hour and then six hours apart.

Safety: every request, robots.txt and each redirect hop included, must be http(s) to a name whose addresses are all
public, and the connection goes to an address that passed that check (PinnedBackend, so DNS can't be switched in
between). Every hop must be allowed by its own site's robots.txt; a robots.txt that can't be read (5xx, 429,
network error) blocks the site for now rather than allowing everything. Bodies are capped after decompression
(gzip/deflate are decoded here, a bounded amount at a time), robots.txt to 512 KB.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import warnings
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpcore
import httpx
from PIL import Image
from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data/media/web"
UA = "classical-radio-family-archive/0.1 (private, non-commercial)"
SKIP_HOSTS = re.compile(r"(^|\.)(archive\.org|archive\.ph|archive\.today|wikipedia\.org|wikidata\.org|wikimedia\.org|"
                        r"musicbrainz\.org|operadis-opera-discography\.org\.uk)$")
SKIP_TYPES = {"wikidata", "wikipedia", "discography", "tags", "sidecar_file"}
BAD_URL = re.compile(r"logo|icon|sprite|avatar|placeholder|badge|pixel|1x1|spacer|blank\.|tracking|banner|button|"
                     r"/ads?/|doubleclick|gravatar|emoji", re.I)
CHROME = re.compile(r"(^|[-_ ])(related|ads?|advert\w*|promo\w*|sidebar|footer|header|masthead|nav\w*|menu|share|"
                    r"social|newsletter|comments?|subscribe|cookie)([-_ ]|$)", re.I)
BODY = re.compile(r"(^|[-_ ])(content|entry|article|post|story|body|main|gallery|slideshow|record)([-_ ]|$)", re.I)
CREDIT = re.compile(r"(©|&copy;|\(c\)|photo(?:graph)?s?(?: by)?\s*:|photo by|credit\s*:|courtesy)\s*([^.;|\n]{2,80})", re.I)
TRANSIENT = re.compile(r"^(http_(429|5\d\d)|error|cooldown)")
IMG_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
PER_PAGE, MAX_LISTED, MAX_BYTES, MIN_SIDE = 8, 15, 8 << 20, 300
PAGE_BYTES, ROBOTS_BYTES = 12 << 20, 512 << 10
MIN_FREE = 3 << 30   # stop saving pictures (a retryable error) when the disk has less free than this
ROBOTS_RETRY = 600   # s before an unreadable robots.txt is asked for again
BACKOFF = (0, 3600, 6 * 3600)  # s to wait before the 2nd and 3rd try of a passing failure (runs may come every 10 min)
Image.MAX_IMAGE_PIXELS = 40_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)  # refuse, don't just warn, above the pixel cap


def key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def shard_of(h: str, n: int) -> int:
    site = ".".join(h.split(".")[-2:])  # www.bbc.co.uk and genome.ch.bbc.co.uk -> same laptop
    return int(hashlib.sha1(site.encode()).hexdigest(), 16) % n


# ------------------------------------------------------------------ plan

def _results() -> dict[str, Path]:
    """{recording: its liner-notes file}; research overrides pilot, as in pipeline.readiness and the importer."""
    out = {}
    for d in ("pilot/results", "research/results"):
        for f in glob.glob(str(ROOT / "data" / d / "*.json")):
            out[os.path.basename(f)[:-5]] = Path(f)
    return out


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def final(doc: dict | None) -> bool:
    """Fetched for good: a final status, or a passing one already tried three times (as pipeline.readiness)."""
    return bool(doc) and (not TRANSIENT.match(doc.get("status") or "") or doc.get("attempts", 0) >= 3)


def _due(t: float | None, attempts: int) -> bool:
    return time.time() - (t or 0) >= BACKOFF[min(max(attempts, 0), 2)]


def _warm(paths: list[str], workers: int = 16) -> int:
    """Read evicted iCloud files ("dataless": each first read downloads it, seconds apiece) in parallel, so the
    passes below don't wait on them one at a time. Returns how many were evicted."""
    def evicted(p: str) -> bool:
        try:
            return bool(getattr(os.stat(p), "st_flags", 0) & 0x40000000)  # SF_DATALESS
        except OSError:
            return False

    def rd(p: str) -> None:
        try:
            with open(p, "rb") as fh:
                fh.read(1)
        except OSError:
            pass

    todo = [p for p in paths if evicted(p)]
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(rd, todo))
    return len(todo)


def plan(shards: int, out: Path) -> None:
    from .. import readiness, urgent
    _warm(glob.glob(str(out / "pages" / "*.json")) + [str(f) for f in _results().values()])
    st = readiness.status()  # {recording: steps still missing}, release order
    pos = {pid: i for i, pid in enumerate(st)}
    dead = {pid: i for i, pid in enumerate(urgent.ids())}

    def rec_rank(pid: str) -> int:
        if pid in dead:  # recent deaths first
            return dead[pid]
        miss = st.get(pid)
        return (1 + (len(miss) if miss is not None else 8)) * 10**6 + pos.get(pid, 10**6 - 1)

    pages: dict[str, dict] = {}
    for pid, f in sorted(_results().items()):
        try:
            doc = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for s in doc.get("sources") or []:
            url = (s.get("url") or "").strip()
            if not url.startswith(("http://", "https://")) or SKIP_HOSTS.search(host(url)) or s.get("type") in SKIP_TYPES:
                continue
            p = pages.setdefault(url, {"url": url, "host": host(url), "rank": 10**9, "recordings": []})
            p["rank"] = min(p["rank"], rec_rank(pid))
            p["recordings"].append({"rec": pid, "source_id": s.get("id"),
                                    **{k: s.get(k) for k in ("type", "date_specific", "title")}})
    out.mkdir(parents=True, exist_ok=True)
    tmps = [out / f"todo.{k}.jsonl.tmp" for k in range(shards)]
    files = [open(t, "w", encoding="utf-8") for t in tmps]
    left = [0] * shards
    for p in sorted(pages.values(), key=lambda p: (p["rank"], p["url"])):
        k = shard_of(p["host"], shards)
        files[k].write(json.dumps(p, ensure_ascii=False) + "\n")
        left[k] += not final(_load(out / "pages" / f"{key(p['url'])}.json"))
    for fh in files:
        fh.close()
    for k, t in enumerate(tmps):  # a run starting now reads a whole old or a whole new list
        t.replace(out / f"todo.{k}.jsonl")
    print(f"{len(pages)} pages on {len({p['host'] for p in pages.values()})} sites -> {shards} shards; "
          f"not fetched yet: {', '.join(f'shard {k} {n}' for k, n in enumerate(left))}")


# ------------------------------------------------------------------ fetching

class Site:
    """Politeness for one host, shared by every thread in this process."""
    spacing = 1.5

    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0.0
        self.cool_until = 0.0


class Robots:
    """One origin's robots.txt: rp once read; while unreadable, rp is None and `status` says why."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rp: RobotFileParser | None = None
        self.status = ""
        self.until = 0.0


SITES: dict[str, Site] = {}
ROBOTS: dict[str, Robots] = {}
SITES_LOCK = threading.Lock()


def site(h: str) -> Site:
    with SITES_LOCK:
        return SITES.setdefault(h, Site())


def resolve(h: str, port: int | None = None) -> tuple[str, list[str]]:
    """("ok", addresses) if the name resolves and every address is on the public internet; ("dns", []) if it
    doesn't resolve; ("private", []) if any address is local, private, reserved or multicast."""
    try:
        infos = socket.getaddrinfo(h, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return "dns", []
    addrs: list[str] = []
    for info in infos:
        a = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return "private", []
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            return "private", []
        if a not in addrs:
            addrs.append(a)
    return ("ok", addrs) if addrs else ("dns", [])


class PinnedBackend(httpcore.SyncBackend):
    """Connects only to addresses that passed resolve(), looked up once at connect time, so a name can't pass the
    check and then be pointed somewhere private (DNS rebinding). TLS still checks the certificate against the
    original host name (httpcore passes it as server_hostname)."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        why, addrs = resolve(host, port)
        if why != "ok":
            raise httpcore.ConnectError(f"refused {host}: {why}")
        err = None
        for a in addrs:
            try:
                return super().connect_tcp(a, port, timeout=timeout, local_address=local_address,
                                           socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                err = e
        raise err


def make_client() -> httpx.Client:
    transport = httpx.HTTPTransport(retries=0)
    pool = getattr(transport, "_pool", None)
    if not isinstance(pool, httpcore.ConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("unexpected httpx internals: refusing to run without address pinning")
    pool._network_backend = PinnedBackend()
    return httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                        headers={"User-Agent": UA, "Accept-Language": "en,*;q=0.5",
                                 "Accept-Encoding": "gzip, deflate"})


def _read(r: httpx.Response, cap: int, deadline: float, truncate: bool = False) -> tuple[str, bytes]:
    """The body, at most `cap` bytes both as sent and once decompressed. gzip/deflate are decoded here a bounded
    amount at a time, so a small compressed body can't balloon in memory. truncate: keep the first `cap` bytes
    instead of giving up (robots.txt)."""
    enc = (r.headers.get("content-encoding") or "identity").strip().lower()
    if enc in ("identity", ""):
        dec = None
    elif enc in ("gzip", "x-gzip", "deflate"):
        dec = zlib.decompressobj(zlib.MAX_WBITS | 32)  # zlib or gzip wrapper, detected from the header
    else:
        return f"encoding:{enc[:20]}", b""
    out, raw = bytearray(), 0

    def over() -> tuple[str, bytes]:
        return ("ok", bytes(out[:cap])) if truncate else ("too_big", b"")

    try:
        for chunk in r.iter_raw():
            if time.time() > deadline:
                return "error:slow", b""
            raw += len(chunk)
            if dec is None:
                out += chunk
            else:
                data = chunk
                while data and not dec.eof and len(out) <= cap:
                    out += dec.decompress(data, cap + 1 - len(out))
                    data = dec.unconsumed_tail
            if len(out) > cap or raw > cap:
                return over()
            if dec is not None and dec.eof:
                break
        if dec is not None and not dec.eof:
            out += dec.flush()
    except zlib.error:
        return "error:decode", b""
    return over() if len(out) > cap else ("ok", bytes(out))


def _hop(client: httpx.Client, url: str, cap: int, truncate: bool = False) -> tuple[str, httpx.Response | None, bytes, str]:
    """One GET, redirects not followed, with per-site spacing and cooldowns. (status, response, body, redirect)."""
    s = site(host(url))
    with s.lock:
        if time.time() < s.cool_until:
            return "cooldown", None, b"", ""
        wait = s.last + s.spacing - time.time()
        if wait > 0:
            time.sleep(wait)
        s.last = time.time()
    try:
        with client.stream("GET", url, timeout=30) as r:
            if r.is_redirect:
                return "redirect", r, b"", urljoin(url, r.headers["location"].strip())
            if r.status_code in (429, 503):
                ra = r.headers.get("retry-after", "")
                s.cool_until = time.time() + (int(ra) if ra.isdigit() else 900)
            if r.status_code != 200:
                return f"http_{r.status_code}", r, b"", ""
            status, body = _read(r, cap, time.time() + 120, truncate)
            return status, r, body, ""
    except httpx.HTTPError as e:
        return f"error:{type(e).__name__}", None, b"", ""


def _check(url: str) -> str:
    """'ok' if url may be requested at all: http(s), a host, every address public."""
    try:
        u = urlparse(url)
        u.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return "bad_url"
    if u.scheme not in ("http", "https") or not host(url):
        return "bad_url"
    why, _ = resolve(host(url))
    return {"ok": "ok", "dns": "error:dns"}.get(why, "private")


def _load_robots(client: httpx.Client, url: str) -> tuple[RobotFileParser | None, str]:
    u = urlparse(url)
    target = f"{u.scheme}://{u.netloc.rpartition('@')[2]}/robots.txt"
    for _ in range(6):  # RFC 9309: follow at least five redirects
        why = _check(target)
        if why != "ok":
            return None, f"error:robots_{why.split(':')[-1]}"
        status, _r, body, loc = _hop(client, target, ROBOTS_BYTES, truncate=True)
        if status == "redirect":
            target = loc
            continue
        rp = RobotFileParser()
        if status == "ok":
            rp.parse(body.decode("utf-8", "replace").splitlines())
            return rp, "ok"
        if status in ("http_401", "http_403"):
            rp.disallow_all = True  # as the standard library does: a site that bars robots.txt bars us
            return rp, "ok"
        if re.fullmatch(r"http_4\d\d", status) and status != "http_429":
            rp.parse([])  # no robots.txt: no rules (RFC 9309)
            return rp, "ok"
        return None, "cooldown" if status == "cooldown" else f"error:robots_{status.split(':')[-1]}"
    return None, "error:robots_redirects"


def robots(client: httpx.Client, url: str) -> str:
    """'ok' if url's own origin's robots.txt allows it, 'robots' if it forbids it, otherwise a passing status
    ('error:robots_…', 'cooldown'): an unreadable robots.txt never counts as allow-all."""
    u = urlparse(url)
    origin = f"{u.scheme}://{host(url)}:{u.port or (443 if u.scheme == 'https' else 80)}"
    with SITES_LOCK:
        ent = ROBOTS.setdefault(origin, Robots())
    with ent.lock:
        if ent.rp is None and time.time() >= ent.until:
            ent.rp, ent.status = _load_robots(client, url)
            ent.until = time.time() + (0 if ent.status == "cooldown" else ROBOTS_RETRY)
        if ent.rp is None:
            return ent.status
        rp = ent.rp
    return "ok" if rp.can_fetch(UA, url) else "robots"


def get(client: httpx.Client, url: str, *, cap: int) -> tuple[str, httpx.Response | None, bytes, list[str]]:
    """GET with manual redirects: every hop must pass _check and its own site's robots.txt, and connects only to
    a checked address (PinnedBackend). Returns (status, response, body, redirect hops)."""
    hops: list[str] = []
    for _ in range(6):
        ok = _check(url)
        if ok == "ok":
            ok = robots(client, url)
        if ok != "ok":
            return ok, None, b"", hops
        status, r, body, loc = _hop(client, url, cap)
        if status != "redirect":
            return status, r, body, hops
        url = loc
        hops.append(url)
    return "too_many_redirects", None, b"", hops


# ------------------------------------------------------------------ extraction

def _ancestors(n: Node):
    p = n.parent
    while p is not None and p.tag not in ("html", "-undef", "#document"):
        yield p
        p = p.parent


def _labels(n: Node) -> str:
    a = n.attributes
    s = f"{a.get('class') or ''} {a.get('id') or ''} {a.get('role') or ''}"
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)  # relatedArticles, siteHeader -> related Articles, site Header


def _best_src(img: Node, base: str) -> str:
    a = img.attributes
    cands = []
    srcsets = [a.get("srcset"), a.get("data-srcset")]
    if img.parent is not None and img.parent.tag == "picture":
        srcsets += [s.attributes.get("srcset") for s in img.parent.css("source")]
    for ss in srcsets:
        for part in (ss or "").split(","):
            bits = part.strip().split()
            if bits:
                w = int(re.sub(r"\D", "", bits[1]) or 0) if len(bits) > 1 and bits[1].endswith("w") else 0
                cands.append((w, bits[0]))
    if cands:
        return urljoin(base, max(cands)[1])
    src = a.get("data-src") or a.get("data-lazy-src") or a.get("data-original") or a.get("src") or ""
    return urljoin(base, src.strip()) if src.strip() else ""


def _text(n: Node | None, limit: int) -> str:
    return re.sub(r"\s+", " ", n.text(separator=" ") if n is not None else "").strip()[:limit]


def _rank(i: dict) -> tuple:
    """Likeliest to be the page's own photograph first."""
    return (not i["in_body"], not (i["caption"] or i["alt"]), not i["from_meta"], i["pos"])


def _merge(old: dict, new: dict) -> None:
    """A picture shown again: keep the richer evidence (a later figure with a caption and credit beats an earlier
    bare thumbnail of the same file)."""
    if new["caption"] and not old["caption"]:
        for k in ("caption", "credit", "near_text"):
            old[k] = new[k]
    for k in ("alt", "credit", "near_text"):
        if not old[k] and new[k]:
            old[k] = new[k]
    old["in_body"] = old["in_body"] or new["in_body"]
    old["w_attr"], old["h_attr"] = max(old["w_attr"], new["w_attr"]), max(old["h_attr"], new["h_attr"])
    old["occurrences"] = old.get("occurrences", 1) + 1


def page_media(html: str, base: str) -> dict:
    """The pictures on one page, best first, and what the page is (title, site, date, author). No network."""
    tree = HTMLParser(html)
    meta = {}
    for m in tree.css("meta"):
        k = (m.attributes.get("property") or m.attributes.get("name") or "").lower()
        if k and m.attributes.get("content") and k not in meta:
            meta[k] = m.attributes["content"].strip()
    info = {"title": meta.get("og:title") or _text(tree.css_first("title"), 300),
            "site_name": meta.get("og:site_name", ""),
            "published": meta.get("article:published_time") or meta.get("date") or meta.get("pubdate") or "",
            "author": meta.get("author") or meta.get("article:author") or "",
            "description": meta.get("og:description") or meta.get("description") or ""}
    images: list[dict] = []
    by_src: dict[str, dict] = {}
    for pos, img in enumerate(tree.css("img")):
        src = _best_src(img, base)
        a = img.attributes
        if not src.startswith("http") or BAD_URL.search(src) or re.search(r"\.(svg|gif)(\?|$)", src, re.I):
            continue
        anc = list(_ancestors(img))
        if any(p.tag in ("header", "nav", "footer", "aside") or CHROME.search(_labels(p)) for p in anc):
            continue
        dims = [int(re.sub(r"\D", "", a.get(d) or "") or 0) for d in ("width", "height")]
        if any(0 < d < 200 for d in dims):
            continue
        fig = next((p for p in anc if p.tag == "figure"), None)
        cap = _text(fig.css_first("figcaption"), 500) if fig is not None else ""
        credit = _text(fig.css_first(".credit, [class*=credit]"), 120) if fig is not None else ""
        if not credit and (m := CREDIT.search(cap or a.get("title") or "")):
            credit = m.group(0).strip()
        e = {"src": src, "alt": (a.get("alt") or "").strip()[:300], "caption": cap, "credit": credit,
             "near_text": _text(img.parent.parent if img.parent is not None and img.parent.parent is not None
                                else img.parent, 300),
             "in_body": fig is not None or any(p.tag in ("article", "main") or BODY.search(_labels(p))
                                               or p.attributes.get("itemprop") == "articleBody" for p in anc),
             "from_meta": False, "w_attr": dims[0], "h_attr": dims[1], "pos": pos}
        if src in by_src:
            _merge(by_src[src], e)
        else:
            by_src[src] = e
            images.append(e)
    for k in ("og:image", "og:image:url", "twitter:image"):
        src = urljoin(base, meta[k]) if meta.get(k) else ""
        if src.startswith("http") and not BAD_URL.search(src):
            if src in by_src:
                by_src[src]["from_meta"] = True
            else:
                by_src[src] = {"src": src, "alt": "", "caption": "", "credit": "", "near_text": "", "in_body": False,
                               "from_meta": True, "w_attr": 0, "h_attr": 0, "pos": -1}
                images.append(by_src[src])
    images.sort(key=_rank)  # rank first, then keep the best 15
    return {"info": info, "images": images[:MAX_LISTED]}


def dhash(im: Image.Image) -> str:
    g = im.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.get_flattened_data() if hasattr(g, "get_flattened_data") else g.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = bits << 1 | (px[r * 9 + c] > px[r * 9 + c + 1])
    return f"{bits:016x}"


def download(client: httpx.Client, img: dict, out: Path) -> dict:
    status, r, body, hops = get(client, img["src"], cap=MAX_BYTES)
    if status != "ok":
        return {"status": status}
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in IMG_TYPES:
        return {"status": f"not_image:{ctype[:40]}"}
    try:
        with Image.open(io.BytesIO(body)) as im:
            im.load()
            w, h, fmt, dh = im.width, im.height, im.format, dhash(im)
    except Exception as e:  # noqa: BLE001 - a broken or hostile image is recorded, never fatal
        return {"status": f"bad_image:{type(e).__name__}"}
    if min(w, h) < MIN_SIDE:
        return {"status": "small", "w": w, "h": h}
    sha = hashlib.sha256(body).hexdigest()
    dest = out / "blobs" / sha[:2] / f"{sha}.{IMG_TYPES[ctype]}"
    if not dest.exists():
        if shutil.disk_usage(out).free < MIN_FREE:
            return {"status": "error:disk_full"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        # one temp file per thread: two pages can download the same picture at once
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.part")
        tmp.write_bytes(body)
        os.replace(tmp, dest)
    return {"status": "ok", "sha256": sha, "ext": IMG_TYPES[ctype], "bytes": len(body), "w": w, "h": h, "format": fmt,
            "dhash": dh, "final_url": str(r.url), "redirects": hops}


def _write(dest: Path, doc: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, dest)


def merge_recordings(doc: dict, recs: list[dict]) -> bool:
    """Add the recordings that now cite this page. True if any were new."""
    have = {(r.get("rec"), r.get("source_id")) for r in doc.get("recordings") or []}
    new = [r for r in recs if (r.get("rec"), r.get("source_id")) not in have]
    doc["recordings"] = (doc.get("recordings") or []) + new
    return bool(new)


def _retry_due(img: dict, page: dict) -> bool:
    """A picture whose download failed for a passing reason, with tries left and its wait over."""
    d = img.get("download")
    if not d or not TRANSIENT.match(d.get("status") or ""):
        return False
    n = d.get("attempts", 1)
    return n < 3 and _due(d.get("t") or page.get("t"), n)


def _fetch_images(client: httpx.Client, images: list[dict], out: Path) -> None:
    for i in images:
        tries = (i["download"].get("attempts", 1)) if i.get("download") else 0
        i["download"] = {**download(client, i, out), "attempts": tries + 1, "t": time.time()}


def _page_due(doc: dict | None) -> bool:
    """Worth fetching now: never fetched, or a passing failure whose wait is over."""
    return doc is None or (not final(doc) and _due(doc.get("t"), doc.get("attempts", 0)))


def needs_work(out: Path, p: dict) -> bool:
    doc = _load(out / "pages" / f"{key(p['url'])}.json")
    return _page_due(doc) or (doc.get("status") == "ok" and any(_retry_due(i, doc) for i in doc.get("images") or []))


def do_page(client: httpx.Client, p: dict, out: Path) -> str:
    dest = out / "pages" / f"{key(p['url'])}.json"
    old = _load(dest)
    if not _page_due(old):  # not fetched (again) now; new citing recordings added, picture failures retried
        changed = merge_recordings(old, p["recordings"])
        retry = [i for i in old.get("images") or [] if _retry_due(i, old)] if old.get("status") == "ok" else []
        if retry:
            _fetch_images(client, retry, out)
            old["images_retried"] = time.time()
        if changed or retry:
            _write(dest, old)
        return "retried" if retry else ("skip" if final(old) else "wait")
    doc = {"url": p["url"], "t": time.time(), "attempts": (old or {}).get("attempts", 0) + 1,
           "recordings": list(p["recordings"])}
    merge_recordings(doc, (old or {}).get("recordings") or [])
    status, r, body, hops = get(client, p["url"], cap=PAGE_BYTES)
    doc.update(status=status, redirects=hops, final_url=str(r.url) if r is not None else "")
    ctype = (r.headers.get("content-type") or "") if r is not None else ""
    if status == "ok" and "html" not in ctype:
        doc["status"] = f"not_html:{ctype.split(';')[0][:40]}"
    elif status == "ok":
        m = page_media(body.decode(r.encoding or "utf-8", "replace"), doc["final_url"])
        doc["info"] = m["info"]
        _fetch_images(client, m["images"][:PER_PAGE], out)  # page_media lists them best first
        doc["images"] = m["images"]
    _write(dest, doc)
    return doc["status"]


def run(todo: list[Path], out: Path, workers: int) -> None:
    pages = [json.loads(l) for f in todo for l in open(f, encoding="utf-8") if l.strip()]
    by_host: dict[str, list[dict]] = {}
    for p in pages:  # one thread works through a site's pages in order; sites run side by side
        by_host.setdefault(p["host"], []).append(p)
    _warm([str(out / "pages" / f"{key(p['url'])}.json") for p in pages])
    live = {h: [p["rank"] for p in ps if needs_work(out, p)] for h, ps in by_host.items()}
    # sites with pages still to fetch first, nearest-done first; the rest only get new recordings merged in
    hosts = sorted(by_host, key=lambda h: (0, min(live[h])) if live[h] else (1, by_host[h][0]["rank"]))
    n_live = sum(len(v) for v in live.values())
    print(f"{time.strftime('%H:%M:%S')} {len(pages)} pages, {n_live} to fetch on {sum(1 for v in live.values() if v)} "
          f"sites", flush=True)
    counts: dict[str, int] = {}
    lock = threading.Lock()
    t0 = time.time()

    def one_host(h: str) -> None:
        with make_client() as client:
            for p in by_host[h]:
                try:
                    st = do_page(client, p, out)
                except Exception as e:  # noqa: BLE001 - one bad page must not stop the site
                    st = f"crash:{type(e).__name__}"
                    print(f"{p['url']}: {e!r}", flush=True)
                with lock:
                    counts[st.split(":")[0]] = counts.get(st.split(":")[0], 0) + 1
                    n = sum(counts.values())
                    if n % 50 == 0:
                        print(f"{time.strftime('%H:%M:%S')} {n}/{len(pages)} pages {counts} "
                              f"{n / (time.time() - t0) * 3600:.0f}/h", flush=True)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one_host, hosts))
    print(f"{time.strftime('%H:%M:%S')} done: {len(pages)} pages {counts}", flush=True)


# ------------------------------------------------------------------ index

def index(out: Path) -> None:
    """Per-recording candidate lists from every page file (both laptops' pages land in the same folder)."""
    recs: dict[str, dict] = {}
    pages = imgs = 0
    for f in glob.glob(str(out / "pages/*.json")):
        doc = json.load(open(f, encoding="utf-8"))
        pages += 1
        got = [i for i in doc.get("images") or [] if (i.get("download") or {}).get("status") == "ok"]
        imgs += len(got)
        for r in doc["recordings"]:
            rec = recs.setdefault(r["rec"], {"pages": [], "images": 0})
            rec["pages"].append({**r, "url": doc["url"], "status": doc["status"], "final_url": doc.get("final_url", ""),
                                 "info": doc.get("info") or {},
                                 "images": [{**i, "kind": "unverified"} for i in got]})
            rec["images"] += len(got)
    (out / "recordings").mkdir(parents=True, exist_ok=True)
    for pid, rec in recs.items():
        (out / "recordings" / f"{pid}.json").write_text(json.dumps({"id": pid, "t": time.time(), **rec},
                                                                   ensure_ascii=False))
    summary = {"t": time.time(), "pages": pages, "pictures": imgs, "recordings": len(recs),
               "recordings_with_pictures": sum(1 for r in recs.values() if r["images"])}
    (out / "summary.json").write_text(json.dumps(summary))
    print(summary)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--shards", type=int, default=2)
    r = sub.add_parser("run")
    r.add_argument("--todo", type=Path, nargs="+", required=True)
    r.add_argument("--workers", type=int, default=6)
    sub.add_parser("index")
    for s in (p, r, sub.choices["index"]):
        s.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        plan(a.shards, a.out)
    elif a.cmd == "run":
        run(a.todo, a.out, a.workers)
    else:
        index(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
