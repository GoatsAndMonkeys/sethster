"""In Memoriam + dated calendar events (run daily, e.g. from cron / Synology Task Scheduler).

    uv run python -m pipeline.context.memoriam                 # fresh Wikidata P570 check for every performer
    uv run python -m pipeline.context.memoriam --obituaries    # + recent-obituary web search (codex, then gemini)
    uv run python -m pipeline.context.memoriam --feeds         # + Wikipedia "Deaths in <month>" + obituary RSS (no AI)
    uv run python -m pipeline.context.memoriam --ring          # refresh Ring-cycle events (codex web search)

* P570 (date of death) is fetched UNCACHED for every performer QID in the collection (50 per request)
  and written back into data/context/people.json; a death that is new since the last run is added to
  data/calendar/memoriam.json with status "wikidata".
* --obituaries asks a search-capable CLI for deaths in the last 60 days among living performers with
  at least 2 recordings. A hit is kept only if its URL loads and mentions the surname; it is stored
  with status "reported" until Wikidata confirms it (the server prefers Wikidata).
* --feeds (plain HTTP, no LLM; cache in ~/.cache/sethster-obits): (1) Wikipedia "Deaths in <Month> <Year>" for the
  current + previous 2 months, entries resolved to QIDs via pageprops and matched exactly -> "confirmed";
  (2) classical/obituary RSS feeds filtered by death keywords and matched on accent-folded full name
  -> "reported" (one outlet) / "reported_multi" (>=2 outlets). Every source goes in item["sources"].
  Status rank: reported < reported_multi < confirmed <= wikidata; a status is never downgraded.
* --ring asks for officially announced Ring cycles at major houses (dates + official URL); entries
  whose URL loads are written to data/calendar/events.json (kind "ring_cycle"), with a Wayback link.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from pipeline.context.generate import extract_json
from pipeline.enrich_wiki import USER_AGENT, wd_time

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CTX = DATA / "context"
CAL = DATA / "calendar"
WORK = DATA / "cache" / "context_work" / "memoriam"
WAYBACK = "https://web.archive.org/web/{}"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.with_suffix(".tmp").write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    p.with_suffix(".tmp").replace(p)


def fresh_deaths(qids: list[str]) -> dict[str, str | None]:
    out = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
        for i in range(0, len(qids), 50):
            chunk = qids[i:i + 50]
            r = c.get("https://www.wikidata.org/w/api.php", params={
                "action": "wbgetentities", "ids": "|".join(chunk), "props": "claims", "format": "json"})
            r.raise_for_status()
            for q, ent in (r.json().get("entities") or {}).items():
                vals = [wd_time(cl["mainsnak"]["datavalue"]["value"]) for cl in (ent.get("claims") or {}).get("P570", [])
                        if cl.get("rank") != "deprecated" and cl.get("mainsnak", {}).get("datavalue")]
                vals = [v for v in vals if v]
                out[q] = min(vals) if vals else None
            time.sleep(0.6)
    return out


def check_wikidata() -> list[dict]:
    pj = load(CTX / "people.json", {"items": {}})
    people = pj.get("items") or {}
    performers = [q for q, p in people.items() if "performer" in (p.get("kinds") or ["performer"])]
    deaths = fresh_deaths(sorted(performers))
    mem = load(CAL / "memoriam.json", {"items": []})
    known = {m["qid"] for m in mem["items"]}
    new = []
    for q, d in deaths.items():
        p = people.get(q)
        if not p:
            continue
        if d and d != p.get("death"):
            p["death"] = d
            p["death_year"] = int(d[:4])
        if d and q not in known and d >= (date.today() - timedelta(days=365)).isoformat():
            m = {"qid": q, "name": p.get("name"), "died": d, "status": "wikidata", "found_at": now(),
                 "source_title": f"Wikidata: {p.get('name')} (P570 date of death)",
                 "source_url": f"https://www.wikidata.org/wiki/{q}",
                 "wayback_url": WAYBACK.format(f"https://www.wikidata.org/wiki/{q}")}
            mem["items"].append(m)
            new.append(m)
        elif d:
            for m in mem["items"]:
                if m["qid"] == q and m.get("status") == "reported":
                    m["status"], m["died"] = "confirmed", d
    pj["checked_deaths_at"] = now()
    save(CTX / "people.json", pj)
    mem["checked_at"] = now()
    save(CAL / "memoriam.json", mem)
    print(f"P570 checked for {len(deaths)} performers; new deaths in the last year: {[m['name'] for m in new]}")
    return new


def search_cli(prompt: str) -> str:
    """codex with live web search; gemini (google_web_search) as a fallback."""
    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / "last.txt"
    out.unlink(missing_ok=True)
    p = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-C", str(WORK),
                        "-c", 'web_search="live"', "-o", str(out), "-"], input=prompt, capture_output=True,
                       text=True, timeout=1200)
    if out.exists() and out.read_text().strip():
        return out.read_text()
    p = subprocess.run(["gemini", "--skip-trust", "--yolo", "--output-format", "json", "-p", prompt], cwd=WORK,
                       capture_output=True, text=True, timeout=1200)
    doc = extract_json(p.stdout) or {}
    return doc.get("response") or ""


def url_mentions(url: str, needle: str | None) -> bool:
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)
    except httpx.HTTPError:
        return False
    if r.status_code != 200:
        return False
    return not needle or needle.lower() in r.text.lower()


def obituaries() -> None:
    pj = load(CTX / "people.json", {"items": {}})
    conn = sqlite3.connect(f"file:{DATA / 'sethster.db'}?mode=ro", uri=True)
    counts = dict(conn.execute("""SELECT p.wikidata, COUNT(DISTINCT c.performance_id) FROM people p JOIN credits c
                                  ON c.person_id=p.id WHERE p.wikidata IS NOT NULL GROUP BY p.wikidata"""))
    alive = [p for q, p in (pj.get("items") or {}).items()
             if not p.get("death") and counts.get(q, 0) >= 2 and (p.get("birth_year") or 0) < 1975]
    names = "\n".join(f"- {p['name']} (born {p.get('birth_year')}, {p['qid']})" for p in alive)
    today = date.today().isoformat()
    prompt = f"""Today is {today}. Search the web for obituaries or death announcements published in the last
