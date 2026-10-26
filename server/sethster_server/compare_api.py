"""Sections + Compare: canonical works, section maps, version comparison and cross-version jumps.

Reads data/sections/*.json (docs/sections.md, written by pipeline/segment) and the cross-version
alignment maps in data/sections/align/. Pure stdlib (no numpy). Routes (all need auth):

  GET /api/canonical-works?min_versions=2      pieces with several versions in the collection
  GET /api/performances/{pid}/sections         logical sections of one performance
  GET /api/works/{wid}/compare                 every version of the piece (wid: work id or canonical id)
  GET /api/compare/jump?from=&to=&track_id=&t= equivalent moment in another version
"""
from __future__ import annotations

import bisect
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from statistics import median
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from . import canonical as CANON
from .importer import CONDUCTOR_ROLE, STAFF_ROLE
from .liner import version_rows
from .text import norm_key

log = logging.getLogger("sethster.compare")
NON_MUSIC = {"applause", "announcer", "intermission", "interview", "tuning", "silence", "other"}


class SectionStore:
    """data/sections/*.json and align/**.json, reloaded per file when its mtime changes."""

    def __init__(self, directory: Path | None):
        self.dir = directory
        self._docs: dict[str, tuple[float, dict | None]] = {}
        self._aligns: dict[Path, tuple[float, dict | None]] = {}

    def _load(self, path: Path, cache: dict) -> dict | None:
        try:
            m = path.stat().st_mtime
        except OSError:
            cache.pop(path, None)
            return None
        hit = cache.get(path)
        if hit and hit[0] == m:
            return hit[1]
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as e:
            log.warning("section file %s unreadable (%s)", path, e)
            doc = None
        cache[path] = (m, doc)
        return doc

    def doc(self, pid: str) -> dict | None:
        if not self.dir or "/" in pid or pid.startswith("."):
            return None
        doc = self._load(self.dir / f"{pid}.json", self._docs)
        return doc if doc and doc.get("format_version") == 1 else None

    def alignment(self, cid: str, a: str, b: str) -> tuple[dict | None, bool]:
        """(doc, swapped): the map between a and b, stored once as min__max."""
        if not self.dir or not cid:
            return None, False
        lo, hi = sorted([a, b])
        doc = self._load(self.dir / "align" / cid / f"{lo}__{hi}.json", self._aligns)
        return doc, a != lo


# ---------------------------------------------------------------------------------------- helpers

def logical_sections(doc: dict) -> list[dict]:
    """Merge the per-file pieces of a sections doc back into logical sections (by `section`)."""
    out: list[dict] = []
    by: dict[str, dict] = {}
    for p in doc.get("sections") or []:
        if not isinstance(p, dict) or "track_id" not in p:
            continue
        sid = p.get("section") or p.get("id")
        tidx = _track_index(doc, p["track_id"])
        if sid in by:
            cur = by[sid]
            cur["end"] = {"track_id": p["track_id"], "track_index": tidx, "t": p.get("end_s")}
            cur["duration_s"] = round(cur["duration_s"] + (p.get("duration_s") or 0), 2)
            if p.get("music_end_s") is not None:
                cur["music_end"] = {"track_id": p["track_id"], "track_index": tidx, "t": p["music_end_s"]}
            continue
        dur = p.get("duration_s")
        if dur is None and p.get("end_s") is not None:
            dur = p["end_s"] - (p.get("start_s") or 0)
        cur = {
            "id": sid, "kind": p.get("kind"), "music": p.get("kind") not in NON_MUSIC,
            "title": p.get("title"), "act": p.get("act"), "group": p.get("group"), "key": p.get("key"),
            "work_id": p.get("work_id"), "canonical_work_id": p.get("canonical_work_id"),
            "start": {"track_id": p["track_id"], "track_index": tidx, "t": p.get("start_s") or 0.0},
            "end": {"track_id": p["track_id"], "track_index": tidx, "t": p.get("end_s")},
            "start_abs": p.get("section_start_abs", p.get("start_abs")),
            "duration_s": round(dur or 0, 2),
            "music_end": {"track_id": p["track_id"], "track_index": tidx, "t": p["music_end_s"]}
            if p.get("music_end_s") is not None else None,
            "confidence": p.get("confidence"), "needs_review": bool(p.get("needs_review")),
            "label_source": p.get("label_source"), "source": p.get("source"),
            "tempo_ratio": p.get("tempo_ratio"),
            "reference_title": (p.get("reference") or {}).get("title"),
            "characters": p.get("characters") or [],
        }
        by[sid] = cur
        out.append(cur)
    for s in out:   # a section split by an announcer break (key r012.1/.2) compares as one number
        if s["key"] and re.search(r"\.\d+$", s["key"]):
            s["key"] = s["key"].rsplit(".", 1)[0]
    return out


