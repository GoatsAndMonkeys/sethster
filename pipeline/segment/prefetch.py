"""Analyse audio for the splitter ahead of research.

Splitting a recording needs to know which work each track holds, and only research says that. The slow
part, fetching every file from the NAS and computing its frame features, doesn't need it, so this fills
the feature cache (features.ensure) for recordings research hasn't reached yet. When their research
lands, the split itself takes seconds. Runs anywhere the NAS is reachable over ssh, e.g. a second Mac:

    python -m pipeline.segment.prefetch --list todo.tsv --workers 6       # lines: <NAS path>\t<size>
    python -m pipeline.segment.prefetch --make-list todo.tsv --shard 0,1,2/5  # from data/release/order.txt

The list is grouped by recording, most complete first; files already cached are skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from . import features as F


def make_list(out: str, shard: str) -> int:
    """Write the files of not-yet-split recordings, in release order, for shard "k/n" or "k1,k2/n"
    (whole recordings, so each machine finishes its own)."""
    data = F.DATA
    order = [l.strip() for l in open(data / "release" / "order.txt") if l.strip()]
    inv = {}
    for line in open(data / "inventory" / "broadcasts.jsonl"):
        b = json.loads(line)
        inv[b["id"]] = b
    ks, n = shard.split("/")
    ks, n = {int(x) for x in ks.split(",")}, int(n)
    todo = [i for i in order if i in inv and not (data / "sections" / f"{i}.json").exists()]
    rows = 0
    with open(out, "w") as fh:
        for j, pid in enumerate(todo):
            if j % n not in ks:
                continue
            for t in inv[pid]["tracks"]:
                if F.load(t["path"], t["size"]) is not None:
                    continue
                fh.write(f"{t['path']}\t{t['size']}\n")
                rows += 1
    return rows


def one(path: str, size: int) -> tuple[str, bool, str]:
    try:
        F.ensure(path, size)
        return path, True, ""
    except Exception as e:  # noqa: BLE001 - logged and skipped; the splitter retries it later
        return path, False, f"{e!r}"[:300]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", help="file of <NAS path>\\t<size> lines to analyse")
    ap.add_argument("--make-list", help="write that list instead, then exit")
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    if a.make_list:
        print(f"{make_list(a.make_list, a.shard)} files")
        return 0
    jobs = []
    for line in open(a.list, encoding="utf-8"):
        p, _, s = line.rstrip("\n").rpartition("\t")
        if p and F.load(p, int(s)) is None:
            jobs.append((p, int(s)))
    t0, ok, bad = time.time(), 0, 0
    print(f"{time.strftime('%H:%M:%S')} {len(jobs)} files to analyse, {a.workers} workers", flush=True)
    with ProcessPoolExecutor(a.workers) as ex:
        # submit in order, a bounded window ahead, so the most complete recordings finish first
        it = iter(jobs)
        live = {ex.submit(one, *j) for j in [next(it) for _ in range(min(len(jobs), a.workers * 2))]}
        while live:
            fut = next(as_completed(live))
            live.remove(fut)
            path, good, err = fut.result()
            ok += good
            bad += not good
            if not good:
                print(f"  ! {path}: {err}", flush=True)
            n = ok + bad
            if n % 50 == 0:
                el = time.time() - t0
                print(f"{time.strftime('%H:%M:%S')} {n}/{len(jobs)} files ({bad} failed), "
                      f"{n / el * 3600:.0f} files/h", flush=True)
            nxt = next(it, None)
            if nxt:
                live.add(ex.submit(one, *nxt))
    print(f"{time.strftime('%H:%M:%S')} done: {ok} ok, {bad} failed in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
