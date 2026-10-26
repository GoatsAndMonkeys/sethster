"""Seeded radio over the collection: excerpt catalog, famous-number matching, weighted shuffle with
work-level repeat control. See docs/RADIO.md.

Units are *excerpts* = (track, start_s, end_s): single arias, duets, ensembles, overtures, movements.
Sources, best first:
  1. section maps written by the segmenter (data/sections/{performance_id}.json, docs/sections.md);
  2. tracks that are already a single number (judged from the cleaned track title and the duration).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .importer import CONDUCTOR_ROLE, STAFF_ROLE
from .radio_store import RadioStore, parse_iso
from .text import fold, norm_key

log = logging.getLogger("sethster.radio")

MUSIC_KINDS = {"aria", "duet", "trio", "quartet", "quintet", "sextet", "ensemble", "chorus", "overture", "prelude",
               "intermezzo", "ballet", "march", "orchestral", "scene", "song", "movement", "work", "number", "excerpt",
               "recitative"}
VOCAL_GROUP_KINDS = {"duet", "trio", "quartet", "quintet", "sextet", "ensemble", "chorus"}
SEED_TYPES = ("composer", "person", "ensemble")

MIN_LEN, MAX_LEN, MAX_LEN_FAMOUS = 40.0, 900.0, 1200.0
SECTION_MIN, SECTION_MAX = 30.0, 1200.0

# A cleaned track title that hedges, spans several numbers or is a whole work is not a radio unit.
_REJECT = re.compile(
    r"\b(probabl\w*|possibl\w*|plausibl\w*|likely|perhaps|nearby|mistag\w*|continuation|^end of|conclusion|spoken|, or |or the|\(?start\b|inferred|unidentified|unverified|not identified|not verified|"
    r"unknown|unclear|uncertain|complete|in full|entire|whole|start of|beginning of|to (the )?end of|"
    r"continued|continues|cont\.|remainder|rest of|part [a-z], track|track \d+|segments?|content|"
    r"applause|intermission|interval|announc\w*|host|interview|tuning|silence|sign-off|encore\(s\)|"
    r"acts? [ivx]+ and [ivx]+|acts? \d and \d)\b|\?", re.I)
# bare "Act 1", "Part 2, segment 4", "Act I, Scene 1, first part": a whole act/side, not a number
_BARE_PART = re.compile(r"^\s*(act|akt|part|teil|side|disc|cd|scene|tableau|segment)\b[^'‘’\"“”:]*$", re.I)
_UNIDENTIFIED_WORK = re.compile(r"unidentified|unknown|^untitled", re.I)

_KIND_PATTERNS = [
    ("recitative", r"^[^'‘’]*\b(recitativ\w*|recit)\b(?!.*\b(aria|chorus|duet)\b)"),
    ("overture", r"\b(overture|ouverture|sinfonia\b(?! concertante)|vorspiel zum|ouvertüre)"),
    ("prelude", r"\b(prelude|preludio|prelude|vorspiel)\b"),
    ("intermezzo", r"\b(intermezzo|interlude|entr'acte|entracte|zwischenspiel)\b"),
    ("ballet", r"\b(ballet|ballo|danse|dance|waltz|valse|tanz)\b"),
    ("march", r"\b(march|marcia|marche|marsch)\b"),
    ("chorus", r"\b(chorus|coro|choeur|chor)\b"),
    ("sextet", r"\b(sextet|sestetto)\b"),
    ("quintet", r"\b(quintet|quintetto)\b"),
    ("quartet", r"\b(quartet|quartetto)\b"),
    ("trio", r"\b(trio|terzetto)\b"),
    ("duet", r"\b(duet|duetto|duo)\b"),
    ("ensemble", r"\b(ensemble|finale|concertato|stretta)\b"),
    ("aria", r"\b(aria|cavatina|cabaletta|romanza|romance|scena|monologue|monolog|narration|erzahlung|lied|song|"
             r"canzone|ballata|air)\b"),
    ("movement", r"\b(mvt|movement|satz|i{1,3}\.|iv\.|allegro|adagio|andante|largo|presto|scherzo|menuetto|"
                 r"minuet|rondo|finale|lento|vivace|moderato)\b"),
]
_KIND_RES = [(k, re.compile(p, re.I)) for k, p in _KIND_PATTERNS]


def compact(s: str | None) -> str:
    """norm_key with all spaces removed: 'Vissi d'arte' == 'vissi darte' == 'vissidarte'."""
    return norm_key(s).replace(" ", "")


