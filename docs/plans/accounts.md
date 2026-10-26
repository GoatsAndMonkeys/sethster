# Sethster accounts with owner approval — plan rev 3 (2026-10-23)

Rev 2 answered Astra round 1 ([R1-n]); rev 3 answers round 2 ([R2-n]).

## Goal
Friends and family sign up in the app with a username and password. Until the owner approves them they get
nothing from the server: no metadata, no audio. The owner approves or rejects from their own copy of Sethster and
hears about sign-ups by push and by an in-app badge. Each approved person has their own Rotation. Replaces the
shared tokens (already blank in the NAS `.env`).

Server: FastAPI + SQLite in Docker on the NAS. Reached publicly at `https://<public-host>:<port>` (DSM
nginx → host :8642 → container), on the tailnet via Tailscale Serve, and on the LAN. App: SwiftUI + CarPlay via
TestFlight.

## Owner decisions
- Username + password, no email. Pending users see only a waiting screen (bundled demo data still reachable).
- Approve in the owner's app; push **and** badge. Rotation per person; the current `default` list goes to the main listener.
- [R1-10] Approved users may keep playing **downloaded** Rotation items offline; revocation takes effect the next
  time the app reaches the server. (Owner confirmed 2026-10-23.)
- [R1-3] Questions queue ("Ask Dad"): any approved user may answer; only admins may edit or delete answers.
- [R1-11] Distribution: TestFlight external testing, friends and family only. Known risk: TestFlight is meant for
  pre-release testing (guideline 2.2) and builds expire after 90 days, so the owner uploads a fresh build at least
  every ~80 days and describes it to review as a beta of a private family library app.

## Server
### Storage [R1-1]
- New `accounts.db` next to `rotation.db` under `SETHSTER_DATA` (env `SETHSTER_ACCOUNTS_DB`), never touched by
  the importer. Included in the existing data-dir backup. `accounts_store.py` follows `rotation_store.py`.
- `users(id INTEGER PRIMARY KEY AUTOINCREMENT` (AUTOINCREMENT so ids are never reused), `username TEXT UNIQUE
  COLLATE NOCASE, pw_hash TEXT, display_name TEXT, status TEXT CHECK(status IN
  ('pending','approved','rejected','disabled')), is_admin INT, created_at, decided_at, decided_by INT NULL)`.
  `decided_by` is a plain id (no FK), set NULL when that admin is deleted.
- `sessions(token_hash TEXT PRIMARY KEY, user_id, created_at, last_seen_at, expires_at, device TEXT)`. 32-byte
  random tokens, SHA-256 stored. Lifetime 180 days sliding (bumped at most once a day). Revoked on logout,
  password change/reset, status change to rejected/disabled, admin grant, and deletion.
- `push_devices(apns_token TEXT PRIMARY KEY, user_id, env, updated_at)` [R1-12]: keyed by device token alone, so
  a device belongs to one account at a time; re-registration moves it; logout deletes it.
- Passwords: `hashlib.scrypt` n=2**14 r=8 p=1, 16-byte salt, stored as `scrypt$n$r$p$salt$hash` [R1-5].
  Password 8–128 chars, username 3–32 `[A-Za-z0-9._-]`, display name ≤ 64, request bodies ≤ 4 KB. A global
  semaphore of 2 concurrent hashes; benchmark on the NAS before shipping (target < 150 ms per hash).

### Modes [R1-2]
`SETHSTER_ACCOUNTS` = `off` | `dormant` | `on`, mutually exclusive with legacy auth:
- `off`: today's behaviour (legacy `SETHSTER_TOKENS`/`SETHSTER_AUTH`, FunnelGuard). Accounts routes absent.
- `dormant`: accounts routes live; content open to anyone (documented temporary window); requests that carry a
  valid approved session get their personal Rotation; admin routes require an approved admin.
