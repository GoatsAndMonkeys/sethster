"""Accounts (docs/plans/accounts.md, Tests): modes, route matrix, statuses, sessions, throttles, push, CLI."""
import asyncio
import json
import logging
import socket
import sqlite3
import threading
import time

import httpx
import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from sethster_server import accounts as cli
from sethster_server import accounts_api, accounts_store as acc
from sethster_server import apns as apnsmod
from sethster_server import db as dbmod
from sethster_server.app import RedactTokens, create_app
from sethster_server.config import Settings
from sethster_server.rotation_store import RotationStore

PW = "correct horse"
PRODUCTION_SCRYPT_N = acc.SCRYPT_N  # before the fixture below cheapens it


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    # the stored hash carries its own n, so a cheaper n only changes speed
    monkeypatch.setattr(acc, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(acc, "_dummy", [])


@pytest.fixture
def env(tmp_path, monkeypatch):
    media = tmp_path / "media"
    (media / "music").mkdir(parents=True)
    (media / "music" / "one.mp3").write_bytes(b"0123456789")
    database = tmp_path / "sethster.db"
    conn = sqlite3.connect(database)
    dbmod.init(conn)
    conn.executemany(
        "INSERT INTO performances (id, title, date, date_status, kind, collection, duration_s) VALUES (?,?,?,?,?,?,?)",
        [("p1", "First", "2020-01-01", "confirmed", "broadcast", "Radio", 60),
         ("p2", "Second", "2021-01-01", "confirmed", "release", "CD", 120)])
    conn.execute("INSERT INTO tracks (id, performance_id, idx, title, format, path, stream_path, size) "
                 "VALUES ('p1:01','p1',1,'One','mp3','music/one.mp3','music/one.mp3',10)")
    conn.execute("INSERT INTO questions (performance_id, ord, question) VALUES ('p1', 0, 'Who sang?')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("SETHSTER_ROTATION_DB", str(tmp_path / "rotation.db"))
    monkeypatch.delenv("SETHSTER_ACCOUNTS_DB", raising=False)
    monkeypatch.delenv("SETHSTER_TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("SETHSTER_APNS_KEY", str(tmp_path / "no-key.p8"))

    def settings(mode="on", tokens=(), auth_scope="all"):
        return Settings(data_dir=tmp_path / "data", db_path=database, cache_dir=tmp_path / "cache",
                        media_root=media, tokens=list(tokens), ffmpeg=None, aac_encoder="aac", aac_bitrate="256k",
                        auth_scope=auth_scope, accounts_mode=mode)
    return settings


@pytest.fixture
def make(env):
    clients = []

    def make(mode="on", **kw):
        c = TestClient(create_app(env(mode, **kw)))
        c.__enter__()
        clients.append(c)
        return c
    yield make
    for c in clients:
        c.__exit__(None, None, None)


def store_of(c) -> acc.AccountsStore:
    return c.app.state.accounts.store


def H(token):
    return {"Authorization": f"Bearer {token}"}


def signup(c, username, password=PW, **kw):
    r = c.post("/api/auth/signup", json={"username": username, "password": password, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def user(c, username, status="approved", admin=False):
    """A user created straight in the store, signed in; returns (token, id)."""
    u = store_of(c).create_user(username, PW, status=status, is_admin=admin)
    r = c.post("/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"], u["id"]


# ---- modes ----------------------------------------------------------------------------------------------

def test_off_mode_is_unchanged(make):
    c = make("off", tokens=["sekrit"])
    assert c.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code == 404
    assert c.get("/api/admin/users").status_code == 404
    h = c.get("/api/health").json()
    assert h["ok"] and h["performances"] == 2 and h["auth"] is True and h["auth_scope"] == "all"
    r = c.get("/api/performances")
    assert r.status_code == 401 and r.json() == {"detail": "missing or invalid token"}
    assert c.get("/api/performances", headers=H("sekrit")).status_code == 200
    assert c.get("/api/performances", params={"token": "sekrit"}).status_code == 200  # legacy: anywhere
    assert c.get("/docs").status_code == 200
    # rotation listener still comes from the client
    assert c.post("/api/rotation", json={"performance_id": "p1", "listener": "u:9"}, headers=H("sekrit")).json()
    assert c.get("/api/rotation", params={"listener": "u:9"}, headers=H("sekrit")).json()["listener"] == "u:9"


def test_off_mode_keeps_funnel_guard(make):
    c = make("off", tokens=["sekrit"], auth_scope="funnel")
    assert c.get("/api/performances", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 401
    assert c.get("/api/performances").status_code == 200


def test_dormant_content_open_and_accounts_live(make):
    c = make("dormant", tokens=["legacy"])
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/performances").status_code == 200  # open to anyone (temporary window)
    assert c.get("/api/status").json()["performances"] == 2
    assert c.get("/api/admin/users").json()["code"] == "signed_out"
    assert c.get("/docs").status_code == 200
    me = signup(c, "newbie")
    assert me["user"]["status"] == "pending"


def test_on_mode_requires_approved_account(make):
    c = make("on", tokens=["legacy"])
    assert c.get("/api/health").json() == {"ok": True}
    r = c.get("/api/performances", headers=H("legacy"))  # legacy tokens mean nothing in `on`
    assert r.status_code == 401 and r.json()["code"] == "signed_out"
    assert r.headers["www-authenticate"] == "Bearer"
    assert c.get("/api/status").status_code == 401
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(p).status_code == 404
    tok, _ = user(c, "ann")
    assert c.get("/api/performances", headers=H(tok)).status_code == 200
    assert c.get("/api/status", headers=H(tok)).json()["accounts"] == "on"


def test_on_mode_ignores_funnel_and_networks(make):
    c = make("on", auth_scope="funnel")
    for xff in ("100.64.1.1", "10.0.0.3", "8.8.8.8"):
        assert c.get("/api/performances", headers={"X-Forwarded-For": xff}).status_code == 401


def test_dormant_to_on_transition(env):
    with TestClient(create_app(env("dormant"))) as c:
        tok, uid = user(c, "alice")
        pend = signup(c, "cousin")["token"]
        c.post("/api/rotation", json={"performance_id": "p2"}, headers=H(tok))
        c.post("/api/rotation", json={"performance_id": "p1"})  # anonymous -> default
    with TestClient(create_app(env("on"))) as c:
        assert c.get("/api/performances").status_code == 401
        assert c.get("/api/performances", headers=H(pend)).json()["code"] == "pending"
        r = c.get("/api/rotation", headers=H(tok)).json()
        assert r["listener"] == f"u:{uid}" and [i["id"] for i in r["items"]] == ["p2"]
        assert c.get("/api/auth/me", headers=H(tok)).json()["username"] == "alice"


# ---- route matrix ---------------------------------------------------------------------------------------

def _routes(app):
    """Every route as served, with prefixes and router-level dependencies folded in."""
    return list(iter_route_contexts(app.routes))


def _dep_calls(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _dep_calls(d)


def test_route_matrix_walker(make):
    c = make("on")
    a = c.app.state.accounts
    assert a.content is a.approved_user
    seen = set()
    for route in _routes(c.app):
        if not isinstance(route.original_route, APIRoute):
            assert route.path not in ("/docs", "/openapi.json")
            continue
        calls = set(_dep_calls(route.dependant))
        p = route.path
        seen.add(p)
        if p in accounts_api.PUBLIC_ROUTES or p in ("/", "/privacy"):
            assert not calls & {a.approved_user, a.current_session}, p
        elif p in accounts_api.SESSION_ROUTES:
            assert a.current_session in calls and a.approved_user not in calls, p
        elif p.startswith("/api/admin/"):
            assert a.admin_user in calls, p
        else:
            assert p.startswith("/api/") and a.approved_user in calls, f"{p} lacks approved_user"
    assert {"/api/rotation", "/api/tracks/{track_id}/stream", "/api/radio/stations", "/api/calendar",
            "/api/canonical-works", "/api/admin/users"} <= seen
    assert accounts_api.MEDIA_ROUTES <= seen


def test_route_matrix_live(make):
    """Every non-public route answers an anonymous request with 401 signed_out (no content leaks)."""
    c = make("on")
    for route in _routes(c.app):
        if not isinstance(route.original_route, APIRoute) or route.path in accounts_api.PUBLIC_ROUTES | {"/", "/privacy"}:
            continue
        path = route.path.replace("{eid:path}", "x")
        for k in ("track_id", "pid", "cid", "wid", "person_id", "vid", "sid", "n", "user_id", "action",
                  "performance_id", "play_id"):
            path = path.replace("{" + k + "}", "1" if k in ("n", "user_id") else "x")
        for m in route.methods:
            r = c.request(m, path, json={})
            assert r.status_code == 401, (m, path, r.status_code)
            assert m == "HEAD" or r.json()["code"] == "signed_out", (m, path)


# ---- media: HEAD, Range, ?token= ------------------------------------------------------------------------

def test_media_head_range_and_query_token(make):
    c = make("on")
    tok, _ = user(c, "ann")
    pend = signup(c, "pat")["token"]
    url = "/api/tracks/p1:01/stream"
    assert c.head(url, headers=H(tok)).status_code == 200
    r = c.get(url, headers={**H(tok), "Range": "bytes=2-5"})
    assert r.status_code == 206 and r.content == b"2345"
    assert c.get(url, params={"token": tok}, headers={"Range": "bytes=0-0"}).status_code == 206
    assert c.head(url, params={"token": tok}).status_code == 200
    assert c.head("/api/tracks/p1:01/download", params={"token": tok}).status_code == 200
    for kw in ({"headers": H(pend)}, {"params": {"token": pend}}):
        assert c.head(url, **kw).status_code == 403
        r = c.get(url, headers={**kw.get("headers", {}), "Range": "bytes=0-1"}, params=kw.get("params"))
        assert r.status_code == 403 and r.json()["code"] == "pending"
    assert c.head(url).status_code == 401
    # ?token= is only for media
    assert c.get("/api/performances", params={"token": tok}).json()["code"] == "signed_out"
    assert c.get("/api/auth/me", params={"token": tok}).status_code == 401
    assert c.post("/api/rotation", params={"token": tok}, json={"performance_id": "p1"}).status_code == 401


def test_access_log_redacts_tokens():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/tracks/x/stream?a=1&token=SECRET&b=2", "1.1", 200), None)
    RedactTokens().filter(rec)
    assert "SECRET" not in rec.getMessage() and "token=REDACTED&b=2" in rec.getMessage()


# ---- statuses, sessions ---------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["pending", "rejected", "disabled"])
def test_status_vs_content_and_account_routes(make, status):
    c = make("on")
    tok, uid = user(c, "sam", status=status)
    r = c.get("/api/performances", headers=H(tok))
    assert r.status_code == 403 and r.json() == {"code": status, "detail": f"account {status}"}
    assert c.get("/api/rotation", headers=H(tok)).json()["code"] == status
    me = c.get("/api/auth/me", headers=H(tok)).json()
    assert me == {"user_id": uid, "username": "sam", "display_name": "sam", "status": status, "is_admin": False}
    assert c.post("/api/auth/push-token", json={"apns_token": "ab" * 32, "env": "sandbox"},
                  headers=H(tok)).json() == {"ok": True}
    assert c.request("DELETE", "/api/auth/me", json={"password": PW}, headers=H(tok)).json() == {"ok": True}
    assert c.get("/api/auth/me", headers=H(tok)).status_code == 401


def test_login_issues_session_whatever_the_status(make):
    c = make("on")
    store_of(c).create_user("rex", PW, status="rejected")
    r = c.post("/api/auth/login", json={"username": "REX", "password": PW})
    assert r.status_code == 200 and r.json()["user"]["status"] == "rejected" and r.json()["token"]


def test_expired_revoked_and_stale_tokens(make):
    c = make("on")
    tok, uid = user(c, "ann")
    db = store_of(c).path
    assert c.get("/api/performances", headers=H("garbage")).json()["code"] == "signed_out"
    con = sqlite3.connect(db)
    con.execute("UPDATE sessions SET expires_at='2020-01-01T00:00:00Z'")
    con.commit()
    assert c.get("/api/performances", headers=H(tok)).json()["code"] == "signed_out"
    assert con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0  # expired row removed
    # sliding expiry: bumped once a day
    tok2 = c.post("/api/auth/login", json={"username": "ann", "password": PW}).json()["token"]
    con.execute("UPDATE sessions SET last_seen_at='2020-01-01T00:00:00Z', expires_at='2099-01-01T00:00:00Z'")
    con.commit()
    assert c.get("/api/performances", headers=H(tok2)).status_code == 200
    exp = con.execute("SELECT expires_at FROM sessions").fetchone()[0]
    assert exp < "2099" and acc.parse_iso(exp) > acc.now()
    # logout revokes
    assert c.post("/api/auth/logout", headers=H(tok2)).json() == {"ok": True}
    assert c.get("/api/performances", headers=H(tok2)).status_code == 401
    # a deleted user's token is stale
    tok3 = c.post("/api/auth/login", json={"username": "ann", "password": PW}).json()["token"]
    store_of(c).delete_user(uid)
    assert c.get("/api/auth/me", headers=H(tok3)).json()["code"] == "signed_out"
    con.close()


def test_password_change_revokes_other_sessions(make):
    c = make("on")
    a, _ = user(c, "ann")
    b = c.post("/api/auth/login", json={"username": "ann", "password": PW}).json()["token"]
    r = c.post("/api/auth/password", json={"old": "nope nope", "new": "brand new pw"}, headers=H(a))
    assert r.status_code == 401 and r.json()["code"] == "bad_credentials"
    assert c.post("/api/auth/password", json={"old": PW, "new": "short"}, headers=H(a)).json()["code"] == \
        "invalid_password"
    assert c.post("/api/auth/password", json={"old": PW, "new": "brand new pw"}, headers=H(a)).json() == {"ok": True}
    assert c.get("/api/auth/me", headers=H(a)).status_code == 200
    assert c.get("/api/auth/me", headers=H(b)).status_code == 401
    assert c.post("/api/auth/login", json={"username": "ann", "password": "brand new pw"}).status_code == 200


def test_signup_validation_and_conflict(make):
    c = make("on")
    body = signup(c, "Ann.B-1", display_name="Ann B")
    assert set(body) == {"token", "user"}
    assert body["user"] == {"user_id": body["user"]["user_id"], "username": "Ann.B-1", "display_name": "Ann B",
                            "status": "pending", "is_admin": False}
    r = c.post("/api/auth/signup", json={"username": "ann.b-1", "password": PW})
    assert r.status_code == 409 and r.json() == {"code": "username_unavailable", "detail": "username unavailable"}
    for bad, code in (({"username": "a b", "password": PW}, "invalid_username"),
                      ({"username": "ab", "password": PW}, "invalid_username"),
                      ({"username": "abc", "password": "1234567"}, "invalid_password"),
                      ({"username": "abc", "password": "x" * 129}, "invalid_password"),
                      ({"username": "abc", "password": PW, "display_name": "d" * 65}, "invalid_display_name"),
                      ({"username": "abc"}, "invalid")):
        r = c.post("/api/auth/signup", json=bad)
        assert r.status_code == 422 and r.json()["code"] == code, (bad, r.text)
    r = c.post("/api/auth/signup", content=json.dumps({"username": "abc", "password": "x" * 5000}),
               headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "too_large"


def test_password_hash_format():
    h = acc.hash_password("pw123456")
    kind, n, r, p, salt, digest = h.split("$")
    assert kind == "scrypt" and (int(r), int(p)) == (8, 1) and len(acc.base64.b64decode(salt)) == 16
    assert acc.verify_password("pw123456", h) and not acc.verify_password("pw1234567", h)


def test_production_scrypt_cost_is_2_14():
    assert PRODUCTION_SCRYPT_N == 2 ** 14


# ---- login timing parity, throttles -------------------------------------------------------------------

def test_unknown_username_verifies_dummy_hash(make, monkeypatch):
    c = make("on")
    user(c, "ann")
    calls = []
    real = acc.verify_password
    monkeypatch.setattr(acc, "verify_password", lambda pw, h: calls.append(h) or real(pw, h))
    a = c.post("/api/auth/login", json={"username": "nobody", "password": PW})
    b = c.post("/api/auth/login", json={"username": "ann", "password": "wrong pw!"})
    assert calls[0] == acc.dummy_hash() and calls[1] != acc.dummy_hash()
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() == {"code": "bad_credentials", "detail": "wrong username or password"}


def test_status_revealed_only_after_password(make):
    c = make("on")
    store_of(c).create_user("pen", PW)
    assert c.post("/api/auth/login", json={"username": "pen", "password": "wrong pw!"}).json()["code"] == \
        "bad_credentials"


def test_signup_throttle(make):
    c = make("on")
    for i in range(5):
        signup(c, f"user{i}")
    r = c.post("/api/auth/signup", json={"username": "user9", "password": PW})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and 0 < int(r.headers["retry-after"]) <= 3600


def test_failed_login_throttles(make):
    c = make("on")
    store_of(c).create_user("ann", PW, status="approved")
    for _ in range(10):
        assert c.post("/api/auth/login", json={"username": "Ann", "password": "wrong pw!"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "ann", "password": PW})  # even the right password
    assert r.status_code == 429 and int(r.headers["retry-after"]) <= 15 * 60
    for i in range(10):  # per IP: 20 failures in all, across usernames
        assert c.post("/api/auth/login", json={"username": f"x{i}", "password": "wrong pw!"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "fresh", "password": "wrong pw!"}).status_code == 429


def test_throttle_window_expires(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(accounts_api.time, "monotonic", lambda: t[0])
    th = accounts_api.Throttle(2, 60)
    th.hit("k"); th.hit("k")
    assert th.retry_after("k") == 61
    t[0] += 61
    assert th.retry_after("k") == 0


# ---- client IP --------------------------------------------------------------------------------------------

def test_client_ip_rules():
    trusted = [accounts_api.ipaddress.ip_network(n) for n in ("127.0.0.1/32", "172.17.0.1/32")]

    def scope(peer, *xff):
        return {"client": (peer, 1), "headers": [(b"x-forwarded-for", v.encode()) for v in xff]
                + [(b"x-real-ip", b"6.6.6.6")]}
    ip = accounts_api.client_ip
    assert ip(scope("172.17.0.1", "1.1.1.1, 9.9.9.9"), trusted) == "9.9.9.9"  # rightmost: what the proxy added
    assert ip(scope("127.0.0.1", "1.1.1.1", "9.9.9.9"), trusted) == "9.9.9.9"
    assert ip(scope("172.17.0.1"), trusted) == "172.17.0.1"
    assert ip(scope("172.17.0.1", "garbage"), trusted) == "172.17.0.1"
    assert ip(scope("203.0.113.5", "9.9.9.9"), trusted) == "203.0.113.5"  # untrusted peer: XFF ignored
    assert ip(scope("::ffff:127.0.0.1", "9.9.9.9"), trusted) == "9.9.9.9"


def test_trusted_proxies_env(monkeypatch):
    monkeypatch.setenv("SETHSTER_TRUSTED_PROXIES", "10.0.0.1, 172.18.0.0/16, bogus")
    nets = [str(n) for n in accounts_api.trusted_proxies()]
    assert nets == ["10.0.0.1/32", "172.18.0.0/16"]
    monkeypatch.delenv("SETHSTER_TRUSTED_PROXIES")
    assert "127.0.0.1/32" in [str(n) for n in accounts_api.trusted_proxies()]


def test_main_turns_proxy_headers_off():
    import inspect
    from sethster_server import __main__ as m
    src = inspect.getsource(m)
    assert "proxy_headers=False" in src and "forwarded_allow_ips" not in src


def _serve(app):
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, proxy_headers=False, log_level="warning", lifespan="on"))
    th = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    return server, th, f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("trusted", [True, False])
def test_forged_xff_through_real_asgi_stack(env, monkeypatch, trusted):
    """Through uvicorn itself: a client can't dodge the sign-up throttle by forging X-Forwarded-For. From a
    trusted proxy (127.0.0.1 here) only the rightmost entry counts; from anyone else XFF is ignored."""
    monkeypatch.setenv("SETHSTER_TRUSTED_PROXIES", "127.0.0.1" if trusted else "192.0.2.1")
    server, th, base = _serve(create_app(env("on")))
    try:
        with httpx.Client(base_url=base) as h:
            for i in range(5):  # forged left entries vary, the proxy-added one doesn't
                r = h.post("/api/auth/signup", json={"username": f"f{i}x", "password": PW},
                           headers={"X-Forwarded-For": f"10.9.{i}.1, 198.51.100.7", "X-Real-IP": f"10.8.{i}.1"})
                assert r.status_code == 200, r.text
            r = h.post("/api/auth/signup", json={"username": "f9x", "password": PW},
                       headers={"X-Forwarded-For": "10.9.9.9, 198.51.100.7"})
            assert r.status_code == 429
            # a different rightmost entry is a different client only when the peer is a trusted proxy
            r = h.post("/api/auth/signup", json={"username": "g1x", "password": PW},
                       headers={"X-Forwarded-For": "198.51.100.8"})
            assert r.status_code == (200 if trusted else 429)
    finally:
        server.should_exit = True
        th.join(5)


# ---- rotation ownership ---------------------------------------------------------------------------------

def test_cross_account_rotation(make):
    c = make("on")
    a, aid = user(c, "ann")
    b, bid = user(c, "bob")
    assert c.post("/api/rotation", json={"performance_id": "p1", "listener": f"u:{bid}"}, headers=H(a)).status_code == 200
    assert c.post("/api/rotation", json={"performance_id": "p2", "listener": "default"}, headers=H(a)).status_code == 200
    ra = c.get("/api/rotation", params={"listener": f"u:{bid}"}, headers=H(a)).json()
    assert ra["listener"] == f"u:{aid}" and [i["id"] for i in ra["items"]] == ["p1", "p2"]
    assert c.get("/api/rotation", headers=H(b)).json() == {"listener": f"u:{bid}", "items": []}
    c.post("/api/rotation", json={"performance_id": "p2"}, headers=H(b))
    assert c.put("/api/rotation", json={"performance_ids": ["p2", "p1"], "listener": f"u:{bid}"},
                 headers=H(a)).status_code == 200
    assert [i["id"] for i in c.get("/api/rotation", headers=H(a)).json()["items"]] == ["p2", "p1"]
    assert c.delete("/api/rotation/p2", params={"listener": f"u:{bid}"}, headers=H(a)).json() == {"ok": True}
    assert [i["id"] for i in c.get("/api/rotation", headers=H(b)).json()["items"]] == ["p2"]
    assert [i["id"] for i in c.get("/api/rotation", headers=H(a)).json()["items"]] == ["p1"]
    assert RotationStore(c.app.state.accounts.rotation.path).items("default") == []


def test_dormant_anonymous_rotation_pinned_to_default(make):
    c = make("dormant")
    tok, uid = user(c, "ann")
    c.post("/api/rotation", json={"performance_id": "p1"}, headers=H(tok))
    for call in (lambda: c.get("/api/rotation", params={"listener": f"u:{uid}"}),
                 lambda: c.post("/api/rotation", json={"performance_id": "p2", "listener": f"u:{uid}"}),
                 lambda: c.put("/api/rotation", json={"performance_ids": [], "listener": f"u:{uid}"}),
                 lambda: c.delete("/api/rotation/p1", params={"listener": f"u:{uid}"})):
        r = call()
        assert r.status_code == 400 and r.json()["code"] == "bad_listener"
    assert c.post("/api/rotation", json={"performance_id": "p2", "listener": "alice"}).status_code == 200
    assert c.get("/api/rotation", params={"listener": "alice"}).json()["listener"] == "default"
    assert [i["id"] for i in c.get("/api/rotation").json()["items"]] == ["p2"]
    assert [i["id"] for i in c.get("/api/rotation", headers=H(tok)).json()["items"]] == ["p1"]
    pend = signup(c, "pat")["token"]  # not approved: anonymous for content in dormant
    assert c.get("/api/rotation", headers=H(pend)).json()["listener"] == "default"


# ---- admin ----------------------------------------------------------------------------------------------

def test_admin_routes(make):
    c = make("on")
    adm, admin_id = user(c, "owner", admin=True)
    reg, _ = user(c, "ann")
    new = signup(c, "newbie")
    nid, ntok = new["user"]["user_id"], new["token"]
    r = c.get("/api/admin/users", headers=H(reg))
    assert r.status_code == 403 and r.json() == {"code": "not_admin", "detail": "admins only"}
    assert c.post(f"/api/admin/users/{nid}/approve", headers=H(reg)).json()["code"] == "not_admin"
    lst = c.get("/api/admin/users", params={"status": "pending"}, headers=H(adm)).json()
    assert lst["pending_count"] == 1 and [u["username"] for u in lst["items"]] == ["newbie"]
    assert set(lst["items"][0]) == {"user_id", "username", "display_name", "status", "is_admin", "created_at",
                                    "decided_at", "decided_by"}
    assert c.get("/api/admin/users", params={"status": "bogus"}, headers=H(adm)).json()["code"] == "invalid"
    assert c.post("/api/admin/users/999/approve", headers=H(adm)).json()["code"] == "not_found"
    assert c.post(f"/api/admin/users/{nid}/frobnicate", headers=H(adm)).status_code == 422

    assert c.get("/api/performances", headers=H(ntok)).json()["code"] == "pending"
    u = c.post(f"/api/admin/users/{nid}/approve", headers=H(adm)).json()
    assert u["status"] == "approved" and u["decided_by"] == admin_id
    assert c.get("/api/performances", headers=H(ntok)).status_code == 200  # approve doesn't revoke
    assert c.post(f"/api/admin/users/{nid}/disable", headers=H(adm)).json()["status"] == "disabled"
    assert c.get("/api/performances", headers=H(ntok)).json()["code"] == "signed_out"  # disable revokes
    ntok = c.post("/api/auth/login", json={"username": "newbie", "password": PW}).json()["token"]
    assert c.get("/api/performances", headers=H(ntok)).json()["code"] == "disabled"
    assert c.post(f"/api/admin/users/{nid}/enable", headers=H(adm)).json()["status"] == "approved"
    assert c.get("/api/performances", headers=H(ntok)).status_code == 200
    assert c.post(f"/api/admin/users/{nid}/make-admin", headers=H(adm)).json()["is_admin"] is True
    assert c.get("/api/auth/me", headers=H(ntok)).status_code == 401  # make-admin revokes
    ntok = c.post("/api/auth/login", json={"username": "newbie", "password": PW}).json()["token"]
    assert c.get("/api/admin/users", headers=H(ntok)).status_code == 200
    assert c.post(f"/api/admin/users/{nid}/reject", headers=H(adm)).json()["status"] == "rejected"
    assert c.get("/api/admin/push-status", headers=H(adm)).json()["enabled"] is False


def test_question_answers_admin_edit(make):
    c = make("on")
    adm, _ = user(c, "owner", admin=True)
    reg, _ = user(c, "ann")
    q = {"performance_id": "p1", "question": "Who sang?"}
    assert c.post("/api/questions/answer", json={**q, "answer": "Callas"}, headers=H(reg)).json() == {"ok": True}
    r = c.post("/api/questions/answer", json={**q, "answer": "Tebaldi"}, headers=H(reg))
    assert r.status_code == 403 and r.json()["code"] == "not_admin"
    assert c.post("/api/questions/answer", json={**q, "answer": None}, headers=H(reg)).json()["code"] == "not_admin"
    assert c.post("/api/questions/answer", json={**q, "answer": "Tebaldi"}, headers=H(adm)).json() == {"ok": True}
    assert c.post("/api/questions/answer", json={**q, "answer": ""}, headers=H(adm)).json() == {"ok": True}


def test_question_edit_needs_admin_in_dormant(make):
    c = make("dormant")
    q = {"performance_id": "p1", "question": "Who sang?"}
    assert c.post("/api/questions/answer", json={**q, "answer": "Callas"}).json() == {"ok": True}
    assert c.post("/api/questions/answer", json={**q, "answer": "X"}).json()["code"] == "signed_out"


# ---- deletion, last admin -------------------------------------------------------------------------------

def test_deletion_cascade(make):
    c = make("on")
    adm, admin_id = user(c, "owner", admin=True)
    tok, uid = user(c, "ann", status="pending")
    c.post(f"/api/admin/users/{uid}/approve", headers=H(adm))
    other, oid = user(c, "bob")
    c.post("/api/rotation", json={"performance_id": "p1"}, headers=H(tok))
    c.post("/api/rotation", json={"performance_id": "p1"}, headers=H(other))
    c.post("/api/auth/push-token", json={"apns_token": "aa" * 32, "env": "production"}, headers=H(tok))
    c.post("/api/auth/login", json={"username": "ann", "password": PW})  # a second session
    r = c.request("DELETE", "/api/auth/me", json={"password": "wrong pw!"}, headers=H(tok))
    assert r.status_code == 401 and r.json()["code"] == "bad_credentials"
    assert c.request("DELETE", "/api/auth/me", json={"password": PW}, headers=H(tok)).json() == {"ok": True}
    con = sqlite3.connect(store_of(c).path)
    assert con.execute("SELECT COUNT(*) FROM users WHERE id=?", (uid,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (uid,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0
    con.close()
    rot = c.app.state.accounts.rotation
    assert rot.items(f"u:{uid}") == [] and len(rot.items(f"u:{oid}")) == 1
    # the deleting admin's decisions lose their decided_by; ids are never reused
    c.request("DELETE", "/api/auth/me", json={"password": PW}, headers=H(adm))
    assert store_of(c).user(uid) is None
    assert store_of(c).create_user("zed", PW)["id"] > oid


def test_last_admin_deletion_allowed_and_warned(make, caplog):
    c = make("on")
    adm, aid = user(c, "owner", admin=True)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sethster.accounts"):
        r = c.request("DELETE", "/api/auth/me", json={"password": PW}, headers=H(adm))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert any("no approved admin" in m for m in caplog.messages)
    assert store_of(c).admin_count() == 0
    assert signup(c, "after")["user"]["status"] == "pending"  # sign-ups still queue


def test_no_admin_warning_at_startup_and_hourly(env, caplog, monkeypatch):
    with caplog.at_level(logging.WARNING, logger="sethster.accounts"):
        with TestClient(create_app(env("on"))) as c:
            pass
    assert sum("no approved admin" in m for m in caplog.messages) == 1
    accounts = c.app.state.accounts
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(accounts_api.asyncio, "sleep", fake_sleep)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sethster.accounts"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(accounts.watch_admins())
    assert sleeps == [3600, 3600] and sum("no approved admin" in m for m in caplog.messages) == 2
    with TestClient(create_app(env("dormant"))) as c:  # dormant: no warning
        assert c.app.state.accounts.warn_if_no_admin() is False


# ---- push devices, APNs ---------------------------------------------------------------------------------

def test_push_token_moves_between_accounts(make):
    c = make("on")
    a, aid = user(c, "ann")
    b, bid = user(c, "bob")
    dev = {"apns_token": "AB" * 32, "env": "sandbox"}
    assert c.post("/api/auth/push-token", json=dev, headers=H(a)).json() == {"ok": True}
    st = store_of(c)
    assert [d["user_id"] for d in st.devices(aid)] == [aid]
    c.post("/api/auth/push-token", json=dev, headers=H(b))
    assert st.devices(aid) == [] and [d["apns_token"] for d in st.devices(bid)] == ["ab" * 32]
    assert c.post("/api/auth/push-token", json={**dev, "env": "dev"}, headers=H(a)).json()["code"] == "invalid_push_env"
    assert c.post("/api/auth/push-token", json={**dev, "apns_token": "zz"}, headers=H(a)).json()["code"] == \
        "invalid_push_token"
    assert c.request("DELETE", "/api/auth/push-token", headers=H(b)).json() == {"ok": True}
    assert st.devices(bid) == []
    c.post("/api/auth/push-token", json=dev, headers=H(b))
    c.post("/api/auth/logout", headers=H(b))  # logout drops the device too
    assert st.devices(bid) == []


@pytest.fixture
def p8(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "apns.p8"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return path, key.public_key()


def _sender(p8, handler, dropped):
    return apnsmod.ApnsSender(p8[0], "KEY123", "MMZ8VT9YVZ", "com.goatsandmonkeys.sethster", dropped.append,
                              client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_apns_sender(p8, monkeypatch):
    import jwt
    monkeypatch.setattr(apnsmod, "RETRY_DELAYS", (0, 0))
    seen, dropped = [], []
    replies = {"ok": [httpx.Response(200)], "gone": [httpx.Response(410, json={"reason": "Unregistered"})],
               "bad": [httpx.Response(400, json={"reason": "BadDeviceToken"})],
               "flaky": [httpx.Response(503), httpx.Response(500), httpx.Response(200)],
               "down": [httpx.Response(503)] * 3, "creds": [httpx.Response(403, json={"reason": "InvalidProviderToken"})]}

    def handler(req):
        seen.append(req)
        return replies[req.url.path.rsplit("/", 1)[-1]].pop(0)
    s = _sender(p8, handler, dropped)
    assert s.enabled

    async def run():
        out = [await s.send(t, "sandbox", "hi", badge=3) for t in ("ok", "gone", "bad", "flaky", "down")]
        out.append(await s.send("creds", "production", "hi"))
        await s.aclose()
        return out
    assert asyncio.run(run()) == [True, False, False, True, False, False]
    assert dropped == ["gone", "bad"]
    first = seen[0]
    assert str(first.url) == "https://api.sandbox.push.apple.com/3/device/ok"
    assert first.headers["apns-topic"] == "com.goatsandmonkeys.sethster"
    assert first.headers["apns-push-type"] == "alert"
    assert json.loads(first.content) == {"aps": {"alert": "hi", "sound": "default", "badge": 3}}
    tok = first.headers["authorization"].split(" ", 1)[1]
    hdr = jwt.get_unverified_header(tok)
    assert hdr["kid"] == "KEY123" and hdr["alg"] == "ES256"
    assert jwt.decode(tok, p8[1], algorithms=["ES256"])["iss"] == "MMZ8VT9YVZ"
    assert len({r.headers["authorization"] for r in seen[:5]}) == 1  # provider token cached
    assert str(seen[-1].url).startswith("https://api.push.apple.com/")
    assert sum(1 for r in seen if r.url.path.endswith("/flaky")) == 3 and \
        sum(1 for r in seen if r.url.path.endswith("/down")) == 3  # 2 retries
    st = s.status()
    assert st["enabled"] and "InvalidProviderToken" in st["last_error"] and st["sent"] == 2


def test_apns_off_without_key(tmp_path):
    s = apnsmod.ApnsSender(tmp_path / "missing.p8", "K", "T", "topic", lambda t: None)
    assert not s.enabled and asyncio.run(s.send("ab", "sandbox", "x")) is False


def test_signup_and_decisions_push(make, p8):
    c = make("on")
    sent, dropped = [], []

    def handler(req):
        sent.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content)["aps"]))
        return httpx.Response(410) if "dead" in req.url.path else httpx.Response(200)
    real = c.app.state.accounts.apns
    fake = _sender(p8, handler, dropped)
    real._key, real.key_id, real.team_id, real._client_factory = fake._key, "KEY123", "T", fake._client_factory
    adm, aid = user(c, "owner", admin=True)
    for t in ("aa" * 32, "dead" + "d" * 60):
        c.post("/api/auth/push-token", json={"apns_token": t, "env": "sandbox"}, headers=H(adm))
    new = signup(c, "newbie", display_name="New B")
    assert sent[0] == ("aa" * 32, {"alert": "New Sethster sign-up: New B (newbie)", "sound": "default", "badge": 1})
    assert [d["apns_token"] for d in store_of(c).devices(aid)] == ["aa" * 32]  # 410 removed the dead one
    sent.clear()
    c.post("/api/auth/push-token", json={"apns_token": "bb" * 32, "env": "production"}, headers=H(new["token"]))
    c.post(f"/api/admin/users/{new['user']['user_id']}/approve", headers=H(adm))
    assert sent == [("bb" * 32, {"alert": "You're in — Sethster is ready", "sound": "default"})]
    sent.clear()
    c.post(f"/api/admin/users/{new['user']['user_id']}/reject", headers=H(adm))
    assert sent == [("bb" * 32, {"alert": "Your Sethster request wasn't approved", "sound": "default"})]


def test_push_failure_never_blocks(make, p8):
    c = make("on")
    real = c.app.state.accounts.apns
    real._key, real.key_id, real.team_id = p8[0].read_text(), "K", "T"

    def boom():
        raise RuntimeError("no network")
    real._client_factory = boom
    adm, _ = user(c, "owner", admin=True)
    c.post("/api/auth/push-token", json={"apns_token": "aa" * 32, "env": "sandbox"}, headers=H(adm))
    apnsmod.RETRY_DELAYS, saved = (0, 0), apnsmod.RETRY_DELAYS
    try:
        assert c.post("/api/auth/signup", json={"username": "newbie", "password": PW}).status_code == 200
    finally:
        apnsmod.RETRY_DELAYS = saved
    assert "no network" in real.status()["last_error"]


# ---- CLI ------------------------------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SETHSTER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("SETHSTER_ROTATION_DB", str(tmp_path / "data" / "rotation.db"))
    monkeypatch.delenv("SETHSTER_ACCOUNTS_DB", raising=False)
    return tmp_path / "data"


def run_cli(monkeypatch, capsys, *argv, stdin=None):
    if stdin is not None:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert cli.main(list(argv)) == 0
    return capsys.readouterr().out


def test_cli_create_reset_list_delete(cli_env, monkeypatch, capsys):
    out = run_cli(monkeypatch, capsys, "create-admin", "owner", "--password-stdin", stdin=PW + "\n")
    assert "created owner (#1) approved admin" in out
    st = acc.AccountsStore(cli_env / "accounts.db")
    assert st.authenticate("owner", PW)["is_admin"] == 1
    run_cli(monkeypatch, capsys, "create-user", "appreview", "--approved", "--password-stdin", stdin=PW + "\n")
    assert st.user_by_name("appreview")["status"] == "approved"
    tok = st.create_session(1)
    out = run_cli(monkeypatch, capsys, "reset-password", "owner")
    pw = out.split("owner: ")[1].split()[0]
    assert st.authenticate("owner", pw) and st.session(tok) is None
    out = run_cli(monkeypatch, capsys, "list")
    assert "owner" in out and "appreview" in out
    RotationStore(cli_env / "rotation.db").add("u:2", "p1")
    out = run_cli(monkeypatch, capsys, "delete", "appreview", "--yes")
    assert "1 Rotation item" in out and st.user_by_name("appreview") is None
    run_cli(monkeypatch, capsys, "create-user", "pat", "--password-stdin", stdin=PW + "\n")
    out = run_cli(monkeypatch, capsys, "create-admin", "pat")  # recovery: promote an existing user
    assert "promoted" in out and st.user_by_name("pat")["status"] == "approved"


def test_cli_move_rotation_merge_and_idempotent(cli_env, monkeypatch, capsys):
    st = acc.AccountsStore(cli_env / "accounts.db")
    alice = st.create_user("alice", PW, status="approved")["id"]
    rot = RotationStore(cli_env / "rotation.db")
    for pid in ("p1", "p2", "p3"):
        rot.add("default", pid)
    src_added = {i["performance_id"]: i["added_at"] for i in rot.items("default")}
    rot.add(f"u:{alice}", "p9")
    rot.add(f"u:{alice}", "p2")  # already there: skipped
    out = run_cli(monkeypatch, capsys, "move-rotation", "default", "alice")
    assert "moved 2, skipped 1" in out and "before: default=3 u:1=2" in out and "after:  default=0 u:1=4" in out
    items = rot.items(f"u:{alice}")
    assert [i["performance_id"] for i in items] == ["p9", "p2", "p1", "p3"]
    assert {i["performance_id"]: i["added_at"] for i in items}["p3"] == src_added["p3"]
    backups = list(cli_env.glob("rotation.db.bak-*"))
    assert len(backups) == 1
    bak = RotationStore(backups[0])
    assert len(bak.items("default")) == 3
    out = run_cli(monkeypatch, capsys, "move-rotation", "default", "alice")
    assert "moved 0, skipped 0" in out
    assert [i["performance_id"] for i in rot.items(f"u:{alice}")] == ["p9", "p2", "p1", "p3"]


# ---- misc -----------------------------------------------------------------------------------------------

def test_privacy_page(make):
    c = make("on")
    r = c.get("/privacy")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<html" in r.text.lower()


def test_importer_leaves_accounts_db(env, tmp_path):
    from sethster_server.importer import build
    s = env("on")
    s.data_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(s)) as c:
        user(c, "alice")
        path = store_of(c).path
    before = path.read_bytes()
    s.db_path = tmp_path / "rebuilt.db"
    s.result_dirs = [tmp_path / "none"]
    s.wiki_dir = s.transcripts_dir = s.programs_dir = tmp_path / "none"
    build(s, use_media=False)
    assert s.db_path.exists() and path.read_bytes() == before
    assert path.parent == c.app.state.accounts.rotation.path.parent  # next to rotation.db


def test_accounts_db_env(env, tmp_path, monkeypatch):
    monkeypatch.setenv("SETHSTER_ACCOUNTS_DB", str(tmp_path / "elsewhere" / "acc.db"))
    with TestClient(create_app(env("on"))) as c:
        assert store_of(c).path == tmp_path / "elsewhere" / "acc.db"


def test_bad_mode_rejected(monkeypatch):
    from sethster_server.config import load_settings
    monkeypatch.setenv("SETHSTER_ACCOUNTS", "maybe")
    with pytest.raises(ValueError):
        load_settings()


# ---- races (Codex review 1) -----------------------------------------------------------------------------

def test_throttle_take_is_atomic_under_a_burst():
    th = accounts_api.Throttle(10, 60)
    allowed = []
    barrier = threading.Barrier(32)

    def go():
        barrier.wait()
        allowed.append(th.take("ann")[1] is not None)
    ts = [threading.Thread(target=go) for _ in range(32)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(allowed) == 10
    wait, stamp = th.take("bob")
    th.give_back("bob", stamp)
    assert th.retry_after("bob") == 0 and wait == 0


def test_session_refused_if_password_changed_meanwhile(make):
    c = make("on")
    st = store_of(c)
    u = st.create_user("ann", PW, status="approved")
    seen = st.authenticate("ann", PW)
    st.set_password(u["id"], "another password")
    with pytest.raises(acc.AccountError) as e:
        st.create_session(u["id"], pw_hash=seen["pw_hash"])
    assert e.value.code == "bad_credentials"


def test_first_answer_is_insert_only(make):
    c = make("on")
    reg, _ = user(c, "ann")
    q = {"performance_id": "p1", "question": "Who sang?"}
    con = sqlite3.connect(c.app.state.settings.db_path)  # someone answered first, between check and write
    con.execute("INSERT INTO answers VALUES ('p1','Who sang?','Callas','2026-01-01T00:00:00Z')")
    con.commit()
    con.close()
    r = c.post("/api/questions/answer", json={**q, "answer": "Tebaldi"}, headers=H(reg))
    assert r.json()["code"] == "not_admin"


def test_rotation_add_after_deletion_refused(make):
    c = make("on")
    tok, uid = user(c, "ann")
    accounts = c.app.state.accounts
    accounts.store.delete_user(uid)
    with pytest.raises(acc.AccountError):
        accounts.rotation.add(acc.listener(uid), "p1", accounts.check_owner)
    assert accounts.rotation.items(acc.listener(uid)) == []


def test_push_registration_needs_live_session(make):
    c = make("on")
    st = store_of(c)
    u = st.create_user("ann", PW, status="approved")
    with pytest.raises(acc.AccountError):
        st.register_device("aa" * 32, u["id"], "production", "no-such-session")


def test_orphan_rotations_cleared_at_startup(env):
    s = env("on")
    rot = RotationStore(accounts_api.rotation_db_path(s))
    rot.add("u:99", "p1")
    rot.add("default", "p1")
    accounts_api.build(s)
    assert rot.items("u:99") == [] and len(rot.items("default")) == 1


def test_access_log_redacts_encoded_token_name():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/tracks/x/stream?%74oken=SECRET", "1.1", 200), None)
    RedactTokens().filter(rec)
    assert "SECRET" not in rec.getMessage()
