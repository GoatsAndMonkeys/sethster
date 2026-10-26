"""Wiki layer for Sethster: verify / find Wikidata QIDs for every entity in the research results and
cache Wikidata + Wikipedia + Commons metadata under data/wiki/.

Usage:
    uv run python pipeline/enrich_wiki.py                 # data/pilot/results + data/research/results
    uv run python pipeline/enrich_wiki.py path/to/*.json  # explicit result files

Output format is documented in data/wiki/README.md. HTTP responses are cached under
data/cache/wiki_http, so reruns only hit the network for new entities.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline import localconfig  # noqa: E402

RESULT_GLOBS = ["data/pilot/results/*.json", "data/research/results/*.json"]
OUT = ROOT / "data/wiki"
CACHE = ROOT / "data/cache/wiki_http"
USER_AGENT = f"Sethster/0.1 (private family music app; {localconfig.contact_email()})"
WD_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
FORMAT_VERSION = 1
CONCURRENCY = 3
FALLBACK_WIKIS = ["de", "it", "fr", "es"]  # used for bios only when there is no English article

# ----------------------------------------------------------------------------------------------
# Normalization


def norm(s: str) -> str:
    """Index key normalization (documented in README): NFKD, strip combining marks, casefold,
    '&' -> 'and', every run of non-alphanumerics -> one space, trim."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold().replace("ß", "ss").replace("&", " and ")
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return s.strip()


def strip_parens(s: str) -> str:
    return re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", s).strip()


def work_key(composer: str, work: str) -> str:
    return f"{norm(composer)}|{norm(work)}"


# ----------------------------------------------------------------------------------------------
# HTTP with disk cache, retries, per-host politeness


class Http:
    """Cached, polite GET. One request at a time per host at most every MIN_INTERVAL seconds,
    at most CONCURRENCY in flight overall; 429/5xx back off (honouring Retry-After) and pause the host."""
    MIN_INTERVAL = {"www.wikidata.org": 0.5, "commons.wikimedia.org": 0.6}
    DEFAULT_INTERVAL = 0.6  # *.wikipedia.org

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Api-User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0), follow_redirects=True)
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.host_lock: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.host_next: dict[str, float] = defaultdict(float)
        self.inflight: dict[str, asyncio.Future] = {}
        self.stats = defaultdict(int)

    def _path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode()).hexdigest()
        return CACHE / h[:2] / f"{h}.json"

    async def _gate(self, host: str) -> None:
        async with self.host_lock[host]:
            loop = asyncio.get_running_loop()
            wait = self.host_next[host] - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self.host_next[host] = loop.time() + self.MIN_INTERVAL.get(host, self.DEFAULT_INTERVAL)

    async def get_json(self, url: str, params: dict | None = None) -> dict | None:
        """GET JSON; returns None on 404. Responses (200 and 404) are cached forever."""
        full = str(httpx.URL(url, params=params or {}))
        p = self._path(full)
        if p.exists():
            self.stats["cache_hit"] += 1
            rec = json.loads(p.read_text())
            return rec["body"] if rec["status"] == 200 else None
        if full in self.inflight:
            return await self.inflight[full]
        fut = asyncio.get_running_loop().create_future()
        self.inflight[full] = fut
        try:
            body = await self._fetch(full, p)
            fut.set_result(body)
            return body
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved
            raise
        finally:
            self.inflight.pop(full, None)

    async def _fetch(self, full: str, p: Path) -> dict | None:
        host = httpx.URL(full).host
        delay = 5.0
        err = None
        for attempt in range(9):
            await self._gate(host)
            async with self.sem:
                try:
                    r = await self.client.get(full)
                except httpx.HTTPError as e:
                    r, err = None, e
            if r is not None and r.status_code in (200, 404):
                self.stats["fetched"] += 1
                body = r.json() if r.status_code == 200 else None
                if isinstance(body, dict) and body.get("error", {}).get("code") in ("maxlag", "ratelimited"):
                    await asyncio.sleep(delay); delay = min(delay * 2, 120); continue
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"url": full, "status": r.status_code, "fetched_at": now(),
                                         "body": body}, ensure_ascii=False))
                return body
            wait = delay
            if r is not None and r.headers.get("retry-after", "").isdigit():
                wait = max(wait, float(r.headers["retry-after"]))
            self.host_next[host] = asyncio.get_running_loop().time() + wait  # pause the whole host
            self.stats["retry"] += 1
            print(f"  retry {attempt + 1} in {wait:.0f}s ({r.status_code if r is not None else err}) {full[:110]}",
                  file=sys.stderr)
            await asyncio.sleep(wait)
            delay = min(delay * 2, 120)
        raise RuntimeError(f"giving up on {full}")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------------------------
# Wikidata access


class WD:
    def __init__(self, http: Http) -> None:
        self.http = http
        self.entities: dict[str, dict] = {}
        self.labels_path = CACHE / "labels.json"
        self.labels: dict[str, dict] = json.loads(self.labels_path.read_text()) if self.labels_path.exists() else {}
        self.labels_lock = asyncio.Lock()

    def save_labels(self) -> None:
        self.labels_path.parent.mkdir(parents=True, exist_ok=True)
        self.labels_path.write_text(json.dumps(self.labels, ensure_ascii=False))

    async def entity(self, qid: str) -> dict | None:
        """Full entity (labels, descriptions, aliases, claims, sitelinks). Follows redirects."""
        if qid in self.entities:
            return self.entities[qid]
        body = await self.http.get_json(WD_API, {
            "action": "wbgetentities", "ids": qid, "format": "json",
            "props": "labels|descriptions|aliases|claims|sitelinks"})
        ent = None
        if body and "entities" in body:
            for k, v in body["entities"].items():
                if "missing" not in v:
                    ent = v
        self.entities[qid] = ent
        return ent

    async def fetch_labels(self, qids: set[str]) -> None:
        """English(ish) labels for referenced items; persisted per QID in labels.json."""
        if all(q in self.labels for q in qids):
            return
        async with self.labels_lock:
            await self._fetch_labels(qids)

    async def _fetch_labels(self, qids: set[str]) -> None:
        todo = sorted(q for q in qids if q not in self.labels and re.fullmatch(r"[QP]\d+", q))
        for i in range(0, len(todo), 50):
            chunk = todo[i:i + 50]
            body = await self.http.get_json(WD_API, {
                "action": "wbgetentities", "ids": "|".join(chunk), "format": "json",
                "props": "labels|descriptions", "languages": "en|mul|de|it|fr",
                "languagefallback": "1"})
            for k, v in (body or {}).get("entities", {}).items():
                self.labels[k] = {"label": pick_lang(v.get("labels")), "description": pick_lang(v.get("descriptions"))}
            for q in chunk:
                self.labels.setdefault(q, {"label": None, "description": None})

    def label(self, qid: str) -> str | None:
        return (self.labels.get(qid) or {}).get("label")

    async def search(self, text: str, lang: str = "en", limit: int = 10) -> list[dict]:
        body = await self.http.get_json(WD_API, {
            "action": "wbsearchentities", "search": text, "language": lang, "uselang": "en",
            "type": "item", "limit": str(limit), "format": "json"})
        return (body or {}).get("search", [])

    async def fulltext(self, query: str, limit: int = 15) -> list[str]:
        body = await self.http.get_json(WD_API, {
            "action": "query", "list": "search", "srsearch": query, "srnamespace": "0",
            "srlimit": str(limit), "format": "json"})
        return [h["title"] for h in (body or {}).get("query", {}).get("search", [])]


