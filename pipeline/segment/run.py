"""Batch segmentation: one pass per recording, resumable, safe to run overnight.

    nice -n 10 .venv-segment/bin/python -m pipeline.segment.run --all            # every performance in the DB
    nice -n 10 .venv-segment/bin/python -m pipeline.segment.run --ids a b c      # just these
    ... --force        rebuild even if data/sections/<id>.json is current
    ... --pairs        afterwards, cross-version alignment maps for works with >1 version (compare.py)
    ... --workers 2    parallel performances (each worker ~1 CPU core + <1 GB RAM)

State: data/cache/segment/state.json ({id: {status, pipeline, wall_s, error}}); log: data/cache/segment/run.log.
A performance is skipped when its sections file exists with the current pipeline version, unless --force.
Features (per audio file) and the reference choice (per canonical work) are cached, so a re-run after a
crash or a pipeline tweak only redoes the cheap parts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .. import urgent
from . import VERSION
from . import build as B
from . import features as F

STATE = F.CACHE / "state.json"
LOG = F.CACHE / "run.log"


def _log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE)


def is_current(pid: str) -> bool:
    p = B.OUT / f"{pid}.json"
    if not p.exists():
        return False
    try:
        return json.loads(p.read_text()).get("pipeline") == VERSION
    except Exception:
        return False


def one(pid: str, use_refs: bool = True) -> dict:
    try:
        os.nice(0)
    except OSError:  # a darwin-background process (priority 20) can't renice; fine as is
        pass
    t0 = time.time()
    try:
        doc = B.build(pid, log=lambda m: _log(f"[{pid}] {m}"), use_refs=use_refs)
        B.write(doc)
        st = doc["stats"]
        return {"id": pid, "status": "ok", "pipeline": VERSION, "wall_s": round(time.time() - t0, 1),
                "audio_s": st["audio_s"], "sections": st["music_sections"], "needs_review": st["needs_review"],
                "methods": sorted({w["method"] for w in doc["works"]})}
    except Exception as e:
        return {"id": pid, "status": "error", "pipeline": VERSION, "wall_s": round(time.time() - t0, 1),
                "error": f"{e!r}", "trace": traceback.format_exc()[-1500:]}


def all_ids() -> list[str]:
    with B.db() as conn:
        rows = conn.execute("SELECT p.id, COALESCE(SUM(t.size), 0) AS sz FROM performances p "
                            "LEFT JOIN tracks t ON t.performance_id = p.id GROUP BY p.id").fetchall()
    # small ones first: quick feedback, and long operas share references warmed by earlier versions
    return [r["id"] for r in sorted(rows, key=lambda r: (r["sz"], r["id"]))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-refs", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--pairs", action="store_true", help="build cross-version alignment maps afterwards")
    ap.add_argument("--pairs-only", action="store_true")
    a = ap.parse_args(argv)
    if not a.pairs_only:
        ids = a.ids or (all_ids() if a.all else [])
        if not ids:
            ap.error("give --ids or --all")
        todo = [i for i in ids if a.force or not is_current(i)]
        first = set(urgent.ids())  # recent-death recordings first (data/deaths/priority.txt)
        todo.sort(key=lambda i: i not in first)
        _log(f"segment {VERSION}: {len(todo)} to do ({len(ids) - len(todo)} current), workers={a.workers}")
        st = load_state()
        done_audio, t_start = 0.0, time.time()
        if a.workers <= 1:
            results = (one(pid, not a.no_refs) for pid in todo)
            iterator = results
        else:
            ex = ProcessPoolExecutor(max_workers=a.workers)
            futs = [ex.submit(one, pid, not a.no_refs) for pid in todo]
            iterator = (f.result() for f in as_completed(futs))
        for n, res in enumerate(iterator, 1):
            st = load_state()
            st[res["id"]] = {k: v for k, v in res.items() if k != "id"}
            save_state(st)
            done_audio += res.get("audio_s") or 0
            el = time.time() - t_start
            rate = done_audio / el if el else 0
            if res["status"] == "ok":
                _log(f"[{n}/{len(todo)}] {res['id']}: {res['sections']} sections ({res['needs_review']} review), "
                     f"{res['audio_s'] / 60:.0f} min audio in {res['wall_s']:.0f}s; "
                     f"overall {rate:.0f}x realtime, {el / n * (len(todo) - n) / 60:.0f} min left")
            else:
                _log(f"[{n}/{len(todo)}] {res['id']}: ERROR {res['error']}")
    if a.pairs or a.pairs_only:
        from . import pairs
        pairs.main(log=_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
