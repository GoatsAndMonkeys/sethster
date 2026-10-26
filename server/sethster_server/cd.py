"""Crutchfield CDs as performances (docs/plans/cd-catalog.md §4.4, §6).

For every CD that is ready (cd_ready: harvested + matched on MusicBrainz + art decided) this builds a result in
the research-result shape (data/pilot/performance.schema.json) plus a track listing, so the importer handles CDs
and broadcasts the same way. Liner notes come later: once a research result data/research/results/<id>.json
exists it replaces this mechanical one (the importer prefers results it already loaded).

Tracks point at the NAS sandbox (`Crutchfield/...`, relative to MEDIA_ROOT = the sandbox share, read-only),
so the existing /api/tracks/{id}/stream serves them. Artwork comes from the CD art manifest
data/media/cd/<id>.json: performances.artwork_path is its front thumbnail, relative to the data dir.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from collections import Counter, OrderedDict
from pathlib import Path, PurePosixPath

from . import cd_ready
from .media import is_audio
from .text import fold, is_ensemble, norm_key, surname

log = logging.getLogger("sethster.cd")

CD_ID = re.compile(r"^cf-[a-z0-9][a-z0-9-]{0,200}$")
ART_ROLES = ("front", "back", "disc")
ART_SIZES = (160, 480, 1200)
DEFAULT_SIZE = 480

# tag keys across ID3v2, Vorbis (any case), m4a ilst (as the harvest names them) and ASF/WMA
TITLE_KEYS = ("TIT2", "TITLE", "title", "Title", "©nam")
ALBUM_KEYS = ("TALB", "ALBUM", "album", "WM/AlbumTitle", "©alb")
COMPOSER_KEYS = ("TCOM", "COMPOSER", "composer", "WM/Composer", "©wrt")
LABEL_KEYS = ("TPUB", "LABEL", "ORGANIZATION", "WM/Publisher")
CONDUCTOR_KEYS = ("TPE3", "CONDUCTOR", "conductor", "WM/Conductor")
ENSEMBLE_KEYS = ("ORCHESTRA", "ENSEMBLE")
PERFORMER_KEYS = ("TPE1", "TPE2", "ARTIST", "PERFORMER", "ALBUMARTIST", "ALBUM ARTIST", "artist", "album_artist",
                  "WM/AlbumArtist", "Author", "©ART", "aART")
_SPLIT = re.compile(r"\s*(?:/|;|\x00|\s&\s)\s*")
_JUNK_NAME = re.compile(r"^(various( artists)?|unknown( artist)?|va|track ?\d*|\d+|n/?a|-+)$", re.I)

MB_ROLE = {"conductor": "conductor", "performing orchestra": "orchestra", "chorus master": "chorus master",
           "concertmaster": "concertmaster", "producer": "producer", "engineer": "engineer",
           "recording": "recording engineer", "mix": "mixing engineer", "mastering": "mastering engineer"}


# ---- loading -----------------------------------------------------------------------------------------------

def _read_json(p: Path):
    try:
        d = json.loads(p.read_bytes())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def load_inventory_sizes(data_dir: Path, ids: set[str]) -> dict[str, int]:
    """{share path: bytes} for the tracks of these CDs (the harvest has durations, the inventory has sizes)."""
    p = data_dir / "crutchfield" / "inventory.jsonl"
    sizes: dict[str, int] = {}
    try:
        fh = p.open(encoding="utf-8")
    except OSError:
        return sizes
    with fh:
        for line in fh:
            if not line.startswith('{"id": "') or line[8:line.find('"', 8)] not in ids:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("id") not in ids:
                continue
            for disc in rec.get("discs") or []:
                for t in disc.get("tracks") or []:
                    if isinstance(t, dict) and t.get("file") and t.get("size"):
                        sizes[f"{disc.get('dir')}/{t['file']}"] = t["size"]
    return sizes


def load_ready(data_dir: Path, skip: set[str] = frozenset()) -> "OrderedDict[str, tuple[dict, dict, dict]]":
    """{id: (result, listing dossier, art manifest)} for every ready CD, except ids in `skip` (CDs that already
    have a research result). Unreadable files make a CD not ready (it is left out and logged)."""
    state = cd_ready.scan(data_dir)
    ids = sorted(state["ready"] - set(skip))
    sizes = load_inventory_sizes(data_dir, set(ids)) if ids else {}
    out: OrderedDict[str, tuple[dict, dict, dict]] = OrderedDict()
    for pid in ids:
        harvest = _read_json(data_dir.joinpath(*cd_ready.HARVEST_DIR, f"{pid}.json"))
        match = _read_json(data_dir.joinpath(*cd_ready.RESULTS_DIR, f"{pid}.json"))
        art = _read_json(data_dir.joinpath(*cd_ready.ART_DIR, f"{pid}.json"))
        if not cd_ready.is_ready(harvest, match, art):
            log.warning("CD %s changed while importing; left out", pid)
            continue
        try:
            result, dossier = to_result(harvest, match, sizes)
        except Exception as e:  # noqa: BLE001 - one odd CD must not stop the import
            log.warning("CD %s not imported: %s", pid, e)
            continue
        out[pid] = (result, dossier, art)
    log.info("CDs: %d harvested, %d matched, %d art decided, %d ready, %d imported",
             len(state["harvested"]), len(state["matched"]), len(state["art_decided"]), len(state["ready"]), len(out))
    return out


# ---- tags --------------------------------------------------------------------------------------------------

def _flat_tags(tags: dict) -> dict:
    """One flat {key: [str values]} from a harvest track's tags (Vorbis comments are nested one level)."""
    out: dict[str, list[str]] = {}

    def put(k, v):
        if isinstance(v, (list, tuple)):
            for x in v:
                put(k, x)
        elif isinstance(v, (str, int, float)) and str(v).strip():
            out.setdefault(k, []).append(str(v).strip())
    for k, v in (tags or {}).items():
        if isinstance(v, dict) and k in ("VORBIS_COMMENT", "vorbis"):
            for k2, v2 in v.items():
                put(str(k2).upper(), v2)
        elif not isinstance(v, dict):
            put(k, v)
    return out


