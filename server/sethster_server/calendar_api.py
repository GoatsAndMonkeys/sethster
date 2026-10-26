"""Time-based Home features (docs/CALENDAR.md):

  GET /api/on-this-day?date=MM-DD|YYYY-MM-DD   the Home card: recordings performed on this day in past
                                              years (reliable day-precision dates only), widened to
                                              this week when there are none; plus premieres and
                                              composer/performer birthdays on this date as fallbacks
  GET /api/on-this-day/ids                    just the performance ids (for an On This Day radio station)
  GET /api/calendar?date=YYYY-MM-DD           Home shelves: in memoriam, composer of the month, Ring
                                              season, round-number anniversaries
  GET /api/performances/{id}/context          historical context for the liner notes (context.py)
  GET /api/dates                              the reliable-date table (perfdates.py), for review
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from .context import ContextStore, historical_context, nice_date, year_of
from .importer import CONDUCTOR_ROLE, STAFF_ROLE
from .perfdates import DateIndex
from .text import fold

RING = [("das rheingold", "rheingold"), ("die walkure", "walkure", "walkuere"), ("siegfried",),
        ("gotterdammerung", "goetterdaemmerung", "gotterdaemmerung")]


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _load_str(v, default=None):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def parse_day(s: str | None) -> dt.date:
    """'MM-DD' (this year) or 'YYYY-MM-DD'; default today."""
    today = dt.date.today()
    if not s:
        return today
    try:
        if re.fullmatch(r"\d{2}-\d{2}", s):
            return dt.date(today.year if s != "02-29" else 2028, int(s[:2]), int(s[3:]))
        return dt.date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, "date must be MM-DD or YYYY-MM-DD")


def _md(d: dt.date) -> str:
    return d.strftime("%m-%d")


ANNIVERSARY_DAYS = 7   # birth anniversaries show from a week before to a week after the date
DEATH_DAYS = 30        # death anniversaries show for the 30 days after the date (none ahead of it)


def anniversary_year(v: str, day: dt.date, days: int = ANNIVERSARY_DAYS, before: int | None = None) -> int | None:
    """The year of the anniversary of YYYY-MM-DD `v` falling from `before` days ahead of `day` (default `days`)
    to `days` days behind it (None if none does).
    Checks last, this and next year so late-December and early-January dates still match across New Year."""
    for y in (day.year - 1, day.year, day.year + 1):
        try:
            a = dt.date(y, int(v[5:7]), int(v[8:10]))
        except ValueError:   # Feb 29 in a non-leap year: use Feb 28
            a = dt.date(y, 2, 28)
        if -days <= (a - day).days <= (days if before is None else before):
            return y
    return None


def round_years(n: int, living_performer: bool = False) -> bool:
    return n > 0 and (n % 25 == 0 or (living_performer and n >= 60 and n % 10 == 0))


def build_router(settings, get_db: Callable, auth: Callable, summaries: Callable, track_shape: Callable,
                 completion: Callable | None = None) -> APIRouter:
    """completion(conn) -> {performance id: (done at, has picture)}: when given, On This Day only suggests
    performances that have everything, pictured ones first."""
    ctx_dir = _env_path("SETHSTER_CONTEXT", settings.data_dir / "context")
    cal_dir = _env_path("SETHSTER_CALENDAR", settings.data_dir / "calendar")
    store = ContextStore(ctx_dir)
    dates_ix = DateIndex(settings.transcripts_dir)
    r = APIRouter(prefix="/api", dependencies=[Depends(auth)], tags=["history"])

    def dates(conn) -> dict[str, dict]:
        return dates_ix.get(conn, settings.db_path)

    # ---- shared helpers -----------------------------------------------------------------------------------
    def cast_of(conn, pid: str, n: int = 4) -> tuple[list[dict], dict | None]:
        rows = conn.execute("""SELECT c.role, c.status, pe.id, pe.name, pe.kind, pe.image_src FROM credits c
                               JOIN people pe ON pe.id=c.person_id WHERE c.performance_id=? ORDER BY c.ord""", (pid,))
        cast, cond, seen = [], None, set()
        for c in sorted(rows, key=lambda c: c["status"] != "confirmed"):
            role = (c["role"] or "").strip()
            ref = {"id": c["id"], "name": c["name"], "kind": c["kind"],
                   "image_url": f"/api/people/{c['id']}/image" if c["image_src"] else None}
            if c["status"] == "production_only":
                continue
            if cond is None and CONDUCTOR_ROLE.search(role):
                cond = ref
                continue
            if c["kind"] == "ensemble" or STAFF_ROLE.search(role) or c["id"] in seen:
                continue
            seen.add(c["id"])
            cast.append({"person": ref, "role": role or None, "status": c["status"]})
        return cast[:n], cond

    def first_music_track(conn, pid: str) -> dict | None:
        t = conn.execute("""SELECT * FROM tracks WHERE performance_id=? ORDER BY segment<>'music', idx LIMIT 1""",
                         (pid,)).fetchone()
        return track_shape(t) if t else None

    def short_note(conn, pid: str) -> dict | None:
        """One cited paragraph to pair with the card: the night's context, else the piece's premiere."""
        note = store.performance_note(pid)
        cands = []
        if note:
            for topic in ("debuts_farewells", "season", "that_year"):
                sec = (note.get("sections") or {}).get(topic)
                if isinstance(sec, dict) and sec.get("text"):
                    cands.append((topic, sec, note))
        if not cands:
            for g in conn.execute("SELECT work_id FROM program WHERE performance_id=? ORDER BY ord", (pid,)):
                wn = store.work_note(g[0])
                for topic in ("premiere", "reception", "composer_life"):
                    sec = ((wn or {}).get("sections") or {}).get(topic)
                    if isinstance(sec, dict) and sec.get("text"):
                        cands.append((topic, sec, wn))
                        break
                if cands:
                    break
        if not cands:
            return None
        topic, sec, doc = cands[0]
        srcs = {s["id"]: s for s in doc.get("sources") or [] if isinstance(s, dict) and s.get("id")}
        if topic in ("premiere", "reception", "composer_life"):
            wf = store.work_facts(doc.get("work_id")) or {}
            srcs.update({s["id"]: s for s in wf.get("sources") or [] if isinstance(s, dict) and s.get("id")})
        perf = conn.execute("SELECT sources FROM performances WHERE id=?", (pid,)).fetchone()
        for s in (_load_str(perf["sources"]) if perf else None) or []:
            if isinstance(s, dict) and s.get("id"):
                srcs.setdefault(s["id"], s)
        cited = [{"id": c, "title": srcs[c].get("title"), "url": srcs[c].get("url"),
                  "wayback_url": srcs[c].get("wayback_url")} for c in sec.get("cites") or [] if c in srcs]
        return {"topic": topic, "text": sec["text"], "cites": [c["id"] for c in cited], "sources": cited,
                "check": sec.get("check") or "unchecked", "context_url": f"/api/performances/{pid}/context"}

    def otd_items(conn, day: dt.date, window: int) -> list[dict]:
        out = []
        done = completion(conn) if completion else None
        for pid, d in dates(conn).items():
            if not d.get("reliable") or not d.get("date") or (done is not None and pid not in done):
                continue
            try:
                pdate = dt.date.fromisoformat(d["date"])
            except ValueError:
                continue
            if pdate.year >= day.year and pdate >= day:
                continue
            best = None
            for off in range(-window, window + 1):
                try:
                    probe = day + dt.timedelta(days=off)
                    if (pdate.month, pdate.day) == (probe.month, probe.day):
                        best = off if best is None or abs(off) < abs(best) else best
                except OverflowError:
                    pass
            if best is not None:
                out.append((abs(best), not (done or {}).get(pid, (0, True))[1], -pdate.year, pid, best, d))
        out.sort()
        return [{"pid": pid, "offset_days": off, "dateinfo": d} for _, _, _, pid, off, d in out]

    def otd_card(conn, it: dict, day: dt.date) -> dict | None:
        s = summaries(conn, [it["pid"]])
        if not s:
            return None
        d = it["dateinfo"]
        y = int(d["date"][:4])
        cast, cond = cast_of(conn, it["pid"])
        return {"performance": s[0], "date": d["date"], "year": y, "years_ago": day.year - y,
                "offset_days": it["offset_days"], "date_status": d["status"], "date_basis": d["basis"],
                "headline": f"On this day in {y}" if it["offset_days"] == 0 else f"This week in {y}",
                "cast": cast, "conductor": cond, "venue": s[0].get("venue"),
                "play": first_music_track(conn, it["pid"]), "context": short_note(conn, it["pid"])}

    def premieres_on(conn, day: dt.date) -> list[dict]:
        out = []
        fdir = ctx_dir / "facts" / "works"
        if not fdir.is_dir():
            return out
        for f in fdir.glob("*.json"):
            wf = store.work_facts(f.stem) or {}
            pd = (wf.get("facts") or {}).get("premiere_date") or ""
            if len(pd) == 10 and pd[5:] == _md(day):
                n = conn.execute("SELECT COUNT(DISTINCT performance_id) FROM program WHERE work_id=?",
                                 (wf["work_id"],)).fetchone()[0]
                wd = next((s for s in wf.get("sources") or [] if s.get("kind") == "wikidata"), None)
                out.append({"work_id": wf["work_id"], "title": wf.get("title"), "composer": wf.get("composer"),
                            "date": pd, "years_ago": day.year - int(pd[:4]),
                            "place": ", ".join(p.get("label") or "" for p in (wf["facts"].get("premiere_place") or [])) or None,
                            "performance_count": n, "status": "wikidata",
                            "source": {"title": wd.get("title"), "url": wd.get("url"),
                                       "wayback_url": wd.get("wayback_url")} if wd else None})
        out.sort(key=lambda x: (-x["performance_count"], x["date"]))
        return out

    def people_index(conn, kind: str | None = None) -> dict[str, dict]:
        """QID -> {kind, id, name, count} for performers and composers in the collection. A QID that is both
        (Bernstein) prefers `kind` if given, else composer; rows sharing a QID (variant composer rows) keep the
        biggest row's id and sum the counts."""
        idx: dict[str, dict] = {}

        def add(k, qid, i, name, n):
            if k == "composer":
                name = clean_name(name)
            e = idx.get(qid)
            if e and e["kind"] == k:
                e["count"] += n
                if n > e["_max"]:
                    e.update(id=i, name=name, _max=n)
            elif not e or (kind or "composer") == k:
                idx[qid] = {"kind": k, "id": i, "name": name, "count": n, "_max": n}

        if kind != "composer":
            for row in conn.execute("""SELECT p.wikidata, p.id, p.name, COUNT(DISTINCT c.performance_id) n FROM people p
                                       JOIN credits c ON c.person_id=p.id WHERE p.wikidata IS NOT NULL AND p.kind='person'
                                       GROUP BY p.id"""):
                add("performer", *row)
        if kind != "performer":
            for row in conn.execute("""SELECT c.wikidata, c.id, c.name, COUNT(DISTINCT pc.performance_id) n FROM composers c
                                       JOIN performance_composers pc ON pc.composer_id=c.id WHERE c.wikidata IS NOT NULL
                                       GROUP BY c.id"""):
                add("composer", *row)
        return idx

    def clean_name(n: str) -> str:
        """'Richard Wagner (arr. Lorin Maazel)' -> 'Richard Wagner' (merged composer rows keep a variant name)."""
        return re.sub(r"\s*(?:\(|,\s*)(?:arr|transcr|orch|ed)\.?\b.*$", "", n or "", flags=re.I).strip() or n

    def entity_ref(e: dict) -> dict:
        base = "composers" if e["kind"] == "composer" else "people"
        return {"kind": e["kind"], "id": e["id"], "name": e["name"], "url": f"/api/{base}/{e['id']}",
                "image_url": f"/api/{base}/{e['id']}/image", "performance_count": e["count"]}

    def life_events(conn, day: dt.date, field: str, match) -> list[dict]:
        out = []
        idx = people_index(conn)
        for qid, pf in store.people().items():
            v = pf.get(field) or ""
            e = idx.get(qid)
            if not e or len(v) != 10 or not match(v):
                continue
            win = dict(days=DEATH_DAYS, before=0) if field == "death" else {}
            out.append({**entity_ref(e), "date": v, "years": (anniversary_year(v, day, **win) or day.year) - int(v[:4]),
                        "status": "wikidata",
                        "source": {"title": f"Wikidata: {pf.get('name')}", "url": pf.get("wikidata_url"),
                                   "wayback_url": f"https://web.archive.org/web/{pf.get('wikidata_url')}"},
                        "alive": not pf.get("death")})
        out.sort(key=lambda x: (x["kind"] != "composer", -x["performance_count"]))
        return out

    # ---- On This Day --------------------------------------------------------------------------------------
    @r.get("/on-this-day")
    def on_this_day(date: str | None = Query(None, description="MM-DD or YYYY-MM-DD; default today"),
                    limit: int = Query(10, ge=1, le=50), conn: sqlite3.Connection = Depends(get_db)):
        day = parse_day(date)
        items, mode = otd_items(conn, day, 0), "day"
        if not items:
            items, mode = otd_items(conn, day, 3), "week"
        cards = [c for c in (otd_card(conn, it, day) for it in items[:limit]) if c]
        if not cards:
            mode = "none"
        births = life_events(conn, day, "birth", lambda v: v[5:] == _md(day))
        deaths = life_events(conn, day, "death", lambda v: v[5:] == _md(day))
        return {"date": day.isoformat(), "month_day": _md(day), "label": day.strftime("%B ") + str(day.day),
                "mode": mode, "items": cards, "premieres": premieres_on(conn, day)[:10],
                "birthdays": births[:12], "deathdays": deaths[:12],
                "station": {"performance_ids": [c["performance"]["id"] for c in cards],
                            "ids_url": f"/api/on-this-day/ids?date={day.isoformat()}"}}

    @r.get("/on-this-day/ids")
    def on_this_day_ids(date: str | None = None, widen: bool = True, conn: sqlite3.Connection = Depends(get_db)):
        day = parse_day(date)
        items, mode = otd_items(conn, day, 0), "day"
        if not items and widen:
            items, mode = otd_items(conn, day, 3), "week"
        return {"date": day.isoformat(), "mode": mode if items else "none",
                "performance_ids": [i["pid"] for i in items],
                "items": [{"performance_id": i["pid"], "date": i["dateinfo"]["date"], "offset_days": i["offset_days"]}
                          for i in items]}

    @r.get("/dates")
    def dates_table(reliable: bool | None = None, conn: sqlite3.Connection = Depends(get_db)):
        rows = [d for d in dates(conn).values() if reliable is None or bool(d["reliable"]) == reliable]
        rows.sort(key=lambda d: d["performance_id"])
        return {"items": rows, "total": len(rows)}

    @r.get("/performances/{pid}/context")
    def performance_context(pid: str, conn: sqlite3.Connection = Depends(get_db)):
        doc = historical_context(conn, pid, store, dates(conn))
        if doc is None:
            raise HTTPException(404, "performance not found")
        return doc

    # ---- calendar shelves -----------------------------------------------------------------------------------
    def perf_ids_for(conn, kind: str, ident: str, limit: int = 40) -> list[str]:
        if kind == "composer":
            q = """SELECT p.id FROM performances p JOIN performance_composers pc ON pc.performance_id=p.id
                   WHERE pc.composer_id=? GROUP BY p.id ORDER BY p.date IS NULL, p.date LIMIT ?"""
        elif kind == "work":
            q = """SELECT p.id FROM performances p JOIN program g ON g.performance_id=p.id WHERE g.work_id=?
                   GROUP BY p.id ORDER BY p.date IS NULL, p.date LIMIT ?"""
        else:
            q = """SELECT p.id FROM performances p JOIN credits c ON c.performance_id=p.id WHERE c.person_id=?
                   GROUP BY p.id ORDER BY p.date IS NULL, p.date LIMIT ?"""
        return [x[0] for x in conn.execute(q, (ident, limit))]

    def in_memoriam(conn, day: dt.date, days: int = 120) -> list[dict]:
        idx = people_index(conn, "performer")
        deaths: dict[str, dict] = {}
        for qid, pf in store.people().items():
            if pf.get("death") and len(pf["death"]) >= 7:
                deaths[qid] = {"died": pf["death"], "name": pf.get("name"), "status": "wikidata",
                               "source": {"title": f"Wikidata: {pf.get('name')}", "url": pf.get("wikidata_url"),
                                          "wayback_url": f"https://web.archive.org/web/{pf.get('wikidata_url')}"}}
        for m in (_load_json(cal_dir / "memoriam.json", {}) or {}).get("items", []):
            if m.get("qid") and m.get("died") and (m["qid"] not in deaths or m.get("status") == "confirmed"):
                deaths[m["qid"]] = {"died": m["died"], "name": m.get("name"), "status": m.get("status", "reported"),
                                    "source": {"title": m.get("source_title"), "url": m.get("source_url"),
                                               "wayback_url": m.get("wayback_url")},
                                    "extra": [{"title": f"{x.get('outlet')}: {x.get('title')}", "url": x.get("url"),
                                               "wayback_url": f"https://web.archive.org/web/{x.get('url')}"}
                                              for x in m.get("sources") or [] if x.get("url") != m.get("source_url")]}
        shelves = []
        for qid, d in deaths.items():
            e = idx.get(qid)
            if not e or e["kind"] != "performer":
                continue
            died = d["died"] if len(d["died"]) == 10 else d["died"][:7] + "-01"
            try:
                age = (day - dt.date.fromisoformat(died)).days
            except ValueError:
                continue
            if not (0 <= age <= days):
                continue
            ids = perf_ids_for(conn, "person", e["id"])
            pf = store.people().get(qid) or {}
            shelves.append({"id": f"memoriam:{e['id']}", "kind": "in_memoriam",
                            "title": f"In Memoriam: {e['name']}",
                            "subtitle": " – ".join(x for x in [str(pf.get("birth_year") or ""), nice_date(d["died"])] if x),
                            "entity": entity_ref(e), "died": d["died"], "status": d["status"],
                            "sources": [d["source"], *d.get("extra", [])], "station_id": f"person:{e['id']}",
                            "items": summaries(conn, ids)})
        shelves.sort(key=lambda s: s["died"], reverse=True)
        return shelves

    def composer_month(conn, day: dt.date) -> list[dict]:
        cfg = _load_json(cal_dir / "composer_months.json", {}) or {}
        boost = cfg.get("boost") or {}
        pinned = {k: int(v) for k, v in (cfg.get("months") or {}).items()}   # composer id -> month override
        min_count = int(cfg.get("min_performances", 3))
        only_listed = bool(cfg.get("only_listed"))   # just the hand-picked majors, not every composer born this month
        idx = people_index(conn, "composer")
        out = []
        for qid, pf in store.people().items():
            e = idx.get(qid)
            if not e or e["kind"] != "composer" or (only_listed and qid not in pinned and e["id"] not in pinned):
                continue
            month = pinned.get(qid) or pinned.get(e["id"]) or \
                (int(pf["birth"][5:7]) if len(pf.get("birth") or "") >= 7 else None)
            b = boost.get(qid) or boost.get(e["id"])
            if month != day.month or (e["count"] < min_count and not b):
                continue
            ids = perf_ids_for(conn, "composer", e["id"], 60)
            score = e["count"] * float(b or 1)
            out.append((score, {
                "id": f"composer-month:{e['id']}", "kind": "composer_month",
                "title": f"{e['name']} Month", "subtitle": f"Born {nice_date(pf['birth'])}" if pf.get("birth") else None,
                "entity": entity_ref(e), "status": "wikidata",
                "sources": [{"title": f"Wikidata: {pf.get('name')}", "url": pf.get("wikidata_url"),
                             "wayback_url": f"https://web.archive.org/web/{pf.get('wikidata_url')}"}],
                "station_id": f"composer:{e['id']}", "items": summaries(conn, ids)}))
        out.sort(key=lambda x: -x[0])
        return [s for _, s in out][:1]   # one composer per month

    def ring_recordings(conn) -> list[str]:
        rows = conn.execute("""SELECT DISTINCT p.id, p.date, w.title FROM program g JOIN works w ON w.id=g.work_id
                               JOIN performances p ON p.id=g.performance_id JOIN composers c ON c.id=w.composer_id
                               WHERE c.name LIKE '%Wagner%'""").fetchall()
        picked = []
        for r_ in rows:
            t = re.sub(r"[^a-z ]", "", fold(r_["title"]).replace("ä", "a").replace("ö", "o"))
            for i, names in enumerate(RING):
                if any(n in t for n in names):
                    picked.append((i, r_["date"] or "9999", r_["id"]))
                    break
        picked.sort()
        return list(dict.fromkeys(p[2] for p in picked))

    def ring_season(conn, day: dt.date) -> list[dict]:
        ev = (_load_json(cal_dir / "events.json", {}) or {}).get("events", [])
        live = []
        for e in ev:
            if e.get("kind") != "ring_cycle" or not e.get("start"):
                continue
            try:
                start = dt.date.fromisoformat(e["start"][:10])
                end = dt.date.fromisoformat((e.get("end") or e["start"])[:10])
            except ValueError:
                continue
            lead = int(e.get("lead_days", 30))
            if start - dt.timedelta(days=lead) <= day <= end:
                live.append(e)
        if not live:
            return []
        ids = ring_recordings(conn)
        if not ids:
            return []
        return [{"id": "ring-season", "kind": "ring_season", "title": "Ring Season",
                 "subtitle": "; ".join(f"{e.get('house')}: {nice_date(e['start'][:10])}"
                                       f"{' – ' + nice_date(e['end'][:10]) if e.get('end') else ''}" for e in live),
                 "events": live, "status": "sourced",
                 "sources": [{"title": e.get("source_title"), "url": e.get("url"), "wayback_url": e.get("wayback_url")}
                             for e in live], "station_id": None, "items": summaries(conn, ids)}]

    def anniversaries(conn, day: dt.date) -> list[dict]:
        out = []
        for f in (ctx_dir / "facts" / "works").glob("*.json") if (ctx_dir / "facts" / "works").is_dir() else []:
            wf = store.work_facts(f.stem) or {}
            pd = (wf.get("facts") or {}).get("premiere_date") or ""
            if len(pd) >= 7 and int(pd[5:7]) == day.month and round_years(day.year - int(pd[:4])):
                ids = perf_ids_for(conn, "work", wf["work_id"])
                if ids:
                    n = day.year - int(pd[:4])
                    wd = next((s for s in wf.get("sources") or [] if s.get("kind") == "wikidata"), {})
                    out.append({"id": f"premiere:{wf['work_id']}", "kind": "anniversary", "reason": "premiere",
                                "title": f"{wf.get('title')}: {n} years", "years": n,
                                "subtitle": f"Premiered {nice_date(pd)}", "date": pd, "status": "wikidata",
                                "sources": [{"title": wd.get("title"), "url": wd.get("url"),
                                             "wayback_url": wd.get("wayback_url")}],
                                "station_id": None, "items": summaries(conn, ids)})
        for field, word in (("birth", "born"), ("death", "died")):
            win = dict(days=DEATH_DAYS, before=0) if field == "death" else {}
            for e in life_events(conn, day, field, lambda v: anniversary_year(v, day, **win) is not None):
                if not round_years(e["years"], living_performer=(field == "birth" and e["kind"] == "performer"
                                                                 and e["alive"])):
                    continue
                ids = perf_ids_for(conn, "composer" if e["kind"] == "composer" else "person", e["id"])
                label = f"{e['years']}th birthday" if field == "birth" and e["alive"] else \
                    (f"{e['years']} years since {'birth' if field == 'birth' else 'death'}")
                out.append({"id": f"{field}:{e['id']}", "kind": "anniversary", "reason": field,
                            "title": f"{e['name']}: {label}", "years": e["years"],
                            "subtitle": f"{word.capitalize()} {nice_date(e['date'])}", "date": e["date"],
                            "entity": {k: e[k] for k in ("kind", "id", "name", "url", "image_url")},
                            "status": "wikidata", "sources": [e["source"]],
                            "station_id": f"{'composer' if e['kind'] == 'composer' else 'person'}:{e['id']}",
                            "items": summaries(conn, ids)})
        out.sort(key=lambda s: (-len(s["items"]), s["date"][5:]))
        return out

    @r.get("/calendar")
    def calendar(date: str | None = Query(None, description="YYYY-MM-DD; default today"),
                 conn: sqlite3.Connection = Depends(get_db)):
        day = parse_day(date)
        shelves = in_memoriam(conn, day) + composer_month(conn, day) + ring_season(conn, day) + anniversaries(conn, day)
        shelves = [s for s in shelves if s["items"]]
        return {"date": day.isoformat(), "shelves": shelves}

    return r
