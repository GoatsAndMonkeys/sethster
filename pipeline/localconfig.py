"""Private infrastructure settings (NAS ssh login, share names and paths).

Values come from environment variables, or from a gitignored `local.env` at the repo root
(KEY=value lines; copy `local.env.example`). Environment variables win over the file.
There are no built-in real defaults: anything that needs the NAS fails with a clear message
until these are set.

  SETHSTER_NAS_SSH         ssh login for the NAS, e.g. user@nas.local
  SETHSTER_NAS_ROOT        root of the read-only sandbox share on the NAS, e.g. /volume1/music_sandbox
  SETHSTER_SANDBOX_SHARE   SMB share name of that sandbox, e.g. music_sandbox (tagger safety check)
  SETHSTER_ORIGINAL_SHARE  SMB share name of the original collection that must never be written
  SETHSTER_TAG_MOUNT       default read-write mount point of the sandbox share for tag_sandbox.py
  SETHSTER_CONTACT_EMAIL   contact address sent in the User-Agent of web requests
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / "local.env"


def _load() -> None:
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


_load()


def get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or default


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set (environment or {ENV_FILE.name}; see local.env.example)")
    return value


def nas_ssh() -> str:
    return require("SETHSTER_NAS_SSH")


def nas_root() -> str:
    return require("SETHSTER_NAS_ROOT").rstrip("/")


def contact_email() -> str:
    return get("SETHSTER_CONTACT_EMAIL", "contact@example.com")
