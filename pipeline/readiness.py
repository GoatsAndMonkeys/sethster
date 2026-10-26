"""Which broadcasts are completely done, and what the rest are still waiting on.

A broadcast is done when it has all five:
  notes       liner notes (a research or pilot result)
  transcript  the Whisper transcript of its announcers
  split       split into pieces (data/sections)
  nas_photos  its NAS folders scanned for pictures (data/media/nas)
  web_photos  every live page its notes cite checked for photos (data/media/web/pages; the same pages
              pipeline.media.web plans, so Wikipedia, the Wayback Machine etc. don't count)

The app lists only done broadcasts (data/release/ready.txt), and every queue takes the ones closest to done
first (data/release/near.txt), so more broadcasts finish instead of every one being partly done.

    python -m pipeline.readiness        # writes ready.txt and near.txt, prints counts
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "data"
STEPS = ("notes", "transcript", "split", "nas_photos", "web_photos")
# kept in step with pipeline.media.web (not imported: that module needs httpx and Pillow)
SKIP_HOSTS = re.compile(r"(^|\.)(archive\.org|archive\.ph|archive\.today|wikipedia\.org|wikidata\.org|wikimedia\.org|"
                        r"musicbrainz\.org|operadis-opera-discography\.org\.uk)$")
SKIP_TYPES = {"wikidata", "wikipedia", "discography", "tags", "sidecar_file"}
TRANSIENT = re.compile(r"^(http_(429|5\d\d)|error|cooldown)")


def _stems(pattern: str) -> set[str]:
    return {os.path.basename(p).rsplit(".", 1)[0] for p in glob.glob(str(D / pattern))}


def _results() -> dict[str, Path]:
    out = {}
    for d in ("pilot/results", "research/results"):  # research overrides pilot, as in the importer
        for p in glob.glob(str(D / d / "*.json")):
            out[os.path.basename(p)[:-5]] = Path(p)
    return out


def web_pages(doc: dict) -> set[str]:
    """Page keys (pipeline.media.web.key) of the live pages these notes cite."""
    keys = set()
    for s in doc.get("sources") or []:
        url = (s.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or s.get("type") in SKIP_TYPES:
            continue
        if SKIP_HOSTS.search((urlparse(url).hostname or "").lower()):
            continue
        keys.add(hashlib.sha1(url.encode()).hexdigest()[:16])
    return keys


def _pages_checked() -> set[str]:
    """Pages fetched for good: any final status, or a transient one already tried three times."""
    done = set()
    for p in glob.glob(str(D / "media/web/pages/*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not TRANSIENT.match(d.get("status") or "") or d.get("attempts", 0) >= 3:
            done.add(os.path.basename(p)[:-5])
    return done


def order() -> list[str]:
    """Every broadcast (duplicates left out) in release order, then inventory order for the rest."""
    inv = [json.loads(l) for l in open(D / "inventory/broadcasts.jsonl", encoding="utf-8")]
    ids = [r["id"] for r in inv if not r.get("duplicate_of_performance")]
    known = set(ids)
    rel = [l.strip() for l in open(D / "release/order.txt") if l.strip() in known] if (D / "release/order.txt").exists() else []
    seen = set(rel)
    return rel + [i for i in ids if i not in seen]


def status() -> dict[str, list[str]]:
    """{broadcast id: steps still missing}, in release order."""
    results = _results()
    have = {
        "transcript": _stems("transcripts/*.json"),
        "split": _stems("sections/*.json"),
        "nas_photos": _stems("media/nas/*.json"),
    }
    checked = _pages_checked()
    out = {}
    for pid in order():
        miss = []
        f = results.get(pid)
        if not f:
            miss.append("notes")
        for step in ("transcript", "split", "nas_photos"):
            if pid not in have[step]:
                miss.append(step)
        if f:
            try:
                pages = web_pages(json.load(open(f, encoding="utf-8")))
            except (OSError, ValueError):
                pages = {""}
            if not pages <= checked:
                miss.append("web_photos")
        else:
            miss.append("web_photos")  # nothing to check until the notes say which pages to look at
        out[pid] = miss
    return out


def ready(st: dict[str, list[str]] | None = None) -> list[str]:
    st = status() if st is None else st
    return [p for p, m in st.items() if not m]


def near(st: dict[str, list[str]] | None = None) -> list[str]:
    """Not-yet-done broadcasts, fewest missing steps first (release order within a tie)."""
    st = status() if st is None else st
    pos = {p: i for i, p in enumerate(st)}
    return sorted((p for p, m in st.items() if m), key=lambda p: (len(st[p]), pos[p]))


def main() -> None:
    st = status()
    rd, nr = ready(st), near(st)
    (D / "release").mkdir(parents=True, exist_ok=True)
    tmp = D / "release/ready.txt.tmp"
    tmp.write_text("".join(p + "\n" for p in rd))
    tmp.replace(D / "release/ready.txt")
    tmp = D / "release/near.txt.tmp"
    tmp.write_text("".join(f"{p}  # missing {','.join(st[p])}\n" for p in nr))
    tmp.replace(D / "release/near.txt")
    c = Counter(tuple(m) for m in st.values() if m and "notes" not in m)
    print(f"done {len(rd)} of {len(st)}")
    for k, v in c.most_common(8):
        print(f"  {v:5d} researched, waiting on {' + '.join(k)}")
    print(f"  {sum(1 for m in st.values() if 'notes' in m):5d} waiting on liner notes")


if __name__ == "__main__":
    main()