def pick_lang(d: dict | None, langs=("en", "mul", "en-gb", "en-us", "de", "it", "fr", "es")) -> str | None:
    if not d:
        return None
    for l in langs:
        if l in d:
            return d[l]["value"]
    return next(iter(d.values()))["value"]


def claim_values(ent: dict, pid: str) -> list:
    out = []
    for c in ent.get("claims", {}).get(pid, []):
        if c.get("rank") == "deprecated":
            continue
        dv = c.get("mainsnak", {}).get("datavalue")
        if dv:
            out.append(dv["value"])
    return out


def claim_qids(ent: dict, pid: str) -> list[str]:
    return [v["id"] for v in claim_values(ent, pid) if isinstance(v, dict) and "id" in v]


def wd_time(v: dict) -> str | None:
    """Wikidata time value -> partial ISO string honouring precision (YYYY, YYYY-MM, YYYY-MM-DD)."""
    t, prec = v.get("time", ""), v.get("precision", 11)
    m = re.match(r"([+-])(\d+)-(\d\d)-(\d\d)", t)
    if not m:
        return None
    sign, y, mo, d = m.groups()
    y = int(y) * (-1 if sign == "-" else 1)
    ys = f"{y:04d}" if y >= 0 else f"-{-y:04d}"
    if prec >= 11 and mo != "00" and d != "00":
        return f"{ys}-{mo}-{d}"
    if prec == 10 and mo != "00":
        return f"{ys}-{mo}"
    if prec < 9:  # decade/century/millennium ("20th century" is stored as 2000): too vague to use
        return None
    return ys


def first_time(ent: dict, *pids: str) -> str | None:
    for pid in pids:
        vals = [wd_time(v) for v in claim_values(ent, pid) if isinstance(v, dict) and "time" in v]
        vals = [v for v in vals if v]
        if vals:
            return min(vals)
    return None


def year_of(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"(-?\d{1,4})", s)
    return int(m.group(1)) if m else None


def all_names(ent: dict) -> set[str]:
    names = {v["value"] for v in ent.get("labels", {}).values()}
    for vs in ent.get("aliases", {}).values():
        names |= {v["value"] for v in vs}
    return {norm(n) for n in names if n}


# ----------------------------------------------------------------------------------------------
# Role classification and type checks

KW = {
    "singer": r"singer|soprano|tenor|baritone|bass|bass baritone|mezzo|mezzo soprano|contralto|alto|countertenor|vocalist|cantor|opera singer|treble",
    "conductor": r"conductor|choirmaster|chorus master|choral conductor|choir director|music director|kapellmeister|chorus director",
    "instrumentalist": r"pianist|violinist|violist|cellist|flautist|flutist|harpist|trumpeter|timpanist|percussionist|harpsichordist|organist|oboist|clarinettist|clarinetist|bassoonist|hornist|horn player|instrumentalist|guitarist|double bassist|concertmaster|accompanist|glass harmonica|repetiteur|musician",
    "composer": r"composer",
    "theatre": r"director|designer|choreographer|dancer|ballet|scenographer|stage|costume|lighting|artist|painter|architect|actor|actress|theatre|theater|producer|sculptor|photographer|dramaturge|dramaturg|film|opera director|illustrator|circus|acrobat",
    "broadcast": r"broadcaster|presenter|radio|journalist|announcer|writer|critic|musicologist|host|producer|author|historian|scholar|actor|television|engineer|professor|music critic|lecturer|editor|politician|teacher|academic|personality",
}
# Stricter lists used only when *searching* (a hit must have a role-specific occupation).
KW["theatre_strict"] = r"director|designer|choreographer|dancer|ballet|scenographer|stage|costume|lighting|theatre|theater|dramaturge|dramaturg|opera director|scenic designer|set designer|lighting designer|circus|acrobat"
KW["broadcast_strict"] = r"broadcaster|presenter|radio|announcer|journalist|music critic|musicologist|host|audio engineer|sound engineer|recording engineer|record producer|music producer|radio producer|television presenter|radio personality"
KW["music"] = "|".join([KW["singer"], KW["conductor"], KW["instrumentalist"], KW["composer"], r"music|opera|répétiteur"])
KW_RE = {k: re.compile(rf"\b(?:{v})\b") for k, v in KW.items()}

ENSEMBLE_TYPE = re.compile(r"\b(orchestra|choir|chorus|ensemble|band|musical group|philharmonic|chamber group|quartet|quintet|trio|opera company|music organization|musical ensemble|boys choir|children s choir|circus|organization|chorale|singers)\b")
WORK_TYPE = re.compile(r"\b(work|composition|opera|symphony|concerto|oratorio|requiem|mass|cantata|suite|overture|ballet|song|sonata|piece|idyll|poem|music|movement|quartet|variations|prelude|musical|lied|cycle|tableau|serenade|fantasia|rhapsody|aria|scene|act|tone poem|symphonic poem|nocturne|march|dance|chamber)\b")
VENUE_TYPE = re.compile(r"\b(hall|house|theatre|theater|opera|venue|building|auditorium|arena|church|cathedral|chapel|center|centre|stadium|structure|amphitheatre|amphitheater|room|museum|palace|festival|concert|studio|performing arts|cultural|basilica|complex|facility)\b")
ENSEMBLE_NAME = re.compile(r"(orchest|chorus|choir|\bchor\b|philharmon|symphon|ensemble|singers|quartet|sängerknaben|saengerknaben|kapelle|\bcoro\b|chœur|choeur|konzertvereinigung|acrobats|band\b)", re.I)


