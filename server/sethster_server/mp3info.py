"""Pure-Python MP3 duration probe (first frame header + Xing/Info/VBRI), for tracks without a dossier duration.

Reads at most ~64 KB from the start of the file, so it is cheap even over SMB.
"""
from __future__ import annotations

import struct
from pathlib import Path

_BITRATES = {  # (version_is_mpeg1, layer) -> kbps table index 1..14
    (True, 3): [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (True, 2): [32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (True, 1): [32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (False, 3): [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (False, 2): [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (False, 1): [32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
}
_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _id3_size(head: bytes) -> int:
    if head[:3] != b"ID3" or len(head) < 10:
        return 0
    s = head[6:10]
    size = (s[0] << 21) | (s[1] << 14) | (s[2] << 7) | s[3]
    return 10 + size + (10 if head[5] & 0x10 else 0)


def parse_frame_header(b: bytes) -> dict | None:
    if len(b) < 4 or b[0] != 0xFF or (b[1] & 0xE0) != 0xE0:
        return None
    ver = (b[1] >> 3) & 3          # 3 = MPEG1, 2 = MPEG2, 0 = MPEG2.5, 1 reserved
    layer_bits = (b[1] >> 1) & 3   # 1 = Layer III, 2 = II, 3 = I
    if ver == 1 or layer_bits == 0:
        return None
    layer = 4 - layer_bits
    br_idx = b[2] >> 4
    sr_idx = (b[2] >> 2) & 3
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    mpeg1 = ver == 3
    kbps = _BITRATES[(mpeg1, layer)][br_idx - 1]
    rate = _RATES[ver][sr_idx]
    padding = (b[2] >> 1) & 1
    mono = (b[3] >> 6) == 3
    if layer == 1:
        samples = 384
        length = (12 * kbps * 1000 // rate + padding) * 4
    else:
        samples = 1152 if (layer == 2 or mpeg1) else 576
        length = samples // 8 * kbps * 1000 // rate + padding
    return {"mpeg1": mpeg1, "layer": layer, "kbps": kbps, "rate": rate, "samples": samples, "mono": mono,
            "length": length}


def duration_from_bytes(data: bytes, file_size: int) -> float | None:
    """Duration in seconds from the first bytes of an MP3 file of `file_size` bytes."""
    off = _id3_size(data)
    end = min(len(data) - 4, off + 32768)
    while off < end:
        if data[off] == 0xFF:
            h = parse_frame_header(data[off:off + 4])
            if h and h["length"] > 0:
                nxt = off + h["length"]
                # require a second frame header right after (avoids false syncs), unless near the buffer end
                if nxt + 4 <= len(data) and not parse_frame_header(data[nxt:nxt + 4]):
                    off += 1
                    continue
                frame = data[off:off + h["length"]]
                # Xing / Info (inside the first frame, after the side info)
                side = (17 if h["mono"] else 32) if h["mpeg1"] else (9 if h["mono"] else 17)
                x = 4 + side
                tag = frame[x:x + 4]
                if tag in (b"Xing", b"Info") and len(frame) >= x + 12:
                    flags = struct.unpack(">I", frame[x + 4:x + 8])[0]
                    if flags & 1:
                        frames = struct.unpack(">I", frame[x + 8:x + 12])[0]
                        if frames:
                            return frames * h["samples"] / h["rate"]
                v = frame.find(b"VBRI", 0, 64)
                if v == 36 and len(frame) >= v + 18:
                    frames = struct.unpack(">I", frame[v + 14:v + 18])[0]
                    if frames:
                        return frames * h["samples"] / h["rate"]
                audio = max(file_size - off, 0)
                return audio * 8 / (h["kbps"] * 1000)
        off += 1
    return None


def mp3_duration(path: Path) -> float | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(10)
            skip = _id3_size(head)
            if skip > 4 * 1024 * 1024:
                return None
            f.seek(0)
            data = f.read(skip + 65536)
    except OSError:
        return None
    return duration_from_bytes(data, size)
