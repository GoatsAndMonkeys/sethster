"""Tolerant loader for the Wikidata/Wikipedia enrichment files under data/wiki/**.

The exact format is produced by another pipeline step, so this accepts several shapes:
  * one entity per file, file named Q123.json (or with a "qid"/"id"/"wikidata" field)
  * one file holding {"Q123": {...}, "Q456": {...}}
  * one file holding a list of entity dicts with a qid field
Field names are normalised (label/name, bio/extract/summary, image_url/image, ...).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .text import years_label

log = logging.getLogger(__name__)
QID = re.compile(r"^Q\d+$")
NON_ENTITY_FILES = {"index", "rejected", "unmatched", "stats"}


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _normalise(raw: dict) -> dict:
    birth = _first(raw, "birth_year", "born", "birth", "birth_date", "date_of_birth", "inception_year", "inception",
                   "founded")
    death = _first(raw, "death_year", "died", "death", "death_date", "date_of_death", "dissolved_year", "dissolved")
    image = _first(raw, "image_url", "image", "thumbnail", "thumbnail_url", "commons_image_url")
    credit = None
    if isinstance(image, dict):  # data/wiki format_version 1: {"thumb_url", "url_600", "artist", "license", ...}
        credit = {k: image.get(k) for k in ("artist", "license", "license_url", "commons_page", "credit")
                  if image.get(k)} or None
        image = _first(image, "thumb_url", "url_600", "original_url", "url", "source", "src")
    wpd = raw.get("wikipedia") if isinstance(raw.get("wikipedia"), dict) else {}
    bio = _first(raw, "bio", "extract", "summary", "abstract", "wikipedia_extract") or wpd.get("summary")
    wp = _first(raw, "wikipedia_url", "enwiki_url", "enwiki", "url_wikipedia") or wpd.get("url")
    if not wp and isinstance(raw.get("wikipedia"), str):
        wp = raw["wikipedia"]
    if not image and wpd.get("thumbnail"):
        image = wpd["thumbnail"]
    return {
        "label": _first(raw, "label", "name", "title"),
        "description": _first(raw, "description", "desc"),
        "bio": bio,
        "intro": _first(raw, "intro") or wpd.get("intro"),
        "synopsis": wpd.get("synopsis"),
        "synopsis_heading": wpd.get("synopsis_heading"),
        "premiere_date": _first(raw, "premiere_date", "premiere_year"),
        "premiere_place": ", ".join(x.get("label") for x in raw.get("premiere_place") or []
                                    if isinstance(x, dict) and x.get("label")) or None,
        "librettists": [x.get("label") for x in raw.get("librettists") or []
                        if isinstance(x, dict) and x.get("label")] or None,
        "wikipedia_lang": wpd.get("lang"),
        "image_url": image if isinstance(image, str) else None,
        "image_credit": credit or _first(raw, "image_credit", "image_attribution"),
        "wikipedia_url": wp if isinstance(wp, str) else None,
        "years": _first(raw, "years") or years_label(birth, death),
    }


def load_index(wiki_dir: Path | None) -> dict:
    """data/wiki/index.json (QID corrections per performance), or {}."""
    if not wiki_dir:
        return {}
    p = wiki_dir / "index.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception as e:  # noqa: BLE001
        log.warning("wiki: unreadable index.json (%s)", e)
        return {}


def load_wiki(wiki_dir: Path | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not wiki_dir or not wiki_dir.is_dir():
        return out

    def add(qid: str, raw: dict, category: str):
        if not isinstance(raw, dict) or not QID.match(qid or ""):
            return
        ent = _normalise(raw)
        ent["category"] = category
        cur = out.setdefault(qid, {})
        for k, v in ent.items():
            if v not in (None, "") and not cur.get(k):
                cur[k] = v

    from .importer import _read_many  # noqa: PLC0415 (parallel reads; see there)

    paths = [p for p in sorted(wiki_dir.rglob("*.json")) if not (p.parent == wiki_dir and p.stem in NON_ENTITY_FILES)]
    blobs = _read_many(paths)
    for path in paths:
        try:
            data = json.loads(blobs[path] or b"")
        except Exception as e:  # noqa: BLE001 - tolerate anything half-written
            log.warning("wiki: skipping %s (%s)", path, e)
            continue
        category = path.parent.name if path.parent != wiki_dir else ""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    add(str(_first(item, "qid", "wikidata", "id") or ""), item, category)
        elif isinstance(data, dict):
            keys = list(data.keys())
            if keys and all(QID.match(k) for k in keys):
                for k, v in data.items():
                    add(k, v, category)
            else:
                qid = _first(data, "qid", "wikidata", "id")
                if not (isinstance(qid, str) and QID.match(qid)):
                    qid = path.stem
                add(qid, data, category)
    log.info("wiki: loaded %d entities from %s", len(out), wiki_dir)
    return out