def classify_role(role: str, name: str) -> str:
    """Map a free-form credit role to: ensemble | singer | conductor | instrumentalist | theatre | broadcast | other."""
    r = role.casefold()
    base = strip_parens(r).strip()
    if base in {"orchestra", "chorus", "choir", "children's chorus", "children's choir", "ensemble", "band", "acrobats",
                "boys chorus", "boys' chorus"}:
        return "ensemble"
    if ENSEMBLE_NAME.search(name) and not re.search(r"master|director|conductor|leader", r):
        return "ensemble"
    if re.search(r"conductor|chorus master|chorus director|choirmaster|musical preparation|prompter|director, .*(chorus|choir)", r):
        return "conductor"
    if re.search(r"\b(piano|violin|viola|cello|flute|harp|trumpet|timpani|horn|harmonica|harpsichord|organ|oboe|clarinet|bassoon|percussion|concertmaster|leader|solo)\b", r):
        return "instrumentalist"
    if re.search(r"announc|host|presenter|interview|commentat|panelist|producer|engineer|narrat|speaker|guest|radio|quiz|intermission|interval", r):
        return "broadcast"
    if re.search(r"direct|design|costume|lighting|choreograph|dancer|production|staging|dramaturg|\bsets?\b|video|projection|rehears|stage|assistant|inszenierung|ausstattung", r):
        return "theatre"
    return "singer"


# Which occupation keyword groups count as consistent, for verification (lenient) and search (strict).
VERIFY_OK = {"singer": ["music"], "conductor": ["music"], "instrumentalist": ["music"], "composer": ["composer"],
             "theatre": ["theatre", "music", "broadcast"], "broadcast": None, "other": None}
SEARCH_OK = {"singer": ["singer"], "conductor": ["conductor", "music"], "instrumentalist": ["instrumentalist"],
             "composer": ["composer"], "theatre": ["theatre_strict"], "broadcast": ["broadcast_strict"], "other": None}


# ----------------------------------------------------------------------------------------------
# Entities collected from the results


@dataclass
class Mention:
    perf_id: str
    perf_date: str
    perf_kind: str
    date_status: str
    where: tuple  # ("credit", i) | ("program", i, "composer"|"work") | ("venue",)
    role: str | None = None


@dataclass
class Entity:
    etype: str               # person | ensemble | composer | work | venue
    name: str                # as it appears in results (first seen)
    given_qid: str | None
    composer_name: str | None = None   # works
    composer_qid: str | None = None    # works (as given in results)
    catalogue: str | None = None       # works
    city: str | None = None            # venues
    names: set = field(default_factory=set)
    categories: set = field(default_factory=set)  # person role categories
    roles: set = field(default_factory=set)
    mentions: list = field(default_factory=list)
    # outcome
    status: str = "unmatched"   # verified | matched | rejected | unmatched
    qid: str | None = None
    scope: str = "exact"        # works: exact | parent
    evidence: list = field(default_factory=list)
    reject_reason: str | None = None
    rejected_info: dict | None = None
    candidates: list = field(default_factory=list)
    production_only: bool = False      # every credit is status production_only (e.g. original director)


def composer_key(e: Entity) -> str:
    return e.composer_qid or "name:" + norm(e.composer_name or "")


def load_results(paths: list[Path]) -> list[dict]:
    docs = []
    for p in paths:
        try:
            d = json.loads(p.read_text())
        except Exception as e:  # noqa: BLE001
            print(f"skip {p}: {e}", file=sys.stderr); continue
        if isinstance(d, dict) and "credits" in d and "program" in d:
            d["_path"] = str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)
            docs.append(d)
    return docs


def collect(docs: list[dict]) -> dict[tuple, Entity]:
    ents: dict[tuple, Entity] = {}

    def get(key, **kw) -> Entity:
        if key not in ents:
            ents[key] = Entity(**kw)
        return ents[key]

    for d in docs:
        p = d.get("performance", {})
        base = dict(perf_id=d["id"], perf_date=p.get("date", ""), perf_kind=d.get("kind", "unknown"),
                    date_status=p.get("date_status", "unknown"))
        if p.get("venue"):
            q = (p.get("venue_wikidata") or "").strip() or None
            e = get(("venue", q or norm(strip_parens(p["venue"]))), etype="venue", name=p["venue"], given_qid=q,
                    city=p.get("city"))
            e.names.add(p["venue"]); e.mentions.append(Mention(**base, where=("venue",)))
        for i, w in enumerate(d.get("program", [])):
            cq = (w.get("composer_wikidata") or "").strip() or None
            e = get(("composer", cq or norm(w["composer"])), etype="composer", name=w["composer"], given_qid=cq)
            e.names.add(w["composer"]); e.categories.add("composer")
            e.mentions.append(Mention(**base, where=("program", i, "composer")))
            wq = (w.get("work_wikidata") or "").strip() or None
            e = get(("work", wq or work_key(w["composer"], w["work"])), etype="work", name=w["work"], given_qid=wq,
                    composer_name=w["composer"], composer_qid=cq, catalogue=w.get("catalogue") or None)
            e.names.add(w["work"]); e.mentions.append(Mention(**base, where=("program", i, "work")))
        for i, c in enumerate(d.get("credits", [])):
            cat = classify_role(c.get("role", ""), c["name"])
            etype = "ensemble" if cat == "ensemble" else "person"
            q = (c.get("wikidata") or "").strip() or None
            e = get((etype, q or norm(c["name"])), etype=etype, name=c["name"], given_qid=q)
            if not e.mentions:
                e.production_only = True
            e.production_only = e.production_only and c.get("status") == "production_only"
            e.names.add(c["name"]); e.roles.add(c.get("role", ""))
            if etype == "person":
                e.categories.add(cat)
            e.mentions.append(Mention(**base, where=("credit", i), role=c.get("role")))
    return ents


# ----------------------------------------------------------------------------------------------
# Checks


def child_role(roles) -> bool:
    return any(re.search(r"\bchild\b|knabe|boy", r, re.I) for r in roles)


def archival(e: Entity) -> bool:
    return any(re.search(r"archiv", r or "", re.I) for r in e.roles) or \
        any(m.perf_kind in ("compilation", "commercial_release") or m.date_status == "unknown" for m in e.mentions)


