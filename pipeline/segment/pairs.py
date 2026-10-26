"""Cross-version alignment maps: chroma-DTW between two performances of the same piece.

For every canonical work with >= 2 segmented performances, each pair gets
data/sections/align/<canonical_work_id>/<pidA>__<pidB>.json (pidA < pidB):

    {"schema": "sethster.alignment/1", "canonical_work_id", "a": pidA, "b": pidB, "quality",
     "a_tracks": [{"track_id", "work_offset_s", "duration_s"}], "b_tracks": [...],
     "points": [[a_work_s, b_work_s], ...]}      # monotone, every ~2 s of A

Work time = seconds into the concatenation of that work's music tracks (works[].tracks in the
sections file). Uses only cached features; a pair is skipped if either side's features are missing.
"""
from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

import numpy as np

from . import align as AL
from . import build as B
from . import features as F

ALIGN_OUT = B.OUT / "align"


def _work_view(doc: dict, wid: str) -> tuple[np.ndarray, list[dict]] | None:
    w = next((x for x in doc["works"] if x["work_id"] == wid), None)
    if not w:
        return None
    tracks = {t["track_id"]: t for t in doc["tracks"]}
    sizes = B.nas_sizes()
    parts, out, off = [], [], 0.0
    for tid in w["tracks"]:
        t = tracks[tid]
        f = F.load(t["file"], sizes.get(t["file"]))
        if f is None:
            return None
        c = AL.cens(f["chroma"])
        n = int(round(t["duration_s"] * AL.RATE))
        if len(c) < n:
            c = np.vstack([c, np.repeat(c[-1:], n - len(c), 0)]) if len(c) else np.zeros((n, 12), np.float32)
        parts.append(c[:n])
        out.append({"track_id": tid, "work_offset_s": round(off, 2), "duration_s": t["duration_s"]})
        off += t["duration_s"]
    return np.vstack(parts).astype(np.float32), out


def main(log=print, only: set[str] | None = None, force: bool = False) -> int:
    docs = {}
    for p in sorted(B.OUT.glob("*.json")):
        try:
            docs[p.stem] = json.loads(p.read_text())
        except Exception:
            continue
    by_canon: dict[str, list[tuple[str, str]]] = {}
    for pid, d in docs.items():
        for w in d.get("works", []):
            cid = w.get("canonical_work_id")
            if cid and w.get("music_s", 0) >= 120:
                by_canon.setdefault(cid, []).append((pid, w["work_id"]))
    n_done = 0
    for cid, members in sorted(by_canon.items()):
        if only and cid not in only:
            continue
        members = sorted(set(members))
        if len({m[0] for m in members}) < 2:
            continue
        for (pa, wa), (pb, wb) in itertools.combinations(members, 2):
            if pa == pb:
                continue
            out = ALIGN_OUT / cid / f"{pa}__{pb}.json"
            if out.exists() and not force:
                gen = max(docs[pa]["generated_at"], docs[pb]["generated_at"])
                try:
                    if json.loads(out.read_text()).get("generated_at", "") >= gen:
                        continue
                except Exception:
                    pass
            va, vb = _work_view(docs[pa], wa), _work_view(docs[pb], wb)
            if va is None or vb is None:
                continue
            t0 = time.time()
            al = AL.Alignment(va[0], vb[0])
            doc = {"schema": "sethster.alignment/1", "canonical_work_id": cid, "a": pa, "b": pb,
                   "a_work_id": wa, "b_work_id": wb,
                   "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "quality": round(al.quality, 3), "rate": al.rate,
                   "a_tracks": va[1], "b_tracks": vb[1], "points": al.sample_points(2.0)}
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(out)
            n_done += 1
            log(f"pair {cid}: {pa} ~ {pb} q={al.quality:.2f} ({time.time() - t0:.1f}s)")
    log(f"pairs: {n_done} alignment maps written")
    return n_done


if __name__ == "__main__":
    main()
