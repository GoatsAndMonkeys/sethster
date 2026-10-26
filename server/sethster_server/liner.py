"""Phase 3: transcripts, program-note docs, versions and the liner-notes document (docs/API.md v0.2)."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path, PurePosixPath

from .importer import CONDUCTOR_ROLE, STAFF_ROLE
from .text import fold, norm_key, surname

log = logging.getLogger("sethster.liner")


def _j(v, default=None):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _person_ref(row) -> dict:
    return {"id": row["id"], "name": row["name"], "kind": row["kind"],
            "image_url": f"/api/people/{row['id']}/image" if row["image_src"] else None}


def _col(row, key):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


# ---- transcripts --------------------------------------------------------------------------------------

class TranscriptStore:
    """Reads data/transcripts/<id>.json on demand (cached by mtime; files are being written live)."""

    def __init__(self, directory: Path | None):
        self.dir = directory
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def raw(self, pid: str) -> dict | None:
        if not self.dir or "/" in pid or pid.startswith("."):
            return None
        p = self.dir / f"{pid}.json"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            hit = self._cache.get(pid)
            if hit and hit[0] == mtime:
                return hit[1]
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("transcript %s unreadable (%s)", p, e)
            return None
        if not isinstance(doc, dict) or not isinstance(doc.get("segments"), list):
            return None
        with self._lock:
            self._cache[pid] = (mtime, doc)
        return doc

    def mapped(self, conn: sqlite3.Connection, pid: str) -> dict | None:
        doc = self.raw(pid)
        if doc is None:
            return None
        tracks = conn.execute("SELECT id, idx, path, stream_path FROM tracks WHERE performance_id=? ORDER BY idx",
                              (pid,)).fetchall()
        if not tracks:
            return None
        by_path: dict[str, tuple] = {}
        by_base: dict[str, list] = {}
        by_stem: dict[str, list] = {}
        for t in tracks:
            for p in {t["path"], t["stream_path"]} - {None}:
                by_path[p] = (t["id"], t["idx"])
                pp = PurePosixPath(p)
                by_base.setdefault(pp.name, []).append((t["id"], t["idx"]))
                by_stem.setdefault(str(pp.with_suffix("")), []).append((t["id"], t["idx"]))

        def find(f: str):
            if f in by_path:
                return by_path[f]
            for table, key in ((by_stem, str(PurePosixPath(f).with_suffix(""))), (by_base, PurePosixPath(f).name)):
                c = list(dict.fromkeys(table.get(key, [])))
                if len(c) == 1:
                    return c[0]
            return None

        segs, unmapped, langs = [], 0, Counter()
        for s in doc["segments"]:
            if not isinstance(s, dict) or not (s.get("text") or "").strip():
                continue
            hit = find(s.get("file") or "")
            if not hit:
                unmapped += 1
                continue
            lang = s.get("lang") or s.get("language")
            if lang:
                langs[lang] += 1
            segs.append({"track_id": hit[0], "track_index": hit[1], "start": s.get("start"), "end": s.get("end"),
                         "text": s["text"].strip(), "language": lang, "confidence": s.get("avg_logprob")})
        segs.sort(key=lambda x: (x["track_index"], x["start"] or 0.0))
        return {"performance_id": pid, "model": doc.get("model"),
                "language": langs.most_common(1)[0][0] if langs else None,
                "segments": segs, "unmapped_segments": unmapped}


# ---- program-note docs -----------------------------------------------------------------------------------

FOOTER = re.compile(
    r"Source:\s*(?P<title>.+?),\s*(?P<url>https?://\S+?)\s*[;,]\s*archived copy\s*(?P<wb>https?://\S+?)"
    r"\s*(?:\(captured\s*(?P<cap>[^)]+)\))?\s*\.?\s*\*?\s*$", re.I | re.S)
URL = re.compile(r"https?://[^\s)>\]*]+")


def _parse_footer(md: str) -> dict:
    out = {"title": None, "url": None, "wayback_url": None, "captured": None}
    idx = md.rfind("Source:")
    if idx < 0:
        return out
    tail = md[idx:].strip()
    m = FOOTER.search(tail)
    if m:
        out.update(title=m["title"].strip(" *_"), url=m["url"].rstrip(".,;"), wayback_url=m["wb"].rstrip(".,;"),
                   captured=(m["cap"] or "").strip() or None)
        return out
    urls = [u.rstrip(".,;") for u in URL.findall(tail)]
    out["wayback_url"] = next((u for u in urls if "web.archive.org" in u), None)
    out["url"] = next((u for u in urls if "web.archive.org" not in u), None)
    t = re.match(r"Source:\s*([^,\n]+)", tail)
    out["title"] = t.group(1).strip(" *_") if t else None
    c = re.search(r"captured\s*([^)\n]+)\)", tail)
    out["captured"] = c.group(1).strip() if c else None
    return out


class ProgramStore:
    def __init__(self, directory: Path | None):
        self.dir = directory

    def _dir(self, pid: str) -> Path | None:
        if not self.dir or "/" in pid or pid.startswith("."):
            return None
        d = self.dir / pid
        return d if d.is_dir() else None

    def numbers(self, pid: str) -> list[int]:
        d = self._dir(pid)
        if not d:
            return []
        return sorted(int(p.stem) for p in d.glob("*.md") if p.stem.isdigit())

    def get(self, pid: str, n: int) -> dict | None:
        d = self._dir(pid)
        if not d:
            return None
        p = d / f"{n}.md"
        try:
            md = p.read_text(encoding="utf-8")
        except OSError:
            return None
        src = _parse_footer(md)
        side = d / f"{n}.json"
        if side.exists():
            try:
                extra = json.loads(side.read_text(encoding="utf-8"))
                if isinstance(extra, dict):
                    for k in src:
                        if extra.get(k):
                            src[k] = extra[k]
                    if not extra.get("wayback_url") and extra.get("wayback"):
                        src["wayback_url"] = extra["wayback"]
            except (OSError, ValueError):
                pass
        h = re.search(r"^#\s+(.+)$", md, re.M)
        return {"performance_id": pid, "n": n, "title": h.group(1).strip() if h else f"Program note {n}",
                "markdown": md, "source": src}

    def refs(self, pid: str) -> list[dict]:
        out = []
        for n in self.numbers(pid):
            doc = self.get(pid, n)
            if doc:
                out.append({"n": n, "title": doc["title"], "url": f"/api/programs/{pid}/{n}",
                            "source": doc["source"], "cites": [f"program:{n}"]})
        return out


# ---- versions ------------------------------------------------------------------------------------------

VERSION_SORTS = {
    "date": lambda r: (r["date"] is None, r["date"] or ""),
    "-date": None, "duration": lambda r: (r["duration_s"] is None, r["duration_s"] or 0),
    "-duration": None, "confidence": lambda r: (r["confidence"] is None, r["confidence"] or 0),
    "-confidence": None,
    "venue": lambda r: (r["venue"] is None, fold((r["venue"] or {}).get("name") or "")),
    "conductor": lambda r: (r["conductor"] is None, fold(surname((r["conductor"] or {}).get("name") or ""))),
}


def sort_versions(rows: list[dict], sort: str) -> list[dict]:
    if sort not in VERSION_SORTS:
        raise ValueError(sort)
    if sort.startswith("-"):
        key = VERSION_SORTS[sort[1:]]
        nulls = [r for r in rows if key(r)[0]]
        rest = sorted((r for r in rows if not key(r)[0]), key=key, reverse=True)
        return rest + nulls
    return sorted(rows, key=VERSION_SORTS[sort])


def version_rows(conn: sqlite3.Connection, wid: str, current: str | None = None) -> list[dict]:
    perfs = conn.execute(
        """SELECT DISTINCT p.*, v.name AS venue_name, v.city AS venue_city FROM program g
           JOIN performances p ON p.id=g.performance_id LEFT JOIN venues v ON v.id=p.venue_id
           WHERE g.work_id=?""", (wid,)).fetchall()
    rows = []
    for p in perfs:
        pid = p["id"]
        n_works = conn.execute("SELECT COUNT(DISTINCT work_id) FROM program WHERE performance_id=?", (pid,)).fetchone()[0]
        credits = conn.execute(
            """SELECT c.role, c.status, c.work_id, pe.id, pe.name, pe.kind, pe.image_src FROM credits c
               JOIN people pe ON pe.id=c.person_id WHERE c.performance_id=? ORDER BY c.ord""", (pid,)).fetchall()
        mine = [c for c in credits if c["work_id"] == wid or c["work_id"] is None or n_works <= 1]
        conductor, singers, seen = None, [], set()
        for c in sorted(mine, key=lambda c: c["status"] != "confirmed"):
            role = (c["role"] or "").strip()
            if c["status"] == "production_only":
                continue
            if conductor is None and CONDUCTOR_ROLE.search(role):
                conductor = _person_ref(c)
                continue
            if c["kind"] == "ensemble" or STAFF_ROLE.search(role) or c["id"] in seen:
                continue
            seen.add(c["id"])
            singers.append({"person": _person_ref(c), "role": role or None})
        # keep the research order (principals first) among the picked singers
        order = {c["id"]: i for i, c in enumerate(mine)}
        singers = sorted(singers, key=lambda s: order.get(s["person"]["id"], 0))[:4]
        dur = conn.execute("""SELECT SUM(duration_s) FROM tracks WHERE performance_id=? AND work_id=?
                              AND segment='music'""", (pid, wid)).fetchone()[0]
        if not dur:
            dur = p["duration_s"]
        conf = round(sum(1 for c in mine if c["status"] == "confirmed") / len(mine), 2) if mine else None
        rows.append({
            "performance_id": pid, "title": p["title"], "date": p["date"], "date_status": p["date_status"],
            "venue": {"id": p["venue_id"], "name": p["venue_name"], "city": p["venue_city"]} if p["venue_id"] else None,
            "conductor": conductor, "lead_singers": singers,
            "duration_s": int(round(dur)) if dur else None, "confidence": conf,
            "kind": p["kind"], "collection": p["collection"],
            "artwork_url": f"/api/performances/{pid}/artwork" if p["artwork_path"] else None,
            "is_current": pid == current,
        })
    return sort_versions(rows, "date")


# ---- liner notes ---------------------------------------------------------------------------------------

CAST_CHANGE = re.compile(
    r"\breplac\w*|\bindispos\w*|\bin place of\b|\bstepp\w* in\b|\bsubstitut\w*|\bcancel\w*|\bwithdr[ae]w\w*|"
    r"\b(?:was|is|fell|been) ill\b|\billness\b|\bunwell\b|\bstand-?in\b|\bcover(?:ed|ing)? for\b|\bunderstud\w*",
    re.I)
DATE_TALK = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}"
    r"(st|nd|rd|th)?\b|\b(19|20)\d\d\b|\bwe are live\b|\blive from\b", re.I)
WELCOME = re.compile(r"\bwelcome to\b|\bthis is the\b.*\bbroadcast\b|\bintermission\b|\bour (host|guest)\b", re.I)
SID = re.compile(r"\bs\d+\b")
WIKI_WAYBACK = "https://web.archive.org/web/{}"


def _sentences_with(text: str | None, pat: re.Pattern) -> list[str]:
    if not text:
        return []
    parts = re.split(r"(?<=[.;!?])\s+(?=[A-Z(])", text)
    return [p.strip() for p in parts if pat.search(p)]


def _ref_ids(text: str | None, known: set[str]) -> list[str]:
    return [s for s in dict.fromkeys(SID.findall(text or "")) if s in known]


def liner_notes(conn: sqlite3.Connection, pid: str, transcripts: TranscriptStore, programs: ProgramStore) -> dict | None:
    p = conn.execute("""SELECT p.*, v.name AS venue_name, v.city AS venue_city, v.wikidata AS venue_qid,
                               v.wikipedia_url AS venue_wp FROM performances p
                        LEFT JOIN venues v ON v.id=p.venue_id WHERE p.id=?""", (pid,)).fetchone()
    if not p:
        return None
    sources: dict[str, dict] = {}
    research = _j(p["sources"], []) or []
    for s in research:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sources[s["id"]] = {"id": s["id"], "kind": "research", "title": s.get("title"), "url": s.get("url"),
                            "wayback_url": s.get("wayback_url"), "type": s.get("type"),
                            "date_specific": s.get("date_specific"), "excerpt": s.get("excerpt")}
    known = set(sources)
    date_specific = [s["id"] for s in research if isinstance(s, dict) and s.get("date_specific") and s.get("id")]
    used: set[str] = set()

    def record_src():
        sources.setdefault("record", {"id": "record", "kind": "record",
                                      "title": f"Sethster research record {pid}", "url": None, "wayback_url": None,
                                      "type": None, "date_specific": None,
                                      "excerpt": "Compiled by the research pipeline from the sources listed here "
                                                 "and the recording itself."})
        return "record"

    def cite(ids) -> list[str]:
        ids = [i for i in dict.fromkeys(ids or []) if i in sources]
        if not ids:
            ids = [record_src()]
        used.update(ids)
        return ids

    def wiki_src(qid: str | None, title: str, wp_url: str | None) -> str | None:
        if not qid and not wp_url:
            return None
        sid = f"wiki:{qid or norm_key(title)}"
        if sid not in sources:
            if wp_url:
                sources[sid] = {"id": sid, "kind": "wikipedia", "title": f"Wikipedia: {title}", "url": wp_url,
                                "wayback_url": WIKI_WAYBACK.format(wp_url), "type": None, "date_specific": None,
                                "excerpt": None, "wikidata_url": f"https://www.wikidata.org/wiki/{qid}" if qid else None}
            else:
                u = f"https://www.wikidata.org/wiki/{qid}"
                sources[sid] = {"id": sid, "kind": "wikidata", "title": f"Wikidata: {title} ({qid})", "url": u,
                                "wayback_url": WIKI_WAYBACK.format(u), "type": None, "date_specific": None,
                                "excerpt": None}
        return sid

    # --- The Performance
    facts = []
    status = p["date_status"] or "unknown"
    if p["date"]:
        dcites = _ref_ids(p["date_evidence"], known) or date_specific
        facts.append({"label": "Date", "text": p["date"] + ("" if status == "confirmed" else f" ({status})"),
                      "cites": cite(dcites)})
    if p["venue_name"]:
        vtxt = p["venue_name"] + (f", {p['venue_city']}" if p["venue_city"] else "")
        vc = date_specific[:3] or []
        ws = wiki_src(p["venue_qid"], p["venue_name"], p["venue_wp"])
        facts.append({"label": "Venue", "text": vtxt, "cites": cite(vc + ([ws] if ws else []))})
    if p["broadcaster"] or p["broadcast_date"]:
        txt = ", ".join(x for x in [p["broadcaster"], p["broadcast_date"] and f"broadcast {p['broadcast_date']}"] if x)
        facts.append({"label": "Broadcast", "text": txt, "cites": cite(_ref_ids(p["date_evidence"], known) or date_specific)})
    if p["event_name"]:
        facts.append({"label": "Event", "text": p["event_name"], "cites": cite(date_specific)})
    rel = _j(p["release"])
    if isinstance(rel, dict) and any(rel.values()):
        facts.append({"label": "Release", "text": " · ".join(str(rel[k]) for k in ("label", "catalogue", "year") if rel.get(k)),
                      "cites": cite([s["id"] for s in research if isinstance(s, dict) and s.get("id")
                                     and re.search(r"discogs|release|label|cd", f"{s.get('type')} {s.get('title')}", re.I)])})
    if p["summary"]:
        facts.append({"label": "Summary", "text": p["summary"], "cites": cite(_ref_ids(p["summary"], known) or date_specific)})
    if p["date_evidence"]:
        facts.append({"label": "Date evidence", "text": p["date_evidence"],
                      "cites": cite(_ref_ids(p["date_evidence"], known) or date_specific)})

    credits = conn.execute("""SELECT c.*, pe.name AS pname FROM credits c JOIN people pe ON pe.id=c.person_id
                              WHERE c.performance_id=? ORDER BY c.ord""", (pid,)).fetchall()
    cast_changes, seen_txt = [], set()
    for c in credits:
        if c["notes"] and CAST_CHANGE.search(c["notes"]):
            cast_changes.append({"label": c["pname"], "text": c["notes"],
                                 "cites": cite(_j(c["source_ids"], []) or _ref_ids(c["notes"], known))})
            seen_txt.add(c["notes"])
    for field in ("summary", "date_evidence"):
        for s in _sentences_with(p[field], CAST_CHANGE):
            if s not in seen_txt:
                seen_txt.add(s)
                cast_changes.append({"label": "Note", "text": s, "cites": cite(_ref_ids(s, known) or date_specific)})

    # --- Program notes
    prog_refs = programs.refs(pid)
    for r in prog_refs:
        src = r["source"]
        sources[f"program:{r['n']}"] = {"id": f"program:{r['n']}", "kind": "program",
                                        "title": src.get("title") or r["title"], "url": src.get("url"),
                                        "wayback_url": src.get("wayback_url"), "type": "program_note",
                                        "date_specific": None, "excerpt": None, "captured": src.get("captured"),
                                        "doc_url": r["url"]}
        used.add(f"program:{r['n']}")

    # --- The Work
    work_items, work_ids = [], []
    for g in conn.execute("""SELECT g.work_id, g.title, g.catalogue, w.*, c.id AS cid, c.name AS cname
                             FROM program g LEFT JOIN works w ON w.id=g.work_id
                             LEFT JOIN composers c ON c.id=g.composer_id
                             WHERE g.performance_id=? ORDER BY g.ord""", (pid,)):
        wid = g["work_id"]
        if wid in work_ids:
            continue
        work_ids.append(wid)
        ws = wiki_src(g["wikidata"], g["title"], g["wikipedia_url"])
        has_wiki = any(_col(g, k) for k in ("synopsis", "premiere_date", "librettists", "description"))
        timeline = [{"track_id": t["id"], "index": t["idx"], "title": t["title"], "duration_s": t["duration_s"]}
                    for t in conn.execute("""SELECT id, idx, title, duration_s FROM tracks WHERE performance_id=?
                                             AND work_id=? AND segment='music' ORDER BY idx""", (pid, wid))]
        prem = None
        if _col(g, "premiere_date") or _col(g, "premiere_place"):
            prem = {"date": _col(g, "premiere_date"), "place": _col(g, "premiere_place")}
        work_items.append({
            "work_id": wid, "title": g["title"], "composer": {"id": g["cid"], "name": g["cname"]},
            "catalogue": g["catalogue"], "description": g["description"], "synopsis": g["synopsis"],
            "synopsis_heading": _col(g, "synopsis_heading"), "premiere": prem,
            "librettists": _j(_col(g, "librettists"), []) or [],
            "wikipedia_url": g["wikipedia_url"], "wikidata": g["wikidata"], "timeline": timeline,
            "cites": cite([ws]) if (ws and has_wiki) else (cite([ws]) if ws else []),
        })

    # --- From the Broadcast
    tr = transcripts.mapped(conn, pid)
    quotes, features = [], []
    if tr:
        sources["broadcast"] = {"id": "broadcast", "kind": "broadcast",
                                "title": f"The broadcast itself (announcer speech, transcribed by {tr.get('model') or 'Whisper'})",
                                "url": None, "wayback_url": None, "type": "audio_transcript", "date_specific": True,
                                "excerpt": "Machine transcription; names and numbers may be misheard.",
                                "transcript_url": f"/api/transcripts/{pid}"}
        names = set()
        for c in credits:
            n = c["pname"] or ""
            sn = surname(n)
            if len(sn) >= 4:
                names.add(fold(sn))
        good = [s for s in tr["segments"] if (s["confidence"] is None or s["confidence"] > -0.8)
                and len(s["text"].split()) >= 5]
        picked: set[tuple] = set()

        def add(s, reason):
            k = (s["track_id"], s["start"])
            if k in picked:
                return
            picked.add(k)
            quotes.append({"track_id": s["track_id"], "track_index": s["track_index"], "start": s["start"],
                           "end": s["end"], "text": s["text"], "reason": reason, "cites": ["broadcast"]})

        for s in good:
            if CAST_CHANGE.search(s["text"]):
                add(s, "cast_change")
        for s in [s for s in good if DATE_TALK.search(s["text"])][:4]:
            add(s, "date")
        cast_hits = [s for s in good if any(re.search(rf"\b{re.escape(n)}\b", fold(s["text"])) for n in names)]
        for s in cast_hits[:8]:
            add(s, "cast")
        for s in [s for s in good if WELCOME.search(s["text"])][:3]:
            add(s, "welcome")
        quotes.sort(key=lambda q: (q["track_index"], q["start"] or 0))
        quotes = quotes[:20]
        if quotes:
            used.add("broadcast")
    first_line = {}
    if tr:
        for s in tr["segments"]:
            first_line.setdefault(s["track_id"], s["text"])
    for t in conn.execute("""SELECT id, idx, title, duration_s FROM tracks WHERE performance_id=?
                             AND segment IN ('intermission','interview') ORDER BY idx""", (pid,)):
        fl = first_line.get(t["id"])
        cs = [record_src()] + (["broadcast"] if fl else [])
        used.update(cs)
        features.append({"track_id": t["id"], "track_index": t["idx"], "title": t["title"],
                         "duration_s": t["duration_s"], "first_line": fl, "cites": cs})

    # --- Versions
    blocks = []
    for wid in work_ids:
        if not wid:
            continue
        rows = version_rows(conn, wid, current=pid)
        if len(rows) >= 2:
            title = conn.execute("SELECT title FROM works WHERE id=?", (wid,)).fetchone()
            blocks.append({"work_id": wid, "work_title": title[0] if title else wid, "rows": rows})

    # --- Sources: everything cited, plus all research sources
    items = [s for sid, s in sources.items() if sid in used or s["kind"] == "research"]
    return {
        "performance_id": pid, "title": p["title"], "subtitle": p["subtitle"],
        "artwork_url": f"/api/performances/{pid}/artwork" if p["artwork_path"] else None,
        "performance": {"title": "The Performance", "facts": facts, "cast_changes": cast_changes},
        "program_notes": {"title": "Program Notes", "items": prog_refs},
        "work": {"title": "The Work", "items": work_items},
        "broadcast": {"title": "From the Broadcast", "has_transcript": bool(tr), "quotes": quotes,
                      "intermission_features": features},
        "versions": {"title": "Versions", "items": blocks},
        "sources": {"title": "Sources", "items": items},
    }
