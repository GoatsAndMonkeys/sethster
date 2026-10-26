"""Reliable performance dates for "On This Day".

For each performance we gather date evidence from, in order:

1. the NAS folder name (Dad's label) and the research id derived from it: ``190329 Uchida ...``
   (YYMMDD), ``Ariadne Vienna 1-28-25`` (M-D-YY), ``0505 Emersons`` (YYMM, month precision),
   ``76 Walk Bayreuth`` (YY, year precision), ``Semiramide Chicago 1971`` (year);
2. the research record (``performance.date`` + ``date_status``, ``broadcast_date``);
3. the announcer transcripts ("It's April 9th, 2016, and we are live ...").

and pick one date with its precision (day | month | year | unknown). Only ``reliable`` dates (day
precision, not in conflict) are used for On This Day matching. A folder date is often the *air*
date of a delayed broadcast (WRTI's Sunday Philadelphia Orchestra show); when the research record
says so (``broadcast_date`` equals the folder date and the performance date differs) the folder
date is kept as ``aired`` and the research date is the performance date.

Pure Python, no I/O except reading the transcripts directory; results are cached per DB file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

ISO = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
SPOKEN = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+((?:19|20)\d\d))?\b", re.I)
PIVOT = 27  # two-digit years > 26 are 19xx (the collection spans 1940-2026)


def _yy(y: int) -> int:
    return 1900 + y if y > PIVOT - 1 else 2000 + y


def _valid(y: int, m: int, d: int | None = None) -> bool:
    try:
        dt.date(y, m, 1 if d is None else d)
        return 1930 <= y <= 2035
    except ValueError:
        return False


def precision(s: str | None) -> str:
    m = ISO.match(s or "")
    if not m:
        return "unknown"
    try:
        dt.date(int(m[1]), int(m[2] or 1), int(m[3] or 1))
    except ValueError:
        return "unknown"
    return "day" if m.group(3) else ("month" if m.group(2) else "year")


def parse_label(label: str | None) -> tuple[str, str] | None:
    """One folder component or id -> (partial ISO date, precision), or None."""
    if not label:
        return None
    s = label.strip()
    # YYMMDD at the start of a component or after "<prefix>-" in an id
    m = re.match(r"^(?:[a-z]+-)?(\d{2})(\d{2})(\d{2})(?:\D|$)", s)
    if m:
        y, mo, d = _yy(int(m[1])), int(m[2]), int(m[3])
        if _valid(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}", "day"
    # M-D-YY or M-D-YYYY anywhere ("Die Walkure - Bayreuth - 7-28-23")
    m = re.search(r"(?<!\d)(\d{1,2})[-./](\d{1,2})[-./](\d{4}|\d{2})(?!\d)", s)
    if m:
        y = int(m[3]) if len(m[3]) == 4 else _yy(int(m[3]))
        mo, d = int(m[1]), int(m[2])
        if _valid(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}", "day"
    # YYMM at the start ("0505 Emersons", "2502 Phila") -- a 19xx/20xx year is caught below
    m = re.match(r"^(?:[a-z]+-)?(\d{2})(\d{2})(?:\D|$)", s)
    if m and 1 <= int(m[2]) <= 12 and not re.match(r"^(?:[a-z]+-)?(19|20)\d\d(?:\D|$)", s):
        y = _yy(int(m[1]))
        if _valid(y, int(m[2])):
            return f"{y:04d}-{int(m[2]):02d}", "month"
    # a four-digit year anywhere ("Semiramide Chicago 1971", "Abbado, 1988")
    m = re.search(r"(?<!\d)(19[3-9]\d|20[0-3]\d)(?!\d)", s)
    if m:
        return m[1], "year"
    # YY at the start ("76 Walk Bayreuth")
    m = re.match(r"^(?:[a-z]+-)?(\d{2})(?:[\s_-]|$)", s)
    if m:
        return f"{_yy(int(m[1])):04d}", "year"
    return None


RANK = {"day": 3, "month": 2, "year": 1, "unknown": 0}


def folder_date(folders: list[str], pid: str) -> dict | None:
    """Best date from the folder names (deepest dated component of each folder), else from the id."""
    best = None
    for f in folders or []:
        for comp in reversed([c for c in f.split("/") if c]):
            hit = parse_label(comp)
            if hit:
                if best is None or RANK[hit[1]] > RANK[best[1]]:
                    best = (hit[0], hit[1], f"folder: {f}")
                break
    hit = parse_label(pid)
    if hit and (best is None or RANK[hit[1]] > RANK[best[1]]):
        best = (hit[0], hit[1], f"id: {pid}")
    if not best:
        return None
    return {"date": best[0], "precision": best[1], "label": best[2]}


def transcript_dates(doc: dict | None, years: set[int]) -> list[dict]:
    """Spoken dates in announcer speech. A date without a year is kept only when exactly one of the
    candidate years makes it valid (the year is then marked as assumed)."""
    out, seen = [], set()
    if not isinstance(doc, dict):
        return out
    for s in doc.get("segments") or []:
        if not isinstance(s, dict):
            continue
        text = s.get("text") or ""
        if (s.get("avg_logprob") or 0) < -0.9:
            continue
        for m in SPOKEN.finditer(text):
            mo, d = MONTHS[m[1].lower()], int(m[2])
            ys = [int(m[3])] if m[3] else sorted(years)
            ys = [y for y in ys if _valid(y, mo, d)]
            if len(ys) != 1:
                continue
            iso = f"{ys[0]:04d}-{mo:02d}-{d:02d}"
            if iso in seen:
                continue
            seen.add(iso)
            out.append({"date": iso, "year_spoken": bool(m[3]), "text": text.strip()[:240],
                        "file": s.get("file"), "start": s.get("start")})
    return out


def decide(pid: str, research_date: str | None, research_status: str | None, broadcast_date: str | None,
           folders: list[str], tdates: list[dict] | None = None) -> dict:
    R = research_date if precision(research_date) != "unknown" else None
    Rp = precision(R)
    st = (research_status or "unknown").lower()
    F = folder_date(folders, pid)
    Fd, Fp = (F["date"], F["precision"]) if F else (None, "unknown")
    B = broadcast_date if precision(broadcast_date) == "day" else None
    T = [t["date"] for t in (tdates or [])]
    out = {"performance_id": pid, "research_date": R, "research_status": st, "folder_date": Fd,
           "folder_precision": Fp, "folder_label": F["label"] if F else None, "broadcast_date": B,
           "transcript_dates": tdates or [], "aired": None}

    def pick(date, prec, basis, status, note, reliable=None):
        out.update(date=date, precision=prec, basis=basis, status=status, note=note)
        out["reliable"] = (prec == "day" and status != "conflict") if reliable is None else reliable
        return out

    if Fp == "day" and B == Fd and R and R != Fd:
        # the folder is the air date of a delayed broadcast; the research date is the concert
        out["aired"] = Fd
        if Rp == "day":
            return pick(R, "day", "research", st, f"folder {Fd} is the broadcast date; performed {R}")
        return pick(R, Rp, "research", st, f"folder {Fd} is the broadcast date; performance only known to "
                                           f"{Rp} precision ({R})", reliable=False)
    if Fp == "day" and B == Fd and not R:
        # the research names the folder date as the air date and found no concert date
        out["aired"] = Fd
        return pick(None, "unknown", None, "unknown", f"folder {Fd} is the broadcast date; performance date "
                                                      f"unknown", reliable=False)
    if Rp == "day" and st == "confirmed":
        agree = Fd == R if Fp == "day" else None
        note = "research date confirmed by date-specific sources"
        if agree is False:
            note += f"; folder label says {Fd}"
        return pick(R, "day", "research", "confirmed", note)
    if Rp == "day" and Fp == "day" and R == Fd:
        return pick(R, "day", "folder+research", "corroborated", "folder label and research agree")
    if Rp == "day" and Fp == "day":  # inferred/conflict research date that disagrees with the folder
        if R in T and Fd not in T:
            return pick(R, "day", "research+transcript", "corroborated", f"announcer gives {R}; folder says {Fd}")
        if Fd in T and R not in T:
            return pick(Fd, "day", "folder+transcript", "corroborated", f"announcer gives {Fd}; research said {R}")
        return pick(R, "day", "research", "conflict", f"research {R} ({st}) vs folder {Fd}")
    if Fp == "day":
        if R and not Fd.startswith(R):
            return pick(Fd, "day", "folder", "conflict", f"folder {Fd} vs research {R} ({st})")
        status = "corroborated" if Fd in T else "folder"
        return pick(Fd, "day", "folder" + ("+transcript" if Fd in T else ""), status,
                    "date from Dad's folder label" + (" and the announcer" if Fd in T else ""))
    if Rp == "day":
        return pick(R, "day", "research", "conflict" if st == "conflict" else st, f"research date ({st})")
    fitting = [t for t in T if (not R or t.startswith(R)) and (not Fd or t.startswith(Fd))]
    if len(set(fitting)) == 1:
        return pick(fitting[0], "day", "transcript", "inferred", "date spoken by the announcer")
    best = max([(R, Rp, "research"), (Fd, Fp, "folder")], key=lambda x: RANK[x[1]])
    if best[0]:
        return pick(best[0], best[1], best[2], st if best[2] == "research" else "folder",
                    f"only {best[1]} precision", reliable=False)
    return pick(None, "unknown", None, "unknown", "no usable date", reliable=False)


def _transcript(tdir: Path | None, pid: str) -> dict | None:
    if not tdir or "/" in pid:
        return None
    p = tdir / f"{pid}.json"
    try:
        if os.lstat(p).st_flags & 0x40000000:  # macOS iCloud-evicted placeholder: reading may block forever
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def compute_all(conn: sqlite3.Connection, transcripts_dir: Path | None) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT id, date, date_status, broadcast_date, folder, folders FROM performances"):
        try:
            folders = json.loads(r["folders"] or "[]") or []
        except ValueError:
            folders = []
        if r["folder"] and r["folder"] not in folders:
            folders.append(r["folder"])
        years = set()
        for d in (r["date"], r["broadcast_date"]):
            if precision(d) != "unknown":
                years.add(int(d[:4]))
        fd = folder_date(folders, r["id"])
        if fd:
            years.add(int(fd["date"][:4]))
        tdates = transcript_dates(_transcript(transcripts_dir, r["id"]), years)
        out[r["id"]] = decide(r["id"], r["date"], r["date_status"], r["broadcast_date"], folders, tdates)
    return out


class DateIndex:
    """compute_all() cached per (db path, mtime)."""

    def __init__(self, transcripts_dir: Path | None):
        self.tdir = transcripts_dir
        self._key = None
        self._val: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, conn: sqlite3.Connection, db_path: Path) -> dict[str, dict]:
        try:
            key = (str(db_path), db_path.stat().st_mtime)
        except OSError:
            key = None
        with self._lock:
            if key and key == self._key:
                return self._val
        val = compute_all(conn, self.tdir)
        with self._lock:
            self._key, self._val = key, val
        return val


def main() -> None:
    """python -m sethster_server.perfdates [out.json] -- snapshot for review (data/context/dates.json)."""
    import sys

    from . import db as dbmod
    from .config import load_settings

    s = load_settings()
    conn = dbmod.connect(s.db_path, readonly=True)
    rows = compute_all(conn, s.transcripts_dir)
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else s.data_dir / "context" / "dates.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    stats: dict[str, int] = {}
    for r in rows.values():
        stats[f"precision:{r['precision']}"] = stats.get(f"precision:{r['precision']}", 0) + 1
        stats[f"status:{r['status']}"] = stats.get(f"status:{r['status']}", 0) + 1
        stats["reliable"] = stats.get("reliable", 0) + int(r["reliable"])
    out.write_text(json.dumps({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "stats": stats, "items": dict(sorted(rows.items()))}, indent=1, ensure_ascii=False))
    print(json.dumps(stats, indent=1), "->", out)


if __name__ == "__main__":
    main()
