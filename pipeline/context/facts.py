"""Historical-context facts: structured Wikidata claims + Wikipedia text, cached, every item sourced.

    uv run python -m pipeline.context.facts            # all works / venues / people / years in the DB
    uv run python -m pipeline.context.facts --only works

Writes (format_version 1; regenerated on each run, do not hand-edit):

  data/context/facts/works/<work_id>.json   premiere (P1191 date, P4647 place), inception (P571),
                                            publication (P577), librettists, plus source texts:
                                            the work's Wikipedia sections about composition /
                                            premiere / reception, the composer's article paragraphs
                                            around the premiere year, and the year articles.
  data/context/facts/venues/<QID>.json      the house: opened, history section of its article
  data/context/facts/years/<YYYY>.json      "<YYYY> in music" and "<YYYY>" Events sections
  data/context/people.json                  {QID: birth/death, work period start/end (P2031/P2032),
                                            debut/retirement years found in the Wikipedia lead}

Every source is {id, kind, title, url, wayback_url, text}; ids are stable (wd:Q.., wp:<lang>:<title>).
HTTP goes through pipeline.enrich_wiki.Http (cached in data/cache/wiki_http, polite per host).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

from pipeline.enrich_wiki import WD, Http as _Http, claim_qids, claim_values, first_time, now, year_of

SF_DATALESS = 0x40000000  # macOS: an iCloud-evicted placeholder; reading it can block indefinitely


def is_dataless(p: Path) -> bool:
    try:
        return bool(os.lstat(p).st_flags & SF_DATALESS)
    except (OSError, AttributeError):
        return False


class Http(_Http):
    """enrich_wiki's cached Http, but a cache entry that iCloud has evicted is treated as missing and the
    fresh copy goes to data/cache/context_http/wiki_alt (reading the placeholder would hang)."""

    def _path(self, url: str) -> Path:
        p = super()._path(url)
        if is_dataless(p):
            return DATA / "cache" / "context_http" / "wiki_alt" / p.parent.name / p.name
        return p

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = DATA / "context"
FACTS = OUT / "facts"
WAYBACK = "https://web.archive.org/web/{}"
FORMAT_VERSION = 1
MISMATCHES: list[dict] = []

WORK_SECTIONS = re.compile(r"premi|first perform|performance history|composition|background|history|reception|"
                           r"origin|genesis|creation|writing|critical|context|commission|later perform", re.I)
SKIP_SECTIONS = re.compile(r"synopsis|plot|roles|recording|instrumentation|scoring|discography|references|"
                           r"notes|sources|external|further|see also|bibliography|cast|libretto$", re.I)


def wp_url(lang: str, title: str) -> str:
    return f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"


def src(sid: str, kind: str, title: str, url: str, text: str | None = None) -> dict:
    return {"id": sid, "kind": kind, "title": title, "url": url, "wayback_url": WAYBACK.format(url),
            "text": text}


def sections(text: str) -> list[tuple[int, str, str]]:
    parts = re.split(r"\n(={2,6})\s*(.*?)\s*\1\n", "\n" + (text or ""))
    out = [(1, "Lead", parts[0].strip())]
    out += [(len(parts[i]), parts[i + 1].strip(), parts[i + 2].strip()) for i in range(1, len(parts) - 2, 3)]
    return out


def cap(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + " …"


async def wp_extract(http: Http, lang: str, title: str) -> str:
    """Full plaintext article (same request as enrich_wiki's want_full, so it is usually cached)."""
    body = await http.get_json(f"https://{lang}.wikipedia.org/w/api.php", {
        "action": "query", "prop": "extracts", "explaintext": "1", "exsectionformat": "wiki",
        "titles": title, "redirects": "1", "format": "json", "formatversion": "2"})
    pages = (body or {}).get("query", {}).get("pages", [])
    return (pages[0].get("extract") or "") if pages and not pages[0].get("missing") else ""


def sitelink(ent: dict | None) -> tuple[str, str] | None:
    for lang in ("en", "de", "it", "fr", "es"):
        sl = ((ent or {}).get("sitelinks") or {}).get(f"{lang}wiki")
        if sl:
            return lang, sl["title"]
    return None


def qualifier_qids(ent: dict, pid: str, qual: str) -> list[str]:
    out = []
    for c in (ent.get("claims") or {}).get(pid, []):
        for q in (c.get("qualifiers") or {}).get(qual, []):
            v = (q.get("datavalue") or {}).get("value")
            if isinstance(v, dict) and v.get("id"):
                out.append(v["id"])
    return out


# ---- works -----------------------------------------------------------------------------------------------

async def work_facts(http: Http, wd: WD, row: sqlite3.Row, composer_qid: str | None, years: set[int]) -> dict | None:
    qid = row["wikidata"]
    ent = await wd.entity(qid)
    if not ent:
        return None
    if composer_qid and composer_qid not in claim_qids(ent, "P86"):
        # a wrong QID in the DB (e.g. Lohengrin -> Q207587 Rochefort): never build context on it
        MISMATCHES.append({"work_id": row["id"], "qid": qid, "composer_qid": composer_qid,
                           "p86": claim_qids(ent, "P86")})
        return None
    wd_url = f"https://www.wikidata.org/wiki/{qid}"
    sources = [src(f"wd:{qid}", "wikidata", f"Wikidata: {row['title']} ({qid})", wd_url)]
    prem_date = first_time(ent, "P1191")
    place_q = claim_qids(ent, "P4647") + qualifier_qids(ent, "P1191", "P276")
    lib_q = claim_qids(ent, "P87")
    perf_q = qualifier_qids(ent, "P1191", "P175")  # performers at the premiere
    await wd.fetch_labels(set(place_q + lib_q + perf_q))
    places = [{"qid": q, "label": wd.label(q)} for q in dict.fromkeys(place_q)]
    facts = {
        "premiere_date": prem_date, "premiere_place": places, "premiere_place_check": None,
        "premiere_performers": [{"qid": q, "label": wd.label(q)} for q in dict.fromkeys(perf_q)],
        "inception": first_time(ent, "P571"), "publication_date": first_time(ent, "P577"),
        "librettists": [{"qid": q, "label": wd.label(q)} for q in lib_q],
    }
    wdtext = []
    if prem_date:
        wdtext.append(f"date of first performance (P1191): {prem_date}")
    if places:
        wdtext.append("location of first performance (P4647): " + ", ".join(p["label"] or p["qid"] for p in places))
    if facts["premiere_performers"]:
        wdtext.append("performers at the first performance (P175 on P1191): " +
                      ", ".join(p["label"] or p["qid"] for p in facts["premiere_performers"]))
    if facts["inception"]:
        wdtext.append(f"inception (P571): {facts['inception']}")
    if facts["publication_date"]:
        wdtext.append(f"publication date (P577): {facts['publication_date']}")
    if facts["librettists"]:
        wdtext.append("librettist (P87): " + ", ".join(l["label"] or l["qid"] for l in facts["librettists"]))
    sources[0]["text"] = "; ".join(wdtext) or None

    sl = sitelink(ent)
    if sl:
        text = await wp_extract(http, *sl)
        keep, total = [], 0
        for level, head, body in sections(text):
            if not body or SKIP_SECTIONS.search(head):
                continue
            if head == "Lead" or WORK_SECTIONS.search(head):
                piece = f"[{head}]\n{cap(body, 2500)}"
                if total + len(piece) > 8000:
                    break
                keep.append(piece)
                total += len(piece)
        if keep:
            sources.append(src(f"wp:{sl[0]}:{sl[1]}", "wikipedia", f"Wikipedia: {sl[1]}", wp_url(*sl), "\n\n".join(keep)))
        # Wikidata premiere places are sometimes wrong or list several premieres (Boccanegra 1857 vs 1881):
        # keep the place(s) named in a Wikipedia sentence together with the premiere year
        py = (prem_date or "")[:4]
        if py and places:
            sents = [x for x in re.split(r"(?<=[.!?])\s+", text) if py in x]
            def hit(label):
                core = re.sub(r"^(teatro|theatre|théâtre|theater|the)\s+", "", (label or "").lower())
                return bool(core) and any(core in x.lower() for x in sents)
            good = [p for p in places if hit(p["label"])]
            if good:
                facts["premiere_place"] = good
                facts["premiere_place_check"] = "wikipedia_agrees"
            else:
                facts["premiere_place_check"] = "wikidata_only"
        if py and facts["premiere_performers"]:
            sents = [x.lower() for x in re.split(r"(?<=[.!?])\s+", text) if py in x]
            raw = facts["premiere_performers"]
            facts["premiere_performers"] = [p for p in raw if p["label"] and
                                            any(p["label"].split()[-1].lower() in x for x in sents)]
            facts["premiere_performers_unverified"] = [p for p in raw if p not in facts["premiere_performers"]]
            sources[0]["text"] = re.sub(r"; performers at the first performance \(P175 on P1191\): [^;]*", "",
                                        sources[0]["text"] or "")
            if facts["premiere_performers"]:
                sources[0]["text"] += "; performers at the first performance (P175 on P1191): " + \
                    ", ".join(p["label"] for p in facts["premiere_performers"])
    # the composer's life around the premiere (or composition) year
    year = year_of(prem_date) or year_of(facts["inception"]) or year_of(facts["publication_date"])
    facts["year"] = year
    if composer_qid and year:
        cent = await wd.entity(composer_qid)
        csl = sitelink(cent)
        if csl:
            ctext = await wp_extract(http, *csl)
            pat = re.compile(r"\b(" + "|".join(str(y) for y in range(year - 2, year + 2)) + r")\b")
            paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?==)", ctext) if pat.search(p) and len(p) > 80]
            if paras:
                sources.append(src(f"wp:{csl[0]}:{csl[1]}", "wikipedia", f"Wikipedia: {csl[1]}", wp_url(*csl),
                                   cap("\n\n".join(paras[:6]), 3500)))
        facts["composer_birth"] = first_time(cent or {}, "P569")
        facts["composer_death"] = first_time(cent or {}, "P570")
    if year:
        years.add(year)
    return {"format_version": FORMAT_VERSION, "work_id": row["id"], "qid": qid, "title": row["title"],
            "composer": {"id": row["composer_id"], "name": row["cname"], "qid": composer_qid},
            "facts": facts, "sources": sources, "fetched_at": now()}


