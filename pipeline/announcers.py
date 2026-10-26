"""Transcribe the radio-announcer segments of a broadcast recording.

For one performance (a dossier JSON, or any inventory record with a `tracks` list of
{path, duration_s}), pick the likely speech windows:

  * the first ~6 min of the first track and the last ~6 min of the last track;
  * the whole of any track that the track map (data/pilot/results/<id>.json) or the
    file name marks as intro / interval / intermission / commentary / announcer /
    interview / applause, and any short track (<= 150 s);

copy those tracks from the (read-only, slow) SMB share to a local temp dir, decode the
windows with ffmpeg, run Silero VAD to drop music stretches, and transcribe the speech
regions with mlx-whisper (large-v3-turbo). The temp dir is deleted afterwards.

Writes data/transcripts/<id>.json  {id, model, stats, windows, segments:[{file, start, end, text}]}
   and data/transcripts/<id>.txt   (compact, one line per segment).

Usage:
  uv run python pipeline/announcers.py --audio-root <share mount> roh-191026-werther [more ids | path.json ...]
  uv run python pipeline/announcers.py --ssh-host user@nas.local --remote-root /volume1/music_sandbox <ids>
"""
import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DOSSIERS = ROOT / "data/pilot/dossiers"
RESULTS = ROOT / "data/pilot/results"
OUT = ROOT / "data/transcripts"
MODEL = "mlx-community/whisper-large-v3-turbo"
SR = 16000

HEAD_S = 360          # first track: transcribe this much from the start
TAIL_S = 360          # last track: this much from the end
SHORT_S = 150         # tracks this short are taken whole
WEAK_TAIL_S = 180     # "probably with applause" tracks: just the end
MAX_WHOLE_S = 45 * 60

# Words in a track-map description or file name that mean "someone is talking here".
STRONG = re.compile(
    r"\b(intro\w*|interval\w*|intermission\w*|commentary|comment|announc\w*|interview\w*|host\w*|"
    r"presenter|synopsis|credits|sign-off|outro|continuity|feature\w*|talk|backstage|quiz|"
    r"broadcast (opening|close)|closing (remarks|announcement)|a&c)\b",
    re.I,
)
WEAK = re.compile(r"\b(applause|curtain calls?|pause|scene change|encore)\b", re.I)
# Track-map phrases that say the file is (mostly) music even if a keyword appears.
MUSIC_FIRST = re.compile(r"^\s*(end of |start of )?(mvt|movement|act|scene|complete|continuous segment|prologue)\b", re.I)
PART_EDGE_S = 120     # head/tail of the first/last track of each sub-folder (disc / part A, B, C)


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


# ---------------------------------------------------------------- track selection

def load_record(arg: str) -> tuple[dict, list]:
    p = Path(arg)
    rec = json.loads(p.read_text()) if p.suffix == ".json" and p.exists() else json.loads((DOSSIERS / f"{arg}.json").read_text())
    res = RESULTS / f"{rec['id']}.json"
    track_map = json.loads(res.read_text()).get("track_map", []) if res.exists() else []
    return rec, track_map


def _num(s) -> tuple:
    m = re.match(r"\s*(\d+)", str(s or ""))
    return (int(m.group(1)),) if m else (10**6,)


def order_tracks(rec: dict, track_map: list) -> list[dict]:
    """Broadcast order: track-map order when present, else (folder, tag track no., path). Drops exact duplicates."""
    tracks = [dict(t) for t in rec["tracks"]]
    tm_index, tm_content = {}, {}
    for i, e in enumerate(track_map):
        f = e.get("file", "")
        for t in tracks:
            if t["path"] not in tm_index and (t["path"] == f or t["path"].endswith("/" + f)):
                tm_index[t["path"]] = i
                tm_content[t["path"]] = e.get("content", "")
                break
    for t in tracks:
        t["map"] = tm_content.get(t["path"], "")
    key = lambda t: (tm_index.get(t["path"], 10**6), str(Path(t["path"]).parent), _num(t.get("tags", {}).get("track")), t["path"])
    if not tm_index:
        key = lambda t: (str(Path(t["path"]).parent), _num(t.get("tags", {}).get("track")), _num(Path(t["path"]).stem[-3:]), t["path"])
    seen, out = set(), []
    for t in sorted(tracks, key=key):
        # same length and title = the same track filed twice (re-tagged copies differ in size)
        sig = ((t.get("duration_s") or t.get("dur_s")), str(t.get("tags", {}).get("title") or Path(t["path"]).stem).lower())
        if sig in seen:
            continue
        seen.add(sig)
        out.append(t)
    return out


