#!/opt/homebrew/bin/python3
"""Write the small, read-only-input progress snapshot for the CD pipeline."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data"
CRUTCH = DATA / "crutchfield"
INVENTORY = CRUTCH / "inventory.jsonl"
OUTPUT = CRUTCH / "progress.json"


def iso(ts: float | None = None) -> str:
    return dt.datetime.fromtimestamp(time.time() if ts is None else ts, dt.timezone.utc).isoformat()


def running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def total_releases() -> int:
    with INVENTORY.open(encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def stage(files: list[Path], total: int, done_test, error_test, process_pattern: str, note: str, extra=None) -> dict:
    now=time.time(); completed=[]; errors=0; all_mtimes=[]
    for path in files:
        try:
            doc=json.loads(path.read_text(encoding="utf-8")); mtime=path.stat().st_mtime
        except (OSError,ValueError):
            errors+=1; continue
        all_mtimes.append(mtime)
        if done_test(doc): completed.append(mtime)
        if error_test(doc): errors+=1
    started=min(all_mtimes) if all_mtimes else None
    window=30 if started is not None and now-started<3600 else 60
    recent=sum(m>=now-window*60 for m in completed)
    rate=recent/(window/60) if recent else None
    done=len(completed); eta=None
    if done>=total and total:
        eta=iso(now)
    elif rate:
        eta=iso(now+(total-done)/rate*3600)
    result={"total":total,"done":done,"errors":errors,"started_at":iso(started) if started else None,
            "updated_at":iso(now),"rate_per_h":round(rate,3) if rate else None,"rate_window_min":window,
            "eta":eta,"running":running(process_pattern),"note":note}
    if extra: result.update(extra(files))
    return result


def detect_dates(_total: int):
    # TODO: define terminal facts/date states when facts.py lands.
    return None


def detect_align(_total: int):
    # TODO: use alignment-v2's registry/output contract once it is final.
    return None


def detect_notes(_total: int):
    # TODO: use researchd's CD-lane terminal states once that lane lands.
    return None


def detect_split(total: int):
    files=sorted((DATA/"sections").glob("cf-*.json")) if (DATA/"sections").is_dir() else []
    if not files: return None
    return stage(files,total,lambda d:bool(d),lambda d:bool(d.get("error")),r"pipeline\.crutchfield\.split|crutchfield/split\.py",
                 "CD releases with generated section records.")


def build() -> dict:
    total=total_releases()
    harvest_files=sorted((CRUTCH/"harvest").glob("*.json")) if (CRUTCH/"harvest").is_dir() else []
    art_files=[p for p in (DATA/"media"/"cd").glob("cf-*.json") if p.name!="dhash_index.jsonl"] if (DATA/"media"/"cd").is_dir() else []
    stages={
        "harvest":stage(harvest_files,total,lambda d:not d.get("fatal_error"),
                        lambda d:bool(d.get("fatal_error") or d.get("errors")),
                        r"pipeline\.crutchfield\.harvest run|harvest\.py run",
                        "Reads tags, sidecars and picture inventories from the read-only CD collection."),
        "art":stage(art_files,total,lambda d:d.get("status") in {"ok","none"},lambda d:d.get("status")=="error",
                    r"pipeline\.crutchfield\.art run|crutchfield/art\.py run",
                    "Selects CD fronts, backs and disc art and creates local thumbnails.",
                    lambda paths:{"with_front":sum(1 for p in paths if _has_front(p))}),
    }
    for key,detector in (("dates",detect_dates),("align",detect_align),("split",detect_split),("notes",detect_notes)):
        found=detector(total)
        if found is not None: stages[key]=found
    return {"schema":"sethster.cd-progress/1","updated_at":iso(),"stages":stages}


def _has_front(path: Path) -> bool:
    try: return bool(json.loads(path.read_text(encoding="utf-8")).get("front"))
    except (OSError,ValueError): return False


def write() -> dict:
    value=build(); OUTPUT.parent.mkdir(parents=True,exist_ok=True); tmp=OUTPUT.with_suffix(".json.tmp")
    with tmp.open("w",encoding="utf-8") as fh:
        json.dump(value,fh,ensure_ascii=False,indent=2); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp,OUTPUT); return value


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("command",choices=("update","loop")); args=p.parse_args()
    while True:
        value=write(); print(json.dumps(value,ensure_ascii=False,indent=2))
        if args.command=="update": return
        time.sleep(300)


if __name__=="__main__": main()
