"""SQLite schema and connection helper (stdlib sqlite3)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS venues (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT, wikidata TEXT, description TEXT, wikipedia_url TEXT
);
CREATE TABLE IF NOT EXISTS composers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_name TEXT, wikidata TEXT, years TEXT, bio TEXT,
  description TEXT, image_src TEXT, wikipedia_url TEXT, search_text TEXT, intro TEXT, image_credit TEXT
);
CREATE TABLE IF NOT EXISTS works (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, composer_id TEXT REFERENCES composers(id), wikidata TEXT,
  catalogue TEXT, description TEXT, wikipedia_url TEXT, search_text TEXT, synopsis TEXT,
  synopsis_heading TEXT, premiere_date TEXT, premiere_place TEXT, librettists TEXT
);
CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, sort_name TEXT, kind TEXT NOT NULL, wikidata TEXT, years TEXT,
  bio TEXT, description TEXT, image_src TEXT, wikipedia_url TEXT, search_text TEXT, intro TEXT, image_credit TEXT
);
CREATE TABLE IF NOT EXISTS performances (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, subtitle TEXT, date TEXT, date_status TEXT,
  venue_id TEXT REFERENCES venues(id), kind TEXT, collection TEXT, duration_s REAL,
  artwork_path TEXT, confirmed_ratio REAL, summary TEXT, date_evidence TEXT, candidate_dates TEXT,
  broadcast_date TEXT, broadcaster TEXT, event_name TEXT, release TEXT, folder TEXT, folders TEXT,
  sources TEXT, resources TEXT, open_questions TEXT, source_file TEXT, search_text TEXT,
  artwork_meta TEXT
);
CREATE TABLE IF NOT EXISTS performance_composers (
  performance_id TEXT, composer_id TEXT, ord INTEGER, PRIMARY KEY (performance_id, composer_id)
);
CREATE TABLE IF NOT EXISTS program (
  performance_id TEXT, ord INTEGER, work_id TEXT, title TEXT, composer_id TEXT, catalogue TEXT, notes TEXT,
  PRIMARY KEY (performance_id, ord)
);
CREATE TABLE IF NOT EXISTS credits (
  performance_id TEXT, ord INTEGER, person_id TEXT, role TEXT, work_id TEXT, status TEXT, notes TEXT,
  source_ids TEXT, PRIMARY KEY (performance_id, ord)
);
CREATE TABLE IF NOT EXISTS tracks (
  id TEXT PRIMARY KEY, performance_id TEXT, idx INTEGER, title TEXT, content TEXT, work_id TEXT,
  segment TEXT, duration_s REAL, format TEXT, path TEXT, stream_path TEXT, size INTEGER
);
CREATE TABLE IF NOT EXISTS questions (
  performance_id TEXT, ord INTEGER, question TEXT, PRIMARY KEY (performance_id, question)
);
-- answers survive re-imports (the importer copies them across)
CREATE TABLE IF NOT EXISTS answers (
  performance_id TEXT, question TEXT, answer TEXT, answered_at TEXT, PRIMARY KEY (performance_id, question)
);
CREATE INDEX IF NOT EXISTS credits_person ON credits(person_id);
CREATE INDEX IF NOT EXISTS program_work ON program(work_id);
CREATE INDEX IF NOT EXISTS tracks_perf ON tracks(performance_id, idx);
CREATE INDEX IF NOT EXISTS perf_date ON performances(date);
"""


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
