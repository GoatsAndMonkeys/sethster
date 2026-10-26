"""Media access on the read-only share: path safety, artwork discovery, transcoding, image cache.

Nothing in this module ever writes under MEDIA_ROOT. All derived files go to the cache dir.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".mp4", ".flac", ".wav", ".wma", ".ogg", ".oga", ".opus", ".ape", ".aif", ".aiff", ".alac"}
DIRECT_MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


def is_audio(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in AUDIO_EXTS


def needs_transcode(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() not in DIRECT_MIME


def safe_join(root: Path, rel: str) -> Path:
    """Join a share-relative path onto MEDIA_ROOT, refusing anything that escapes it."""
    rel = rel.replace("\\", "/").lstrip("/")
    p = PurePosixPath(rel)
    if any(part == ".." for part in p.parts):
        raise ValueError(f"unsafe path {rel!r}")
    return root.joinpath(*p.parts)


# ---- artwork --------------------------------------------------------------------------------------

# Desktop screenshots saved next to a recording are web pages or program listings (often of other broadcasts),
# and get cropped into squares: never cover art.
SCREENSHOT = re.compile(r"screen ?shot|bildschirmfoto|capture d.[ée]cran", re.I)
BACK_ART = re.compile(r"back|rear|inlay|tray|inside|booklet|insert|label|\bcd ?\d|\bdisc\b", re.I)
MAX_ASPECT = 2.0  # a cover is shown square; anything wider or taller than 2:1 is a banner, a strip or a scan spread


def is_screenshot(name: str) -> bool:
    return bool(SCREENSHOT.search(name))


def _art_rank(name: str, size: int) -> tuple:
    n = name.lower()
    stem = n.rsplit(".", 1)[0]
    if stem in ("folder", "cover", "front"):
        tier = 0
    elif re.match(r"(cover|front|folder)\b", stem):
        tier = 1
    elif stem.startswith("albumart") and "large" in stem:
        tier = 2
    elif stem.startswith("albumart") and "small" not in stem:
        tier = 3
    elif stem.startswith("albumart") or "thumb" in stem:
        tier = 7  # tiny thumbnails last
    elif BACK_ART.search(stem):
        tier = 5  # back covers, inlays, booklet pages, disc labels: after any plain picture
    else:
        tier = 4
    return (tier, -size)


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the first bytes of a JPEG, PNG, GIF or WebP file (no Pillow on the server)."""
    import struct
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if data[12:16] == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if data[12:16] == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if data[12:16] == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
            return None
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 <= len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                    i += 1 if marker == 0xFF else 2
                    continue
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + seg
    except (struct.error, IndexError):
        return None
    return None


def _read_size(media_root: Path | None, path: str) -> tuple[int, int]:
    """Pixel size of an image on the share, (0, 0) when it can't be read (so it is never used)."""
    if not media_root:
        return (0, 0)
    try:
        with open(safe_join(media_root, path), "rb") as fh:
            return image_size(fh.read(256 * 1024)) or (0, 0)
    except (OSError, ValueError):
        return (0, 0)


def _usable(path: str, dims: tuple | None, allow) -> bool:
    pp = PurePosixPath(path)
    name = pp.name
    if name.startswith("._") or pp.suffix.lower() not in IMAGE_EXTS or is_screenshot(name):
        return False
    if any(part.startswith(("@", "#", ".")) for part in pp.parts[:-1]):
        return False  # Synology @eaDir thumbnails (often of other recordings' embedded art), #recycle, hidden dirs
    if dims is not None:
        w, h = dims
        if not (w and h) or max(w, h) / min(w, h) > MAX_ASPECT:
            return False
    return allow is None or allow(path)


