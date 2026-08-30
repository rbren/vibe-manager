"""Per-workspace primary colour tests — plain script, no pytest dependency.

Each workspace picks one of ten primaries; the SPA writes it to <html> as
data-accent and style.css derives the whole palette from it. The API side is
just the `accent` column: a default, a validated PATCH, and no interference
with the other settings.

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_workspace_accent.py
Uses a temp DB + data dir via VIBE_DB_PATH / VIBE_DATA_DIR so the live
vibe.db and data/ are never touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="vibe-test-"))
os.environ["VIBE_DB_PATH"] = str(TMP / "vibe.db")
os.environ["VIBE_DATA_DIR"] = str(TMP / "data")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as vibe_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(vibe_app.app)


def seed_workspace() -> str:
    ws_id = uuid.uuid4().hex[:12]
    now = time.time()
    with vibe_app.db() as conn:
        conn.execute(
            "INSERT INTO workspaces(id, path, name, created_at) VALUES(?,?,?,?)",
            (ws_id, f"/tmp/ws-{ws_id}", "testws", now),
        )
    return ws_id


def listed(ws_id: str) -> dict:
    r = client.get("/api/workspaces")
    assert r.status_code == 200, r.text
    return next(w for w in r.json()["selected"] if w["id"] == ws_id)


def test_ten_accents_offered():
    assert len(vibe_app.ACCENTS) == 10, vibe_app.ACCENTS
    assert len(set(vibe_app.ACCENTS)) == 10, "accent names must be unique"
    assert vibe_app.DEFAULT_ACCENT in vibe_app.ACCENTS
    print("ok: exactly ten distinct primaries, default among them")


def test_accent_defaults():
    ws = seed_workspace()
    assert listed(ws)["accent"] == vibe_app.DEFAULT_ACCENT
    print("ok: a new workspace starts on the default primary")


def test_patch_sets_accent():
    ws = seed_workspace()
    for accent in vibe_app.ACCENTS:
        r = client.patch(f"/api/workspaces/{ws}", json={"accent": accent})
        assert r.status_code == 200, r.text
        assert r.json()["accent"] == accent
        assert listed(ws)["accent"] == accent
    print("ok: every one of the ten primaries round-trips through PATCH")


def test_unknown_accent_rejected():
    ws = seed_workspace()
    client.patch(f"/api/workspaces/{ws}", json={"accent": "teal"})
    r = client.patch(f"/api/workspaces/{ws}", json={"accent": "chartreuse"})
    assert r.status_code == 400, r.text
    assert "accent must be one of" in r.json()["detail"]
    assert listed(ws)["accent"] == "teal", "a rejected value leaves the choice alone"
    print("ok: an unknown primary is rejected and nothing changes")


def test_other_settings_keep_accent():
    ws = seed_workspace()
    client.patch(f"/api/workspaces/{ws}", json={"accent": "orchid"})
    r = client.patch(f"/api/workspaces/{ws}", json={"max_concurrent": 5, "push_mode": "main"})
    assert r.status_code == 200, r.text
    assert r.json()["accent"] == "orchid"
    assert r.json()["max_concurrent"] == 5
    print("ok: PATCHing other settings leaves the primary untouched")


def test_accent_is_per_workspace():
    a, b = seed_workspace(), seed_workspace()
    client.patch(f"/api/workspaces/{a}", json={"accent": "jade"})
    client.patch(f"/api/workspaces/{b}", json={"accent": "rose"})
    assert listed(a)["accent"] == "jade"
    assert listed(b)["accent"] == "rose"
    print("ok: each workspace keeps its own primary")


if __name__ == "__main__":
    test_ten_accents_offered()
    test_accent_defaults()
    test_patch_sets_accent()
    test_unknown_accent_rejected()
    test_other_settings_keep_accent()
    test_accent_is_per_workspace()
    print("all workspace accent tests passed")
