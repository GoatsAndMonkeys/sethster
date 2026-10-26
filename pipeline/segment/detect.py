"""Silence and applause detection from cached frame features (candidate cut points).

Tuned on Bayreuth 2023 Walküre (applause after Act 1: flatness jumps from ~0.02-0.04 to ~0.15 and
chroma entropy sits at ~2.47, i.e. no pitch class dominates), Met broadcasts and Carnegie concerts.

* applause: broadband noise -> high spectral flatness AND near-uniform chroma, sustained >= 2.5 s.
* silence: frame level far below the file's music level (p95) or below an absolute floor,
  sustained >= 0.8 s. Broadcast noise floors differ, so the threshold is relative.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d

from .features import FPS

LN12 = float(np.log(12))


@dataclass
class Event:
    kind: str          # applause | silence
    start: float       # seconds within the file
    end: float
    strength: float    # 0..1, how clearly this is applause/silence

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2

    def to_json(self) -> dict:
        d = asdict(self)
        d["start"], d["end"], d["strength"] = round(self.start, 2), round(self.end, 2), round(self.strength, 3)
        return d


def frame_series(f: dict) -> dict[str, np.ndarray]:
    rms = f["rms"].astype(np.float64)
    db = 20 * np.log10(rms + 1e-7)
    c = f["chroma"].astype(np.float64) + 1e-9
    p = c / c.sum(0, keepdims=True)
    ent = -(p * np.log(p)).sum(0) / LN12          # 1.0 = flat chroma
    flat = f["flat"].astype(np.float64)
    return {"db": db, "ent": ent, "flat": flat}


def _runs(mask: np.ndarray, min_len: int, merge_gap: int = 0) -> list[tuple[int, int]]:
    """[start, end) runs of True, merging gaps <= merge_gap, keeping runs >= min_len."""
    idx = np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(np.int8), [0]])))
    runs = list(zip(idx[::2], idx[1::2]))
    merged: list[list[int]] = []
    for a, b in runs:
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_len]


def music_level(db: np.ndarray) -> float:
    return float(np.percentile(db, 95)) if len(db) else -20.0


def detect(f: dict, *, applause_flat: float = 0.075, applause_ent: float = 0.955,
           silence_rel_db: float = 32.0, silence_abs_db: float = -58.0) -> list[Event]:
    s = frame_series(f)
    n = len(s["db"])
    if n < 5:
        return []
    w = max(3, int(round(1.5 * FPS)) | 1)
    flat_s = median_filter(s["flat"], size=w, mode="nearest")
    ent_s = median_filter(s["ent"], size=w, mode="nearest")
    db_s = uniform_filter1d(s["db"], size=max(3, int(0.5 * FPS)), mode="nearest")
    level = music_level(s["db"])
    events: list[Event] = []

    # applause
    app = (flat_s > applause_flat) & (ent_s > applause_ent) & (db_s > level - 40)
    for a, b in _runs(app, int(2.5 * FPS), merge_gap=int(1.5 * FPS)):
        fl = float(np.median(flat_s[a:b]))
        en = float(np.median(ent_s[a:b]))
        dur = (b - a) / FPS
        strength = min(1.0, 0.4 * min(1, (fl - applause_flat) / 0.07 + 0.3)
                       + 0.3 * min(1, (en - applause_ent) / 0.03 + 0.3) + 0.3 * min(1, dur / 15))
        events.append(Event("applause", a / FPS, b / FPS, max(0.05, strength)))

    # silence
    thr = max(level - silence_rel_db, silence_abs_db)
    quiet = db_s < thr
    for a, b in _runs(quiet, int(0.8 * FPS), merge_gap=int(0.3 * FPS)):
        depth = float(thr - np.median(db_s[a:b]))
        dur = (b - a) / FPS
        strength = min(1.0, 0.5 * min(1, dur / 4) + 0.5 * min(1, max(depth, 0) / 12 + 0.2))
        events.append(Event("silence", a / FPS, b / FPS, strength))
    events.sort(key=lambda e: e.start)
    return events


def nearest(events: list[Event], t: float, window: float, kinds=("applause", "silence")) -> tuple[Event | None, float]:
    """Best candidate within +-window of t (strength-weighted distance). Returns (event, snapped time).

    The snapped time is where the next section would start: the end of applause, or the end of a
    silence (the music's first attack), unless t already falls inside the event.
    """
    best, best_score, best_t = None, -1.0, t
    for e in events:
        if e.kind not in kinds:
            continue
        if e.end < t - window or e.start > t + window:
            continue
        cut = e.end if e.kind == "applause" else max(e.start, e.end - 0.3)
        if e.start <= t <= e.end:
            dist = 0.0
        else:
            dist = min(abs(t - e.start), abs(t - e.end))
        score = e.strength * (1 - dist / (window + 1e-9))
        if score > best_score:
            best, best_score, best_t = e, score, cut
    return best, best_t