# ---- famous numbers -------------------------------------------------------------------------------------

@dataclass
class FamousNumber:
    work_id: str
    id: str
    title: str
    type: str
    tier: int
    characters: list[str]
    keys: list[str]

    @property
    def key(self) -> str:
        return f"{self.work_id}/{self.id}"


@dataclass
class FamousWork:
    id: str
    title: str
    surname: str
    keys: list[str]
    numbers: list[FamousNumber]


class FamousIndex:
    def __init__(self, path: Path | None):
        self.path = path
        self.works: list[FamousWork] = []
        self.mtime = 0.0
        self.load()

    def load(self) -> None:
        p = self.path
        if not p or not p.is_file():
            self.works, self.mtime = [], 0.0
            return
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("famous numbers %s unreadable (%s)", p, e)
            return
        works = []
        for w in doc.get("works") or []:
            try:
                wid = w["id"]
                nums = [FamousNumber(wid, n["id"], n["title"], n.get("type") or "number", int(n.get("tier") or 2),
                                     list(n.get("characters") or []),
                                     [k for k in {compact(m) for m in [n["title"], *(n.get("match") or []),
                                                                       *(n.get("aka") or [])]} if len(k) >= 3])
                        for n in w.get("numbers") or []]
                keys = [k for k in {compact(t) for t in [w["work"], *(w.get("aliases") or [])]} if k]
                works.append(FamousWork(wid, w["work"], compact(w.get("composer_surname") or ""), keys, nums))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("famous numbers: bad work entry %r (%s)", w.get("id") if isinstance(w, dict) else w, e)
        self.works = works
        self.mtime = p.stat().st_mtime

    def refresh(self) -> None:
        p = self.path
        if p and p.is_file() and p.stat().st_mtime != self.mtime:
            self.load()

    def match_work(self, work_title: str | None, composer_name: str | None) -> FamousWork | None:
        wt, cn = compact(work_title), compact(composer_name)
        best, best_len = None, 0
        for w in self.works:
            if w.surname and w.surname not in cn:
                continue
            for k in w.keys:
                if k and k in wt and len(k) > best_len:
                    best, best_len = w, len(k)
        return best

    def match_number(self, fw: FamousWork | None, *texts: str | None) -> FamousNumber | None:
        if not fw:
            return None
        hay = " ".join(compact(t) for t in texts if t)
        best, best_len = None, 0
        for n in fw.numbers:
            for k in n.keys:
                if k in hay and len(k) > best_len:
                    best, best_len = n, len(k)
        return best


# ---- excerpts --------------------------------------------------------------------------------------------

@dataclass
class Excerpt:
    id: str
    track_id: str
    performance_id: str
    start_s: float
    end_s: float | None
    duration_s: float | None
    duration_estimated: bool
    title: str
    track_title: str
    kind: str
    work_id: str | None
    work_title: str | None
    work_key: str              # canonical work identity for repeat control
    number_key: str            # canonical number identity (same aria in two performances = same key)
    composer_id: str | None
    composer_name: str | None
    famous: FamousNumber | None
    source: str                # "section" | "track"
    characters: list[str] = field(default_factory=list)
    # person_id -> (role, certainty) ; certainty: featured | present | maybe
    performers: dict[str, tuple[str, str]] = field(default_factory=dict)

    def summary(self) -> dict:
        return {"id": self.id, "track_id": self.track_id, "performance_id": self.performance_id,
                "work_key": self.work_key, "number_key": self.number_key, "composer_id": self.composer_id,
                "performer_ids": [p for p, (_, c) in self.performers.items() if c != "maybe"]}


def guess_kind(*texts: str | None) -> str:
    t = " ".join(x for x in texts if x)
    for k, rx in _KIND_RES:
        if rx.search(t):
            return k
    return "number" if re.search(r"['‘’\"“”]", t) else "excerpt"


