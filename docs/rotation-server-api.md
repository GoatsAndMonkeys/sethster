# Rotation server API

Rotation is an authenticated, ordered list of performances for one listener. The listener defaults to
`"default"`; the field is present now so separate family profiles can be added later. State lives in
`SETHSTER_ROTATION_DB`, or `SETHSTER_DATA/rotation.db` by default, and therefore survives catalog re-imports.

## Endpoints

All endpoints use the server's existing authentication rules and `/api` prefix.

### `GET /api/rotation?listener=default`

Returns the listener's performances in rotation order:

```json
{
  "listener": "default",
  "items": [
    {
      "id": "performance-id",
      "title": "Performance title",
      "subtitle": null,
      "composers": [{"id": "composer-id", "name": "Composer Name"}],
      "date": "2020-01-01",
      "date_status": "confirmed",
      "venue": {"id": "venue-id", "name": "Venue Name", "city": "City"},
      "kind": "broadcast",
      "collection": "Collection",
      "duration_s": 3600,
      "artwork_url": "/api/performances/performance-id/artwork",
      "artwork": null,
      "confirmed_ratio": 1.0,
      "added_at": "2026-10-21T18:00:00Z",
      "download": {
        "tracks": [
          {
            "id": "performance-id:01",
            "url": "/api/tracks/performance-id:01/download",
            "bytes": 12345678,
            "format": "mp3"
          }
        ]
      }
    }
  ]
}
```

Each item contains exactly the normal performance-list summary fields, followed by `added_at` and `download`.
Nullable summary fields remain `null`. `download.tracks` is in track order. `bytes` is an integer when recorded in
the catalog or obtainable with a filesystem stat; it can be `null` when neither is available. The URL serves the
original catalog file and never transcodes it.

### `POST /api/rotation`

Adds a performance at the end. Adding one already present is idempotent: its position and `added_at` do not change.

Request:

```json
{"performance_id": "performance-id", "listener": "default"}
```

`listener` may be omitted. The response is the single enriched performance item shown inside `items` above.
An unknown `performance_id` returns `404` with `{"detail":"performance not found"}`.

### `DELETE /api/rotation/{performance_id}?listener=default`

Removes the performance and returns:

```json
{"ok": true}
```

An unknown catalog ID returns `404` with `{"detail":"performance not found"}`. A known performance that is not in
that listener's rotation returns `404` with `{"detail":"performance not in rotation"}`.

### `PUT /api/rotation`

Reorders a listener's rotation. `performance_ids` is the complete desired order, not a partial update.

Request:

```json
{
  "performance_ids": ["second-performance-id", "first-performance-id"],
  "listener": "default"
}
```

`listener` may be omitted. The response has the same `{ "listener", "items" }` shape as `GET /api/rotation`.
Unknown performance IDs return `404`. Duplicate IDs, omitted current IDs, or extra known IDs return `422`.

### `GET|HEAD /api/tracks/{track_id}/download`

Serves the track's original file with its original byte length. This route does not use the streaming transcoder.
An unknown track or missing file returns `404`; an unavailable media share returns `503`.

## Files changed

- `server/sethster_server/rotation_store.py` — persistent ordered-list store.
- `server/sethster_server/rotation_api.py` — rotation and original-download routes.
- `server/sethster_server/app.py` — registers the rotation router.
- `server/tests/test_rotation.py` — API, ordering, listeners, idempotency, errors, sizes, and original-file tests.
- `docs/rotation-server-api.md` — this client contract and implementation summary.

## Test result

Run from `server` with `~/.local/share/sethster-venv/bin/python -m pytest`: **23 passed** (Python 3.14.3,
pytest 9.1.1).

## Review pass 1

- Fixed original-download path containment. Catalog paths are now opened component-by-component beneath an open
  media-root directory without following symlinks, preventing `..`, absolute-path, backslash, symlink, and
  resolve/open race escapes. Missing, invalid, non-regular, and symlink paths return `404`; an unavailable media
  root still returns `503`.
- Fixed download byte metadata preferring a stale catalog value even when the original file is mounted. Rotation
  responses now report the opened file's actual byte length, and GET/HEAD use that same length.
- Fixed concurrent appends racing between `MAX(position)` and `INSERT`, which could return `500` with a uniqueness
  or SQLite-lock error. Mutations now acquire a SQLite `BEGIN IMMEDIATE` write reservation before reading state.
- Fixed reorder validation racing with an append. Exact-list validation and position updates now share one write
  transaction, so a concurrent item cannot be left at a negative position; a stale partial reorder returns `422`.
- Fixed a successful add returning `500` (`StopIteration`) if that item was removed by another request while its
  response was being assembled. The POST response is now built from the completed add result.
- Added regression coverage for each issue, plus HEAD length/body behavior. The complete server suite passes.
