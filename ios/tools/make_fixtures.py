#!/usr/bin/env python3
"""Build API-shaped (docs/API.md) PerformanceDetail fixtures from data/pilot/results/*.json.

The Mock API client in the app derives home / composers / works / people from these.
Usage: python3 ios/tools/make_fixtures.py [ids...]
"""
import json, os, re, sys, unicodedata, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data/pilot/results"
OUT = ROOT / "ios/Sethster/Resources/Fixtures"
DEFAULT = ["met-160409-boccanegra", "vienna-840322-boccanegra", "proms-150822-prom49",
           "met-920307-rigoletto", "nypo-160322-boulez", "bayreuth-76-walkuere"]

def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def last(name):
    return slug(name.split()[-1]) if name else "anon"

def dur(text):
    m = re.search(r"\((\d+):(\d\d)(?::(\d\d))?", text or "")
    if not m: return None
    a, b, c = m.groups()
    return (int(a) * 3600 + int(b) * 60 + int(c)) if c else int(a) * 60 + int(b)

def segment(text):
    t = (text or "").lower()
    head = t[:60]
    if "intermission" in head or "interval" in head: return "intermission"
    if "interview" in head: return "interview"
    if head.startswith(("applause", "curtain call")): return "applause"
    if any(k in head for k in ("announce", "host", "presenter", "billboard", "opening remarks", "broadcast opening", "sign-off")):
        return "announcer"
    return "music"

def match_work(s, program):
    if not s: return None
    sl = s.lower()
    for p in program:
        w = p["work"].lower()
        core = re.split(r"[,(:]", w)[0].strip()
        if w in sl or (core and core in sl) or sl.split(":")[-1].strip()[:12] in w:
            return p["_id"]
    return None

def convert(pid):
    d = json.loads((SRC / f"{pid}.json").read_text())
    perf = d["performance"]
    program = d["program"]
    for p in program:
        p["_id"] = f"{last(p['composer'])}-{slug(p['work'])}"[:80]
    composers, seen = [], set()
    for p in program:
        cid = slug(p["composer"])
        if cid not in seen:
            seen.add(cid); composers.append({"id": cid, "name": p["composer"]})
    venue = {"id": slug(perf["venue"]), "name": perf["venue"], "city": perf.get("city")}
    credits = []
    for c in d["credits"]:
        credits.append({
            "person": {"id": slug(c["name"]), "name": c["name"], "image_url": None},
            "role": c["role"],
            "work_id": match_work(c.get("work"), program) if len(program) > 1 else program[0]["_id"],
            "status": c["status"], "notes": c.get("notes"), "source_ids": c.get("source_ids", []),
        })
    tracks = []
    for i, t in enumerate(d["track_map"], 1):
        tracks.append({
            "id": f"{pid}:{i:02d}", "index": i, "description": t["content"],
            "title": re.sub(r"\s*\(\d{1,2}:\d{2}[^)]*\)", "", t["content"]).strip(),
            "work_id": match_work(t.get("work"), program),
            "segment": segment(t["content"]), "duration_s": dur(t["content"]) or 600,
            "stream_url": f"/api/tracks/{pid}:{i:02d}/stream",
            "format": t["file"].rsplit(".", 1)[-1].lower(),
        })
    conf = sum(1 for c in credits if c["status"] == "confirmed") / max(1, len(credits))
    main = program[0]["work"] if len(program) == 1 else (perf.get("event_name") or program[0]["work"])
    heads = [c["name"] for c in d["credits"] if c["role"] not in ("orchestra", "chorus", "production") and c["status"] == "confirmed"]
    cond = next((c["name"] for c in d["credits"] if c["role"] == "conductor"), None)
    sub = [n.split()[-1] for n in heads if n != cond][:3]
    subtitle = " · ".join(x for x in [", ".join(sub), cond.split()[-1] if cond else ""] if x)
    return {
        "id": pid, "title": main, "subtitle": subtitle, "composers": composers,
        "date": perf["date"], "date_status": perf["date_status"], "venue": venue, "kind": d["kind"],
        "collection": d["track_map"][0]["file"].split("/")[0] if "/" in d["track_map"][0]["file"] else "NY Phil",
        "duration_s": sum(t["duration_s"] for t in tracks), "artwork_url": None,
        "confirmed_ratio": round(conf, 2),
        "summary": d["summary"], "date_evidence": perf.get("date_evidence"),
        "candidate_dates": perf.get("candidate_dates", []), "broadcast_date": perf.get("broadcast_date"),
        "broadcaster": perf.get("broadcaster"), "event_name": perf.get("event_name"),
        "program": [{"work_id": p["_id"], "title": p["work"], "composer": {"id": slug(p["composer"]), "name": p["composer"]},
                     "catalogue": p.get("catalogue") or None, "notes": p.get("notes")} for p in program],
        "credits": credits, "tracks": tracks,
        "sources": [{k: s.get(k) for k in ("id", "url", "wayback_url", "title", "type", "date_specific", "excerpt")} for s in d["sources"]],
        "resources": [{k: r.get(k) for k in ("url", "wayback_url", "title", "kind", "description")} for r in d["resources"]],
        "open_questions": d["open_questions"],
        "folder": d["track_map"][0]["file"].rsplit("/", 1)[0],
    }

