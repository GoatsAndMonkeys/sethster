"""Write short, cited historical-context paragraphs with the codex and gemini CLIs, in parallel.

    uv run python -m pipeline.context.generate                       # all works + performances not done yet
    uv run python -m pipeline.context.generate --kind works --limit 10 --codex 2 --gemini 2
    uv run python -m pipeline.context.generate --force --only met-160409-boccanegra

Inputs: data/context/facts/** and people.json (pipeline.context.facts), the DB, data/context/dates.json.
The model gets ONLY numbered source texts (Wikidata facts, Wikipedia sections, the research record's
sources and excerpts) and must cite them; no web search. Each paragraph is then checked: every year
and number in it must occur in a cited source text (check = "checked"), else "unchecked". Paragraphs
citing nothing valid are dropped.

Outputs: data/context/notes/works/<work_id>.json, data/context/notes/performances/<id>.json
Throughput: one line per call in data/context/logs/calls.jsonl, a summary in data/context/throughput.json
(items/hour per backend = finished items / that backend's wall-clock hours).
"""
from __future__ import annotations

import argparse
import json
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sethster_server.context import career_stage, nice_date, season_label
from sethster_server.perfdates import compute_all

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CTX = DATA / "context"
WORK = DATA / "cache" / "context_work"  # scratch dirs for the CLIs
FORMAT_VERSION = 1
NUM = re.compile(r"\b\d{3,4}\b")
QUOTA = re.compile(r"\b429\b|RESOURCE_EXHAUSTED|quota|usage limit|rate limit|too many requests|hit your limit", re.I)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M)
    start = text.find("{")
    while start >= 0:
        depth, instr, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if instr:
                esc = (ch == "\\" and not esc)
                if ch == '"' and not esc:
                    instr = False
                continue
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


# ---- prompts -------------------------------------------------------------------------------------------

RULES = """Rules:
- Use ONLY the numbered sources below. Do not use outside knowledge. If the sources do not support a
  section, set it to null. Never guess dates, names, places or numbers.
- Every section is 1-3 plain-English sentences, at most 70 words, no hype, no superlatives that are
  not in the sources, no Markdown.
- "cites" lists the source labels (like "S2") that support the sentences. Cite only sources that
  really contain the facts.
- Reply with ONE JSON object and nothing else."""


def source_block(srcs: list[dict]) -> str:
    out = []
    for i, s in enumerate(srcs, 1):
        out.append(f"[S{i}] {s.get('title') or s['id']} <{s.get('url') or 'no url'}>\n{(s.get('text') or '').strip()}")
    return "\n\n".join(out)


def work_item(wf: dict, years: dict[int, dict]) -> dict | None:
    srcs = [s for s in wf.get("sources") or [] if s.get("text")]
    y = (wf.get("facts") or {}).get("year")
    if y and y in years:
        srcs += [s for s in years[y].get("sources") or [] if s.get("text")]
    if not any(s["kind"] == "wikipedia" for s in srcs):
        return None
    comp = (wf.get("composer") or {}).get("name") or "the composer"
    prompt = f"""You write liner-note context for a classical-music listening app.

Work: "{wf['title']}" by {comp}.

Write JSON with these keys:
{{"premiere": {{"text": "...", "cites": ["S1"]}} or null,   // when and where it was first performed, and by whom (singers, conductor, soloist) if the sources say
 "reception": {{...}} or null,      // how it was received at the premiere and soon after
 "composer_life": {{...}} or null,  // what was going on in {comp}'s life when it was written / first performed
 "world": {{...}} or null}}         // one or two things happening the same year (in music or the wider world) from the year sources; say "the same year", do not claim a connection

{RULES}

Sources:

{source_block(srcs)}
"""
    return {"kind": "works", "id": wf["work_id"], "prompt": prompt, "sources": srcs,
            "meta": {"work_id": wf["work_id"], "qid": wf.get("qid"), "year": y}}


