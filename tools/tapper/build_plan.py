#!/usr/bin/env python3
"""Build the GT-C tap plan from the research results and local inventories."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data/gt/taps/plan.json"


SELECTION = [
    ("verdi-rigoletto", "Giuseppe Verdi", "Rigoletto", "opera"),
    ("puccini-tosca", "Giacomo Puccini", "Tosca", "opera"),
    ("verdi-traviata", "Giuseppe Verdi", "La traviata", "opera"),
    ("bizet-carmen", "Georges Bizet", "Carmen", "opera"),
    ("mahler-symphony-no-2", "Gustav Mahler", "Symphony No. 2 in C minor 'Resurrection'", "orchestral"),
    ("shostakovich-symphony-no-5", "Dmitri Shostakovich", "Symphony No. 5 in D minor", "orchestral"),
    ("beethoven-piano-concerto-no-4", "Ludwig van Beethoven", "Piano Concerto No. 4 in G major", "concerto"),
    ("beethoven-string-quartet-no-12", "Ludwig van Beethoven", "String Quartet No. 12 in E-flat major", "chamber"),
]


TEMPLATES = {
    "verdi-rigoletto": [
        "Prelude — opening orchestral attack", "‘Questa o quella’ — Duke’s entrance",
        "‘Gran nuova!’ — Marullo’s entrance", "Monterone enters", "Monterone’s curse",
        "Rigoletto–Sparafucile duet begins", "‘Figlia! Mio padre!’ — Gilda enters",
        "‘Gualtier Maldè’ — Duke names himself", "‘Caro nome’ — Gilda begins",
        "Abduction chorus ‘Zitti, zitti’", "‘Ella mi fu rapita’ — Duke begins",
        "‘Cortigiani, vil razza dannata’", "‘La donna è mobile’",
        "Quartet ‘Bella figlia dell’amore’", "Final cry ‘La maledizione!’",
    ],
    "puccini-tosca": [
        "Act I opening chords", "Angelotti: ‘Ah! Finalmente!’", "Cavaradossi: ‘Recondita armonia’",
        "Tosca’s first ‘Mario!’", "Scarpia enters", "Te Deum begins",
        "Act II orchestral opening", "Cavaradossi brought in", "Torture scene begins",
        "Cavaradossi: ‘Vittoria!’", "Tosca: ‘Vissi d’arte’", "Tosca kills Scarpia",
        "Act III dawn / shepherd’s song", "Cavaradossi: ‘E lucevan le stelle’", "Execution volley",
    ],
    "verdi-traviata": [
        "Prelude begins", "Party chorus enters", "Brindisi ‘Libiamo’ begins",
        "Waltz / party resumes", "Violetta: ‘È strano!’", "‘Sempre libera’ begins",
        "Act II pastoral prelude", "Germont enters", "Violetta: ‘Dite alla giovine’",
        "Germont: ‘Di Provenza il mar’", "Act II party chorus", "Alfredo’s denunciation",
        "Act III prelude", "Violetta: ‘Addio del passato’", "Alfredo returns / final duet",
    ],
    "bizet-carmen": [
        "Prelude opening tutti", "Children’s chorus enters", "Cigarette girls enter",
        "Carmen: Habanera begins", "Micaëla–Don José duet begins", "Carmen: Seguidilla begins",
        "Act II gypsy song begins", "Escamillo enters / Toreador song", "Don José’s flower song",
        "Act III prelude begins", "Card trio begins", "Micaëla: ‘Je dis que rien’",
        "Act IV procession begins", "Escamillo enters with Carmen", "Final Carmen–Don José confrontation",
    ],
    "mahler-symphony-no-2": [
        "I — opening tremolo and first low-string attack", "I — first full-orchestra statement",
        "I — lyrical second subject", "I — development return / tutti attack", "I — final coda attack",
        "II — Ländler opening", "II — contrasting string passage", "III — timpani stroke and scherzo opening",
        "III — brass-band interruption", "IV — ‘Urlicht’ alto entrance", "V — explosive opening",
        "V — offstage-band entrance", "V — great summons brass attack", "V — chorus entry ‘Aufersteh’n’",
        "V — final organ-and-orchestra peroration",
    ],
    "shostakovich-symphony-no-5": [
        "I — opening string canon", "I — first full-orchestra attack", "I — piano-led development entrance",
        "I — recapitulation return", "II — scherzo opening", "II — trio entrance",
        "II — scherzo return", "III — Largo string opening", "III — first woodwind solo",
        "III — central orchestral climax", "IV — finale opening brass attack", "IV — quieter contrasting theme",
        "IV — accelerating return", "IV — final broadening", "IV — last repeated tutti chords",
    ],
    "beethoven-piano-concerto-no-4": [
        "I — solo piano opening", "I — orchestra’s first entrance", "I — piano re-entry",
        "I — first strong tutti", "I — development entrance", "I — cadenza begins",
        "I — orchestra returns after cadenza", "II — stern orchestral opening", "II — piano’s first reply",
        "II — dialogue reaches its climax", "III — rondo theme begins", "III — piano takes the rondo theme",
        "III — contrasting episode", "III — cadenza begins", "III — final presto/coda attack",
    ],
    "beethoven-string-quartet-no-12": [
        "I — slow introduction begins", "I — Allegro main theme", "I — second theme",
        "I — development begins", "I — recapitulation return", "II — Adagio theme begins",
        "II — first variation", "II — faster variation", "II — quiet final variation",
        "III — Scherzando opening", "III — trio entrance", "III — scherzo return",
        "IV — finale opening", "IV — contrasting lyrical idea", "IV — final coda begins",
    ],
}


def norm(value: Optional[str]) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def inventory():
    records = {}
    with (ROOT / "data/inventory/broadcasts.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            records[row["id"]] = row
    return records


def result_counts():
    counts = Counter()
    for path in (ROOT / "data/research/results").glob("*.json"):
        data = load_json(path)
        for item in data.get("program", []):
            if item.get("composer") and item.get("work"):
                counts[(norm(item["composer"]), norm(item["work"]))] += 1
    for path in (ROOT / "data/crutchfield/results").glob("*.json"):
        parsed = load_json(path).get("parsed") or {}
        if parsed.get("composer") and parsed.get("work"):
            counts[(norm(parsed["composer"]), norm(parsed["work"]))] += 1
    return counts


def selected_candidates(composer, title, canonical_id, inv):
    wanted = (norm(composer), norm(title))
    found = []
    for path in (ROOT / "data/research/results").glob("*.json"):
        data = load_json(path)
        if data.get("id") not in inv or not (ROOT / "data/sections" / path.name).exists():
            continue
        programs = {(norm(x.get("composer")), norm(x.get("work"))) for x in data.get("program", [])}
        if wanted not in programs:
            continue
        sections = load_json(ROOT / "data/sections" / path.name)
        usable = target_sections(sections, canonical_id, data, title)
        inventory_paths = {track.get("path") for track in inv[data["id"]].get("tracks", [])}
        if not any(s.get("file") in inventory_paths for s in usable):
            continue
        descriptive = sum(bool(s.get("work_id")) and norm(s.get("title")) not in {"", "part 1", "part 2"}
                          for s in sections.get("sections", []))
        target_map = sum((norm(x.get("work")) == wanted[1]) for x in data.get("track_map", []))
        score = (len(programs) == 1, target_map, descriptive, -len(inv[data["id"]].get("tracks", [])))
        found.append((score, data, sections))
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def target_sections(section_doc, canonical_id, data=None, title=None):
    exact = [s for s in section_doc.get("sections", []) if s.get("work_id") == canonical_id]
    if exact:
        return exact
    # Older section files sometimes use a longer canonical spelling.
    tail = canonical_id.split("-", 1)[-1]
    partial = [s for s in section_doc.get("sections", []) if tail in (s.get("work_id") or "")]
    if partial:
        return partial
    if data and title:
        paths = {x.get("file") for x in data.get("track_map", []) if norm(x.get("work")) == norm(title)}
        if paths:
            return [s for s in section_doc.get("sections", []) if s.get("file") in paths]
        if len(data.get("program", [])) == 1:
            paths = {x.get("file") for x in data.get("track_map", []) if x.get("file")}
            return [s for s in section_doc.get("sections", []) if s.get("file") in paths]
    return []


def opera_landmarks(canonical_id, sections):
    useful = []
    rejected = {"announcer", "intermission", "applause", "other"}
    for section in sections:
        title = (section.get("title") or "").strip()
        simple = norm(title)
        if (not title or section.get("kind") in rejected or simple.startswith("part ") or
                "curtain call" in simple or "final applause" in simple):
            continue
        if simple not in {norm(x["label"]) for x in useful}:
            useful.append({"label": title, "source": "sections"})
    if len(useful) <= 15:
        chosen = useful
    else:
        chosen = [useful[round(i * (len(useful) - 1) / 14)] for i in range(15)]
    while len(chosen) < 15:
        chosen.append({"label": f"Distinct musical entrance {len(chosen) + 1}", "source": "proposal"})
    return chosen[:15]


def similarity(a, b):
    aa, bb = norm(a), norm(b)
    aset, bset = set(aa.split()), set(bb.split())
    jac = len(aset & bset) / max(1, len(aset | bset))
    return max(jac, SequenceMatcher(None, aa, bb).ratio())


def make_performance(data, section_doc, inv_row, canonical_id, title, landmarks):
    sections = target_sections(section_doc, canonical_id, data, title)
    wanted_paths = {s.get("file") for s in sections if s.get("file")}
    tracks = []
    for track in inv_row.get("tracks", []):
        if track.get("path") in wanted_paths:
            tracks.append({
                "path": track["path"], "duration_s": track.get("dur_s"),
                "label": next((s.get("title") for s in sections if s.get("file") == track["path"]), Path(track["path"]).name),
            })
    if not tracks:
        raise RuntimeError(f"No target tracks for {data['id']} / {canonical_id}")
    track_index = {track["path"]: i for i, track in enumerate(tracks)}
    suggestions = {}
    for n, landmark in enumerate(landmarks):
        candidates = [s for s in sections if s.get("file") in track_index and s.get("title")]
        best = max(candidates, key=lambda s: similarity(landmark["label"], s["title"]), default=None)
        if best and similarity(landmark["label"], best["title"]) >= 0.32:
            suggestions[landmark["id"]] = {"track_index": track_index[best["file"]], "time_s": best.get("start_s", 0)}
        else:
            suggestions[landmark["id"]] = {"track_index": min(len(tracks) - 1, n * len(tracks) // len(landmarks)), "time_s": 0}
    perf = data.get("performance") or {}
    return {
        "id": data["id"],
        "label": " — ".join(x for x in [perf.get("date"), perf.get("event_name") or data.get("summary", "")[:90]] if x) or data["id"],
        "tracks": tracks,
        "suggestions": suggestions,
    }


def main():
    inv = inventory()
    counts = result_counts()
    works = []
    for canonical_id, composer, title, genre in SELECTION:
        count = counts[(norm(composer), norm(title))]
        if count < 3:
            raise RuntimeError(f"Only {count} result performances for {composer}: {title}")
        candidates = selected_candidates(composer, title, canonical_id, inv)
        if len(candidates) < 3:
            raise RuntimeError(f"Only {len(candidates)} playable sectioned performances for {composer}: {title}")
        exemplar_sections = target_sections(candidates[0][2], canonical_id, candidates[0][1], title)
        proposed = ([{"label": label, "source": "research track_map/sections + landmark proposal"} for label in TEMPLATES[canonical_id]]
                    if canonical_id in TEMPLATES else opera_landmarks(canonical_id, exemplar_sections))
        landmarks = [{"id": f"lm-{i + 1:02d}", **item, "skipped": False} for i, item in enumerate(proposed)]
        performances = [make_performance(data, sections, inv[data["id"]], canonical_id, title, landmarks)
                        for _, data, sections in candidates[:3]]
        works.append({
            "id": canonical_id, "composer": composer, "title": title, "genre": genre,
            "result_performance_count": count, "landmarks": landmarks, "performances": performances,
        })
    plan = {
        "schema": "sethster.gt-c-plan/1", "passes_required": 2, "calibration_seconds": 120,
        "calibration_bpm": 90, "target_minutes": 60, "works": works,
        "selection_note": "No named evaluation selection was present in alignment-v2.md; selected from works with >=3 combined research/Crutchfield result records and >=3 inventory-backed sectioned broadcasts.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}: {len(works)} works, {sum(len(w['performances']) for w in works)} performances")


if __name__ == "__main__":
    main()