def speech_windows(tracks: list[dict]) -> list[dict]:
    """[{path, start, end, why}] — merged per track."""
    wins: dict[str, list] = {}
    for i, t in enumerate(tracks):
        d = float((t.get("duration_s") or t.get("dur_s")) or 0)
        name = Path(t["path"]).stem
        desc = t.get("map", "")
        w = []
        if i == 0:
            w.append((0.0, min(d, HEAD_S), "first track head"))
        if i == len(tracks) - 1:
            w.append((max(0.0, d - TAIL_S), d, "last track tail"))
        folder = str(Path(t["path"]).parent)
        if 0 < i and str(Path(tracks[i - 1]["path"]).parent) != folder:
            w.append((0.0, min(d, PART_EDGE_S), "part head"))
        if i < len(tracks) - 1 and str(Path(tracks[i + 1]["path"]).parent) != folder:
            w.append((max(0.0, d - PART_EDGE_S), d, "part tail"))
        strong = STRONG.search(name) or (STRONG.search(desc) and not MUSIC_FIRST.search(desc))
        # "Act II, possibly preceded by ... the host's re-introduction": take the head of the track.
        strong_in_music = STRONG.search(desc) and MUSIC_FIRST.search(desc)
        weak = WEAK.search(name) or WEAK.search(desc)
        if d <= SHORT_S:
            w.append((0.0, d, "short track"))
        elif strong and d <= MAX_WHOLE_S:
            w.append((0.0, d, "speech track"))
        elif strong_in_music:
            w.append((0.0, min(d, HEAD_S), "track map mentions speech: head"))
            w.append((max(0.0, d - WEAK_TAIL_S), d, "track map mentions speech: tail"))
        elif weak:
            w.append((0.0, d, "applause/pause track") if d <= 2 * WEAK_TAIL_S else (max(0.0, d - WEAK_TAIL_S), d, "applause tail"))
        if w:
            wins[t["path"]] = w
    out = []
    for t in tracks:
        if t["path"] not in wins:
            continue
        spans = sorted(wins[t["path"]])
        merged = [list(spans[0])]
        for s, e, why in spans[1:]:
            if s <= merged[-1][1] + 5:
                merged[-1][1] = max(merged[-1][1], e)
                if why not in merged[-1][2]:
                    merged[-1][2] += "; " + why
            else:
                merged.append([s, e, why])
        out += [{"path": t["path"], "start": round(s, 1), "end": round(e, 1), "why": why} for s, e, why in merged]
    return out


# ---------------------------------------------------------------- audio

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4", "-o", "LogLevel=ERROR"]
# (no ControlPersist: the forked master would hold the stderr pipe open and hang subprocess.run)


def ssh_fetch(host: str, remote_root: str, rel: str, dst: Path, tries: int = 4) -> None:
    """Stream <remote_root>/<rel> to dst with `ssh host cat` (read-only on the NAS; scp is flaky there).
    The remote size is printed to stderr first and checked against what arrived; retried on failure."""
    q = shlex.quote(f"{remote_root.rstrip('/')}/{rel}")
    cmd = f"s=$(stat -c %s -- {q}) || exit 3; echo \"SIZE=$s\" >&2; exec cat -- {q}"
    err = ""
    for attempt in range(tries):
        with open(dst, "wb") as fh:
            p = subprocess.run(["ssh", *SSH_OPTS, host, cmd], stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.PIPE, timeout=1800)
        err = p.stderr.decode("utf-8", "replace")
        m = re.search(r"SIZE=(\d+)", err)
        if p.returncode == 0 and m and dst.stat().st_size == int(m.group(1)):
            return
        if p.returncode == 3:
            break  # file not there; retrying will not help
        time.sleep(5 * (attempt + 1))
    dst.unlink(missing_ok=True)
    raise OSError(f"ssh fetch failed for {rel!r}: rc={p.returncode} {err.strip()[-300:]}")