def _role_class(role: str | None) -> str:
    """'staff' (not performing), 'group' (conductor/orchestra/chorus: in every number of the work),
    'soloist' (voice type / instrument: in every number), or 'character' (an opera role)."""
    r = (role or "").strip()
    rf = fold(r)
    if not rf:
        return "soloist"
    if CONDUCTOR_ROLE.search(r) or re.search(r"\b(conductor|orchestra|chorus|choir|ensemble|quartet|band|"
                                              r"consort|players|singers|coro|chor)\b", rf):
        if re.search(r"chorus master|choir master|chormeister|chorus director", rf):
            return "staff"
        return "group"
    if re.search(r"piano and director|director and piano|violin and director|music director", rf):
        return "group"
    if STAFF_ROLE.search(r):
        return "staff"
    if re.search(r"\b(soprano|mezzo|alto|contralto|tenor|baritone|bass|countertenor|treble|voice|vocal|"
                 r"piano|pianist|violin|viola|cello|bass|flute|oboe|clarinet|bassoon|horn|trumpet|trombone|tuba|"
                 r"harp|organ|harpsichord|guitar|percussion|timpani|soloist|recorder|lute|fortepiano|narrator|"
                 r"speaker|reciter|saxophone|accordion|mandolin|soloists?)\b", rf) and not re.search(r"\(", r):
        return "soloist"
    return "character"


def _char_match(role: str, chars: list[str]) -> bool:
    rk = compact(re.sub(r"\(.*?\)", "", role))
    if not rk:
        return False
    for c in chars:
        ck = compact(c)
        if ck and (ck == rk or ck in rk or rk in ck):
            return True
    return False


def _role_in_text(role: str, text: str) -> bool:
    name = norm_key(re.sub(r"\(.*?\)", "", role))
    if len(name) < 3:
        return False
    return re.search(rf"\b{re.escape(name)}\b", norm_key(text)) is not None


def _sig(path: Path | None) -> tuple:
    if not path or not path.is_dir():
        return ()
    try:
        return tuple(sorted((f.name, f.stat().st_mtime) for f in path.glob("*.json")))
    except OSError:
        return ()


def load_sections(directory: Path | None) -> dict[str, list[dict]]:
    """performance_id -> sections (docs/sections.md). Malformed files are skipped."""
    out: dict[str, list[dict]] = {}
    if not directory or not directory.is_dir():
        return out
    for f in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            pid = doc.get("performance_id") or f.stem
            secs = [s for s in doc.get("sections") or [] if isinstance(s, dict)]
            if secs:
                out[pid] = secs
        except (OSError, ValueError, AttributeError) as e:
            log.warning("section map %s unreadable (%s)", f, e)
    return out