def perf_item(conn, pid: str, dates: dict, people: dict, venues: dict, years: dict) -> dict | None:
    p = conn.execute("""SELECT p.*, v.name vname, v.city vcity, v.wikidata vqid FROM performances p
                        LEFT JOIN venues v ON v.id=p.venue_id WHERE p.id=?""", (pid,)).fetchone()
    if not p:
        return None
    d = dates.get(pid) or {}
    date = d.get("date") or p["date"]
    year = int(date[:4]) if date and date[:4].isdigit() else None
    research = [s for s in json.loads(p["sources"] or "[]") if isinstance(s, dict) and s.get("id")]
    srcs = []
    for s in research:
        txt = " ".join(x for x in [s.get("excerpt"), s.get("notes")] if x)
        srcs.append({"id": s["id"], "kind": "research", "title": s.get("title"), "url": s.get("url"),
                     "wayback_url": s.get("wayback_url"), "text": txt[:1500] or "(no excerpt)"})
    credits = conn.execute("""SELECT c.role, c.status, c.notes, pe.name, pe.wikidata, pe.kind FROM credits c
                              JOIN people pe ON pe.id=c.person_id WHERE c.performance_id=? ORDER BY c.ord""",
                           (pid,)).fetchall()
    cast_lines, stage_lines = [], []
    for c in credits[:30]:
        line = f"- {c['name']}: {c['role'] or ''} ({c['status']})" + (f" -- note: {c['notes']}" if c["notes"] else "")
        cast_lines.append(line)
        st = career_stage(people.get(c["wikidata"]) if c["wikidata"] else None, year, c["role"])
        if st and c["kind"] != "ensemble" and len(stage_lines) < 10:
            stage_lines.append(f"- {c['name']}: {st['text']}")
    season = season_label(date, p["collection"], p["event_name"], p["vname"])
    others = []
    if date and len(date) >= 7 and p["collection"]:
        y0 = int(date[:4])
        for o in conn.execute("""SELECT title, date FROM performances WHERE collection=? AND id<>? AND
                                 substr(date,1,4) IN (?,?,?) ORDER BY date""",
                              (p["collection"], pid, str(y0 - 1), str(y0), str(y0 + 1))):
            others.append(f"- {o['title']} ({o['date']})")
    record = (f"Recording: {p['title']} ({p['subtitle'] or ''})\nDate: {nice_date(date)} ({d.get('status')}, "
              f"{d.get('precision')} precision){'; aired ' + nice_date(d['aired']) if d.get('aired') else ''}\n"
              f"Venue: {p['vname'] or 'unknown'}, {p['vcity'] or ''}\nCollection folder: {p['collection']}\n"
              f"Event: {p['event_name'] or ''}\nBroadcaster: {p['broadcaster'] or ''}\nSeason (computed): {season or ''}\n"
              f"Summary: {p['summary'] or ''}\nCredits:\n" + "\n".join(cast_lines) +
              ("\nOther recordings in the same collection within a year:\n" + "\n".join(others[:12]) if others else ""))
    srcs.insert(0, {"id": "record", "kind": "record", "title": f"Sethster research record {pid}", "url": None,
                    "text": record})
    if stage_lines:
        srcs.append({"id": "computed:careers", "kind": "computed", "title": "Career stage computed from Wikidata/Wikipedia years",
                     "url": None, "text": "\n".join(stage_lines)})
    vf = venues.get(p["vqid"]) if p["vqid"] else None
    for s in (vf or {}).get("sources") or []:
        if s.get("text") and s["kind"] == "wikipedia":
            srcs.append(s)
    for s in (years.get(year) or {}).get("sources") or []:
        if s.get("scope") == "music" and s.get("text"):
            srcs.append(s)
    prompt = f"""You write liner-note context for a classical-music listening app. This is about the night
(or afternoon) of one live recording in a family collection of off-air broadcasts.

Write JSON with these keys:
{{"season": {{"text": "...", "cites": ["S1"]}} or null,   // the season or festival edition and what the house / company / orchestra was like then (music director, notable features of that season)
 "debuts_farewells": {{...}} or null,  // notable debuts, farewells, role debuts, cast changes or anniversaries THAT date -- only if a source says so
 "that_year": {{...}} or null}}        // other productions or concerts by this house/company that season or year, or notable events in music that year

Credit each performer only with what the sources say about THIS date. The career-stage lines are computed
estimates: you may mention them ("then in her early career") but cite that source.

{RULES}

Sources:

{source_block(srcs)}
"""
    return {"kind": "performances", "id": pid, "prompt": prompt, "sources": srcs, "meta": {"performance_id": pid}}


# ---- validation -------------------------------------------------------------------------------------------

