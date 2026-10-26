"""Reference recordings with track boundaries: Crutchfield CD rips / studio sets on the NAS.

`data/crutchfield/inventory.jsonl` lists 5,000+ commercial and private recordings, each with discs
and per-track durations. A CD set's track boundaries mark musical numbers (arias, scenes,
movements) and its file names usually carry the number's title or incipit. For one live work we
pick the best-matching complete set and align to it (align.py).
"""
from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .features import DATA, ROOT

sys.path.insert(0, str(ROOT / "server"))
from sethster_server.text import fold, norm_key, surname  # noqa: E402

INVENTORY = DATA / "crutchfield" / "inventory.jsonl"
CF_RESULTS = DATA / "crutchfield" / "results"

_EXCERPT = re.compile(r"\b(exc|excs|excerpts?|highlights?|selections?|scenes? from|fragments?|arias|recital|"
                      r"duets|extras?|dup\??|defective|incomplete|rehearsal|act \d+ only|acts? [i1v]+ only)\b", re.I)
_STOP = {"the", "der", "die", "das", "des", "le", "la", "les", "il", "lo", "l", "i", "of", "and", "et", "und",
         "in", "major", "minor", "no", "op", "a", "di", "de", "d", "du", "auf", "von", "zu", "flat", "sharp",
         "complete", "ballet", "opera", "version"}
_UNTITLED = re.compile(r"^(?:track|pista|piste|audiotrack|audio track|titel|title|plage|cd|disc|disk|side|part)?"
                       r"[\s\d._-]*(?:track|pista|piste|audiotrack)?[\s\d._-]*$", re.I)


def title_tokens(title: str) -> list[str]:
    t = re.sub(r"\(.*?\)|['\"].*?['\"]", " ", title)
    return [w for w in norm_key(t).split() if w not in _STOP and (len(w) > 2 or w.isdigit())]


def clean_track_title(fname: str, work_title: str = "", composer: str = "") -> str | None:
    """'1-04 A te l'estremo addio - il lacerato spirito.mp3' -> "A te l'estremo addio - il lacerato spirito"."""
    s = Path(fname).stem
    s = s.replace("_", " ").strip()
    s = re.sub(r"^\(?\d{1,2}\)?\s*[-.]?\s*\d{0,3}\s*[-.)]?\s*", "", s)       # "1-04 ", "04. ", "(01) "
    s = re.sub(r"^\d{1,3}\s*-\s*", "", s)                                      # "104 - "
    for name in (composer, surname(composer) if composer else "", work_title):
        if name:
            s = re.sub(rf"^\s*{re.escape(name)}\s*[:\-–]?\s*", "", s, flags=re.I)
    s = re.sub(r"^(?:act|akt|atto|acte)\b", lambda m: m.group(0).title(), s.strip(" -–_."))
    s = re.sub(r"\s+", " ", s).strip(" -–_.")
    if not s or _UNTITLED.match(s) or len(re.findall(r"[A-Za-zÀ-ÿ]{2,}", s)) < 1:
        return None
    if re.fullmatch(r"(?i)(track|pista|piste|audiotrack)\s*\d+", s):
        return None
    return s


def good_title(t: str | None) -> bool:
    if not t or not re.match(r"[A-Za-zÀ-ÿ'\"«„“(]", t):
        return False
    if re.search(r"[A-Za-z]\d|\d[A-Za-z]", t):
        return False
    words = re.findall(r"[A-Za-zÀ-ÿ]{3,}", t)
    return len(words) >= 2 or any(len(w) >= 6 for w in words)


def tidy_titles(ref: "Reference") -> None:
    """Strip a prefix shared by every title ("simon boc 01 che dicesti" -> "che dicesti")."""
    titles = [t.title for t in ref.tracks if t.title]
    if len(titles) < 3 or len(titles) < len(ref.tracks) * 0.8:
        return
    pre = titles[0]
    for t in titles[1:]:
        while pre and not t.lower().startswith(pre.lower()):
            pre = pre[:-1]
    pre = pre[: pre.rfind(" ") + 1] if " " in pre else ""
    if len(pre) >= 4 and not re.fullmatch(r"(?i)(act|akt|atto|acte)\s*[\divx]*\s*[-:]?\s*", pre.strip() + " "):
        for t in ref.tracks:
            if t.title:
                rest = re.sub(r"^[\d\s._-]+", "", t.title[len(pre):]).strip()
                t.title = rest or None