# ---- years -------------------------------------------------------------------------------------------------

async def year_facts(http: Http, year: int) -> dict:
    sources = []
    for title, kind in ((f"{year} in music", "music"), (str(year), "world")):
        text = await wp_extract(http, "en", title)
        if not text:
            continue
        body = []
        for level, head, b in sections(text):
            if re.match(r"events|premieres|opera|by month|january|february|march|april|may|june|july|august|"
                        r"september|october|november|december|classical music|ongoing", head, re.I) and b:
                body.append(f"[{head}]\n{b}")
        if body:
            sources.append({**src(f"wp:en:{title}", "wikipedia", f"Wikipedia: {title}", wp_url("en", title),
                                  cap("\n\n".join(body), 5000)), "scope": kind})
    return {"format_version": FORMAT_VERSION, "year": year, "sources": sources, "fetched_at": now()}


# ---- venues -----------------------------------------------------------------------------------------------

async def venue_facts(http: Http, wd: WD, qid: str, name: str) -> dict | None:
    ent = await wd.entity(qid)
    if not ent:
        return None
    sources = [src(f"wd:{qid}", "wikidata", f"Wikidata: {name} ({qid})", f"https://www.wikidata.org/wiki/{qid}",
                   f"opened: {first_time(ent, 'P1619', 'P571')}")]
    sl = sitelink(ent)
    if sl:
        text = await wp_extract(http, *sl)
        keep = [f"[{h}]\n{cap(b, 2500)}" for lvl, h, b in sections(text)
                if b and (h == "Lead" or re.search(r"history|opening|season|company|tradition", h, re.I))]
        if keep:
            sources.append(src(f"wp:{sl[0]}:{sl[1]}", "wikipedia", f"Wikipedia: {sl[1]}", wp_url(*sl),
                               cap("\n\n".join(keep), 6000)))
    return {"format_version": FORMAT_VERSION, "qid": qid, "name": name, "opened": first_time(ent, "P1619", "P571"),
            "sources": sources, "fetched_at": now()}


