"""Whisper cross-check: does the sung text at a section's start match the reference title's incipit?

Optional pass after the batch, run with the MAIN venv (mlx-whisper lives there), at nice 10 because
the announcer Whisper shards share the GPU:

    nice -n 10 .venv/bin/python -m pipeline.segment.textcheck [--ids ...] [--limit 300]

For each music section labelled from a reference whose title carries words ("Celeste Aida",
"'Suona ogni labbro il mio nome'"), a 35 s clip from the section start is transcribed and fuzzily
matched against the first words of the incipit. Results are cached in
data/cache/segment/text/<pid>.json and applied to the sections file in place; build.py re-applies them
on every rebuild (see apply()).

* match (>= half of the incipit's first 6 words found)  -> confidence +0.15, source "+text"
* clear lyrics that don't match                          -> confidence -0.1, flagged for review
* nothing intelligible (orchestral start, bad audio)     -> no change
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = DATA / "sections"
TEXT_DIR = DATA / "cache" / "segment" / "text"
CLIP_S = 35.0
_STOP = {"act", "atto", "akt", "acte", "one", "two", "three", "four", "prologue", "scene", "scena", "no",
         "coro", "chorus", "e", "il", "la", "le", "di", "der", "die", "das", "und", "the", "and", "of", "a"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z' ]+", " ", s).replace("'", " ")


def incipit_words(title: str | None) -> list[str]:
    if not title:
        return []
    q = re.findall(r"['\"‘“«]([^'\"’”»]{4,})['\"’”»]", title)
    t = q[0] if q else re.sub(r"\([^)]*\)", " ", title)
    t = re.sub(r"^(?:(?:act|atto|akt|acte)\s+\w+|prologue|prolog)\s*[-:]\s*", "", t, flags=re.I)
    words = [w for w in _norm(t).split() if len(w) > 1 and w not in _STOP]
    return words[:6]


def match_score(words: list[str], heard: str) -> float:
    hw = _norm(heard).split()
    if not words or not hw:
        return 0.0
    hit = sum(1 for w in words if difflib.get_close_matches(w, hw, n=1, cutoff=0.75))
    return hit / len(words)


def apply(doc: dict, results: dict) -> int:
    """Apply cached text results ({"<track_id>@<start_s>": {...}}) to a sections doc. Returns count."""
    n = 0
    for s in doc.get("sections", []):
        r = results.get(f"{s['track_id']}@{s['start_s']:.1f}")
        if not r or s.get("kind") in ("applause", "announcer", "intermission", "interview", "other"):
            continue
        s["text_check"] = r
        conf = s.get("confidence") or 0.0
        if r["verdict"] == "match":
            conf = min(1.0, conf + 0.15)
            if s.get("boundaries") and s["boundaries"].get("start"):
                b = s["boundaries"]["start"]
                if "+text" not in b["source"]:
                    b["source"] += "+text"
        elif r["verdict"] == "mismatch":
            conf = max(0.0, conf - 0.1)
        s["confidence"] = round(conf, 3)
        s["needs_review"] = conf < 0.5
        n += 1
    return n


def _clip(local: Path, start: float, dur: float) -> "np.ndarray":
    import numpy as np
    p = subprocess.run(["/opt/homebrew/bin/ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
                        "-i", str(local), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(p.stdout, np.float32)


def check(pid: str, budget: int, log=print) -> int:
    from . import features as F
    from pipeline.announcers import transcribe

    p = OUT / f"{pid}.json"
    doc = json.loads(p.read_text())
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    cache_p = TEXT_DIR / f"{pid}.json"
    results = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    todo: dict[str, list[dict]] = {}
    for s in doc["sections"]:
        if s.get("label_source") != "reference" or s.get("part", 1) != 1:
            continue
        key = f"{s['track_id']}@{s['start_s']:.1f}"
        if key in results or len(incipit_words(s.get("title"))) < 2:
            continue
        todo.setdefault(s["file"], []).append(s)
    done = 0
    for file, secs in todo.items():
        if done >= budget:
            break
        local = F.fetch(file)
        try:
            for s in secs:
                if done >= budget:
                    break
                audio = _clip(local, s["start_s"], min(CLIP_S, max(5.0, s["end_s"] - s["start_s"])))
                segs = transcribe(audio, None) if len(audio) > 16000 else []
                heard = " ".join(x["text"] for x in segs)
                words = incipit_words(s["title"])
                sc = match_score(words, heard)
                real = len(_norm(heard).split()) >= 4 and (segs and min(x["avg_logprob"] for x in segs) > -1.0)
                verdict = "match" if sc >= 0.5 else ("mismatch" if real and sc == 0 else "unclear")
                results[f"{s['track_id']}@{s['start_s']:.1f}"] = {
                    "verdict": verdict, "score": round(sc, 2), "heard": heard[:200], "incipit": " ".join(words),
                    "language": segs[0]["lang"] if segs else None, "source": "text"}
                done += 1
                log(f"[{pid}] {verdict:8s} {sc:.2f}  {' '.join(words)[:40]!r} <- {heard[:60]!r}")
        finally:
            local.unlink(missing_ok=True)
    cache_p.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    if apply(doc, results):
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
        tmp.replace(p)
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--limit", type=int, default=300, help="max clips this run")
    a = ap.parse_args(argv)
    ids = a.ids or sorted(f.stem for f in OUT.glob("*.json"))
    left = a.limit
    for pid in ids:
        if left <= 0:
            break
        try:
            left -= check(pid, left)
        except Exception as e:  # keep going
            print(f"[{pid}] text check failed: {e!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