class Catalog:
    """All radio excerpts, rebuilt when sethster.db, the section maps, the famous list or probed durations change."""

    def __init__(self, db_path: Path, sections_dir: Path | None, famous: FamousIndex, store: RadioStore,
                 media_root: Path | None = None, probe: bool = True):
        self.db_path, self.sections_dir, self.famous, self.store = db_path, sections_dir, famous, store
        self.media_root, self.probe = media_root, probe
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._durations_version = 0
        self._probing = False
        self.excerpts: list[Excerpt] = []
        self.by_id: dict[str, Excerpt] = {}
        self.names: dict[str, dict] = {}   # person/composer id -> {name, kind, image}

    def get(self) -> "Catalog":
        self.famous.refresh()
        try:
            dbm = self.db_path.stat().st_mtime
        except OSError:
            dbm = 0
        key = (dbm, _sig(self.sections_dir), self.famous.mtime, self._durations_version)
        if key != self._key:
            with self._lock:
                if key != self._key:
                    conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
                    conn.row_factory = sqlite3.Row
                    try:
                        self._build(conn)
                    finally:
                        conn.close()
                    self._key = key
        return self

    # -- durations
    def _durations(self, conn) -> tuple[dict[str, tuple[float, bool]], list[sqlite3.Row]]:
        rows = conn.execute("SELECT id, performance_id, duration_s, size, format, path, stream_path FROM tracks").fetchall()
        probed = self.store.durations()
        rate: dict[str, list[float]] = {}
        for r in rows:
            if r["duration_s"] and r["size"] and r["format"] == "mp3":
                rate.setdefault(r["performance_id"], []).append(r["size"] / r["duration_s"])
        out: dict[str, tuple[float, bool]] = {}
        missing = []
        for r in rows:
            if r["duration_s"]:
                out[r["id"]] = (r["duration_s"], False)
                continue
            p = probed.get(r["id"])
            if p and p[0] == r["size"] and p[1]:
                out[r["id"]] = (p[1], False)
                continue
            if r["size"] and r["format"] == "mp3":
                rs = rate.get(r["performance_id"])
                bps = sorted(rs)[len(rs) // 2] if rs else 192000 / 8
                out[r["id"]] = (r["size"] / bps, True)
                if not p:
                    missing.append(r)
        if missing and self.probe and self.media_root and self.media_root.is_dir() and not self._probing:
            self._probing = True
            threading.Thread(target=self._probe, args=(missing,), daemon=True, name="radio-probe").start()
        return out, rows

    def _probe(self, rows) -> None:
        from .mp3info import mp3_duration
        from .media import safe_join
        try:
            batch = []
            for r in rows:
                try:
                    p = safe_join(self.media_root, r["path"])
                except ValueError:       # record it, or every rebuild re-probes it forever
                    batch.append((r["id"], r["size"], None, "bad-path"))
                    continue
                d = mp3_duration(p)
                batch.append((r["id"], r["size"], d, "mp3-header"))
                if len(batch) >= 200:
                    self.store.put_durations(batch); batch = []
            self.store.put_durations(batch)
            log.info("radio: probed %d mp3 durations", len(rows))
        except Exception:  # noqa: BLE001
            log.exception("radio: duration probe failed")
        finally:
            self._durations_version += 1
            self._probing = False

    # -- build
    def _build(self, conn: sqlite3.Connection) -> None:
        durs, track_rows = self._durations(conn)
        works = {w["id"]: w for w in conn.execute(
            "SELECT w.id, w.title, w.wikidata, w.composer_id, c.name AS cname FROM works w "
            "LEFT JOIN composers c ON c.id=w.composer_id")}
        perf_comp = {}
        for r in conn.execute("SELECT performance_id, composer_id FROM performance_composers ORDER BY ord"):
            perf_comp.setdefault(r["performance_id"], r["composer_id"])
        credits: dict[str, list] = {}
        for c in conn.execute("SELECT c.performance_id, c.person_id, c.role, c.work_id, c.status, p.kind "
                              "FROM credits c JOIN people p ON p.id=c.person_id ORDER BY c.ord"):
            credits.setdefault(c["performance_id"], []).append(c)
        tracks = {t["id"]: t for t in conn.execute("SELECT * FROM tracks")}
        by_path = {t["path"]: t for t in tracks.values()}
        sections = load_sections(self.sections_dir)
        fam_cache: dict[str, object] = {}

        def famous_work(wid):
            if wid not in fam_cache:
                w = works.get(wid)
                fam_cache[wid] = self.famous.match_work(w["title"], w["cname"]) if w else None
            return fam_cache[wid]

        def make(eid, t, start, end, dur, est, title, kind, wid, chars, performer_ids, source, extra_text=""):
            w = works.get(wid) if wid else None
            fw = famous_work(wid) if wid else None
            fn = self.famous.match_number(fw, title, extra_text)
            if not fn and w and ":" in (w["title"] or ""):
                # single-number works: "L'elisir d'amore: Act II, 'Una furtiva lagrima'", "Tannhäuser: Overture"
                fn = self.famous.match_number(fw, w["title"].split(":", 1)[1])
            if fn and kind in ("number", "excerpt", "movement", "scene"):
                kind = fn.type
            if fn and not chars:
                chars = list(fn.characters)
            canon = (w["wikidata"] if w and w["wikidata"] else None) or wid or f"perf:{t['performance_id']}"
            work_key = fw.id if fw else canon
            number_key = fn.key if fn else f"{work_key}#{compact(title)[:60]}"
            ex = Excerpt(eid, t["id"], t["performance_id"], start, end, dur, est,
                         fn.title if fn else title, t["title"] or "", kind, wid, w["title"] if w else None,
                         work_key, number_key, (w["composer_id"] if w else None) or perf_comp.get(t["performance_id"]),
                         w["cname"] if w else None, fn, source, chars)
            text = " ".join([t["title"] or "", t["content"] or "", title or ""])
            for c in credits.get(t["performance_id"], []):
                if c["work_id"] and wid and c["work_id"] != wid:
                    continue
                cls = _role_class(c["role"])
                if cls == "staff":
                    continue
                if c["person_id"] in (performer_ids or []):
                    cert = "featured"
                elif cls in ("group", "soloist"):
                    cert = "present"
                elif chars:
                    cert = "featured" if _char_match(c["role"], chars) else None
                else:
                    cert = "featured" if _role_in_text(c["role"], text) else "maybe"
                if cert:
                    prev = ex.performers.get(c["person_id"])
                    if not prev or prev[1] == "maybe":
                        ex.performers[c["person_id"]] = (c["role"], cert)
            return ex

        out: list[Excerpt] = []
        covered: set[str] = set()
        for pid, secs in sections.items():
            for s in secs:
                kind = (s.get("kind") or "").lower()
                if kind not in MUSIC_KINDS or s.get("continues"):
                    continue
                if s.get("confidence") is not None and float(s["confidence"]) < 0.5:
                    continue
                t = tracks.get(s.get("track_id") or "") or by_path.get(s.get("file") or "")
                if not t or t["performance_id"] != pid:
                    continue
                start = float(s.get("start_s") or 0)
                end = s.get("end_s")
                full = durs.get(t["id"], (None, True))
                end_v = float(end) if end is not None else full[0]
                length = (end_v - start) if end_v is not None else None
                covered.add(t["id"])
                if length is None or not (SECTION_MIN <= length <= SECTION_MAX):
                    continue
                out.append(make(f"s:{pid}:{s.get('id')}", t, start, float(end) if end is not None else None,
                                length, end is None and full[1], s.get("title") or t["title"] or "", kind,
                                s.get("work_id") or t["work_id"], list(s.get("characters") or []),
                                list(s.get("performer_ids") or []), "section"))
        for t in tracks.values():
            if t["id"] in covered or t["segment"] != "music" or not t["work_id"]:
                continue
            if _UNIDENTIFIED_WORK.search(t["work_id"]):
                continue
            title = t["title"] or ""
            if not title or _REJECT.search(title) or _BARE_PART.search(title):
                continue
            d, est = durs.get(t["id"], (None, True))
            if d is None:
                continue
            w = works.get(t["work_id"])
            ex = make(f"t:{t['id']}", t, 0.0, None, d, est, title, guess_kind(title), t["work_id"], [], [], "track",
                      extra_text="")
            limit = MAX_LEN_FAMOUS if ex.famous else MAX_LEN
            if not (MIN_LEN <= d <= limit):
                continue
            if w and ex.title == title and ":" in (w["title"] or "") and not re.search(r"['‘’]", title):
                # single-number works ("L'elisir d'amore: Act II, 'Una furtiva lagrima'") read better by work title
                ex.title = w["title"].split(":", 1)[1].strip(" ,")
            out.append(ex)
        self.excerpts = out
        self.by_id = {e.id: e for e in out}
        names = {}
        for r in conn.execute("SELECT id, name, kind, image_src FROM people"):
            names[r["id"]] = {"name": r["name"], "type": "ensemble" if r["kind"] == "ensemble" else "person",
                              "image_url": f"/api/people/{r['id']}/image" if r["image_src"] else None}
        for r in conn.execute("SELECT id, name, years, image_src FROM composers"):
            names[r["id"]] = {"name": r["name"], "type": "composer", "years": r["years"],
                              "image_url": f"/api/composers/{r['id']}/image" if r["image_src"] else None}
        self.names = names
        log.info("radio catalog: %d excerpts (%d from section maps, %d famous)", len(out),
                 sum(e.source == "section" for e in out), sum(e.famous is not None for e in out))

    # -- seeds
    def matches(self, ex: Excerpt, seed: dict) -> float:
        """0 = not on this station; otherwise a relevance multiplier."""
        typ, sid = seed.get("type"), seed.get("id")
        if typ == "composer":
            return 1.0 if ex.composer_id == sid else 0.0
        p = ex.performers.get(sid)
        if not p:
            return 0.0
        return {"featured": 1.0, "present": 0.8, "maybe": 0.3}[p[1]]

    def seed_options(self, q: str | None = None, typ: str | None = None, limit: int = 50) -> list[dict]:
        counts: dict[tuple[str, str], int] = {}
        famous: dict[tuple[str, str], int] = {}
        roles: dict[str, dict[str, int]] = {}
        for e in self.excerpts:
            keys = []
            if e.composer_id:
                keys.append(("composer", e.composer_id))
            for pid, (role, cert) in e.performers.items():
                n = self.names.get(pid)
                if n and cert != "maybe":
                    keys.append((n["type"], pid))
                    roles.setdefault(pid, {})
                    roles[pid][role] = roles[pid].get(role, 0) + 1
            for k in keys:
                counts[k] = counts.get(k, 0) + 1
                if e.famous:
                    famous[k] = famous.get(k, 0) + 1
        toks = [t for t in norm_key(q).split() if t]
        out = []
        for (t, i), n in counts.items():
            if typ and t != typ:
                continue
            nm = self.names.get(i)
            if not nm:
                continue
            if toks and not all(tok in norm_key(nm["name"]) for tok in toks):
                continue
            top_role = max(roles.get(i, {"": 0}).items(), key=lambda kv: kv[1])[0] or None
            out.append({"type": t, "id": i, "name": nm["name"], "image_url": nm.get("image_url"),
                        "subtitle": nm.get("years") if t == "composer" else top_role,
                        "excerpt_count": n, "famous_count": famous.get((t, i), 0)})
        out.sort(key=lambda d: (-(d["excerpt_count"] + 2 * d["famous_count"]), d["name"]))
        return out[:limit]


# ---- stations --------------------------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "work_cooldown_tracks": 6,      # no excerpt from the same (canonical) work within N tracks ...
    "work_cooldown_hours": 2.0,     # ... or N hours
    "number_cooldown_tracks": 40,   # the same number (any performance) not within N tracks ...
    "number_cooldown_hours": 24.0,  # ... or N hours
    "highlights_only": False,       # only excerpts on the famous-numbers list
}