_FILLER = re.compile(r"^(?:from\b|bonus|appendix|encore|filler|interview|rehearsal)|\b(?:bonus track|appendix)\b",
                     re.I)


def drop_fillers(ref: "Reference") -> None:
    """Drop trailing bonus tracks ("From Falstaff - ...") that belong to another work."""
    while ref.tracks and ref.tracks[-1].title and _FILLER.search(ref.tracks[-1].title):
        ref.tracks.pop()


@dataclass
class RefTrack:
    path: str            # NAS path relative to the sandbox share
    size: int | None
    duration: float
    title: str | None
    disc: str
    number: int          # 1-based across the whole set


@dataclass
class Reference:
    id: str
    path: str
    name: str
    tracks: list[RefTrack] = field(default_factory=list)
    score: float = 0.0
    release: dict | None = None

    @property
    def duration(self) -> float:
        return sum(t.duration for t in self.tracks)

    def to_json(self) -> dict:
        return {"recording_id": self.id, "path": self.path, "name": self.name, "n_tracks": len(self.tracks),
                "duration_s": round(self.duration, 1), "release": self.release, "score": round(self.score, 3)}


@lru_cache(maxsize=1)
def inventory() -> list[dict]:
    if not INVENTORY.exists():
        return []
    return [json.loads(line) for line in INVENTORY.open(encoding="utf-8") if line.strip()]


def _release(rid: str) -> dict | None:
    p = CF_RESULTS / f"{rid}.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    if d.get("status") != "matched" or not d.get("release"):
        return None
    r = d["release"]
    return {k: r.get(k) for k in ("mbid", "title", "label", "catalogue", "date", "url") if r.get(k)} | \
        {"confidence": d.get("confidence")}


def build_reference(row: dict, work_title: str = "", composer: str = "") -> Reference:
    ref = Reference(id=row["id"], path=row["path"], name=row["name"], release=_release(row["id"]))
    n = 0
    for disc in row.get("discs", []):
        for t in disc.get("tracks", []):
            n += 1
            ref.tracks.append(RefTrack(path=f"{disc['dir']}/{t['file']}", size=t.get("size"),
                                       duration=float(t.get("dur") or 0), disc=str(disc.get("label") or ""),
                                       title=clean_track_title(t["file"], work_title, composer), number=n))
    return ref


def candidates(composer: str, work_title: str, music_s: float | None, *, exclude_paths: set[str] = frozenset(),
               limit: int = 5) -> list[Reference]:
    """Complete reference sets for one work, best first."""
    comp = norm_key(surname(composer or "", particles=False))
    toks = title_tokens(work_title)
    if not toks or not comp:
        return []
    out = []
    for row in inventory():
        hay = norm_key(f"{row.get('path', '')} {row.get('work_dir') or ''}")
        if comp not in hay.split() and comp not in norm_key(row.get("composer") or ""):
            continue
        name_hay = set(norm_key(f"{row.get('name', '')} {row.get('work_dir') or ''} {row['path']}").split())
        if not all(t in name_hay for t in toks):
            continue
        if _EXCERPT.search(fold(row["path"])):
            continue
        n_tracks = row.get("n_tracks") or 0
        if n_tracks < 4 or any(t in row["path"] for t in exclude_paths):
            continue
        dur = row.get("total_dur") or 0
        if music_s and music_s > 600:
            ratio = dur / music_s
            if not 0.72 <= ratio <= 1.3:
                continue
            fit = 1 - min(1.0, abs(math.log(ratio)) / math.log(1.3))
        else:
            fit = 0.5
        ref = build_reference(row, work_title, composer)
        drop_fillers(ref)
        if not ref.tracks or any(t.duration <= 0 for t in ref.tracks):
            continue
        tidy_titles(ref)
        titled = sum(1 for t in ref.tracks if good_title(t.title)) / len(ref.tracks)
        mean_len = dur / n_tracks
        granular = min(1.0, math.log(n_tracks) / math.log(30)) if 30 <= mean_len <= 1200 else 0.2
        lossless_penalty = 0.05 if any(t.path.lower().endswith((".flac", ".wav", ".ape")) for t in ref.tracks) else 0
        ref.score = 0.35 * fit + 0.35 * titled + 0.25 * granular + (0.1 if ref.release else 0) - lossless_penalty
        out.append(ref)
    out.sort(key=lambda r: (-r.score, r.id))
    return out[:limit]
