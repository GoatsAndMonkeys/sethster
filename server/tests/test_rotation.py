import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from inspect import getclosurevars

import pytest
from fastapi.testclient import TestClient

from sethster_server import db as dbmod
from sethster_server.app import create_app
from sethster_server.config import Settings
from sethster_server.rotation_store import RotationStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    media = tmp_path / "media"
    (media / "music").mkdir(parents=True)
    (media / "music" / "one.mp3").write_bytes(b"one")
    (media / "music" / "two.flac").write_bytes(b"original-flac")
    database = tmp_path / "sethster.db"
    conn = sqlite3.connect(database)
    dbmod.init(conn)
    conn.executemany(
        "INSERT INTO performances (id, title, date, date_status, kind, collection, duration_s) VALUES (?,?,?,?,?,?,?)",
        [("p1", "First performance", "2020-01-01", "confirmed", "broadcast", "Radio", 60),
         ("p2", "Second performance", "2021-01-01", "confirmed", "release", "CD", 120)])
    conn.execute("INSERT INTO composers (id, name) VALUES ('c1', 'Composer')")
    conn.executemany("INSERT INTO performance_composers VALUES (?,?,?)", [("p1", "c1", 0), ("p2", "c1", 0)])
    conn.executemany(
        "INSERT INTO tracks (id, performance_id, idx, title, format, path, stream_path, size) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [("p1:01", "p1", 1, "One", "mp3", "music/one.mp3", "music/one.mp3", 3),
         ("p2:01", "p2", 1, "Two", "flac", "music/two.flac", "music/two.flac", None)])
    conn.commit()
    conn.close()
    settings = Settings(data_dir=tmp_path / "data", db_path=database, cache_dir=tmp_path / "cache",
                        media_root=media, tokens=[], ffmpeg=None, aac_encoder="aac", aac_bitrate="256k")
    monkeypatch.setenv("SETHSTER_ROTATION_DB", str(tmp_path / "rotation.db"))
    with TestClient(create_app(settings)) as c:
        yield c


def test_rotation_add_list_and_idempotency(client):
    assert client.get("/api/rotation").json() == {"listener": "default", "items": []}
    first = client.post("/api/rotation", json={"performance_id": "p1"})
    assert first.status_code == 200
    item = first.json()
    assert item["id"] == "p1" and item["title"] == "First performance"
    assert item["composers"] == [{"id": "c1", "name": "Composer"}]
    assert item["added_at"].endswith("Z")
    assert item["download"] == {"tracks": [
        {"id": "p1:01", "url": "/api/tracks/p1:01/download", "bytes": 3, "format": "mp3"}]}

    again = client.post("/api/rotation", json={"performance_id": "p1"})
    assert again.status_code == 200 and again.json()["added_at"] == item["added_at"]
    assert [x["id"] for x in client.get("/api/rotation").json()["items"]] == ["p1"]
    assert client.post("/api/rotation", json={"performance_id": "missing"}).status_code == 404


def test_rotation_listener_reorder_and_remove(client):
    for pid in ("p1", "p2"):
        assert client.post("/api/rotation", json={"performance_id": pid}).status_code == 200
    reordered = client.put("/api/rotation", json={"performance_ids": ["p2", "p1"]})
    assert reordered.status_code == 200
    assert [x["id"] for x in reordered.json()["items"]] == ["p2", "p1"]
    assert reordered.json()["items"][0]["download"]["tracks"][0]["bytes"] == len(b"original-flac")

    assert client.put("/api/rotation", json={"performance_ids": ["p1"]}).status_code == 422
    assert client.put("/api/rotation", json={"performance_ids": ["p1", "p1"]}).status_code == 422
    assert client.put("/api/rotation", json={"performance_ids": ["p1", "missing"]}).status_code == 404
    assert client.delete("/api/rotation/p2").json() == {"ok": True}
    assert client.delete("/api/rotation/p2").status_code == 404

    assert client.post("/api/rotation", json={"performance_id": "p2", "listener": "alice"}).status_code == 200
    assert client.get("/api/rotation", params={"listener": "alice"}).json()["items"][0]["id"] == "p2"
    assert [x["id"] for x in client.get("/api/rotation").json()["items"]] == ["p1"]