def station_id_for(seeds: list[dict]) -> str:
    norm = sorted({(s["type"], s["id"]) for s in seeds})
    if len(norm) == 1:
        return f"{norm[0][0]}:{norm[0][1]}"
    return "mix:" + hashlib.sha1(json.dumps(norm).encode()).hexdigest()[:12]


def station_name(seeds: list[dict], names: dict[str, dict]) -> str:
    labels = [names.get(s["id"], {}).get("name") or s["id"] for s in seeds]
    if len(labels) == 1:
        return f"{labels[0]} Radio"
    return " + ".join(labels[:3]) + (" …" if len(labels) > 3 else "") + " Radio"


@dataclass
class Pick:
    excerpt: Excerpt
    weight: float
    reasons: list[str]


def _tier_weight(ex: Excerpt) -> tuple[float, str | None]:
    if ex.famous:
        return {1: 4.0, 2: 2.5, 3: 1.6}.get(ex.famous.tier, 2.0), f"famous {ex.famous.type}"
    if ex.kind in VOCAL_GROUP_KINDS:
        return 1.2, None
    if ex.kind in ("aria", "overture", "prelude", "number"):
        return 1.0, None
    if ex.kind in ("movement", "excerpt"):
        return 0.8, None
    if ex.kind == "recitative":
        return 0.3, None
    return 0.9, None