# ---- people -----------------------------------------------------------------------------------------------

DEBUT = re.compile(r"[^.]*\bdebut\w*\b[^.]*\.", re.I)
RETIRE = re.compile(r"[^.]*\b(retire\w*|farewell|final (?:stage )?(?:appearance|performance)|last (?:stage )?"
                    r"(?:appearance|performance))\b[^.]*\.", re.I)
YEAR = re.compile(r"\b(1[89]\d\d|20[0-3]\d)\b")


def text_years(text: str, pat: re.Pattern) -> tuple[int | None, str | None]:
    best = None
    for m in pat.finditer(text or ""):
        sent = m.group(0).strip()
        ys = [int(y) for y in YEAR.findall(sent)]
        if ys:
            y = min(ys) if pat is DEBUT else max(ys)
            if best is None or (y < best[0] if pat is DEBUT else y > best[0]):
                best = (y, sent)
    return (best[0], cap(best[1], 400)) if best else (None, None)


def entities_batch(qids: list[str]) -> dict[str, dict]:
    """claims for many QIDs, 50 per request, cached per batch in data/cache/context_http (sync, polite)."""
    import hashlib

    import httpx

    from pipeline.enrich_wiki import USER_AGENT
    cache = DATA / "cache" / "context_http"
    cache.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict] = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
        for i in range(0, len(qids), 50):
            chunk = sorted(qids[i:i + 50])
            key = cache / (hashlib.sha1("|".join(chunk).encode()).hexdigest() + ".json")
            if key.exists():
                body = json.loads(key.read_text())
            else:
                for attempt in range(5):
                    r = c.get("https://www.wikidata.org/w/api.php", params={
                        "action": "wbgetentities", "ids": "|".join(chunk), "props": "claims", "format": "json"})
                    if r.status_code == 200:
                        break
                    time.sleep(5 * (attempt + 1))
                r.raise_for_status()
                body = r.json()
                key.write_text(json.dumps(body))
                time.sleep(0.6)
            for q, ent in (body.get("entities") or {}).items():
                if "missing" not in ent:
                    out[q] = ent
    return out


