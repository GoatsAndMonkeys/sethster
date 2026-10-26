"""Fetch audio from the NAS over ssh, decode it, and cache compact frame features.

Features are computed once per file and cached as .npz under data/cache/segment/features/, keyed by
the NAS path and file size. The audio itself is deleted right after decoding.

Frame rate: sr 11025, hop 1024 -> FPS = 10.77 frames/s (93 ms frames).
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np

from .. import localconfig

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CACHE = Path(os.environ.get("SEG_CACHE", DATA / "cache" / "segment"))
FEAT_DIR = CACHE / "features"
TMP_DIR = CACHE / "tmp"
# ssh login and sandbox root on the NAS: SEG_NAS_HOST / SEG_NAS_ROOT, else SETHSTER_NAS_SSH / SETHSTER_NAS_ROOT
# (environment or local.env, see pipeline/localconfig.py). No built-in default; fetch() fails until set.
NAS_HOST = localconfig.get("SEG_NAS_HOST") or localconfig.get("SETHSTER_NAS_SSH") or ""
NAS_ROOT = (localconfig.get("SEG_NAS_ROOT") or localconfig.get("SETHSTER_NAS_ROOT") or "").rstrip("/")
FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")

SR = 11025
N_FFT = 2048
HOP = 1024
FPS = SR / HOP


def feature_key(rel_path: str, size: int | None) -> str:
    return hashlib.sha1(f"{rel_path}|{size or ''}".encode()).hexdigest()[:20]


def feature_path(rel_path: str, size: int | None) -> Path:
    return FEAT_DIR / f"{feature_key(rel_path, size)}.npz"


# One shared ssh connection per machine: a fresh login for every file made the NAS write a login record each
# time, and with 12 workers that kept its system disk busy. The master is started on its own with no pipes
# (a master forked by subprocess.run would hold its stderr open and hang it); if it is missing or dead, each
# copy just logs in directly as before.
MUX = Path.home() / ".ssh" / f"cm-seg-{hashlib.sha1(NAS_HOST.encode()).hexdigest()[:10]}"
MUX_OPTS = ["-o", "ControlMaster=no", "-o", f"ControlPath={MUX}"]


def _ensure_master() -> None:
    if MUX.exists():
        return
    MUX.parent.mkdir(mode=0o700, exist_ok=True)
    with open(f"{MUX}.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if MUX.exists():
            return
        try:
            subprocess.run(["ssh", "-MNf", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=20",
                            "-o", "ControlPersist=900", "-o", "ServerAliveInterval=30", "-o", f"ControlPath={MUX}",
                            NAS_HOST], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - network
            pass


def _assert_sandbox(rel_path: str) -> str:
    if not NAS_HOST or not NAS_ROOT:
        raise RuntimeError("set SETHSTER_NAS_SSH and SETHSTER_NAS_ROOT (environment or local.env)")
    full = f"{NAS_ROOT}/{rel_path.lstrip('/')}"
    norm = os.path.normpath(full)
    if not norm.startswith(NAS_ROOT + "/") or ".." in rel_path.split("/"):
        raise ValueError(f"refusing path outside the sandbox share: {rel_path}")
    return norm


def fetch(rel_path: str, dest_dir: Path = TMP_DIR, retries: int = 3) -> Path:
    """Copy one file from the NAS (read-only `cat` over ssh) into a temp file. Caller deletes it."""
    full = _assert_sandbox(rel_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(rel_path).suffix.lower() or ".bin"
    last = None
    for attempt in range(retries):
        fd, tmp = tempfile.mkstemp(suffix=suffix, dir=dest_dir)
        try:
            # the size goes to stderr first so a short copy is caught; this exact form is also what the
            # read-only key on the second Mac is allowed to run (NAS ~/bin/sandbox-read.sh)
            q = shlex.quote(full)
            _ensure_master()
            with os.fdopen(fd, "wb") as fh:
                p = subprocess.run(["ssh", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=20", *MUX_OPTS, NAS_HOST,
                                    f"s=$(stat -c %s -- {q}) || exit 3; echo \"SIZE=$s\" >&2; exec cat -- {q}"],
                                   stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.PIPE, timeout=1800)
            err = p.stderr.decode(errors="replace")
            m = re.search(r"SIZE=(\d+)", err)
            if p.returncode == 0 and m and m.group(1) == "0":   # the file on the NAS is empty: retrying won't help
                os.unlink(tmp)
                raise IOError(f"empty file on NAS: {rel_path}")
            if p.returncode == 0 and m and os.path.getsize(tmp) == int(m.group(1)):
                return Path(tmp)
            last = err.strip()
            if p.returncode == 3:          # not on the NAS
                os.unlink(tmp)
                break
        except subprocess.TimeoutExpired as e:  # pragma: no cover - network
            last = str(e)
        os.unlink(tmp)
        time.sleep(2 + 5 * attempt)
    raise IOError(f"fetch failed for {rel_path}: {last}")


def decode(path: Path) -> np.ndarray:
    p = subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR),
                        "-f", "f32le", "-"], capture_output=True)
    if p.returncode != 0 and not p.stdout:
        raise IOError(f"ffmpeg failed: {p.stderr.decode(errors='replace')[-400:]}")
    return np.frombuffer(p.stdout, np.float32)


def compute(y: np.ndarray) -> dict[str, np.ndarray]:
    import librosa

    n_expected = 1 + len(y) // HOP
    # frame k is centred on sample k*HOP: pad half a window each side, then frame without centring.
    yp = np.pad(y, (N_FFT // 2, N_FFT // 2 + HOP))
    out: dict[str, list] = {"rms": [], "flat": [], "cent": [], "chroma": []}
    # chunk the STFT so a 3-hour file doesn't need a 1 GB spectrogram at once
    chunk = HOP * 20000
    for s in range(0, n_expected * HOP, chunk):
        seg = yp[s: s + chunk + N_FFT - HOP]
        if len(seg) < N_FFT:
            break
        S = np.abs(librosa.stft(seg, n_fft=N_FFT, hop_length=HOP, center=False))
        out["rms"].append(librosa.feature.rms(S=S, frame_length=N_FFT)[0])
        out["flat"].append(librosa.feature.spectral_flatness(S=S)[0])
        out["cent"].append(librosa.feature.spectral_centroid(S=S, sr=SR)[0])
        out["chroma"].append(librosa.feature.chroma_stft(S=S ** 2, sr=SR, n_fft=N_FFT))
    feats = {
        "rms": np.concatenate(out["rms"]).astype(np.float32),
        "flat": np.concatenate(out["flat"]).astype(np.float16),
        "cent": np.concatenate(out["cent"]).astype(np.float16),
        "chroma": np.concatenate(out["chroma"], axis=1).astype(np.float16),
    }
    for k in feats:  # chunk overlap can add a frame or two; trim to the nominal frame count
        feats[k] = feats[k][..., :n_expected]
    return feats


def load(rel_path: str, size: int | None) -> dict[str, np.ndarray] | None:
    fp = feature_path(rel_path, size)
    if not fp.exists():
        return None
    try:
        with np.load(fp) as z:
            return {k: z[k] for k in z.files}
    except (OSError, EOFError, ValueError, KeyError, zipfile.BadZipFile):
        # an evicted iCloud stub that can't be downloaded, or a truncated file: treat it as missing, so
        # ensure() recomputes it (and overwrites the bad file) and prefetch pulls it again
        return None


def ensure(rel_path: str, size: int | None) -> tuple[dict[str, np.ndarray], bool]:
    """Features for one NAS file, from cache or computed now. Returns (features, was_cached)."""
    got = load(rel_path, size)
    if got is not None:
        return got, True
    tmp = fetch(rel_path)
    try:
        y = decode(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    feats = compute(y)
    feats["duration"] = np.array([len(y) / SR], np.float64)
    FEAT_DIR.mkdir(parents=True, exist_ok=True)
    fp = feature_path(rel_path, size)
    part = fp.with_name(f"{fp.stem}.{os.getpid()}.part.npz")  # unique per worker process
    np.savez_compressed(part, **feats)
    os.replace(part, fp)
    return feats, False
