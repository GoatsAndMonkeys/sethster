"""Canonical work IDs: one id per piece across every recording of it in the collection.

The importer gives each (composer, title) pair its own work row, so the same piece can appear under
several ids ("Symphony No. 6 in A minor 'Tragic'" vs "... ('Tragic')", one with a Wikidata QID and
one without), and composer naming slips ("Ludwig van Beethoven, arr. Franz Liszt") leak into slugs.
This module groups work rows into canonical works:

* composer key: Wikidata QID, else the folded name with arranger/attribution notes removed;
* two works of the same composer are the same piece when they share a work QID, a normalized
  catalogue number (``Op. 6 No. 4`` -> ``op-6-4``, ``K. 550`` -> ``k-550``, ``BWV 245``), a form+number
  ("piano concerto 1") or a core title (accents, keys, nicknames, "complete", "(encore)" removed);
* excerpts ("Così fan tutte: 'Ah guarda, sorella'", "... (excerpt)", "'Hoe-Down' from Rodeo") never
  merge with the complete work: they get their own canonical id with ``excerpt_of`` set.

Canonical ids look like ``beethoven-symphony-no-7-op-92`` or ``verdi-simon-boccanegra``. They are
derived from data, so they are stable across re-imports as long as the research text is.
Pure stdlib; the segmentation pipeline imports it too.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .text import fold, norm_key, slugify, surname

# catalogue prefixes that identify the composer on their own (used to fix composer slips)
CATALOGUE_COMPOSER = {
    "bwv": "bach", "k": "mozart", "kv": "mozart", "d": "schubert", "wab": "bruckner", "hob": "haydn",
    "hwv": "handel", "wwv": "wagner", "s": "liszt", "lw": "liszt", "sz": "bartok", "bb": "bartok",
    "l": "debussy", "rv": "vivaldi", "trv": "strauss", "av": "strauss", "m": "ravel", "lwv": "lully",
    "jw": "janacek", "woo": "beethoven", "h": "berlioz",
}
_CAT_RE = re.compile(
    r"\b(op(?:us)?|bwv|kv?|d|wab|hob|hwv|wwv|s|lw|sz|bb|l|rv|trv|av|m|lwv|jw|woo|h|b)\.?\s*"
    r"([ivxlc]+:\s*)?(\d+[a-z]?)(?:\s*,?\s*no\.?\s*(\d+))?", re.I)
_KEY_RE = re.compile(r"\bin\s+[a-g](?:[\s-]*(?:flat|sharp|♭|♯|b|#))?(?:\s+(?:major|minor))?\b", re.I)
_FORM_RE = re.compile(
    r"^(?P<form>(?:[a-z]+\s){0,3}?(?:symphony|symphonie|sinfonia|concerto|sonata|quartet|quintet|trio|sextet|"
    r"octet|suite|mass|cantata|serenade|divertimento|partita|etude|prelude|nocturne|ballade|scherzo|"
    r"impromptu|rhapsody|overture|string quartet|piano trio|piano sonata|violin sonata|piano concerto|"
    r"violin concerto|cello concerto))\s+no\.?\s*(?P<n>\d+)\b", re.I)
_EXCERPT_RE = re.compile(r"\b(excerpts?|selections?|highlights|aria|duet|trio '|scene|act [ivx\d]+\s*[:,]"
                         r"|movement|mvt|intermezzo from|overture to|prelude to)\b", re.I)
_ARR_RE = re.compile(r"\s*(?:,\s*|\()\s*(?:arr\.?|arranged|transcr\.?|orch\.?|attributed|attr\.?|after|ed\.)"
                     r"[^)]*\)?\s*$", re.I)
_GENERIC = re.compile(
    r"(?:[a-z]+ ){0,3}(?:symphony|concerto|sonata|quartet|quintet|trio|sextet|octet|suite|mass|cantata|"
    r"serenade|divertimento|partita|etude|prelude|nocturne|ballade|scherzo|impromptu|rhapsody|overture|"
    r"fantasia|fugue|toccata|variations|waltz|mazurka|polonaise|sinfonia|motet|aria|song|romance|dance)"
    r"(?: [a-z]+){0,2}?(?: in [a-g](?: flat| sharp)?(?: major| minor)?)?(?: (?:for|no) .*)?")
_UNIDENTIFIED = re.compile(r"\b(unidentified|unknown|encores?\b.*if any|untitled)\b", re.I)


def clean_composer(name: str | None) -> str:
    n = (name or "").strip()
    prev = None
    while prev != n:
        prev = n
        n = _ARR_RE.sub("", n).strip()
    return n or "Unknown"


def catalogue_key(cat: str | None) -> str | None:
    """'Op. 27 No. 2' -> 'op-27-2'; 'K. 533/494' -> 'k-533'; 'Sz. 102, BB 110' -> 'sz-102'."""
    if not cat:
        return None
    m = _CAT_RE.search(fold(cat))
    if not m:
        return None
    pre = m.group(1).lower()
    pre = {"opus": "op", "kv": "k"}.get(pre, pre)
    roman = (m.group(2) or "").strip().rstrip(":")
    parts = [pre] + ([roman] if roman else []) + [m.group(3)] + ([m.group(4)] if m.group(4) else [])
    return "-".join(parts)


def split_excerpt(title: str) -> tuple[str, str] | None:
    """('Così fan tutte', "'Ah guarda, sorella'") for an excerpt title, else None."""
    t = title.strip()
    m = re.match(r"^['\"‘“](.+?)['\"’”]\s+from\s+(.+)$", t, re.I)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    m = re.match(r"^(.+?)\s+from\s+(?:the\s+)?([A-Z].+)$", t)
    if m and not re.search(r"\bsuite\b", m.group(1), re.I):
        return m.group(2).strip(), m.group(1).strip()
    if ":" in _CAT_RE.sub("", t):      # not the colon of a catalogue number (Hob. I:104)
        head, tail = t.split(":", 1)
        if len(head) > 2 and tail.strip():
            return head.strip(), tail.strip()
    m = re.match(r"^(.+?)\s*\(([^)]*\b(?:excerpts?|selections?|closing|opening|scenes?|highlights|"
                 r"movements?|finale)\b[^)]*)\)", t, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None


def core_title(title: str, strip_quotes: bool = True) -> str:
    t = fold(title)
    t = re.sub(r"\((?:[^)]*\b(?:encore|complete|concert performance|tentative|version|revision|"
               r"orchestral encore)\b[^)]*)\)", " ", t)
    t = re.sub(r"\bcomplete( ballet| opera)?\b", " ", t)
    t = re.sub(r",?\s*\b(op|bwv|k|kv|d|hwv|wwv|hob|wab|rv)(?:\.\s*|\s+)(?:[ivxlc]+:)?\d.*$", " ", t)
    if form_key(title):          # numbered: the key is redundant ("Symphony No. 5" == "... in D minor")
        t = _KEY_RE.sub(" ", t)
    if strip_quotes:             # nicknames (not apostrophes inside words: l'elisir d'amore)
        t = re.sub(r"(?<!\w)['\"‘“].*?['\"’”](?!\w)", " ", t)
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"^(the|der|die|das|le|la|les|il|lo|l'|i)\s+", "", t.strip())
    return norm_key(t)


def form_key(title: str) -> str | None:
    m = _FORM_RE.match(fold(title))
    if not m:
        return None
    form = norm_key(m.group("form"))
    form = form.replace("symphonie", "symphony").replace("sinfonia", "symphony")
    return f"{form} {m.group('n')}"


@dataclass
class CanonicalWork:
    id: str
    title: str
    composer_id: str | None
    composer_name: str
    catalogue: str | None
    work_ids: list[str] = field(default_factory=list)
    excerpt_of: str | None = None
    unidentified: bool = False
    wikidata: str | None = None


class _UF:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def build(rows: list[dict]) -> tuple[dict[str, CanonicalWork], dict[str, str]]:
    """rows: dicts with id, title, catalogue, wikidata, composer_id, composer_name, composer_qid,
    and optionally ``uses`` (performance count, for picking display titles).
    Returns (canonical works by id, work id -> canonical id)."""
    uf = _UF()
    info: dict[str, dict] = {}
    by_key: dict[tuple, str] = {}
    for r in rows:
        wid = r["id"]
        title = (r.get("title") or "").strip()
        cname = clean_composer(r.get("composer_name"))
        ckey = r.get("composer_qid") or "n:" + norm_key(cname)
        exc = split_excerpt(title)
        cat = catalogue_key(r.get("catalogue"))
        unident = bool(_UNIDENTIFIED.search(title)) or cname.lower() in ("(unknown)", "unknown")
        is_exc = exc is not None
        info[wid] = dict(r, title=title, cname=cname, ckey=ckey, exc=exc if is_exc else None, cat=cat, unident=unident)
        uf.find(wid)
        if unident:
            continue
        if is_exc:
            parent_core = core_title(exc[0])
            keys = [("exc", ckey, parent_core, core_title(exc[1], False) or norm_key(exc[1]))]
        else:
            keys = []
            if r.get("wikidata"):
                keys.append(("q", r["wikidata"]))
            if cat:
                keys.append(("cat", ckey, cat))
            fk = form_key(title)
            if fk:
                keys.append(("form", ckey, fk))
            ct = core_title(title)
            if ct:
                # generic titles ("Concerto Grosso in A minor") only merge with the same catalogue no.
                keys.append(("core", ckey, ct, (catalogue_key(title) or cat or "") if _GENERIC.fullmatch(ct) else ""))
        for k in keys:
            if k in by_key:
                uf.union(wid, by_key[k])
            else:
                by_key[k] = wid

    groups: dict[str, list[str]] = defaultdict(list)
    for wid in info:
        groups[uf.find(wid)].append(wid)

    canon: dict[str, CanonicalWork] = {}
    w2c: dict[str, str] = {}
    used_ids: Counter = Counter()
    pending_parent: dict[str, tuple[str, str]] = {}
    for _, members in sorted(groups.items()):
        members.sort()
        rs = [info[m] for m in members]
        titles = Counter()
        for x in rs:
            titles[x["title"]] += 1 + (x.get("uses") or 0)
        title = max(titles, key=lambda t: (titles[t], -len(t)))
        lead = next(x for x in rs if x["title"] == title)
        cname = lead["cname"]
        cat_raw = next((x.get("catalogue") for x in rs if x.get("catalogue")), None)
        cat = next((x["cat"] for x in rs if x["cat"]), None)
        comp_slug = slugify(surname(cname, particles=False)) if cname != "Unknown" else "unknown"
        base_title = re.sub(r"\s*\((?:encore|orchestral encore|tentative)\)\s*", " ", title).strip()
        if lead["exc"]:
            stem = f"{comp_slug} {core_title(lead['exc'][0])} {lead['exc'][1]}"
        else:
            fk = form_key(base_title)
            if fk:
                form, _, n = fk.rpartition(" ")
                fk = f"{form} no {n}"
            stem = f"{comp_slug} {fk or core_title(base_title) or base_title}"
            if cat:
                stem += f" {cat}"
        cid = slugify(stem, max_len=90)
        if lead["unident"]:
            cid = slugify(f"unidentified {members[0]}", max_len=90)
        used_ids[cid] += 1
        if used_ids[cid] > 1:
            cid = f"{cid}-{used_ids[cid]}"
        cw = CanonicalWork(id=cid, title=base_title, composer_id=lead.get("composer_id"), composer_name=cname,
                           catalogue=cat_raw, work_ids=members, unidentified=lead["unident"],
                           wikidata=next((x.get("wikidata") for x in rs if x.get("wikidata")), None))
        canon[cid] = cw
        for m in members:
            w2c[m] = cid
        if lead["exc"]:
            pending_parent[cid] = (lead["ckey"], core_title(lead["exc"][0]))

    # link excerpts to their parent piece when the parent is in the collection
    parent_index: dict[tuple[str, str], str] = {}
    for cid, cw in canon.items():
        if cid in pending_parent:
            continue
        x = info[cw.work_ids[0]]
        for wid in cw.work_ids:
            parent_index.setdefault((info[wid]["ckey"], core_title(info[wid]["title"])), cid)
        parent_index.setdefault((x["ckey"], core_title(cw.title)), cid)
    for cid, key in pending_parent.items():
        canon[cid].excerpt_of = parent_index.get(key)
    return canon, w2c


def load_rows(conn: sqlite3.Connection) -> list[dict]:
    q = """SELECT w.id, w.title, w.catalogue, w.wikidata, w.composer_id, c.name AS composer_name,
                  c.wikidata AS composer_qid,
                  (SELECT COUNT(DISTINCT performance_id) FROM program p WHERE p.work_id = w.id) AS uses
           FROM works w LEFT JOIN composers c ON c.id = w.composer_id"""
    return [dict(r) for r in conn.execute(q)]


_CACHE: dict[str, tuple] = {}


def for_db(conn: sqlite3.Connection, cache_key: str | None = None) -> tuple[dict[str, CanonicalWork], dict[str, str]]:
    """Canonical works for this DB, memoized on ``cache_key`` (e.g. the DB's imported_at)."""
    if cache_key and cache_key in _CACHE:
        return _CACHE[cache_key]
    result = build(load_rows(conn))
    if cache_key:
        _CACHE.clear()
        _CACHE[cache_key] = result
    return result
