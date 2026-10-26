"""Collect, per Optoberfest work, every eligible recording and the spooky passages in its broadcast transcript.

Output feeds the Codex judging step (data/themes/spooky_versions/). Read-only: reads the live server catalog and local transcripts.
"""
import json
import os, re, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORDS = re.compile(r"ghost|spirit|phantom|apparition|spectr|specter|haunt|witch|devil|demon|satan|hell\b|infern|"
                   r"underworld|furies|fury\b|curse|supernatural|eerie|spook|creep|horror|terrif|skull|skelet|grave|"
                   r"tomb|graveyard|cemeter|corpse|dead\b|undead|vampire|magic|sorcer|enchant|wolf.s glen|samiel|"
                   r"blood|stab|dagger|shadow|nightmare|monster|dragon|hallow", re.I)
QUAR = {"cso-0603-haitink", "cso-0606-beethoven-9", "bbcconcert-121117-missa", "phila-040115-sawallisch",
        "vienna-401105-missa", "bso-221029-uchida", "bso-230211-bso", "bbcconcert-050419-sq", "phila-021215-messiah",
        "vienna-401105-missa"}

def hits(pid, limit=40):
    p = ROOT / "data/transcripts" / f"{pid}.txt"
    if not p.exists():
        return None
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    out, part, last = [], "", -10
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            part = ln[3:].strip()
        elif WORDS.search(ln) and i > last + 3:
            ctx = " ".join(l.split("  ", 1)[-1] for l in lines[max(0, i - 2): i + 3] if not l.startswith("## "))
            out.append({"part": part, "at": ln.split("  ", 1)[0], "text": ctx[:500]})
            last = i
            if len(out) >= limit:
                break
    return out

def main():
    tags = json.loads((ROOT / "ios/Sethster/Resources/optoberfest.json").read_text())
    reasons = {json.loads(l)["work_id"]: json.loads(l) for l in open(ROOT / "data/themes/supernatural_tags.jsonl")}
    server = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SETHSTER_SERVER", "")
    if not server:
        sys.exit("usage: spooky_candidates.py <server url>  (or set SETHSTER_SERVER)")
    out = ROOT / "data/themes/spooky_versions/candidates.jsonl"
    n = 0
    with out.open("w") as f:
        for t in tags:
            w = t["work_id"]
            # The live catalog, not the Mac's data/sethster.db (stale); same eligibility as the app's resolver.
            try:
                with urllib.request.urlopen(f"{server}/api/works/{w}", timeout=20) as r:
                    rows = json.load(r).get("performances", [])
            except OSError as e:
                print("skip", w, e, file=sys.stderr)
                rows = []
            perfs = []
            for p in rows:
                pid = p["id"]
                if (p.get("confirmed_ratio") or 0) < 0.5 or pid in QUAR:
                    continue
                perfs.append({"performance_id": pid, "title": p.get("title"), "subtitle": p.get("subtitle"),
                              "date": p.get("date"), "collection": p.get("collection"), "transcript_hits": hits(pid)})
            src = reasons.get(w, {})
            f.write(json.dumps({"work_id": w, "value": t["value"], "reason": src.get("reason"),
                                "hint": t.get("hint"), "performances": perfs}, ensure_ascii=False) + "\n")
            n += 1
    print(n, "works ->", out)

if __name__ == "__main__":
    main()
