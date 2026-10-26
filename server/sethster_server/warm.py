"""Pre-transcode every track that needs it (flac/wav/wma...) into the cache, so first play is instant.

    uv run python -m sethster_server.warm [performance-id ...]
"""
import logging
import sys

from . import db as dbmod
from .config import load_settings
from .media import Transcoder, needs_transcode, safe_join


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    s = load_settings()
    if not s.media_root or not s.media_root.is_dir():
        print("MEDIA_ROOT not mounted", file=sys.stderr)
        return 1
    tc = Transcoder(s.cache_dir, s.ffmpeg, s.aac_encoder, s.aac_bitrate)
    conn = dbmod.connect(s.db_path, readonly=True)
    rows = conn.execute("SELECT id, performance_id, path, stream_path FROM tracks ORDER BY performance_id, idx").fetchall()
    todo = [r for r in rows if needs_transcode(r["stream_path"] or r["path"]) and (not argv or r["performance_id"] in argv)]
    for i, r in enumerate(todo, 1):
        src = safe_join(s.media_root, r["stream_path"] or r["path"])
        try:
            out = tc.ensure(src)
            print(f"[{i}/{len(todo)}] {r['id']} -> {out.name}")
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(todo)}] {r['id']} FAILED: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