def _first(flat: dict, keys) -> str | None:
    for k in keys:
        for v in flat.get(k) or []:
            if v:
                return v
    return None


def _names(flat: dict, keys) -> list[str]:
    out = []
    for k in keys:
        for v in flat.get(k) or []:
            for n in _SPLIT.split(v):
                n = n.strip(" ,.-")
                if n and len(n) <= 80 and not _JUNK_NAME.match(n):
                    out.append(n)
    return out


def _most_common(c: Counter) -> str | None:
    return c.most_common(1)[0][0] if c else None


# ---- result ------------------------------------------------------------------------------------------------

def _mb_role(c: dict) -> str:
    t = (c.get("type") or "").strip()
    attrs = [a for a in c.get("attributes") or [] if isinstance(a, str)]
    if t == "vocal":
        voice = next((a for a in attrs if a.endswith(" vocals")), None)
        if voice:
            v = voice[: -len(" vocals")]
            return "chorus" if v in ("choir", "background") else v
        return "vocals"
    if t == "instrument":
        return attrs[0] if attrs else "instrument"
    return MB_ROLE.get(t, t or "performer")


def _ensemble_role(name: str) -> str:
    if re.search(r"chor|choir|coro|singers|voices", name, re.I):
        return "chorus"
    if re.search(r"orch|sinfoni|symphon|philharm|kapelle|filarm", name, re.I):
        return "orchestra"
    return "ensemble"