def perf_years(e: Entity) -> list[int]:
    return [y for y in (year_of(m.perf_date) for m in e.mentions if m.date_status != "unknown") if y]


def occupation_text(wd: WD, ent: dict) -> tuple[list[str], str]:
    occ = [wd.label(q) or "" for q in claim_qids(ent, "P106")]
    return occ, " ; ".join(norm(o) for o in occ)


def check_person(wd: WD, e: Entity, ent: dict, strict: bool) -> tuple[bool, list[str]]:
    """Return (ok, reasons/evidence)."""
    ev: list[str] = []
    if "Q5" not in claim_qids(ent, "P31"):
        types = [wd.label(q) for q in claim_qids(ent, "P31")]
        return False, [f"not a human (instance of: {', '.join(t or '?' for t in types) or 'none'})"]
    occ, occ_t = occupation_text(wd, ent)
    desc = norm(pick_lang(ent.get("descriptions"), ("en", "mul", "en-gb")) or "")
    table = SEARCH_OK if strict else VERIFY_OK
    cats = sorted(e.categories) or ["other"]
    groups = set()
    for c in cats:
        g = table.get(c)
        if g is None:
            groups = None; break
        groups.update(g)
    if groups is None:
        ev.append("human (role does not constrain occupation)")
        if strict and not occ_t and not desc:
            return False, ["no occupation or description to confirm identity"]
    else:
        hit = [g for g in groups if KW_RE[g].search(occ_t)]
        dhit = [g for g in groups if KW_RE[g].search(desc)]
        if hit:
            ev.append(f"occupation consistent with {'/'.join(cats)}: {', '.join(o for o in occ if o)}")
        elif dhit and (not strict or not occ_t):
            ev.append(f"description consistent with {'/'.join(cats)}: {desc!r}")
        else:
            return False, [f"occupation inconsistent with role {'/'.join(cats)} ({sorted(e.roles)[:3]}): "
                           f"occupations={occ or 'none'}, description={desc!r}"]
    ok, dev = check_life(e, ent)
    return ok, ev + dev


def check_life(e: Entity, ent: dict) -> tuple[bool, list[str]]:
    b, dth = year_of(first_time(ent, "P569")), year_of(first_time(ent, "P570"))
    years = perf_years(e)
    if not years:
        return True, []
    lo, hi = min(years), max(years)
    min_age = 4 if child_role(e.roles) else 12
    if e.etype == "composer":
        if b and b > lo:
            return False, [f"born {b}, after the performance ({lo})"]
        return True, [f"born {b}" if b else "no birth date"]
    if b and lo - b < min_age:
        return False, [f"born {b}: aged {lo - b} at the {lo} performance"]
    if b and hi - b > 100:
        return False, [f"born {b}: would be {hi - b} at the {hi} performance"]
    posthumous_ok = archival(e) or e.categories <= {"theatre"} or e.production_only
    if dth and dth < hi and not posthumous_ok:
        return False, [f"died {dth}, before the {hi} performance"]
    return True, [f"alive at performance ({b or '?'}-{dth or ''}, performed {lo}{'-' + str(hi) if hi != lo else ''})"]


def check_group(wd: WD, e: Entity, ent: dict) -> tuple[bool, list[str]]:
    types = [wd.label(q) or "" for q in claim_qids(ent, "P31")]
    desc = norm(pick_lang(ent.get("descriptions"), ("en", "mul")) or "")
    tt = " ; ".join(norm(t) for t in types)
    if "Q5" in claim_qids(ent, "P31"):
        return False, ["is a human, not an ensemble"]
    if not (ENSEMBLE_TYPE.search(tt) or ENSEMBLE_TYPE.search(desc)):
        return False, [f"instance of {types or 'none'} / description {desc!r} is not an ensemble type"]
    years = perf_years(e)
    inc, dis = year_of(first_time(ent, "P571")), year_of(first_time(ent, "P576"))
    if years and inc and inc > min(years):
        return False, [f"founded {inc}, after the {min(years)} performance"]
    if years and dis and dis < max(years) and not archival(e):
        return False, [f"dissolved {dis}, before the {max(years)} performance"]
    return True, [f"ensemble type: {', '.join(types) or desc}"]


def check_venue(wd: WD, e: Entity, ent: dict) -> tuple[bool, list[str]]:
    types = [wd.label(q) or "" for q in claim_qids(ent, "P31")]
    desc = norm(pick_lang(ent.get("descriptions"), ("en", "mul")) or "")
    tt = " ; ".join(norm(t) for t in types)
    if not (VENUE_TYPE.search(tt) or VENUE_TYPE.search(desc)) or re.search(r"\b(orchestra|human|city|company)\b", tt) and not VENUE_TYPE.search(tt):
        return False, [f"instance of {types or 'none'} / description {desc!r} is not a venue/building type"]
    years = perf_years(e)
    opened = year_of(first_time(ent, "P1619", "P571"))
    closed = year_of(first_time(ent, "P3999", "P576"))
    if years and opened and opened > min(years) + 1:
        return False, [f"opened {opened}, after the {min(years)} performance"]
    if years and closed and closed < max(years) and not archival(e):
        return False, [f"closed/demolished {closed}, before the {max(years)} performance"]
    return True, [f"venue type: {', '.join(types) or desc}" + (f"; opened {opened}" if opened else "")]


def check_work(wd: WD, e: Entity, ent: dict, composer_qid: str | None) -> tuple[bool, list[str]]:
    types = [wd.label(q) or "" for q in claim_qids(ent, "P31")]
    desc = norm(pick_lang(ent.get("descriptions"), ("en", "mul")) or "")
    tt = " ; ".join(norm(t) for t in types)
    comps = claim_qids(ent, "P86")
    if "Q5" in claim_qids(ent, "P31"):
        return False, ["is a human, not a work"]
    if composer_qid and comps and composer_qid not in comps:
        return False, [f"composer (P86) is {[wd.label(c) for c in comps]}, not {e.composer_name} ({composer_qid})"]
    if not (WORK_TYPE.search(tt) or WORK_TYPE.search(desc)):
        return False, [f"instance of {types or 'none'} / description {desc!r} is not a musical work type"]
    ev = [f"work type: {', '.join(types) or desc}"]
    if composer_qid and composer_qid in comps:
        ev.append("composer (P86) matches")
    elif composer_qid and not comps:
        if norm(e.composer_name or "").split()[-1:] and norm(e.composer_name).split()[-1] in desc:
            ev.append("no P86; description names the composer")
        else:
            return False, [f"no composer (P86) and description {desc!r} does not name {e.composer_name}"]
    return True, ev