60 days for any of these classical musicians. Only report a death if you find a reliable news or
institutional page that says so. Reply with ONE JSON object only:
{{"deaths": [{{"qid": "Q...", "name": "...", "died": "YYYY-MM-DD", "source_title": "...", "source_url": "https://..."}}]}}
Use an empty list if you find none.

{names}
"""
    obj = extract_json(search_cli(prompt)) or {}
    mem = load(CAL / "memoriam.json", {"items": []})
    known = {m["qid"] for m in mem["items"]}
    by_q = {p["qid"]: p for p in alive}
    added = []
    for d in obj.get("deaths") or []:
        q, url = d.get("qid"), d.get("source_url")
        if q not in by_q or q in known or not url or not re.match(r"\d{4}-\d{2}(-\d{2})?$", d.get("died") or ""):
            continue
        surname = by_q[q]["name"].split()[-1]
        if not url_mentions(url, surname):
            continue
        added.append({"qid": q, "name": by_q[q]["name"], "died": d["died"], "status": "reported", "found_at": now(),
                      "source_title": d.get("source_title"), "source_url": url, "wayback_url": WAYBACK.format(url)})
    mem["items"] += added
    mem["obituaries_checked_at"] = now()
    save(CAL / "memoriam.json", mem)
    print(f"obituary search over {len(alive)} performers: added {[a['name'] for a in added]}")

# ---------- --feeds: Wikipedia "Deaths in <month>" + obituary RSS (no AI) ----------
CACHE = Path.home() / ".cache" / "sethster-obits"
FEEDS = {  # verified 2026-10-18; Gramophone/Limelight (403), Classic FM/Bachtrack/BBC Music Mag/Opera News (no usable RSS) are not usable
    "The Guardian (classical)": "https://www.theguardian.com/music/classical-music-and-opera/rss",
    "The Guardian (obituaries)": "https://www.theguardian.com/tone/obituaries/rss",
    "The Guardian (music)": "https://www.theguardian.com/music/rss",
    "Slipped Disc": "https://slippedisc.com/feed/",
    "OperaWire": "https://operawire.com/feed/",
    "Opera magazine": "https://www.operamag.com/feed",
    "VAN Magazine": "https://van-magazine.com/feed",
    "Musical America": "https://www.musicalamerica.com/rss",
    "NYT (music)": "https://rss.nytimes.com/services/xml/rss/nyt/Music.xml",
    "NYT (obituaries)": "https://rss.nytimes.com/services/xml/rss/nyt/Obituaries.xml",
    "LA Times (obituaries)": "https://www.latimes.com/obituaries/rss2.0.xml",
    "The Telegraph (obituaries)": "https://www.telegraph.co.uk/obituaries/rss.xml",
    "The Arts Desk": "https://www.theartsdesk.com/rss.xml",
    "San Francisco Classical Voice": "https://www.sfcv.org/rss.xml",
}
DEATH_RE = re.compile(r"\b(died|dies|dead|death|obituary|obituaries|rip|passed away|has passed|in memoriam|mourns?|"
                      r"1[89]\d\d\s*[–—-]\s*20[12]\d)\b", re.I)
RANK = {"reported": 1, "reported_multi": 2, "confirmed": 3, "wikidata": 3}


def get_cached(url: str, params: dict | None = None, ttl: int = 10800) -> str | None:
    """GET with a polite UA and an on-disk cache (a stale copy is used if the fetch fails)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / hashlib.md5((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        return f.read_text()
    try:
        r = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT, "Api-User-Agent": USER_AGENT},
                      timeout=30, follow_redirects=True)
        r.raise_for_status()
        f.write_text(r.text)
        return r.text
    except httpx.HTTPError as e:
        print(f"  fetch failed {url}: {e}", file=sys.stderr)
        return f.read_text() if f.exists() else None


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").replace("ı", "i").replace("ł", "l").replace("ø", "o"))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def outlet(url: str) -> str:
    return ".".join((urlparse(url).hostname or url).split(".")[-2:])


