#!/usr/bin/env python3
"""Build the local Spooky-October/Verdi priority queue from Crutchfield metadata."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "crutchfield"
INVENTORY = DATA / "inventory.jsonl"
HARVEST = DATA / "harvest"
THEMES = REPO / "data" / "themes" / "spooky_works.json"
PRIORITY = DATA / "priority_spooky.jsonl"
SUMMARY = DATA / "priority_spooky_summary.json"

EXCERPT_RE = re.compile(r"\b(?:hlts?|highlights?|excerpts?|extracts?|selections?|scenes?|arias?|suite|choruses?|overtures?)\b")
VERDI_OTHER_RE = re.compile(r"\b(?:requiem|messa|sacred|songs?|romances?|recital|gala|concert|anthology)\b")

# Used only to distinguish complete Verdi operas from other Verdi releases. All
# Verdi releases still enter the queue; unrecognized/other ones receive rank 4.
VERDI_OPERAS = (
    "oberto", "un giorno di regno", "nabucco", "i lombardi", "ernani",
    "i due foscari", "giovanna d arco", "alzira", "attila", "macbeth",
    "i masnadieri", "jerusalem", "il corsaro", "la battaglia di legnano",
    "luisa miller", "stiffelio", "rigoletto", "il trovatore", "la traviata",
    "les vepres siciliennes", "i vespri siciliani", "simon boccanegra",
    "aroldo", "un ballo in maschera", "la forza del destino", "don carlos",
    "don carlo", "aida", "otello", "falstaff",
)


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def contains(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number}: {exc}") from exc
    return rows


def first_track_fields(release_id: str) -> dict[str, str]:
    path = HARVEST / f"{release_id}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        track = doc["discs"][0]["tracks"][0]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return {}
    tags = track.get("tags") or {}
    flat: dict[str, object] = {}
    for family in tags.values():
        if isinstance(family, dict):
            flat.update({str(k).upper(): v for k, v in family.items()})
    flat.update({str(k).upper(): v for k, v in tags.items() if not isinstance(v, dict)})
    aliases = {
        "harvest.album": ("ALBUM", "TALB"),
        "harvest.title": ("TITLE", "TIT2"),
        "harvest.composer": ("COMPOSER", "TCOM", "TPE1", "ARTIST"),
    }
    result = {}
    for label, keys in aliases.items():
        value = next((flat[k] for k in keys if flat.get(k)), None)
        if isinstance(value, list):
            value = " ".join(map(str, value))
        if value:
            result[label] = str(value)
    return result


def work_index(theme: dict) -> list[dict]:
    indexed = []
    for work in theme.get("works", []):
        if not isinstance(work, dict) or not work.get("key"):
            continue
        item = dict(work)  # extra future fields are deliberately preserved/ignored
        item["_composers"] = [norm(work.get("composer"))]
        item["_composers"] += [norm(x) for x in work.get("composer_aliases", [])]
        item["_titles"] = [norm(work.get("title"))]
        item["_titles"] += [norm(x) for x in work.get("aliases", [])]
        item["_spooky"] = "spooky" in work.get("themes", []) or "spooky" in work.get("tags", [])
        item["_orchestral"] = "orchestral" in work.get("tags", [])
        indexed.append(item)
    return indexed


def matching_fields(fields: dict[str, str], needles: list[str]) -> list[str]:
    return [name for name, value in fields.items() if any(contains(norm(value), n) for n in needles)]


def choose_work(fields: dict[str, str], works: list[dict]) -> tuple[dict | None, list[str], list[dict]]:
    title_fields = {k: v for k, v in fields.items() if k != "composer" and not k.endswith(".composer")}
    composer_fields = {k: v for k, v in fields.items() if k == "composer" or k.endswith(".composer") or k in {"path", "name"}}
    candidates = []
    near = []
    for work in works:
        title_hits = matching_fields(title_fields, work["_titles"])
        if not title_hits:
            continue
        composer_hits = matching_fields(composer_fields, work["_composers"])
        if composer_hits:
            candidates.append((work, composer_hits + title_hits))
        else:
            near.append({"work_key": work["key"], "title_fields": title_hits})
    if not candidates:
        return None, [], near
    # Prefer the most specific (longest) matching title, then stable catalog order.
    candidates.sort(key=lambda pair: max((len(t) for t in pair[0]["_titles"] if any(contains(norm(fields[f]), t) for f in pair[1] if f in fields)), default=0), reverse=True)
    return candidates[0][0], list(dict.fromkeys(candidates[0][1])), near


def is_verdi(fields: dict[str, str]) -> tuple[bool, list[str]]:
    composer_fields = {k: v for k, v in fields.items() if k == "composer" or k.endswith(".composer") or k in {"path", "name"}}
    hits = matching_fields(composer_fields, ["verdi", "giuseppe verdi"])
    return bool(hits), hits


def build() -> tuple[list[dict], dict, list[dict]]:
    theme = json.loads(THEMES.read_text(encoding="utf-8"))
    works = work_index(theme)
    matches: list[dict] = []
    near_misses: list[dict] = []
    for rec in load_jsonl(INVENTORY):
        fields = {k: str(rec[k]) for k in ("composer", "title", "path", "name") if rec.get(k)}
        fields.update(first_track_fields(str(rec.get("id", ""))))
        work, work_fields, near = choose_work(fields, works)
        spooky_work = work if work and work["_spooky"] else None
        verdi, verdi_fields = is_verdi(fields)
        if near and not spooky_work:
            for item in near:
                near_misses.append({"id": rec.get("id"), "composer": rec.get("composer"), "title": rec.get("title") or rec.get("name"), **item})
        if not spooky_work and not verdi:
            continue

        combined = norm(" ".join(fields.values()))
        excerpt = bool(EXCERPT_RE.search(combined))
        themes = []
        if spooky_work:
            themes.append("spooky")
        if verdi:
            themes.append("verdi_month")
        if spooky_work:
            rank = 4 if spooky_work["_orchestral"] else (2 if excerpt else 1)
        else:
            opera = any(contains(combined, title) for title in VERDI_OPERAS)
            rank = 3 if opera and not excerpt and not VERDI_OTHER_RE.search(combined) else 4
        used = list(dict.fromkeys(work_fields + verdi_fields))
        confidence = "high" if spooky_work or (verdi and rank == 3) else "medium"
        matches.append({
            "id": rec.get("id"),
            "work_key": spooky_work.get("key") if spooky_work else None,
            "composer": spooky_work.get("composer") if spooky_work else (rec.get("composer") or "Verdi"),
            "title": rec.get("title") or rec.get("name") or fields.get("harvest.album") or "",
            "theme": themes,
            "match_on": used,
            "confidence": confidence,
            "rank": rank,
        })

    rank_counts = Counter(str(row["rank"]) for row in matches)
    work_counts = Counter(row["work_key"] if row["work_key"] is not None else "null" for row in matches)
    summary = {
        "schema": "sethster.priority-summary/1",
        "theme": theme.get("theme", "optoberfest"),
        "tag": theme.get("tag", "spooky"),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "total": len(matches),
        "by_rank": {key: rank_counts.get(key, 0) for key in ("1", "2", "3", "4")},
        "by_work_key": dict(sorted(work_counts.items())),
        "near_miss_count": len(near_misses),
    }
    atomic_text(PRIORITY, "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in matches))
    atomic_text(SUMMARY, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return matches, summary, near_misses


def order(ids: Iterable[str]) -> list[str]:
    """Stable-sort IDs by spooky priority rank; unknown IDs remain last."""
    try:
        ranks = {row["id"]: int(row["rank"]) for row in load_jsonl(PRIORITY)}
    except OSError:
        ranks = {}
    values = list(ids)
    return [value for _, value in sorted(enumerate(values), key=lambda pair: (ranks.get(pair[1], 5), pair[0]))]


def print_spot_check(matches: list[dict], near_misses: list[dict]) -> None:
    rng = random.Random(0x0C70B3F)  # repeatable random spot-checks across identical builds
    sample = rng.sample(matches, min(30, len(matches)))
    near = rng.sample(near_misses, min(15, len(near_misses)))
    print(f"Random sample matches ({len(sample)}):")
    for row in sample:
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    print(f"Near-misses: title hit without composer ({len(near)} of {len(near_misses)}):")
    for row in near:
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="rebuild priority JSONL and summary")
    args = parser.parse_args(argv)
    if args.command == "build":
        matches, summary, near_misses = build()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print_spot_check(matches, near_misses)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