def desc_supports(e: Entity, ent: dict) -> bool:
    if e.etype not in ("person", "composer"):
        return False
    desc = norm(pick_lang(ent.get("descriptions"), ("en", "mul", "en-gb")) or "")
    groups = set()
    for c in e.categories:
        groups.update(SEARCH_OK.get(c) or [])
    return bool(desc) and any(KW_RE[g].search(desc) for g in groups)


def name_matches(e: Entity, ent: dict) -> bool:
    """Loose identity check for provided person QIDs: some label/alias shares the surname."""
    names = all_names(ent)
    for n in e.names:
        toks = norm(strip_parens(n)).split()
        if not toks:
            continue
        if norm(strip_parens(n)) in names:
            return True
        last = toks[-1]
        if any(last in nm.split() for nm in names):
            return True
    return False


# ----------------------------------------------------------------------------------------------
# Verification and search


async def verify(wd: WD, e: Entity, composer_status: dict) -> None:
    ent = await wd.entity(e.given_qid)
    if ent is None:
        e.status, e.reject_reason = "rejected", f"{e.given_qid} does not exist on Wikidata"
        return
    real_qid = ent["id"]
    await wd.fetch_labels(set(claim_qids(ent, "P31")) | set(claim_qids(ent, "P106")) | set(claim_qids(ent, "P86")))
    if e.etype in ("person", "composer"):
        ok, ev = check_person(wd, e, ent, strict=False)
        if ok and not name_matches(e, ent):
            ok, ev = False, [f"name {sorted(e.names)} does not match label {pick_lang(ent.get('labels'))!r}"]
    elif e.etype == "ensemble":
        ok, ev = check_group(wd, e, ent)
    elif e.etype == "venue":
        ok, ev = check_venue(wd, e, ent)
    else:
        ok, ev = check_work(wd, e, ent, composer_status.get(composer_key(e)))
    if ok:
        e.status, e.qid, e.evidence = "verified", real_qid, ev
        if real_qid != e.given_qid:
            e.evidence.append(f"{e.given_qid} redirects to {real_qid}")
    else:
        e.status, e.reject_reason = "rejected", "; ".join(ev)
        e.rejected_info = summarize_candidate(wd, ent)


def summarize_candidate(wd: WD, ent: dict) -> dict:
    return {"qid": ent["id"], "label": pick_lang(ent.get("labels")),
            "description": pick_lang(ent.get("descriptions"), ("en", "mul", "de", "it", "fr")),
            "instance_of": [wd.label(q) for q in claim_qids(ent, "P31")],
            "occupations": [wd.label(q) for q in claim_qids(ent, "P106")],
            "born": first_time(ent, "P569"), "died": first_time(ent, "P570")}


async def candidates_by_name(wd: WD, names: list[str]) -> list[str]:
    """wbsearchentities hits whose matched label/alias equals one of the names (normalized)."""
    wanted = {norm(n) for n in names if norm(n)}
    out: list[str] = []
    for n in names:
        for lang in ["en"] + FALLBACK_WIKIS:
            hits = await wd.search(n, lang)
            for h in hits:
                texts = {norm(h.get("label", "")), norm(h.get("match", {}).get("text", ""))}
                texts |= {norm(a) for a in h.get("aliases", [])}
                if texts & wanted and h["id"] not in out:
                    out.append(h["id"])
            if out:
                break
    return out


async def search_entity(wd: WD, e: Entity, composer_status: dict) -> None:
    if e.etype == "work":
        return await search_work(wd, e, composer_status)
    names = sorted({strip_parens(n) for n in e.names} | ({re.split(r",", strip_parens(n))[0] for n in e.names}
                                                          if e.etype == "venue" else set()))
    names = [n for n in names if n]
    qids = await candidates_by_name(wd, names)
    passing, cands = [], []
    for q in qids[:12]:
        ent = await wd.entity(q)
        if not ent:
            continue
        await wd.fetch_labels(set(claim_qids(ent, "P31")) | set(claim_qids(ent, "P106")))
        if e.etype in ("person", "composer"):
            ok, ev = check_person(wd, e, ent, strict=True)
        elif e.etype == "ensemble":
            ok, ev = check_group(wd, e, ent)
        else:
            ok, ev = check_venue(wd, e, ent)
        info = summarize_candidate(wd, ent)
        info["verdict"] = ("ok: " if ok else "no: ") + "; ".join(ev)
        cands.append(info)
        if ok:
            passing.append((ent["id"], ev, desc_supports(e, ent)))
    e.candidates = cands
    strong = [p for p in passing if p[2]]
    if len(passing) == 1:
        e.status, e.qid, e.evidence = "matched", passing[0][0], ["unique exact-name search hit"] + passing[0][1]
    elif len(passing) > 1 and len(strong) == 1 and e.etype in ("person", "composer"):
        # duplicates/stubs: accept the only candidate whose *description* also fits the role
        e.status, e.qid = "matched", strong[0][0]
        e.evidence = [f"{len(passing)} exact-name candidates passed; only this one's description fits the role"] + strong[0][1]
    else:
        e.status = "rejected" if e.status == "rejected" else "unmatched"
        e.evidence = [f"{len(passing)} candidates passed checks" if passing else
                      ("no exact-name hits" if not qids else "no candidate passed checks")]


async def propagate_names(wd: WD, ents: dict[tuple, Entity]) -> None:
    """A credit without a QID whose exact name was verified (from a provided QID) elsewhere in the results
    gets that QID, if the date checks still pass (e.g. Charles Anthony: QID given as Borsa, missing as Servant)."""
    known: dict[tuple[str, str], set] = defaultdict(set)
    for e in ents.values():
        if e.status == "verified" and e.etype in ("person", "ensemble"):
            for n in e.names:
                known[(e.etype, norm(n))].add(e.qid)
    for e in ents.values():
        if e.qid or e.given_qid or e.etype not in ("person", "ensemble"):
            continue
        qs = set().union(*(known.get((e.etype, norm(n)), set()) for n in e.names))
        if len(qs) != 1:
            continue
        q = qs.pop()
        ent = await wd.entity(q)
        ok, ev = check_life(e, ent) if e.etype == "person" else check_group(wd, e, ent)
        if ok:
            e.status, e.qid = "matched", q
            e.evidence = ["same name has a verified QID in another result"] + ev


