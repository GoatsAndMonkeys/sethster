"""/api/rotation routes: a listener's short, ordered offline-ready performance list."""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path, PurePosixPath
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .media import DIRECT_MIME
from .rotation_store import RotationStore, rotation_db_path


class RotationAddIn(BaseModel):
    performance_id: str
    listener: str | None = Field(None, min_length=1)


class RotationOrderIn(BaseModel):
    performance_ids: list[str]
    listener: str | None = Field(None, min_length=1)


def _supplied_listener(request: Request, supplied: str | None) -> str:
    """SETHSTER_ACCOUNTS=off: the listener is whatever the client names (default "default")."""
    return supplied or "default"


def _open_media_file(root: Path, rel: str):
    """Open a regular file beneath root without following catalog symlinks.

    Opening every component relative to an already-open directory also closes
    the usual resolve-then-open race.  The configured root itself may be a
    symlink (a common way to mount the media share); paths below it may not be.
    """
    if not isinstance(rel, str) or not rel:
        raise OSError("missing media path")
    p = PurePosixPath(rel.replace("\\", "/"))
    if p.is_absolute() or not p.parts or any(part in ("", ".", "..") for part in p.parts):
        raise OSError("unsafe media path")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(root, directory)
    try:
        for part in p.parts[:-1]:
            child = os.open(part, directory | nofollow, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(p.parts[-1], os.O_RDONLY | nofollow, dir_fd=fd)
    finally:
        os.close(fd)
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("media path is not a regular file")
        return os.fdopen(file_fd, "rb"), info.st_size
    except BaseException:
        os.close(file_fd)
        raise


def build_router(settings, get_db: Callable, auth: Callable, summaries: Callable,
                 listener_for: Callable = _supplied_listener, check_owner: Callable | None = None) -> APIRouter:
    """listener_for(request, supplied) picks the rotation; with accounts it comes from the session
    (accounts_api.rotation_listener) and the supplied listener is ignored. check_owner(listener) raises when an
    account listener's account has been deleted (RotationStore.add)."""
    store = RotationStore(rotation_db_path(settings))
    r = APIRouter(prefix="/api", dependencies=[Depends(auth)], tags=["rotation"])

    def download_tracks(conn: sqlite3.Connection, performance_id: str) -> list[dict]:
        out = []
        for t in conn.execute("SELECT id, path, format, size FROM tracks WHERE performance_id=? ORDER BY idx",
                              (performance_id,)):
            size = t["size"]
            if settings.media_root and settings.media_root.is_dir():
                try:
                    opened, size = _open_media_file(settings.media_root, t["path"])
                    opened.close()
                except (OSError, ValueError):
                    pass
            out.append({"id": t["id"], "url": f"/api/tracks/{t['id']}/download",
                        "bytes": size, "format": t["format"]})
        return out

    def rotation_item(item: dict, row: dict, conn: sqlite3.Connection) -> dict:
        return {**item, "added_at": row["added_at"],
                "download": {"tracks": download_tracks(conn, item["id"])}}

    def rotation_shape(listener: str, conn: sqlite3.Connection) -> dict:
        rows = store.items(listener)
        by_id = {item["id"]: item for item in summaries(conn, [row["performance_id"] for row in rows])}
        items = []
        for row in rows:
            item = by_id.get(row["performance_id"])
            if item:
                items.append(rotation_item(item, row, conn))
        return {"listener": listener, "items": items}

    @r.get("/rotation")
    def rotation(request: Request, listener: str | None = Query(None, min_length=1),
                 conn: sqlite3.Connection = Depends(get_db)):
        return rotation_shape(listener_for(request, listener), conn)

    @r.post("/rotation")
    def add_rotation(body: RotationAddIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        listener = listener_for(request, body.listener)
        if not conn.execute("SELECT 1 FROM performances WHERE id=?", (body.performance_id,)).fetchone():
            raise HTTPException(404, "performance not found")
        row = store.add(listener, body.performance_id, check_owner)
        # Shape the successful add directly. A concurrent remove after add must
        # not turn this response into StopIteration/500.
        item = summaries(conn, [body.performance_id])[0]
        return rotation_item(item, row, conn)

    @r.delete("/rotation/{performance_id}")
    def remove_rotation(performance_id: str, request: Request, listener: str | None = Query(None, min_length=1),
                        conn: sqlite3.Connection = Depends(get_db)):
        listener = listener_for(request, listener)
        if not conn.execute("SELECT 1 FROM performances WHERE id=?", (performance_id,)).fetchone():
            raise HTTPException(404, "performance not found")
        if not store.remove(listener, performance_id):
            raise HTTPException(404, "performance not in rotation")
        return {"ok": True}

    @r.put("/rotation")
    def reorder_rotation(body: RotationOrderIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        listener = listener_for(request, body.listener)
        if len(body.performance_ids) != len(set(body.performance_ids)):
            raise HTTPException(422, "performance_ids must not contain duplicates")
        if body.performance_ids:
            marks = ",".join("?" * len(body.performance_ids))
            known = {row[0] for row in conn.execute(
                f"SELECT id FROM performances WHERE id IN ({marks})", body.performance_ids)}
            missing = next((pid for pid in body.performance_ids if pid not in known), None)
            if missing:
                raise HTTPException(404, f"performance not found: {missing}")
        if not store.reorder(listener, body.performance_ids):
            raise HTTPException(422, "performance_ids must contain exactly the current rotation")
        return rotation_shape(listener, conn)

    @r.api_route("/tracks/{track_id}/download", methods=["GET", "HEAD"])
    def download(track_id: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        t = conn.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not t:
            raise HTTPException(404, "track not found")
        root = settings.media_root
        if not root or not root.is_dir():
            raise HTTPException(503, "media share not mounted")
        try:
            opened, size = _open_media_file(root, t["path"])
        except (OSError, ValueError):
            raise HTTPException(404, "bad track path")
        ext = PurePosixPath(t["path"]).suffix.lower()
        headers = {"Cache-Control": "private, max-age=86400", "Content-Length": str(size)}
        if request.method == "HEAD":
            opened.close()
            return Response(headers=headers, media_type=DIRECT_MIME.get(ext, "application/octet-stream"))

        def chunks():
            with opened:
                while data := opened.read(1024 * 1024):
                    yield data

        return StreamingResponse(chunks(), media_type=DIRECT_MIME.get(ext, "application/octet-stream"),
                                 headers=headers)

    r.state = {"store": store}  # for tests
    return r