def precise_time(ent: dict, pid: str) -> str | None:
    """Earliest year, most precise value within it ('1813' and '1813-05-22' -> '1813-05-22')."""
    from pipeline.enrich_wiki import wd_time
    vals = [wd_time(v) for v in claim_values(ent, pid) if isinstance(v, dict) and "time" in v]
    vals = [v for v in vals if v]
    if not vals:
        return None
    y = min(v[:4] for v in vals)
    return max((v for v in vals if v[:4] == y), key=len)


def person_facts(ent: dict | None, qid: str, name: str) -> dict | None:
    if not ent:
        return None
    wf = DATA / "wiki" / "people" / f"{qid}.json"
    if not wf.exists():
        wf = DATA / "wiki" / "composers" / f"{qid}.json"
    wiki = {}
    if wf.exists() and not is_dataless(wf):
        try:
            wiki = json.loads(wf.read_text()).get("wikipedia") or {}
        except ValueError:
            pass
    intro = wiki.get("intro") or wiki.get("summary") or ""
    debut, debut_sent = text_years(intro, DEBUT)
    ret, ret_sent = text_years(intro, RETIRE)
    rec = {"qid": qid, "name": name, "birth": precise_time(ent, "P569"), "death": precise_time(ent, "P570"),
           "work_start": first_time(ent, "P2031"), "work_end": first_time(ent, "P2032"),
           "debut_year": debut, "debut_evidence": debut_sent, "retired_year": ret, "retired_evidence": ret_sent,
           "wikidata_url": f"https://www.wikidata.org/wiki/{qid}",
           "wikipedia_url": wiki.get("url"), "wikipedia_lang": wiki.get("lang")}
    rec["birth_year"] = year_of(rec["birth"])
    rec["death_year"] = year_of(rec["death"])
    return rec


