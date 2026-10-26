"""Live internet radio: the curated station list (data/radio/live_stations.json) and "now playing" lookups
(ICY in-stream metadata or a station JSON endpoint). Stdlib only; results cached briefly per station."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("sethster.radio.live")

UA = "Sethster/0.1 (+private classical player)"
NOW_TTL = 20.0


class LiveStations:
    def __init__(self, path: Path | None):
        self.path = path
        self._mtime = None
        self._doc: dict = {"stations": []}
        self._now: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _load(self) -> dict:
        p = self.path
        if not p or not p.is_file():
            return {"stations": []}
        m = p.stat().st_mtime
        if m != self._mtime:
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                self._doc = doc if isinstance(doc, dict) else {"stations": []}
                self._mtime = m
            except (OSError, ValueError) as e:
                log.warning("live stations %s unreadable (%s)", p, e)
        return self._doc

    def list(self) -> list[dict]:
        return [self.shape(s) for s in self._load().get("stations") or [] if s.get("stream_url")]

    def get(self, sid: str) -> dict | None:
        for s in self._load().get("stations") or []:
            if s.get("id") == sid:
                return s
        return None

    @staticmethod
    def shape(s: dict) -> dict:
        md = s.get("metadata") or {}
        return {"id": s.get("id"), "name": s.get("name"), "broadcaster": s.get("broadcaster"),
                "country": s.get("country"), "city": s.get("city"), "homepage": s.get("homepage"),
                "stream_url": s.get("stream_url"), "format": s.get("format"), "bitrate_kbps": s.get("bitrate_kbps"),
                "logo_url": s.get("logo_url"), "description": s.get("description"),
                "airs_opera": bool(s.get("airs_opera")), "tags": s.get("tags") or [],
                "has_now_playing": (md.get("type") or "none") != "none"}

    def now_playing(self, sid: str, fetch=None) -> dict | None:
        s = self.get(sid)
        if not s:
            return None
        with self._lock:
            hit = self._now.get(sid)
            if hit and time.time() - hit[0] < NOW_TTL:
                return hit[1]
        md = s.get("metadata") or {}
        typ = md.get("type") or "none"
        out = {"station_id": sid, "source": typ, "title": None, "artist": None, "composer": None,
               "programme": None, "raw": None}
        try:
            if typ == "icy":
                f = fetch or icy_title
                try:
                    raw = f(s["stream_url"])
                except OSError:   # load-balanced streams sometimes redirect to a dead node: retry once
                    raw = f(s["stream_url"])
                out["raw"] = raw
                out.update(split_icy(raw))
            elif typ == "json" and md.get("url"):
                doc = (fetch or fetch_json)(md["url"])
                for k, path in (md.get("fields") or {}).items():
                    if k in out and path:
                        v = dig(doc, path)
                        out[k] = v if isinstance(v, (str, int, float)) or v is None else json.dumps(v)[:200]
        except Exception as e:  # noqa: BLE001
            log.info("now playing for %s failed: %s", sid, e)
            out["error"] = str(e)[:200]
        out["fetched_at"] = int(time.time())
        with self._lock:
            self._now[sid] = (time.time(), out)
        return out


def dig(doc, path: str):
    cur = doc
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def fetch_json(url: str, timeout: float = 6.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2_000_000).decode("utf-8", "replace"))


def parse_icy_block(block: bytes) -> str | None:
    text = block.rstrip(b"\0").decode("utf-8", "replace")
    m = re.search(r"StreamTitle='(.*?)';", text, re.S)
    return m.group(1).strip() if m and m.group(1).strip() else None


def icy_title(url: str, timeout: float = 8.0) -> str | None:
    """Reads one ICY metadata block from a Shoutcast/Icecast stream (at most ~metaint + 4 KB)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Icy-MetaData": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        metaint = int(r.headers.get("icy-metaint") or 0)
        if not metaint or metaint > 512 * 1024:
            return None
        for _ in range(2):   # the first block is often empty; try the next one
            r.read(metaint)
            n = r.read(1)
            if not n:
                return None
            size = n[0] * 16
            if size:
                t = parse_icy_block(r.read(size))
                if t:
                    return t
    return None


def split_icy(raw: str | None) -> dict:
    """'Composer - Title' / 'Artist - Title' heuristics; leaves the raw string as title when unsure."""
    if not raw:
        return {}
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", raw, maxsplit=1)]
    if len(parts) == 2 and all(parts):
        return {"artist": parts[0], "title": parts[1]}
    return {"title": raw}