def decode(local: Path, start: float, end: float) -> np.ndarray:
    raw = subprocess.run(
        [ffmpeg_bin(), "-v", "quiet", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(local),
         "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.float32).copy()


_VAD = None


def speech_regions(audio: np.ndarray, gap_s=8.0, pad_s=1.0, min_s=1.0) -> list[tuple[float, float]]:
    """Silero VAD; merge nearby speech, pad a little. Music without talk comes back empty."""
    global _VAD
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    if _VAD is None:
        _VAD = load_silero_vad()
    ts = get_speech_timestamps(torch.from_numpy(audio), _VAD, threshold=0.5, sampling_rate=SR, return_seconds=True)
    dur = len(audio) / SR
    regs = []
    for s in ts:
        a, b = max(0.0, s["start"] - pad_s), min(dur, s["end"] + pad_s)
        if regs and a <= regs[-1][1] + gap_s:
            regs[-1][1] = b
        else:
            regs.append([a, b])
    return [(a, b) for a, b in regs if b - a >= min_s]


def transcribe(audio: np.ndarray, language: str | None) -> list[dict]:
    import mlx.core as mx
    import mlx_whisper
    # MLX keeps freed GPU buffers cached without limit; with several shards that grew to ~10 GB per process
    (getattr(mx, "set_cache_limit", None) or mx.metal.set_cache_limit)(1 << 30)
    r = mlx_whisper.transcribe(
        audio, path_or_hf_repo=MODEL, language=language, condition_on_previous_text=False,
        verbose=None, no_speech_threshold=0.6, compression_ratio_threshold=2.4, logprob_threshold=-1.0,
    )
    lang = r.get("language")
    segs = []
    for s in r["segments"]:
        text = s["text"].strip()
        if not text:
            continue
        if s.get("compression_ratio", 0) > 2.4 or (s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0):
            continue  # typical Whisper hallucination on music / applause
        if s["end"] - s["start"] > 8 and len(text.split()) <= 3:
            continue  # "Thank you." stretched over 30 s of applause
        segs.append({"start": s["start"], "end": s["end"], "text": text, "lang": lang,
                     "avg_logprob": round(s.get("avg_logprob", 0), 2)})
    return segs


# ---------------------------------------------------------------- driver

def fmt_t(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def run(arg: str, audio_root: Path | None, language: str | None, tmp_parent: str | None,
        ssh_host: str | None = None, remote_root: str | None = None) -> dict:
    """Audio comes from audio_root (a mounted share) or, with ssh_host + remote_root, over ssh."""
    rec, track_map = load_record(arg)
    pid = rec["id"]
    tracks = order_tracks(rec, track_map)
    wins = speech_windows(tracks)
    tmp = Path(tempfile.mkdtemp(prefix=f"announcers_{pid}_", dir=tmp_parent))
    stats = {"tracks": len(tracks), "windows": len(wins), "window_audio_s": 0.0, "speech_audio_s": 0.0,
             "copy_s": 0.0, "vad_s": 0.0, "asr_s": 0.0}
    segments = []
    t_all = time.time()
    try:
        local = {}
        for w in wins:
            if w["path"] in local:
                continue
            dst = tmp / f"{len(local):03d}{Path(w['path']).suffix}"
            t = time.time()
            if ssh_host:
                ssh_fetch(ssh_host, remote_root, w["path"], dst)  # read from NAS, write only to local temp
            else:
                shutil.copyfile(audio_root / w["path"], dst)  # read from share, write only to local temp
            stats["copy_s"] += time.time() - t
            local[w["path"]] = dst
        for w in wins:
            audio = decode(local[w["path"]], w["start"], w["end"])
            stats["window_audio_s"] += len(audio) / SR
            t = time.time()
            regs = speech_regions(audio)
            stats["vad_s"] += time.time() - t
            w["speech"] = [[round(w["start"] + a, 1), round(w["start"] + b, 1)] for a, b in regs]
            for a, b in regs:
                chunk = audio[int(a * SR):int(b * SR)]
                stats["speech_audio_s"] += len(chunk) / SR
                t = time.time()
                for s in transcribe(chunk, language):
                    segments.append({"file": w["path"], "start": round(w["start"] + a + s["start"], 1),
                                     "end": round(w["start"] + a + s["end"], 1), "text": s["text"],
                                     "lang": s["lang"], "avg_logprob": s["avg_logprob"]})
                stats["asr_s"] += time.time() - t
            print(f"  {Path(w['path']).name} [{fmt_t(w['start'])}-{fmt_t(w['end'])}] {w['why']}: "
                  f"{sum(b - a for a, b in regs):.0f}s speech", file=sys.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    stats["wall_s"] = time.time() - t_all
    for k in list(stats):
        if k.endswith("_s"):
            stats[k] = round(stats[k], 1)
    stats["window_audio_min_per_wall_min"] = round(stats["window_audio_s"] / max(stats["wall_s"], 1e-9), 1)
    stats["asr_realtime_factor"] = round(stats["asr_s"] / max(stats["speech_audio_s"], 1e-9), 4)

    OUT.mkdir(parents=True, exist_ok=True)
    doc = {"id": pid, "model": MODEL, "stats": stats, "windows": wins, "segments": segments}
    (OUT / f"{pid}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    lines, cur = [f"# {pid}  ({MODEL})"], None
    for s in segments:
        if s["file"] != cur:
            cur = s["file"]
            lines.append(f"\n## {cur}")
        lines.append(f"{fmt_t(s['start'])}-{fmt_t(s['end'])}  {s['text']}")
    (OUT / f"{pid}.txt").write_text("\n".join(lines) + "\n")
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="+", help="performance ids (dossiers) or paths to record JSON")
    ap.add_argument("--audio-root", type=Path, help="mount point the track paths are relative to")
    ap.add_argument("--ssh-host", help="fetch audio over ssh instead of a mount, e.g. user@nas.local")
    ap.add_argument("--remote-root", help="share root on the ssh host, e.g. /volume1/music_sandbox")
    ap.add_argument("--language", default=None, help="force a language (default: auto-detect per region)")
    ap.add_argument("--tmp", default=None, help="parent dir for the local audio copy (default: system temp)")
    a = ap.parse_args()
    if not (a.audio_root or (a.ssh_host and a.remote_root)):
        ap.error("give --audio-root, or --ssh-host and --remote-root")
    for i in a.ids:
        print(f"{i}", file=sys.stderr)
        d = run(i, a.audio_root, a.language, a.tmp, a.ssh_host, a.remote_root)
        print(json.dumps({"id": d["id"], **d["stats"]}))


if __name__ == "__main__":
    main()
