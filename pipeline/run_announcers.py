"""Transcribe the announcer segments of every broadcast in the inventory, resumably.

Runs pipeline/announcers.run() over data/inventory/broadcasts.jsonl in priority order, skipping
performances that already have data/transcripts/<id>.json. Failures are logged and skipped.
Progress (done, rate, ETA) goes to data/transcripts/progress.log.

    uv run python pipeline/run_announcers.py --audio-root <read-only share mount>
    uv run python pipeline/run_announcers.py --ssh-host user@nas.local --remote-root /volume1/music_sandbox
"""
import argparse
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import announcers  # noqa: E402
import urgent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "data/inventory/broadcasts.jsonl"
RECORDS = ROOT / "data/inventory/records"
OUT = ROOT / "data/transcripts"
WAVE = ROOT / "data/release/wave1.txt"
PRIORITY = ["Met", "BBC Proms", "NYPO", "Carnegie", "Boston", "Chicago", "Royal Opera", "Vienna",
            "Bayreuth", "Philadephia", "BBC Opera", "BBC Concert", "San Francisco", "Salzburg",
            "La Scala", "Cleveland"]


def queue() -> list[Path]:
    """Write each inventory record to its own JSON file (announcers.run takes a path) and order them."""
    RECORDS.mkdir(parents=True, exist_ok=True)
    recs = [json.loads(line) for line in INVENTORY.open()]
    recs = [r for r in recs if not r.get("duplicate_of_performance")]
    rank = {c: i for i, c in enumerate(PRIORITY)}
    # the release wave (data/release/wave1.txt) goes first so those performances are complete end to end
    # wave1.txt is ordered most-complete first, so the nearly finished ones get plugged first
    wave = {pid: i for i, pid in enumerate(WAVE.read_text().split())} if WAVE.exists() else {}
    recs.sort(key=lambda r: (wave.get(r["id"], len(wave)), rank.get(r["collection"], len(PRIORITY)), r["id"]))
    paths = []
    for r in recs:
        p = RECORDS / f"{r['id']}.json"
        if not p.exists():
            p.write_text(json.dumps(r, ensure_ascii=False))
        paths.append(p)
    return paths


def clean_stale_tmp(parent: str | None, max_age_h: float = 6) -> None:
    """Drop audio temp dirs left by a worker that was killed mid-item (live shards' dirs are younger)."""
    for d in Path(parent or tempfile.gettempdir()).glob("announcers_*"):
        if d.is_dir() and time.time() - d.stat().st_mtime > max_age_h * 3600:
            shutil.rmtree(d, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio-root", type=Path)
    ap.add_argument("--ssh-host", help="fetch audio over ssh instead of the mount")
    ap.add_argument("--remote-root", help="share root on the ssh host, e.g. /volume1/music_sandbox")
    ap.add_argument("--tmp", default=None)
    ap.add_argument("--shard", default="0/1", help="k/n: this worker takes every n-th item starting at k")
    a = ap.parse_args()
    if not (a.audio_root or (a.ssh_host and a.remote_root)):
        ap.error("give --audio-root, or --ssh-host and --remote-root")
    clean_stale_tmp(a.tmp)
    k, n = map(int, a.shard.split("/"))
    todo = [p for i, p in enumerate(queue()) if i % n == k and not (OUT / f"{p.stem}.json").exists()]
    log = (OUT / (f"progress.{k}.log" if n > 1 else "progress.log")).open("a")
    start, done = time.time(), 0
    log.write(f"{time.strftime('%F %T')} start: {len(todo)} to do\n")
    log.flush()
    left = list(todo)
    while left:
        # recordings of musicians who just died (data/deaths/priority.txt, reread each time) skip the line
        first = set(urgent.ids())
        p = next((q for q in left if q.stem in first), left[0])
        left.remove(p)
        try:
            d = announcers.run(str(p), a.audio_root, None, a.tmp, a.ssh_host, a.remote_root)
            done += 1
            per = (time.time() - start) / done
            eta_h = per * (len(todo) - done) / 3600
            log.write(f"{time.strftime('%F %T')} ok {d['id']} {json.dumps(d['stats'])} | {done}/{len(todo)} eta {eta_h:.1f}h\n")
        except Exception as e:  # keep going; one bad folder must not stop a multi-day run
            log.write(f"{time.strftime('%F %T')} FAIL {p.stem}: {e!r}\n{traceback.format_exc(limit=2)}\n")
        log.flush()


if __name__ == "__main__":
    main()
