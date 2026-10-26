"""Accounts: users, sessions and push devices in accounts.db (docs/plans/accounts.md, docs/api-accounts.md).

accounts.db lives next to rotation.db under SETHSTER_DATA (or SETHSTER_ACCOUNTS_DB) and the importer never
touches it. Every refusal is an AccountError, which the app turns into `{"code": ..., "detail": ...}` with the
error's status (see docs/api-accounts.md for the codes the app matches on).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','disabled')),
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, device TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS push_devices (
  apns_token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, env TEXT NOT NULL, updated_at TEXT NOT NULL,
  session_hash TEXT
);
CREATE INDEX IF NOT EXISTS push_devices_user ON push_devices(user_id);
"""

STATUSES = ("pending", "approved", "rejected", "disabled")
PUSH_ENVS = ("sandbox", "production")
SESSION_DAYS = 180
USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{3,32}")
APNS_TOKEN_RE = re.compile(r"[0-9A-Fa-f]{32,200}")

# scrypt n=2**14 r=8 p=1 uses 16 MB per hash; at most two at a time so a burst of logins can't starve the NAS.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
_hash_slots = threading.BoundedSemaphore(2)


class AccountError(Exception):
    """A refusal the app matches on by `code`; rendered as {"code", "detail"} with `status` (app.py)."""

    def __init__(self, status: int, code: str, detail: str | None = None, headers: dict | None = None):
        super().__init__(code)
        self.status, self.code, self.detail, self.headers = status, code, detail or code, headers


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- passwords ------------------------------------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    with _hash_slots:
        return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, salt and hash base64."""
    salt = os.urandom(16)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(_scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P))}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        got = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


_dummy: list[str] = []


def dummy_hash() -> str:
    """A real hash of a random password: login with an unknown username verifies against it, so it costs the
    same time as a wrong password."""
    if not _dummy:
        _dummy.append(hash_password(secrets.token_urlsafe(16)))
    return _dummy[0]


def check_username(username: str) -> str:
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise AccountError(422, "invalid_username", "username must be 3-32 letters, digits, '.', '_' or '-'")
    return username


def check_password(password: str) -> str:
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise AccountError(422, "invalid_password", "password must be 8-128 characters")
    return password


def check_display_name(name: str | None, username: str) -> str:
    name = (name or "").strip() or username
    if len(name) > 64:
        raise AccountError(422, "invalid_display_name", "display name must be at most 64 characters")
    return name


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def me(user) -> dict:
    """The `Me` shape the app reads."""
    return {"user_id": user["id"], "username": user["username"], "display_name": user["display_name"],
            "status": user["status"], "is_admin": bool(user["is_admin"])}


def admin_shape(user) -> dict:
    return {**me(user), "created_at": user["created_at"], "decided_at": user["decided_at"],
            "decided_by": user["decided_by"]}


def listener(user_id: int) -> str:
    """The Rotation listener for an account."""
    return f"u:{user_id}"


class AccountsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._ready = False

    def connect(self) -> sqlite3.Connection:
        if not self._ready:
            with self._lock:
                if not self._ready:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    c = sqlite3.connect(self.path)
                    c.executescript(SCHEMA)
                    c.commit()
                    c.close()
                    self._ready = True
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _tx(self, *, write: bool = False):
        conn = self.connect()
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    # ---- users ------------------------------------------------------------------------------------------
    def create_user(self, username: str, password: str, display_name: str | None = None, *,
                    status: str = "pending", is_admin: bool = False) -> sqlite3.Row:
        check_username(username)
        check_password(password)
        name = check_display_name(display_name, username)
        pw = hash_password(password)  # outside the write transaction: it takes ~100 ms
        t = iso(now())
        with self._tx(write=True) as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise AccountError(409, "username_unavailable", "username unavailable")
            decided = t if status != "pending" else None
            uid = c.execute("INSERT INTO users (username, pw_hash, display_name, status, is_admin, created_at, "
                            "decided_at) VALUES (?,?,?,?,?,?,?)",
                            (username, pw, name, status, int(is_admin), t, decided)).lastrowid
            return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def user_ids(self) -> set[int]:
        with self._tx() as c:
            return {r[0] for r in c.execute("SELECT id FROM users")}

    def user(self, user_id: int) -> sqlite3.Row | None:
        with self._tx() as c:
            return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def user_by_name(self, username: str) -> sqlite3.Row | None:
        with self._tx() as c:
            return c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

    def users(self, status: str | None = None) -> list[sqlite3.Row]:
        with self._tx() as c:
            if status:
                return c.execute("SELECT * FROM users WHERE status=? ORDER BY created_at, id", (status,)).fetchall()
            return c.execute("SELECT * FROM users ORDER BY created_at, id").fetchall()

    def pending_count(self) -> int:
        with self._tx() as c:
            return c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0]

    def admin_count(self) -> int:
        """Approved admins: the people who can approve sign-ups."""
        with self._tx() as c:
            return c.execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND status='approved'").fetchone()[0]

    def authenticate(self, username: str, password: str) -> sqlite3.Row | None:
        """The user if the password matches. An unknown username verifies against the dummy hash, so both
        failures take the same time and give the same answer."""
        u = self.user_by_name(username) if isinstance(username, str) else None
        ok = verify_password(password or "", u["pw_hash"] if u else dummy_hash())
        return u if u and ok else None

    def set_password(self, user_id: int, password: str, keep: str | None = None, *,
                     pw_hash: str | None = None) -> int:
        """New password and every other session revoked, in one transaction (a login that checked the old
        password can't then get a session: create_session checks the hash it saw). pw_hash: the hash the old
        password was checked against; if it changed since, or the `keep` session was revoked, nothing changes
        (401). Returns sessions revoked."""
        check_password(password)
        pw = hash_password(password)
        with self._tx(write=True) as c:
            if pw_hash is not None:
                u = c.execute("SELECT pw_hash FROM users WHERE id=?", (user_id,)).fetchone()
                live = keep is None or c.execute("SELECT 1 FROM sessions WHERE token_hash=?", (keep,)).fetchone()
                if u is None or u["pw_hash"] != pw_hash or not live:
                    raise AccountError(401, "signed_out", "the password changed; sign in again")
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw, user_id))
            return c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash IS NOT ?", (user_id, keep)).rowcount

    def set_status(self, user_id: int, status: str, by: int | None) -> sqlite3.Row:
        """Rejected/disabled also revokes every session."""
        assert status in STATUSES
        with self._tx(write=True) as c:
            c.execute("UPDATE users SET status=?, decided_at=?, decided_by=? WHERE id=?",
                      (status, iso(now()), by, user_id))
            if status in ("rejected", "disabled"):
                c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def make_admin(self, user_id: int, by: int | None) -> sqlite3.Row:
        """Grants admin and revokes the user's sessions (they log in again to pick it up)."""
        with self._tx(write=True) as c:
            c.execute("UPDATE users SET is_admin=1, decided_at=?, decided_by=? WHERE id=?",
                      (iso(now()), by, user_id))
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def delete_user(self, user_id: int) -> None:
        """The user, their sessions and push devices. Their Rotation lives in rotation.db; the caller clears it."""
        with self._tx(write=True) as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM push_devices WHERE user_id=?", (user_id,))
            c.execute("UPDATE users SET decided_by=NULL WHERE decided_by=?", (user_id,))
            c.execute("DELETE FROM users WHERE id=?", (user_id,))

    # ---- sessions ---------------------------------------------------------------------------------------
    def create_session(self, user_id: int, device: str | None = None, *, pw_hash: str | None = None) -> str:
        """pw_hash: the hash the password was checked against. If the password changed (or the user was
        deleted) since, no session: 401 bad_credentials."""
        token = secrets.token_urlsafe(32)
        t = now()
        with self._tx(write=True) as c:
            u = c.execute("SELECT pw_hash FROM users WHERE id=?", (user_id,)).fetchone()
            if u is None or (pw_hash is not None and u["pw_hash"] != pw_hash):
                raise AccountError(401, "bad_credentials", "wrong username or password")
            c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                      (token_hash(token), user_id, iso(t), iso(t), iso(t + dt.timedelta(days=SESSION_DAYS)),
                       (device or "")[:64] or None))
        return token

    def session(self, token: str | None) -> tuple[str, sqlite3.Row] | None:
        """(token hash, user) for a live session, else None. Expiry slides: bumped at most once a day."""
        if not token:
            return None
        h = token_hash(token)
        with self._tx() as c:
            s = c.execute("SELECT * FROM sessions WHERE token_hash=?", (h,)).fetchone()
            u = c.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone() if s else None
        if not s or not u:
            return None
        t = now()
        if parse_iso(s["expires_at"]) <= t:
            self.revoke(h)
            return None
        if t - parse_iso(s["last_seen_at"]) >= dt.timedelta(days=1):
            with self._tx(write=True) as c:
                c.execute("UPDATE sessions SET last_seen_at=?, expires_at=? WHERE token_hash=?",
                          (iso(t), iso(t + dt.timedelta(days=SESSION_DAYS)), h))
        return h, u

    def revoke(self, session_hash: str) -> None:
        """Ends one session and drops the push device registered through it."""
        with self._tx(write=True) as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (session_hash,))
            c.execute("DELETE FROM push_devices WHERE session_hash=?", (session_hash,))

    def revoke_user_sessions(self, user_id: int, keep: str | None = None) -> int:
        with self._tx(write=True) as c:
            return c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash IS NOT ?", (user_id, keep)).rowcount

    # ---- push devices -----------------------------------------------------------------------------------
    def register_device(self, apns_token: str, user_id: int, env: str, session_hash: str | None) -> None:
        """Keyed by the device token alone: re-registering under another account moves it. The session is
        rechecked in the same transaction, so a registration racing sign-out or deletion can't outlive it."""
        if not isinstance(apns_token, str) or not APNS_TOKEN_RE.fullmatch(apns_token):
            raise AccountError(422, "invalid_push_token", "apns_token must be hex")
        if env not in PUSH_ENVS:
            raise AccountError(422, "invalid_push_env", f"env must be one of {list(PUSH_ENVS)}")
        with self._tx(write=True) as c:
            if not c.execute("SELECT 1 FROM sessions WHERE token_hash=? AND user_id=?", (session_hash, user_id)).fetchone():
                raise AccountError(401, "signed_out", "sign in required", headers={"WWW-Authenticate": "Bearer"})
            c.execute("INSERT OR REPLACE INTO push_devices VALUES (?,?,?,?,?)",
                      (apns_token.lower(), user_id, env, iso(now()), session_hash))

    def unregister_session_device(self, session_hash: str) -> int:
        with self._tx(write=True) as c:
            return c.execute("DELETE FROM push_devices WHERE session_hash=?", (session_hash,)).rowcount

    def drop_device(self, apns_token: str) -> None:
        with self._tx(write=True) as c:
            c.execute("DELETE FROM push_devices WHERE apns_token=?", (apns_token.lower(),))

    def devices(self, user_id: int) -> list[sqlite3.Row]:
        with self._tx() as c:
            return c.execute("SELECT * FROM push_devices WHERE user_id=?", (user_id,)).fetchall()

    def admin_devices(self) -> list[sqlite3.Row]:
        with self._tx() as c:
            return c.execute("""SELECT d.* FROM push_devices d JOIN users u ON u.id=d.user_id
                                WHERE u.is_admin=1 AND u.status='approved'""").fetchall()