def to_result(harvest: dict, match: dict, sizes: dict[str, int] | None = None) -> tuple[dict, dict]:
    """(result in the research-result shape, dossier-like listing {folders, tracks}) for one ready CD."""
    pid = harvest["id"]
    rel = match.get("release") if isinstance(match.get("release"), dict) else {}
    parsed = match.get("parsed") if isinstance(match.get("parsed"), dict) else {}
    sizes = sizes or {}

    # tracks + tag tallies
    tracks, track_map, folders = [], [], []
    albums, composers, labels, conductors = Counter(), Counter(), Counter(), Counter()
    performers, ensembles = Counter(), Counter()
    for disc in harvest.get("discs") or []:
        d = (disc.get("dir") or "").strip("/")
        if d and d not in folders:
            folders.append(d)
        for t in disc.get("tracks") or []:
            f = t.get("file") if isinstance(t, dict) else None
            if not f or not is_audio(f):
                continue
            path = f"{d}/{f}" if d else f
            flat = _flat_tags(t.get("tags") or {})
            title = _first(flat, TITLE_KEYS) or PurePosixPath(f).stem
            for c, keys in ((albums, ALBUM_KEYS), (composers, COMPOSER_KEYS), (labels, LABEL_KEYS)):
                v = _first(flat, keys)
                if v:
                    c[v] += 1
            conductors.update(set(_names(flat, CONDUCTOR_KEYS)))
            ensembles.update(set(_names(flat, ENSEMBLE_KEYS)))
            performers.update(set(_names(flat, PERFORMER_KEYS)))
            tracks.append({"path": path, "dur_s": t.get("dur"), "size": sizes.get(path)})
            track_map.append({"file": path, "content": title})
    if not tracks:
        raise ValueError("no audio tracks in the harvest")

    # program: one work (the folder's work, else the release group); the composer's full name when known
    composer = rel.get("composer") or _most_common(composers) or parsed.get("composer")
    work = parsed.get("work") or (rel.get("release_group") or {}).get("title") or rel.get("title")
    if work and ":" in work and not parsed.get("work"):
        work = work.split(":", 1)[0].strip()
    program = [{"composer": composer, "work": work}] if composer and work else []
    composer_keys = {norm_key(composer), norm_key(surname(composer or "", particles=False))} - {""}

    def is_composer(name: str) -> bool:
        return norm_key(name) in composer_keys or norm_key(surname(name, particles=False)) in composer_keys

    band = match.get("confidence_band") or ("high" if (match.get("confidence") or 0) >= 0.9 else "medium")
    mb_status = "confirmed" if band == "high" else "inferred"
    credits, seen = [], set()

    def same(k: str) -> bool:  # the same name spelled a little differently ("Philarmonia", "The ...")
        k2 = re.sub(r"^the ", "", k)
        return any(k2 == re.sub(r"^the ", "", s) or difflib.SequenceMatcher(None, k2, s).ratio() >= 0.88
                   for s in seen)

    def add(name: str, role: str, status: str, src: str, notes: str | None = None, aka: str | None = None):
        name = (name or "").strip()
        k = norm_key(name)
        if not k or is_composer(name) or (k in seen if src == "mb" and status == "confirmed" else same(k)):
            return
        seen.add(k)
        if aka and norm_key(aka):
            seen.add(norm_key(aka))
        c = {"name": name, "role": role, "status": status, "source_ids": [src]}
        if notes:
            c["notes"] = notes
        credits.append(c)

    # 1. MusicBrainz relationships (recording/release level), most-credited first
    mb_credits = [c for c in rel.get("credits") or [] if isinstance(c, dict) and c.get("name")]
    mb_credits.sort(key=lambda c: (_mb_role(c) != "conductor", -(c.get("tracks") or 0)))
    for c in mb_credits:
        add(c["name"], _mb_role(c), mb_status, "mb",
            f"credited as {c['credited_as']}" if c.get("credited_as") else None, aka=c.get("credited_as"))
    # 2. names in the MB artist credit (no roles there): the folder name says who conducted
    ac = rel.get("artist_credit") or ""
    if ";" in ac:
        ac = ac.split(";", 1)[1]
    cond = norm_key(parsed.get("conductor"))
    for n in [x.strip() for x in ac.split(",") if x.strip()]:
        if is_ensemble(n, ""):
            role = _ensemble_role(n)
        elif cond and norm_key(surname(n, particles=False)) == cond.split()[-1]:
            role = "conductor"
        else:
            role = "performer"
        add(n, role, "inferred", "mb")
    # 3. file tags (inferred), most frequent first
    for n, _ in conductors.most_common(3):
        add(n, "conductor", "inferred", "tags")
    for n, _ in ensembles.most_common(3):
        add(n, "orchestra", "inferred", "tags")
    for n, _ in performers.most_common(12):
        add(n, _ensemble_role(n) if is_ensemble(n, "") else "performer", "inferred", "tags")

    # date: MB recording relationships only (never the release date), else the folder name
    rec_dates = sorted({d for d in rel.get("recording_dates") or [] if isinstance(d, str) and d})
    places = [p for p in rel.get("recording_places") or [] if isinstance(p, str) and p]
    if rec_dates:
        date, evidence = rec_dates[0], "MusicBrainz recording relationship"
        if len(rec_dates) > 1:
            evidence += f"s ({len(rec_dates)} sessions, {rec_dates[0]} to {rec_dates[-1]})"
    elif parsed.get("date"):
        date, evidence = str(parsed["date"]), "the CD folder name"
    elif parsed.get("year"):
        date, evidence = str(parsed["year"]), "the year in the CD folder name"
    else:
        date, evidence = "", "unknown"
    venue = places[0] if len(places) == 1 else (parsed.get("venue") or "")

    labels_mb = [x for x in rel.get("labels") or [] if isinstance(x, dict)]
    label = (labels_mb[0].get("name") if labels_mb else None) or _most_common(labels) or parsed.get("label")
    catno = labels_mb[0].get("catno") if labels_mb else None
    year = (rel.get("date") or (rel.get("release_group") or {}).get("first_release_date") or "")[:4]
    release = {k: v for k, v in (("label", label), ("catalogue", catno), ("year", year)) if v}

    kind = "live_unofficial_release" if (rel.get("status") or "").lower() == "bootleg" else "commercial_release"
    what = ", ".join(x for x in (label, catno, year) if x)
    summary = f"CD release{f' ({what})' if what else ''}, identified on MusicBrainz"
    summary += f" ({match.get('method')} match)." if match.get("method") else "."
    sources = []
    if rel.get("url"):
        sources.append({"id": "mb", "url": rel["url"], "title": f"MusicBrainz release: {rel.get('title') or work}",
                        "type": "discography", "date_specific": bool(rec_dates), "read_ok": True})
    sources.append({"id": "tags", "url": "", "title": "Tags of the CD's audio files", "type": "tags",
                    "date_specific": False, "read_ok": True})
    resources = [{"url": rel["url"], "title": "MusicBrainz release", "kind": "discography"}] if rel.get("url") else []

    result = {
        "id": pid, "kind": kind, "summary": summary,
        "performance": {"date": date, "date_status": "inferred" if date else "unknown", "date_evidence": evidence,
                        "candidate_dates": rec_dates[1:], "venue": venue, "city": "",
                        **({"release": release} if release else {})},
        "program": program, "credits": credits, "track_map": track_map, "sources": sources,
        "resources": resources, "open_questions": [],
        "origin": "crutchfield",
    }
    if not program:
        result["title"] = _most_common(albums) or rel.get("title") or harvest.get("name") or pid
    return result, {"folders": folders, "tracks": tracks}