def title_variants(title: str) -> list[tuple[str, str]]:
    """(text, scope) pairs to try, most specific first."""
    t = re.sub(r"\((complete|excerpt|excerpts)\)", "", title, flags=re.I).strip()
    out = [(strip_parens(t), "exact")]
    if ":" in t:
        out.append((strip_parens(t.split(":")[0]), "parent"))
    if "," in strip_parens(t):
        out.append((strip_parens(t).split(",")[0], "parent"))
    seen, res = set(), []
    for v, s in out:
        v = v.strip(" '\"")
        if v and norm(v) not in seen:
            seen.add(norm(v)); res.append((v, s))
    return res


def title_compatible(variant: str, ent: dict) -> str | None:
    """'equal' | 'prefix' | None. Prefix = candidate label is a leading part of the title with the same numbers
    (e.g. 'Symphony No. 4' vs 'Symphony No. 4 in G major')."""
    v = norm(variant)
    vnums = re.findall(r"\d+", v)
    best = None
    for nm in all_names(ent):
        nm2 = norm(re.sub(r"\s*\([^)]*\)", "", nm))
        for cand in {nm, nm2}:
            if not cand:
                continue
            if cand == v:
                return "equal"
            if (v.startswith(cand + " ") or cand.startswith(v + " ")) and re.findall(r"\d+", cand) == vnums[:len(re.findall(r'\d+', cand))] and \
                    sorted(set(re.findall(r"\d+", cand))) == sorted(set(vnums)) :
                best = "prefix"
    return best


async def search_work(wd: WD, e: Entity, composer_status: dict) -> None:
    cq = composer_status.get(composer_key(e))
    if not cq:
        e.evidence = ["composer has no verified QID; works are only searched by composer"]
        return
    cands, found = [], []
    for variant, scope in title_variants(e.name):
        qids = await wd.fulltext(f'{variant} haswbstatement:P86={cq}')
        local = []
        for q in qids[:10]:
            ent = await wd.entity(q)
            if not ent:
                continue
            await wd.fetch_labels(set(claim_qids(ent, "P31")) | set(claim_qids(ent, "P86")))
            comp = title_compatible(variant, ent)
            ok, ev = check_work(wd, e, ent, cq)
            info = summarize_candidate(wd, ent)
            info["verdict"] = f"title={comp}; " + ("ok: " if ok else "no: ") + "; ".join(ev)
            cands.append(info)
            if ok and comp:
                local.append((comp, ent["id"], ev))
        eq = [x for x in local if x[0] == "equal"]
        pick = eq if eq else local
        if len({x[1] for x in pick}) == 1:
            found = [(pick[0][1], scope, pick[0][2], variant)]
            break
        if len(pick) > 1:
            e.evidence = [f"ambiguous: {len(pick)} works by the composer match {variant!r}"]
            break
    e.candidates = cands
    if found:
        q, scope, ev, variant = found[0]
        e.status, e.qid, e.scope = "matched", q, scope
        e.evidence = [f"unique work by composer matching {variant!r} ({scope})"] + ev
    else:
        e.status = "rejected" if e.status == "rejected" else "unmatched"
        if not e.evidence:
            e.evidence = ["no compatible work by this composer found"]


# ----------------------------------------------------------------------------------------------
# Detail fetching


async def wikipedia(http: Http, ent: dict, want_full: bool) -> dict | None:
    links = ent.get("sitelinks", {})
    for lang in ["en"] + FALLBACK_WIKIS:
        sl = links.get(f"{lang}wiki")
        if not sl:
            continue
        title = sl["title"]
        base = f"https://{lang}.wikipedia.org"
        summ = await http.get_json(f"{base}/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}")
        params = {"action": "query", "prop": "extracts", "explaintext": "1", "exsectionformat": "wiki",
                  "titles": title, "redirects": "1", "format": "json", "formatversion": "2"}
        if not want_full:
            params["exintro"] = "1"
        ext = await http.get_json(f"{base}/w/api.php", params)
        text = ""
        pages = (ext or {}).get("query", {}).get("pages", [])
        if pages:
            text = pages[0].get("extract", "") or ""
        intro = re.split(r"\n==[^=]", text, maxsplit=1)[0].strip()
        out = {"lang": lang, "title": title, "url": sl.get("url") or f"{base}/wiki/{quote(title.replace(' ', '_'))}",
               "summary": (summ or {}).get("extract"), "intro": intro or None,
               "thumbnail": ((summ or {}).get("thumbnail") or {}).get("source")}
        if want_full:
            out["synopsis"], out["synopsis_heading"] = synopsis(text)
        return out
    return None


def synopsis(text: str) -> tuple[str | None, str | None]:
    """Extract the Synopsis/Plot section (with its sub-sections) from a plaintext extract."""
    parts = re.split(r"\n(={2,6})\s*(.*?)\s*\1\n", "\n" + text)
    # parts = [lead, marker, heading, body, marker, heading, body, ...]
    secs = [(len(parts[i]), parts[i + 1], parts[i + 2].strip()) for i in range(1, len(parts) - 2, 3)]
    for k, (level, heading, body) in enumerate(secs):
        if level == 2 and re.match(r"(synopsis|plot|story|scenario|summary|roles and synopsis)\b", heading, re.I):
            out = [body] if body else []
            for lvl, h, bd in secs[k + 1:]:
                if lvl <= 2:
                    break
                out.append(f"{h}\n{bd}" if bd else h)
            s = "\n\n".join(out).strip()
            if len(s) > 8000:
                s = s[:8000].rsplit("\n", 1)[0] + "\n…"
            return (s or None), heading
    return None, None


def strip_html(s: str | None) -> str | None:
    if not s:
        return s
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip() or None


async def commons_image(http: Http, filename: str) -> dict:
    fn = filename.replace(" ", "_")
    out = {"file": filename,
           "commons_page": f"https://commons.wikimedia.org/wiki/File:{quote(fn)}",
           "url_600": f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(fn)}?width=600",
           "thumb_url": None, "original_url": None, "width": None, "height": None,
           "artist": None, "license": None, "license_url": None, "credit": None, "attribution_required": None}
    body = await http.get_json(COMMONS_API, {"action": "query", "titles": f"File:{filename}", "prop": "imageinfo",
                                             "iiprop": "url|size|extmetadata", "iiurlwidth": "600",
                                             "format": "json", "formatversion": "2"})
    pages = (body or {}).get("query", {}).get("pages", [])
    if pages and pages[0].get("imageinfo"):
        ii = pages[0]["imageinfo"][0]
        md = ii.get("extmetadata", {})
        g = lambda k: strip_html((md.get(k) or {}).get("value"))  # noqa: E731
        out.update(thumb_url=ii.get("thumburl"), original_url=ii.get("url"), width=ii.get("width"),
                   height=ii.get("height"), artist=g("Artist"), license=g("LicenseShortName"),
                   license_url=g("LicenseUrl"), credit=g("Credit"),
                   attribution_required=(g("AttributionRequired") == "true") if md.get("AttributionRequired") else None)
    return out