# ---- main -------------------------------------------------------------------------------------------------

def write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


async def run(only: set[str]) -> None:
    conn = sqlite3.connect(f"file:{DATA / 'sethster.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    http = Http()
    wd = WD(http)
    years: set[int] = set()
    n = {"works": 0, "venues": 0, "people": 0, "years": 0}
    sem = asyncio.Semaphore(6)

    async def guarded(coro):
        async with sem:
            try:
                return await coro
            except Exception as e:  # noqa: BLE001
                print("  !", e, file=sys.stderr)
                return None

    if "works" in only:
        rows = conn.execute("""SELECT w.*, c.name AS cname, c.wikidata AS cqid FROM works w
                               LEFT JOIN composers c ON c.id=w.composer_id WHERE w.wikidata IS NOT NULL""").fetchall()
        res = await asyncio.gather(*(guarded(work_facts(http, wd, r, r["cqid"], years)) for r in rows))
        for r in res:
            if r:
                write(FACTS / "works" / f"{r['work_id']}.json", r)
                n["works"] += 1
        write(OUT / "facts" / "rejected_works.json", {"generated_at": now(), "reason": "work QID whose P86 composer "
                                                       "is not the program's composer", "items": MISMATCHES})
        print(f"works: {n['works']}/{len(rows)}; rejected QIDs: {len(MISMATCHES)}", file=sys.stderr)
    if "years" in only:
        for r in conn.execute("SELECT DISTINCT substr(date,1,4) FROM performances WHERE date GLOB '[12][0-9][0-9][0-9]*'"):
            years.add(int(r[0]))
        if not (FACTS / "works").exists():
            pass
        for f in (FACTS / "works").glob("*.json"):
            y = (json.loads(f.read_text()).get("facts") or {}).get("year")
            if y:
                years.add(y)
        res = await asyncio.gather(*(guarded(year_facts(http, y)) for y in sorted(years) if 1600 <= y <= 2030))
        for r in res:
            if r and r["sources"]:
                write(FACTS / "years" / f"{r['year']}.json", r)
                n["years"] += 1
        print(f"years: {n['years']}/{len(years)}", file=sys.stderr)
    if "venues" in only:
        rows = conn.execute("SELECT DISTINCT wikidata, name FROM venues WHERE wikidata IS NOT NULL").fetchall()
        res = await asyncio.gather(*(guarded(venue_facts(http, wd, r[0], r[1])) for r in rows))
        for r in res:
            if r:
                write(FACTS / "venues" / f"{r['qid']}.json", r)
                n["venues"] += 1
        print(f"venues: {n['venues']}/{len(rows)}", file=sys.stderr)
    if "people" in only:
        rows = conn.execute("""SELECT wikidata, name, 'performer' FROM people WHERE wikidata IS NOT NULL
                               AND kind='person' UNION
                               SELECT wikidata, name, 'composer' FROM composers WHERE wikidata IS NOT NULL""").fetchall()
        kinds: dict[str, set] = {}
        names: dict[str, str] = {}
        for q, nm, k in rows:
            kinds.setdefault(q, set()).add(k)
            names.setdefault(q, nm)
        ents = entities_batch(sorted(kinds))
        res = [person_facts(ents.get(q), q, names[q]) for q in kinds]
        people = {r["qid"]: {**r, "kinds": sorted(kinds[r["qid"]])} for r in res if r}
        write(OUT / "people.json", {"format_version": FORMAT_VERSION, "generated_at": now(), "items": people})
        n["people"] = len(people)
        print(f"people: {len(people)}/{len(rows)} (debut found: "
              f"{sum(1 for p in people.values() if p['debut_year'] or p['work_start'])})", file=sys.stderr)
    await http.client.aclose()
    print(json.dumps({"http": dict(http.stats), **n}), file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="works,years,venues,people")
    a = ap.parse_args()
    asyncio.run(run(set(a.only.split(","))))


if __name__ == "__main__":
    main()