def wiki_deaths(months: int = 3) -> list[dict]:
    """[{title, qid, died, page}] from Wikipedia 'Deaths in <Month> <Year>' (first link of each bullet)."""
    today, ents = date.today(), []
    for k in range(months):
        y, m = divmod(today.year * 12 + today.month - 1 - k, 12)
        d0 = date(y, m + 1, 1)
        page = f"Deaths in {d0:%B %Y}"
        txt = get_cached("https://en.wikipedia.org/w/api.php", {"action": "parse", "page": page, "prop": "wikitext",
                                                                 "format": "json", "formatversion": 2})
        try:
            wt = json.loads(txt)["parse"]["wikitext"]
        except (TypeError, ValueError, KeyError):
            continue
        day = None
        for line in wt.splitlines():
            h = re.match(r"=== *(\d{1,2}) *===\s*$", line)
            if h:
                day = int(h.group(1))
            b = re.match(r"\*+\s*\[\[([^\]|#]+)", line)
            if b and day:
                try:
                    ents.append({"title": b.group(1).strip(), "died": date(d0.year, d0.month, day).isoformat(),
                                 "page": page})
                except ValueError:
                    pass
    qid_of, titles = {}, sorted({e["title"] for e in ents})
    for i in range(0, len(titles), 50):
        txt = get_cached("https://en.wikipedia.org/w/api.php", {
            "action": "query", "titles": "|".join(titles[i:i + 50]), "redirects": 1, "prop": "pageprops",
            "ppprop": "wikibase_item", "format": "json", "formatversion": 2})
        try:
            q = json.loads(txt)["query"]
        except (TypeError, ValueError, KeyError):
            continue
        alias = {x["from"]: x["to"] for x in (q.get("normalized") or []) + (q.get("redirects") or [])}
        item = {p["title"]: (p.get("pageprops") or {}).get("wikibase_item") for p in q.get("pages") or []}
        for t in titles[i:i + 50]:
            t1 = alias.get(t, t)
            qid_of[t] = item.get(alias.get(t1, t1))
    for e in ents:
        e["qid"] = qid_of.get(e["title"])
    return ents


