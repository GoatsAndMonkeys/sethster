"""Home Spotlight and the plain-English credit rating.

Spotlight: one composer, performer, venue or city with at least MIN_COUNT broadcasts, a different one each
day (deterministic by date, so every phone shows the same pick). When someone in the collection died in the
last REMEMBER_DAYS days, the spotlight is "Remembering <name>" instead. Deaths come from
data/deaths/recent.json (pipeline/deaths.py), data/calendar/memoriam.json and the Wikidata deaths in
data/context/people.json (the same lists the In Memoriam shelf uses).

Credit rating: 1-5 stars plus a percent, from the credit's status and how many independent sources back it
(date-specific ones count most; the announcer on the recording counts as one).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .importer import CONDUCTOR_ROLE, STAFF_ROLE
from .text import fold

MIN_COUNT = 3
REMEMBER_DAYS = 30
KINDS = ("composer", "performer", "venue", "city")
MAX_ITEMS = 10
SALT = "sethster-spotlight-v1"

# Theme days (the user's picks). They win over the daily rotation but not over Remembering, and are skipped
# when the collection has nothing finished for them. June (Pride Month) is opera queens: June 1 is Callas,
# then one a day through the queens the collection has finished broadcasts for (so no June day goes unthemed).
OPERA_QUEENS = ["Maria Callas", "Joan Sutherland", "Renata Tebaldi", "Leontyne Price", "Montserrat Caballé",
                "Beverly Sills", "Birgit Nilsson", "Jessye Norman", "Kiri Te Kanawa", "Mirella Freni",
                "Leonie Rysanek", "Marilyn Horne", "Renée Fleming", "Cecilia Bartoli", "Anna Netrebko",
                "Edita Gruberová", "Kathleen Battle", "Shirley Verrett", "Grace Bumbry", "Elisabeth Schwarzkopf",
                "Victoria de los Ángeles", "Frederica von Stade", "Joyce DiDonato", "Sondra Radvanovsky",
                "Natalie Dessay", "Karita Mattila", "Waltraud Meier", "Christa Ludwig", "Martina Arroyo"]


# ---- credit rating -------------------------------------------------------------------------------------

def _is_transcript(s: dict) -> bool:
    return (s.get("url") or "").startswith("transcript:") or str(s.get("id") or "").lower().startswith("t")


def _date_specific(s: dict) -> bool:
    v = s.get("date_specific")
    return v is True or (isinstance(v, str) and v.lower() == "true")


def _site(s: dict) -> str:
    """Sources from the same website count once (two pages of one archive are not independent)."""
    url = (s.get("url") or "").strip()
    parts = urlsplit(url)
    host = (parts.netloc or "").lower().split("@")[-1].split(":")[0]
    if host.endswith("web.archive.org") and parts.path.startswith("/web/"):
        inner = re.sub(r"^/web/[^/]*/", "", parts.path)
        host = urlsplit(inner if "://" in inner else "http://" + inner).netloc.lower().split(":")[0]
    labels = [x for x in host.removeprefix("www.").split(".") if x]
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "ac", "gov", "net", "or", "ne"}:
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return "id:" + str(s.get("id") or url)


LABELS = {5: "Very well backed up", 4: "Well backed up", 3: "Backed up", 2: "Likely", 1: "Unverified"}


def credit_rating(status: str | None, source_ids: list[str] | None, sources: dict[str, dict]) -> dict:
    """-> {"stars": 1-5, "confidence": percent, "label": ..., "basis": one plain sentence}.

    5: two independent sources about this date, or one plus the announcer on the recording
    4: one source about this date plus other support, or the announcer alone
    3: one source (three stars is still good)
    2: production cast / inferred, nothing about this exact date
    1: conflicting or unchecked"""
    srcs = [sources.get(i) or {"id": i} for i in dict.fromkeys(source_ids or [])]
    transcript = any(_is_transcript(s) for s in srcs)
    dated = {_site(s) for s in srcs if not _is_transcript(s) and _date_specific(s)}
    general = {_site(s) for s in srcs if not _is_transcript(s) and not _date_specific(s)} - dated
    extra = max(0, len(dated) + len(general) + transcript - 1)

    def said() -> str:
        bits = []
        if dated:
            bits.append(f"{len(dated)} source{'s' if len(dated) > 1 else ''} about this date")
        if transcript:
            bits.append("the announcer on the recording")
        if general:
            bits.append(f"{len(general)} general source{'s' if len(general) > 1 else ''}")
        return ", ".join(bits[:-1]) + (" and " if len(bits) > 1 else "") + bits[-1] if bits else ""

    if status == "confirmed":
        if len(dated) >= 2 or (transcript and dated):
            stars, pct = 5, 95 + min(4, extra - 1)
        elif transcript or (dated and general):
            stars, pct = 4, 86 + min(4, extra)
        elif dated:
            stars, pct = 3, 78
        elif general:
            stars, pct = 3, 66 + min(4, len(general) - 1) * 2
        else:
            stars, pct = 2, 55
        basis = f"Confirmed by {said()}." if said() else "Marked as confirmed, but no source is attached."
    elif status == "production_only":
        stars, pct = 2, 50 + min(5, len(dated) + len(general))
        basis = "In this production's cast, but no source names them for this exact date."
    elif status == "inferred":
        stars, pct = 2, 40 + min(5, len(dated) + len(general) + transcript)
        basis = "A careful guess (for example from that season's cast); nothing confirms this exact date."
    elif status == "conflict":
        stars, pct = 1, 25
        basis = "Sources disagree about this one."
    else:
        stars, pct = 1, 15
        basis = "Not checked yet."
    return {"stars": stars, "confidence": pct, "label": LABELS[stars], "basis": basis}


# ---- spotlight -----------------------------------------------------------------------------------------

def clean_name(n: str | None) -> str:
    """'Richard Wagner (arr. Lorin Maazel)' -> 'Richard Wagner' (same rule as calendar_api)."""
    return re.sub(r"\s*(?:\(|,\s*)(?:arr|transcr|orch|ed)\.?\b.*$", "", n or "", flags=re.I).strip() or (n or "")


def _performing(role: str | None) -> bool:
    r = (role or "").strip()
    return bool(CONDUCTOR_ROLE.search(r)) or not STAFF_ROLE.search(r)


def _parse_died(v: str | None) -> dt.date | None:
    v = v.strip() if isinstance(v, str) else ""
    try:
        if len(v) >= 10:
            return dt.date.fromisoformat(v[:10])
        if len(v) == 7:
            return dt.date.fromisoformat(v + "-01")
    except ValueError:
        return None
    return None


def _nice(d: dt.date) -> str:
    return f"{d.day} {d.strftime('%B')} {d.year}"


def _plural(n: int, word: str = "broadcast") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class _Json:
    """Small mtime cache for the JSON lists (they are rewritten while the server runs)."""

    def __init__(self):
        self._c: dict[Path, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, p: Path, default):
        try:
            m = p.stat().st_mtime
        except OSError:
            return default
        with self._lock:
            hit = self._c.get(p)
            if hit and hit[0] == m:
                return hit[1]
        try:
            v = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        with self._lock:
            self._c[p] = (m, v)
        return v


class Spotlight:
    def __init__(self, data_dir: Path, context_dir: Path | None = None, calendar_dir: Path | None = None):
        self.deaths_file = data_dir / "deaths" / "recent.json"
        self.memoriam_file = (calendar_dir or data_dir / "calendar") / "memoriam.json"
        self.people_file = (context_dir or data_dir / "context") / "people.json"
        self._json = _Json()

    # -- candidates --
    def candidates(self, conn, done: dict) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {k: [] for k in KINDS}
        comp: dict[str, dict] = {}
        for r in conn.execute("""SELECT c.id, c.name, c.years, c.description, c.image_src, pc.performance_id pid
                                 FROM composers c JOIN performance_composers pc ON pc.composer_id=c.id
                                 WHERE c.wikidata IS NOT NULL"""):
            if clean_name(r["name"]) != r["name"]:
                continue  # an "arr." variant row; the main composer row carries the count
            e = comp.setdefault(r["id"], {"kind": "composer", "id": r["id"], "name": r["name"], "years": r["years"],
                                          "description": r["description"],
                                          "image_url": f"/api/composers/{r['id']}/image" if r["image_src"] else None,
                                          "pids": set()})
            e["pids"].add(r["pid"])
        out["composer"] = list(comp.values())

        perf: dict[str, dict] = {}
        for r in conn.execute("""SELECT p.id, p.name, p.years, p.description, p.image_src, c.performance_id pid, c.role
                                 FROM people p JOIN credits c ON c.person_id=p.id
                                 WHERE p.kind='person' AND c.status IN ('confirmed', 'inferred')"""):
            if not _performing(r["role"]):
                continue
            e = perf.setdefault(r["id"], {"kind": "performer", "id": r["id"], "name": r["name"], "years": r["years"],
                                          "description": r["description"],
                                          "image_url": f"/api/people/{r['id']}/image" if r["image_src"] else None,
                                          "pids": set()})
            e["pids"].add(r["pid"])
        out["performer"] = list(perf.values())

        ven: dict[str, dict] = {}
        city: dict[str, dict] = {}
        for r in conn.execute("""SELECT v.id, v.name, v.city, v.description, p.id pid FROM performances p
                                 JOIN venues v ON v.id=p.venue_id"""):
            e = ven.setdefault(r["id"], {"kind": "venue", "id": r["id"], "name": r["name"], "city": r["city"],
                                         "description": r["description"], "image_url": None, "pids": set()})
            e["pids"].add(r["pid"])
            c = (r["city"] or "").strip()
            if c:
                e = city.setdefault(c, {"kind": "city", "id": c, "name": c, "description": None, "image_url": None,
                                        "pids": set(), "venues": {}})
                e["pids"].add(r["pid"])
                e["venues"][r["name"]] = e["venues"].get(r["name"], 0) + 1
        out["venue"] = list(ven.values())
        out["city"] = list(city.values())
        for k in KINDS:
            out[k] = [e for e in out[k] if len(e["pids"]) >= MIN_COUNT and any(p in done for p in e["pids"])]
        return out

    # -- remembering --
    def deaths(self) -> list[dict]:
        out = []
        for p in (self._json.get(self.deaths_file, {}) or {}).get("people") or []:
            out.append({"name": p.get("name"), "qid": p.get("qid"), "died": p.get("died"), "desc": p.get("desc")})
        for m in (self._json.get(self.memoriam_file, {}) or {}).get("items") or []:
            out.append({"name": m.get("name"), "qid": m.get("qid"), "died": m.get("died"), "desc": None})
        for qid, pf in ((self._json.get(self.people_file, {}) or {}).get("items") or {}).items():
            if pf.get("death"):
                out.append({"name": pf.get("name"), "qid": qid, "died": pf.get("death"), "desc": None})
        return out

    def remembering(self, conn, day: dt.date) -> dict | None:
        recent = []
        for d in self.deaths():
            died = _parse_died(d.get("died"))
            if died and 0 <= (day - died).days <= REMEMBER_DAYS and (d.get("name") or d.get("qid")):
                recent.append((died, d))
        if not recent:
            return None
        people = conn.execute("SELECT id, name, wikidata, years, description, image_src FROM people WHERE kind='person'").fetchall()
        comps = conn.execute("SELECT id, name, wikidata, years, description, image_src FROM composers").fetchall()
        by_qid = {("performer", r["wikidata"]): r for r in people if r["wikidata"]}
        by_qid.update({("composer", r["wikidata"]): r for r in comps if r["wikidata"]})
        by_name = {("performer", fold(r["name"])): r for r in people}
        by_name.update({("composer", fold(clean_name(r["name"]))): r for r in comps if clean_name(r["name"]) == r["name"]})
        best = None
        for died, d in sorted(recent, key=lambda x: x[0], reverse=True):
            for kind in ("performer", "composer"):
                row = by_qid.get((kind, d.get("qid"))) or by_name.get((kind, fold(d.get("name"))))
                if not row:
                    continue
                if kind == "performer":
                    pids = [x[0] for x in conn.execute(
                        """SELECT DISTINCT c.performance_id FROM credits c WHERE c.person_id=?
                           AND c.status != 'production_only'""", (row["id"],))]
                    base = "people"
                else:
                    pids = [x[0] for x in conn.execute(
                        "SELECT DISTINCT performance_id FROM performance_composers WHERE composer_id=?", (row["id"],))]
                    base = "composers"
                if not pids:
                    continue
                cand = {"kind": kind, "id": row["id"], "name": row["name"], "years": row["years"],
                        "description": row["description"],
                        "image_url": f"/api/{base}/{row['id']}/image" if row["image_src"] else None,
                        "pids": set(pids), "died": died, "desc": d.get("desc")}
                if best is None or (died, len(pids)) > (best["died"], len(best["pids"])):
                    best = cand
                break
            if best and best["died"] > died:
                break  # deaths are newest first; an older one can't win
        return best

    # -- the pick --
    @staticmethod
    def _order(entities: list[dict]) -> list[dict]:
        return sorted(entities, key=lambda e: hashlib.sha256(f"{SALT}:{e['kind']}:{e['id']}".encode()).hexdigest())

    @staticmethod
    def themed(cands: dict[str, list[dict]], day: dt.date) -> dict | None:
        """-> the day's theme entity with a "theme" eyebrow, or None (no theme, or nothing finished for it)."""
        def find(kind: str, name: str) -> dict | None:
            return next((e for e in cands[kind] if fold(e["name"]) == fold(name)), None)

        if day.month == 6:
            queens = [e for e in (find("performer", n) for n in OPERA_QUEENS) if e]
            if queens:
                return {**queens[(day.day - 1) % len(queens)], "theme": "Pride Month · Opera Queens"}
        if day.weekday() == 0:  # Mahler and Mozart Mondays alternate (ordinal weeks, so also across New Year)
            name, theme = (("Gustav Mahler", "Mahler Monday") if (day.toordinal() // 7) % 2 == 0
                           else ("Wolfgang Amadeus Mozart", "Mozart Monday"))
            e = find("composer", name)
            if e:
                return {**e, "theme": theme}
        if day.weekday() == 2:
            e = find("composer", "Richard Wagner")
            if e:
                return {**e, "theme": "Wagner Vednesday"}
        return None

    def pick(self, conn, day: dt.date, done: dict) -> dict | None:
        mem = self.remembering(conn, day)
        if mem:
            return mem
        cands = self.candidates(conn, done)
        theme = self.themed(cands, day)
        if theme:
            return theme
        kinds = [k for k in KINDS if cands[k]]
        if not kinds:
            return None
        n = day.toordinal()
        kind = kinds[n % len(kinds)]
        lst = self._order(cands[kind])
        return lst[(n // len(kinds)) % len(lst)]

    def build(self, conn, day: dt.date, done: dict, summaries: Callable) -> dict | None:
        e = self.pick(conn, day, done)
        if not e:
            return None
        remembering = "died" in e
        pids = list(e["pids"])
        dates = dict(conn.execute(f"SELECT id, date FROM performances WHERE id IN ({','.join('?' * len(pids))})", pids).fetchall())

        # finished ones first, pictured ones first among those, newest first within each (stable sorts)
        ordered = sorted(pids, key=lambda p: dates.get(p) or "", reverse=True)
        ordered.sort(key=lambda p: (p not in done, not done.get(p, (0, False))[1]))
        if not remembering:
            ordered = [p for p in ordered if p in done]
        items = summaries(conn, ordered[:MAX_ITEMS])
        count = len(pids)
        kind = e["kind"]
        if remembering:
            sub = " · ".join(x for x in [f"Died {_nice(e['died'])}", _plural(count)] if x)
        elif kind == "venue":
            sub = " · ".join(x for x in [e.get("city"), _plural(count)] if x)
        elif kind == "city":
            top = sorted(e["venues"].items(), key=lambda kv: -kv[1])[:2]
            sub = " · ".join([_plural(count)] + [v for v, _ in top])
        else:
            sub = " · ".join(x for x in [str(e.get("years") or "").strip() or None, _plural(count)] if x)
        blurb = e.get("description")
        if remembering and e.get("desc"):
            blurb = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", e["desc"]).replace("''", "")
            blurb = re.sub(r"^\d+,\s*", "", blurb).strip()
            blurb = blurb[:1].upper() + blurb[1:]
        image = e.get("image_url") or next((i.get("artwork_url") for i in items if i.get("artwork_url")), None)
        label = {"composer": "Composer", "performer": "Performer", "venue": "Venue", "city": "City"}[kind]
        return {
            "mode": "remembering" if remembering else "spotlight",
            "kind": kind, "id": e["id"], "name": e["name"],
            "eyebrow": "Remembering" if remembering else e.get("theme") or f"Today's Spotlight · {label}",
            "title": f"Remembering {e['name']}" if remembering else e["name"],
            "subtitle": sub, "blurb": blurb, "image_url": image, "count": count,
            "date": day.isoformat(), "died": e["died"].isoformat() if remembering else None,
            "items": items,
        }