def _track_index(doc: dict, tid: str) -> int | None:
    for t in doc.get("tracks") or []:
        if t.get("track_id") == tid:
            return t.get("index")
    try:
        return int(tid.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def work_tracks(doc: dict, wid: str) -> list[dict]:
    """[{track_id, index, work_offset_s, duration_s}] for one work's music tracks, in play order."""
    w = next((x for x in doc.get("works") or [] if x.get("work_id") == wid), None)
    tracks = {t["track_id"]: t for t in doc.get("tracks") or []}
    out, off = [], 0.0
    for tid in (w or {}).get("tracks") or []:
        t = tracks.get(tid)
        if not t:
            continue
        out.append({"track_id": tid, "index": t.get("index"), "work_offset_s": off, "duration_s": t["duration_s"]})
        off += t["duration_s"] or 0
    return out


def to_work_time(wt: list[dict], tid: str, t: float) -> float | None:
    for x in wt:
        if x["track_id"] == tid:
            return x["work_offset_s"] + min(max(t, 0.0), x["duration_s"] or t)
    return None


def from_work_time(wt: list[dict], w: float) -> tuple[dict, float] | None:
    if not wt:
        return None
    for x in wt:
        if w < x["work_offset_s"] + (x["duration_s"] or 0):
            return x, max(0.0, w - x["work_offset_s"])
    last = wt[-1]
    return last, last["duration_s"] or 0.0


def interp(points: list[list[float]], x: float, swapped: bool) -> float:
    """Piecewise-linear map through monotone [[a, b], ...]; swapped maps b -> a."""
    ia, ib = (1, 0) if swapped else (0, 1)
    xs = [p[ia] for p in points]
    k = bisect.bisect_left(xs, x)
    if k <= 0:
        return points[0][ib] + (x - xs[0])
    if k >= len(points):
        return points[-1][ib] + (x - xs[-1])
    x0, x1 = xs[k - 1], xs[k]
    y0, y1 = points[k - 1][ib], points[k][ib]
    if x1 - x0 <= 1e-9:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _role_key(role: str) -> str:
    return norm_key(re.sub(r"\(.*?\)", " ", role or ""))


# ------------------------------------------------------------------------------------------ router

def build_router(settings, get_db: Callable, auth: Callable, summaries: Callable) -> APIRouter:
    d = os.environ.get("SETHSTER_SECTIONS")
    store = SectionStore(Path(d) if d else settings.data_dir / "sections")
    r = APIRouter(prefix="/api", dependencies=[Depends(auth)], tags=["compare"])

    def canon(conn) -> tuple[dict, dict]:
        row = conn.execute("SELECT value FROM meta WHERE key='imported_at'").fetchone()
        key = f"{settings.db_path}|{row[0] if row else ''}"
        return CANON.for_db(conn, key)

    def resolve(conn, wid: str) -> CANON.CanonicalWork:
        cw_by_id, w2c = canon(conn)
        if wid in cw_by_id:
            return cw_by_id[wid]
        if wid in w2c:
            return cw_by_id[w2c[wid]]
        raise HTTPException(404, "unknown work")

    def perf_ids(conn, cw) -> dict[str, str]:
        """performance id -> its work id for this canonical work (first program match)."""
        q = ",".join("?" * len(cw.work_ids))
        out: dict[str, str] = {}
        for row in conn.execute(f"SELECT performance_id, work_id FROM program WHERE work_id IN ({q}) ORDER BY ord",
                                cw.work_ids):
            out.setdefault(row[0], row[1])
        return out

    @r.get("/canonical-works")
    def canonical_works(min_versions: int = Query(1, ge=1), q: str | None = None,
                        include_excerpts: bool = True, limit: int = Query(200, ge=1, le=2000),
                        offset: int = Query(0, ge=0), conn: sqlite3.Connection = Depends(get_db)):
        cw_by_id, w2c = canon(conn)
        counts: dict[str, set] = {}
        for pid, wid in conn.execute("SELECT performance_id, work_id FROM program WHERE work_id IS NOT NULL"):
            if wid in w2c:
                counts.setdefault(w2c[wid], set()).add(pid)
        items = []
        multi_all = [(cid, cw) for cid, cw in cw_by_id.items() if len(counts.get(cid, ())) >= 2 and not cw.unidentified]
        for cid, cw in cw_by_id.items():
            n = len(counts.get(cid, ()))
            if n < min_versions or cw.unidentified or (cw.excerpt_of and not include_excerpts):
                continue
            if q and norm_key(q) not in norm_key(f"{cw.title} {cw.composer_name} {cw.catalogue or ''}"):
                continue
            items.append({"id": cid, "title": cw.title, "composer": {"id": cw.composer_id, "name": cw.composer_name},
                          "catalogue": cw.catalogue, "wikidata": cw.wikidata, "work_ids": cw.work_ids,
                          "excerpt_of": cw.excerpt_of, "version_count": n})
        items.sort(key=lambda x: (-x["version_count"], norm_key(x["composer"]["name"]), norm_key(x["title"])))
        complete = sum(1 for _, cw in multi_all if not cw.excerpt_of and not CANON.split_excerpt(cw.title))
        return {"items": items[offset: offset + limit], "total": len(items),
                "multi_version": {"pieces": len(multi_all), "complete_works": complete,
                                  "excerpts": len(multi_all) - complete}}

    @r.get("/performances/{pid}/sections")
    def performance_sections(pid: str, music_only: bool = False, conn: sqlite3.Connection = Depends(get_db)):
        if not conn.execute("SELECT 1 FROM performances WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "unknown performance")
        doc = store.doc(pid)
        if not doc:
            return {"performance_id": pid, "available": False, "pipeline": None, "works": [], "sections": []}
        secs = logical_sections(doc)
        if music_only:
            secs = [s for s in secs if s["music"]]
        works = [{k: w.get(k) for k in ("work_id", "canonical_work_id", "title", "method", "music_s", "sections",
                                        "needs_review")} | {"reference": _ref_summary(w.get("reference"))}
                 for w in doc.get("works") or []]
        return {"performance_id": pid, "available": True, "pipeline": doc.get("pipeline"),
                "generated_at": doc.get("generated_at"), "works": works, "sections": secs}

    @r.get("/works/{wid}/compare")
    def work_compare(wid: str, conn: sqlite3.Connection = Depends(get_db)):
        cw = resolve(conn, wid)
        pmap = perf_ids(conn, cw)
        rows: dict[str, dict] = {}
        for w in cw.work_ids:
            for row in version_rows(conn, w):
                if row["performance_id"] in pmap and pmap[row["performance_id"]] == w:
                    rows.setdefault(row["performance_id"], dict(row, work_id=w))
        versions = sorted(rows.values(), key=lambda x: (x["date"] is None, x["date"] or "", x["performance_id"]))
        per_version_secs: dict[str, list[dict]] = {}
        for v in versions:
            pid = v["performance_id"]
            doc = store.doc(pid)
            v["year"] = int(v["date"][:4]) if v.get("date") and v["date"][:4].isdigit() else None
            v["sections_available"] = bool(doc)
            wmeta = next((x for x in (doc or {}).get("works") or [] if x.get("work_id") == v["work_id"]), None)
            v["method"] = wmeta.get("method") if wmeta else None
            v["music_s"] = wmeta.get("music_s") if wmeta else v.get("duration_s")
            v["reference"] = _ref_summary(wmeta.get("reference")) if wmeta else None
            v["first_track_id"] = (wmeta.get("tracks") or [None])[0] if wmeta else None
            secs = [s for s in logical_sections(doc) if s["music"] and s["work_id"] == v["work_id"]] if doc else []
            v["section_count"] = len(secs)
            v["needs_review"] = sum(1 for s in secs if s["needs_review"])
            per_version_secs[pid] = secs
        return {
            "canonical": {"id": cw.id, "title": cw.title, "composer": {"id": cw.composer_id, "name": cw.composer_name},
                          "catalogue": cw.catalogue, "wikidata": cw.wikidata, "work_ids": cw.work_ids,
                          "excerpt_of": cw.excerpt_of},
            "versions": versions,
            "differences": _differences(conn, versions),
            "sections": _section_table(versions, per_version_secs),
        }

    @r.get("/compare/jump")
    def compare_jump(to: str, track_id: str, t: float = Query(0.0, ge=0),
                     from_: str | None = Query(None, alias="from"),
                     mode: str = Query("auto", pattern="^(auto|section|moment)$"),
                     conn: sqlite3.Connection = Depends(get_db)):
        src_pid = from_ or track_id.rsplit(":", 1)[0]
        tr = conn.execute("SELECT performance_id, work_id FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not tr or tr["performance_id"] != src_pid:
            raise HTTPException(404, "unknown track")
        if not conn.execute("SELECT 1 FROM performances WHERE id=?", (to,)).fetchone():
            raise HTTPException(404, "unknown performance")
        da, db_ = store.doc(src_pid), store.doc(to)
        cw_by_id, w2c = canon(conn)
        wid_a = tr["work_id"]
        # the source moment's work: the track's, else the section containing it
        sec_a = None
        if da:
            for s in logical_sections(da):
                if s["music"] and _contains(da, s, track_id, t):
                    sec_a = s
                    break
            if sec_a and not wid_a:
                wid_a = sec_a["work_id"]
        cid = w2c.get(wid_a or "")
        if not cid:
            raise HTTPException(404, "this moment is not part of an identified work")
        cw = cw_by_id[cid]
        wid_b = perf_ids(conn, cw).get(to)
        if not wid_b:
            raise HTTPException(404, "the other performance has no version of this work")
        result = {"performance_id": to, "work_id": wid_b, "canonical_work_id": cid}

        # 1. exact moment through the cross-version alignment map
        if mode in ("auto", "moment"):
            al, swapped = store.alignment(cid, src_pid, to)
            if al and al.get("points"):
                a_wt = al["b_tracks"] if swapped else al["a_tracks"]
                b_wt = al["a_tracks"] if swapped else al["b_tracks"]
                w = to_work_time(a_wt, track_id, t)
                if w is not None:
                    hit = from_work_time(b_wt, interp(al["points"], w, swapped))
                    if hit:
                        x, off = hit
                        return result | {"track_id": x["track_id"], "track_index": _idx(conn, x["track_id"]),
                                         "t": round(off, 2), "method": "alignment",
                                         "confidence": al.get("quality"), "section": _sec_at(db_, x["track_id"], off)}
        # 2. same section (by cross-version key), at the same relative position for "moment"
        if sec_a and db_ and sec_a.get("key"):
            target = next((s for s in logical_sections(db_) if s["music"] and s["work_id"] == wid_b
                           and s["key"] == sec_a["key"]), None)
            if target:
                frac = 0.0
                if mode == "moment" and sec_a["duration_s"]:
                    w0 = _abs(da, sec_a["start"]["track_id"], sec_a["start"]["t"])
                    w1 = _abs(da, track_id, t)
                    if w0 is not None and w1 is not None:
                        frac = min(max((w1 - w0) / sec_a["duration_s"], 0.0), 1.0)
                tid, off = target["start"]["track_id"], target["start"]["t"]
                if frac:
                    hit = _advance(db_, tid, off, frac * target["duration_s"])
                    if hit:
                        tid, off = hit
                return result | {"track_id": tid, "track_index": _idx(conn, tid), "t": round(off, 2),
                                 "method": "section", "confidence": min(sec_a.get("confidence") or 0,
                                                                         target.get("confidence") or 0),
                                 "section": target}
        # 3. proportional position within the work's music
        if da and db_:
            a_wt, b_wt = work_tracks(da, wid_a), work_tracks(db_, wid_b)
            w = to_work_time(a_wt, track_id, t)
            tot_a = sum(x["duration_s"] or 0 for x in a_wt)
            tot_b = sum(x["duration_s"] or 0 for x in b_wt)
            if w is not None and tot_a > 0 and tot_b > 0:
                hit = from_work_time(b_wt, w / tot_a * tot_b)
                if hit:
                    x, off = hit
                    return result | {"track_id": x["track_id"], "track_index": _idx(conn, x["track_id"]),
                                     "t": round(off, 2), "method": "proportional", "confidence": 0.2,
                                     "section": _sec_at(db_, x["track_id"], off)}
        # 4. the start of the work in the other performance
        first = conn.execute("SELECT id, idx FROM tracks WHERE performance_id=? AND work_id=? AND segment='music' "
                             "ORDER BY idx LIMIT 1", (to, wid_b)).fetchone() or \
            conn.execute("SELECT id, idx FROM tracks WHERE performance_id=? ORDER BY idx LIMIT 1", (to,)).fetchone()
        if not first:
            raise HTTPException(404, "the other performance has no tracks")
        return result | {"track_id": first["id"], "track_index": first["idx"], "t": 0.0, "method": "start",
                         "confidence": 0.0, "section": None}

    return r


def _idx(conn, tid: str) -> int | None:
    row = conn.execute("SELECT idx FROM tracks WHERE id=?", (tid,)).fetchone()
    return row[0] if row else None


def _abs(doc: dict, tid: str, t: float) -> float | None:
    for x in doc.get("tracks") or []:
        if x["track_id"] == tid:
            return (x.get("offset_s") or 0) + t
    return None


def _advance(doc: dict, tid: str, off: float, delta: float) -> tuple[str, float] | None:
    """(track, offset) `delta` seconds after (tid, off), following play order."""
    tracks = doc.get("tracks") or []
    k = next((i for i, x in enumerate(tracks) if x["track_id"] == tid), None)
    if k is None:
        return None
    pos = off + delta
    while k < len(tracks) and pos > (tracks[k]["duration_s"] or 0):
        pos -= tracks[k]["duration_s"] or 0
        k += 1
    if k >= len(tracks):
        return tracks[-1]["track_id"], tracks[-1]["duration_s"] or 0
    return tracks[k]["track_id"], pos


def _contains(doc: dict, s: dict, tid: str, t: float) -> bool:
    a = _abs(doc, s["start"]["track_id"], s["start"]["t"] or 0)
    x = _abs(doc, tid, t)
    if a is None or x is None:
        return False
    return a <= x < a + (s["duration_s"] or 0) + 1e-6


def _sec_at(doc: dict | None, tid: str, t: float) -> dict | None:
    if not doc:
        return None
    for s in logical_sections(doc):
        if s["music"] and _contains(doc, s, tid, t):
            return s
    return None


def _ref_summary(ref: dict | None) -> dict | None:
    if not ref:
        return None
    return {k: ref.get(k) for k in ("recording_id", "name", "kind", "n_tracks", "alignment_quality", "release")}


def _differences(conn, versions: list[dict]) -> dict:
    roles: dict[str, dict] = {}
    for v in versions:
        pid, wid = v["performance_id"], v["work_id"]
        n_works = conn.execute("SELECT COUNT(DISTINCT work_id) FROM program WHERE performance_id=?", (pid,)).fetchone()[0]
        for c in conn.execute("""SELECT c.role, c.status, c.work_id, pe.id, pe.name, pe.kind FROM credits c
                                 JOIN people pe ON pe.id=c.person_id WHERE c.performance_id=? ORDER BY c.ord""", (pid,)):
            if not (c["work_id"] == wid or c["work_id"] is None or n_works <= 1):
                continue
            role = (c["role"] or "").strip()
            if not role or c["status"] == "production_only" or (STAFF_ROLE.search(role) and not CONDUCTOR_ROLE.search(role)):
                continue
            k = "conductor" if CONDUCTOR_ROLE.search(role) else _role_key(role)
            if not k:
                continue
            ent = roles.setdefault(k, {"role": "Conductor" if k == "conductor" else role, "by_version": {}})
            names = ent["by_version"].setdefault(pid, [])
            if not any(n["id"] == c["id"] for n in names):
                names.append({"id": c["id"], "name": c["name"], "status": c["status"]})
    out_roles = []
    for k, ent in roles.items():
        sets = [frozenset(n["id"] for n in ent["by_version"].get(v["performance_id"], [])) for v in versions]
        known = [s for s in sets if s]
        ent["differs"] = len(set(known)) > 1 or (0 < len(known) < len(sets))
        ent["present_in"] = len(known)
        out_roles.append(ent)
    out_roles.sort(key=lambda e: (e["role"] != "Conductor", -e["present_in"]))
    venues = {(v.get("venue") or {}).get("id") for v in versions}
    years = [v["year"] for v in versions if v.get("year")]
    conductors = {(v.get("conductor") or {}).get("id") for v in versions}
    return {"conductor_differs": len(conductors) > 1, "venue_differs": len(venues) > 1,
            "years": {"min": min(years), "max": max(years), "span": max(years) - min(years)} if years else None,
            "venues": sorted({(v.get("venue") or {}).get("name") or "?" for v in versions}),
            "roles": out_roles}


_KEY_RANK = {"r": 0, "mv": 1, "t": 2, "m": 3}


def _key_order(key: str) -> tuple:
    m = re.match(r"([a-z]+)(\d+)", key or "")
    return (_KEY_RANK.get(m.group(1), 4), int(m.group(2))) if m else (5, 0)


def _section_table(versions: list[dict], secs: dict[str, list[dict]]) -> dict:
    """Rows = cross-version section keys; cells = that section in each version (duration, tempo)."""
    rows: dict[str, dict] = {}
    for v in versions:
        for s in secs.get(v["performance_id"], []):
            k = s.get("key")
            if not k:
                continue
            row = rows.setdefault(k, {"key": k, "label": s["title"], "group": s.get("group"), "cells": {}})
            if s.get("label_source") == "reference" and row["label"] != s["title"] and not row.get("_ref"):
                row["label"], row["_ref"] = s["title"], True
            cell = row["cells"].get(v["performance_id"])
            if cell:   # several logical pieces with one key (e.g. split by a break): add up
                cell["duration_s"] = round(cell["duration_s"] + s["duration_s"], 2)
                continue
            row["cells"][v["performance_id"]] = {
                "section_id": s["id"], "duration_s": s["duration_s"], "start": s["start"],
                "confidence": s.get("confidence"), "needs_review": s.get("needs_review"),
                "tempo_ratio": s.get("tempo_ratio")}
    out = []
    for row in sorted(rows.values(), key=lambda r: _key_order(r["key"])):
        row.pop("_ref", None)
        if _key_order(row["key"])[0] >= 2 and len(row["cells"]) < 2:
            continue      # an unmatched acoustic part of one version: nothing to compare it with
        durs = [c["duration_s"] for c in row["cells"].values() if c["duration_s"]]
        med = median(durs) if durs else None
        for c in row["cells"].values():
            c["relative"] = round(c["duration_s"] / med, 3) if med and c["duration_s"] else None
        row["median_s"] = round(med, 1) if med else None
        row["versions"] = len(row["cells"])
        out.append(row)
    shared = [r for r in out if r["versions"] >= 2]
    return {"rows": out, "shared_rows": len(shared),
            "comparable": len(shared) >= 1 and len([v for v in versions if secs.get(v["performance_id"])]) >= 2}