def feed_items(name: str, url: str) -> list[dict]:
    txt = get_cached(url)
    try:
        root = ET.fromstring(re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)\w+;", " ",
                                    txt[max(txt.find("<" + "?xml"), 0):]).encode())
    except (AttributeError, ET.ParseError):
        return []
    out = []
    for it in root.iter():
        if it.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        g = {c.tag.rsplit("}", 1)[-1]: c for c in it}
        link = (g["link"].text or g["link"].get("href")) if "link" in g else None
        ds = next((g[k].text for k in ("pubDate", "published", "updated", "date") if k in g and g[k].text), "")
        try:
            d = parsedate_to_datetime(ds).date()
        except (TypeError, ValueError):
            try:
                d = date.fromisoformat(ds.strip()[:10])
            except ValueError:
                continue
        summ = re.sub(r"<[^>]+>", " ", " ".join((g[k].text or "") for k in ("description", "summary") if k in g))
        title = ((g["title"].text or "") if "title" in g else "").strip()
        if link and title:
            out.append({"outlet": name, "title": title, "url": link.strip(), "date": d.isoformat(), "summary": summ[:500]})
    return out


def name_hits(title: str, summary: str, names: dict[str, list[str]]) -> list[str]:
    """QIDs whose full folded name is in the title, or in the summary lead next to a death word."""
    t, s = f" {fold(title)} ", f" {fold(summary[:400])} "
    return [q for q, ns in names.items()
            if any(f" {n} " in t or (f" {n} " in s and DEATH_RE.search(summary[:400])) for n in ns)]


def feeds() -> None:
    pj = load(CTX / "people.json", {"items": {}}).get("items") or {}
    conn = sqlite3.connect(f"file:{DATA / 'sethster.db'}?mode=ro", uri=True)
    ours, names = {}, {}
    for nm, q in conn.execute("SELECT name, wikidata FROM people WHERE kind='person' AND wikidata IS NOT NULL"):
        if "performer" not in ((pj.get(q) or {}).get("kinds") or ["performer"]):
            continue
        ours[q] = nm
        toks = fold(nm).split()
        if len(toks) >= 2:                                       # never match a lone token (surname alone)
            names[q] = [" ".join(toks)] + ([f"{toks[0]} {toks[-1]}"] if len(toks) > 2 else [])
    alive = {q: n for q, n in names.items() if not (pj.get(q) or {}).get("death")}
    found: dict[str, dict] = {}                                  # qid -> {died, wiki, sources[]}
    cutoff = (date.today() - timedelta(days=120)).isoformat()
    for e in wiki_deaths():
        if e["qid"] in ours:
            f = found.setdefault(e["qid"], {"sources": [], "wiki": False})
            f["wiki"], f["died"] = True, e["died"]
            f["sources"].append({"outlet": "Wikipedia", "title": e["page"], "date": e["died"],
                                 "url": "https://en.wikipedia.org/wiki/" + e["page"].replace(" ", "_")})
    ok = {}
    for nm, url in FEEDS.items():
        its = feed_items(nm, url)
        ok[nm] = len(its)
        for it in its:
            if it["date"] < cutoff or not DEATH_RE.search(it["title"] + " " + it["summary"][:400]):
                continue
            for q in name_hits(it["title"], it["summary"], alive):
                f = found.setdefault(q, {"sources": [], "wiki": False})
                if not any(s["url"] == it["url"] for s in f["sources"]):
                    f["sources"].append({k: it[k] for k in ("outlet", "title", "url", "date")})
    mem = load(CAL / "memoriam.json", {"items": []})
    by_q = {m["qid"]: m for m in mem["items"]}
    for q, f in found.items():
        rss = [s for s in f["sources"] if s["outlet"] != "Wikipedia"]
        status = "confirmed" if f["wiki"] else ("reported_multi" if len({outlet(s["url"]) for s in rss}) >= 2 else "reported")
        approx = not f["wiki"]
        died = f.get("died") or min(s["date"] for s in rss)
        m = by_q.get(q)
        if not m:
            m = by_q[q] = {"qid": q, "name": ours[q], "status": None, "found_at": now()}
            mem["items"].append(m)
        have = {s["url"] for s in m.get("sources") or []}
        m["sources"] = (m.get("sources") or []) + [s for s in f["sources"] if s["url"] not in have]
        if RANK[status] > RANK.get(m["status"], 0):
            s0 = f["sources"][0] if f["wiki"] else min(rss, key=lambda s: s["date"])
            m.update(status=status, source_title=s0["title"], source_url=s0["url"], wayback_url=WAYBACK.format(s0["url"]))
            if not approx or not m.get("died"):
                m["died"] = died
                m["died_approx"] = approx
        elif m.get("died_approx") and not approx:
            m["died"], m["died_approx"] = died, False
    mem["feeds_checked_at"], mem["feeds_ok"] = now(), ok
    save(CAL / "memoriam.json", mem)
    print("feeds:", ", ".join(f"{k}={v}" for k, v in ok.items()))
    horizon = (date.today() - timedelta(days=90)).isoformat()
    for m in sorted(mem["items"], key=lambda m: m.get("died") or "", reverse=True):
        if (m.get("died") or "") >= horizon:
            print(f"  {m['died']}{'~' if m.get('died_approx') else ' '} {m['status']:<15} {m['name']}  "
                  f"[{', '.join(sorted({s['outlet'] for s in m.get('sources') or []})) or 'wikidata'}]")


