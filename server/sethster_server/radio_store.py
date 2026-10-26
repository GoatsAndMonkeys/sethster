"""Radio state that must survive re-imports: stations, play/skip history, probed durations.

Lives in its own SQLite file (`SETHSTER_RADIO_DB`, default `$SETHSTER_DATA/radio.db`) because the
importer rebuilds sethster.db from scratch.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, seeds TEXT NOT NULL, settings TEXT, created_at TEXT, last_played_at TEXT
);
CREATE TABLE IF NOT EXISTS plays (
  id INTEGER PRIMARY KEY AUTOINCREMENT, station_id TEXT, excerpt_id TEXT NOT NULL, track_id TEXT,
  performance_id TEXT, work_key TEXT, number_key TEXT, composer_id TEXT, performer_ids TEXT,
  started_at TEXT NOT NULL, ended_at TEXT, listened_s REAL, outcome TEXT, client TEXT
);
CREATE INDEX IF NOT EXISTS plays_station ON plays(station_id, id);
CREATE INDEX IF NOT EXISTS plays_excerpt ON plays(excerpt_id);
CREATE TABLE IF NOT EXISTS durations (
  track_id TEXT PRIMARY KEY, size INTEGER, duration_s REAL, method TEXT
);
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class RadioStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._ready = False

    def connect(self) -> sqlite3.Connection:
        if not self._ready:
            with self._lock:
                if not self._ready:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    c = sqlite3.connect(self.path)
                    c.executescript(SCHEMA)
                    c.commit()
                    c.close()
                    self._ready = True
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _tx(self):
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---- stations -----------------------------------------------------------------------------------------
    def upsert_station(self, sid: str, name: str, seeds: list[dict], settings: dict) -> dict:
        with self._tx() as c:
            row = c.execute("SELECT * FROM stations WHERE id=?", (sid,)).fetchone()
            if row:
                merged = {**json.loads(row["settings"] or "{}"), **settings}
                c.execute("UPDATE stations SET settings=? WHERE id=?", (json.dumps(merged), sid))
            else:
                c.execute("INSERT INTO stations VALUES (?,?,?,?,?,NULL)",
                          (sid, name, json.dumps(seeds), json.dumps(settings), now_iso()))
        return self.station(sid)

    def station(self, sid: str) -> dict | None:
        with self._tx() as c:
            r = c.execute("""SELECT s.*, (SELECT COUNT(*) FROM plays p WHERE p.station_id=s.id) AS play_count
                             FROM stations s WHERE s.id=?""", (sid,)).fetchone()
        return self._station_shape(r) if r else None

    def stations(self) -> list[dict]:
        with self._tx() as c:
            rows = c.execute("""SELECT s.*, (SELECT COUNT(*) FROM plays p WHERE p.station_id=s.id) AS play_count
                                FROM stations s ORDER BY s.last_played_at IS NULL, s.last_played_at DESC,
                                s.created_at DESC""").fetchall()
        return [self._station_shape(r) for r in rows]

    def delete_station(self, sid: str) -> bool:
        with self._tx() as c:
            n = c.execute("DELETE FROM stations WHERE id=?", (sid,)).rowcount
            c.execute("DELETE FROM plays WHERE station_id=?", (sid,))
        return n > 0

    @staticmethod
    def _station_shape(r) -> dict:
        return {"id": r["id"], "name": r["name"], "seeds": json.loads(r["seeds"]),
                "settings": json.loads(r["settings"] or "{}"), "created_at": r["created_at"],
                "last_played_at": r["last_played_at"], "play_count": r["play_count"]}

    # ---- plays --------------------------------------------------------------------------------------------
    def start_play(self, station_id: str | None, ex: dict, client: str | None = None,
                   started_at: str | None = None) -> int:
        ts = started_at or now_iso()
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO plays (station_id, excerpt_id, track_id, performance_id, work_key, number_key,
                   composer_id, performer_ids, started_at, outcome, client) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (station_id, ex["id"], ex.get("track_id"), ex.get("performance_id"), ex.get("work_key"),
                 ex.get("number_key"), ex.get("composer_id"), json.dumps(ex.get("performer_ids") or []), ts,
                 "playing", client))
            if station_id:
                c.execute("UPDATE stations SET last_played_at=? WHERE id=?", (ts, station_id))
            return int(cur.lastrowid)

    def finish_play(self, play_id: int, outcome: str, listened_s: float | None) -> bool:
        with self._tx() as c:
            n = c.execute("UPDATE plays SET outcome=?, listened_s=?, ended_at=? WHERE id=?",
                          (outcome, listened_s, now_iso(), play_id)).rowcount
        return n > 0

    def history(self, station_id: str, limit: int = 200) -> list[dict]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM plays WHERE station_id=? ORDER BY id DESC LIMIT ?",
                             (station_id, limit)).fetchall()
        return [self._play_shape(r) for r in rows]

    @staticmethod
    def _play_shape(r) -> dict:
        return {"id": r["id"], "station_id": r["station_id"], "excerpt_id": r["excerpt_id"], "track_id": r["track_id"],
                "performance_id": r["performance_id"], "work_key": r["work_key"], "number_key": r["number_key"],
                "composer_id": r["composer_id"], "performer_ids": json.loads(r["performer_ids"] or "[]"),
                "started_at": r["started_at"], "ended_at": r["ended_at"], "listened_s": r["listened_s"],
                "outcome": r["outcome"]}

    def global_stats(self) -> dict:
        """Aggregate play data across all stations: per excerpt (plays, finished, skipped) and per performance."""
        ex: dict[str, dict] = {}
        perf: dict[str, int] = {}
        with self._tx() as c:
            for r in c.execute("""SELECT excerpt_id, COUNT(*) n, SUM(outcome='played') done, SUM(outcome='skipped') sk
                                  FROM plays GROUP BY excerpt_id"""):
                ex[r["excerpt_id"]] = {"plays": r["n"], "finished": r["done"] or 0, "skipped": r["sk"] or 0}
            for r in c.execute("SELECT performance_id, COUNT(*) n FROM plays GROUP BY performance_id"):
                if r["performance_id"]:
                    perf[r["performance_id"]] = r["n"]
        return {"excerpts": ex, "performances": perf}

    # ---- probed durations ---------------------------------------------------------------------------------
    def durations(self) -> dict[str, tuple[int, float]]:
        with self._tx() as c:
            return {r["track_id"]: (r["size"], r["duration_s"]) for r in c.execute("SELECT * FROM durations")}

    def put_durations(self, rows: list[tuple[str, int, float | None, str]]) -> None:
        if not rows:
            return
        with self._tx() as c:
            c.executemany("INSERT OR REPLACE INTO durations VALUES (?,?,?,?)", rows)
