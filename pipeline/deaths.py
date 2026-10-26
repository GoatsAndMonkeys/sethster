"""Recent classical deaths: find their recordings in the collection and move them to the front of the research queue.

    python -m pipeline.deaths              # last 45 days
    python -m pipeline.deaths --days 60 --dry-run

Who died comes from Wikipedia: the "Deaths in <month> <year>" lists for this month and last (kept only when the
description is a classical one: soprano, conductor, pianist, ...) and the Deaths section of "<year> in classical
music". A recording matches when the full name appears in its folder names or tags (the inventory), its announcer
transcript, or its research. Transcripts are done before web research for most recordings, so that is where most
names are found.

Each matched recording still waiting for research gets priority 0 (the front of the queue) and a note saying who
it is for. The run is written to data/deaths/recent.json. The catch-up loop runs this twice a day.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data/deaths/recent.json"
QUEUE = Path.home() / ".local/state/researchd/queue.db"
API = "https://en.wikipedia.org/w/api.php"
UA = "classical-radio-family-archive/0.1 (private, non-commercial)"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
CLASSICAL = re.compile(
    r"\b(opera|operatic|soprano|mezzo|contralto|tenor|baritone|bass-baritone|countertenor|bass singer|"
    r"conductor|pianist|violinist|violist|cellist|harpsichordist|organist|composer|oboist|flautist|flutist|"
    r"clarinet(t)?ist|bassoonist|horn player|harpist|musicologist|choral|chorus master|choirmaster|lieder|"
    r"classical|orchestra|impresario|accompanist|répétiteur|repetiteur)", re.I)
SOURCES = [("data/inventory/broadcasts.jsonl", "folder/tags"), ("data/transcripts/*.json", "transcript"),
           ("data/research/results/*.json", "research"), ("data/pilot/results/*.json", "research")]


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def wikitext(client: httpx.Client, title: str) -> str:
    for attempt in range(6):  # Wikipedia answers 429 while enrich_wiki is busy; wait as it asks
        r = client.get(API, params={"action": "parse", "page": title, "prop": "wikitext", "redirects": 1,
                                    "format": "json", "formatversion": 2, "maxlag": 5})
        if r.status_code != 429:
            break
        time.sleep(int(r.headers.get("Retry-After") or 0) or 15 * (attempt + 1))
    r.raise_for_status()
    return (r.json().get("parse") or {}).get("wikitext") or ""


def link_name(s: str) -> str:
    """[[Target (singer)|Shown]] -> Shown; [[Target (singer)]] -> Target."""
    m = re.match(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", s)
    if not m:
        return ""
    return (m.group(2) or re.sub(r"\s*\([^)]*\)$", "", m.group(1))).strip()


def month_deaths(text: str, year: int, month: int) -> list[dict]:
    """Entries under ==<Month>== / ===<day>=== (the year page) or ===<day>=== (a month page)."""
    name = MONTHS[month - 1]
    sec = re.search(rf"^==\s*{name}(?: {year})?\s*==\s*$(.*?)(?=^==[^=]|\Z)", text, re.M | re.S)
    body = sec.group(1) if sec else text
    out, day = [], None
    for line in body.splitlines():
        if m := re.match(r"^===\s*(\d{1,2})\s*===", line):
            day = int(m.group(1))
        elif line.startswith("*[[") and day:
            who = link_name(line[1:])
            desc = re.sub(r"<ref.*", "", line.split("]]", 1)[1]).strip(" ,.")
            if who and CLASSICAL.search(desc):
                out.append({"name": who, "died": date(year, month, day).isoformat(), "desc": desc[:160],
                            "source": f"Wikipedia: Deaths in {name} {year}"})
    return out


def classical_year_deaths(text: str, year: int) -> list[dict]:
    """"* [[20 August]] – [[Ileana Cotrubas]], Romanian soprano, 87" under ==Deaths==."""
    sec = re.search(r"^==\s*Deaths\s*==\s*$(.*?)(?=^==[^=]|\Z)", text, re.M | re.S)
    out = []
    for line in (sec.group(1) if sec else "").splitlines():
        m = re.match(r"^\*\s*\[\[(\d{1,2}) (\w+)\]\]\s*[–-]\s*(\[\[[^\]]+\]\])(.*)", line)
        if m and m.group(2) in MONTHS:
            desc = re.sub(r"<ref.*", "", m.group(4)).strip(" ,.")
            out.append({"name": link_name(m.group(3)), "died": date(year, MONTHS.index(m.group(2)) + 1,
                        int(m.group(1))).isoformat(), "desc": desc[:160], "source": f"Wikipedia: {year} in classical music"})
    return out


def who_died(since: date, today: date) -> list[dict]:
    seen: dict[str, dict] = {}
    months = sorted({(since.year, since.month), (today.year, today.month)})
    with httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as client:
        found = []
        for y, m in months:
            found += month_deaths(wikitext(client, f"Deaths in {MONTHS[m - 1]} {y}"), y, m)
        for y in sorted({since.year, today.year}):
            found += classical_year_deaths(wikitext(client, f"{y} in classical music"), y)
    for d in found:
        if since.isoformat() <= d["died"] <= today.isoformat():
            seen.setdefault(fold(d["name"]), d)
    return sorted(seen.values(), key=lambda d: d["died"], reverse=True)


def name_pattern(name: str) -> re.Pattern:
    """The full name, any spacing or hyphens between its parts; ö also as oe and so on."""
    variants = {fold(name), fold(name.replace("ö", "oe").replace("ü", "ue").replace("ä", "ae"))}
    parts = [r"[\s\-]+".join(re.escape(w) for w in re.split(r"[\s\-]+", v) if w) for v in variants]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b")


def find_recordings(people: list[dict]) -> None:
    pats = [(p, name_pattern(p["name"])) for p in people]
    for p in people:
        p["recordings"] = {}
    for pattern, where in SOURCES:
        for f in glob.glob(str(ROOT / pattern)):
            try:
                text = open(f, encoding="utf-8").read()
            except OSError:
                continue
            docs = ([(json.loads(l)["id"], l) for l in text.splitlines() if l.strip()] if f.endswith(".jsonl")
                    else [(os.path.basename(f)[:-5], text)])
            for pid, s in docs:
                s = fold(s)
                for p, pat in pats:
                    if pat.search(s):
                        p["recordings"].setdefault(pid, [])
                        if where not in p["recordings"][pid]:
                            p["recordings"][pid].append(where)


def prioritise(people: list[dict], dry_run: bool) -> list[str]:
    """Matched recordings still waiting for research go to the front of the queue."""
    c = sqlite3.connect(QUEUE, timeout=30)
    moved = []
    for p in people:
        for pid in p["recordings"]:
            r = c.execute("SELECT priority, state, stage FROM items WHERE id=?", (pid,)).fetchone()
            if r and r[1] == "pending" and r[0] > 0:
                moved.append(pid)
                if not dry_run:
                    c.execute("UPDATE items SET priority=0, note=?, updated=? WHERE id=?",
                              (f"recent death: {p['name']} ({p['died']})", time.time(), pid))
    c.commit()
    return moved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    today = date.today()
    people = who_died(today - timedelta(days=a.days), today)
    find_recordings(people)
    moved = prioritise(people, a.dry_run)
    hit = [p for p in people if p["recordings"]]
    print(f"{time.strftime('%F %T')} {len(people)} classical deaths in {a.days} days, {len(hit)} in the collection "
          f"({sum(len(p['recordings']) for p in hit)} recordings); {len(moved)} moved to the front of the queue"
          f"{' (dry run)' if a.dry_run else ''}")
    for p in hit:
        print(f"  {p['died']} {p['name']}: {', '.join(sorted(p['recordings']))}")
    if not a.dry_run:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps({"t": time.time(), "days": a.days, "people": people, "moved": moved},
                                  ensure_ascii=False, indent=1))
        tmp.replace(OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