def finalize(item: dict, obj: dict) -> tuple[dict, dict]:
    label = {f"S{i}": s for i, s in enumerate(item["sources"], 1)}
    sections, stats = {}, {"sections": 0, "checked": 0, "dropped": 0}
    for key, sec in (obj or {}).items():
        if not isinstance(sec, dict):
            continue
        text = (sec.get("text") or "").strip()
        cites = [c.strip() for c in (sec.get("cites") or []) if isinstance(c, str)]
        cited = [label[c] for c in cites if c in label]
        if not text or not cited:
            if text:
                stats["dropped"] += 1
            continue
        blob = " ".join((s.get("text") or "") + " " + (s.get("title") or "") for s in cited)
        nums = set(NUM.findall(text))
        missing = sorted(n for n in nums if n not in blob)
        sections[key] = {"text": text, "cites": list(dict.fromkeys(s["id"] for s in cited)),
                         "check": "checked" if not missing else "unchecked",
                         **({"unverified_numbers": missing} if missing else {})}
        stats["sections"] += 1
        stats["checked"] += int(not missing)
    srcs = [{k: v for k, v in s.items() if k != "text"} | {"excerpt": (s.get("text") or "")[:300]}
            for s in item["sources"] if s["id"] in {c for sec in sections.values() for c in sec["cites"]}]
    return sections, {"stats": stats, "sources": srcs}


# ---- backends --------------------------------------------------------------------------------------------

def call_codex(prompt: str, wd: Path, timeout: int = 600) -> tuple[str, dict]:
    out = wd / "codex.last.txt"
    out.unlink(missing_ok=True)
    p = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-C", str(wd), "-o", str(out),
                        "-"], input=prompt, capture_output=True, text=True, timeout=timeout)
    text = out.read_text() if out.exists() else ""
    m = re.search(r"tokens used\s*\n?\s*([\d,]+)", p.stdout + p.stderr)
    if not text.strip() and QUOTA.search(p.stderr[-3000:] + p.stdout[-3000:]):
        raise QuotaHit((p.stderr + p.stdout)[-300:])
    return text, {"tokens": int(m.group(1).replace(",", "")) if m else None, "rc": p.returncode,
                  "model": "codex-default"}


def call_gemini(prompt: str, wd: Path, timeout: int = 600) -> tuple[str, dict]:
    p = subprocess.run(["gemini", "--skip-trust", "--output-format", "json", "-p", prompt],
                       cwd=wd, capture_output=True, text=True, timeout=timeout)
    doc = extract_json(p.stdout) or {}
    resp = doc.get("response") if isinstance(doc.get("response"), str) else ""
    models = list(((doc.get("stats") or {}).get("models") or {}).keys())
    if not resp and QUOTA.search(p.stderr[-3000:] + json.dumps(doc.get("error") or "")):
        raise QuotaHit(p.stderr[-300:])
    toks = sum(int((m.get("tokens") or {}).get("total", 0) or 0)
               for m in ((doc.get("stats") or {}).get("models") or {}).values())
    return resp, {"tokens": toks, "rc": p.returncode, "model": ",".join(models)}


class QuotaHit(Exception):
    pass


SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
TOPIC_PATTERNS = {
    "premiere": re.compile(r"\bpremi[eè]r|\bfirst (?:public )?perform|\bfirst (?:staged|given|heard)|\bopened\b|\bconducted\b", re.I),
    "reception": re.compile(r"\breception|\breceived\b|\bsuccess|\bfailure|\bfailed\b|\bcritic|\bacclaim|\bovation|"
                            r"\bfiasco|\bpopular|\bhostil|\benthusias|\bwarmly|\bcoolly|\breview", re.I),
}


def _sentences(text: str) -> list[str]:
    out = []
    for para in re.split(r"\n+", text or ""):
        para = para.strip()
        if not para or para.startswith("[") and para.endswith("]"):
            continue
        out += [re.sub(r"\s+", " ", x).strip() for x in SENT.split(para) if 30 <= len(x.strip()) <= 400]
    return out


