"""Personal rotation state that must survive catalog re-imports."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS rotation (
  listener TEXT NOT NULL, performance_id TEXT NOT NULL, position INTEGER NOT NULL, added_at TEXT NOT NULL,
  PRIMARY KEY (listener, performance_id), UNIQUE (listener, position)
);
CREATE INDEX IF NOT EXISTS rotation_listener ON rotation(listener, position);
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class RotationStore:
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
    def _tx(self, *, write: bool = False):
        conn = self.connect()
        try:
            if write:
                # sqlite3's default deferred transaction does not begin for the
                # SELECTs in add/reorder.  Take the write reservation before
                # reading positions so two processes cannot choose the same one.
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def items(self, listener: str) -> list[dict]:
        with self._tx() as c:
            rows = c.execute("SELECT performance_id, added_at FROM rotation WHERE listener=? ORDER BY position",
                             (listener,)).fetchall()
        return [{"performance_id": r["performance_id"], "added_at": r["added_at"]} for r in rows]

    def add(self, listener: str, performance_id: str, check_owner: Callable[[str], None] | None = None) -> dict:
        """check_owner(listener) runs inside the write transaction and raises if the listener's account is gone:
        account deletion deletes the user before clearing the Rotation, so an add either lands before the clear
        or sees the deletion."""
        with self._tx(write=True) as c:
            if check_owner:
                check_owner(listener)
            row = c.execute("SELECT added_at FROM rotation WHERE listener=? AND performance_id=?",
                            (listener, performance_id)).fetchone()
            if row:
                return {"performance_id": performance_id, "added_at": row["added_at"]}
            position = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM rotation WHERE listener=?",
                                 (listener,)).fetchone()[0]
            added_at = now_iso()
            c.execute("INSERT INTO rotation VALUES (?,?,?,?)", (listener, performance_id, position, added_at))
        return {"performance_id": performance_id, "added_at": added_at}

    def remove(self, listener: str, performance_id: str) -> bool:
        with self._tx(write=True) as c:
            n = c.execute("DELETE FROM rotation WHERE listener=? AND performance_id=?",
                          (listener, performance_id)).rowcount
        return n > 0

    def reorder(self, listener: str, performance_ids: list[str]) -> bool:
        """Reorder only if performance_ids still exactly matches the rotation.

        The comparison belongs in the same write transaction as the updates:
        otherwise an append between API validation and this method is assigned
        a negative position by the first UPDATE and is silently left there.
        """
        with self._tx(write=True) as c:
            current = [r[0] for r in c.execute(
                "SELECT performance_id FROM rotation WHERE listener=? ORDER BY position", (listener,))]
            if len(current) != len(performance_ids) or set(current) != set(performance_ids):
                return False
            c.execute("UPDATE rotation SET position=-position-1 WHERE listener=?", (listener,))
            c.executemany("UPDATE rotation SET position=? WHERE listener=? AND performance_id=?",
                          [(i, listener, pid) for i, pid in enumerate(performance_ids)])
        return True

    def clear(self, listener: str) -> int:
        """Drops a listener's whole rotation (account deletion)."""
        with self._tx(write=True) as c:
            return c.execute("DELETE FROM rotation WHERE listener=?", (listener,)).rowcount

    def listeners(self) -> set[str]:
        with self._tx() as c:
            return {r[0] for r in c.execute("SELECT DISTINCT listener FROM rotation")}

    def counts(self, *listeners: str) -> dict[str, int]:
        with self._tx() as c:
            return {lst: c.execute("SELECT COUNT(*) FROM rotation WHERE listener=?", (lst,)).fetchone()[0]
                    for lst in listeners}

    def move(self, src: str, dest: str) -> dict:
        """Moves src's rotation onto dest in one transaction (docs/plans/accounts.md, move-rotation): src items
        are appended after dest's in src order with their added_at, items dest already has are skipped, and the
        src rows are deleted. A second run finds src empty and moves nothing."""
        with self._tx(write=True) as c:
            have = {r[0] for r in c.execute("SELECT performance_id FROM rotation WHERE listener=?", (dest,))}
            pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM rotation WHERE listener=?",
                            (dest,)).fetchone()[0]
            rows = c.execute("SELECT performance_id, added_at FROM rotation WHERE listener=? ORDER BY position",
                             (src,)).fetchall()
            moved = skipped = 0
            for r in rows:
                if r["performance_id"] in have:
                    skipped += 1
                    continue
                c.execute("INSERT INTO rotation VALUES (?,?,?,?)", (dest, r["performance_id"], pos, r["added_at"]))
                have.add(r["performance_id"])
                pos += 1
                moved += 1
            c.execute("DELETE FROM rotation WHERE listener=?", (src,))
        return {"moved": moved, "skipped": skipped}

    def backup(self, dest: Path) -> None:
        """A consistent copy of the whole database (sqlite's online backup)."""
        src = self.connect()
        try:
            out = sqlite3.connect(dest)
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()


def rotation_db_path(settings) -> Path:
    v = os.environ.get("SETHSTER_ROTATION_DB")
    return Path(v).expanduser() if v else settings.data_dir / "rotation.db"
