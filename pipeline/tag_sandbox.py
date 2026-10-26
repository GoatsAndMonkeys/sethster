"""Write researched classical-music metadata into the SANDBOX copy of the share.

    uv run python -m pipeline.tag_sandbox --pilot                # dry run, 26 pilots
    uv run python -m pipeline.tag_sandbox --pilot --diff met-220426-lucia
    uv run python -m pipeline.tag_sandbox --pilot --apply        # write

Safety (see pipeline/tagsafe.py): every write is guarded so it can only land
inside the read-write mount of the sandbox SMB share ($SETHSTER_SANDBOX_SHARE); any path with a
component named like the ORIGINAL share ($SETHSTER_ORIGINAL_SHARE) is refused.  Before a file is
modified its size, sha256 of first/last 1 MB, the raw leading tag region
(MP3/FLAC: byte-exact) and a readable tag dump are appended to
data/tagging/manifest/<performance id>.jsonl; pipeline/untag.py restores.
Dry run is the default.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

from mutagen.flac import FLAC
from mutagen.id3 import (COMM, GRP1, ID3, MVIN, MVNM, TALB, TCOM, TCON, TDRC, TIT1, TIT2,
                         TMCL, TPE1, TPE2, TPE3, TPOS, TRCK, TXXX, ID3NoHeaderError)
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from pipeline import tagsafe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
MANIFEST_DIR = os.path.join(DATA, "tagging", "manifest")
SOURCES = {
    "pilot": os.path.join(DATA, "pilot", "results"),
    "research": os.path.join(DATA, "research", "results"),
    "archives": os.path.join(DATA, "archives", "full"),
}
AUDIO_FMT = {".mp3": "mp3", ".flac": "flac", ".ogg": "ogg", ".oga": "ogg", ".opus": "opus",
             ".m4a": "m4a", ".mp4": "m4a", ".aac": "m4a"}
SKIP_AUDIO = {".wma", ".wav", ".aif", ".aiff"}
OK = {"confirmed", "inferred"}
TOOL = "sethster tag_sandbox v1"

# ------------------------------------------------------------------ loading

def synth_track_map(rec: dict, inv: dict) -> list[dict]:
    """Archive drafts carry no track_map: derive one from the inventory track list."""
    b = inv.get(rec["id"])
    if not b:
        return []
    prog = rec.get("program") or []
    work = prog[0]["work"] if len(prog) == 1 else None
    out = []
    for tr in b.get("tracks", []):
        stem = os.path.splitext(os.path.basename(tr["path"]))[0]
        e = {"file": tr["path"], "content": stem + " (track content not researched; from filename)"}
        if work:
            e["work"] = work
        out.append(e)
    return out


def load_records(sources: list[str], ids: set[str] | None, min_conf: str,
                 inv: dict | None = None) -> dict[str, dict]:
    rank = {"high": 3, "medium": 2, "low": 1}
    recs: dict[str, dict] = {}
    for src in sources:                      # earlier source wins
        for f in sorted(glob.glob(os.path.join(SOURCES[src], "*.json"))):
            try:
                d = json.load(open(f))
            except Exception:
                continue
            rid = d.get("id")
            if not rid or rid in recs or (ids and rid not in ids):
                continue
            if not d.get("track_map") and inv is not None and d.get("draft_meta") is not None:
                d["track_map"] = synth_track_map(d, inv)
                d["_synth_tracks"] = True
            if not d.get("track_map"):
                continue
            dm = d.get("draft_meta")
            if dm is not None and rank.get(dm.get("confidence"), 0) < rank[min_conf]:
                continue
            d["_source"] = src
            recs[rid] = d
    return recs


def load_inventory() -> dict[str, dict]:
    inv = {}
    p = os.path.join(DATA, "inventory", "broadcasts.jsonl")
    if os.path.exists(p):
        for line in open(p):
            b = json.loads(line)
            inv[b["id"]] = b
    return inv


def resolve(rel: str, rec: dict, inv: dict, mount: str) -> str | None:
    cand = [rel]
    dos = os.path.join(DATA, "pilot", "dossiers", rec["id"] + ".json")
    if os.path.exists(dos):
        try:
            cand.append(os.path.join(json.load(open(dos))["folder"], rel))
        except Exception:
            pass
    b = inv.get(rec["id"])
    if b:
        cand += [os.path.join(fd, rel) for fd in b.get("folders", [])]
        cand += [t["path"] for t in b.get("tracks", []) if t["path"].endswith("/" + rel)]
    for c in cand:
        if os.path.isfile(os.path.join(mount, c)):
            return c
    return None

# ------------------------------------------------------------------ text helpers

STOP = set("the a an of in and for no op nr k bwv in major minor la le les il lo di de del des der die das du a "
           "excerpt complete per listing to with on from by from".split())


def toks(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return {t for t in re.findall(r"[a-z0-9]+", s) if t not in STOP}


def match_program(text: str | None, program: list[dict]) -> int | None:
    if not text or not text.strip() or text.strip().startswith("("):
        return None
    if text.strip().lower().startswith("all "):
        return None
    tt = toks(text)
    best, best_s = None, 0.0
    for i, p in enumerate(program):
        wt = toks(p.get("work", "")) | toks(p.get("catalogue", ""))
        ov = len(tt & wt)
        if ov == 0:
            continue
        sur = toks((p.get("composer") or "").split()[-1] if p.get("composer") else "")
        s = ov + (0.5 if sur & tt else 0) + (0.25 * len({x for x in tt & wt if x.isdigit()}))
        if s > best_s:
            best, best_s = i, s
    if best is None and len(program) == 1:
        return 0
    return best


def credit_works(c: dict, program: list[dict]) -> set[int] | None:
    w = c.get("work")
    if not w or w.strip().lower().startswith("all "):
        return None
    out = set()
    for part in re.split(r";", w):
        i = match_program(part, program)
        if i is not None:
            out.add(i)
    return out or None


NONPERF = re.compile(r"director|design|costume|lighting|dramaturg|production|producer|engineer|"
                     r"announcer|\bhost\b|presenter|panelist|interview|commentator|guest|choreograph|"
                     r"chorus master|prompter|musical preparation|rehears|adaptation|^sets|designer|"
                     r"speaker|intermission|quiz|assistant conductor|choreographic assistant|dancer|acrobat|^leader$|^concertmaster$|"
                     r"projection|^lighting|^production")
BROADCAST = re.compile(r"announcer|\bhost\b|presenter|commentator|interviewer|narrator \(radio")
ENSEMBLE = re.compile(r"^(orchestra|chorus|choir|children's chorus|children's choir|ensemble|band|"
                      r"string quartet|quartet|consort)\b")


def category(role: str) -> str:
    r = (role or "").lower().strip()
    if r.startswith("conductor"):
        return "conductor"
    if re.search(r"chorus (master|director)|choir (master|director)|^director, ", r):
        return "production"
    if ENSEMBLE.match(r):
        return "ensemble"
    if BROADCAST.search(r):
        return "broadcast"
    if NONPERF.search(r):
        return "production"
    if re.search(r"principal|concertmaster|\bleader\b|section", r):
        return "section"
    return "performer"


ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}


ABBR = re.compile(r"(?:\b(?:No|Nos|Op|Mvt|Mvts|St|Dr|Mr|Mrs|vs|ca|cf|approx|nr|Nr)|(?:^|[;,]\s*)[IVX]+)$")
EVIDENCE = re.compile(r"^(?:The|This|That|These|There|It|Its|Inferred|Probably|Possibly|Presumably|Per|"
                      r"Based|Split|Content|Duration|Estimated|From|See|Not|Unconfirmed|Likely|Perhaps|"
                      r"Maybe|Whether|Source|Sources|Tags?|Filename|File|'A&C')\b|presumably|unconfirmed|"
                      r"not confirmed|inferred|\(s\d|\bs\d+\b")


def short_content(c: str) -> str:
    s = re.sub(r"\s*\[[^\[\]]*\]", "", c or "")
    s = re.sub(r"\s*\([^()]*\)", "", s)
    s = re.sub(r"\s*\([^()]*\)", "", s)                      # nested once more
    m = re.match(r"^([^:]{0,60}\b(?:kbps|copy|rip)\b[^:]*):\s*(.+)$", s)
    if m:                                                    # "128 kbps 2-disc copy: Sinfonia"
        s = m.group(2)
    # cut at the first sentence break that begins commentary/evidence
    pos = 0
    for mm in re.finditer(r"\.\s+", s):
        head, rest = s[:mm.start()], s[mm.end():]
        if ABBR.search(head):
            continue
        nxt = re.split(r"(?<=[a-z0-9'\"’)])\.\s+", rest, maxsplit=1)[0]
        if EVIDENCE.search(nxt) or len(head) >= 40:
            s = head
            break
    s = re.sub(r"[,;]?\s*(Inferred|inferred|probably|possibly|estimated)\b.*$", "", s)
    s = re.sub(r"[,;]?\s*\d{1,3}:\d{2}(:\d{2})?\s*$", "", s).strip(" ,;.:")
    if len(s) > 140:
        s = s[:140].rsplit(" ", 1)[0] + "…"
    return s or (c or "").strip()[:140]


def movement(c: str) -> tuple[str, int] | None:
    s = short_content(c)
    m = re.match(r"^(?:Mvt\.?|Movement)\s*(\d+)\s*[:.\-]\s*(.+)$", s)
    if m:
        return m.group(2).strip(), int(m.group(1))
    m = re.match(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s+(.+)$", s)
    if m:
        return m.group(2).strip(), ROMAN[m.group(1)]
    return None


def strip_paren(s: str) -> str:
    return re.sub(r"\s*\([^()]*\)\s*$", "", s or "").strip()


VOICE_INSTR = re.compile(r"soprano|mezzo|alto|contralto|tenor|baritone|bass|countertenor|treble|piano|"
                         r"violin|viola|cello|double bass|flute|trumpet|horn|harp|organ|harpsichord|clarinet|"
                         r"oboe|bassoon|timpani|percussion|guitar|narrator|solo|obbligato|trombone|tuba|"
                         r"saxophone|celesta|glass harmonica|speaker|reciter|voice")


def genre_of(p: dict, rec: dict) -> str:
    work, notes = p.get("work", "").lower(), p.get("notes", "").lower()
    if re.search(r"overture|prelude|suite|intermezzo|excerpt|ballet|\bdances?\b|aria", work):
        return "Classical"
    if re.search(r"oratorio|requiem|\bmass\b|cantata|passion|symphon|concerto", work + " " + notes):
        return "Classical"
    if re.search(r"\bopera\b|dramma|melodramma|libretto|tragédie|opéra|singspiel|music drama|musikdrama|"
                 r"bühnenfestspiel|zarzuela|operetta", notes):
        return "Opera"
    idx = rec.get("program", []).index(p)
    chars = [c for c in rec.get("credits", []) if c.get("_cat") == "performer"
             and (c.get("_works") is None or idx in c["_works"]) and not VOICE_INSTR.search(c["role"].lower())]
    return "Opera" if len(chars) >= 2 else "Classical"


def clean_name(n: str) -> str:
    return re.sub(r"\s*\([^()]*\)\s*$", "", n or "").strip()


# ------------------------------------------------------------------ plan

def plan_record(rec: dict, inv: dict, mount: str) -> tuple[list[dict], dict]:
    perf = rec.get("performance", {})
    program = rec.get("program", [])
    credits = rec.get("credits", [])
    stats = {"skipped_wav_wma": 0, "skipped_other": 0, "missing": [], "dup": 0}

    for c in credits:
        c["_cat"] = category(c.get("role", ""))
        c["_works"] = credit_works(c, program)

    # which program entry each track belongs to
    tracks = []
    for t in rec["track_map"]:
        ext = os.path.splitext(t["file"])[1].lower()
        pi = match_program(t.get("work"), program)
        if pi is None and t.get("work") and not t["work"].strip().startswith("(") and len(program) > 1:
            pi = None
        tracks.append({"t": t, "ext": ext, "pi": pi})

    music = [x for x in tracks if x["pi"] is not None and not x["t"].get("duplicate_of")]

    def applies(c, pi):
        return c["_works"] is None or (pi is not None and pi in c["_works"])

    def main_of(cat):
        best, bs = None, -1
        for c in credits:
            if c["_cat"] != cat or c["status"] not in OK:
                continue
            cov = sum(1 for x in music if applies(c, x["pi"]))
            if c["_works"] is None:
                cov += 0.5
            if cat == "ensemble" and "orchestra" in c["role"].lower():
                cov += 0.25
            if cov > bs:
                best, bs = c, cov
        return best

    main_cond, main_ens = main_of("conductor"), main_of("ensemble")
    album_artist = [clean_name(c["name"]) for c in (main_cond, main_ens) if c]

    # album title
    if len(program) == 1:
        comp = (program[0].get("composer") or "").split()
        title = (f"{comp[-1]}: " if comp else "") + program[0]["work"]
    else:
        ev = re.split(r" \(|:| - ", perf.get("event_name") or "")[0].strip()
        if ev and len(ev) <= 60:
            title = ev
        else:
            title = "; ".join(f"{(p.get('composer') or '').split()[-1]}: {strip_paren(p['work'])}"
                              for p in program[:3]) + ("; …" if len(program) > 3 else "")
    venue = re.split(r" \(|, ", perf.get("venue") or "")[0].strip()
    if len(venue) > 45:
        venue = perf.get("city") or ""
    date = perf.get("date") or ""
    dstat = perf.get("date_status", "unknown")
    tag_date = date if dstat in OK else date[:4]
    album = f"{title} — {venue}, {date}" if venue else f"{title} — {date}"
    gs = [genre_of(program[x["pi"]], rec) for x in music]
    rec_genre = "Opera" if gs and gs.count("Opera") * 2 >= len(gs) else "Classical"

    srcs = rec.get("sources", [])
    top = next((s for s in srcs if s.get("read_ok") and s.get("date_specific")), srcs[0] if srcs else None)
    summ = rec.get("summary", "")
    if len(summ) > 400:
        summ = summ[:400].rsplit(" ", 1)[0] + "…"
    comment = summ + (f" Source: {top['url']}" if top else "")
    credits_json = json.dumps([{k: v for k, v in (("n", c["name"]), ("r", c["role"]), ("s", c["status"]),
                                                   ("w", c.get("work"))) if v}
                               for c in credits], ensure_ascii=False, separators=(",", ":"))

    # disc / track numbering (play order = track_map order, duplicates skipped)
    dirs = []
    for x in tracks:
        if not x["t"].get("duplicate_of"):
            d = os.path.dirname(x["t"]["file"])
            if d not in dirs:
                dirs.append(d)
    multi = len(dirs) > 1
    per_disc: dict[str, int] = {}
    numbering: dict[str, tuple[int, int]] = {}
    for x in tracks:
        if x["t"].get("duplicate_of"):
            continue
        d = os.path.dirname(x["t"]["file"]) if multi else ""
        per_disc[d] = per_disc.get(d, 0) + 1
        numbering[x["t"]["file"]] = (per_disc[d], (dirs.index(d) + 1) if multi else 0)
    totals = {}
    for f, (n, disc) in numbering.items():
        totals[disc] = max(totals.get(disc, 0), n)

    plans = []
    for x in tracks:
        t, ext, pi = x["t"], x["ext"], x["pi"]
        if ext in SKIP_AUDIO:
            stats["skipped_wav_wma"] += 1
            continue
        if ext not in AUDIO_FMT:
            stats["skipped_other"] += 1
            continue
        rel = resolve(t["file"], rec, inv, mount)
        if not rel:
            stats["missing"].append(t["file"])
            continue
        dup = t.get("duplicate_of")
        key = dup if dup else t["file"]
        n, disc = numbering.get(key, (0, 0))
        if dup:
            stats["dup"] += 1

        appl = [c for c in credits if c["status"] in OK and applies(c, pi)]
        if pi is None:
            appl = [c for c in credits if c["status"] in OK and c["_works"] is None]

        def pick(cat):
            sp = [c for c in appl if c["_cat"] == cat and c["_works"] is not None]
            return sp if sp else [c for c in appl if c["_cat"] == cat]
        conds, ens, perfs = pick("conductor"), pick("ensemble"), [c for c in appl if c["_cat"] == "performer"]
        if pi is not None and any(c["_works"] for c in conds) and main_cond not in conds \
                and not any(c["_works"] for c in ens):
            ens = []                                  # e.g. a commercial-recording filler
        bcast = [c for c in appl if c["_cat"] == "broadcast"]
        sect = [c for c in appl if c["_cat"] == "section"]

        def names(cs):
            out = []
            for c in cs:
                n = clean_name(c["name"])
                if n not in out:
                    out.append(n)
            return out

        content = t["content"]
        if dup:
            orig = next((y["t"] for y in tracks if y["t"]["file"] == dup), None)
            if orig:
                content = orig["content"]
        sc = short_content(content)
        mv = movement(content) if pi is not None else None
        if pi is not None:
            p = program[pi]
            work = p["work"]
            ws = strip_paren(work)
            if (toks(sc[:len(ws) + 3]) >= toks(ws) and toks(ws)) or sc.lower() in ws.lower():
                ttl = sc if len(sc) >= len(ws) else ws
            elif toks(sc) and toks(sc) <= toks(ws):
                ttl = ws
            else:
                ttl = f"{ws}: {sc}"
            composer = p.get("composer")
            genre = genre_of(p, rec)
            artist = names(perfs) or names(ens) or names(conds)
        else:
            work, composer, genre = None, None, rec_genre
            ttl = sc
            artist = names(bcast) or album_artist
        tags = {
            "title": ttl, "work": work, "composer": composer,
            "movement": mv[0] if mv else None, "movement_n": mv[1] if mv else None,
            "artist": artist, "albumartist": album_artist,
            "conductor": names(conds) if pi is not None else [],
            "ensemble": names(ens) if pi is not None else [],
            "performers": ([(c["role"], clean_name(c["name"])) for c in conds + ens + perfs + sect]
                           if pi is not None else [(c["role"], clean_name(c["name"])) for c in bcast]),
            "album": album, "date": tag_date,
            "track": n, "tracktotal": totals.get(disc, 0),
            "disc": disc, "disctotal": len(dirs) if multi else 0,
            "genre": genre, "comment": comment,
            "x": {"SETHSTER_ID": rec["id"], "BROADCAST_DATE": perf.get("broadcast_date"),
                  "VENUE": ", ".join(v for v in (perf.get("venue"), perf.get("city")) if v),
                  "DATE_STATUS": dstat, "CREDITS_JSON": credits_json,
                  "SETHSTER_CONTENT": t["content"],
                  "SETHSTER_NOTE": (f"Duplicate of {dup}; not counted in track numbering" if dup else None)},
        }
        plans.append({"rel": rel, "fmt": AUDIO_FMT[ext], "tags": tags})
    return plans, stats

# ------------------------------------------------------------------ format writers

X_KEYS = ["SETHSTER_ID", "BROADCAST_DATE", "VENUE", "DATE_STATUS", "CREDITS_JSON",
          "SETHSTER_CONTENT", "SETHSTER_NOTE"]
ID3_MANAGED = ["TCOM", "TIT1", "GRP1", "TIT2", "MVNM", "MVIN", "TPE1", "TPE2", "TPE3", "TMCL",
               "TALB", "TDRC", "TRCK", "TPOS", "TCON"]


def id3_desired(T: dict) -> dict[str, object]:
    """Return {frame HashKey: frame} of what we want."""
    fr = []
    J = "; ".join
    if T["composer"]: fr.append(TCOM(encoding=3, text=T["composer"]))
    if T["work"]:
        fr.append(TIT1(encoding=3, text=T["work"]))
        fr.append(GRP1(encoding=3, text=T["work"]))
    fr.append(TIT2(encoding=3, text=T["title"]))
    if T["movement"]:
        fr.append(MVNM(encoding=3, text=T["movement"]))
        fr.append(MVIN(encoding=3, text=str(T["movement_n"])))
    if T["artist"]: fr.append(TPE1(encoding=3, text=J(T["artist"])))
    if T["albumartist"]: fr.append(TPE2(encoding=3, text=J(T["albumartist"])))
    if T["conductor"]: fr.append(TPE3(encoding=3, text=J(T["conductor"])))
    if T["performers"]:
        fr.append(TMCL(encoding=3, people=[[r, n] for r, n in T["performers"]]))
    fr.append(TALB(encoding=3, text=T["album"]))
    if T["date"]: fr.append(TDRC(encoding=3, text=T["date"]))
    if T["track"]: fr.append(TRCK(encoding=3, text=f"{T['track']}/{T['tracktotal']}"))
    if T["disc"]: fr.append(TPOS(encoding=3, text=f"{T['disc']}/{T['disctotal']}"))
    fr.append(TCON(encoding=3, text=T["genre"]))
    for k in X_KEYS:
        v = T["x"].get(k)
        if v: fr.append(TXXX(encoding=3, desc=k, text=v))
    fr.append(COMM(encoding=3, lang="eng", desc="", text=T["comment"]))
    return {f.HashKey: f for f in fr}


def id3_is_managed(key: str) -> bool:
    if key.split(":")[0] in ID3_MANAGED:
        return True
    if key.startswith("TXXX:") and key[5:] in X_KEYS:
        return True
    return key.startswith("COMM::")            # unnamed comments only (iTunNORM etc. kept)


def vorbis_desired(T: dict) -> dict[str, list[str]]:
    d: dict[str, list[str]] = {}
    def put(k, v):
        if v:
            d[k] = v if isinstance(v, list) else [str(v)]
    put("TITLE", T["title"]); put("WORK", T["work"]); put("GROUPING", T["work"])
    put("COMPOSER", T["composer"]); put("MOVEMENTNAME", T["movement"])
    put("MOVEMENT", T["movement_n"] and str(T["movement_n"]))
    put("ARTIST", T["artist"]); put("ALBUMARTIST", T["albumartist"])
    put("CONDUCTOR", T["conductor"]); put("ENSEMBLE", T["ensemble"])
    put("PERFORMER", [f"{n} ({r})" for r, n in T["performers"]])
    put("ALBUM", T["album"]); put("DATE", T["date"])
    if T["track"]:
        put("TRACKNUMBER", str(T["track"])); put("TRACKTOTAL", str(T["tracktotal"]))
    if T["disc"]:
        put("DISCNUMBER", str(T["disc"])); put("DISCTOTAL", str(T["disctotal"]))
    put("GENRE", T["genre"]); put("COMMENT", T["comment"])
    for k in X_KEYS:
        put(k, T["x"].get(k))
    return d


VORBIS_MANAGED = ["TITLE", "WORK", "GROUPING", "COMPOSER", "MOVEMENTNAME", "MOVEMENT", "ARTIST",
                  "ALBUMARTIST", "CONDUCTOR", "ENSEMBLE", "PERFORMER", "ALBUM", "DATE", "TRACKNUMBER",
                  "TRACKTOTAL", "TOTALTRACKS", "DISCNUMBER", "DISCTOTAL", "TOTALDISCS", "GENRE",
                  "COMMENT", "DESCRIPTION"] + X_KEYS


def mp4_desired(T: dict) -> dict[str, list]:
    J = "; ".join
    d: dict[str, list] = {"\xa9nam": [T["title"]], "\xa9alb": [T["album"]], "\xa9gen": [T["genre"]],
                          "\xa9cmt": [T["comment"]]}
    if T["composer"]: d["\xa9wrt"] = [T["composer"]]
    if T["work"]: d["\xa9wrk"] = [T["work"]]; d["\xa9grp"] = [T["work"]]
    if T["movement"]: d["\xa9mvn"] = [T["movement"]]; d["\xa9mvi"] = [T["movement_n"]]; d["shwm"] = [1]
    if T["artist"]: d["\xa9ART"] = [J(T["artist"])]
    if T["albumartist"]: d["aART"] = [J(T["albumartist"])]
    if T["date"]: d["\xa9day"] = [T["date"]]
    if T["track"]: d["trkn"] = [(T["track"], T["tracktotal"])]
    if T["disc"]: d["disk"] = [(T["disc"], T["disctotal"])]
    ff = {"CONDUCTOR": J(T["conductor"]), "ENSEMBLE": J(T["ensemble"]),
          "PERFORMER": J(f"{n} ({r})" for r, n in T["performers"]), **T["x"]}
    for k, v in ff.items():
        if v:
            d[f"----:com.sethster:{k}"] = [MP4FreeForm(str(v).encode("utf-8"))]
    return d


MP4_MANAGED = ["\xa9nam", "\xa9alb", "\xa9gen", "\xa9cmt", "\xa9wrt", "\xa9wrk", "\xa9grp", "\xa9mvn",
               "\xa9mvi", "shwm", "\xa9ART", "aART", "\xa9day", "trkn", "disk"]


def _jsonable(v):
    if isinstance(v, (bytes, bytearray)):
        return {"b64": tagsafe.b64(bytes(v))}
    if isinstance(v, MP4FreeForm):
        return {"b64": tagsafe.b64(bytes(v)), "dataformat": v.dataformat}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def open_tags(path: str, fmt: str):
    if fmt == "mp3":
        try:
            return ID3(path)
        except ID3NoHeaderError:
            return None
    if fmt == "flac":
        return FLAC(path)
    if fmt == "ogg":
        return OggVorbis(path)
    if fmt == "opus":
        return OggOpus(path)
    if fmt == "m4a":
        return MP4(path)
    raise ValueError(fmt)


def dump_tags(obj, fmt: str) -> dict:
    """Readable full dump (APIC/covr summarised by size)."""
    if obj is None:
        return {}
    if fmt == "mp3":
        out = {}
        for k, f in obj.items():
            if k.startswith("APIC"):
                out[k] = f"<picture {f.mime} {len(f.data)} bytes>"
            else:
                out[k] = frame_text(f)[:4000]
        return {"id3_version": "2.%d" % obj.version[1], "frames": out}
    if fmt in ("flac", "ogg", "opus"):
        d = {"comments": [[k, v] for k, v in (obj.tags or [])]}
        if fmt == "flac":
            d["pictures"] = [f"{p.mime} {len(p.data)} bytes" for p in obj.pictures]
        return d
    if fmt == "m4a":
        return {"items": {k: (f"<covr {len(v)} images>" if k == "covr" else _jsonable(v))
                          for k, v in (obj.tags or {}).items()}}
    return {}


def frame_text(f) -> str:
    if f.FrameID in ("TMCL", "TIPL"):
        return f"{f.FrameID}=" + "; ".join(f"{r}: {n}" for r, n in f.people)
    return str(f.pprint())


def current_view(obj, fmt: str) -> dict[str, str]:
    """Flattened {key: text} of managed keys, for diffs/idempotency."""
    out = {}
    if obj is None:
        return out
    if fmt == "mp3":
        for k, f in obj.items():
            if id3_is_managed(k):
                out[k] = frame_text(f)
    elif fmt in ("flac", "ogg", "opus"):
        tags = obj.tags or []
        for k in {k.upper() for k, _ in tags}:
            if k in VORBIS_MANAGED:
                out[k] = " | ".join(obj.tags[k])
    elif fmt == "m4a":
        for k, v in (obj.tags or {}).items():
            if k in MP4_MANAGED or k.startswith("----:com.sethster:"):
                out[k] = " | ".join(bytes(x).decode("utf-8", "replace") if isinstance(x, MP4FreeForm)
                                    else str(x) for x in v)
    return out


def desired_view(T: dict, fmt: str) -> dict[str, str]:
    if fmt == "mp3":
        return {k: frame_text(f) for k, f in id3_desired(T).items()}
    if fmt in ("flac", "ogg", "opus"):
        return {k: " | ".join(v) for k, v in vorbis_desired(T).items()}
    return {k: " | ".join(bytes(x).decode() if isinstance(x, MP4FreeForm) else str(x) for x in v)
            for k, v in mp4_desired(T).items()}


def write_tags(path: str, fmt: str, T: dict, mount_real: str):
    tagsafe.assert_writable(path, mount_real)
    if fmt == "mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        for k in [k for k in tags.keys() if id3_is_managed(k)]:
            del tags[k]
        for f in id3_desired(T).values():
            tags.add(f)
        tagsafe.assert_writable(path, mount_real)
        tags.save(path, v2_version=4, v1=1)
        return
    if fmt in ("flac", "ogg", "opus"):
        obj = open_tags(path, fmt)
        if obj.tags is None:
            obj.add_tags()
        for k in list({k.upper() for k, _ in obj.tags}):
            if k in VORBIS_MANAGED:
                del obj.tags[k]
        for k, v in vorbis_desired(T).items():
            obj.tags[k] = v
        tagsafe.assert_writable(path, mount_real)
        obj.save()
        return
    if fmt == "m4a":
        obj = MP4(path)
        if obj.tags is None:
            obj.add_tags()
        for k in list(obj.tags.keys()):
            if k in MP4_MANAGED or k.startswith("----:com.sethster:"):
                del obj.tags[k]
        for k, v in mp4_desired(T).items():
            obj.tags[k] = v
        tagsafe.assert_writable(path, mount_real)
        obj.save()
        return
    raise ValueError(fmt)

# ------------------------------------------------------------------ manifest

def manifest_originals(rid: str) -> set[str]:
    """Files whose original tags are already recorded.  ~/Documents is synced by iCloud, which can make a
    read time out while it fetches the file back, so a timeout is retried before giving up."""
    p = os.path.join(MANIFEST_DIR, rid + ".jsonl")
    for attempt in range(5):
        try:
            seen = set()
            if os.path.exists(p):
                for line in open(p):
                    e = json.loads(line)
                    if e.get("event") == "original":
                        seen.add(e["rel"])
            return seen
        except TimeoutError:
            if attempt == 4:
                raise
            time.sleep(10 * (attempt + 1))


def manifest_append(rid: str, entry: dict):
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    with open(os.path.join(MANIFEST_DIR, rid + ".jsonl"), "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def record_original(rid: str, rel: str, path: str, fmt: str):
    st = os.stat(path)
    e = {"event": "original", "rel": rel, "fmt": fmt, "mtime": st.st_mtime,
         "time": datetime.now(timezone.utc).isoformat(), "tool": TOOL, **tagsafe.head_tail_hashes(path)}
    if fmt in ("mp3", "flac"):
        region, v1 = tagsafe.read_region(path, fmt)
        e["restore"] = "byte_region"
        e["region_len"] = len(region)
        e["region_b64"] = tagsafe.b64(region)
        e["id3v1_b64"] = tagsafe.b64(v1) if v1 else None
    else:
        e["restore"] = "tag_keys"
    obj = open_tags(path, fmt)
    e["tags"] = dump_tags(obj, fmt)
    if fmt == "m4a" and obj is not None and obj.tags is not None:
        e["mp4_managed"] = {k: _jsonable(v) for k, v in obj.tags.items()
                            if k in MP4_MANAGED or k.startswith("----:com.sethster:")}
    manifest_append(rid, e)

# ------------------------------------------------------------------ main

def print_diff(rel, cur, want):
    print(f"  --- {rel}")
    for k in sorted(set(cur) | set(want)):
        a, b = cur.get(k), want.get(k)
        if a == b:
            continue
        if a is not None:
            print(f"    - {k}: {a[:160]!s}")
        if b is not None:
            print(f"    + {k}: {b[:160]!s}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mount", default=tagsafe.DEFAULT_MOUNT, required=tagsafe.DEFAULT_MOUNT is None,
                    help="read-write mount of the sandbox share (default: $SETHSTER_TAG_MOUNT)")
    ap.add_argument("--pilot", action="store_true", help="only the pilot records (data/pilot/results)")
    ap.add_argument("--sources", default="pilot,research,archives")
    ap.add_argument("--ids", default="", help="comma-separated performance ids")
    ap.add_argument("--min-confidence", default="high", choices=["high", "medium", "low"],
                    help="minimum draft_meta.confidence for archive drafts")
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--diff", default="", help="comma-separated ids to print full tag diffs for")
    ap.add_argument("--limit", type=int, default=0, help="max performances")
    a = ap.parse_args(argv)

    mount_real = tagsafe.verify_mount(a.mount, need_rw=a.apply)
    sources = ["pilot"] if a.pilot else a.sources.split(",")
    ids = set(filter(None, a.ids.split(","))) or None
    inv = load_inventory()
    recs = load_records(sources, ids, a.min_confidence, inv)
    if a.limit:
        recs = dict(list(recs.items())[: a.limit])
    diff_ids = set(filter(None, a.diff.split(",")))
    print(f"{'APPLY' if a.apply else 'DRY RUN'}: {len(recs)} performances from {sources}; mount {mount_real}")

    T0 = time.time()
    tot = {"files": 0, "changed": 0, "unchanged": 0, "written": 0, "wav_wma": 0, "other": 0,
           "missing": 0, "dup": 0, "errors": 0, "bytes": 0}
    for rid, rec in recs.items():
        t0 = time.time()
        plans, st = plan_record(rec, inv, mount_real)
        tot["wav_wma"] += st["skipped_wav_wma"]; tot["other"] += st["skipped_other"]
        tot["missing"] += len(st["missing"]); tot["dup"] += st["dup"]
        try:
            done = manifest_originals(rid) if a.apply else set()
        except OSError as ex:
            # without the manifest we can't tell which originals are recorded, so leave this one for the next run
            tot["errors"] += 1
            print(f"  !! {rid}: manifest unreadable ({type(ex).__name__}: {ex}); skipped")
            continue
        nchg = 0
        if rid in diff_ids:
            print(f"\n=== {rid}")
        for p in plans:
            path = os.path.join(mount_real, p["rel"])
            tot["files"] += 1
            try:
                obj = open_tags(path, p["fmt"])
                cur, want = current_view(obj, p["fmt"]), desired_view(p["tags"], p["fmt"])
                if cur == want:
                    tot["unchanged"] += 1
                    continue
                nchg += 1
                tot["changed"] += 1
                if rid in diff_ids:
                    print_diff(p["rel"], cur, want)
                if a.apply:
                    tagsafe.assert_writable(path, mount_real)
                    if p["rel"] not in done:
                        record_original(rid, p["rel"], path, p["fmt"])
                        done.add(p["rel"])
                    write_tags(path, p["fmt"], p["tags"], mount_real)
                    after = open_tags(path, p["fmt"])
                    ok = current_view(after, p["fmt"]) == want
                    manifest_append(rid, {"event": "applied", "rel": p["rel"], "verified": ok,
                                          "time": datetime.now(timezone.utc).isoformat(),
                                          **tagsafe.head_tail_hashes(path)})
                    tot["written"] += 1
                    tot["bytes"] += os.path.getsize(path)
                    if not ok:
                        tot["errors"] += 1
                        print(f"  !! verify mismatch {p['rel']}")
            except tagsafe.SafetyError:
                raise
            except Exception as ex:                  # noqa: BLE001
                tot["errors"] += 1
                print(f"  !! {p['rel']}: {type(ex).__name__}: {ex}")
        miss = f" missing={len(st['missing'])}" if st["missing"] else ""
        print(f"{rid:34s} files={len(plans):3d} change={nchg:3d} dup={st['dup']} "
              f"wav/wma={st['skipped_wav_wma']} other={st['skipped_other']}{miss}  {time.time()-t0:5.1f}s")
    el = time.time() - T0
    print(f"\nTOTAL {json.dumps(tot)}  elapsed {el:.1f}s"
          f"  ({el / max(1, len(recs)):.2f}s/performance, {el / max(1, tot['files']):.3f}s/file)")
    return 1 if tot["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
