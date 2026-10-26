"""FastAPI application implementing docs/API.md."""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import ipaddress
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.encoders import jsonable_encoder as jsonable
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import cd as cdmod
from . import db as dbmod
from .accounts_store import AccountError
from .config import Settings, load_settings
from .media import DIRECT_MIME, IMAGE_MIME, ImageCache, Transcoder, needs_transcode, safe_join
from .liner import ProgramStore, TranscriptStore, VERSION_SORTS, liner_notes, sort_versions, version_rows
from .spotlight import Spotlight, credit_rating
from .text import fold

log = logging.getLogger("sethster")
STATIC = Path(__file__).resolve().parent / "static"

SORTS = {
    "date": "p.date IS NULL, p.date ASC, p.id",
    "-date": "p.date IS NULL, p.date DESC, p.id",
    "title": "p.title COLLATE NOCASE ASC, p.date",
    "-title": "p.title COLLATE NOCASE DESC, p.date",
    "duration": "p.duration_s ASC",
    "-duration": "p.duration_s DESC",
}


class AnswerIn(BaseModel):
    performance_id: str
    question: str
    answer: str | None = None


def _j(v, default=None):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _credit_text(v) -> str | None:
    """A portrait credit as one line for the app, e.g. "Jane Doe · CC BY-SA 3.0" (the app reads it as text)."""
    c = _j(v, v)
    if not c:
        return None
    if not isinstance(c, dict):
        return str(c)
    return " · ".join(str(c[k]) for k in ("artist", "license") if c.get(k)) or (str(c["credit"]) if c.get("credit") else None)


# ---- shape builders ----------------------------------------------------------------------------------

def person_ref(row) -> dict:
    return {"id": row["id"], "name": row["name"], "kind": row["kind"],
            "image_url": f"/api/people/{row['id']}/image" if row["image_src"] else None}


def composer_summary(row) -> dict:
    return {"id": row["id"], "name": row["name"], "years": row["years"],
            "image_url": f"/api/composers/{row['id']}/image" if row["image_src"] else None,
            "performance_count": row["performance_count"]}


