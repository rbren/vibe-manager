"""New-entry reopen tests — plain script, no pytest dependency.

A new *user* entry on a finished/needs_input ticket must immediately move it
back to pending (bottom of the pending column) instead of waiting for the
manager cycle. in_progress, pending and verified tickets are left alone, as
are manager/agent entries.

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_reopen_on_entry.py
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


def make_ticket(ws_id: str, status: str = "pending") -> str:
    r = client.post(f"/api/workspaces/{ws_id}/tickets", json={"body": "build the thing"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    if status != "pending":
        r = client.patch(f"/api/manager/tickets/{tid}", json={"status": status})
        assert r.status_code == 200, r.text
    return tid


def append(tid: str, author: str = "user") -> dict:
    r = client.post(f"/api/tickets/{tid}/entries", json={"body": "one more thing", "author": author})
    assert r.status_code == 200, r.text
    return r.json()


def test_user_entry_reopens_finished_and_needs_input():
    ws_id = seed_workspace()
    for status in ("finished", "needs_input"):
        tid = make_ticket(ws_id, status)
        t = append(tid)
        assert t["status"] == "pending", f"{status}: expected pending, got {t['status']}"
        assert len(t["entries"]) == 2


def test_reopened_ticket_goes_to_bottom_of_pending():
    ws_id = seed_workspace()
    make_ticket(ws_id)  # existing pending ticket
    tid = make_ticket(ws_id, "finished")
    append(tid)
    board = client.get(f"/api/workspaces/{ws_id}/board").json()
    pending = [t["id"] for t in board["tickets"] if t["status"] == "pending"]
    assert pending[-1] == tid, f"reopened ticket should sort last, got {pending}"


def test_manager_and_agent_entries_do_not_reopen():
    ws_id = seed_workspace()
    for author in ("manager", "agent"):
        tid = make_ticket(ws_id, "finished")
        t = append(tid, author=author)
        assert t["status"] == "finished", f"{author}: expected finished, got {t['status']}"


def test_pending_and_in_progress_untouched():
    ws_id = seed_workspace()
    for status in ("pending", "in_progress"):
        tid = make_ticket(ws_id, status)
        t = append(tid)
        assert t["status"] == status, f"expected {status}, got {t['status']}"


def test_verified_stays_verified():
    ws_id = seed_workspace()
    tid = make_ticket(ws_id, "finished")
    r = client.post(f"/api/tickets/{tid}/verify")
    assert r.status_code == 200, r.text
    t = append(tid)
    assert t["status"] == "verified", f"expected verified, got {t['status']}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