def refs(wd: WD, qids: list[str]) -> list[dict]:
    return [{"qid": q, "label": wd.label(q)} for q in qids]


REF_PROPS = ["P31", "P106", "P86", "P87", "P136", "P7937", "P407", "P826", "P4647", "P800", "P131", "P17",
             "P27", "P1303", "P412", "P159", "P740", "P84", "P144", "P361", "P463"]


async def build_record(http: Http, wd: WD, qid: str, etype: str, ents: list[Entity]) -> dict:
    ent = await wd.entity(qid)
    await wd.fetch_labels({q for p in REF_PROPS for q in claim_qids(ent, p)})
    is_work = etype == "work"
    wp = await wikipedia(http, ent, want_full=is_work)
    imgs = [v for v in claim_values(ent, "P18") if isinstance(v, str)]
    if not imgs and etype == "ensemble":
        imgs = [v for v in claim_values(ent, "P154") if isinstance(v, str)]
    image = await commons_image(http, imgs[0]) if imgs else None
    rec = {
        "format_version": FORMAT_VERSION,
        "qid": qid, "type": etype,
        "label": pick_lang(ent.get("labels")),
        "description": pick_lang(ent.get("descriptions"), ("en", "mul", "en-gb")),
        "aliases_en": [a["value"] for a in ent.get("aliases", {}).get("en", [])][:10],
        "wikidata_url": f"https://www.wikidata.org/wiki/{qid}",
        "names_in_results": sorted({n for e in ents for n in e.names}),
        "match": {"status": ents[0].status if len({e.status for e in ents}) == 1 else
                  ("verified" if any(e.status == "verified" for e in ents) else "matched"),
                  "evidence": sorted({x for e in ents for x in e.evidence})},
        "instance_of": refs(wd, claim_qids(ent, "P31")),
        "image": image,
        "wikipedia": wp,
        "fetched_at": now(),
    }
    if etype in ("person", "composer"):
        birth, death = first_time(ent, "P569"), first_time(ent, "P570")
        rec.update(birth=birth, death=death, birth_year=year_of(birth), death_year=year_of(death),
                   occupations=refs(wd, claim_qids(ent, "P106")),
                   voice_type=refs(wd, claim_qids(ent, "P412")),
                   instruments=refs(wd, claim_qids(ent, "P1303")),
                   nationality=refs(wd, claim_qids(ent, "P27")))
        if etype == "person":
            rec["roles_in_results"] = sorted({r for e in ents for r in e.roles})
    if etype == "composer":
        rec["notable_works"] = refs(wd, claim_qids(ent, "P800"))
    if etype == "ensemble":
        inc, dis = first_time(ent, "P571"), first_time(ent, "P576")
        rec.update(inception=inc, dissolved=dis, inception_year=year_of(inc), dissolved_year=year_of(dis),
                   location=refs(wd, claim_qids(ent, "P159") or claim_qids(ent, "P740") or claim_qids(ent, "P131")),
                   roles_in_results=sorted({r for e in ents for r in e.roles}))
    if etype == "venue":
        opened = first_time(ent, "P1619") or first_time(ent, "P571")
        closed = first_time(ent, "P3999") or first_time(ent, "P576")
        coords = next((v for v in claim_values(ent, "P625") if isinstance(v, dict) and "latitude" in v), None)
        cap = next((v for v in claim_values(ent, "P1083") if isinstance(v, dict) and "amount" in v), None)
        rec.update(opened=opened, closed=closed, opened_year=year_of(opened), closed_year=year_of(closed),
                   location=refs(wd, claim_qids(ent, "P131")), country=refs(wd, claim_qids(ent, "P17")),
                   coordinates={"lat": coords["latitude"], "lon": coords["longitude"]} if coords else None,
                   capacity=int(float(cap["amount"])) if cap else None,
                   architect=refs(wd, claim_qids(ent, "P84")),
                   cities_in_results=sorted({e.city for e in ents if e.city}))
    if is_work:
        prem = first_time(ent, "P1191")
        pub = first_time(ent, "P577")
        inc = first_time(ent, "P571")
        cats = []
        for c in ent.get("claims", {}).get("P528", []):
            dv = c.get("mainsnak", {}).get("datavalue")
            if dv:
                cat_q = [q["datavalue"]["value"]["id"] for q in c.get("qualifiers", {}).get("P972", []) if q.get("datavalue")]
                cats.append({"code": dv["value"], "catalog": wd.label(cat_q[0]) if cat_q and cat_q[0] in wd.labels else None})
        rec.update(composers=refs(wd, claim_qids(ent, "P86")), librettists=refs(wd, claim_qids(ent, "P87")),
                   premiere_date=prem, premiere_year=year_of(prem),
                   premiere_place=refs(wd, claim_qids(ent, "P4647")),
                   publication_date=pub, inception=inc,
                   genre=refs(wd, claim_qids(ent, "P136")), form=refs(wd, claim_qids(ent, "P7937")),
                   language=refs(wd, claim_qids(ent, "P407")), key=refs(wd, claim_qids(ent, "P826")),
                   catalogue_codes=cats, part_of=refs(wd, claim_qids(ent, "P361")),
                   based_on=refs(wd, claim_qids(ent, "P144")),
                   scope_in_results=sorted({e.scope for e in ents}),
                   titles_in_results=[{"composer": e.composer_name, "work": n, "scope": e.scope}
                                      for e in ents for n in sorted(e.names)])
    return rec


# ----------------------------------------------------------------------------------------------
# Main


DIRS = {"person": "people", "ensemble": "ensembles", "composer": "composers", "work": "works", "venue": "venues"}


