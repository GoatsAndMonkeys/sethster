"""Accounts routes and the auth dependencies for SETHSTER_ACCOUNTS=dormant|on (docs/plans/accounts.md).

Route shapes and error codes: docs/api-accounts.md. `build(settings)` returns an Accounts holding the router and
the dependencies app.py mounts on every content router:
- current_session: a live session, any status (401 signed_out otherwise);
- approved_user: plus status approved, re-read from the DB on every request (403 pending/rejected/disabled);
- admin_user: plus is_admin (403 not_admin);
- content: approved_user in `on`; in `dormant` anyone, with request.state.account set when an approved session
  came along (that is what gives a signed-in listener their own Rotation).
"""
from __future__ import annotations

import asyncio
import collections
import ipaddress
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from . import accounts_store as acc
from .accounts_store import AccountError, AccountsStore
from .apns import ApnsSender
from .rotation_store import RotationStore, rotation_db_path

log = logging.getLogger("sethster.accounts")

# GET/HEAD on these may carry the session as ?token= (AVPlayer and AsyncImage can't set headers); nowhere else.
MEDIA_ROUTES = {"/api/tracks/{track_id}/stream", "/api/tracks/{track_id}/download",
                "/api/performances/{pid}/artwork", "/api/composers/{cid}/image", "/api/people/{person_id}/image"}
# The route matrix: everything else under /api needs `content` (approved_user in `on`) or admin_user.
PUBLIC_ROUTES = {"/api/health", "/api/auth/signup", "/api/auth/login"}
SESSION_ROUTES = {"/api/auth/me", "/api/auth/logout", "/api/auth/password", "/api/auth/push-token"}
BODY_LIMIT = 4096
BODY_LIMIT_PREFIXES = ("/api/auth/", "/api/admin/")


# ---- client IP ------------------------------------------------------------------------------------------