def test_rotation_download_serves_original_without_transcoding(client):
    response = client.get("/api/tracks/p2:01/download")
    assert response.status_code == 200
    assert response.content == b"original-flac"
    assert response.headers["content-type"] == "application/octet-stream"
    head = client.head("/api/tracks/p2:01/download")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(b"original-flac"))
    assert client.get("/api/tracks/missing/download").status_code == 404


def test_rotation_download_does_not_follow_symlinks_outside_media_root(client, tmp_path):
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"private")
    media = tmp_path / "media"
    (media / "escape.mp3").symlink_to(outside)
    database = tmp_path / "sethster.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tracks (id, performance_id, idx, title, format, path, stream_path) "
            "VALUES (?,?,?,?,?,?,?)",
            ("p1:escape", "p1", 2, "Escape", "mp3", "escape.mp3", "escape.mp3"),
        )
    assert client.get("/api/tracks/p1:escape/download").status_code == 404


@pytest.mark.parametrize("bad_path", [None, "bad\x00path.mp3"])
def test_rotation_download_with_invalid_catalog_path_returns_404(client, tmp_path, bad_path):
    database = tmp_path / "sethster.db"
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE tracks SET path=? WHERE id='p1:01'", (bad_path,))
    assert client.get("/api/tracks/p1:01/download").status_code == 404


def test_rotation_reports_actual_file_size_when_catalog_size_is_stale(client, tmp_path):
    database = tmp_path / "sethster.db"
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE tracks SET size=999 WHERE id='p1:01'")
    item = client.post("/api/rotation", json={"performance_id": "p1"}).json()
    assert item["download"]["tracks"][0]["bytes"] == len(b"one")


def test_rotation_add_is_not_a_500_if_concurrently_removed(client, monkeypatch):
    route = next(route for included in client.app.routes if hasattr(included, "original_router")
                 for route in included.original_router.routes
                 if route.path == "/api/rotation" and "POST" in route.methods)
    store = getclosurevars(route.endpoint).nonlocals["store"]
    original_add = store.add

    def add_then_remove(listener, performance_id, check_owner=None):
        row = original_add(listener, performance_id, check_owner)
        store.remove(listener, performance_id)
        return row

    monkeypatch.setattr(store, "add", add_then_remove)
    response = client.post("/api/rotation", json={"performance_id": "p1"})
    assert response.status_code == 200
    assert response.json()["id"] == "p1"


def test_concurrent_rotation_appends_choose_distinct_positions(tmp_path, monkeypatch):
    # Force both old deferred implementations past their MAX(position) reads
    # before either inserts. BEGIN IMMEDIATE serializes them before this point.
    rendezvous = threading.Barrier(2)

    def simultaneous_timestamp():
        try:
            rendezvous.wait(timeout=0.2)
        except threading.BrokenBarrierError:
            pass
        return "2026-10-21T18:00:00Z"

    monkeypatch.setattr("sethster_server.rotation_store.now_iso", simultaneous_timestamp)
    path = tmp_path / "rotation.db"
    stores = [RotationStore(path), RotationStore(path)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(stores[i].add, "default", f"p{i}") for i in range(2)]
        for future in futures:
            future.result()
    assert {row["performance_id"] for row in stores[0].items("default")} == {"p0", "p1"}


def test_reorder_rejects_a_stale_partial_list_without_corrupting_positions(tmp_path):
    store = RotationStore(tmp_path / "rotation.db")
    store.add("default", "p1")
    stale_order = ["p1"]
    store.add("default", "p2")

    assert store.reorder("default", stale_order) is False
    assert [row["performance_id"] for row in store.items("default")] == ["p1", "p2"]
