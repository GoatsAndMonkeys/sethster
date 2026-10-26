"""Shared safety guard + byte-region helpers for the sandbox tagger / untagger.

HARD RULE: nothing in this project may write to the ORIGINAL family share
(SMB share $SETHSTER_ORIGINAL_SHARE).  Every path that is about to be opened for
writing must pass `assert_writable()`, which requires that

  * the path's realpath lies inside the configured sandbox mount point,
  * that mount point is an SMB mount of the share named exactly $SETHSTER_SANDBOX_SHARE
    and is NOT mounted read-only,
  * no component of the path (case-insensitively) is the original share's name and
    "/volume1/<original share>" never appears.

Share names and the default mount come from the environment or local.env (pipeline/localconfig.py).
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
from pipeline import localconfig  # noqa: E402

DEFAULT_MOUNT = localconfig.get("SETHSTER_TAG_MOUNT")
SANDBOX_SHARE = localconfig.require("SETHSTER_SANDBOX_SHARE")
ORIGINAL_SHARE = localconfig.require("SETHSTER_ORIGINAL_SHARE")
MB = 1024 * 1024


class SafetyError(RuntimeError):
    pass


def _mount_table() -> list[tuple[str, str, str]]:
    out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        m = re.match(r"^(.*?) on (.*?) \((.*)\)$", line)
        if m:
            rows.append((m.group(1), m.group(2), m.group(3)))
    return rows


def verify_mount(mount: str, need_rw: bool) -> str:
    """Return realpath of the mount after checking it is the sandbox share."""
    real = os.path.realpath(mount)
    for src, mp, opts in _mount_table():
        if os.path.realpath(mp) == real:
            share = src.rstrip("/").rsplit("/", 1)[-1]
            if share != SANDBOX_SHARE:
                raise SafetyError(f"{mount} is share {share!r}, not {SANDBOX_SHARE!r}")
            if need_rw and "read-only" in opts:
                raise SafetyError(f"{mount} is mounted read-only")
            return real
    raise SafetyError(f"{mount} is not a mount point")


def _forbidden(p: str) -> bool:
    low = p.lower()
    orig = ORIGINAL_SHARE.lower()
    if f"/volume1/{orig}/" in low + "/" and f"/volume1/{SANDBOX_SHARE.lower()}" not in low:
        return True
    # any path component named exactly like the original share root
    return any(part.lower() == orig for part in p.split("/"))


def assert_writable(path: str, mount_real: str) -> str:
    """Raise unless `path` is safely inside the sandbox mount. Returns realpath."""
    if _forbidden(path):
        raise SafetyError(f"refusing path containing the original share root: {path}")
    real = os.path.realpath(path)
    if _forbidden(real):
        raise SafetyError(f"refusing realpath containing the original share root: {real}")
    if not real.startswith(mount_real.rstrip("/") + "/"):
        raise SafetyError(f"refusing path outside sandbox mount {mount_real}: {real}")
    return real


# ---------------------------------------------------------------- hashing
def head_tail_hashes(path: str) -> dict:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(MB)
        f.seek(max(0, size - MB))
        tail = f.read(MB)
    return {"size": size,
            "sha256_first_1mb": hashlib.sha256(head).hexdigest(),
            "sha256_last_1mb": hashlib.sha256(tail).hexdigest()}


# ---------------------------------------------------------------- tag regions
def id3v2_region_len(head: bytes) -> int:
    """Length of a leading ID3v2 tag (incl. padding/footer), 0 if none."""
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    s = head[6:10]
    size = (s[0] << 21) | (s[1] << 14) | (s[2] << 7) | s[3]
    footer = 10 if head[5] & 0x10 else 0
    return 10 + size + footer


def flac_region_len(f) -> int:
    """Bytes before the first FLAC audio frame (optional ID3v2 + fLaC + blocks)."""
    f.seek(0)
    head = f.read(10)
    off = id3v2_region_len(head)
    f.seek(off)
    if f.read(4) != b"fLaC":
        raise ValueError("not a FLAC stream")
    off += 4
    while True:
        f.seek(off)
        h = f.read(4)
        if len(h) < 4:
            raise ValueError("truncated FLAC metadata")
        last = h[0] & 0x80
        length = int.from_bytes(h[1:4], "big")
        off += 4 + length
        if last:
            return off


def read_region(path: str, fmt: str) -> tuple[bytes, bytes]:
    """(leading tag region, trailing ID3v1 block) for byte-exact restore."""
    with open(path, "rb") as f:
        if fmt == "mp3":
            head = f.read(10)
            n = id3v2_region_len(head)
            f.seek(0)
            region = f.read(n) if n else b""
            size = os.fstat(f.fileno()).st_size
            v1 = b""
            if size >= 128 + n:
                f.seek(size - 128)
                t = f.read(128)
                if t[:3] == b"TAG":
                    v1 = t
            return region, v1
        if fmt == "flac":
            n = flac_region_len(f)
            f.seek(0)
            return f.read(n), b""
    raise ValueError(fmt)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))