SERVER = os.environ.get("SETHSTER_SERVER", "http://localhost:8642")

def fetch(path):
    """GET a Phase 3 endpoint from the running server; None on 404 / server down."""
    try:
        with urllib.request.urlopen(SERVER + "/api" + path, timeout=10) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

def local_transcript(pid):
    """Until the server serves /transcripts: data/transcripts/<id>.json in the API.md Transcript shape
    (segments mapped from file to track via the research track_map; unmapped ones dropped)."""
    src = ROOT / "data/transcripts" / f"{pid}.json"
    if not src.exists(): return None
    raw = json.loads(src.read_text())
    tm = json.loads((SRC / f"{pid}.json").read_text())["track_map"]
    index = {t["file"]: i for i, t in enumerate(tm, 1)}
    segs = [{"track_id": f"{pid}:{index[s['file']]:02d}", "track_index": index[s["file"]],
             "start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip(),
             "language": s.get("lang"), "confidence": s.get("avg_logprob")}
            for s in raw.get("segments", []) if s.get("file") in index and s.get("text", "").strip()]
    segs.sort(key=lambda s: (s["track_index"], s["start"]))
    if not segs: return None
    return {"performance_id": pid, "model": raw.get("model"), "segments": segs,
            "unmapped_segments": len(raw.get("segments", [])) - len(segs)}

def phase3(pid, det):
    """Liner notes, transcript, program notes and versions for the demo, as the server serves them."""
    got = []
    for name, path in ((f"{pid}.liner-notes", f"/performances/{pid}/liner-notes"), (f"{pid}.transcript", f"/transcripts/{pid}")):
        d = fetch(path)
        if d is None and name.endswith(".transcript"):
            d = local_transcript(pid)
        if d is not None:
            (OUT / f"{name}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1)); got.append(name)
    for ref in (fetch(f"/programs/{pid}") or {}).get("items", []):
        d = fetch(f"/programs/{pid}/{ref['n']}")
        if d is not None:
            (OUT / f"{pid}.program-{ref['n']}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1)); got.append(f"program {ref['n']}")
    for item in det["program"]:
        w = item["work_id"]
        d = fetch(f"/works/{w}/versions")
        if d is not None and d.get("items"):
            # Keep only rows the demo can open.
            d["items"] = [r for r in d["items"] if r.get("performance_id") in DEMO_IDS]
            (OUT / f"work-{w}.versions.json").write_text(json.dumps(d, ensure_ascii=False, indent=1)); got.append(f"versions {w}")
    return got

DEMO_IDS = set()

if __name__ == "__main__":
    ids = sys.argv[1:] or DEFAULT
    DEMO_IDS.update(ids)
    OUT.mkdir(parents=True, exist_ok=True)
    qs = []
    for pid in ids:
        det = convert(pid)
        (OUT / f"{pid}.json").write_text(json.dumps(det, ensure_ascii=False, indent=1))
        extra = phase3(pid, det)
        print(pid, "phase 3:", ", ".join(extra) or "none (endpoints missing or server down)")
        for q in det["open_questions"][:2]:
            qs.append({"performance_id": pid, "performance_title": det["title"], "question": q, "answer": None, "answered_at": None})
    (OUT / "questions.json").write_text(json.dumps({"items": qs}, ensure_ascii=False, indent=1))
    (OUT / "index.json").write_text(json.dumps(ids))
    print("wrote", len(ids), "fixtures to", OUT)
