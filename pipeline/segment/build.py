"""Build data/sections/<performance_id>.json for one performance (schema: docs/sections.md)."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import VERSION
from . import align as AL
from . import detect as D
from . import features as F
from . import refs as R
from .features import DATA, ROOT

sys.path.insert(0, str(ROOT / "server"))
from sethster_server import canonical as CANON  # noqa: E402
from sethster_server.text import slugify  # noqa: E402

DB = Path(os.environ.get("SETHSTER_DB") or DATA / "sethster.db")  # the catch-up loop splits from its full import
OUT = DATA / "sections"
ALIGN_DIR = F.CACHE / "align"
REF_CHOICE = F.CACHE / "ref_choice.json"
TRANSCRIPTS = DATA / "transcripts"

SNAP_WINDOW = 6.0        # s: snap a reference-mapped boundary to applause/silence this close
TRACK_SNAP = float(os.environ.get("SEG_TRACK_SNAP", 10.0))  # s: ... or to a live track boundary this close
MIN_QUALITY = 0.22       # reject a reference alignment below this global quality
REVIEW_BELOW = 0.5       # sections whose weakest boundary is below this need review

ACT_RE = re.compile(r"\b(?:act|akt|atto|acte|aufzug)\s*([ivx]+|\d+)\b|\b(erster|zweiter|dritter|first|second|third)"
                    r"\s+(?:aufzug|akt|act)\b|\b(prolog(?:ue|o)?|epilog(?:ue|o)?)\b", re.I)
_ORD = {"erster": 1, "first": 1, "zweiter": 2, "second": 2, "dritter": 3, "third": 3}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def act_of(text: str | None) -> str | None:
    if not text:
        return None
    m = ACT_RE.search(text)
    if not m:
        return None
    if m.group(1):
        v = m.group(1).lower()
        n = int(v) if v.isdigit() else _ROMAN.get(v)
        return f"Act {n}" if n else None
    if m.group(2):
        return f"Act {_ORD[m.group(2).lower()]}"
    return "Prologue" if m.group(3).lower().startswith("prolog") else "Epilogue"


# ---------------------------------------------------------------------------------------------- data

@lru_cache(maxsize=1)
def nas_sizes() -> dict[str, int]:
    out = {}
    p = DATA / "nas_files.tsv"
    if p.exists():
        for line in p.open(encoding="utf-8", errors="replace"):
            a, _, b = line.rstrip("\n").rpartition("\t")
            if b.isdigit():
                out[a] = int(b)
    return out


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@lru_cache(maxsize=1)
def canonical_map() -> tuple[dict, dict]:
    with db() as conn:
        return CANON.build(CANON.load_rows(conn))


@dataclass
class LiveTrack:
    id: str
    index: int
    title: str
    work_id: str | None
    segment: str
    path: str               # file analysed and played (stream_path)
    size: int | None
    duration: float = 0.0
    events: list[D.Event] = field(default_factory=list)
    feats: dict | None = None
    offset: float = 0.0     # start within the performance timeline


def load_tracks(conn, pid: str) -> list[LiveTrack]:
    rows = conn.execute("SELECT id, idx, title, work_id, segment, stream_path, path, duration_s FROM tracks "
                        "WHERE performance_id=? ORDER BY idx", (pid,)).fetchall()
    sizes = nas_sizes()
    out = []
    for r in rows:
        p = r["stream_path"] or r["path"]
        out.append(LiveTrack(id=r["id"], index=r["idx"], title=r["title"] or "", work_id=r["work_id"],
                             segment=r["segment"] or "other", path=p, size=sizes.get(p),
                             duration=float(r["duration_s"] or 0)))
    return out


def speech_by_file(pid: str) -> dict[str, list[tuple[float, float, str]]]:
    p = TRANSCRIPTS / f"{pid}.json"
    out: dict[str, list] = {}
    if not p.exists():
        return out
    try:
        d = json.loads(p.read_text())
    except Exception:
        return out
    for s in d.get("segments", []):
        out.setdefault(s.get("file"), []).append((float(s["start"]), float(s["end"]), s.get("text") or ""))
    return out


# ---------------------------------------------------------------------------------------- references

def _ref_choices() -> dict:
    try:
        return json.loads(REF_CHOICE.read_text())
    except Exception:
        return {}


def _save_ref_choice(cid: str, value: dict) -> None:
    d = _ref_choices()
    d[cid] = value
    REF_CHOICE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REF_CHOICE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    tmp.replace(REF_CHOICE)


def ref_cens(ref: R.Reference, log) -> tuple[np.ndarray, list[float]]:
    """CENS of the whole reference set + its track start times (and total) in seconds."""
    parts, starts, t = [], [], 0.0
    for tr in ref.tracks:
        f, cached = F.ensure(tr.path, tr.size)
        dur = float(f["duration"][0])
        if not cached:
            log(f"    ref features: {tr.path.rsplit('/', 1)[-1]} ({dur:.0f}s)")
        starts.append(t)
        c = AL.cens(f["chroma"])
        n = int(round(dur * AL.RATE))
        c = c[:n] if len(c) >= n else np.vstack([c, np.repeat(c[-1:], n - len(c), 0)]) if len(c) else np.zeros((n, 12))
        parts.append(c)
        tr.duration = dur
        t += dur
    starts.append(t)
    return np.vstack(parts).astype(np.float32), starts


# ------------------------------------------------------------------------------------------ timeline

class WorkTimeline:
    """The music tracks of one work, concatenated: work time <-> (track, offset)."""

    def __init__(self, tracks: list[LiveTrack]):
        self.tracks = tracks
        self.starts = []
        t = 0.0
        for tr in tracks:
            self.starts.append(t)
            t += tr.duration
        self.total = t

    def locate(self, t: float) -> tuple[LiveTrack, float]:
        t = min(max(t, 0.0), self.total)
        k = max(0, int(np.searchsorted(self.starts, t, side="right")) - 1)
        tr = self.tracks[k]
        return tr, min(t - self.starts[k], tr.duration)

    def work_time(self, tr: LiveTrack, off: float) -> float:
        return self.starts[self.tracks.index(tr)] + off

    def cens(self) -> np.ndarray:
        parts = []
        for tr in self.tracks:
            c = AL.cens(tr.feats["chroma"])
            n = int(round(tr.duration * AL.RATE))
            if len(c) < n:
                c = np.vstack([c, np.repeat(c[-1:] if len(c) else np.full((1, 12), 12 ** -0.5), n - len(c), 0)])
            parts.append(c[:n])
        return np.vstack(parts).astype(np.float32) if parts else np.zeros((0, 12), np.float32)


@dataclass
class Boundary:
    work_t: float
    source: str                 # reference | applause | silence | track | text | ordinal
    confidence: float
    snapped: str | None = None
    shift_s: float = 0.0

    def to_json(self) -> dict:
        return {"source": self.source, "confidence": round(float(self.confidence), 3), "snapped_to": self.snapped,
                "shift_s": round(self.shift_s, 2)}


def snap(tl: WorkTimeline, t: float, base_conf: float, source: str) -> Boundary:
    """Move a mapped boundary onto the nearest live track start, applause end or silence."""
    tr, off = tl.locate(t)
    k = tl.tracks.index(tr)
    # live track boundaries are exact cut points
    cand = []
    if off < TRACK_SNAP:
        cand.append((off, tl.starts[k]))
    if tr.duration - off < TRACK_SNAP and k + 1 < len(tl.tracks):
        cand.append((tr.duration - off, tl.starts[k + 1]))
    if cand:
        d, tt = min(cand)
        return Boundary(tt, f"{source}+track", min(1.0, base_conf + 0.25), "track", tt - t)
    ev, cut = D.nearest(tr.events, off, SNAP_WINDOW)
    if ev is not None:
        bonus = (0.2 if ev.kind == "applause" else 0.12) * ev.strength
        if tr.duration - cut < 20 and k + 1 < len(tl.tracks):
            # applause running out the track: the next number starts with the next file
            return Boundary(tl.starts[k + 1], f"{source}+{ev.kind}+track", min(1.0, base_conf + bonus + 0.1),
                            "track", tl.starts[k + 1] - t)
        return Boundary(tl.starts[k] + cut, f"{source}+{ev.kind}", min(1.0, base_conf + bonus), ev.kind, cut - off)
    return Boundary(t, source, base_conf * 0.9, None, 0.0)


def applause_tail(tl: WorkTimeline, start: float, end: float) -> float | None:
    """Work time where trailing applause begins inside [start, end), if any."""
    best = None
    for k, tr in enumerate(tl.tracks):
        t0 = tl.starts[k]
        for e in tr.events:
            if e.kind != "applause":
                continue
            a, b = t0 + e.start, t0 + e.end
            if a > start + 10 and b >= end - 4 and a < end:
                best = a if best is None else min(best, a)
    return best


# ------------------------------------------------------------------------------------------ sections

_MVT = re.compile(r"^\s*(?:mvts?\.?|movements?|satz)\s*(\d+)|^\s*([IVX]+)\.\s", re.I)
_HEDGE = re.compile(r"\b(inferred|hypothesis|probabl\w*|possibl\w*|perhaps|tentative|unclear|unidentified)\b", re.I)


def movement_label(title: str) -> str:
    """'Mvt 2: Allegretto, inferred from duration' -> '2. Allegretto'."""
    t = re.split(r",?\s*\(?(?:inferred|hypothesis|probably|possibly|perhaps|tentative)\b", title)[0].strip(" ,;-(")
    m = re.match(r"^\s*(?:mvts?\.?|movements?)\s*(\d+)\s*[:.-]?\s*(.*)$", t, re.I)
    if m:
        return f"{m.group(1)}. {m.group(2)}".strip(" .") if m.group(2) else f"Movement {m.group(1)}"
    return t


def internal_candidates(conn, cid: str, pid: str, music_s: float) -> list[R.Reference]:
    """Other versions of the same piece in the collection whose files are one movement each."""
    canon, w2c = canonical_map()
    wids = [w for w, c in w2c.items() if c == cid]
    if not wids:
        return []
    q = ",".join("?" * len(wids))
    rows = conn.execute(f"SELECT performance_id, idx, title, stream_path, path, duration_s FROM tracks WHERE "
                        f"work_id IN ({q}) AND segment='music' AND performance_id != ? ORDER BY performance_id, idx",
                        (*wids, pid)).fetchall()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["performance_id"], []).append(r)
    sizes = nas_sizes()
    out = []
    for opid, trs in by.items():
        if not 2 <= len(trs) <= 12 or any(b["idx"] - a["idx"] > 2 for a, b in zip(trs, trs[1:])):
            continue
        mv = sum(1 for t in trs if _MVT.search(t["title"] or ""))
        if mv < 0.75 * len(trs):
            continue
        hedge = sum(1 for t in trs if _HEDGE.search(t["title"] or "")) / len(trs)
        ref = R.Reference(id=f"perf:{opid}", path=opid, name=f"{opid} (per-movement files)")
        for n, t in enumerate(trs, 1):
            p = t["stream_path"] or t["path"]
            ref.tracks.append(R.RefTrack(path=p, size=sizes.get(p), duration=float(t["duration_s"] or 0),
                                         title=movement_label(t["title"] or ""), disc="", number=n))
        dur = sum(t.duration for t in ref.tracks)
        fit = 0.5
        if dur > 0 and music_s:
            ratio = dur / music_s
            if not 0.7 <= ratio <= 1.4:
                continue
            fit = 1 - min(1.0, abs(math.log(ratio)) / math.log(1.4))
        ref.score = 0.6 - 0.3 * hedge + 0.3 * fit
        out.append(ref)
    out.sort(key=lambda r: (-r.score, r.id))
    return out[:2]


def reference_sections(cw, tl: WorkTimeline, comp: str, log, *, work_title: str, conn=None, pid: str = ""
                       ) -> tuple[list[dict], dict] | None:
    """Sections for one work by aligning to a reference: a Crutchfield set, else another version in the
    collection with one file per movement. None if no usable reference."""
    if tl.total < 300:
        return None
    choice = _ref_choices().get(cw.id) or {}
    cands: list[R.Reference] = []
    if not (choice.get("none") and choice.get("v") == VERSION):
        cands = R.candidates(comp, work_title, tl.total)
        rid = choice.get("recording_id")
        if rid and not any(c.id == rid for c in cands):
            # the reference other versions of this piece use: keep section keys comparable across versions
            row = next((x for x in R.inventory() if x["id"] == rid), None)
            if row:
                ref = R.build_reference(row, work_title, comp)
                R.drop_fillers(ref)
                cands.insert(0, ref)
        if rid:
            cands.sort(key=lambda r: r.id != rid)
        if not cands:
            _save_ref_choice(cw.id, {"none": True, "v": VERSION})
    internal = conn is not None and not cands
    if internal:
        cands = internal_candidates(conn, cw.id, pid, tl.total)
        # the version whose own files are per-movement keeps its own track cuts
        own_mv = sum(1 for t in tl.tracks if _MVT.search(t.title or ""))
        if len(tl.tracks) >= 2 and own_mv >= 0.75 * len(tl.tracks):
            return None
    if not cands:
        return None
    A = tl.cens()
    tried = []
    for ref in cands[:3]:
        t0 = time.time()
        try:
            B, rstarts = ref_cens(ref, log)
        except Exception as e:  # pragma: no cover - network
            log(f"    ref {ref.id} unavailable: {e}")
            continue
        al = AL.Alignment(A, B)
        cov_a, cov_b = al.coverage
        log(f"    align {ref.id}: q={al.quality:.2f} cost={al.cost:.3f} base={al.baseline:.3f} "
            f"cov={cov_a:.2f}/{cov_b:.2f} ({time.time() - t0:.1f}s)")
        tried.append({"recording_id": ref.id, "quality": round(al.quality, 3)})
        if al.quality >= MIN_QUALITY and cov_b >= 0.6:
            if not internal:
                _save_ref_choice(cw.id, {"recording_id": ref.id, "v": VERSION})
            secs = _sections_from_alignment(cw, tl, ref, rstarts, al)
            secs, moved = _realign_absorbed(cw, tl, ref, rstarts, B, al, secs, log)
            tq = {}
            for k, tr in enumerate(tl.tracks):
                q = al.quality_in(tl.starts[k], tl.starts[k] + tr.duration)
                tq[tr.id] = round(q, 3) if q is not None else None
            # tracks the alignment can't place: wrong play order, wrong work, or a different edition
            suspect = [t for t, q in tq.items() if q is None or q < 0.25]
            if internal:
                for x in secs:
                    if not x.get("_cut"):
                        x["key"] = f"mv{x['ref']['track']:02d}"
            return secs, {**ref.to_json(), "kind": "collection" if internal else "crutchfield",
                          "alignment_quality": round(al.quality, 3), "coverage_live": round(cov_a, 3),
                          "coverage_ref": round(cov_b, 3), "tried": tried, "track_quality": tq,
                          "suspect_tracks": suspect, "out_of_order_tracks": moved}
    if not internal and not choice:
        _save_ref_choice(cw.id, {"none": True, "v": VERSION, "tried": tried})
    return None


GAP_MIN = 15.0   # s of extra live material at a reference boundary that becomes its own (non-music) span


def snap_end(tl: WorkTimeline, t: float, base_conf: float, source: str) -> Boundary:
    """Snap the END of a number: onto a track end, the start of applause, or the start of a silence."""
    tr, off = tl.locate(t)
    k = tl.tracks.index(tr)
    if off < max(1.0, TRACK_SNAP) and k > 0:   # just after a track start: the number ended with the last file
        k -= 1
        tr, off = tl.tracks[k], tl.tracks[k].duration
    if tr.duration - off < TRACK_SNAP:
        tt = tl.starts[k] + tr.duration
        return Boundary(tt, f"{source}+track", min(1.0, base_conf + 0.25), "track", tt - t)
    best, best_score = None, -1.0
    for e in tr.events:
        if e.end < off - SNAP_WINDOW or e.start > off + SNAP_WINDOW:
            continue
        d = 0.0 if e.start <= off <= e.end else min(abs(off - e.start), abs(off - e.end))
        sc = e.strength * (1 - d / SNAP_WINDOW)
        if sc > best_score:
            best, best_score = e, sc
    if best is not None:
        bonus = (0.2 if best.kind == "applause" else 0.12) * best.strength
        cut = best.start if best.kind == "applause" else min(best.end, best.start + 0.3)
        return Boundary(tl.starts[k] + cut, f"{source}+{best.kind}", min(1.0, base_conf + bonus), best.kind, cut - off)
    return Boundary(t, source, base_conf * 0.9, None, 0.0)


def _sections_from_alignment(cw, tl, ref: R.Reference, rstarts: list[float], al: AL.Alignment) -> list[dict]:
    K = len(ref.tracks)
    starts: list[Boundary | None] = [None] * (K + 1)   # start of ref track k
    ends: list[Boundary | None] = [None] * (K + 1)     # end of ref track k-1
    for k in range(K + 1):
        e_t, s_t, q = al.b_span(rstarts[k] if k < K else rstarts[K] - 1e-6)
        if k == K:
            e_t = s_t = al.b_to_a(rstarts[K])[0]
        base = (0.4 + 0.5 * q) if k in (0, K) else (0.25 + 0.7 * q)
        if k < K:
            starts[k] = snap(tl, s_t, base, "reference")
        if k > 0:
            ends[k] = snap_end(tl, e_t, base, "reference")
            if k < K and starts[k].work_t - ends[k].work_t < GAP_MIN:
                # no real gap: one shared cut (prefer the start-side snap: applause end / next track)
                ends[k] = starts[k]
    # monotone
    last = 0.0
    for k in range(K + 1):
        for b in (ends[k], starts[k]):
            if b is not None:
                b.work_t = max(b.work_t, last)
                last = b.work_t
    out = []
    for k, rt in enumerate(ref.tracks):
        a, b = starts[k], ends[k + 1]
        dur = b.work_t - a.work_t
        if dur < 1.0 and rt.duration > 20:
            out.append({"_cut": True, "ref_track": rt.number, "title": rt.title, "ref_duration_s": rt.duration})
            continue
        tempo = dur / rt.duration if rt.duration > 0 else None
        if tempo is not None and not 0.6 <= tempo <= 1.6:
            # the live span is far longer/shorter than the reference number: something is inserted/cut
            a = Boundary(a.work_t, a.source, a.confidence * 0.6, a.snapped, a.shift_s)
            b = Boundary(b.work_t, b.source, b.confidence * 0.6, b.snapped, b.shift_s)
        out.append({
            "work_start": a.work_t, "work_end": b.work_t, "start_b": a, "end_b": b,
            "label": rt.title or f"Track {rt.number}", "label_source": "reference" if rt.title else "ordinal",
            "key": f"r{rt.number:03d}", "ref": {"track": rt.number, "title": rt.title,
                                                "duration_s": round(rt.duration, 2), "disc": rt.disc},
            "tempo_ratio": round(tempo, 3) if tempo is not None else None,
            "ref_group": R_act(rt.title),
        })
    return out


def _realign_absorbed(cw, tl: WorkTimeline, ref, rstarts, B, al: AL.Alignment, secs: list[dict], log
                      ) -> tuple[list[dict], list[str]]:
    """Tracks the global (monotone) path swallowed -- it advanced < 15% of their length through the
    reference -- are usually in the wrong play order (or a different edition). Realign each run of
    them on its own as a subsequence of the reference and splice their sections in."""
    absorbed = [k for k, tr in enumerate(tl.tracks)
                if tr.duration > 60 and al.b_extent(tl.starts[k], tl.starts[k] + tr.duration) < 0.15 * tr.duration]
    if not absorbed:
        return secs, []
    runs: list[list[int]] = []
    for k in absorbed:
        if runs and k == runs[-1][-1] + 1:
            runs[-1].append(k)
        else:
            runs.append([k])
    moved: list[str] = []
    for run in runs:
        sub = WorkTimeline([tl.tracks[k] for k in run])
        if sub.total < 120:
            continue
        sal = AL.Alignment(sub.cens(), B, subsequence=True)
        if sal.quality < MIN_QUALITY + 0.1:
            continue
        log(f"    realigned out-of-order tracks {[tl.tracks[k].index for k in run]}: q={sal.quality:.2f}")
        off = tl.starts[run[0]]
        j0, j1 = sal.path[0, 1] / sal.rate, sal.path[-1, 1] / sal.rate
        sub_secs = _sections_from_alignment(cw, sub, ref, rstarts, sal)
        keep = []
        for x in sub_secs:
            if x.get("_cut"):
                continue
            rt = ref.tracks[x["ref"]["track"] - 1]
            r0, r1 = rstarts[rt.number - 1], rstarts[rt.number]
            if r1 <= j0 + 1 or r0 >= j1 - 1:        # outside what this run actually matched
                continue
            for k2 in ("start_b", "end_b"):
                b = x[k2]
                x[k2] = Boundary(b.work_t + off, b.source + "+reordered", b.confidence, b.snapped, b.shift_s)
            x["work_start"] += off
            x["work_end"] += off
            keep.append(x)
        if not keep:
            continue
        lo, hi = off, off + sub.total
        matched = {x["ref"]["track"] for x in keep}
        # drop what the global pass put in this stretch, clip sections that run into it, and drop
        # the global "cuts"/duplicates for the reference numbers this run now covers
        new = []
        for x in secs:
            if x.get("_cut"):
                if x["ref_track"] not in matched:
                    new.append(x)
                continue
            if x["work_end"] <= lo or x["work_start"] >= hi:
                if x["ref"] and x["ref"]["track"] in matched and x["work_end"] - x["work_start"] < 30:
                    continue
                new.append(x)
                continue
            if x["work_start"] < lo:
                new.append(dict(x, work_end=lo, end_b=Boundary(lo, "track", 0.9, "track")))
            if x["work_end"] > hi:
                new.append(dict(x, work_start=hi, start_b=Boundary(hi, "track", 0.9, "track")))
        secs = sorted(new + keep, key=lambda x: x.get("work_start", -1))
        moved += [tl.tracks[k].id for k in run]
    out = []
    for x in secs:
        if x.get("_cut") or not x.get("ref"):
            out.append(x)
            continue
        dur = x["work_end"] - x["work_start"]
        rd = x["ref"]["duration_s"] or 0
        if dur < 5 and rd > 30:
            out.append({"_cut": True, "ref_track": x["ref"]["track"], "title": x["ref"]["title"], "ref_duration_s": rd})
            continue
        if rd and dur / rd > 2.5:
            # remnant of a span the monotone pass swallowed: keep it as unlabelled, low-confidence music
            x = dict(x, label=None, label_source="ordinal", key=None, ref=None, tempo_ratio=None,
                     start_b=Boundary(x["work_start"], x["start_b"].source, 0.2, x["start_b"].snapped),
                     end_b=Boundary(x["work_end"], x["end_b"].source, 0.2, x["end_b"].snapped))
        out.append(x)
    # a reference number left in several places (clipped remnants): keep the piece that sits next to
    # its neighbouring numbers, unlabel the rest
    by_key: dict[int, list[dict]] = {}
    for x in out:
        if not x.get("_cut") and x.get("ref"):
            by_key.setdefault(x["ref"]["track"], []).append(x)
    starts_of = {n: [y["work_start"] for y in v] for n, v in by_key.items()}
    ends_of = {n: [y["work_end"] for y in v] for n, v in by_key.items()}
    for n, xs in by_key.items():
        if len(xs) < 2:
            continue
        def score(y):
            nxt = any(abs(y["work_end"] - t) < 2 for t in starts_of.get(n + 1, []))
            prv = any(abs(y["work_start"] - t) < 2 for t in ends_of.get(n - 1, []))
            return (nxt + prv, y["work_end"] - y["work_start"])
        best = max(xs, key=score)
        for y in xs:
            if y is not best:
                y.update(label=None, label_source="ordinal", key=None, ref=None, tempo_ratio=None,
                         start_b=Boundary(y["work_start"], y["start_b"].source, 0.2, y["start_b"].snapped),
                         end_b=Boundary(y["work_end"], y["end_b"].source, 0.2, y["end_b"].snapped))
    return out, moved


def _split_at_foreign_tracks(tl: WorkTimeline, secs: list[dict]) -> list[dict]:
    """A section must not run across a track that is not part of the work (an announcer break between
    two music files): split it there; the pieces share the key with a .1/.2 suffix."""
    out = []
    for s in secs:
        a_tr, _ = tl.locate(s["work_start"])
        b_tr, b_off = tl.locate(s["work_end"])
        ka, kb = tl.tracks.index(a_tr), tl.tracks.index(b_tr)
        if b_off <= 0.0 and kb > ka:
            kb -= 1
        cuts = [k for k in range(ka, kb) if tl.tracks[k + 1].index - tl.tracks[k].index > 1]
        if not cuts:
            out.append(s)
            continue
        edges = [s["work_start"]] + [tl.starts[k + 1] for k in cuts] + [s["work_end"]]
        for n in range(len(edges) - 1):
            piece = dict(s, work_start=edges[n], work_end=edges[n + 1], _part=n + 1, _cont=n > 0)
            if n > 0:
                piece["start_b"] = Boundary(edges[n], "track", 0.9, "track")
            if n < len(edges) - 2:
                piece["end_b"] = Boundary(edges[n + 1], "track", 0.9, "track")
            out.append(piece)
    return out


def R_act(title: str | None) -> str | None:
    return act_of(title)


def fallback_sections(tl: WorkTimeline, many_tracks: bool) -> list[dict]:
    """No reference: cut at live track boundaries, then at strong applause / long silences inside tracks."""
    cuts: list[Boundary] = [Boundary(0.0, "track", 0.95)]
    for k, tr in enumerate(tl.tracks):
        t0 = tl.starts[k]
        if k:
            cuts.append(Boundary(t0, "track", 0.95))
        if many_tracks:
            continue          # tracks already are the numbers/movements
        for e in tr.events:
            if e.start < 30 or e.end > tr.duration - 30:
                continue
            if e.kind == "applause" and e.end - e.start >= 4 and e.strength >= 0.5:
                cuts.append(Boundary(t0 + e.end, "applause", 0.35 + 0.45 * e.strength, "applause"))
            elif e.kind == "silence" and e.end - e.start >= 3.0 and e.strength >= 0.65:
                cuts.append(Boundary(t0 + max(e.start, e.end - 0.3), "silence", 0.25 + 0.35 * e.strength, "silence"))
    cuts.sort(key=lambda b: b.work_t)
    # enforce a minimum section length (keep the stronger cut)
    kept: list[Boundary] = []
    for c in cuts:
        if kept and c.work_t - kept[-1].work_t < 90:
            if c.source != "track" and (kept[-1].source == "track" or c.confidence <= kept[-1].confidence):
                continue
            if kept[-1].source != "track":
                kept[-1] = c
                continue
        kept.append(c)
    end = Boundary(tl.total, "track", 0.95)
    while len(kept) > 1 and kept[-1].source != "track" and tl.total - kept[-1].work_t < 90:
        kept.pop()
    out = []
    for k, a in enumerate(kept):
        b = kept[k + 1] if k + 1 < len(kept) else end
        if b.work_t - a.work_t < 1:
            continue
        out.append({"work_start": a.work_t, "work_end": b.work_t, "start_b": a, "end_b": b,
                    "label": None, "label_source": "ordinal", "key": None, "ref": None, "tempo_ratio": None,
                    "ref_group": None})
    return out


# ---------------------------------------------------------------------------------------------- main

def _r(x: float) -> float:
    return round(float(x), 2)


def build(pid: str, log=print, *, use_refs: bool = True) -> dict:
    t_start = time.time()
    conn = db()
    perf = conn.execute("SELECT id, title FROM performances WHERE id=?", (pid,)).fetchone()
    if not perf:
        raise KeyError(pid)
    tracks = load_tracks(conn, pid)
    canon, w2c = canonical_map()
    works_meta = {r["id"]: dict(r) for r in conn.execute(
        "SELECT w.id, w.title, w.catalogue, c.name AS composer FROM works w LEFT JOIN composers c "
        "ON c.id = w.composer_id WHERE w.id IN (SELECT work_id FROM tracks WHERE performance_id=?)", (pid,))}
    speech = speech_by_file(pid)
    stats = {"fetched": 0, "cached": 0, "audio_s": 0.0}

    # features + events per track
    t = 0.0
    for tr in tracks:
        try:
            f, cached = F.ensure(tr.path, tr.size)
        except Exception as e:
            log(f"  ! {tr.id}: {e}")
            tr.feats = None
            tr.offset = t
            t += tr.duration
            continue
        stats["cached" if cached else "fetched"] += 1
        tr.feats = f
        tr.duration = float(f["duration"][0])
        stats["audio_s"] += tr.duration
        tr.events = D.detect(f)
        tr.offset = t
        t += tr.duration
    total = t

    # group music tracks by work, in order of first appearance
    groups: dict[str, list[LiveTrack]] = {}
    for tr in tracks:
        if tr.segment == "music" and tr.feats is not None:
            groups.setdefault(tr.work_id or "", []).append(tr)     # "": music the research couldn't attribute

    sections: list[dict] = []
    works_out = []
    covered: dict[str, list[tuple[float, float]]] = {}   # track id -> covered spans (by music sections)
    for wid, wtracks in groups.items():
        meta = works_meta.get(wid, {})
        cid = w2c.get(wid)
        cw = canon.get(cid) if cid else None
        tl = WorkTimeline(wtracks)
        ref_info = None
        secs = None
        unident = bool(cw and cw.unidentified)
        if use_refs and cw and not unident and not cw.excerpt_of:
            try:
                got = reference_sections(cw, tl, meta.get("composer") or cw.composer_name, log,
                                         work_title=cw.title, conn=conn, pid=pid)
            except Exception as e:  # keep going without the reference
                log(f"    reference alignment failed: {e!r}")
                got = None
            if got:
                secs, ref_info = got
        cuts = [s for s in (secs or []) if s.get("_cut")]
        if secs is None:
            mv = sum(1 for t in wtracks if _MVT.search(t.title or "")) >= 0.75 * len(wtracks)
            many = (len(wtracks) >= 2 and mv) or (len(wtracks) >= 3 and tl.total / len(wtracks) < 900)
            secs = fallback_sections(tl, many)
            method = "tracks" if many else "acoustic"
        else:
            secs = [s for s in secs if not s.get("_cut")]
            method = "reference"
        n_ord = 0
        first_index = len(sections)
        secs = _split_at_foreign_tracks(tl, secs)
        for s in secs:
            n_ord += 0 if s.get("_cont") else 1
            a_tr, a_off = tl.locate(s["work_start"])
            b_tr, b_off = tl.locate(s["work_end"])
            if b_off <= 0.0 and b_tr is not a_tr:
                # end exactly at a track start: express it as the end of the previous track
                prev = tl.tracks[tl.tracks.index(b_tr) - 1]
                b_tr, b_off = prev, prev.duration
            group = act_of(a_tr.title) or s.get("ref_group")
            label = s["label"]
            label_source = s["label_source"]
            if label is None:
                if method == "tracks" or (len(tl.tracks) > 1 and a_off < 1 and
                                          abs(s["work_end"] - (tl.starts[tl.tracks.index(a_tr)] + a_tr.duration)) < 1):
                    label, label_source = a_tr.title, "track"
                else:
                    label, label_source = (f"{group} · part {n_ord}" if group else f"Part {n_ord}"), "ordinal"
            mv_tracks = sum(1 for t in tl.tracks if _MVT.search(t.title or "")) >= 0.75 * len(tl.tracks)
            if s["key"] is None and mv_tracks and len(tl.tracks) >= 2 and label_source == "track":
                s["key"] = f"mv{tl.tracks.index(a_tr) + 1:02d}"
                label = movement_label(a_tr.title)
            key = s["key"] or (f"t{n_ord:03d}" if method == "tracks" else f"m{n_ord:03d}")
            if s.get("_part"):
                key = f"{key}.{s['_part']}"
                label = f"{label} (cont.)" if s.get("_cont") else label
            conf = float(min(s["start_b"].confidence, s["end_b"].confidence))
            tail = applause_tail(tl, s["work_start"], s["work_end"])
            clean_end_work = tail if tail is not None else s["work_end"]
            ce_tr, ce_off = tl.locate(clean_end_work)
            sec = {
                "kind": "music",
                "work_id": wid or None, "canonical_work_id": cid,
                "label": label, "label_source": label_source, "group": group,
                "key": key,
                "start": {"track_id": a_tr.id, "track_index": a_tr.index, "t": _r(a_off)},
                "end": {"track_id": b_tr.id, "track_index": b_tr.index, "t": _r(b_off)},
                "start_abs": _r(a_tr.offset + a_off), "end_abs": _r(b_tr.offset + b_off),
                "duration_s": _r(s["work_end"] - s["work_start"]),
                "music_end": {"track_id": ce_tr.id, "track_index": ce_tr.index, "t": _r(ce_off)}
                if tail is not None else None,
                "confidence": round(conf, 3),
                "needs_review": bool(conf < REVIEW_BELOW),
                "boundaries": {"start": s["start_b"].to_json(), "end": s["end_b"].to_json()},
                "reference": s["ref"],
                "tempo_ratio": s["tempo_ratio"],
            }
            sections.append(sec)
            for tr in tl.tracks:   # mark coverage per track
                lo = max(sec["start_abs"], tr.offset)
                hi = min(sec["end_abs"], tr.offset + tr.duration)
                if hi > lo:
                    covered.setdefault(tr.id, []).append((lo - tr.offset, hi - tr.offset))
        music_secs = sections[first_index:]
        works_out.append({
            "work_id": wid or None, "canonical_work_id": cid, "title": meta.get("title") or (cw.title if cw else None),
            "composer": meta.get("composer"), "method": method,
            "tracks": [tr.id for tr in wtracks], "music_s": _r(tl.total),
            "sections": len(music_secs),
            "needs_review": sum(1 for s in music_secs if s["needs_review"]),
            "reference": ref_info,
            "cuts": [{"ref_track": c["ref_track"], "title": c["title"], "ref_duration_s": round(c["ref_duration_s"], 1)}
                     for c in cuts],
            "announced": _announcement(tracks, wtracks[0], speech, meta, cw),
        })

    # non-music spans: whole non-music tracks, plus pre-roll / tails of music tracks not covered
    for tr in tracks:
        kind_map = {"announcer": "speech", "interview": "speech", "intermission": "intermission",
                    "applause": "applause", "other": "other", "music": "other"}
        spans = sorted(covered.get(tr.id, []))
        gaps, pos = [], 0.0
        for a, b in spans:
            if a - pos > 2.0:
                gaps.append((pos, a))
            pos = max(pos, b)
        if tr.duration - pos > 2.0:
            gaps.append((pos, tr.duration))
        for a, b in gaps:
            kind = kind_map.get(tr.segment, "other")
            if tr.segment == "music":
                app = sum(min(b, e.end) - max(a, e.start) for e in tr.events
                          if e.kind == "applause" and e.end > a and e.start < b)
                sp = sum(min(b, e) - max(a, s) for s, e, _ in speech.get(tr.path, []) if e > a and s < b)
                kind = "applause" if app > 0.4 * (b - a) else "speech" if sp > 0.3 * (b - a) else "other"
            sections.append({
                "kind": kind, "work_id": tr.work_id if tr.segment == "music" else None,
                "canonical_work_id": None, "label": tr.title if tr.segment != "music" else None,
                "label_source": "track", "group": None, "key": None,
                "start": {"track_id": tr.id, "track_index": tr.index, "t": _r(a)},
                "end": {"track_id": tr.id, "track_index": tr.index, "t": _r(b)},
                "start_abs": _r(tr.offset + a), "end_abs": _r(tr.offset + b), "duration_s": _r(b - a),
                "music_end": None, "confidence": 0.9 if tr.segment != "music" else 0.6,
                "needs_review": False, "boundaries": None, "reference": None, "tempo_ratio": None,
            })
    sections.sort(key=lambda s: (s["start_abs"], s["end_abs"]))
    roles = [r["role"] for r in conn.execute("SELECT DISTINCT role FROM credits WHERE performance_id=? AND role IS NOT NULL",
                                             (pid,))]
    pieces = to_contract(sections, tracks, roles)
    events = []
    for tr in tracks:
        for e in tr.events:
            if e.kind == "applause" or (e.kind == "silence" and e.end - e.start >= 1.5):
                events.append({"track_id": tr.id, "track_index": tr.index, **e.to_json()})
        for s, e, _ in speech.get(tr.path, []):
            events.append({"track_id": tr.id, "track_index": tr.index, "kind": "speech", "start": _r(s),
                           "end": _r(e), "strength": 1.0})
    music = [s for s in sections if s["kind"] == "music"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc = {
        "format_version": 1,
        "schema": "sethster.sections/1",
        "performance_id": pid,
        "generator": f"sethster-segment {VERSION} (chroma-DTW reference alignment + applause/silence detection)",
        "created_at": now,
        "title": perf["title"],
        "generated_at": now,
        "pipeline": VERSION,
        "duration_s": _r(total),
        "tracks": [{"track_id": tr.id, "index": tr.index, "file": tr.path, "duration_s": _r(tr.duration),
                    "offset_s": _r(tr.offset), "segment": tr.segment, "work_id": tr.work_id,
                    "analyzed": tr.feats is not None} for tr in tracks],
        "works": works_out,
        "sections": pieces,
        "events": events,
        "stats": {"music_sections": len(music), "needs_review": sum(1 for s in music if s["needs_review"]),
                  "files_fetched": stats["fetched"], "files_cached": stats["cached"],
                  "audio_s": _r(stats["audio_s"]), "wall_s": _r(time.time() - t_start)},
    }
    conn.close()
    return doc


_KIND_WORDS = [
    ("overture", r"\b(overture|ouvert[uü]re|sinfonia)\b"), ("prelude", r"\b(prelude|pr[eé]lude|preludio|vorspiel)\b"),
    ("intermezzo", r"\b(intermezzo|entr'?acte|zwischenspiel|interlude)\b"), ("ballet", r"\b(ballet|ballo|danza|dance)\b"),
    ("march", r"\b(march|marcia|marsch|marche)\b"), ("recitative", r"\b(recit\w*)\b"),
    ("chorus", r"\b(chorus|coro|chor|choeur|ch[oœ]ur)\b"),
]
_GROUPS = {1: "aria", 2: "duet", 3: "trio", 4: "quartet", 5: "quintet", 6: "sextet"}
_NON_MUSIC = {"speech": "announcer", "intermission": "intermission", "applause": "applause", "other": "other"}
_ROMAN_OUT = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}


def _people_in(title: str | None) -> list[str]:
    m = re.findall(r"\(([^()]{2,80})\)", title or "")
    names = []
    for grp in m:
        names += [x.strip() for x in re.split(r"[,/&]| e | and | und ", grp) if x.strip()]
    return [n for n in names if re.match(r"[A-ZÀ-Ý]", n) and len(n) < 30]


def music_kind(sec: dict, n_in_work: int) -> str:
    label = (sec.get("label") or "")
    key = sec.get("key") or ""
    if key.startswith("mv") or re.match(r"^\s*(\d+|[IVX]+)\.\s", label) or re.search(r"\bmovement\b", label, re.I):
        return "movement"
    bare = re.sub(r"\([^()]*\)", " ", label)
    for kind, rx in _KIND_WORDS:
        if re.search(rx, bare, re.I):
            return kind
    ppl = [p for p in _people_in(label) if not re.fullmatch(r"(?i)coro|chorus|chor", p)]
    if any(re.fullmatch(r"(?i)coro|chorus|chor", p) for p in _people_in(label)):
        return "chorus" if not ppl else "ensemble"
    if ppl:
        return _GROUPS.get(len(ppl), "ensemble")
    if n_in_work == 1:
        return "work"
    if sec.get("group"):
        return "scene"
    if sec.get("label_source") in ("reference",):
        return "number"
    return "movement" if key.startswith("m") else "number"


def _roles_for(names: list[str], roles: list[str]) -> list[str]:
    from sethster_server.text import norm_key as nk
    out = []
    for n in names:
        k = nk(n)
        if len(k) < 3:
            continue
        for r in roles:
            toks = nk(r).split()
            if any(t[:5] == k[:5] and min(len(t), len(k)) >= 4 for t in toks) and r not in out:
                out.append(r)
                break
    return out


def to_contract(sections: list[dict], tracks: list[LiveTrack], roles: list[str]) -> list[dict]:
    """Logical sections (which may run across files) -> the per-file pieces of docs/sections.md."""
    by_index = {t.index: t for t in tracks}
    per_work: dict[str, int] = {}
    for s in sections:
        if s["kind"] == "music":
            per_work[s["work_id"]] = per_work.get(s["work_id"], 0) + 1
    out: list[dict] = []
    for li, s in enumerate(sections, 1):
        a, b = s["start"]["track_index"], s["end"]["track_index"]
        span = [by_index[i] for i in range(a, b + 1) if i in by_index]
        parts = []
        for tr in span:
            st = s["start"]["t"] if tr.index == a else 0.0
            en = s["end"]["t"] if tr.index == b else tr.duration
            if en - st >= 0.5 or len(span) == 1:
                parts.append((tr, st, en))
        is_music = s["kind"] == "music"
        kind = music_kind(s, per_work.get(s["work_id"], 0)) if is_music else _NON_MUSIC.get(s["kind"], "other")
        if not is_music and s["kind"] == "speech" and any(t.segment == "interview" for t, _, _ in parts):
            kind = "interview"
        group = s.get("group")
        act = None
        if group:
            m = re.match(r"Act (\d+)", group)
            act = _ROMAN_OUT.get(int(m.group(1)), m.group(1)) if m else group
        chars = _roles_for(_people_in(s.get("label")), roles) if is_music else []
        b_src = (s.get("boundaries") or {}).get("start", {}).get("source", "") if s.get("boundaries") else ""
        source = "track" if (s["label_source"] == "track" and b_src.startswith("track")) or not is_music else "auto"
        for pi, (tr, st, en) in enumerate(parts, 1):
            piece = {
                "id": f"s{len(out) + 1:03d}",
                "track_id": tr.id, "file": tr.path,
                "start_s": _r(st), "end_s": _r(en),
                "kind": kind,
                "title": s.get("label") or (tr.title if not is_music else None) or kind.title(),
                "work_id": s.get("work_id"),
                "act": act, "number": None,
                "characters": chars, "performer_ids": [],
                "confidence": s.get("confidence"),
                "source": source,
                "continues": ("next" if pi < len(parts) else "prev") if len(parts) > 1 else None,
                "notes": None,
                # --- segmenter extensions (docs/sections.md, "Extensions") ---
                "section": f"S{li:03d}", "part": pi, "parts": len(parts),
                "key": s.get("key"), "canonical_work_id": s.get("canonical_work_id"),
                "label_source": s.get("label_source"), "group": group,
                "start_abs": _r(tr.offset + st), "end_abs": _r(tr.offset + en),
                "duration_s": _r(en - st), "section_start_abs": s["start_abs"], "section_end_abs": s["end_abs"],
                "section_duration_s": s["duration_s"],
                "music_end_s": s["music_end"]["t"] if s.get("music_end") and s["music_end"]["track_id"] == tr.id
                else None,
                "needs_review": s.get("needs_review", False),
                "boundaries": s.get("boundaries") if pi == 1 or pi == len(parts) else None,
                "reference": s.get("reference"),
                "tempo_ratio": s.get("tempo_ratio"),
            }
            out.append(piece)
    return out


def _announcement(tracks, first: LiveTrack, speech, meta, cw) -> dict | None:
    """The announcer's words just before a work starts (label evidence for the listener / reviewer)."""
    surname_ = (meta.get("composer") or (cw.composer_name if cw else "") or "").split()[-1:] or [""]
    words = {w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{4,}", f"{meta.get('title') or ''} {surname_[0]}")}
    i = tracks.index(first)
    lines = []
    for tr in tracks[max(0, i - 2): i + 1]:
        segs = speech.get(tr.path, [])
        if tr is first:
            segs = [s for s in segs if s[0] < 120]
        lines += [(tr, s) for s in segs]
    if not lines:
        return None
    text = " ".join(s[2].strip() for _, s in lines[-12:])
    hits = sorted(w for w in words if w in text.lower())
    if not hits:
        return None
    tr, s = lines[-12:][0]
    return {"track_id": tr.id, "t": _r(s[0]), "text": text[:600], "matched": hits, "source": "announcer"}


def write(doc: dict) -> Path:
    from . import textcheck
    tc = textcheck.TEXT_DIR / f"{doc['performance_id']}.json"
    if tc.exists():
        try:
            textcheck.apply(doc, json.loads(tc.read_text()))
        except Exception:
            pass
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{doc['performance_id']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    tmp.replace(p)
    return p