def summaries(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT p.*, v.name AS venue_name, v.city AS venue_city FROM performances p
            LEFT JOIN venues v ON v.id = p.venue_id WHERE p.id IN ({marks})""", ids).fetchall()
    comps: dict[str, list[dict]] = {}
    for c in conn.execute(
            f"""SELECT pc.performance_id, c.id, c.name FROM performance_composers pc
                JOIN composers c ON c.id = pc.composer_id WHERE pc.performance_id IN ({marks})
                ORDER BY pc.performance_id, pc.ord""", ids):
        comps.setdefault(c["performance_id"], []).append({"id": c["id"], "name": c["name"]})
    by_id = {}
    for r in rows:
        by_id[r["id"]] = {
            "id": r["id"], "title": r["title"], "subtitle": r["subtitle"], "composers": comps.get(r["id"], []),
            "date": r["date"], "date_status": r["date_status"],
            "venue": {"id": r["venue_id"], "name": r["venue_name"], "city": r["venue_city"]} if r["venue_id"] else None,
            "kind": r["kind"], "collection": r["collection"],
            "duration_s": int(round(r["duration_s"])) if r["duration_s"] is not None else None,
            "artwork_url": f"/api/performances/{r['id']}/artwork" if r["artwork_path"] else None,
            # CDs: {w, h, generic, roles} of the chosen cover (roles: front/back/disc, see the artwork endpoint)
            "artwork": _j(r["artwork_meta"]) if "artwork_meta" in r.keys() else None,
            "confirmed_ratio": r["confirmed_ratio"],
        }
    return [by_id[i] for i in ids if i in by_id]


def track_shape(r) -> dict:
    return {"id": r["id"], "index": r["idx"], "title": r["title"], "work_id": r["work_id"], "segment": r["segment"],
            "duration_s": r["duration_s"], "stream_url": f"/api/tracks/{r['id']}/stream", "format": r["format"],
            "stream_format": "aac" if needs_transcode(r["stream_path"] or r["path"])
            else PurePosixPath(r["stream_path"] or r["path"]).suffix.lower().lstrip("."),
            "description": r["content"]}


def _tokens(q: str | None) -> list[str]:
    return [t for t in fold(q or "").replace("'", " ").split() if t]


def _like_clauses(column: str, q: str | None) -> tuple[list[str], list[str]]:
    toks = _tokens(q)
    return [f"{column} LIKE ?" for _ in toks], [f"%{t}%" for t in toks]


# ---- auth -----------------------------------------------------------------------------------------------

TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
FUNNEL_OPEN = {"/api/health"}


def from_internet(headers: dict[str, str]) -> bool:
    """True when Tailscale Funnel (the public internet) delivered the request.

    Newer tailscaled marks Funnel requests with `Tailscale-Funnel-Request: ?1` and strips client copies.
    tailscaled 1.58 (the NAS) doesn't, but its Serve proxy always sets X-Forwarded-For to the real client
    address (replacing anything the client sent): a 100.x / fd7a: tailnet address for our own devices, a public
    one for Funnel. No X-Forwarded-For means the request didn't come through Tailscale Serve (LAN, localhost)."""
    if "tailscale-funnel-request" in headers:
        return True
    xff = headers.get("x-forwarded-for")
    if xff is None:
        return False
    try:
        ip = ipaddress.ip_address(xff.split(",")[-1].strip())
    except ValueError:
        return True
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return not (ip.is_loopback or ip.is_private or any(ip in n for n in TAILNET if n.version == ip.version))


def request_token(headers: dict[str, str], query_string: bytes | str = b"") -> str | None:
    h = headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip() or None
    qs = query_string.decode("latin-1") if isinstance(query_string, bytes) else query_string
    vals = parse_qs(qs).get("token")
    return vals[0] if vals and vals[0] else None


def token_ok(token: str | None, tokens: list[str]) -> bool:
    return bool(token) and any(hmac.compare_digest(token.encode(), t.encode()) for t in tokens)


class FunnelGuard:
    """Pure ASGI middleware: a request from the internet (Tailscale Funnel) needs a valid token for every path
    but /api/health, whatever SETHSTER_AUTH says. With no tokens configured, internet requests are refused."""

    def __init__(self, app, tokens: list[str]):
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("path") not in FUNNEL_OPEN:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
            if from_internet(headers) and not token_ok(request_token(headers, scope.get("query_string", b"")),
                                                       self.tokens):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                resp = JSONResponse({"detail": "missing or invalid token"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


# the name may be percent-encoded (?%74oken= is parsed as token too), so any name that decodes to "token"
_TOKEN_QS = re.compile(r"([?&](?:t|%74)(?:o|%6f)(?:k|%6b)(?:e|%65)(?:n|%6e)=)[^&\s\"]*", re.I)


class RedactTokens(logging.Filter):
    """Access-log filter: `?token=` (media URLs carry the session there) is logged as token=REDACTED."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_TOKEN_QS.sub(r"\1REDACTED", a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = _TOKEN_QS.sub(r"\1REDACTED", record.msg)
        return True


def install_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactTokens) for f in access.filters):
        access.addFilter(RedactTokens())


# ---- app ------------------------------------------------------------------------------------------------

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    mode = settings.accounts_mode
    install_log_redaction()
    accounts = None
    if mode != "off":  # docs/plans/accounts.md: legacy tokens and FunnelGuard play no part in dormant/on
        from .accounts_api import BodyLimit, build as build_accounts
        accounts = build_accounts(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.db_path.exists():
            log.warning("DB %s missing; running importer", settings.db_path)
            from .importer import build
            build(settings)
        auth_desc = f"accounts={mode}" if accounts else ("on" if settings.auth_enabled else "OFF (dev)")
        log.info("Sethster: db=%s media_root=%s auth=%s ffmpeg=%s", settings.db_path, settings.media_root,
                 auth_desc, settings.ffmpeg)
        watch = asyncio.create_task(accounts.watch_admins()) if accounts and mode == "on" else None
        try:
            yield
        finally:
            if watch:
                watch.cancel()
            if accounts:
                await accounts.apns.aclose()

    docs = {} if mode != "on" else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Sethster", version="0.1.0", lifespan=lifespan, **docs)
    app.state.settings = settings
    app.state.accounts = accounts
    if accounts is None and settings.auth_scope == "funnel":   # off unless SETHSTER_AUTH=funnel
        app.add_middleware(FunnelGuard, tokens=settings.tokens)
    if accounts is not None:
        app.add_middleware(BodyLimit)

    @app.exception_handler(AccountError)
    async def account_error(request: Request, e: AccountError):
        return JSONResponse({"code": e.code, "detail": e.detail}, status_code=e.status, headers=e.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, e: RequestValidationError):
        if accounts is not None and request.url.path.startswith(("/api/auth/", "/api/admin/")):
            return JSONResponse({"code": "invalid", "detail": jsonable(e.errors())}, status_code=422)
        return await request_validation_exception_handler(request, e)
    spotlight = Spotlight(settings.data_dir,
                          Path(os.environ["SETHSTER_CONTEXT"]) if os.environ.get("SETHSTER_CONTEXT") else None,
                          Path(os.environ["SETHSTER_CALENDAR"]) if os.environ.get("SETHSTER_CALENDAR") else None)
    transcoder = Transcoder(settings.cache_dir, settings.ffmpeg, settings.aac_encoder, settings.aac_bitrate)
    images = ImageCache(settings.cache_dir)
    transcripts = TranscriptStore(settings.transcripts_dir)
    programs = ProgramStore(settings.programs_dir)

    def get_db() -> Iterator[sqlite3.Connection]:
        if not settings.db_path.exists():
            raise HTTPException(503, "database not built; run the importer")
        conn = dbmod.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def legacy_auth(request: Request) -> None:
        # SETHSTER_AUTH=funnel: only internet (Funnel) requests need the token, and FunnelGuard has already
        # checked those; the tailnet and LAN stay open. SETHSTER_AUTH=all (default): every request, when
        # SETHSTER_TOKENS is set.
        if settings.auth_scope == "funnel" or not settings.auth_enabled:
            return
        headers = {"authorization": request.headers.get("authorization", "")}
        if not token_ok(request_token(headers, request.url.query), settings.tokens):
            raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})

    # every content router is mounted with `auth`: the legacy token check, or accounts' approved_user (`on`)
    auth = accounts.content if accounts else legacy_auth
    public = APIRouter(prefix="/api")
    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])
    Limit = Query(50, ge=1, le=1000)
    Offset = Query(0, ge=0)

    def server_status(conn: sqlite3.Connection) -> dict:
        n = conn.execute("SELECT COUNT(*) FROM performances").fetchone()[0]
        imported = conn.execute("SELECT value FROM meta WHERE key='imported_at'").fetchone()
        mr = settings.media_root
        out = {"ok": True, "performances": n, "imported_at": imported[0] if imported else None,
               "media_root_ok": bool(mr and mr.is_dir()), "auth": settings.auth_enabled,
               "auth_scope": settings.auth_scope}
        if accounts:
            out.update(auth=True, auth_scope="accounts", accounts=mode)
        return out

    if accounts is None:
        @public.get("/health")
        def health(conn: sqlite3.Connection = Depends(get_db)):
            return server_status(conn)
    else:
        @public.get("/health")
        def health():
            # public, so nothing about the catalog; the counts are at /api/status
            return {"ok": True}

    @api.get("/status")
    def status(conn: sqlite3.Connection = Depends(get_db)):
        return server_status(conn)

    # ---- performances ----------------------------------------------------------------------------------
    def query_performances(conn, *, collection=None, composer=None, person=None, venue=None, year=None, q=None,
                           work=None, kind=None, city=None, sort="date", limit=50, offset=0) -> tuple[list[str], int]:
        where, params = [], []
        if collection:
            where.append("p.collection = ? COLLATE NOCASE"); params.append(collection)
        if composer:
            where.append("EXISTS (SELECT 1 FROM performance_composers pc WHERE pc.performance_id=p.id AND pc.composer_id=?)")
            params.append(composer)
        if person:
            where.append("EXISTS (SELECT 1 FROM credits c WHERE c.performance_id=p.id AND c.person_id=?)")
            params.append(person)
        if work:
            where.append("EXISTS (SELECT 1 FROM program g WHERE g.performance_id=p.id AND g.work_id=?)")
            params.append(work)
        if venue:
            where.append("p.venue_id = ?"); params.append(venue)
        if city:
            where.append("EXISTS (SELECT 1 FROM venues v WHERE v.id=p.venue_id AND v.city = ? COLLATE NOCASE)")
            params.append(city)
        if kind:
            where.append("p.kind = ?"); params.append(kind)
        if year:
            where.append("substr(p.date,1,4) = ?"); params.append(str(year))
        cl, cp = _like_clauses("p.search_text", q)
        where += cl; params += cp
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM performances p {w}", params).fetchone()[0]
        order = SORTS.get(sort or "date")
        if order is None:
            raise HTTPException(400, f"sort must be one of {sorted(SORTS)}")
        ids = [r[0] for r in conn.execute(f"SELECT p.id FROM performances p {w} ORDER BY {order} LIMIT ? OFFSET ?",
                                          params + [limit, offset])]
        return ids, total

    @api.get("/performances")
    def list_performances(collection: str | None = None, composer: str | None = None, person: str | None = None,
                          venue: str | None = None, year: int | None = None, q: str | None = None,
                          work: str | None = None, kind: str | None = None, city: str | None = None,
                          sort: str = "date", limit: int = Limit, offset: int = Offset,
                          conn: sqlite3.Connection = Depends(get_db)):
        ids, total = query_performances(conn, collection=collection, composer=composer, person=person, venue=venue,
                                        year=year, q=q, work=work, kind=kind, city=city, sort=sort, limit=limit,
                                        offset=offset)
        return {"items": summaries(conn, ids), "total": total}

    @api.get("/performances/{pid}")
    def performance_detail(pid: str, conn: sqlite3.Connection = Depends(get_db)):
        r = conn.execute("SELECT * FROM performances WHERE id=?", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "performance not found")
        out = summaries(conn, [pid])[0]
        program = [{"work_id": g["work_id"], "title": g["title"],
                    "composer": {"id": g["composer_id"], "name": g["cname"]},
                    "catalogue": g["catalogue"], "notes": g["notes"]}
                   for g in conn.execute("""SELECT g.*, c.name AS cname FROM program g
                                            LEFT JOIN composers c ON c.id=g.composer_id
                                            WHERE g.performance_id=? ORDER BY g.ord""", (pid,))]
        raw_sources = [s for s in _j(r["sources"], []) if isinstance(s, dict)]
        by_id = {str(s.get("id")): s for s in raw_sources if s.get("id") is not None}

        def credit_shape(c) -> dict:
            sids = _j(c["source_ids"], [])
            # rating: 1-5 stars + percent sure, so the app can show plain stars instead of statuses and source ids
            return {"person": person_ref(c), "role": c["role"], "work_id": c["work_id"], "status": c["status"],
                    "notes": c["notes"], "source_ids": sids,
                    "rating": credit_rating(c["status"], [str(x) for x in sids], by_id)}
        credits = [credit_shape(c)
                   for c in conn.execute("""SELECT c.role, c.work_id, c.status, c.notes, c.source_ids,
                                                   p.id, p.name, p.kind, p.image_src
                                            FROM credits c JOIN people p ON p.id=c.person_id
                                            WHERE c.performance_id=? ORDER BY c.ord""", (pid,))]
        tracks = [track_shape(t) for t in conn.execute("SELECT * FROM tracks WHERE performance_id=? ORDER BY idx", (pid,))]
        sources = [{"id": s.get("id"), "url": s.get("url"), "wayback_url": s.get("wayback_url"), "title": s.get("title"),
                    "type": s.get("type"), "date_specific": s.get("date_specific"), "excerpt": s.get("excerpt"),
                    "read_ok": s.get("read_ok")} for s in raw_sources]
        resources = [{"url": s.get("url"), "wayback_url": s.get("wayback_url"), "title": s.get("title"),
                      "kind": s.get("kind"), "description": s.get("description")} for s in _j(r["resources"], [])]
        answers = {a["question"]: a for a in conn.execute(
            "SELECT question, answer, answered_at FROM answers WHERE performance_id=?", (pid,))}
        oq = _j(r["open_questions"], [])
        questions = [{"question": q, "answer": (answers.get(q) or {"answer": None})["answer"],
                      "answered_at": (answers.get(q) or {"answered_at": None})["answered_at"]} for q in oq]
        questions += [{"question": a["question"], "answer": a["answer"], "answered_at": a["answered_at"]}
                      for q, a in answers.items() if q not in oq]
        out.update({
            "summary": r["summary"], "date_evidence": r["date_evidence"],
            "candidate_dates": _j(r["candidate_dates"], []), "broadcast_date": r["broadcast_date"],
            "broadcaster": r["broadcaster"], "event_name": r["event_name"], "release": _j(r["release"]),
            "program": program, "credits": credits, "tracks": tracks, "sources": sources, "resources": resources,
            "open_questions": oq, "questions": questions, "folder": r["folder"],
        })
        return out

    @api.get("/performances/{pid}/artwork")
    def performance_artwork(pid: str, size: int | None = None, role: str | None = None,
                            conn: sqlite3.Connection = Depends(get_db)):
        """The cover. CDs (docs/plans/cd-catalog.md §4.4) also take ?size=160|480|1200&role=front|back|disc,
        resolved only through the CD art manifest; with no query the stored cover is served as before."""
        if size is not None and size not in cdmod.ART_SIZES:
            raise HTTPException(422, f"size must be one of {list(cdmod.ART_SIZES)}")
        if role is not None and role not in cdmod.ART_ROLES:
            raise HTTPException(422, f"role must be one of {list(cdmod.ART_ROLES)}")
        r = conn.execute("SELECT artwork_path FROM performances WHERE id=?", (pid,)).fetchone()
        if not r:
            raise HTTPException(404, "no artwork")
        manifest = cdmod.load_manifest(settings.data_dir, pid)
        if manifest is not None:  # a CD: pictures live under data/media/cd, never on the share
            if size is None and role is None:
                rel = r["artwork_path"]
            else:
                rel = cdmod.art_file(settings.data_dir, manifest, role or "front", size or cdmod.DEFAULT_SIZE)
            if not rel or not rel.startswith("media/cd/"):
                raise HTTPException(404, "no artwork")
            root = settings.data_dir
        else:
            if (role or "front") != "front" or not r["artwork_path"] or not settings.media_root:
                raise HTTPException(404, "no artwork")
            rel, root = r["artwork_path"], settings.media_root
        try:
            p = safe_join(root, rel)
        except ValueError:
            raise HTTPException(404, "no artwork")
        if not p.is_file():
            raise HTTPException(404, "artwork file missing")
        mime = IMAGE_MIME.get(p.suffix.lower(), "application/octet-stream")
        return FileResponse(p, media_type=mime, headers={"Cache-Control": "private, max-age=604800"})

    # ---- home / browse -----------------------------------------------------------------------------------
    def composer_rows(conn, where: str = "", params=(), order="c.sort_name", limit=None, offset=0):
        sql = f"""SELECT c.*, (SELECT COUNT(DISTINCT pc.performance_id) FROM performance_composers pc
                               WHERE pc.composer_id=c.id) AS performance_count
                  FROM composers c {where} ORDER BY {order}"""
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        return conn.execute(sql, params).fetchall()

    _complete = {"at": 0.0, "v": {}}
    ART_BOOST = 14 * 86400  # Recently Completed ranks a recording with a picture as if it finished two weeks later

    def completion(conn) -> dict[str, tuple[float, bool]]:
        """Performances with the full experience (researched, transcribed and split) -> (when the last of those
        landed, has a picture). Suggestions only offer these; the rest of the catalog is found by search.
        Re-checked at most once a minute."""
        if time.time() - _complete["at"] < 60:
            return _complete["v"]

        def mtime(p: Path) -> float | None:
            try:
                return p.stat().st_mtime
            except OSError:
                return None
        done = {}
        for pid, art in conn.execute("SELECT id, artwork_path FROM performances"):
            research = [t for t in (mtime(d / f"{pid}.json") for d in settings.result_dirs) if t]
            parts = [max(research, default=None),
                     mtime(settings.transcripts_dir / f"{pid}.json") if settings.transcripts_dir else None,
                     mtime(settings.data_dir / "sections" / f"{pid}.json")]
            if all(parts):
                done[pid] = (max(parts), bool(art))
        _complete.update(at=time.time(), v=done)
        return done

    def recently_completed(conn, limit: int) -> list[str]:
        """Performances that most recently got the full experience, newest first, with pictured ones pushed
        well ahead (ART_BOOST)."""
        done = completion(conn)
        return sorted(done, key=lambda p: done[p][0] + (ART_BOOST if done[p][1] else 0), reverse=True)[:limit]

    @api.get("/home")
    def home(limit: int = Query(12, ge=1, le=100), today: str | None = Query(None, description="MM-DD override"),
             date: str | None = Query(None, description="YYYY-MM-DD (the phone's local date) for the spotlight"),
             conn: sqlite3.Connection = Depends(get_db)):
        recent = recently_completed(conn, limit)
        day = dt.date.today()
        if date:
            try:
                day = dt.date.fromisoformat(date)
            except ValueError:
                raise HTTPException(422, "date must be YYYY-MM-DD")
        md = today or day.strftime("%m-%d")
        done = completion(conn)
        otd = [r[0] for r in conn.execute(
            "SELECT id FROM performances WHERE length(date)=10 AND substr(date,6,5)=? ORDER BY date DESC", (md,))
            if r[0] in done]
        otd.sort(key=lambda p: not done[p][1])  # pictured ones first, newest first within each
        collections = [{"name": r[0], "count": r[1]} for r in conn.execute(
            "SELECT collection, COUNT(*) FROM performances WHERE collection IS NOT NULL GROUP BY collection "
            "ORDER BY collection COLLATE NOCASE")]
        comps = [composer_summary(c) for c in composer_rows(conn, order="performance_count DESC, c.sort_name", limit=24)]
        try:
            spot = spotlight.build(conn, day, done, summaries)
        except Exception:  # noqa: BLE001 - the spotlight is a nicety; Home must still load
            log.exception("spotlight failed")
            spot = None
        # collections stays for build 7 and CarPlay; build 8's Home shows the spotlight instead
        return {"recent": summaries(conn, recent), "on_this_day": summaries(conn, otd),
                "collections": collections, "composers": comps, "spotlight": spot}

    @api.get("/composers")
    def list_composers(q: str | None = None, limit: int = Limit, offset: int = Offset,
                       conn: sqlite3.Connection = Depends(get_db)):
        cl, cp = _like_clauses("c.search_text", q)
        where = ("WHERE " + " AND ".join(cl)) if cl else ""
        total = conn.execute(f"SELECT COUNT(*) FROM composers c {where}", cp).fetchone()[0]
        rows = composer_rows(conn, where, cp, limit=limit, offset=offset)
        return {"items": [composer_summary(c) for c in rows], "total": total}

    @api.get("/composers/{cid}")
    def composer_detail(cid: str, conn: sqlite3.Connection = Depends(get_db)):
        rows = composer_rows(conn, "WHERE c.id=?", (cid,))
        if not rows:
            raise HTTPException(404, "composer not found")
        c = rows[0]
        works = [{"id": w["id"], "title": w["title"], "performance_count": w["n"]} for w in conn.execute(
            """SELECT w.id, w.title, COUNT(DISTINCT g.performance_id) AS n FROM works w
               LEFT JOIN program g ON g.work_id=w.id WHERE w.composer_id=? GROUP BY w.id
               ORDER BY n DESC, w.title COLLATE NOCASE""", (cid,))]
        return {**composer_summary(c), "wikidata": c["wikidata"], "bio": c["bio"], "bio_long": c["intro"],
                "description": c["description"], "wikipedia_url": c["wikipedia_url"],
                "image_credit": _credit_text(c["image_credit"]), "image_credit_detail": _j(c["image_credit"]), "works": works}

    @api.get("/composers/{cid}/image")
    def composer_image(cid: str, conn: sqlite3.Connection = Depends(get_db)):
        r = conn.execute("SELECT image_src FROM composers WHERE id=?", (cid,)).fetchone()
        return _remote_image("composers", cid, r["image_src"] if r else None)

    @api.get("/works/{wid}")
    def work_detail(wid: str, conn: sqlite3.Connection = Depends(get_db)):
        w = conn.execute("""SELECT w.*, c.name AS cname FROM works w LEFT JOIN composers c ON c.id=w.composer_id
                            WHERE w.id=?""", (wid,)).fetchone()
        if not w:
            raise HTTPException(404, "work not found")
        ids, _ = query_performances(conn, work=wid, limit=1000)
        return {"id": w["id"], "title": w["title"], "composer": {"id": w["composer_id"], "name": w["cname"]},
                "catalogue": w["catalogue"], "wikidata": w["wikidata"], "description": w["description"],
                "wikipedia_url": w["wikipedia_url"], "synopsis": w["synopsis"], "performances": summaries(conn, ids)}

    def people_items(conn, where: str, params, limit, offset):
        rows = conn.execute(
            f"""SELECT p.*, (SELECT COUNT(*) FROM credits c WHERE c.person_id=p.id) AS credit_count
                FROM people p {where} ORDER BY p.sort_name LIMIT ? OFFSET ?""", list(params) + [limit, offset])
        return [{**person_ref(r), "credit_count": r["credit_count"]} for r in rows]

    @api.get("/people")
    def list_people(q: str | None = None, kind: str | None = None, limit: int = Limit, offset: int = Offset,
                    conn: sqlite3.Connection = Depends(get_db)):
        cl, cp = _like_clauses("p.search_text", q)
        if kind:
            cl.append("p.kind = ?"); cp.append(kind)
        where = ("WHERE " + " AND ".join(cl)) if cl else ""
        total = conn.execute(f"SELECT COUNT(*) FROM people p {where}", cp).fetchone()[0]
        return {"items": people_items(conn, where, cp, limit, offset), "total": total}

    @api.get("/people/{person_id}")
    def person_detail(person_id: str, conn: sqlite3.Connection = Depends(get_db)):
        p = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
        if not p:
            raise HTTPException(404, "person not found")
        rows = conn.execute("""SELECT c.performance_id, c.role, c.status, c.work_id FROM credits c
                               JOIN performances pf ON pf.id=c.performance_id WHERE c.person_id=?
                               ORDER BY pf.date IS NULL, pf.date, c.ord""", (person_id,)).fetchall()
        sums = {s["id"]: s for s in summaries(conn, list(dict.fromkeys(r["performance_id"] for r in rows)))}
        credits = [{"performance": sums[r["performance_id"]], "role": r["role"], "status": r["status"],
                    "work_id": r["work_id"]} for r in rows if r["performance_id"] in sums]
        return {**person_ref(p), "years": p["years"], "wikidata": p["wikidata"], "bio": p["bio"],
                "bio_long": p["intro"], "description": p["description"], "wikipedia_url": p["wikipedia_url"],
                "image_credit": _credit_text(p["image_credit"]), "image_credit_detail": _j(p["image_credit"]), "credits": credits}

    def _remote_image(kind: str, ident: str, url: str | None):
        if not url:
            raise HTTPException(404, "no image")
        path = images.get(kind, ident, url)
        if not path:
            raise HTTPException(404, "image unavailable")
        mime = IMAGE_MIME.get(path.suffix.lower(), "image/jpeg")
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=2592000"})

    @api.get("/people/{person_id}/image")
    def person_image(person_id: str, conn: sqlite3.Connection = Depends(get_db)):
        r = conn.execute("SELECT image_src FROM people WHERE id=?", (person_id,)).fetchone()
        return _remote_image("people", person_id, r["image_src"] if r else None)

    @api.get("/venues/{vid}")
    def venue_detail(vid: str, conn: sqlite3.Connection = Depends(get_db)):
        v = conn.execute("SELECT * FROM venues WHERE id=?", (vid,)).fetchone()
        if not v:
            raise HTTPException(404, "venue not found")
        ids, _ = query_performances(conn, venue=vid, limit=1000)
        return {"id": v["id"], "name": v["name"], "city": v["city"], "wikidata": v["wikidata"],
                "description": v["description"], "wikipedia_url": v["wikipedia_url"],
                "performances": summaries(conn, ids)}

    @api.get("/search")
    def search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=200),
               conn: sqlite3.Connection = Depends(get_db)):
        if not _tokens(q):
            return {"performances": [], "people": [], "works": [], "composers": []}
        ids, _ = query_performances(conn, q=q, sort="-date", limit=limit)
        pc, pp = _like_clauses("p.search_text", q)
        people = people_items(conn, "WHERE " + " AND ".join(pc), pp, limit, 0)
        wc, wp = _like_clauses("w.search_text", q)
        works = [{"id": w["id"], "title": w["title"], "composer": {"id": w["composer_id"], "name": w["cname"]},
                  "performance_count": w["n"]} for w in conn.execute(
            f"""SELECT w.*, c.name AS cname, (SELECT COUNT(DISTINCT g.performance_id) FROM program g
                                               WHERE g.work_id=w.id) AS n
                FROM works w LEFT JOIN composers c ON c.id=w.composer_id WHERE {' AND '.join(wc)}
                ORDER BY n DESC, w.title LIMIT ?""", wp + [limit])]
        cc, cp = _like_clauses("c.search_text", q)
        comps = [composer_summary(c) for c in composer_rows(conn, "WHERE " + " AND ".join(cc), cp, limit=limit)]
        return {"performances": summaries(conn, ids), "people": people, "works": works, "composers": comps}

    # ---- audio --------------------------------------------------------------------------------------------
    @api.api_route("/tracks/{track_id}/stream", methods=["GET", "HEAD"])
    def stream(track_id: str, conn: sqlite3.Connection = Depends(get_db)):
        t = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not t:
            raise HTTPException(404, "track not found")
        root = settings.media_root
        if not root or not root.is_dir():
            raise HTTPException(503, "media share not mounted")
        rel = t["stream_path"] or t["path"]
        try:
            src = safe_join(root, rel)
        except ValueError:
            raise HTTPException(404, "bad track path")
        if not src.is_file():
            raise HTTPException(404, f"audio file missing on share: {rel}")
        ext = PurePosixPath(rel).suffix.lower()
        if needs_transcode(rel):
            try:
                out = transcoder.ensure(src)
            except Exception as e:  # noqa: BLE001
                log.exception("transcode failed")
                raise HTTPException(500, f"transcode failed: {e}")
            mime = "audio/mp4"
            # warm the next couple of tracks of this performance that also need transcoding
            nxt = conn.execute("SELECT stream_path, path FROM tracks WHERE performance_id=? AND idx>? ORDER BY idx LIMIT 2",
                               (t["performance_id"], t["idx"])).fetchall()
            warm = []
            for n in nxt:
                p = n["stream_path"] or n["path"]
                if needs_transcode(p):
                    try:
                        warm.append(safe_join(root, p))
                    except ValueError:
                        pass
            if warm:
                transcoder.prewarm([w for w in warm if w.is_file()])
        else:
            out, mime = src, DIRECT_MIME[ext]
        return FileResponse(out, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})

    # ---- Ask Dad --------------------------------------------------------------------------------------------
    @api.get("/questions")
    def list_questions(performance_id: str | None = None, answered: bool | None = None,
                       limit: int = Query(50, ge=1, le=2000), offset: int = Offset,
                       conn: sqlite3.Connection = Depends(get_db)):
        base = """
          SELECT q.performance_id, q.ord, q.question, a.answer, a.answered_at FROM questions q
            LEFT JOIN answers a ON a.performance_id=q.performance_id AND a.question=q.question
          UNION ALL
          SELECT a.performance_id, 1000000, a.question, a.answer, a.answered_at FROM answers a
            WHERE NOT EXISTS (SELECT 1 FROM questions q WHERE q.performance_id=a.performance_id AND q.question=a.question)
        """
        where, params = [], []
        if performance_id:
            where.append("x.performance_id = ?"); params.append(performance_id)
        if answered is True:
            where.append("x.answer IS NOT NULL")
        elif answered is False:
            where.append("x.answer IS NULL")
        w = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"""SELECT x.*, p.title AS performance_title FROM ({base}) x
                  JOIN performances p ON p.id=x.performance_id {w}
                  ORDER BY p.date IS NULL, p.date, x.performance_id, x.ord"""
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        rows = conn.execute(sql + " LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        return {"items": [{"performance_id": r["performance_id"], "performance_title": r["performance_title"],
                           "question": r["question"], "answer": r["answer"], "answered_at": r["answered_at"]}
                          for r in rows], "total": total}

    @api.post("/questions/answer")
    def answer_question(body: AnswerIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        if not conn.execute("SELECT 1 FROM performances WHERE id=?", (body.performance_id,)).fetchone():
            raise HTTPException(404, "performance not found")
        if not body.question.strip():
            raise HTTPException(422, "question is required")
        deleting = body.answer is None or not body.answer.strip()
        if accounts and deleting:  # any approved user may answer; only admins may change or delete an answer
            accounts.require_admin(request)
        if deleting:
            conn.execute("DELETE FROM answers WHERE performance_id=? AND question=?", (body.performance_id, body.question))
        else:
            now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            row = (body.performance_id, body.question, body.answer.strip(), now)
            # insert-only first, so two first answers can't both skip the admin check and overwrite each other
            if not accounts or conn.execute("INSERT OR IGNORE INTO answers VALUES (?,?,?,?)", row).rowcount == 0:
                if accounts:
                    accounts.require_admin(request)
                conn.execute("INSERT OR REPLACE INTO answers VALUES (?,?,?,?)", row)
        conn.commit()
        return {"ok": True}

    # ---- Phase 3: transcripts, liner notes, program notes, versions ---------------------------------------
    def _need_perf(conn, pid):
        if not conn.execute("SELECT 1 FROM performances WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "performance not found")

    @api.get("/transcripts/{pid}")
    def transcript(pid: str, conn: sqlite3.Connection = Depends(get_db)):
        _need_perf(conn, pid)
        t = transcripts.mapped(conn, pid)
        if t is None:
            raise HTTPException(404, "no transcript for this performance")
        return t

    @api.get("/performances/{pid}/liner-notes")
    def performance_liner_notes(pid: str, conn: sqlite3.Connection = Depends(get_db)):
        doc = liner_notes(conn, pid, transcripts, programs)
        if doc is None:
            raise HTTPException(404, "performance not found")
        return doc

    @api.get("/programs/{pid}")
    def program_list(pid: str, conn: sqlite3.Connection = Depends(get_db)):
        _need_perf(conn, pid)
        return {"items": programs.refs(pid)}

    @api.get("/programs/{pid}/{n}")
    def program_doc(pid: str, n: int, conn: sqlite3.Connection = Depends(get_db)):
        _need_perf(conn, pid)
        doc = programs.get(pid, n)
        if doc is None:
            raise HTTPException(404, "program note not found")
        return doc

    @api.get("/works/{wid}/versions")
    def work_versions(wid: str, sort: str = "date", conn: sqlite3.Connection = Depends(get_db)):
        w = conn.execute("""SELECT w.id, w.title, w.composer_id, c.name AS cname FROM works w
                            LEFT JOIN composers c ON c.id=w.composer_id WHERE w.id=?""", (wid,)).fetchone()
        if not w:
            raise HTTPException(404, "work not found")
        if sort not in VERSION_SORTS:
            raise HTTPException(400, f"sort must be one of {sorted(VERSION_SORTS)}")
        rows = sort_versions(version_rows(conn, wid), sort)
        return {"work": {"id": w["id"], "title": w["title"], "composer": {"id": w["composer_id"], "name": w["cname"]}},
                "items": rows, "total": len(rows)}

    @app.get("/", include_in_schema=False)
    def root():
        return JSONResponse({"name": "sethster-server", "api": "/api", "docs": app.docs_url})

    @app.get("/privacy", include_in_schema=False)
    def privacy():
        # The committed page has a placeholder contact; SETHSTER_CONTACT_EMAIL fills in the real one.
        page = (STATIC / "privacy.html").read_text(encoding="utf-8")
        contact = os.environ.get("SETHSTER_CONTACT_EMAIL", "").strip()
        if contact:
            page = page.replace("contact@example.com", contact)
        return HTMLResponse(page, headers={"Cache-Control": "public, max-age=3600"})

    app.include_router(public)
    if accounts:
        app.include_router(accounts.router)
    app.include_router(api)
    from .radio_api import build_router as radio_router  # Radio: seeded stations + live streams (docs/RADIO.md)
    app.include_router(radio_router(settings, get_db, auth, summaries, track_shape))
    from .calendar_api import build_router as history_router  # On This Day, calendar, context (docs/CALENDAR.md)
    app.include_router(history_router(settings, get_db, auth, summaries, track_shape, completion))
    from .compare_api import build_router as compare_router  # Sections + Compare (docs/sections.md, docs/API.md)
    app.include_router(compare_router(settings, get_db, auth, summaries))
    from .rotation_api import build_router as rotation_router
    app.include_router(rotation_router(settings, get_db, auth, summaries,
                                       *([accounts.rotation_listener, accounts.check_owner] if accounts else [])))
    return app