def find_artwork(media_root: Path | None, dirs: list[str], hints: list[tuple[str, int]] = (),
                 images: list[dict] | None = None, allow=None) -> str | None:
    """Return the share-relative path of the best image for one recording.

    `images` is the NAS photo scan of the recording's folders (data/media/nas/<id>.json "images": path, bytes,
    w, h), so those folders need not be read from the slow share. The share-relative `dirs` are listed under
    media_root (pass only folders the scan did not cover). `hints` are (path, size) image sidecars from the dossier.
    `allow(path)` says whether an image lies in the recording's own folder (not a parent shared with other
    recordings). Screenshots, images beyond 2:1 and images whose size can't be read are never used.
    """
    dims: dict[str, tuple] = {}
    candidates: dict[str, tuple] = {}
    for im in images or ():
        if not isinstance(im, dict) or not isinstance(im.get("path"), str):
            continue
        # an image the scan could not open has no size, so it is never used
        dims[im["path"]] = (0, 0) if im.get("error") else (im.get("w") or 0, im.get("h") or 0)
        if _usable(im["path"], dims[im["path"]], allow):
            candidates[im["path"]] = _art_rank(PurePosixPath(im["path"]).name, im.get("bytes") or 0)
    for path, size in hints:
        # one the scan saw was judged above; for the rest the size is read from the file (one small read, and
        # only for a file that passes every other test)
        if path not in dims and _usable(path, None, allow) and _usable(path, _read_size(media_root, path), None):
            candidates[path] = _art_rank(PurePosixPath(path).name, size or 0)
    if dirs and media_root and media_root.is_dir():
        for d in dirs:
            try:
                full = safe_join(media_root, d)
                with os.scandir(full) as it:
                    for e in it:
                        rel = f"{d.rstrip('/')}/{e.name}" if d else e.name
                        if rel not in dims and rel not in candidates and e.is_file() and _usable(rel, None, allow) \
                                and _usable(rel, _read_size(media_root, rel), None):
                            candidates[rel] = _art_rank(e.name, e.stat().st_size)
            except (OSError, ValueError):
                continue
    if not candidates:
        return None
    return min(candidates.items(), key=lambda kv: (kv[1], kv[0]))[0]


# ---- transcoding ------------------------------------------------------------------------------------

def _pid_alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


class Transcoder:
    """Transcodes lossless/odd formats to AAC (.m4a, faststart) into the cache, one job per file."""

    def __init__(self, cache_dir: Path, ffmpeg: str | None, encoder: str = "aac", bitrate: str = "256k"):
        self.dir = cache_dir / "transcode"
        self.ffmpeg = ffmpeg
        self.encoder = encoder
        self.bitrate = bitrate
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prewarm")
        # drop partial outputs left by a killed process (older than 2 h)
        if self.dir.is_dir():
            import time
            for p in self.dir.glob("*.part*"):
                try:
                    if time.time() - p.stat().st_mtime > 7200 or not _pid_alive(p.suffix.removeprefix(".part")):
                        p.unlink()
                except OSError:
                    pass

    def cache_path(self, src: Path) -> Path:
        st = src.stat()
        key = hashlib.sha1(f"{src}|{st.st_size}|{int(st.st_mtime)}|{self.encoder}|{self.bitrate}".encode()).hexdigest()
        return self.dir / f"{key}.m4a"

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def ensure(self, src: Path) -> Path:
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg not available")
        out = self.cache_path(src)
        if out.exists():
            return out
        with self._lock(str(out)):
            if out.exists():
                return out
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(f".part{os.getpid()}")
            cmd = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                   "-vn", "-map", "0:a:0", "-c:a", self.encoder, "-b:a", self.bitrate,
                   "-movflags", "+faststart", "-f", "mp4", str(tmp)]
            log.info("transcoding %s", src)
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
            except subprocess.CalledProcessError as e:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"ffmpeg failed: {e.stderr.decode(errors='replace')[-500:]}") from e
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            os.replace(tmp, out)
            return out

    def prewarm(self, srcs: list[Path]) -> None:
        for s in srcs:
            self._pool.submit(self._safe_ensure, s)

    def _safe_ensure(self, src: Path) -> None:
        try:
            self.ensure(src)
        except Exception as e:  # noqa: BLE001
            log.warning("prewarm failed for %s: %s", src, e)


# ---- remote images (Wikimedia) ---------------------------------------------------------------------

USER_AGENT = "Sethster/0.1 (private non-commercial family music library; python-urllib)"


class ImageCache:
    def __init__(self, cache_dir: Path):
        self.dir = cache_dir / "images"
        self._failed: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _thumb_url(url: str, width: int = 800) -> str:
        if "Special:FilePath" in url and "width=" not in url:
            return url + ("&" if "?" in url else "?") + f"width={width}"
        return url

    def get(self, kind: str, ident: str, url: str) -> Path | None:
        key = hashlib.sha1(url.encode()).hexdigest()[:16]
        base = self.dir / kind
        for ext in (".jpg", ".png", ".webp", ".gif", ".img"):
            p = base / f"{ident}-{key}{ext}"
            if p.exists():
                return p
        if url in self._failed:
            return None
        with self._lock:
            try:
                req = urllib.request.Request(self._thumb_url(url), headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read(20_000_000)
                    ctype = r.headers.get("Content-Type", "")
            except Exception as e:  # noqa: BLE001
                log.warning("image fetch failed %s: %s", url, e)
                self._failed.add(url)
                return None
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(
                ctype.split(";")[0].strip(), Path(urllib.parse.urlparse(url).path).suffix.lower() or ".img")
            if ext not in IMAGE_MIME and ext != ".img":
                ext = ".img"
            base.mkdir(parents=True, exist_ok=True)
            p = base / f"{ident}-{key}{ext}"
            tmp = p.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, p)
            return p
