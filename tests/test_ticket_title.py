"""Ticket title tests — plain script, no pytest dependency.

The manager sets a short emoji-prefixed `title` on tickets via
PATCH /api/manager/tickets/<id>; it must round-trip through the board and
manager snapshot dicts.

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_ticket_title.py
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


def make_ticket(ws_id: str) -> str:
    r = client.post(f"/api/workspaces/{ws_id}/tickets", json={"body": "build the thing"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def board_ticket(ws_id: str, tid: str) -> dict:
    r = client.get(f"/api/workspaces/{ws_id}/board")
    assert r.status_code == 200, r.text
    return next(t for t in r.json()["tickets"] if t["id"] == tid)


def snapshot_ticket(ws_id: str, tid: str) -> dict:
    r = client.get(f"/api/manager/workspaces/{ws_id}/snapshot")
    assert r.status_code == 200, r.text
    return next(t for t in r.json()["tickets"] if t["id"] == tid)


def test_title_defaults_to_none():
    ws = seed_workspace()
    tid = make_ticket(ws)
    assert board_ticket(ws, tid)["title"] is None
    assert snapshot_ticket(ws, tid)["title"] is None
    print("ok: title defaults to None on board + snapshot")


def test_manager_patch_sets_title():
    ws = seed_workspace()
    tid = make_ticket(ws)
    r = client.patch(f"/api/manager/tickets/{tid}", json={"title": "🐛 Login fix"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "🐛 Login fix"
    assert board_ticket(ws, tid)["title"] == "🐛 Login fix"
    assert snapshot_ticket(ws, tid)["title"] == "🐛 Login fix"
    print("ok: manager PATCH sets title, visible on board + snapshot")


def test_title_trim_and_clear():
    ws = seed_workspace()
    tid = make_ticket(ws)
    r = client.patch(f"/api/manager/tickets/{tid}", json={"title": "  🎨 Dark mode  "})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "🎨 Dark mode"
    r = client.patch(f"/api/manager/tickets/{tid}", json={"title": "   "})
    assert r.status_code == 200, r.text
    assert r.json()["title"] is None
    print("ok: title is trimmed; blank title clears it")


def test_patch_without_title_keeps_it():
    ws = seed_workspace()
    tid = make_ticket(ws)
    client.patch(f"/api/manager/tickets/{tid}", json={"title": "📎 Attachments"})
    r = client.patch(f"/api/manager/tickets/{tid}", json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "📎 Attachments"
    print("ok: PATCH without title leaves it untouched")


if __name__ == "__main__":
    test_title_defaults_to_none()
    test_manager_patch_sets_title()
    test_title_trim_and_clear()
    test_patch_without_title_keeps_it()
    print("all ticket title tests passed")