def extractive(item: dict) -> dict:
    """No-LLM backend: quote up to two source sentences per topic, verbatim (always 'checked')."""
    srcs = {f"S{i}": s for i, s in enumerate(item["sources"], 1)}
    work_wp = [k for k, s in srcs.items() if s["kind"] == "wikipedia" and not s.get("scope")]
    obj: dict = {}
    if item["kind"] != "works":
        return obj
    year = str(item["meta"].get("year") or "")
    used: set[str] = set()
    for topic, pat in TOPIC_PATTERNS.items():
        picks = []
        for k in work_wp[:1]:
            for sent in _sentences(srcs[k]["text"]):
                if pat.search(sent) and sent not in used:
                    picks.append(sent)
                    used.add(sent)
                if len(picks) >= 2:
                    break
        if picks:
            obj[topic] = {"text": " ".join(picks), "cites": work_wp[:1]}
    # the composer's article paragraphs around that year (second Wikipedia source, if any)
    for k in work_wp[1:2]:
        sents = [x for x in _sentences(srcs[k]["text"]) if year and year in x and x not in used]
        if sents:
            obj["composer_life"] = {"text": " ".join(sents[:2]), "cites": [k]}
    for k, s in srcs.items():
        if s.get("scope") == "music":
            lines = [ln.strip("-*• ").strip() for ln in (s.get("text") or "").splitlines()
                     if 25 <= len(ln.strip()) <= 260 and not ln.startswith("[")]
            if lines:
                obj["world"] = {"text": f"The same year in music: {lines[0].rstrip('.')}.", "cites": [k]}
            break
    return obj


def call_extract(item: dict, wd: Path) -> tuple[str, dict]:
    return json.dumps(extractive(item)), {"tokens": 0, "rc": 0, "model": "extractive"}


BACKENDS = {"codex": call_codex, "gemini": call_gemini, "extract": call_extract}


# ---- runner ----------------------------------------------------------------------------------------------

class Runner:
    def __init__(self, items: list[dict], workers: dict[str, int]):
        self.q: queue.Queue = queue.Queue()
        for it in items:
            it["tried"] = []
            self.q.put(it)
        self.workers = workers
        self.lock = threading.Lock()
        self.parked: set[str] = set()
        self.log = (CTX / "logs")
        self.log.mkdir(parents=True, exist_ok=True)
        self.calls = self.log / "calls.jsonl"
        self.stats = {b: {"calls": 0, "ok": 0, "failed": 0, "seconds": 0.0, "first": None, "last": None,
                          "sections": 0, "checked": 0, "tokens": 0, "workers": n} for b, n in workers.items()}
        self.t0 = time.time()
        self.pending = len(items)

    def worker(self, backend: str, n: int):
        wd = WORK / f"{backend}-{n}"
        wd.mkdir(parents=True, exist_ok=True)
        while True:
            if backend in self.parked:
                return
            try:
                it = self.q.get(timeout=2)
            except queue.Empty:
                with self.lock:
                    if self.pending <= 0:
                        return
                continue
            if backend in it["tried"] and len(it["tried"]) < len(self.workers) and \
                    any(b not in it["tried"] and b not in self.parked for b in self.workers):
                self.q.put(it)  # let the other backend try it
                time.sleep(0.5)
                continue
            t = time.time()
            ok, err, info, stats = False, None, {}, {}
            try:
                text, info = (call_extract(it, wd) if backend == "extract" else BACKENDS[backend](it["prompt"], wd))
                obj = extract_json(text)
                if not obj:
                    raise ValueError(f"no JSON in reply: {text[:200]!r}")
                sections, extra = finalize(it, obj)
                stats = extra["stats"]
                doc = {"format_version": FORMAT_VERSION, **it["meta"], "sections": sections,
                       "sources": extra["sources"], "backend": backend, "model": info.get("model"),
                       "generated_at": now(), "seconds": round(time.time() - t, 1), "stats": stats}
                out = CTX / "notes" / it["kind"] / f"{it['id']}.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.with_suffix(".tmp").write_text(json.dumps(doc, ensure_ascii=False, indent=1))
                out.with_suffix(".tmp").replace(out)
                ok = True
            except QuotaHit as e:
                err = f"quota: {e}"
                with self.lock:
                    self.parked.add(backend)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:400]
            dt_s = time.time() - t
            with self.lock:
                s = self.stats[backend]
                s["calls"] += 1
                s["seconds"] += dt_s
                s["first"] = s["first"] or t
                s["last"] = time.time()
                s["tokens"] += int(info.get("tokens") or 0)
                if ok:
                    s["ok"] += 1
                    s["sections"] += stats.get("sections", 0)
                    s["checked"] += stats.get("checked", 0)
                    self.pending -= 1
                else:
                    s["failed"] += 1
                    it["tried"].append(backend)
                    if len(it["tried"]) >= 3 or all(b in it["tried"] or b in self.parked for b in self.workers):
                        self.pending -= 1
                    else:
                        self.q.put(it)
                with self.calls.open("a") as fh:
                    fh.write(json.dumps({"at": now(), "backend": backend, "worker": n, "kind": it["kind"],
                                         "id": it["id"], "ok": ok, "seconds": round(dt_s, 1), "error": err,
                                         "model": info.get("model"), "tokens": info.get("tokens"), **stats}) + "\n")
                done = sum(x["ok"] for x in self.stats.values())
                print(f"[{backend}#{n}] {'ok ' if ok else 'ERR'} {it['kind'][:4]} {it['id']} {dt_s:.0f}s"
                      f"{'' if ok else ' ' + (err or '')[:120]}  (done {done}, left {self.pending})", flush=True)

    def run(self) -> dict:
        threads = [threading.Thread(target=self.worker, args=(b, i), daemon=True)
                   for b, n in self.workers.items() for i in range(n)]
        for t in threads:
            t.start()
            time.sleep(1.5)
        for t in threads:
            t.join()
        return self.summary()

    def summary(self) -> dict:
        wall = time.time() - self.t0
        out = {"finished_at": now(), "wall_seconds": round(wall), "backends": {}}
        for b, s in self.stats.items():
            span = (s["last"] - s["first"]) if s["first"] else 0
            out["backends"][b] = {
                "workers": s["workers"], "calls": s["calls"], "ok": s["ok"], "failed": s["failed"],
                "mean_seconds_per_call": round(s["seconds"] / s["calls"], 1) if s["calls"] else None,
                "items_per_hour": round(s["ok"] / (span / 3600), 1) if span > 60 else None,
                "items_per_hour_per_worker": round(s["ok"] / (span / 3600) / s["workers"], 1) if span > 60 else None,
                "sections": s["sections"], "sections_checked": s["checked"], "tokens": s["tokens"]}
        tot = sum(s["ok"] for s in self.stats.values())
        out["total"] = {"ok": tot, "items_per_hour": round(tot / (wall / 3600), 1) if wall > 60 else None}
        return out


