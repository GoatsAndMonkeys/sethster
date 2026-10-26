"""Runtime configuration, all from environment variables (with dev-friendly defaults)."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# server/sethster_server/config.py -> project root is two levels above the package dir
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _path_env(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


@dataclass
class Settings:
    data_dir: Path
    db_path: Path
    cache_dir: Path
    media_root: Path | None
    tokens: list[str]
    ffmpeg: str | None
    aac_encoder: str
    aac_bitrate: str
    result_dirs: list[Path] = field(default_factory=list)
    wiki_dir: Path | None = None
    transcripts_dir: Path | None = None
    programs_dir: Path | None = None
    # "all": with SETHSTER_TOKENS set, every request needs a token. "funnel": only requests from the internet
    # (Tailscale Funnel) do; the tailnet and LAN stay open. Internet requests always need one (app.FunnelGuard).
    auth_scope: str = "all"
    # SETHSTER_ACCOUNTS (docs/plans/accounts.md): "off" = the legacy token auth above, unchanged; "dormant" =
    # accounts routes live but content open to anyone; "on" = an approved account for everything but
    # /api/health, sign-up and login (legacy tokens and FunnelGuard ignored).
    accounts_mode: str = "off"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.tokens)


ACCOUNT_MODES = ("off", "dormant", "on")


def _accounts_mode() -> str:
    v = os.environ.get("SETHSTER_ACCOUNTS", "off").strip().lower() or "off"
    if v not in ACCOUNT_MODES:
        raise ValueError(f"SETHSTER_ACCOUNTS must be one of {ACCOUNT_MODES}, not {v!r}")
    return v


def load_settings() -> Settings:
    data_dir = _path_env("SETHSTER_DATA", PROJECT_ROOT / "data")
    media = os.environ.get("MEDIA_ROOT")
    media_root = Path(media) if media else (Path("/media") if Path("/media").is_dir() else None)
    tokens = [t.strip() for t in os.environ.get("SETHSTER_TOKENS", "").split(",") if t.strip()]
    ffmpeg = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg and Path("/opt/homebrew/bin/ffmpeg").exists():
        ffmpeg = "/opt/homebrew/bin/ffmpeg"
    if os.environ.get("SETHSTER_RESULTS"):
        result_dirs = [Path(p) for p in os.environ["SETHSTER_RESULTS"].split(os.pathsep) if p]
    else:
        result_dirs = [data_dir / "pilot" / "results", data_dir / "research" / "results"]
    return Settings(
        data_dir=data_dir,
        db_path=_path_env("SETHSTER_DB", data_dir / "sethster.db"),
        cache_dir=_path_env("SETHSTER_CACHE", data_dir / "cache"),
        media_root=media_root,
        tokens=tokens,
        ffmpeg=ffmpeg,
        aac_encoder=os.environ.get("SETHSTER_AAC_ENCODER", "aac"),
        aac_bitrate=os.environ.get("SETHSTER_AAC_BITRATE", "256k"),
        result_dirs=result_dirs,
        wiki_dir=_path_env("SETHSTER_WIKI", data_dir / "wiki"),
        transcripts_dir=_path_env("SETHSTER_TRANSCRIPTS", data_dir / "transcripts"),
        programs_dir=_path_env("SETHSTER_PROGRAMS", data_dir / "programs"),
        accounts_mode=_accounts_mode(),
        auth_scope="funnel" if os.environ.get("SETHSTER_AUTH", "").strip().lower() == "funnel" else "all",
    )