- `on`: legacy tokens, FunnelGuard and network exemptions are ignored entirely. Same rule on LAN, tailnet and
  internet. If `on` and no admin exists, the server logs a warning at startup and every hour; sign-ups still queue
  and `create-admin` recovers [R2-3].
Rollback from `on` goes to `on` with a fixed build, never silently to `off`.

### Dependencies [R1-2, R1-7]
- `current_session` → valid unexpired session (any status). `approved_user` → plus status approved, re-read from
  the DB on every request (cheap; ~20 users). `admin_user` → plus `is_admin`.
- Stable errors: 401 `{"code":"signed_out"}` (missing/expired/revoked); 403 `{"code":"pending"|"rejected"|
  "disabled"}`; 403 `{"code":"not_admin"}`.

### Route matrix [R1-3]
- Public: `GET /api/health` returning only `{"ok": true}` (catalog counts/import time move to an approved route
  `/api/status`), `POST /api/auth/signup`, `POST /api/auth/login`.
- `current_session`: `/api/auth/me` (GET/DELETE), `/api/auth/logout`, `/api/auth/password`,
  `/api/auth/push-token` (POST/DELETE).
- `approved_user`: every other `/api/*` route, every method including HEAD and Range requests: performances,
  stream/transcode/download, artwork, composer/person images, programs, transcripts, liner notes, radio (and
  every media URL radio returns, which are server routes and so covered), calendar, compare, search, rotation,
  questions. Implemented by mounting all content routers with `approved_user`, plus a test that walks
  `app.routes` and fails if any route other than the public/session list lacks it.
- `admin_user`: `/api/admin/*`, question edit/delete.
- `/docs`, `/redoc`, `/openapi.json` disabled when mode is `on`.
- DSM reverse proxy does no caching (checked: plain `proxy_pass`, no `proxy_cache`); re-check after DSM updates.

### Client IP and throttling [R1-4]
- [R2-1] One parsing layer: Uvicorn's `proxy_headers` is turned **off** (`__main__.py`), so the app sees the real
  socket peer, and a single `client_ip(scope)` helper decides. Trusted peers: the Docker bridge gateway (DSM nginx
  and Tailscale Serve both reach the container through it) and 127.0.0.1. From a trusted peer, client IP = the
  **rightmost** `X-Forwarded-For` entry, which is the one the proxy itself appended (DSM uses
  `$proxy_add_x_forwarded_for`; Tailscale Serve appends the tailnet peer). `X-Real-IP` is ignored. Any other peer:
  the socket address. Client-supplied entries further left are never used. Verified by a test that sends a forged
  XFF through the real ASGI stack, and once by hand through DSM and Tailscale Serve after deploy. `from_internet`
  goes away with FunnelGuard in mode `on`.
- Throttles (in-memory, single worker, reset on restart — documented): per IP 5 sign-ups/hour and 20 failed
  logins/15 min; per normalized username 10 failed logins/15 min. 429 with Retry-After.

### Auth behaviour [R1-5]
- Login with unknown username hashes against a dummy hash, so timing and response match a wrong password:
  401 `{"code":"bad_credentials"}`. Status (pending/rejected/disabled) is revealed only after the password checks.
- Login always issues a session, whatever the status, so a rejected/disabled user can still reach their account
  and delete it [R1-8]; content routes refuse them.
- Sign-up with a taken username → 409 "username unavailable" (limited, documented disclosure).
- Sign-up → `pending` + session; push to admin devices.

### Endpoints
- `POST /api/auth/signup`, `POST /api/auth/login` → `{token, user: Me}`; `Me = {user_id, username, display_name,
  status, is_admin}` [R1-7].
- `GET /api/auth/me`, `POST /api/auth/logout` (revokes the session and drops its push device),
  `POST /api/auth/password {old, new}` (revokes other sessions), `DELETE /api/auth/me {password}` → deletes user,
  sessions, push devices, Rotation rows. Allowed even for the last admin (after a second confirmation in the app
  that warns approvals will stop until `create-admin` is run) [R2-3].
