"""Recordings that skip the line at every step, in order:
1. data/deaths/priority.txt (written by pipeline.deaths): recent classical deaths;
2. data/themes/priority.txt: upcoming theme needs (Optoberfest spooky works, then Verdi operas).

Read fresh on each call, so a long run picks up changes without a restart."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "data/deaths/priority.txt"
FILES = (FILE, ROOT / "data/themes/priority.txt")


def ids() -> list[str]:
    out: list[str] = []
    for f in FILES:
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        out += [l.split()[0] for l in lines if l.strip() and not l.startswith("#")]
    return list(dict.fromkeys(out))
