"""Which Crutchfield CDs are ready for the app (docs/plans/cd-catalog.md §4.4, §8).

A CD is ready when all three hold:
  1. harvested:   data/crutchfield/harvest/<id>.json exists (stage H);
  2. identified:  data/crutchfield/results/<id>.json says status "matched" (stage M, any method);
  3. art decided: data/media/cd/<id>.json has status "ok" (a front, back or disc chosen) or "none" (confirmed
                  that there is no art). "error" or a missing manifest is not decided.
Liner notes come later; the app shows a ready CD without them.

Standalone and stdlib-only (Python 3.9 compatible) on purpose: the dashboard collector loads this file directly
with the system python, without the server package. Readiness is computed from whatever files exist, so the
count rises by itself as match results and art manifests land.
"""
from __future__ import annotations

import json
import os

HARVEST_DIR = ("crutchfield", "harvest")
RESULTS_DIR = ("crutchfield", "results")
ART_DIR = ("media", "cd")
MATCHED = frozenset({"matched"})
ART_DECIDED = frozenset({"ok", "none"})


def _dir(data_dir, parts) -> str:
    return os.path.join(str(data_dir), *parts)


def _json_files(d: str) -> dict:
    """{id: (path, mtime_ns, size)} for the non-empty <id>.json files in d."""
    out = {}
    try:
        it = os.scandir(d)
    except OSError:
        return out
    with it:
        for e in it:
            if not e.name.endswith(".json") or e.name.startswith("."):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            if e.is_file() and st.st_size > 0:
                out[e.name[:-5]] = (e.path, st.st_mtime_ns, st.st_size)
    return out


def _summaries(files: dict, extract, cache: dict | None) -> dict:
    """{id: extract(doc)} for every readable file; `cache` ({id: [mtime_ns, size, value]}, persisted by the
    caller) skips re-reading files that have not changed. Unreadable or half-written files are left out."""
    out = {}
    for pid, (path, mtime, size) in files.items():
        hit = cache.get(pid) if cache is not None else None
        if hit and hit[0] == mtime and hit[1] == size:
            out[pid] = hit[2]
            continue
        try:
            with open(path, "rb") as fh:
                doc = json.loads(fh.read())
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        value = extract(doc)
        out[pid] = value
        if cache is not None:
            cache[pid] = [mtime, size, value]
    if cache is not None:
        for pid in [k for k in cache if k not in files]:
            del cache[pid]
    return out


def _match_summary(doc: dict) -> list:
    return [doc.get("status"), doc.get("method"), doc.get("confidence_band")]


def _art_summary(doc: dict) -> list:
    return [doc.get("status"), bool(doc.get("front")), bool(doc.get("needs_vision"))]


def scan(data_dir, cache: dict | None = None) -> dict:
    """Readiness of every CD. `cache` (optional, JSON-serializable, kept by the caller between runs) holds
    per-file summaries so a frequent caller only re-reads files that changed.

    Returns {"harvested", "matched", "art_decided", "ready": sets of ids, "match": {id: [status, method, band]},
    "art": {id: [status, has_front, needs_vision]}}."""
    if cache is not None:
        cache.setdefault("results", {})
        cache.setdefault("art", {})
    harvested = set(_json_files(_dir(data_dir, HARVEST_DIR)))
    match = _summaries(_json_files(_dir(data_dir, RESULTS_DIR)), _match_summary,
                       cache["results"] if cache is not None else None)
    art = _summaries(_json_files(_dir(data_dir, ART_DIR)), _art_summary,
                     cache["art"] if cache is not None else None)
    matched = {k for k, v in match.items() if v[0] in MATCHED}
    decided = {k for k, v in art.items() if v[0] in ART_DECIDED}
    return {"harvested": harvested, "matched": matched, "art_decided": decided,
            "ready": harvested & matched & decided, "match": match, "art": art}


def is_ready(harvest: dict | None, match: dict | None, art: dict | None) -> bool:
    """The same gate for one CD's already-loaded documents."""
    return (isinstance(harvest, dict) and isinstance(match, dict) and isinstance(art, dict)
            and match.get("status") in MATCHED and art.get("status") in ART_DECIDED)


def ready_ids(data_dir) -> set:
    return scan(data_dir)["ready"]