- `POST /api/auth/push-token {apns_token, env}`, `DELETE /api/auth/push-token`.
- Admin: `GET /api/admin/users?status=`, `POST /api/admin/users/{id}/approve|reject|disable|enable|make-admin`.
  Approve/enable/make-admin revoke the user's existing sessions only for make-admin (new session on next login is
  simpler than rotation for 20 users; the app re-logs in silently using a fresh login prompt) — approve does not
  revoke, since status is re-read per request.
- CLI (`python -m sethster_server.accounts`): `create-admin`, `reset-password <user>` (prints a one-time password,
  revokes sessions), `list`, `delete`, `move-rotation`.

### Rotation migration [R1-9]
- Table `rotation` in `rotation.db`. Listener for an account = `u:<user id>`.
- [R2-2] Ownership comes only from the session: with a valid approved session, every Rotation read/add/remove/
  reorder uses `u:<id>` and ignores any `listener` in the query or body. In `dormant` without a session, requests
  are pinned to `default`; a supplied `listener` starting with `u:` is rejected (400). Tests: cross-account
  read/add/remove/reorder.
- `move-rotation default <username>`: one transaction; back up `rotation.db` first; if the destination already has
  rows, append source items after them (skip duplicates), keeping source order and `added_at`; delete source rows;
  print before/after counts. Repeat-safe (second run moves nothing).
- Run at cutover, after the server is switched to `on` (so old clients can no longer write `default`).

### APNs [R1-12]
- `PyJWT[crypto]` + `httpx[http2]`. Provider JWT cached, refreshed every 50 min. One shared HTTP/2 client.
  Headers `apns-topic: com.goatsandmonkeys.sethster`, `apns-push-type: alert`. Env per device token as reported
  by the app (from its `aps-environment` entitlement via a build flag).
- 410/`BadDeviceToken`/`Unregistered` → delete the device row. 5xx/timeout → 2 retries with backoff in a
  background task. Bad credentials → logged error and `/api/admin/push-status` shows it. Never blocks signup or
  approval.
- Messages: admins "New Sethster sign-up: <display name> (<username>)"; user "You're in — Sethster is ready" or
  "Your Sethster request wasn't approved". Badge = pending count for admins.
- Key at `<deploy dir>/secrets/apns.p8` (read-only mount), `APNS_KEY_ID`, `APNS_TEAM_ID=MMZ8VT9YVZ`.
  No key → push off, badge still works.

## iOS
- `AccountStore` (@Observable): session in Keychain keyed by server origin; `me`, refreshed on launch, foreground,
  every 30 s while pending, and on push.
- [R1-6] Transport: authenticated traffic only over HTTPS. The fallback list becomes the public address and the
  Tailscale HTTPS name — the plain-http LAN fallback is removed. Health probes go without credentials. The
  session is sent only to its own origin and the built-in trusted alias set (public name + Tailscale name, same
  server); changing the server in Settings signs out. Query tokens are used only for AVPlayer media URLs on that
  origin, never on third-party URLs; server side, `?token=` is accepted only on media GET/HEAD routes, and the
  app's own access log redacts `token=` (uvicorn access log filter). DSM nginx logs: owner-side setting to turn
  off access logs for this reverse-proxy rule (documented step).
- Root gate: signed out → Welcome (Sign In / Create Account / Browse Demo Data). pending/rejected/disabled →
  status screen with Sign Out, **Delete Account**, Browse Demo Data [R1-8]. approved → today's tabs, with Settings
  → Account (username, Change Password, Sign Out, Delete Account).
- Admin: Settings → Approvals (badge), list with Approve/Reject; Everyone list with Disable/Enable/Make Admin;
  app-icon badge and Settings tab badge from `GET /api/admin/users?status=pending` count, refreshed on
  foreground independent of push.
