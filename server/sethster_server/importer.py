"""Build data/sethster.db from the research results (+ dossiers, + optional wiki enrichment).

    uv run python -m sethster_server.importer [--db PATH] [--no-media] [--no-artwork] [--no-cds] [--only data/release/ready.txt]

Covers come only from a recording's own folders (never a collection folder shared with other broadcasts), never
from desktop screenshots or images beyond 2:1. Dead links (data/links/status.json, pipeline/links.py) are swapped
for their Wayback copy, or unlinked (sources) / dropped (resources) when there is none.

Crutchfield CDs (cd.py) come in alongside the broadcasts once they are ready for the app (cd_ready: harvested,
matched on MusicBrainz, art decided), whether or not --only lists them; --no-cds leaves them out. A CD with a
research result of its own uses that instead. CD covers come from the CD art manifest (data/media/cd/<id>.json).

The DB is rebuilt from scratch into a temp file and atomically swapped in. "Ask Dad" answers from
the previous DB are carried over, so re-importing never loses them.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import cd as cdmod
from . import db as dbmod
from .config import Settings, load_settings
from .media import find_artwork, is_audio, needs_transcode
from .text import (clean_track_title, fold, infer_segment, is_ensemble, norm_key, slugify, sort_name,
                   surname)
from .wiki import load_index, load_wiki

log = logging.getLogger("sethster.importer")

STAFF_ROLE = re.compile(
    r"conduct|orchestra|chorus|choir|master|director|design|production|producer|announc|host|presenter|"
    r"interview|commentat|panel|quiz|dramaturg|concertmaster|leader|lighting|costume|\bset\b|stage|"
    r"engineer|choreograph|video|répétiteur|repetiteur|prompter|dancer|translator|surtitle|librettist|"
    r"continuity|editor|recording|remaster|annotator|critic|reviewer|organizer|curator|introduc",
    re.I,
)
GENERIC_WORK = re.compile(
    r"^(symphon\w*|sinfonia|piano|violin|cello|viola|flute|oboe|clarinet|horn|trumpet|organ|harpsichord|keyboard|"
    r"concerto|sonata|string|quartet|quintet|sextet|octet|trio|mass|missa|requiem|suite|overture|serenade|"
    r"variations|nocturne|etude|étude|prelude|cantata|oratorio|te deum|stabat|magnificat|vespers|no\.|op\.)\b",
    re.I,
)
_ARRANGER_NOTE = re.compile(r"\b(arr\.|arranged|transcr\.?|transcribed|orch\.|orchestrated|attributed|attr\.)", re.I)
CONDUCTOR_ROLE = re.compile(r"^(principal |guest |music(al)? director and )?conductor\b|^conductor", re.I)


# ---- loading -----------------------------------------------------------------------------------------

def _read_many(paths: list[Path], workers: int = 32) -> dict[Path, bytes | None]:
    """Read many small files in parallel. data/ may live in iCloud-synced ~/Documents, where evicted
    ("dataless") files take seconds each to fetch; parallel reads hide most of that latency."""
    from concurrent.futures import ThreadPoolExecutor

    def rd(p: Path):
        try:
            return p.read_bytes()
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(zip(paths, ex.map(rd, paths)))


def read_id_list(path: Path) -> set[str]:
    """Ids from a list file (one per line; blank lines and '#' comments ignored), e.g. data/release/ready.txt."""
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.add(line.split()[0])
    return ids


def load_results(result_dirs: list[Path], only: set[str] | None = None) -> "OrderedDict[str, tuple[dict, dict | None, Path]]":
    """{id: (result, dossier, file)}. With `only`, just those ids (files are named <id>.json; the others are
    not even read, which matters when iCloud has evicted them)."""
    out: OrderedDict[str, tuple[dict, dict | None, Path]] = OrderedDict()
    files: list[Path] = []
    for d in result_dirs:
        if d.is_dir():
            files += sorted(f for f in d.glob("*.json") if only is None or f.stem in only)
    dossier_paths = [f.parent.parent / "dossiers" / f.name for f in files]
    blobs = _read_many(files + [p for p in dossier_paths if p.exists()])
    for f, dp in zip(files, dossier_paths):
        try:
            r = json.loads(blobs.get(f) or b"")
        except Exception as e:  # noqa: BLE001
            log.warning("skipping unreadable %s: %s", f, e)
            continue
        if not isinstance(r, dict) or "id" not in r or "track_map" not in r:
            log.warning("skipping %s: not a performance result", f)
            continue
        if only is not None and r["id"] not in only:
            continue
        dossier = None
        if dp.name != f"{r['id']}.json":
            dp = dp.with_name(f"{r['id']}.json")
            if dp.exists():
                blobs[dp] = _read_many([dp])[dp]
        if blobs.get(dp) is not None:
            try:
                dossier = json.loads(blobs[dp])
            except Exception as e:  # noqa: BLE001
                log.warning("unreadable dossier %s: %s", dp, e)
        out[r["id"]] = (r, dossier, f)  # later dirs (research) override earlier (pilot)
    return out


def track_listing(data_dir: Path, pid: str, dossier: dict | None) -> dict:
    """{"folders": [...], "tracks": [{path, size, dur_s...}]} for one recording: from its dossier, else (or for
    whatever the dossier lacks) from its inventory record (data/inventory/records/<id>.json)."""
    d = dossier if isinstance(dossier, dict) else {}
    folders = d.get("folders") or ([d["folder"]] if d.get("folder") else [])
    tracks = d.get("tracks") or []
    if not folders or not tracks:
        try:
            rec = json.loads((data_dir / "inventory" / "records" / f"{pid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = {}
        if isinstance(rec, dict):
            folders = folders or rec.get("folders") or []
            tracks = tracks or rec.get("tracks") or []
    return {"folders": [f.strip("/") for f in folders if isinstance(f, str) and f.strip("/")],
            "tracks": [t for t in tracks if isinstance(t, dict) and isinstance(t.get("path"), str)]}


def make_resolver(listing: list[dict], folders: list[str]):
    """resolve(file named in the notes) -> (share path, its listing entry or None).

    Research often names a track by a shortened file name ("Track 01.mp3" for
    ".../070630 Death in Venice ENO - Track 01.mp3"), so after an exact or folder-relative match it accepts the
    one listed file whose name is the same, then the one whose name ends in it (after a space, dash, underscore
    or dot). Anything ambiguous stays as the notes gave it."""
    by_path = {t["path"]: t for t in listing}
    by_base: dict[str, list[dict]] = defaultdict(list)
    for t in listing:
        by_base[PurePosixPath(t["path"]).name.lower()].append(t)

    def resolve(f: str) -> tuple[str, dict | None]:
        if f in by_path:
            return f, by_path[f]
        for folder in folders:
            if f"{folder}/{f}" in by_path:
                return f"{folder}/{f}", by_path[f"{folder}/{f}"]
        name = PurePosixPath(f).name.lower()
        cands = by_base.get(name, [])
        if not cands and name:
            cands = [t for b, ts in by_base.items() if b.endswith(name) and b[-len(name) - 1] in " -_." for t in ts]
        if len(cands) == 1:
            return cands[0]["path"], cands[0]
        if "/" not in f and len(folders) == 1:
            return f"{folders[0]}/{f}", None
        return f, None

    return resolve


def load_folder_owners(data_dir: Path) -> tuple[dict[str, set[str]], dict[str, str]]:
    """({share folder: ids of the recordings that play from it}, {duplicate id: original id}) for every
    recording in the inventory. A duplicate counts as its original."""
    owners: dict[str, set[str]] = defaultdict(set)
    canon: dict[str, str] = {}
    p = data_dir / "inventory" / "broadcasts.jsonl"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        log.warning("no inventory (%s): folder ownership from the notes alone", e)
        return owners, canon
    for line in lines:
        try:
            b = json.loads(line)
        except ValueError:
            continue
        if not isinstance(b, dict) or not b.get("id"):
            continue
        if b.get("duplicate_of_performance"):
            canon[b["id"]] = b["duplicate_of_performance"]
        for f in b.get("folders") or []:
            if isinstance(f, str) and f.strip("/"):
                owners[f.strip("/")].add(canon.get(b["id"], b["id"]))
    if not owners:
        log.warning("inventory %s is empty: folder ownership from the notes alone", p)
    return owners, canon


def owns(pid: str, path: str, owners: dict[str, set[str]]) -> bool:
    """Does this share path lie in a folder that is pid's alone? The nearest folder above it that any recording
    plays from must belong to pid only, and a collection's top folder ('BBC Proms') never counts: loose
    pictures there belong to whichever of its many broadcasts they were saved for."""
    parts = PurePosixPath(path.strip("/")).parent.parts
    for i in range(len(parts), 1, -1):
        d = "/".join(parts[:i])
        if d in owners:
            return owners[d] == {pid}
    return False


def load_nas_scans(data_dir: Path, ids: list[str]) -> dict[str, dict]:
    """{id: data/media/nas/<id>.json} (pipeline/media/nas.py: every picture in the recording's folders, with
    pixel sizes). Missing or unreadable scans are left out, and those recordings fall back to the share."""
    paths = {pid: data_dir / "media" / "nas" / f"{pid}.json" for pid in ids}
    blobs = _read_many([p for p in paths.values() if p.exists()])
    out = {}
    for pid, p in paths.items():
        try:
            d = json.loads(blobs.get(p) or b"")
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("images"), list) and isinstance(d.get("folders"), list):
            out[pid] = d
    return out


def load_link_status(data_dir: Path) -> dict[str, dict]:
    """data/links/status.json (pipeline/links.py): {url: {"state": "ok"|"dead"|..., "wayback": url|None}}."""
    p = data_dir / "links" / "status.json"
    try:
        d = json.loads(p.read_bytes())
    except (OSError, ValueError) as e:
        if p.exists():
            log.warning("unreadable %s: %s; links left as cited", p, e)
        return {}
    links = d.get("links") if isinstance(d, dict) else None
    return links if isinstance(links, dict) else {}


def fix_links(items, status: dict[str, dict], drop_dead: bool, counts: Counter) -> list:
    """Point dead links at their Wayback copy (the item's own wayback_url, else the one pipeline/links.py found).
    A dead source with no copy keeps its title with url None; a dead resource with no copy is dropped (the app
    needs a link for a resource)."""
    out = []
    for it in items if isinstance(items, list) else []:
        url = it.get("url") if isinstance(it, dict) else None
        st = status.get(url.strip()) if isinstance(url, str) else None
        if not isinstance(st, dict) or st.get("state") != "dead":
            out.append(it)
            continue
        wb = it.get("wayback_url") or st.get("wayback")
        if wb:
            out.append({**it, "url": wb, "wayback_url": wb, "dead_url": url})
            counts["archived"] += 1
        elif drop_dead:
            counts["dropped"] += 1
        else:
            out.append({**it, "url": None, "dead_url": url})
            counts["unlinked"] += 1
    return out


# ---- entity registries ------------------------------------------------------------------------------

@dataclass
class Ent:
    key: str
    qid: str | None = None
    names: Counter = field(default_factory=Counter)
    extra: dict = field(default_factory=dict)
    id: str = ""

    def best_name(self, longest: bool = False) -> str:
        if longest:
            return max(self.names, key=lambda n: (len(n), self.names[n]))
        return max(self.names, key=lambda n: (self.names[n], len(n)))


class Registry:
    def __init__(self):
        self.ents: OrderedDict[str, Ent] = OrderedDict()
        self.name_qids: dict[str, set[str]] = defaultdict(set)

    def learn(self, name_key: str, qid: str | None):
        if qid:
            self.name_qids[name_key].add(qid)

    def key_for(self, name_key: str, qid: str | None) -> str:
        if qid:
            return qid
        qs = self.name_qids.get(name_key)
        if qs and len(qs) == 1:
            return next(iter(qs))
        return "n:" + name_key

    def get(self, key: str, qid: str | None) -> Ent:
        e = self.ents.get(key)
        if e is None:
            e = self.ents[key] = Ent(key=key, qid=qid if qid else (key if re.match(r"^Q\d+$", key) else None))
        return e

    def assign_ids(self, base_fn) -> None:
        used: set[str] = set()
        for e in self.ents.values():
            base = base_fn(e)
            slug = base
            if slug in used:
                slug = f"{base}-{e.qid.lower()}" if e.qid else base
                n = 2
                while slug in used:
                    slug = f"{base}-{n}"
                    n += 1
            used.add(slug)
            e.id = slug


def _qid(v) -> str | None:
    if isinstance(v, str) and re.match(r"^Q\d+$", v.strip()):
        return v.strip()
    return None


def short_venue(name: str) -> str:
    first = name.split(",")[0].strip()
    if re.match(r"^(gro(ß|ss)er|kleiner|main|great|large|small|grand)\s+(saal|hall|auditorium)$", first, re.I):
        return name.strip()
    if re.match(r"^radio|^broadcast|compiled", first, re.I):
        return ""
    return first


# Same building under another name: keyed to one entity (the most-used name is displayed).
VENUE_ALIASES = {
    "david geffen hall": "avery fisher hall",
    "marian anderson hall": "verizon hall",
    "symphony center": "orchestra hall",
    "orchestra hall at symphony center": "orchestra hall",
    "orchestra hall symphony center": "orchestra hall",
    "symphony center orchestra hall": "orchestra hall",
    "the barbican hall": "barbican hall",
    "musikverein wien": "musikverein",
    "grosser saal stiftung mozarteum": "mozarteum",
}


def strip_arranger(name: str) -> str:
    """'Richard Wagner (arr. Lorin Maazel)' / 'Bach, arr. Myra Hess' -> the composer alone."""
    return re.sub(r"\s*(?:[,(]\s*|\s+)(?:arr\.|arranged\b|orch\.|orchestrated\b|transcr\.).*$", "", name, flags=re.I).strip() or name


def canonical_venue(name: str) -> str:
    """'Unknown (probably Musikverein' -> 'Musikverein'; 'Verizon Hall (now Marian Anderson Hall)' -> 'Verizon Hall'.
    Ambiguous hedges ('candidates: A; B', 'A or B') stay as written."""
    n = name.strip()
    m = re.match(r"^unknown[^(;]*[(;]\s*(?:probably|likely|presumably)\s+(?:the\s+)?(.+?)\)?$", n, re.I)
    if m and not re.search(r"\bor\b|candidates", m.group(1), re.I):
        n = m.group(1)
    n = re.sub(r"\s*\((?:presumed|inferred|now|renamed|exact house|unverified)[^)]*\)?\s*$", "", n, flags=re.I)
    n = re.sub(r"\s*\((?:presumed|inferred)\)\s*$", "", n, flags=re.I)
    return n.strip() or name.strip()


def venue_group(name: str, city: str | None) -> str:
    k = norm_key(name)
    k = VENUE_ALIASES.get(k, k)
    c = norm_key(re.sub(r"\(.*", "", (city or "").split(",")[0]))
    c = "" if c.startswith("unknown") else c
    return f"{k}|{c}"


def merge_registry(reg: "Registry", group_fn) -> dict[str, str]:
    """Fold entities that group_fn says are the same into one; the QID'd, most-used one survives.
    Returns {dropped key: surviving key}."""
    groups: dict[str, list[Ent]] = defaultdict(list)
    for e in reg.ents.values():
        g = group_fn(e)
        if g:
            groups[g].append(e)
    remap: dict[str, str] = {}
    for es in groups.values():
        if len(es) < 2:
            continue
        es.sort(key=lambda e: (e.qid is None, -sum(e.names.values())))
        keep = es[0]
        for e in es[1:]:
            keep.names.update(e.names)
            for k, v in e.extra.items():
                if isinstance(v, Counter):
                    keep.extra.setdefault(k, Counter()).update(v)
                else:
                    keep.extra.setdefault(k, v)
            remap[e.key] = keep.key
            del reg.ents[e.key]
    return remap


def match_work(s: str | None, prog: list[tuple[str, str]]) -> int | None:
    """Match a free-text work label ('Bruckner: Symphony No. 6', 'Norma (Bellini)') to a program index."""
    if not s or not prog:
        return None
    if len(prog) == 1:
        return 0
    ns = norm_key(s)
    st = set(ns.split())
    best, best_score = None, 0.0
    for i, (title, comp) in enumerate(prog):
        nt = norm_key(title)
        sn = norm_key(surname(comp, particles=False))
        if ns in (nt, f"{sn} {nt}", f"{nt} {sn}"):
            return i
        tt = set(nt.split()) - {"in", "the", "of", "a", "and", "no", "op", "excerpt", "complete"}
        score = len(st & tt) / max(1, len(tt))
        if sn and sn in st:
            score += 0.35
        if nt and nt in ns:
            score += 0.5
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= 0.5 else None


def drop_mismatched_composer_qids(results) -> int:
    """Research sometimes pastes one composer's QID onto another (Q254, Mozart, on a Brahms work), and merging
    on it would file the work under the wrong composer. A QID is kept only where the composer's surname matches
    the surname most results give it."""
    def sk(p) -> str:
        return norm_key(surname(strip_arranger((p.get("composer") or "").strip()), particles=False))
    votes: dict[str, Counter] = defaultdict(Counter)
    for r, _, _ in results.values():
        for p in r.get("program", []):
            if _qid(p.get("composer_wikidata")) and sk(p):
                votes[_qid(p.get("composer_wikidata"))][sk(p)] += 1
    n = 0
    for r, _, _ in results.values():
        for p in r.get("program", []):
            q = _qid(p.get("composer_wikidata"))
            if q and sk(p) != votes[q].most_common(1)[0][0]:
                p["composer_wikidata"] = None
                n += 1
    return n


def apply_wiki_index(results, index: dict) -> int:
    """Apply data/wiki/index.json per-performance QID verdicts to the (in-memory) results.

    verified/matched rows set the QID, rejected rows drop the research QID. Work QIDs are only taken
    when work_scope == 'exact' (a 'parent' QID would merge an excerpt into the whole work).
    """
    by_perf = (index or {}).get("by_performance") or {}
    n = 0

    def verdict(obj: dict, key: str, qid, status) -> None:
        nonlocal n
        if status in ("verified", "matched") and _qid(qid):
            if obj.get(key) != qid:
                obj[key] = qid
                n += 1
        elif status == "rejected" and obj.get(key):
            obj.pop(key, None)
            n += 1

    for pid, (r, _, _) in results.items():
        e = by_perf.get(pid)
        if not isinstance(e, dict):
            continue
        credits = r.get("credits") or []
        for row in e.get("credits") or []:
            i = row.get("index") if isinstance(row, dict) else None
            if isinstance(i, int) and 0 <= i < len(credits) and norm_key(credits[i].get("name")) == norm_key(row.get("name")):
                verdict(credits[i], "wikidata", row.get("qid"), row.get("status"))
        program = r.get("program") or []
        for row in e.get("program") or []:
            i = row.get("index") if isinstance(row, dict) else None
            if not (isinstance(i, int) and 0 <= i < len(program)):
                continue
            p = program[i]
            if norm_key(p.get("composer")) == norm_key(row.get("composer")):
                verdict(p, "composer_wikidata", row.get("composer_qid"), row.get("composer_status"))
            if norm_key(p.get("work")) == norm_key(row.get("work")):
                if row.get("work_scope", "exact") == "exact" or row.get("work_status") == "rejected":
                    verdict(p, "work_wikidata", row.get("work_qid"), row.get("work_status"))
        v = e.get("venue")
        if isinstance(v, dict) and r.get("performance"):
            verdict(r["performance"], "venue_wikidata", v.get("qid"), v.get("status"))
    return n


# ---- derived fields ------------------------------------------------------------------------------------

def derive_title(r: dict, prog: list[tuple[str, str]], music_secs: dict[int, float]) -> str:
    if r.get("title"):
        return r["title"]
    event = (r.get("performance") or {}).get("event_name") or ""
    if not prog:
        return event or r["id"]
    if len(prog) == 1:
        title, comp = prog[0]
        if GENERIC_WORK.match(title):
            return f"{surname(comp, particles=False)}: {title}"
        return title
    main = max(range(len(prog)), key=lambda i: (music_secs.get(i, 0.0), i))
    title, comp = prog[main]
    sn = surname(comp, particles=False)
    m = re.match(r"^symphony no\.?\s*(\d+)", title, re.I)
    short = f"{sn} {m.group(1)}" if m else f"{sn} {re.split(r'[,(]', title)[0].strip()}"
    pm = re.search(r"\bProm (\d+)\b", event)
    if pm:
        return f"Prom {pm.group(1)}: {short}"
    quoted = re.findall(r"'([^']{6,60})'", event)
    if len(prog) >= 5 and quoted:
        return quoted[0]
    return f"{sn}: {title}"


def derive_subtitle(credits: list[dict], kinds: dict[str, str]) -> str | None:
    """2-4 headline performers: up to 3 principal singers/soloists, then the conductor after a '·'."""
    performers: list[str] = []
    tentative: list[str] = []
    seen: set[str] = set()
    conductor = None
    ensemble = None
    for c in credits:
        if c["status"] == "production_only":
            continue
        role = c["role"] or ""
        pid = c["person_key"]
        if conductor is None and CONDUCTOR_ROLE.search(role.strip()):
            conductor = c["name"]
            continue
        if kinds.get(pid) == "ensemble":
            if ensemble is None and re.search(r"orchestra|ensemble|band", role, re.I):
                ensemble = c["name"]
            continue
        if STAFF_ROLE.search(role) or pid in seen:
            continue
        seen.add(pid)
        (performers if c["status"] == "confirmed" else tentative).append(surname(c["name"]))
    performers = (performers or tentative)[:3]
    parts = []
    if performers:
        parts.append(", ".join(performers))
    elif ensemble:
        parts.append(ensemble)
    if conductor and surname(conductor) not in performers:
        parts.append(surname(conductor))
    return " · ".join(parts) or None


# ---- main build -------------------------------------------------------------------------------------------

def _credit_json(w: dict) -> str | None:
    c = w.get("image_credit")
    if not c:
        return None
    return json.dumps(c if isinstance(c, dict) else {"credit": str(c)}, ensure_ascii=False)


def build(settings: Settings, db_path: Path | None = None, use_media: bool = True,
          only: set[str] | None = None, artwork: bool = True, cds: bool = True) -> dict:
    """Build the DB. `only`: just these performance ids (the release list); the rest are left out entirely.
    `use_media=False` never reads the share (covers from the NAS photo scan and dossier sidecars only);
    `artwork=False` picks no covers at all (a working DB for the splitter, never shown in the app).
    `cds=False` leaves out the Crutchfield CDs that are ready for the app (they ignore `only`)."""
    t0 = time.time()
    db_path = db_path or settings.db_path
    results = load_results(settings.result_dirs, only)
    if only is not None:
        log.info("--only: %d of %d listed ids have notes", len(results), len(only))
    cd_art: dict[str, dict] = {}  # CD id -> its art manifest (data/media/cd/<id>.json)
    n_cds = 0
    if cds:
        for cpid, (cres, clisting, cmanifest) in cdmod.load_ready(settings.data_dir, skip=set(results)).items():
            results[cpid] = (cres, clisting, settings.data_dir.joinpath("crutchfield", "results", f"{cpid}.json"))
            cd_art[cpid] = cmanifest
            n_cds += 1
    for cpid in results:
        if cpid not in cd_art and cpid.startswith("cf-"):  # a researched CD: its cover still comes from the manifest
            m = cdmod.load_manifest(settings.data_dir, cpid)
            if m is not None:
                cd_art[cpid] = m
    wiki = load_wiki(settings.wiki_dir)
    media_root = settings.media_root if use_media and settings.media_root and settings.media_root.is_dir() else None
    if artwork and use_media and not media_root:
        log.warning("MEDIA_ROOT not available (%s); artwork from the NAS photo scan and dossier sidecars only",
                    settings.media_root)
    owners, canon = load_folder_owners(settings.data_dir) if artwork else ({}, {})
    nas_scans = load_nas_scans(settings.data_dir, list(results)) if artwork else {}
    link_status = load_link_status(settings.data_dir)
    link_counts: Counter = Counter()

    n_fixed = apply_wiki_index(results, load_index(settings.wiki_dir))
    if n_fixed:
        log.info("wiki index: applied %d QID corrections/matches", n_fixed)
    n_dropped = drop_mismatched_composer_qids(results)
    if n_dropped:
        log.info("dropped %d composer QIDs that belong to another composer", n_dropped)
    composers, works, people, venues = Registry(), Registry(), Registry(), Registry()

    # pass 1: learn name -> QID so un-QID'd mentions merge with QID'd ones
    initials: dict[tuple[str, str], set[str]] = defaultdict(set)  # (surname key, QID) -> first initials seen
    for r, _, _ in results.values():
        for p in r.get("program", []):
            ck = norm_key(p.get("composer"))
            composers.learn(ck, _qid(p.get("composer_wikidata")))
            sk = norm_key(surname(p.get("composer") or "", particles=False))
            composers.learn(sk, _qid(p.get("composer_wikidata")))
            if _qid(p.get("composer_wikidata")):
                initials[(sk, _qid(p.get("composer_wikidata")))].add(ck[:1] if " " in ck else "")
        for c in r.get("credits", []):
            people.learn(norm_key(c.get("name")), _qid(c.get("wikidata")))
        perf = r.get("performance") or {}
        venues.learn(norm_key(canonical_venue(short_venue(perf.get("venue") or ""))), _qid(perf.get("venue_wikidata")))

    perf_rows = []
    for pid, (r, dossier, src_file) in results.items():
        perf = r.get("performance") or {}
        # venue
        vq = _qid(perf.get("venue_wikidata"))
        vname = short_venue(perf.get("venue") or "")
        vname = canonical_venue(vname) if vname else vname
        venue_ent = None
        if vname or vq:
            vk = venues.key_for(norm_key(vname) + ("" if vq else "|" + norm_key(perf.get("city"))), vq)
            venue_ent = venues.get(vk, vq)
            if vname:
                venue_ent.names[vname] += 1
            venue_ent.extra.setdefault("cities", Counter())[perf.get("city") or ""] += 1
            venue_ent.extra.setdefault("full", perf.get("venue"))

        # program
        prog_entries = []
        for p in r.get("program", []):
            cname = strip_arranger((p.get("composer") or "Unknown").strip())
            cq = _qid(p.get("composer_wikidata"))
            ck = composers.key_for(norm_key(cname), cq)
            if not cq and ck.startswith("n:"):
                # a bare surname, or a full name whose first initial fits the QID'd composer of that surname
                # ("W. A. Mozart" -> Mozart; never "Richard Strauss" -> a Johann Strauss II)
                sk = norm_key(surname(cname, particles=False))
                qs = composers.name_qids.get(sk)
                fn = norm_key(cname)
                if qs and len(qs) == 1 and (" " not in fn or fn[:1] in initials[(sk, next(iter(qs)))]
                                            or "" in initials[(sk, next(iter(qs)))]):
                    ck = composers.key_for(sk, None)
            cent = composers.get(ck, cq)
            cent.names[cname] += 1
            title = (p.get("work") or "Untitled").strip()
            wq = _qid(p.get("work_wikidata"))
            wk = wq or f"w:{ck}|{norm_key(title)}"
            went = works.get(wk, wq)
            went.names[title] += 1
            went.extra["composer_key"] = cent.key
            if p.get("catalogue"):
                went.extra.setdefault("catalogue", p["catalogue"])
            prog_entries.append({"title": title, "composer": cname, "composer_key": cent.key, "work_key": went.key,
                                 "catalogue": p.get("catalogue") or None, "notes": p.get("notes") or None})
        prog_pairs = [(e["title"], e["composer"]) for e in prog_entries]

        # credits
        credits = []
        for c in r.get("credits", []):
            name = (c.get("name") or "").strip()
            if not name:
                continue
            q = _qid(c.get("wikidata"))
            pk = people.key_for(norm_key(name), q)
            pent = people.get(pk, q)
            pent.names[name] += 1
            pent.extra.setdefault("kinds", Counter())["ensemble" if is_ensemble(name, c.get("role") or "") else "person"] += 1
            wi = match_work(c.get("work"), prog_pairs)
            credits.append({"name": name, "person_key": pent.key, "role": (c.get("role") or "").strip(),
                            "work_key": prog_entries[wi]["work_key"] if wi is not None else None,
                            "status": c.get("status") or "inferred", "notes": c.get("notes") or None,
                            "source_ids": c.get("source_ids") or []})

        # tracks
        listing = track_listing(settings.data_dir, pid, dossier)
        folders = listing["folders"]
        folder = folders[0] if len(folders) == 1 else None
        resolve = make_resolver(listing["tracks"], folders)

        tm = r.get("track_map", [])
        dup_alts: dict[str, list[str]] = defaultdict(list)  # primary path -> duplicate paths
        for t in tm:
            if t.get("duplicate_of"):
                dpath, _ = resolve(t["file"])
                ppath, _ = resolve(t["duplicate_of"])
                dup_alts[ppath].append(dpath)
        tracks = []
        music_secs: dict[int, float] = defaultdict(float)
        for t in tm:
            if t.get("duplicate_of") or not is_audio(t.get("file", "")):
                continue
            path, info = resolve(t["file"])
            stream_path = path
            if needs_transcode(path):
                for alt in dup_alts.get(path, []):
                    if not needs_transcode(alt):
                        stream_path = alt
                        break
            content = t.get("content") or ""
            seg = infer_segment(content)
            wi = match_work(t.get("work"), prog_pairs) if t.get("work") else None
            if seg in ("intermission", "interview") and len(prog_pairs) <= 1:
                wi = None
            dur = (info or {}).get("duration_s") or (info or {}).get("dur_s")  # dossiers say dur_s
            if wi is not None and seg == "music" and dur:
                music_secs[wi] += dur
            tracks.append({"path": path, "stream_path": stream_path, "content": content,
                           "title": clean_track_title(content), "segment": seg,
                           "work_key": prog_entries[wi]["work_key"] if wi is not None else None,
                           "duration_s": dur, "size": (info or {}).get("size"),
                           "format": PurePosixPath(path).suffix.lower().lstrip(".")})

        dirs = list(OrderedDict.fromkeys(str(PurePosixPath(t["path"]).parent) for t in tracks))
        if folder and folder not in dirs:
            dirs.append(folder)
        if not folder:
            folder = os.path.commonpath(dirs) if dirs else None
        hints = [(s["path"], s.get("size") or 0) for s in (dossier or {}).get("sidecars", [])
                 if isinstance(s, dict) and isinstance(s.get("path"), str)]
        collection = (folder or (tracks[0]["path"] if tracks else "")).split("/")[0] or None
        if artwork and pid not in cd_art:
            for d in dirs:  # the folders these notes play from count too (a recording missing from the inventory)
                if d.strip("/"):
                    owners[d.strip("/")].add(canon.get(pid, pid))

        perf_rows.append({"r": r, "perf": perf, "venue": venue_ent, "program": prog_entries, "credits": credits,
                          "tracks": tracks, "music_secs": music_secs, "folder": folder, "dirs": dirs,
                          "hints": hints, "artwork": None, "artwork_meta": None, "collection": collection,
                          "src": src_file})

    # ---- cover pictures: only from the recording's own folders --------------------------------------------
    # (ownership needs every recording's folders, so this runs once they are all known)
    for row in perf_rows if artwork else ():
        pid = row["r"]["id"]
        if pid in cd_art:  # CDs: the picture the art stage chose (a thumbnail under data/media/cd), never the share
            row["artwork"] = cdmod.art_file(settings.data_dir, cd_art[pid], "front")
            row["artwork_meta"] = cdmod.art_meta(cd_art[pid]) if row["artwork"] else None
            continue
        cpid = canon.get(pid, pid)
        allow = lambda path, cpid=cpid: owns(cpid, path, owners)  # noqa: E731
        scan = nas_scans.get(pid)
        scanned = [f.strip("/") for f in (scan or {}).get("folders") or [] if isinstance(f, str)]
        # list on the share only own folders the NAS scan did not cover (the share is slow, and can hang)
        todo = [d for d in row["dirs"] if allow(f"{d}/x")
                and not any(d.strip("/") == f or d.strip("/").startswith(f + "/") for f in scanned)]
        row["artwork"] = find_artwork(media_root, todo, row["hints"], (scan or {}).get("images"), allow)

    # ---- merge duplicates (same name under different/missing QIDs, renamed venues) -------------------------
    # Unknown/empty names never merge. People with two different QIDs and the same name are kept apart.
    def _named(e: Ent, longest: bool = False) -> str:
        k = norm_key(e.best_name(longest)) if e.names else ""
        return "" if k in ("", "unknown") else k
    p_qids: dict[str, set] = defaultdict(set)
    for e in people.ents.values():
        if e.qid:
            p_qids[_named(e)].add(e.qid)
    p_map = merge_registry(people, lambda e: _named(e) if len(p_qids[_named(e)]) <= 1 else
                           (_named(e) + "|" + (e.qid or "")) if e.qid else "")
    c_map = merge_registry(composers, lambda e: _named(e, longest=True))
    for e in works.ents.values():
        e.extra["composer_key"] = c_map.get(e.extra["composer_key"], e.extra["composer_key"])
    w_map = merge_registry(works, lambda e: e.extra["composer_key"] + "|" + _named(e) if _named(e) else "")
    v_map = merge_registry(venues, lambda e: venue_group(e.best_name(), e.extra.get("cities", Counter()).most_common(1)[0][0]
                                                          if e.extra.get("cities") else "") if _named(e) else "")
    for row in perf_rows:
        for c in row["credits"]:
            c["person_key"] = p_map.get(c["person_key"], c["person_key"])
            if c["work_key"]:
                c["work_key"] = w_map.get(c["work_key"], c["work_key"])
        for pe in row["program"]:
            pe["composer_key"] = c_map.get(pe["composer_key"], pe["composer_key"])
            pe["work_key"] = w_map.get(pe["work_key"], pe["work_key"])
        for t in row["tracks"]:
            if t["work_key"]:
                t["work_key"] = w_map.get(t["work_key"], t["work_key"])
        if row["venue"] is not None and row["venue"].key in v_map:
            row["venue"] = venues.ents[v_map[row["venue"].key]]
    log.info("merged duplicates: %d people, %d composers, %d works, %d venues",
             len(p_map), len(c_map), len(w_map), len(v_map))

    # ---- names, kinds, ids --------------------------------------------------------------------------------
    for e in people.ents.values():
        kinds = e.extra.get("kinds", Counter())
        e.extra["kind"] = "ensemble" if kinds["ensemble"] > kinds["person"] else "person"
        e.extra["name"] = e.best_name()
    for e in composers.ents.values():
        # longest full name, but never an arranger/attribution variant ("Ludwig van Beethoven, arr. Franz
        # Liszt" would otherwise name Beethoven everywhere and turn his work slugs into liszt-*)
        plain = {n: c for n, c in e.names.items() if not _ARRANGER_NOTE.search(n)}
        e.extra["name"] = max(plain, key=lambda n: (len(n), plain[n])) if plain else e.best_name(longest=True)
    for e in works.ents.values():
        e.extra["name"] = e.best_name()
    for e in venues.ents.values():
        w = wiki.get(e.qid or "", {})
        e.extra["name"] = (min((n for n in e.names if e.names[n] == max(e.names.values())), key=len)
                           if e.names else (w.get("label") or e.extra.get("full") or "Unknown venue"))
        cities = e.extra.get("cities", Counter())
        e.extra["city"] = cities.most_common(1)[0][0] if cities else None

    people.assign_ids(lambda e: slugify(e.extra["name"]))
    composers.assign_ids(lambda e: slugify(e.extra["name"]))
    works.assign_ids(lambda e: slugify(
        f"{surname(composers.ents[e.extra['composer_key']].extra['name'], particles=False)} {e.extra['name']}"))
    venues.assign_ids(lambda e: slugify(e.extra["name"]))

    # ---- write -------------------------------------------------------------------------------------------------
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(f"{db_path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    conn = dbmod.connect(tmp)
    dbmod.init(conn)
    def wk(qid):
        return wiki.get(qid or "", {})

    for e in venues.ents.values():
        w = wk(e.qid)
        conn.execute("INSERT INTO venues VALUES (?,?,?,?,?,?)",
                     (e.id, e.extra["name"], e.extra["city"], e.qid, w.get("description"), w.get("wikipedia_url")))
    for e in composers.ents.values():
        w = wk(e.qid)
        name = e.extra["name"]
        conn.execute("INSERT INTO composers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (e.id, name, sort_name(name), e.qid, w.get("years"), w.get("bio"), w.get("description"),
                      w.get("image_url"), w.get("wikipedia_url"), fold(" ".join([name, *e.names])), w.get("intro"),
                      _credit_json(w)))
    for e in works.ents.values():
        w = wk(e.qid)
        comp = composers.ents[e.extra["composer_key"]]
        conn.execute("INSERT INTO works VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (e.id, e.extra["name"], comp.id, e.qid, e.extra.get("catalogue"),
                      w.get("bio") or w.get("description"), w.get("wikipedia_url"),
                      fold(" ".join([*e.names, comp.extra["name"], e.extra.get("catalogue") or ""])),
                      w.get("synopsis"), w.get("synopsis_heading"),
                      str(w["premiere_date"]) if w.get("premiere_date") else None, w.get("premiere_place"),
                      json.dumps(w["librettists"], ensure_ascii=False) if w.get("librettists") else None))
    for e in people.ents.values():
        w = wk(e.qid)
        name = e.extra["name"]
        conn.execute("INSERT INTO people VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (e.id, name, sort_name(name, e.extra["kind"]), e.extra["kind"], e.qid, w.get("years"),
                      w.get("bio"), w.get("description"), w.get("image_url"), w.get("wikipedia_url"),
                      fold(" ".join(e.names)), w.get("intro"), _credit_json(w)))

    kinds_by_key = {k: e.extra["kind"] for k, e in people.ents.items()}
    n_tracks = n_q = 0
    for row in perf_rows:
        r, perf = row["r"], row["perf"]
        pid = r["id"]
        prog = row["program"]
        title = derive_title(r, [(p["title"], p["composer"]) for p in prog], row["music_secs"])
        subtitle = derive_subtitle(row["credits"], kinds_by_key)
        n_credits = len(row["credits"])
        confirmed = sum(1 for c in row["credits"] if c["status"] == "confirmed")
        ratio = round(confirmed / n_credits, 2) if n_credits else 0.0
        durs = [t["duration_s"] for t in row["tracks"] if t["duration_s"]]
        duration = round(sum(durs)) if durs else None
        venue = row["venue"]
        search = fold(" ".join(filter(None, [
            pid, title, subtitle, r.get("summary"), perf.get("event_name"), perf.get("venue"), perf.get("city"),
            perf.get("date"), perf.get("broadcaster"), row["collection"], row["folder"],
            *[str(v) for v in (perf["release"].values() if isinstance(perf.get("release"), dict) else ())],
            *[p["title"] for p in prog], *[composers.ents[p["composer_key"]].extra["name"] for p in prog],
            *[people.ents[c["person_key"]].extra["name"] for c in row["credits"]],
            *[c["role"] for c in row["credits"]],
        ])))
        conn.execute(
            "INSERT INTO performances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, title, subtitle, perf.get("date"), perf.get("date_status") or "unknown",
             venue.id if venue else None, r.get("kind") or "unknown", row["collection"], duration, row["artwork"],
             ratio, r.get("summary"), perf.get("date_evidence"), json.dumps(perf.get("candidate_dates") or []),
             perf.get("broadcast_date"), perf.get("broadcaster"), perf.get("event_name"),
             json.dumps(perf.get("release")) if perf.get("release") else None, row["folder"],
             json.dumps(row["dirs"]),
             json.dumps(fix_links(r.get("sources") or [], link_status, False, link_counts), ensure_ascii=False),
             json.dumps(fix_links(r.get("resources") or [], link_status, True, link_counts), ensure_ascii=False),
             json.dumps(r.get("open_questions") or [], ensure_ascii=False), str(row["src"]), search,
             json.dumps(row["artwork_meta"]) if row["artwork_meta"] else None))
        seen_c = set()
        for i, p in enumerate(prog):
            cid = composers.ents[p["composer_key"]].id
            if cid not in seen_c:
                conn.execute("INSERT INTO performance_composers VALUES (?,?,?)", (pid, cid, len(seen_c)))
                seen_c.add(cid)
            conn.execute("INSERT INTO program VALUES (?,?,?,?,?,?,?)",
                         (pid, i, works.ents[p["work_key"]].id, p["title"], cid, p["catalogue"], p["notes"]))
        for i, c in enumerate(row["credits"]):
            conn.execute("INSERT INTO credits VALUES (?,?,?,?,?,?,?,?)",
                         (pid, i, people.ents[c["person_key"]].id, c["role"],
                          works.ents[c["work_key"]].id if c["work_key"] else None, c["status"], c["notes"],
                          json.dumps(c["source_ids"])))
        width = max(2, len(str(len(row["tracks"]))))
        for i, t in enumerate(row["tracks"], start=1):
            tid = f"{pid}:{i:0{width}d}"
            conn.execute("INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (tid, pid, i, t["title"], t["content"], works.ents[t["work_key"]].id if t["work_key"] else None,
                          t["segment"], t["duration_s"], t["format"], t["path"], t["stream_path"], t["size"]))
            n_tracks += 1
        for i, q in enumerate(dict.fromkeys(r.get("open_questions") or [])):
            conn.execute("INSERT OR IGNORE INTO questions VALUES (?,?,?)", (pid, i, q))
            n_q += 1

    stats = {"performances": len(perf_rows), "cds": n_cds, "people": len(people.ents), "composers": len(composers.ents),
             "works": len(works.ents), "venues": len(venues.ents), "tracks": n_tracks, "questions": n_q,
             "wiki_entities": len(wiki), "artwork": sum(1 for r in perf_rows if r["artwork"]),
             "dead_links_archived": link_counts["archived"], "dead_links_unlinked": link_counts["unlinked"],
             "dead_links_dropped": link_counts["dropped"], "seconds": round(time.time() - t0, 2)}
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                     [("imported_at", time.strftime("%Y-%m-%dT%H:%M:%S")), ("stats", json.dumps(stats))])
    # carry Ask Dad answers over last, just before the swap, so answers given during the build survive
    if db_path.exists():
        try:
            old = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            rows = old.execute("SELECT performance_id, question, answer, answered_at FROM answers").fetchall()
            conn.executemany("INSERT OR REPLACE INTO answers VALUES (?,?,?,?)", rows)
            old.close()
            log.info("carried over %d answers", len(rows))
        except sqlite3.Error as e:
            if "no such table" not in str(e):  # an old DB with answers we cannot read: replacing it would lose them
                conn.close()
                os.unlink(tmp)
                raise RuntimeError(f"could not carry over the Ask Dad answers from {db_path}: {e}") from e
            log.warning("could not carry over answers: %s", e)
    conn.commit()
    conn.close()
    os.replace(tmp, db_path)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, help="output DB (default data/sethster.db or $SETHSTER_DB)")
    ap.add_argument("--no-media", action="store_true", help="do not scan MEDIA_ROOT for artwork")
    ap.add_argument("--no-artwork", action="store_true", help="pick no cover pictures at all (a working DB)")
    ap.add_argument("--no-cds", action="store_true", help="leave out the Crutchfield CDs that are ready for the app")
    ap.add_argument("--only", type=Path, metavar="FILE",
                    help="import only the ids listed in FILE (one per line; blank lines and '#' comments ignored), "
                         "e.g. data/release/ready.txt")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    only = None
    if args.only:
        try:
            only = read_id_list(args.only)
        except OSError as e:
            ap.error(f"--only: cannot read {args.only}: {e}")
        if not only:
            ap.error(f"--only: no ids in {args.only}")
    settings = load_settings()
    stats = build(settings, args.db, use_media=not args.no_media, only=only, artwork=not args.no_artwork,
                  cds=not args.no_cds)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
