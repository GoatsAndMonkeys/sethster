"""CD readiness gate (cd_ready), CD -> result conversion (cd.to_result) and the artwork endpoint
(?size=&role=, docs/plans/cd-catalog.md §4.4). Everything runs on a throwaway data dir."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sethster_server import cd as cdmod
from sethster_server import cd_ready
from sethster_server.app import create_app
from sethster_server.config import Settings
from sethster_server.importer import build

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def _w(p: Path, doc) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))


def harvest(pid):
    return {"id": pid, "name": "Mahler 2", "discs": [{"dir": f"Crutchfield/{pid}", "label": "CD1", "tracks": [
        {"file": "01.flac", "dur": 600.5, "tags": {"TCOM": ["Gustav Mahler"], "TALB": ["Symphony No. 2"],
                                                     "TPE3": ["Claudio Abbado"], "TPE2": ["Chicago Symphony Orchestra"]}},
        {"file": "02.flac", "dur": 300.0, "tags": {}}]}]}


def match(status="matched"):
    return {"status": status, "method": "discid", "confidence": 0.97, "confidence_band": "high",
            "parsed": {"composer": "Gustav Mahler", "work": "Symphony No. 2", "conductor": "Abbado"},
            "release": {"title": "Symphony No. 2", "status": "Official", "date": "1977",
                        "url": "https://musicbrainz.org/release/x", "labels": [{"name": "DG", "catno": "123"}],
                        "recording_dates": ["1976-02"],
                        "credits": [{"name": "Claudio Abbado", "type": "conductor", "tracks": 2},
                                    {"name": "Chicago Symphony Orchestra", "type": "performing orchestra", "tracks": 2}]}}


def manifest(sha="ab" * 32, status="ok", disc=True):
    entry = lambda s: {"sha": s, "src": "embedded", "w": 1400, "h": 1400, "generic": False,
                       "thumbs": {n: f"media/cd/thumb/{s}_{n}.webp" for n in ("160", "480", "1200")}}
    m = {"status": status, "needs_vision": False}
    if status == "ok":
        m["front"] = entry(sha)
        if disc:
            m["disc"] = entry("cd" * 32)
    return m


def make_cd(data: Path, pid: str, m_status="matched", art=None, harvested=True):
    if harvested:
        _w(data / "crutchfield" / "harvest" / f"{pid}.json", harvest(pid))
    if m_status is not None:
        _w(data / "crutchfield" / "results" / f"{pid}.json", match(m_status))
    if art is not None:
        _w(data / "media" / "cd" / f"{pid}.json", art)
        for e in (art.get("front"), art.get("disc")):
            for n, rel in (e or {}).get("thumbs", {}).items():
                (data / rel).parent.mkdir(parents=True, exist_ok=True)
                (data / rel).write_bytes(b"RIFF0000WEBP" + n.encode())


# ---- readiness gate -------------------------------------------------------------------------------------
def test_gate(tmp_path):
    make_cd(tmp_path, "cf-ready", art=manifest())
    make_cd(tmp_path, "cf-noart-confirmed", art=manifest(status="none"))
    make_cd(tmp_path, "cf-unmatched", m_status="unmatched", art=manifest())
    make_cd(tmp_path, "cf-art-error", art={"status": "error"})
    make_cd(tmp_path, "cf-no-manifest")
    make_cd(tmp_path, "cf-no-result", m_status=None, art=manifest())
    make_cd(tmp_path, "cf-not-harvested", harvested=False, art=manifest())
    _w(tmp_path / "crutchfield" / "harvest" / "cf-empty.json", harvest("cf-empty"))
    _w(tmp_path / "crutchfield" / "results" / "cf-empty.json", "")          # a failed copy: 0 bytes
    _w(tmp_path / "media" / "cd" / "cf-empty.json", manifest())
    _w(tmp_path / "crutchfield" / "harvest" / "cf-bad.json", harvest("cf-bad"))
    _w(tmp_path / "crutchfield" / "results" / "cf-bad.json", "{half-written")
    _w(tmp_path / "media" / "cd" / "cf-bad.json", manifest())

    s = cd_ready.scan(tmp_path)
    assert s["ready"] == {"cf-ready", "cf-noart-confirmed"}
    assert "cf-unmatched" in s["match"] and "cf-unmatched" not in s["matched"]
    assert "cf-empty" not in s["match"] and "cf-bad" not in s["match"]
    assert "cf-art-error" not in s["art_decided"]
    assert cd_ready.ready_ids(tmp_path) == s["ready"]


def test_gate_cache_tracks_changes(tmp_path):
    make_cd(tmp_path, "cf-a", m_status="unmatched", art=manifest())
    cache = {}
    assert cd_ready.scan(tmp_path, cache)["ready"] == set()
    cache = json.loads(json.dumps(cache))  # the dashboard persists it as JSON
    p = tmp_path / "crutchfield" / "results" / "cf-a.json"
    p.write_text(json.dumps(match()) + " ")  # new size -> re-read
    assert cd_ready.scan(tmp_path, cache)["ready"] == {"cf-a"}
    p.unlink()
    assert cd_ready.scan(tmp_path, cache)["ready"] == set()
    assert "cf-a" not in cache["results"]


def test_is_ready():
    assert cd_ready.is_ready({}, match(), manifest())
    assert not cd_ready.is_ready({}, match("unmatched"), manifest())
    assert not cd_ready.is_ready({}, match(), {"status": "error"})
    assert not cd_ready.is_ready(None, match(), manifest())


# ---- conversion ------------------------------------------------------------------------------------------
def test_to_result():
    res, dossier = cdmod.to_result(harvest("cf-x"), match(), {"Crutchfield/cf-x/01.flac": 1234})
    perf = res["performance"]
    assert res["kind"] == "commercial_release" and res["origin"] == "crutchfield"
    assert res["program"][0]["composer"] == "Gustav Mahler"
    roles = {(c["name"], c["role"]) for c in res["credits"]}
    assert ("Claudio Abbado", "conductor") in roles
    assert ("Chicago Symphony Orchestra", "orchestra") in roles
    assert len([c for c in res["credits"] if c["name"] == "Chicago Symphony Orchestra"]) == 1
    assert perf["date"].startswith("1976") and perf["release"]["year"] == "1977"  # the recording date, never the 1977 release date
    assert [t["path"] for t in dossier["tracks"]] == ["Crutchfield/cf-x/01.flac", "Crutchfield/cf-x/02.flac"]
    assert dossier["tracks"][0]["size"] == 1234


def test_art_file_fallbacks(tmp_path):
    m = manifest()
    make_cd(tmp_path, "cf-x", art=m)
    assert cdmod.art_file(tmp_path, m, "front", 160).endswith("_160.webp")
    (tmp_path / m["front"]["thumbs"]["160"]).unlink()
    assert cdmod.art_file(tmp_path, m, "front", 160).endswith("_480.webp")  # nearest other size
    assert cdmod.art_file(tmp_path, m, "back", 480) is None


# ---- artwork endpoint ------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    data, media = tmp_path / "data", tmp_path / "media"
    make_cd(data, "cf-ready", art=manifest())
    make_cd(data, "cf-plain", art=manifest(disc=False))
    # one broadcast with a legacy cover on the share
    _w(data / "pilot" / "results" / "bc-1.json", {
        "id": "bc-1", "kind": "broadcast", "summary": "x", "track_map": [], "credits": [], "sources": [],
        "open_questions": [], "program": [{"composer": "Ludwig van Beethoven", "work": "Symphony No. 5"}],
        "performance": {"date": "2020-01-01", "date_status": "confirmed", "venue": "Hall", "city": ""}})
    (media / "bc").mkdir(parents=True)
    (media / "bc" / "cover.png").write_bytes(PNG)
    settings = Settings(data_dir=data, db_path=tmp_path / "t.db", cache_dir=tmp_path / "cache",
                        media_root=media, tokens=[], ffmpeg=None, aac_encoder="aac", aac_bitrate="256k",
                        result_dirs=[data / "pilot" / "results"], wiki_dir=data / "wiki",
                        transcripts_dir=data / "transcripts", programs_dir=data / "programs")
    stats = build(settings, use_media=False)
    assert stats["cds"] == 2
    import sqlite3
    con = sqlite3.connect(settings.db_path)
    con.execute("UPDATE performances SET artwork_path='bc/cover.png' WHERE id='bc-1'")
    con.commit()
    con.close()
    with TestClient(create_app(settings)) as c:
        yield c


def test_artwork_default_is_stored_front(client):
    r = client.get("/api/performances/cf-ready/artwork")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert r.content.endswith(b"480")


def test_artwork_size_and_role(client):
    assert client.get("/api/performances/cf-ready/artwork?size=160").content.endswith(b"160")
    assert client.get("/api/performances/cf-ready/artwork?size=1200&role=disc").content.endswith(b"1200")
    assert client.get("/api/performances/cf-ready/artwork?role=back").status_code == 404
    assert client.get("/api/performances/cf-plain/artwork?role=disc").status_code == 404


@pytest.mark.parametrize("q", ["size=999", "size=abc", "role=spine", "role=../x"])
def test_artwork_invalid_query(client, q):
    assert client.get(f"/api/performances/cf-ready/artwork?{q}").status_code == 422


def test_artwork_legacy_and_missing(client):
    r = client.get("/api/performances/bc-1/artwork")
    assert r.status_code == 200 and r.content == PNG
    assert client.get("/api/performances/bc-1/artwork?role=disc").status_code == 404
    assert client.get("/api/performances/nope/artwork").status_code == 404


def test_cd_summary_carries_artwork_meta(client):
    rows = client.get("/api/performances", params={"collection": "Crutchfield"}).json()
    rows = rows.get("items", rows) if isinstance(rows, dict) else rows
    cd = next(r for r in rows if r["id"] == "cf-ready")
    assert set(cd["artwork"]["roles"]) == {"front", "disc"} and cd["artwork"]["w"] == 1400
