"""/api/radio routes: seeded stations over the collection + live internet radio (docs/RADIO.md)."""
from __future__ import annotations

import os
import random
import sqlite3
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .radio import SEED_TYPES, Catalog, DEFAULT_SETTINGS, Excerpt, FamousIndex, pick_next, station_id_for, station_name
from .radio_live import LiveStations
from .radio_store import RadioStore


class SeedIn(BaseModel):
    type: Literal["composer", "person", "ensemble"]
    id: str
    weight: float = Field(1.0, gt=0, le=10)


class StationIn(BaseModel):
    seeds: list[SeedIn] = Field(..., min_length=1, max_length=12)
    name: str | None = None
    settings: dict | None = None


class PlayIn(BaseModel):
    excerpt_id: str
    station_id: str | None = None
    outcome: Literal["playing", "played", "skipped"] = "playing"
    listened_s: float | None = None
    client: str | None = None


class PlayUpdate(BaseModel):
    outcome: Literal["played", "skipped"]
    listened_s: float | None = None


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


def build_router(settings, get_db: Callable, auth: Callable, summaries: Callable, track_shape: Callable,
                 *, rng: random.Random | None = None) -> APIRouter:
    data = settings.data_dir
    store = RadioStore(_env_path("SETHSTER_RADIO_DB", data / "radio.db"))
    famous = FamousIndex(_env_path("SETHSTER_FAMOUS", data / "highlights" / "famous_numbers.json"))
    catalog = Catalog(settings.db_path, _env_path("SETHSTER_SECTIONS", data / "sections"), famous, store,
                      media_root=settings.media_root,
                      probe=os.environ.get("SETHSTER_RADIO_PROBE", "1") != "0")
    live = LiveStations(_env_path("SETHSTER_LIVE_STATIONS", data / "radio" / "live_stations.json"))
    r = APIRouter(prefix="/api/radio", dependencies=[Depends(auth)], tags=["radio"])
    rnd = rng or random.Random()

    def cat() -> Catalog:
        if not settings.db_path.exists():
            raise HTTPException(503, "database not built; run the importer")
        return catalog.get()

    def excerpt_shape(e: Excerpt, c: Catalog, conn: sqlite3.Connection, reasons=None, detail=False) -> dict:
        perf = summaries(conn, [e.performance_id])
        t = conn.execute("SELECT * FROM tracks WHERE id=?", (e.track_id,)).fetchone()
        performers = []
        for pid, (role, cert) in e.performers.items():
            nm = c.names.get(pid, {})
            performers.append({"id": pid, "name": nm.get("name") or pid, "type": nm.get("type"), "role": role,
                               "certainty": cert, "image_url": nm.get("image_url")})
        order = {"featured": 0, "present": 1, "maybe": 2}
        performers.sort(key=lambda p: (order[p["certainty"]], p["type"] == "ensemble"))
        n_versions = conn.execute("SELECT COUNT(DISTINCT performance_id) FROM program WHERE work_id=?",
                                  (e.work_id,)).fetchone()[0] if e.work_id else 0
        same = [o for o in c.excerpts if o.number_key == e.number_key and o.id != e.id]
        out = {
            "id": e.id, "title": e.title, "track_title": e.track_title, "kind": e.kind, "source": e.source,
            "start_s": e.start_s, "end_s": e.end_s,
            "duration_s": round(e.duration_s, 1) if e.duration_s else None, "duration_estimated": e.duration_estimated,
            "track": track_shape(t) if t else None,
            "stream_url": f"/api/tracks/{e.track_id}/stream",
            "work": {"id": e.work_id, "title": e.work_title} if e.work_id else None,
            "composer": {"id": e.composer_id, "name": c.names.get(e.composer_id, {}).get("name") or e.composer_name}
            if e.composer_id else None,
            "performance": perf[0] if perf else None,
            "performers": performers,
            "characters": e.characters,
            "highlight": {"id": e.famous.key, "title": e.famous.title, "type": e.famous.type, "tier": e.famous.tier}
            if e.famous else None,
            "number_key": e.number_key, "work_key": e.work_key,
            "liner_notes_url": f"/api/performances/{e.performance_id}/liner-notes",
            "versions_url": f"/api/works/{e.work_id}/versions" if e.work_id else None,
            "version_count": n_versions,
            "same_number": [{"excerpt_id": o.id, "performance_id": o.performance_id, "title": o.title} for o in same][:20],
        }
        if reasons is not None:
            out["reasons"] = [x for x in reasons if x]
        return out

    def ensure_station(sid: str, c: Catalog) -> dict:
        st = store.station(sid)
        if st:
            return st
        typ, _, ident = sid.partition(":")
        if typ in SEED_TYPES and ident and c.names.get(ident, {}).get("type") == typ:
            seeds = [{"type": typ, "id": ident, "weight": 1.0}]
            return store.upsert_station(sid, station_name(seeds, c.names), seeds, {})
        raise HTTPException(404, "station not found")

    # ---- seeded stations ---------------------------------------------------------------------------------
    @r.get("/seeds")
    def seeds(q: str | None = None, type: Literal["composer", "person", "ensemble"] | None = None,
              limit: int = Query(50, ge=1, le=500)):
        items = cat().seed_options(q, type, limit)
        return {"items": items, "total": len(items)}

    @r.get("/stations")
    def stations():
        return {"items": store.stations()}

    @r.post("/stations")
    def create_station(body: StationIn):
        c = cat()
        seeds = []
        for s in body.seeds:
            nm = c.names.get(s.id)
            if not nm or nm["type"] != s.type:
                raise HTTPException(404, f"unknown {s.type} {s.id!r}")
            seeds.append(s.model_dump())
        bad = set(body.settings or {}) - set(DEFAULT_SETTINGS)
        if bad:
            raise HTTPException(422, f"unknown settings {sorted(bad)}; allowed {sorted(DEFAULT_SETTINGS)}")
        for k, v in (body.settings or {}).items():
            d = DEFAULT_SETTINGS[k]
            ok = isinstance(v, bool) if isinstance(d, bool) else \
                (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100000)
            if not ok:
                raise HTTPException(422, f"setting {k!r} must be {'a boolean' if isinstance(d, bool) else 'a non-negative number'}")
        sid = station_id_for(seeds)
        st = store.upsert_station(sid, body.name or station_name(seeds, c.names), seeds, body.settings or {})
        st["excerpt_count"] = sum(1 for e in c.excerpts if any(c.matches(e, s) > 0 for s in seeds))
        return st

    @r.get("/stations/{sid}")
    def station(sid: str):
        c = cat()
        st = ensure_station(sid, c)
        st["excerpt_count"] = sum(1 for e in c.excerpts if any(c.matches(e, s) > 0 for s in st["seeds"]))
        st["effective_settings"] = {**DEFAULT_SETTINGS, **st["settings"]}
        return st

    @r.delete("/stations/{sid}")
    def delete_station(sid: str):
        if not store.delete_station(sid):
            raise HTTPException(404, "station not found")
        return {"ok": True}

    @r.get("/stations/{sid}/next")
    def next_excerpts(sid: str, count: int = Query(3, ge=1, le=20),
                      exclude: str | None = Query(None, description="comma-separated excerpt ids already queued"),
                      conn: sqlite3.Connection = Depends(get_db)):
        c = cat()
        st = ensure_station(sid, c)
        picks = pick_next(c, st["seeds"], store.history(sid, 300), store.global_stats(), count=count,
                          exclude=[x for x in (exclude or "").split(",") if x], settings=st["settings"], rng=rnd)
        if not picks:
            raise HTTPException(404, "no excerpts for this station yet")
        return {"station": {"id": st["id"], "name": st["name"]},
                "items": [excerpt_shape(p.excerpt, c, conn, p.reasons) for p in picks]}

    @r.get("/stations/{sid}/history")
    def history(sid: str, limit: int = Query(50, ge=1, le=1000)):
        if not store.station(sid):
            raise HTTPException(404, "station not found")
        c = cat()
        items = store.history(sid, limit)
        for h in items:
            e = c.by_id.get(h["excerpt_id"])
            h["title"] = e.title if e else None
        return {"items": items}

    @r.get("/excerpts")
    def excerpts(seed_type: Literal["composer", "person", "ensemble"] | None = None, seed_id: str | None = None,
                 highlights: bool = False, limit: int = Query(100, ge=1, le=2000), offset: int = Query(0, ge=0),
                 conn: sqlite3.Connection = Depends(get_db)):
        c = cat()
        items = c.excerpts
        if seed_id:
            seed = {"type": seed_type or c.names.get(seed_id, {}).get("type"), "id": seed_id}
            items = [e for e in items if c.matches(e, seed) > 0]
        if highlights:
            items = [e for e in items if e.famous]
        items = sorted(items, key=lambda e: (e.famous is None, e.famous.tier if e.famous else 9, e.id))
        return {"total": len(items), "items": [excerpt_shape(e, c, conn) for e in items[offset:offset + limit]]}

    @r.get("/excerpts/{eid:path}")
    def excerpt(eid: str, conn: sqlite3.Connection = Depends(get_db)):
        c = cat()
        e = c.by_id.get(eid)
        if not e:
            raise HTTPException(404, "excerpt not found")
        return excerpt_shape(e, c, conn, detail=True)

    @r.post("/plays")
    def log_play(body: PlayIn):
        c = cat()
        e = c.by_id.get(body.excerpt_id)
        if not e:
            raise HTTPException(404, "excerpt not found")
        if body.station_id and not store.station(body.station_id):
            ensure_station(body.station_id, c)
        pid = store.start_play(body.station_id, e.summary(), body.client)
        if body.outcome != "playing":
            store.finish_play(pid, body.outcome, body.listened_s)
        return {"ok": True, "play_id": pid}

    @r.post("/plays/{play_id}")
    def update_play(play_id: int, body: PlayUpdate):
        if not store.finish_play(play_id, body.outcome, body.listened_s):
            raise HTTPException(404, "play not found")
        return {"ok": True}

    # ---- live internet radio -----------------------------------------------------------------------------
    @r.get("/live")
    def live_list():
        items = live.list()
        return {"items": items, "total": len(items)}

    @r.get("/live/{sid}/now-playing")
    def live_now(sid: str):
        out = live.now_playing(sid)
        if out is None:
            raise HTTPException(404, "station not found")
        return out

    r.state = {"store": store, "catalog": catalog, "live": live, "famous": famous}  # for tests
    return r
