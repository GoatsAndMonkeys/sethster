# Sethster accounts API (server side of docs/plans/accounts.md)

Mode: `SETHSTER_ACCOUNTS=off|dormant|on` (default `off` = today's behaviour; none of these routes exist).

- `dormant`: account routes live; content open to anyone; signed-in approved users get their own Rotation.
- `on`: every content route needs an approved account. Legacy `SETHSTER_TOKENS` and funnel/network rules are ignored.

## Auth

`Authorization: Bearer <token>` on every request. `?token=<token>` is accepted **only** on GET/HEAD of media:
`/api/tracks/{id}/stream`, `/api/tracks/{id}/download`, `/api/performances/{pid}/artwork`,
`/api/composers/{cid}/image`, `/api/people/{id}/image`. Tokens last 180 days, sliding (extended at most once a day).

## Shapes

Error (every refusal from account checks and account/admin routes):

```json
{"code": "pending", "detail": "account pending"}
```

Me:

```json
{"user_id": 3, "username": "alice", "display_name": "Alice", "status": "pending|approved|rejected|disabled", "is_admin": false}
```

Admin user = Me plus `"created_at"`, `"decided_at"`, `"decided_by"` (ISO 8601 UTC `...Z` strings / user id; null when undecided).

## Routes

| Route | Needs | Body | Returns |
|---|---|---|---|
| `GET /api/health` | nothing | | `{"ok": true}` (full stats in `off` mode only) |
| `GET /api/status` | approved | | server stats (was in health) |
| `POST /api/auth/signup` | nothing | `{username, password, display_name?, device?}` | `{"token", "user": Me}` (status `pending`) |
| `POST /api/auth/login` | nothing | `{username, password, device?}` | `{"token", "user": Me}` for **any** status |
| `GET /api/auth/me` | session | | Me |
| `DELETE /api/auth/me` | session | `{password}` | `{"ok": true}` |
| `POST /api/auth/logout` | session | | `{"ok": true}` (also drops this session's push device) |
| `POST /api/auth/password` | session | `{old, new}` | `{"ok": true}`; other sessions revoked |
| `POST /api/auth/push-token` | session | `{apns_token: hex, env: "sandbox"\|"production"}` | `{"ok": true}` |
| `DELETE /api/auth/push-token` | session | | `{"ok": true}` |
| `GET /api/admin/users?status=` | admin | | `{"items": [AdminUser], "pending_count": n}` |
| `POST /api/admin/users/{id}/{approve\|reject\|disable\|enable\|make-admin}` | admin | | AdminUser |
| `GET /api/admin/push-status` | admin | | `{"enabled", "sent", "last_error", "last_error_at"}` |
| `GET /privacy` | nothing | | privacy policy HTML |

Rotation routes ignore the client's `listener` once signed in (approved): it is always `u:<user_id>`. In `dormant`,
anonymous requests use `default`; an anonymous `listener` starting with `u:` is refused (400 `bad_listener`).

## Codes

| HTTP | code | When |
|---|---|---|
| 401 | `signed_out` | missing/expired/revoked token, or user deleted (`WWW-Authenticate: Bearer`) |
| 401 | `bad_credentials` | wrong username or password (login, password change, delete account) |
| 403 | `pending` / `rejected` / `disabled` | signed in but not approved (content routes) |
| 403 | `not_admin` | admin routes; editing or deleting an existing question answer |
| 400 | `bad_listener` | see Rotation above |
| 404 | `not_found` | admin action on an unknown user |
| 409 | `username_unavailable` | sign-up name taken (case-insensitive) |
| 413 | `too_large` | body over 4 KB on `/api/auth/*`, `/api/admin/*` |
| 422 | `invalid_username` | not 3–32 of `A-Z a-z 0-9 . _ -` |
| 422 | `invalid_password` | not 8–128 characters |
| 422 | `invalid_display_name` | over 64 characters |
| 422 | `invalid_push_token` / `invalid_push_env` | push-token body |
| 422 | `invalid` | other body/query validation (`detail` is FastAPI's error list) |
| 429 | `rate_limited` | `Retry-After` seconds: 5 sign-ups/IP/hour; 10 failed logins/username or 20/IP per 15 min |

## Push

Alerts (when the server has an APNs key): admins get "New Sethster sign-up: <display name> (<username>)" with
`badge` = pending count; the user gets "You're in — Sethster is ready" on approve/enable and "Your Sethster request
wasn't approved" on reject.