def ring_events() -> None:
    today = date.today().isoformat()
    prompt = f"""Today is {today}. Search the web for complete cycles of Wagner's Der Ring des Nibelungen
(all four operas performed as a cycle) that are officially announced or running at major opera houses
or festivals (for example the Metropolitan Opera, Bayreuth Festival, Wiener Staatsoper, Royal Opera House,
Deutsche Oper Berlin, Staatsoper Berlin, Bayerische Staatsoper, La Scala, Lyric Opera of Chicago,
San Francisco Opera, Seattle Opera) between {today} and 18 months from now, plus any cycle that ended in
the last 30 days. Use the house's own season page as the source. Reply with ONE JSON object only:
{{"events": [{{"house": "...", "title": "Ring cycle 1", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
  "source_title": "...", "url": "https://..."}}]}}
One entry per cycle. Do not include single operas outside a cycle. Do not guess dates."""
    obj = extract_json(search_cli(prompt)) or {}
    cur = load(CAL / "events.json", {"format_version": 1, "events": []})
    keep = [e for e in cur["events"] if e.get("kind") != "ring_cycle" or e.get("manual")]
    added = []
    for e in obj.get("events") or []:
        if not (e.get("url") and re.match(r"\d{4}-\d{2}-\d{2}$", e.get("start") or "")):
            continue
        if not url_mentions(e["url"], None):
            print("  url did not load, skipped:", e["url"], file=sys.stderr)
            continue
        added.append({"id": f"ring:{re.sub(r'[^a-z0-9]+', '-', (e.get('house') or '').lower()).strip('-')}:{e['start']}",
                      "kind": "ring_cycle", "house": e.get("house"), "title": e.get("title") or "Ring cycle",
                      "start": e["start"], "end": e.get("end") or e["start"], "lead_days": 30,
                      "source_title": e.get("source_title"), "url": e["url"], "wayback_url": WAYBACK.format(e["url"]),
                      "status": "announced", "found_by": "codex web search", "checked_at": now()})
    cur["events"] = keep + added
    cur["updated_at"] = now()
    save(CAL / "events.json", cur)
    print(f"ring cycles: {len(added)} -> {CAL / 'events.json'}")
    for e in added:
        print("  ", e["house"], e["start"], e["end"], e["url"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--obituaries", action="store_true")
    ap.add_argument("--feeds", action="store_true")
    ap.add_argument("--ring", action="store_true")
    ap.add_argument("--no-wikidata", action="store_true")
    a = ap.parse_args()
    if not a.no_wikidata:
        check_wikidata()
    if a.obituaries:
        obituaries()
    if a.feeds:
        feeds()
    if a.ring:
        ring_events()


if __name__ == "__main__":
    main()