- [R2-5] Push setup: add `aps-environment` to `Sethster/Entitlements/Sethster.entitlements` (`development`;
  Xcode export rewrites to `production` for App Store Connect builds) and the Push Notifications capability in
  `project.yml`; owner enables Push Notifications on App ID `com.goatsandmonkeys.sethster` and creates the APNs
  key. Verify with `codesign -d --entitlements :- ` on the exported .ipa that `aps-environment` is `production`.
  The app reports env from a compile-time flag (`DEBUG` → sandbox, else production), cross-checked by that step.
- Push: request permission after sign-up and for admins; register on every launch; upload token after
  login; delete on logout. Notification taps deep-link to Approvals only if `me.is_admin`.
- Sign-out / account switch / revocation [R1-10]: stop playback, clear queue and Now Playing, cancel account-bound
  tasks, bump an account epoch that stale callbacks check before publishing, and reset CarPlay to its root
  templates.
- [R2-4] Errors are handled by `code`, in one shared `AuthFailure` mapper used by the API client, radio client,
  player and download coordinator: `signed_out` → clear the session and run the path above; `pending` /
  `rejected` / `disabled` → keep the session, stop content, refresh `me`, show the status screen (account actions
  and deletion still work); `not_admin` → local error only; `bad_credentials` → form error only. Network errors
  keep today's offline fallback and never sign out.
- [R2-3] Delete Account also purges locally: the Keychain session, that account's Rotation metadata, downloaded
  files and download task records, and cached `me`; then returns to Welcome.
- Rotation offline store namespaced by `<server origin>/<user_id>`: metadata, files and background-download task
  descriptors; the download coordinator keys tasks by (account, track) and drops completions whose account doesn't
  match the current one [R1-9].
- CarPlay: not approved → single item "Open Sethster on your iPhone to sign in"; replaced on state change.
- Demo mode works without an account.

## External review [R1-11]
- Dedicated pre-approved account `appreview` (not admin) plus an admin review account `appreview-admin` so the
  reviewer can see Approvals; credentials in App Store Connect review notes, with steps: sign in, play,
  Settings → Account → Delete Account; sign-up → pending screen.
- [R2-6] The privacy policy is linked inside the app on Welcome, on the pending/rejected/disabled screen, and in
  Settings → Account, as well as in App Store Connect.
- Privacy policy: written at `server/sethster_server/static/privacy.html` (owner approved; contact = the owner's email).
  Served publicly at `GET /privacy` → `https://<public-host>:<port>/privacy`; include
  `static/*.html` as package data so the Docker image carries it. Content covers: what's stored (username, display name,
  password hash, Rotation, push token, IP for rate limits in memory only), retention, deletion. URL entered in
  App Store Connect test information along with beta description and feedback email.

## Rollout [R1-13]
1. Deploy server in `dormant`. Acceptance: signup/login/me/admin work; admin routes refuse non-admins;
   anonymous content still works (temporary, documented); a signed-in approved user gets their own Rotation.
2. Owner creates their admin account (CLI) and the main listener's account (approved); reviewer accounts.
3. Upload build; internal testers (owner, main listener) sign in; submit external build for review.
4. After review approval **and** both owner and main listener confirmed signed in on the new build: freeze (switch to `on`),
   back up `rotation.db`, `move-rotation default <username>`, verify counts.
5. Verify `on` over public, tailnet and LAN; restart the container and verify accounts persist; re-run the
   importer on a copy and verify accounts.db untouched.

## Tests
Server pytest: importer survival; every mode; route matrix walker; HEAD/Range on media; each status vs content and
account routes; expired/revoked/stale tokens; forged forwarding headers; throttles; login timing parity (dummy
hash called); last-admin deletion refusal; deletion cascade incl. Rotation and push devices; push token moving
between accounts; APNs sender (mocked) incl. 410 cleanup; move-rotation idempotence and conflict merge; dormant →
on transition.
iOS: AccountStore transitions (mock API); auth-failure path from media; stale download completion after account
switch; simulator run of sign-up → pending → approve from an admin simulator → play; CarPlay sign-in item.

## Out of scope
Email password reset (CLI reset covers it), 2FA, web admin.