def _default_gateway() -> str | None:
    """The container's default gateway (the Docker bridge), which DSM nginx and Tailscale Serve both come
    through. Linux only; None elsewhere."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return str(ipaddress.IPv4Address(int(parts[2], 16).to_bytes(4, "little")))
    except (OSError, ValueError):
        pass
    return None


def trusted_proxies() -> list:
    """SETHSTER_TRUSTED_PROXIES (comma-separated addresses/CIDRs), else 127.0.0.1, ::1 and the default gateway."""
    raw = os.environ.get("SETHSTER_TRUSTED_PROXIES")
    items = [x.strip() for x in raw.split(",") if x.strip()] if raw is not None else \
        ["127.0.0.1", "::1"] + [g for g in [_default_gateway()] if g]
    out = []
    for x in items:
        try:
            out.append(ipaddress.ip_network(x, strict=False))
        except ValueError:
            log.warning("SETHSTER_TRUSTED_PROXIES: ignoring %r", x)
    return out


def _ip(s: str | None):
    try:
        ip = ipaddress.ip_address((s or "").strip())
    except ValueError:
        return None
    return (ip.ipv4_mapped or ip) if ip.version == 6 else ip


def client_ip(scope, trusted: list) -> str:
    """The one place the client address is decided (Uvicorn's proxy_headers is off). From a trusted peer it is
    the rightmost X-Forwarded-For entry, the one that proxy appended; entries further left came from the client
    and are never used, nor is X-Real-IP. Any other peer: the socket address."""
    peer = (scope.get("client") or ("", 0))[0]
    pip = _ip(peer)
    if pip is not None and any(pip.version == n.version and pip in n for n in trusted):
        xff = [v.decode("latin-1") for k, v in scope.get("headers") or [] if k.lower() == b"x-forwarded-for"]
        if xff:
            last = _ip(",".join(xff).split(",")[-1])
            if last is not None:
                return str(last)
    return str(pip) if pip is not None else peer


# ---- throttles ------------------------------------------------------------------------------------------

class Throttle:
    """At most `limit` events per key within `window` seconds. In memory: single worker, reset on restart."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        """Seconds until another event is allowed (0 = now)."""
        t = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return 0
            while q and q[0] <= t - self.window:
                q.popleft()
            if not q:
                del self._hits[key]
                return 0
            return int(q[0] + self.window - t) + 1 if len(q) >= self.limit else 0

    def hit(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, collections.deque()).append(time.monotonic())

    def take(self, key: str) -> tuple[int, float | None]:
        """Checks and counts an attempt in one step, so a burst can't all pass the check before any is counted:
        (0, stamp) when allowed, (wait, None) when not. give_back(key, stamp) un-counts an attempt that
        turned out not to count (a right password)."""
        t = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, collections.deque())
            while q and q[0] <= t - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return int(q[0] + self.window - t) + 1, None
            q.append(t)
            return 0, t

    def give_back(self, key: str, stamp: float | None) -> None:
        with self._lock:
            q = self._hits.get(key)
            if q and stamp in q:
                q.remove(stamp)


def _limited(*waits: int) -> None:
    wait = max(waits)
    if wait:
        raise AccountError(429, "rate_limited", "too many attempts; try again later",
                           headers={"Retry-After": str(wait)})


def _take(*pairs: tuple[Throttle, str]) -> list[tuple[Throttle, str, float | None]]:
    """Counts one attempt on every (throttle, key), or none of them and 429 if any is full."""
    taken, waits = [], []
    for th, key in pairs:
        wait, stamp = th.take(key)
        waits.append(wait)
        taken.append((th, key, stamp))
    if max(waits):
        _give_back(taken)
        _limited(*waits)
    return taken


def _give_back(taken) -> None:
    for th, key, stamp in taken:
        th.give_back(key, stamp)


# ---- request body cap -----------------------------------------------------------------------------------

class BodyLimit:
    """Pure ASGI: account and admin request bodies are at most 4 KB (413 {"code": "too_large"})."""

    def __init__(self, app, limit: int = BODY_LIMIT, prefixes: tuple = BODY_LIMIT_PREFIXES):
        self.app, self.limit, self.prefixes = app, limit, prefixes

    async def _too_large(self, scope, receive, send):
        await JSONResponse({"code": "too_large", "detail": "request body too large"}, status_code=413)(
            scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.prefixes):
            await self.app(scope, receive, send)
            return
        cl = dict(scope.get("headers") or []).get(b"content-length")
        if cl is not None and (not cl.isdigit() or int(cl) > self.limit):
            await self._too_large(scope, receive, send)
            return
        chunks, total = [], 0
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                return  # client went away
            chunks.append(msg.get("body", b""))
            total += len(chunks[-1])
            if total > self.limit:
                await self._too_large(scope, receive, send)
                return
            if not msg.get("more_body"):
                break
        pending = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

        async def replay():
            return pending.pop() if pending else await receive()

        await self.app(scope, replay, send)


# ---- bodies ---------------------------------------------------------------------------------------------

class SignupIn(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    device: str | None = None


class LoginIn(BaseModel):
    username: str
    password: str
    device: str | None = None


class PasswordIn(BaseModel):
    old: str
    new: str


class DeleteMeIn(BaseModel):
    password: str


class PushTokenIn(BaseModel):
    apns_token: str
    env: str


@dataclass
class Session:
    hash: str
    user: object  # sqlite3.Row


@dataclass
class Accounts:
    mode: str
    store: AccountsStore
    rotation: RotationStore
    apns: ApnsSender
    router: APIRouter
    current_session: Callable
    approved_user: Callable
    admin_user: Callable
    content: Callable
    rotation_listener: Callable
    check_owner: Callable
    require_admin: Callable
    warn_if_no_admin: Callable

    async def watch_admins(self) -> None:
        """`on` with no approved admin: warn at startup and every hour (sign-ups still queue; create-admin
        recovers)."""
        while True:
            self.warn_if_no_admin()
            await asyncio.sleep(3600)


def session_token(request: Request) -> str | None:
    """Bearer header; ?token= only for GET/HEAD on MEDIA_ROUTES."""
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip() or None
    route = request.scope.get("route")
    if request.method in ("GET", "HEAD") and getattr(route, "path", None) in MEDIA_ROUTES:
        return request.query_params.get("token") or None
    return None


def accounts_db_path(settings) -> Path:
    v = os.environ.get("SETHSTER_ACCOUNTS_DB")
    return Path(v).expanduser() if v else rotation_db_path(settings).parent / "accounts.db"


def build(settings, *, apns: ApnsSender | None = None) -> Accounts:
    mode = settings.accounts_mode
    store = AccountsStore(accounts_db_path(settings))
    rotation = RotationStore(rotation_db_path(settings))
    apns = apns or ApnsSender.from_env(store.drop_device)
    # Rotations of accounts that no longer exist (a deletion interrupted between accounts.db and rotation.db)
    ids = store.user_ids()
    for lst in rotation.listeners():
        if lst.startswith("u:") and lst[2:].isdigit() and int(lst[2:]) not in ids:
            log.info("clearing the Rotation of deleted account %s (%d item(s))", lst, rotation.clear(lst))
    trusted = trusted_proxies()
    signups_ip = Throttle(5, 3600)
    fails_ip = Throttle(20, 15 * 60)
    fails_user = Throttle(10, 15 * 60)

    # ---- dependencies -------------------------------------------------------------------------------------
    def current_session(request: Request) -> Session:
        s = store.session(session_token(request))
        if s is None:
            raise AccountError(401, "signed_out", "sign in required", headers={"WWW-Authenticate": "Bearer"})
        request.state.session = Session(*s)
        return request.state.session

    def approved_user(request: Request, s: Session = Depends(current_session)):
        if s.user["status"] != "approved":
            raise AccountError(403, s.user["status"], f"account {s.user['status']}")
        request.state.account = s.user
        return s.user

    def admin_user(user=Depends(approved_user)):
        if not user["is_admin"]:
            raise AccountError(403, "not_admin", "admins only")
        return user

    def open_content(request: Request) -> None:
        s = store.session(session_token(request))
        request.state.account = s[1] if s and s[1]["status"] == "approved" else None

    content = approved_user if mode == "on" else open_content

    def rotation_listener(request: Request, supplied: str | None) -> str:
        """[R2-2] Ownership comes only from the session; anonymous (dormant) requests are pinned to default."""
        user = getattr(request.state, "account", None)
        if user is not None:
            return acc.listener(user["id"])
        if supplied and supplied.startswith("u:"):
            raise AccountError(400, "bad_listener", "listener u:… is only reachable with that account's session")
        return "default"

    def check_owner(listener: str) -> None:
        if listener.startswith("u:") and store.user(int(listener[2:])) is None:
            raise AccountError(401, "signed_out", "sign in required", headers={"WWW-Authenticate": "Bearer"})

    def require_admin(request: Request) -> None:
        """For admin-only actions inside content routes (editing or deleting an answer)."""
        user = getattr(request.state, "account", None)
        if user is None:
            raise AccountError(401, "signed_out", "sign in required", headers={"WWW-Authenticate": "Bearer"})
        if not user["is_admin"]:
            raise AccountError(403, "not_admin", "admins only")

    def warn_if_no_admin() -> bool:
        if mode == "on" and store.admin_count() == 0:
            log.warning("SETHSTER_ACCOUNTS=on but there is no approved admin: sign-ups queue with nobody to "
                        "approve them. Run `python -m sethster_server.accounts create-admin`.")
            return True
        return False

    # ---- push (background; never raises) ----------------------------------------------------------------
    async def push_signup(user) -> None:
        try:
            if apns.enabled:
                devices = await run_in_threadpool(store.admin_devices)
                badge = await run_in_threadpool(store.pending_count)
                await apns.send_many(devices, f"New Sethster sign-up: {user['display_name']} ({user['username']})",
                                     badge)
        except Exception:  # noqa: BLE001
            log.exception("sign-up push failed")

    async def push_user(user_id: int, alert: str) -> None:
        try:
            if apns.enabled:
                await apns.send_many(await run_in_threadpool(store.devices, user_id), alert)
        except Exception:  # noqa: BLE001
            log.exception("decision push failed")

    # ---- routes -----------------------------------------------------------------------------------------
    r = APIRouter(prefix="/api", tags=["accounts"])

    def ip_of(request: Request) -> str:
        return client_ip(request.scope, trusted)

    def check_password_or_fail(request: Request, user, password: str) -> None:
        taken = _take((fails_ip, ip_of(request)), (fails_user, user["username"].lower()))
        if not acc.verify_password(password or "", user["pw_hash"]):
            raise AccountError(401, "bad_credentials", "wrong username or password")
        _give_back(taken)

    @r.post("/auth/signup")
    def signup(body: SignupIn, request: Request, background: BackgroundTasks):
        ip = ip_of(request)
        taken = _take((signups_ip, ip))
        try:
            user = store.create_user(body.username, body.password, body.display_name)
        except AccountError:
            _give_back(taken)  # accounts made, not attempts: a taken name or short password doesn't count
            raise
        token = store.create_session(user["id"], body.device)
        log.info("sign-up: %s (#%d) from %s", user["username"], user["id"], ip)
        background.add_task(push_signup, user)
        return {"token": token, "user": acc.me(user)}

    @r.post("/auth/login")
    def login(body: LoginIn, request: Request):
        taken = _take((fails_ip, ip_of(request)), (fails_user, body.username.lower()))
        user = store.authenticate(body.username, body.password)
        if user is None:
            raise AccountError(401, "bad_credentials", "wrong username or password")
        _give_back(taken)
        # a session whatever the status: pending/rejected/disabled users can still see and delete their account;
        # pw_hash: none if the password changed while this one was being checked
        return {"token": store.create_session(user["id"], body.device, pw_hash=user["pw_hash"]), "user": acc.me(user)}

    @r.get("/auth/me")
    def get_me(s: Session = Depends(current_session)):
        return acc.me(s.user)

    @r.delete("/auth/me")
    def delete_me(body: DeleteMeIn, request: Request, s: Session = Depends(current_session)):
        check_password_or_fail(request, s.user, body.password)
        uid = s.user["id"]
        store.delete_user(uid)  # first: a Rotation add racing this then fails check_owner (RotationStore.add)
        rotation.clear(acc.listener(uid))
        log.info("account deleted: %s (#%d)", s.user["username"], uid)
        warn_if_no_admin()
        return {"ok": True}

    @r.post("/auth/logout")
    def logout(s: Session = Depends(current_session)):
        store.revoke(s.hash)
        return {"ok": True}

    @r.post("/auth/password")
    def change_password(body: PasswordIn, request: Request, s: Session = Depends(current_session)):
        check_password_or_fail(request, s.user, body.old)
        store.set_password(s.user["id"], body.new, keep=s.hash, pw_hash=s.user["pw_hash"])
        return {"ok": True}

    @r.post("/auth/push-token")
    def push_token(body: PushTokenIn, s: Session = Depends(current_session)):
        store.register_device(body.apns_token, s.user["id"], body.env, s.hash)
        return {"ok": True}

    @r.delete("/auth/push-token")
    def drop_push_token(s: Session = Depends(current_session)):
        store.unregister_session_device(s.hash)
        return {"ok": True}

    admin = APIRouter(prefix="/admin", dependencies=[Depends(admin_user)], tags=["admin"])

    @admin.get("/users")
    def admin_users(status: str | None = Query(None)):
        if status is not None and status not in acc.STATUSES:
            raise AccountError(422, "invalid", f"status must be one of {list(acc.STATUSES)}")
        return {"items": [acc.admin_shape(u) for u in store.users(status)], "pending_count": store.pending_count()}

    @admin.post("/users/{user_id}/{action}")
    def admin_decide(user_id: int, action: Literal["approve", "reject", "disable", "enable", "make-admin"],
                     background: BackgroundTasks, me=Depends(admin_user)):
        target = store.user(user_id)
        if target is None:
            raise AccountError(404, "not_found", "no such user")
        if action == "make-admin":
            u = store.make_admin(user_id, me["id"])
        else:
            status = {"approve": "approved", "enable": "approved", "reject": "rejected", "disable": "disabled"}[action]
            u = store.set_status(user_id, status, me["id"])
            if status == "approved" and target["status"] != "approved":
                background.add_task(push_user, user_id, "You're in — Sethster is ready")
            elif status == "rejected" and target["status"] != "rejected":
                background.add_task(push_user, user_id, "Your Sethster request wasn't approved")
        log.info("admin %s: %s %s (#%d)", me["username"], action, u["username"], user_id)
        return acc.admin_shape(u)

    @admin.get("/push-status")
    def push_status():
        return apns.status()

    r.include_router(admin)
    return Accounts(mode=mode, store=store, rotation=rotation, apns=apns, router=r, current_session=current_session,
                    approved_user=approved_user, admin_user=admin_user, content=content,
                    rotation_listener=rotation_listener, check_owner=check_owner, require_admin=require_admin,
                    warn_if_no_admin=warn_if_no_admin)