# ---- artwork -----------------------------------------------------------------------------------------------

def _blob(data_dir: Path, sha: str) -> str | None:
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None
    d = data_dir / "media" / "cd" / "blobs" / sha[:2]
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if (d / f"{sha}{ext}").is_file():
            return f"media/cd/blobs/{sha[:2]}/{sha}{ext}"
    return None


def art_file(data_dir: Path, manifest: dict, role: str = "front", size: int = DEFAULT_SIZE) -> str | None:
    """Data-dir-relative path of one picture named in the CD art manifest: the thumbnail of that size, else the
    nearest other thumbnail (larger first), else the original blob. Only paths under media/cd/ are returned."""
    entry = manifest.get(role) if isinstance(manifest, dict) else None
    if not isinstance(entry, dict):
        return None
    thumbs = entry.get("thumbs") if isinstance(entry.get("thumbs"), dict) else {}
    order = [size] + sorted((s for s in ART_SIZES if s != size), key=lambda s: (s < size, abs(s - size)))
    for s in order:
        p = thumbs.get(str(s))
        if isinstance(p, str) and p.startswith("media/cd/") and ".." not in p and (data_dir / p).is_file():
            return p
    return _blob(data_dir, entry.get("sha"))


def art_meta(manifest: dict) -> dict | None:
    """{w, h, generic, roles} of the CD's front (for captions and responsive images)."""
    if not isinstance(manifest, dict):
        return None
    roles = [r for r in ART_ROLES if isinstance(manifest.get(r), dict)]
    if not roles:
        return None
    f = manifest.get("front") if isinstance(manifest.get("front"), dict) else {}
    return {"w": f.get("w"), "h": f.get("h"), "generic": bool(f.get("generic")), "roles": roles}


def load_manifest(data_dir: Path, pid: str) -> dict | None:
    if not CD_ID.match(pid or ""):
        return None
    return _read_json(data_dir.joinpath(*cd_ready.ART_DIR, f"{pid}.json"))