STRONG_ENOUGH = 10   # a performer seed with this many sure excerpts drops the "maybe" ones


def candidates(cat: Catalog, seeds: list[dict], settings: dict) -> list[tuple[Excerpt, float]]:
    strong = {s["id"]: sum(1 for e in cat.excerpts if cat.matches(e, s) >= 0.8) for s in seeds}

    def rel_for(e, s):
        m = cat.matches(e, s)
        if 0 < m < 0.8 and strong[s["id"]] >= STRONG_ENOUGH:
            return 0.0
        return m * float(s.get("weight", 1.0))

    out = []
    for e in cat.excerpts:
        rel = max((rel_for(e, s) for s in seeds), default=0.0)
        if rel <= 0:
            continue
        if settings.get("highlights_only") and not e.famous:
            continue
        out.append((e, rel))
    return out


def pick_next(cat: Catalog, seeds: list[dict], history: list[dict], stats: dict, *, count: int = 1,
              exclude: list[str] | None = None, settings: dict | None = None, rng: random.Random | None = None,
              now=None) -> list[Pick]:
    """Weighted shuffle. `history` is newest first (RadioStore.history). `exclude` = excerpt ids already queued
    on the client (treated as the most recent plays for repeat control)."""
    import datetime as dt
    st = {**DEFAULT_SETTINGS, **(settings or {})}
    rng = rng or random.Random()
    now = now or dt.datetime.now(dt.timezone.utc)
    pool = candidates(cat, seeds, st)
    if not pool:
        return []
    seed_ids = {s["id"] for s in seeds}
    seed_composers = {s["id"] for s in seeds if s["type"] == "composer"}
    # recent = queued (excluded) first, then history newest first
    recent: list[dict] = []
    for eid in reversed(exclude or []):
        e = cat.by_id.get(eid)
        if e:
            recent.append({**e.summary(), "excerpt_id": eid, "started_at": None})
    recent += history
    ex_stats = stats.get("excerpts", {})
    perf_stats = stats.get("performances", {})
    picks: list[Pick] = []
    queued = set(exclude or [])

    def blocked(e: Excerpt, rec: list[dict], scale: tuple[float, float]) -> bool:
        ws, ns = scale
        wt, wh = int(st["work_cooldown_tracks"] * ws), st["work_cooldown_hours"] * ws
        nt, nh = int(st["number_cooldown_tracks"] * ns), st["number_cooldown_hours"] * ns
        for i, h in enumerate(rec):
            ts = parse_iso(h.get("started_at"))
            age_h = (now - ts).total_seconds() / 3600 if ts else 0.0
            if h.get("excerpt_id") == e.id and i < max(nt, 1):
                return True
            if h.get("number_key") == e.number_key and (i < nt or age_h < nh):
                return True
            if h.get("work_key") == e.work_key and (i < wt or age_h < wh):
                return True
            if i >= max(wt, nt) and age_h >= max(wh, nh):
                break
        return False

    for _ in range(count):
        chosen_ids = {p.excerpt.id for p in picks}
        rec = [{**p.excerpt.summary(), "excerpt_id": p.excerpt.id, "started_at": None} for p in reversed(picks)] + recent
        avail: list[tuple[Excerpt, float]] = []
        relaxed = None
        # small station: relax the work window first, then the number window
        for scale in ((1.0, 1.0), (0.5, 1.0), (0.0, 1.0), (0.0, 0.5), (0.0, 0.25), (0.0, 0.0)):
            avail = [(e, r) for e, r in pool if e.id not in chosen_ids and not blocked(e, rec, scale)
                     and (scale != (0.0, 0.0) or e.id not in queued)]
            if avail:
                relaxed = scale
                break
        if not avail:
            last = rec[0]["excerpt_id"] if rec else None
            avail = ([(e, r) for e, r in pool if e.id != last and e.id not in chosen_ids]
                     or [(e, r) for e, r in pool if e.id != last] or pool)
            relaxed = (-1.0, -1.0)
        weighted = []
        recent5 = rec[:5]
        for e, rel in avail:
            w, why = _tier_weight(e)
            reasons = [why] if why else []
            w *= rel
            # recency: performance / performers / composer heard lately
            same_perf = sum(1 for h in recent5 if h.get("performance_id") == e.performance_id)
            w *= 0.45 ** same_perf
            perf_ids = {p for p, (_, c) in e.performers.items() if c != "maybe"} - seed_ids
            for h in recent5:
                if perf_ids & set(h.get("performer_ids") or []):
                    w *= 0.75
            if e.composer_id not in seed_composers:
                w *= 0.6 ** sum(1 for h in rec[:4] if h.get("composer_id") == e.composer_id)
            # deep cuts: rarely played recordings and excerpts get a boost
            n_perf = perf_stats.get(e.performance_id, 0)
            boost = 1.0 + 1.0 / (1.0 + n_perf)
            s = ex_stats.get(e.id)
            if not s:
                reasons.append("never played")
            else:
                boost *= 1.0 / math.sqrt(1.0 + s["plays"])
                # play-through vs skip feedback (Beta(1,1) prior: no data = 0.5 -> x1.0)
                ratio = (s["finished"] + 1) / (s["finished"] + s["skipped"] + 2)
                w *= 0.4 + 1.2 * ratio
            w *= boost
            weighted.append((e, max(w, 1e-6), reasons))
        total = sum(w for _, w, _ in weighted)
        x = rng.random() * total
        for e, w, reasons in weighted:
            x -= w
            if x <= 0:
                break
        if relaxed is not None and relaxed != (1.0, 1.0):
            reasons = reasons + ["repeat window relaxed (small station)"]
        picks.append(Pick(e, w, reasons))
    return picks
