"""Historical context for liner notes (GET /api/performances/{id}/context) -- docs/API.md v0.3.

Reads the files written by pipeline/context (see data/context/README.md):
  data/context/facts/works/<work_id>.json   structured Wikidata facts + Wikipedia source texts
  data/context/notes/works/<work_id>.json   generated, cited paragraphs about the piece's birth
  data/context/notes/performances/<id>.json generated, cited paragraphs about the night of the recording
  data/context/facts/venues/<QID>.json      the house
  data/context/people.json                  birth/death, work period, debut/retirement years

Everything else (season, debuts in the credits, other recordings that season, career stage,
timeline) is computed here from the DB. Every fact carries `cites` (ids into `sources`) and a
`status`: wikidata | wikipedia | research | computed | generated (an LLM paragraph whose
years/numbers were checked against its cited sources: `checked`; else `unchecked`).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from pathlib import Path

from .importer import CONDUCTOR_ROLE, STAFF_ROLE

WAYBACK = "https://web.archive.org/web/{}"
INSTRUMENT = re.compile(r"piano|violin|viol[ae]|cello|bass\b|flute|oboe|clarinet|bassoon|horn|trumpet|trombone|"
                        r"tuba|harp|organ|harpsichord|guitar|percussion|keyboard|fortepiano|lute|recorder|"
                        r"saxophone|soloist", re.I)
DEBUT_NOTE = re.compile(r"\bdebut\w*|\bfarewell|\blast (?:performance|appearance)|\bfinal (?:performance|appearance)|"
                        r"\bfirst (?:performance|appearance)|\bpremiere\b|\bretire", re.I)
FESTIVAL = re.compile(r"festival|festspiele|\bprom\b|\bproms\b|tanglewood|ravinia|aldeburgh|edinburgh|bayreuth|"
                      r"salzburg|glyndebourne|lucerne|verbier", re.I)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
TOPIC_TITLES = {
    "premiere": "The premiere", "reception": "How it was received", "composer_life": "In the composer's life",
    "world": "Meanwhile in the world", "season": "The season", "debuts_farewells": "Debuts, farewells and changes",
    "that_year": "That year",
}


def _j(v, default=None):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def year_of(s) -> int | None:
    m = re.match(r"^(\d{4})", str(s or ""))
    return int(m.group(1)) if m else None


def nice_date(s: str | None) -> str | None:
    """'1857-03-12' -> '12 March 1857'; '1857-03' -> 'March 1857'."""
    if not s:
        return None
    m = re.match(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$", s)
    if not m:
        return s
    y, mo, d = m.groups()
    if mo and not 1 <= int(mo) <= 12:
        return y
    if d:
        return f"{int(d)} {MONTHS[int(mo) - 1]} {y}"
    if mo:
        return f"{MONTHS[int(mo) - 1]} {y}"
    return y


class ContextStore:
    """JSON files under data/context, cached by mtime (the generator writes them while the server runs)."""

    def __init__(self, directory: Path | None):
        self.dir = directory
        self._cache: dict[Path, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def _load(self, rel: str):
        if not self.dir or ".." in rel:
            return None
        p = self.dir / rel
        try:
            m = p.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            hit = self._cache.get(p)
            if hit and hit[0] == m:
                return hit[1]
        try:
            v = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        with self._lock:
            self._cache[p] = (m, v)
        return v

    @staticmethod
    def _safe(ident: str | None) -> bool:
        return bool(ident) and "/" not in ident and not ident.startswith(".")

    def work_facts(self, wid):
        return self._load(f"facts/works/{wid}.json") if self._safe(wid) else None

    def work_note(self, wid):
        return self._load(f"notes/works/{wid}.json") if self._safe(wid) else None

    def performance_note(self, pid):
        return self._load(f"notes/performances/{pid}.json") if self._safe(pid) else None

    def venue_facts(self, qid):
        return self._load(f"facts/venues/{qid}.json") if self._safe(qid) else None

    def people(self) -> dict:
        return ((self._load("people.json") or {}).get("items")) or {}


# ---- career stage --------------------------------------------------------------------------------------

def role_class(role: str | None) -> str:
    r = role or ""
    if CONDUCTOR_ROLE.search(r) or re.search(r"conductor", r, re.I):
        return "conductor"
    if INSTRUMENT.search(r):
        return "instrumentalist"
    return "singer"


# (early if age <, late if age >=) when no debut/retirement year is known
AGE_BANDS = {"singer": (30, 55), "conductor": (38, 70), "instrumentalist": (28, 65)}


def career_stage(p: dict | None, year: int | None, role: str | None) -> dict | None:
    """Where a performer was in their career in `year`, from Wikidata/Wikipedia years. Returns
    {stage: early|peak|late, age, career_start, career_end, basis, text} or None."""
    if not p or not year:
        return None
    cls = role_class(role)
    birth, death = p.get("birth_year"), p.get("death_year")
    if birth and not (birth + 4 <= year <= (death or 9999) + 1):
        return None  # dates don't fit the recording: probably a wrong match, say nothing
    age = year - birth if birth else None
    start = year_of(p.get("work_start")) or p.get("debut_year")
    start_src = "wikidata" if p.get("work_start") else ("wikipedia" if p.get("debut_year") else None)
    if start and year < start:  # a later house debut, not the career start
        start, start_src = None, None
    end = year_of(p.get("work_end")) or p.get("retired_year")
    end_src = "wikidata" if p.get("work_end") else ("wikipedia" if p.get("retired_year") else None)
    if end and end < year:
        end, end_src = None, None
    early_age, late_age = AGE_BANDS[cls]
    bits, basis = [], []
    if age is not None:
        bits.append(f"aged {age}")
        basis.append("wikidata")
    if start:
        n = year - start
        bits.append("in the first year of the career" if n == 0 else
                    f"{n} year{'s' if n != 1 else ''} after the {'debut' if start_src == 'wikipedia' else 'career start'} in {start}")
        basis.append(start_src)
    if end:
        n = end - year
        bits.append(f"{n} year{'s' if n != 1 else ''} before {'retiring' if end_src == 'wikipedia' else 'the end of the career'} in {end}"
                    if n else f"in the year {'of retirement' if end_src == 'wikipedia' else 'the career ended'}")
        basis.append(end_src)
    if not bits:
        return None
    if end is not None and end - year <= 5:
        stage = "late"
    elif start is not None and year - start <= 6:
        stage = "early"
    elif age is not None and age >= late_age:
        stage = "late"
    elif start is None and age is not None and age < early_age:
        stage = "early"
    elif death and death - year <= 3 and age and age >= late_age - 10:
        stage = "late"
    else:
        stage = "peak"
    words = {"early": "early career", "peak": "mid-career, at full stride", "late": "late career"}
    return {"stage": stage, "age": age, "career_start": start, "career_end": end, "role_class": cls,
            "basis": sorted(set(b for b in basis if b)),
            "estimated": start is None and end is None,
            "text": f"{', '.join(bits)}: {words[stage]}" + (" (estimated from age)" if start is None and end is None else "")}


# ---- season ----------------------------------------------------------------------------------------------

def season_label(date: str | None, collection: str | None, event: str | None, venue: str | None) -> str | None:
    y = year_of(date)
    if not y:
        return None
    blob = " ".join(x for x in (collection, event, venue) if x)
    fest = FESTIVAL.search(blob)
    if fest:
        name = {"bayreuth": "Bayreuth Festival", "salzburg": "Salzburg Festival", "proms": "BBC Proms",
                "prom": "BBC Proms"}.get(fest.group(0).lower())
        return f"{name or (event or collection or 'festival')} {y}" if name else f"{y} festival season"
    m = int(date[5:7]) if date and re.match(r"^\d{4}-\d{2}", date) else None
    if m is None:
        return None
    a = y if m >= 8 else y - 1
    return f"{a}–{str(a + 1)[-2:]} season"


def season_window(date: str, festival: bool) -> tuple[str, str]:
    y, m = int(date[:4]), int(date[5:7]) if re.match(r"^\d{4}-\d{2}", date) else 1
    if festival:
        return f"{y}-01-01", f"{y}-12-31"
    a = y if m >= 8 else y - 1
    return f"{a}-08-01", f"{a + 1}-07-31"


# ---- the document ---------------------------------------------------------------------------------------

def _person_ref(row) -> dict:
    return {"id": row["id"], "name": row["name"], "kind": row["kind"],
            "image_url": f"/api/people/{row['id']}/image" if row["image_src"] else None}


def historical_context(conn: sqlite3.Connection, pid: str, store: ContextStore, dates: dict[str, dict]) -> dict | None:
    p = conn.execute("""SELECT p.*, v.name AS venue_name, v.city AS venue_city, v.wikidata AS venue_qid
                        FROM performances p LEFT JOIN venues v ON v.id=p.venue_id WHERE p.id=?""", (pid,)).fetchone()
    if not p:
        return None
    sources: dict[str, dict] = {}
    used: set[str] = set()
    research = [s for s in (_j(p["sources"], []) or []) if isinstance(s, dict) and s.get("id")]
    for s in research:
        sources[s["id"]] = {"id": s["id"], "kind": "research", "title": s.get("title"), "url": s.get("url"),
                            "wayback_url": s.get("wayback_url"), "type": s.get("type"),
                            "date_specific": s.get("date_specific"), "excerpt": s.get("excerpt")}
    date_specific = [s["id"] for s in research if s.get("date_specific")]

    def record():
        sources.setdefault("record", {"id": "record", "kind": "record", "title": f"Sethster research record {pid}",
                                      "url": None, "wayback_url": None, "type": None, "date_specific": None,
                                      "excerpt": "Compiled by the research pipeline; see the other sources."})
        return "record"

    def add_ext(s: dict) -> str:
        sid = s["id"]
        if sid not in sources:
            sources[sid] = {"id": sid, "kind": s.get("kind") or "web", "title": s.get("title"), "url": s.get("url"),
                            "wayback_url": s.get("wayback_url") or (WAYBACK.format(s["url"]) if s.get("url") else None),
                            "type": s.get("type"), "date_specific": s.get("date_specific"),
                            "excerpt": s.get("excerpt")}
        return sid

    def cite(ids) -> list[str]:
        ids = [i for i in dict.fromkeys(ids or []) if i in sources]
        if not ids:
            ids = [record()]
        used.update(ids)
        return ids

    def paragraphs(note: dict | None, ext_sources: list[dict]) -> list[dict]:
        if not note:
            return []
        for s in (note.get("sources") or []) + ext_sources:
            if isinstance(s, dict) and s.get("id") and s.get("kind") != "research":
                add_ext(s)
        out = []
        for topic, sec in (note.get("sections") or {}).items():
            if not isinstance(sec, dict) or not (sec.get("text") or "").strip():
                continue
            cs = [c for c in sec.get("cites") or [] if c in sources]
            if not cs:
                continue
            out.append({"topic": topic, "title": TOPIC_TITLES.get(topic, topic.replace("_", " ").capitalize()),
                        "text": sec["text"].strip(), "cites": cite(cs), "status": "generated",
                        "check": sec.get("check") or "unchecked", "backend": note.get("backend"),
                        "generated_at": note.get("generated_at")})
        return out

    dinfo = dates.get(pid) or {}
    rec_date = dinfo.get("date") or p["date"]
    rec_year = year_of(rec_date)

    # ---- the piece(s): the main program works, in program order
    program = conn.execute("""SELECT g.work_id, g.title, w.wikidata, w.title AS wtitle, c.id AS cid, c.name AS cname
                              FROM program g LEFT JOIN works w ON w.id=g.work_id
                              LEFT JOIN composers c ON c.id=g.composer_id
                              WHERE g.performance_id=? ORDER BY g.ord""", (pid,)).fetchall()
    music = {r[0]: r[1] for r in conn.execute("""SELECT work_id, SUM(duration_s) FROM tracks WHERE performance_id=?
                                                 AND segment='music' GROUP BY work_id""", (pid,))}
    seen, works = set(), []
    for g in program:
        if g["work_id"] and g["work_id"] not in seen:
            seen.add(g["work_id"])
            works.append(g)
    works.sort(key=lambda g: -(music.get(g["work_id"]) or 0))  # stable: biggest works first
    pieces = []
    for g in works[:6]:
        wid = g["work_id"]
        wf = store.work_facts(wid) or {}
        f = wf.get("facts") or {}
        ext = [s for s in wf.get("sources") or [] if isinstance(s, dict)]
        by_id = {s["id"]: s for s in ext}
        wd_id = f"wd:{g['wikidata']}" if g["wikidata"] else None
        facts = []
        if f.get("premiere_date") or f.get("premiere_place"):
            if wd_id in by_id:
                add_ext(by_id[wd_id])
            place = ", ".join(x.get("label") or x.get("qid") or "?" for x in f.get("premiere_place") or [])
            txt = " · ".join(x for x in [nice_date(f.get("premiere_date")), place] if x)
            facts.append({"label": "Premiere", "text": txt, "cites": cite([wd_id]), "status": "wikidata"})
        if f.get("premiere_performers"):
            facts.append({"label": "Premiered by", "status": "wikidata",
                          "text": ", ".join(x.get("label") or x.get("qid") or "?" for x in f["premiere_performers"]),
                          "cites": cite(([add_ext(by_id[wd_id])] if wd_id in by_id else []) +
                                        [add_ext(s) for s in ext if s.get("kind") == "wikipedia"][:1])})
        if f.get("inception") and f.get("inception") != f.get("premiere_date"):
            if wd_id in by_id:
                add_ext(by_id[wd_id])
            facts.append({"label": "Composed", "text": nice_date(f["inception"]), "cites": cite([wd_id]),
                          "status": "wikidata"})
        if f.get("composer_birth") and f.get("year"):
            by = year_of(f["composer_birth"])
            if by and f["year"] > by:
                facts.append({"label": "Composer's age", "text": f"{g['cname']} was about {f['year'] - by}"
                              f" at the {'premiere' if f.get('premiere_date') else 'time'}",
                              "cites": cite([wd_id]) if wd_id in sources else cite([]), "status": "computed"})
        paras = paragraphs(store.work_note(wid), ext)
        if not facts and not paras:
            continue
        pieces.append({"work_id": wid, "title": g["wtitle"] or g["title"],
                       "composer": {"id": g["cid"], "name": g["cname"]},
                       "premiere": {"date": f.get("premiere_date"),
                                    "place": ", ".join(x.get("label") or "" for x in f.get("premiere_place") or []) or None}
                       if (f.get("premiere_date") or f.get("premiere_place")) else None,
                       "facts": facts, "paragraphs": paras})

    # ---- the night of the recording
    rfacts = []
    if rec_date:
        dtxt = nice_date(rec_date)
        if dinfo.get("precision") == "day":
            try:
                dtxt = dt.date.fromisoformat(rec_date).strftime("%A ") + dtxt
            except ValueError:
                pass
        st = dinfo.get("status") or p["date_status"]
        dcite = date_specific if dinfo.get("basis", "").startswith("research") else []
        if (dinfo.get("basis") or "").startswith("folder"):
            sources.setdefault("folder", {"id": "folder", "kind": "record", "title": "Dad's folder label",
                                          "url": None, "wayback_url": None, "type": "folder_name",
                                          "date_specific": True, "excerpt": dinfo.get("folder_label")})
            dcite = ["folder"] + date_specific
        rfacts.append({"label": "Date", "text": f"{dtxt} ({st}; {dinfo.get('precision', 'unknown')} precision)",
                       "cites": cite(dcite), "status": "research"})
        if dinfo.get("aired"):
            rfacts.append({"label": "Aired", "text": nice_date(dinfo["aired"]) + " (the date on Dad's folder)",
                           "cites": cite(date_specific), "status": "research"})
    festival = bool(FESTIVAL.search(" ".join(x for x in (p["collection"], p["event_name"], p["venue_name"]) if x)))
    season = season_label(rec_date, p["collection"], p["event_name"], p["venue_name"])
    if season:
        rfacts.append({"label": "Season", "text": season, "cites": cite([]), "status": "computed"})
    if p["venue_name"]:
        vtxt = p["venue_name"] + (f", {p['venue_city']}" if p["venue_city"] else "")
        vf = store.venue_facts(p["venue_qid"]) or {}
        vc = []
        for s in vf.get("sources") or []:
            vc.append(add_ext(s))
        oy = year_of(vf.get("opened"))
        if oy and rec_year and rec_year >= oy:
            vtxt += f" (opened {oy}; {rec_year - oy} years old that night)"
        rfacts.append({"label": "House", "text": vtxt, "cites": cite(date_specific[:2] + vc), "status": "research"})
    credits = conn.execute("""SELECT c.*, pe.id, pe.name, pe.kind, pe.image_src, pe.wikidata FROM credits c
                              JOIN people pe ON pe.id=c.person_id WHERE c.performance_id=? ORDER BY c.ord""",
                           (pid,)).fetchall()
    for c in credits:
        if c["notes"] and DEBUT_NOTE.search(c["notes"]):
            rfacts.append({"label": c["name"], "text": c["notes"], "cites": cite(_j(c["source_ids"], [])),
                           "status": "research", "person_id": c["id"]})
    others = []
    if rec_date and len(rec_date) >= 7 and p["collection"]:
        lo, hi = season_window(rec_date if len(rec_date) >= 7 else rec_date + "-01", festival)
        for o in conn.execute("""SELECT id, title, date FROM performances WHERE collection=? AND id<>?
                                 AND date >= ? AND date <= ? ORDER BY date""", (p["collection"], pid, lo, hi)):
            others.append({"performance_id": o["id"], "title": o["title"], "date": o["date"]})
    if others:
        rfacts.append({"label": "Also in the collection that season",
                       "text": "; ".join(f"{o['title']} ({nice_date(o['date'])})" for o in others[:8]),
                       "cites": cite([]), "status": "computed"})
    rparas = paragraphs(store.performance_note(pid), [])

    # ---- performers: principals + conductor, with career stage
    people = store.people()
    performers, seenp = [], set()
    for c in sorted(credits, key=lambda c: (c["status"] != "confirmed", c["ord"])):
        role = (c["role"] or "").strip()
        if c["id"] in seenp or c["kind"] == "ensemble" or c["status"] == "production_only":
            continue
        is_cond = bool(CONDUCTOR_ROLE.search(role))
        if STAFF_ROLE.search(role) and not is_cond:
            continue
        seenp.add(c["id"])
        pf = people.get(c["wikidata"]) if c["wikidata"] else None
        stage = career_stage(pf, rec_year, role)
        cs = []
        if stage and pf:
            if "wikidata" in stage["basis"] and pf.get("wikidata_url"):
                cs.append(add_ext({"id": f"wd:{pf['qid']}", "kind": "wikidata", "title": f"Wikidata: {pf['name']}",
                                   "url": pf["wikidata_url"]}))
            if "wikipedia" in stage["basis"] and pf.get("wikipedia_url"):
                cs.append(add_ext({"id": f"wp:person:{pf['qid']}", "kind": "wikipedia",
                                   "title": f"Wikipedia: {pf['name']}", "url": pf["wikipedia_url"],
                                   "excerpt": pf.get("debut_evidence") or pf.get("retired_evidence")}))
        performers.append({"person": _person_ref(c), "role": role or None, "credit_status": c["status"],
                           "stage": stage["stage"] if stage else None, "age": stage["age"] if stage else None,
                           "career_start": stage["career_start"] if stage else None,
                           "career_end": stage["career_end"] if stage else None,
                           "estimated": stage["estimated"] if stage else None,
                           "text": stage["text"] if stage else None,
                           "cites": cite(cs) if stage else [], "status": "computed" if stage else None,
                           "credits_url": f"/api/people/{c['id']}"})
    performers = sorted(performers, key=lambda x: (not CONDUCTOR_ROLE.search(x["role"] or ""),))[:12]

    # ---- timeline: composed, premiere, every version in the collection, this recording
    timelines = []
    for piece in pieces[:3] or [{"work_id": g["work_id"], "title": g["wtitle"] or g["title"]} for g in works[:1]]:
        wid = piece["work_id"]
        wf = store.work_facts(wid) or {}
        f = wf.get("facts") or {}
        ev = []
        wd_id = None
        for s in wf.get("sources") or []:
            if isinstance(s, dict) and s.get("id") == f"wd:{wf.get('qid')}":
                wd_id = add_ext(s)
        if f.get("inception") and f.get("inception") != f.get("premiere_date"):
            ev.append({"kind": "composed", "date": f["inception"], "label": "Composed",
                       "cites": cite([wd_id]), "performance_id": None, "is_current": False})
        if f.get("premiere_date"):
            place = ", ".join(x.get("label") or "" for x in f.get("premiere_place") or [])
            ev.append({"kind": "premiere", "date": f["premiere_date"],
                       "label": "Premiere" + (f", {place}" if place else ""), "cites": cite([wd_id]),
                       "performance_id": None, "is_current": False})
        for v in conn.execute("""SELECT DISTINCT p.id, p.title, p.date, p.collection, v.name AS vname FROM program g
                                 JOIN performances p ON p.id=g.performance_id LEFT JOIN venues v ON v.id=p.venue_id
                                 WHERE g.work_id=?""", (wid,)):
            d = (dates.get(v["id"]) or {}).get("date") or v["date"]
            if not year_of(d):
                continue
            ev.append({"kind": "recording" if v["id"] == pid else "version", "date": d,
                       "label": ("This recording" if v["id"] == pid else (v["vname"] or v["collection"] or v["title"])),
                       "cites": [], "performance_id": v["id"], "is_current": v["id"] == pid})
        ev.sort(key=lambda e: e["date"])
        if ev:
            timelines.append({"work_id": wid, "title": piece["title"], "events": ev})

    items = [s for sid, s in sources.items() if sid in used]
    return {
        "performance_id": pid, "title": p["title"],
        "date": {"date": rec_date, "precision": dinfo.get("precision"), "status": dinfo.get("status"),
                 "reliable": dinfo.get("reliable"), "aired": dinfo.get("aired")},
        "piece": {"title": "When the Piece Was Born", "items": pieces},
        "recording": {"title": "The Night of the Recording", "season": season, "facts": rfacts,
                      "paragraphs": rparas, "same_season": others},
        "performers": {"title": "Where the Performers Were", "items": performers},
        "timeline": {"title": "Timeline", "items": timelines},
        "sources": {"title": "Sources", "items": items},
    }
