#!/usr/bin/env python3
"""Local, dependency-free server for the Sethster GT-C landmark tapper."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root
from pipeline import localconfig  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]
PAGE = Path(__file__).with_name("index.html")
PLAN = ROOT / "data/gt/taps/plan.json"
TAPS = ROOT / "data/gt/taps"
CACHE = ROOT / "data/gt/audio-cache"
# NAS ssh login and sandbox root: SETHSTER_NAS_SSH / SETHSTER_NAS_ROOT (environment or local.env).
NAS_HOST = localconfig.get("SETHSTER_NAS_SSH", "")
NAS_ROOT = (localconfig.get("SETHSTER_NAS_ROOT") or "").rstrip("/")
MAX_BODY = 2 * 1024 * 1024
FETCH_LOCKS: dict[str, threading.Lock] = {}
FETCH_LOCKS_LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_plan():
    return json.loads(PLAN.read_text(encoding="utf-8"))


def plan_lookup(work_id=None, performance_id=None):
    plan = load_plan()
    work = next((x for x in plan["works"] if x["id"] == work_id), None) if work_id else None
    perf = next((x for x in work["performances"] if x["id"] == performance_id), None) if work and performance_id else None
    return plan, work, perf


def state_path(work_id, performance_id):
    _, work, perf = plan_lookup(work_id, performance_id)
    if not work or not perf:
        raise ValueError("Unknown work or performance")
    return TAPS / work["id"] / f"{perf['id']}.json"


def cache_path(remote_path):
    digest = hashlib.sha256(remote_path.encode()).hexdigest()[:24]
    suffix = Path(remote_path).suffix.lower() or ".audio"
    return CACHE / f"{digest}{suffix}"


def fetch(remote_path):
    destination = cache_path(remote_path)
    if destination.is_file() and destination.stat().st_size:
        return destination
    with FETCH_LOCKS_LOCK:
        lock = FETCH_LOCKS.setdefault(remote_path, threading.Lock())
    with lock:
        if destination.is_file() and destination.stat().st_size:
            return destination
        CACHE.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        if not NAS_HOST or not NAS_ROOT:
            raise OSError("set SETHSTER_NAS_SSH and SETHSTER_NAS_ROOT (environment or local.env)")
        remote = NAS_ROOT + "/" + remote_path.lstrip("/")
        try:
            with temporary.open("wb") as output:
                result = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", NAS_HOST,
                     f"cat -- {shlex.quote(remote)}"],
                    stdout=output, stderr=subprocess.PIPE, timeout=900, check=False,
                )
            if result.returncode or not temporary.stat().st_size:
                message = result.stderr.decode("utf-8", "replace").strip()
                raise OSError(f"NAS fetch failed ({result.returncode}): {message}")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


class Handler(BaseHTTPRequestHandler):
    server_version = "SethsterTapper/1"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def json_response(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error_json(self, status, message):
        self.json_response({"error": message}, status)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("Invalid request size")
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/plan":
            self.json_response(load_plan())
        elif parsed.path == "/api/calibration":
            path = TAPS / "calibration.json"
            self.json_response(json.loads(path.read_text()) if path.exists() else None)
        elif parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            try:
                path = state_path(query.get("work", [""])[0], query.get("performance", [""])[0])
                self.json_response(json.loads(path.read_text()) if path.exists() else {"taps": []})
            except ValueError as exc:
                self.error_json(404, str(exc))
        elif parsed.path == "/api/progress":
            states = {}
            for work_dir in TAPS.iterdir() if TAPS.exists() else []:
                if not work_dir.is_dir():
                    continue
                for path in work_dir.glob("*.json"):
                    try:
                        data = json.loads(path.read_text())
                        states[f"{data['work_id']}/{data['performance_id']}"] = data
                    except (OSError, ValueError, KeyError):
                        pass
            self.json_response(states)
        elif parsed.path.startswith("/audio/"):
            self.serve_audio(parsed.path, head=False)
        else:
            self.error_json(404, "Not found")

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/audio/"):
            self.serve_audio(parsed.path, head=True)
        else:
            self.send_error(404)

    def serve_audio(self, url_path, head):
        parts = [unquote(x) for x in url_path.split("/") if x]
        if len(parts) != 3 or not parts[2].isdigit():
            return self.error_json(404, "Invalid audio URL")
        perf_id, index = parts[1], int(parts[2])
        plan = load_plan()
        perf = next((p for w in plan["works"] for p in w["performances"] if p["id"] == perf_id), None)
        if not perf or not (0 <= index < len(perf["tracks"])):
            return self.error_json(404, "Unknown audio track")
        try:
            path = fetch(perf["tracks"][index]["path"])
            size = path.stat().st_size
            start, end, status = 0, size - 1, HTTPStatus.OK
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
                if not match or (not match.group(1) and not match.group(2)):
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                if match.group(1):
                    start = int(match.group(1)); end = int(match.group(2) or size - 1)
                else:
                    count = int(match.group(2)); start = max(0, size - count); end = size - 1
                if start >= size or start > end:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1); status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not head:
                with path.open("rb") as handle:
                    handle.seek(start)
                    remaining = length
                    while remaining:
                        chunk = handle.read(min(128 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk); remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, subprocess.SubprocessError) as exc:
            if not self.wfile.closed:
                self.error_json(502, str(exc))

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
            if parsed.path == "/api/calibration":
                if not isinstance(data.get("raw_taps"), list) or not isinstance(data.get("median_latency_ms"), (int, float)):
                    raise ValueError("Invalid calibration")
                data["saved_at"] = now()
                atomic_json(TAPS / "calibration.json", data)
                self.json_response({"ok": True})
            elif parsed.path == "/api/state":
                path = state_path(data.get("work_id"), data.get("performance_id"))
                if not isinstance(data.get("taps"), list):
                    raise ValueError("Invalid tap state")
                data["schema"] = "sethster.gt-c-taps/1"; data["updated_at"] = now()
                atomic_json(path, data)
                self.json_response({"ok": True, "updated_at": data["updated_at"]})
            elif parsed.path == "/api/landmarks":
                plan, work, _ = plan_lookup(data.get("work_id"))
                landmarks = data.get("landmarks")
                if not work or not isinstance(landmarks, list) or not landmarks:
                    raise ValueError("Invalid landmark update")
                clean = []
                seen = set()
                for n, item in enumerate(landmarks):
                    landmark_id = str(item.get("id") or f"lm-user-{n + 1}")
                    label = str(item.get("label") or "").strip()[:240]
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", landmark_id) or landmark_id in seen or not label:
                        raise ValueError("Invalid landmark")
                    seen.add(landmark_id)
                    clean.append({"id": landmark_id, "label": label, "source": item.get("source", "user"), "skipped": bool(item.get("skipped"))})
                work["landmarks"] = clean
                atomic_json(PLAN, plan)
                self.json_response({"ok": True, "landmarks": clean})
            elif parsed.path == "/api/clear-cache":
                CACHE.mkdir(parents=True, exist_ok=True)
                removed = 0
                for item in CACHE.iterdir():
                    if item.is_file() or item.is_symlink():
                        item.unlink(); removed += 1
                    elif item.is_dir():
                        shutil.rmtree(item); removed += 1
                self.json_response({"ok": True, "removed": removed})
            else:
                self.error_json(404, "Not found")
        except (ValueError, json.JSONDecodeError) as exc:
            self.error_json(400, str(exc))
        except OSError as exc:
            self.error_json(500, str(exc))


def main():
    if not PLAN.exists():
        raise SystemExit(f"Missing {PLAN}; run build_plan.py")
    TAPS.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Sethster landmark tapper: http://127.0.0.1:8765", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