def needs(path: Path, force: bool, upgrade: bool) -> bool:
    if force or not path.exists():
        return True
    if upgrade:
        try:
            return json.loads(path.read_text()).get("backend") == "extract"
        except ValueError:
            return True
    return False


def load_dir(d: Path) -> dict:
    out = {}
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out[f.stem] = json.loads(f.read_text())
        except ValueError:
            pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="works,performances")
    ap.add_argument("--only", default="", help="comma-separated work/performance ids")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--upgrade", action="store_true", help="also redo items whose notes came from the extractive backend")
    # small caps by default: codex is shared with researchd and with programming work
    ap.add_argument("--codex", type=int, default=0, help="codex workers (keep <= 2)")
    ap.add_argument("--gemini", type=int, default=1, help="gemini workers")
    ap.add_argument("--extract", type=int, default=0, help="no-LLM extractive workers (quotes sources verbatim)")
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{DATA / 'sethster.db'}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    years = {int(k): v for k, v in load_dir(CTX / "facts" / "years").items()}
    only = {x for x in a.only.split(",") if x}
    items = []
    if "works" in a.kind:
        for wid, wf in load_dir(CTX / "facts" / "works").items():
            if only and wid not in only:
                continue
            if not needs(CTX / "notes" / "works" / f"{wid}.json", a.force, a.upgrade):
                continue
            it = work_item(wf, years)
            if it:
                items.append(it)
    if "performances" in a.kind:
        people = (json.loads((CTX / "people.json").read_text()).get("items") or {}) if (CTX / "people.json").exists() else {}
        venues = load_dir(CTX / "facts" / "venues")
        dates = compute_all(conn, DATA / "transcripts")
        # main collection order: confirmed/reliable dates first (On This Day pairs with these)
        pids = sorted(dates, key=lambda p: (not dates[p]["reliable"], p))
        for pid in pids:
            if only and pid not in only:
                continue
            if not needs(CTX / "notes" / "performances" / f"{pid}.json", a.force, a.upgrade):
                continue
            it = perf_item(conn, pid, dates, people, venues, years)
            if it:
                items.append(it)
    if a.limit:
        items = items[:a.limit]
    workers = {b: n for b, n in (("codex", min(a.codex, 2)), ("gemini", min(a.gemini, 3)), ("extract", a.extract))
               if n > 0}
    print(f"{len(items)} items; workers {workers}", flush=True)
    if not items:
        return
    summary = Runner(items, workers).run()
    hist = CTX / "throughput.json"
    prev = json.loads(hist.read_text()) if hist.exists() else {"runs": []}
    prev["runs"].append({"kind": a.kind, "items": len(items), **summary})
    hist.write_text(json.dumps(prev, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