async def run(paths: list[Path]) -> None:
    docs = load_results(paths)
    ents = collect(docs)
    print(f"{len(docs)} result files, {len(ents)} entities", file=sys.stderr)
    http = Http()
    wd = WD(http)
    t0 = time.time()
    try:
        # 1. composers first (works verification depends on them)
        comp = [e for e in ents.values() if e.etype == "composer"]
        await asyncio.gather(*(verify(wd, e, {}) for e in comp if e.given_qid))
        await asyncio.gather(*(search_entity(wd, e, {}) for e in comp if e.status in ("unmatched", "rejected")))
        composer_status = {}
        for e in comp:
            if e.given_qid:
                composer_status[e.given_qid] = e.qid
            for n in e.names:
                composer_status["name:" + norm(n)] = e.qid
        # 2. everything else
        rest = [e for e in ents.values() if e.etype != "composer"]
        await asyncio.gather(*(verify(wd, e, composer_status) for e in rest if e.given_qid))
        await propagate_names(wd, ents)
        await asyncio.gather(*(search_entity(wd, e, composer_status) for e in rest if e.status in ("unmatched", "rejected")))
        # a rejected-then-rematched entity is reported as rejected (with rematch) and its QID is used.
        # 3. fetch details for every resolved QID
        by_qid: dict[tuple[str, str], list[Entity]] = defaultdict(list)
        for e in ents.values():
            if e.qid:
                by_qid[(DIRS[e.etype], e.qid)].append(e)
        records = {}

        async def one(k, es):
            records[k] = await build_record(http, wd, k[1], es[0].etype, es)
        await asyncio.gather(*(one(k, es) for k, es in by_qid.items()))
    finally:
        wd.save_labels()
        await http.client.aclose()

    write_outputs(docs, ents, records)
    print(f"done in {time.time() - t0:.0f}s; http {dict(http.stats)}", file=sys.stderr)


def write_outputs(docs, ents: dict[tuple, Entity], records: dict) -> None:
    for d in DIRS.values():
        (OUT / d).mkdir(parents=True, exist_ok=True)
    keep = defaultdict(set)
    for (d, q), rec in records.items():
        keep[d].add(f"{q}.json")
        (OUT / d / f"{q}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    for d in DIRS.values():  # drop stale files from earlier runs
        for f in (OUT / d).glob("*.json"):
            if f.name not in keep[d]:
                f.unlink()

    index = {"format_version": FORMAT_VERSION, "generated_at": now(),
             "sources": sorted(d["_path"] for d in docs),
             **{d: {} for d in DIRS.values()}, "conflicts": [], "by_performance": {}}
    rejected, unmatched = [], []

    def put(section, key, qid, name):
        cur = index[section].get(key)
        if cur and cur != qid:
            index["conflicts"].append({"section": section, "key": key, "name": name, "qids": sorted({cur, qid})})
        else:
            index[section][key] = qid

    for e in ents.values():
        sec = DIRS[e.etype]
        appears = sorted({m.perf_id for m in e.mentions})
        if e.qid:
            for n in e.names:
                key = work_key(e.composer_name, n) if e.etype == "work" else norm(n)
                put(sec, key, e.qid, n)
        if e.reject_reason:
            rejected.append({"type": e.etype, "name": e.name, "names": sorted(e.names), "given_qid": e.given_qid,
                             "reason": e.reject_reason, "given_qid_is": e.rejected_info,
                             "roles": sorted(e.roles), "composer": e.composer_name, "appears_in": appears,
                             "rematched_qid": e.qid, "rematch_evidence": e.evidence if e.qid else None,
                             "search_candidates": e.candidates})
        if not e.qid:
            unmatched.append({"type": e.etype, "name": e.name, "names": sorted(e.names),
                              "roles": sorted(e.roles), "categories": sorted(e.categories),
                              "composer": e.composer_name, "catalogue": e.catalogue, "appears_in": appears,
                              "given_qid_rejected": e.given_qid if e.reject_reason else None,
                              "reason": "; ".join(e.evidence) or "not searched", "candidates": e.candidates})

    lookup = {}
    for e in ents.values():
        for m in e.mentions:
            lookup[(m.perf_id, m.where)] = e
    for d in docs:
        pid = d["id"]
        bp = {"file": d["_path"], "venue": None, "program": [], "credits": []}
        if (pid, ("venue",)) in lookup:
            e = lookup[(pid, ("venue",))]
            bp["venue"] = {"name": d["performance"].get("venue"), "qid": e.qid, "status": e.status}
        for i, w in enumerate(d.get("program", [])):
            c, wk = lookup[(pid, ("program", i, "composer"))], lookup[(pid, ("program", i, "work"))]
            bp["program"].append({"index": i, "composer": w["composer"], "composer_qid": c.qid,
                                  "composer_status": c.status, "work": w["work"], "work_qid": wk.qid,
                                  "work_status": wk.status, "work_scope": wk.scope if wk.qid else None})
        for i, c in enumerate(d.get("credits", [])):
            e = lookup[(pid, ("credit", i))]
            bp["credits"].append({"index": i, "name": c["name"], "role": c.get("role"), "type": e.etype,
                                  "dir": DIRS[e.etype], "qid": e.qid, "status": e.status})
        index["by_performance"][pid] = bp

    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    (OUT / "rejected.json").write_text(json.dumps({"generated_at": now(), "items": rejected}, ensure_ascii=False, indent=2) + "\n")
    (OUT / "unmatched.json").write_text(json.dumps({"generated_at": now(), "items": unmatched}, ensure_ascii=False, indent=2) + "\n")

    # counts
    counts = defaultdict(lambda: defaultdict(int))
    for e in ents.values():
        counts[e.etype][e.status if not (e.reject_reason and e.qid) else "rejected_rematched"] += 1
        counts[e.etype]["total"] += 1
    stats = {t: dict(c) for t, c in counts.items()}
    stats["files_written"] = {d: len(keep[d]) for d in DIRS.values()}
    (OUT / "stats.json").write_text(json.dumps({"generated_at": now(), "entities": stats}, indent=2) + "\n")
    print(json.dumps(stats, indent=1))


def main() -> None:
    global OUT
    args = sys.argv[1:]
    if "--out" in args:  # write somewhere else (testing); default data/wiki
        i = args.index("--out")
        OUT = Path(args[i + 1]).resolve()
        del args[i:i + 2]
    paths = [Path(a).resolve() for a in args] if args else sorted(
        p for g in RESULT_GLOBS for p in ROOT.glob(g))
    paths = [p for p in paths if p.suffix == ".json"]
    asyncio.run(run(paths))


if __name__ == "__main__":
    main()
