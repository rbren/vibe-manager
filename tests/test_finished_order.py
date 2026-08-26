"""Finished-column ordering tests — plain script, no pytest dependency.

Tickets get a `finished_at` timestamp when they transition to finished (via
the manager PATCH endpoint); the SPA orders the finished column by it, most
recently finished first. The migration backfills existing finished/verified
rows with COALESCE(verified_at, updated_at).

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_finished_order.py
Uses a temp DB + data dir via VIBE_DB_PATH / VIBE_DATA_DIR so the live
vibe.db and data/ are never touched.
"""

from __future__ import annotations

import os
import sqlite3
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


def make_ticket(ws_id: str, body: str) -> str:
    r = client.post(f"/api/workspaces/{ws_id}/tickets", json={"body": body})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def patch_ticket(tid: str, payload: dict) -> dict:
    r = client.patch(f"/api/manager/tickets/{tid}", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def board_tickets(ws_id: str) -> list[dict]:
    r = client.get(f"/api/workspaces/{ws_id}/board")
    assert r.status_code == 200, r.text
    return r.json()["tickets"]


def test_finished_at_stamped_and_ordered():
    ws_id = seed_workspace()
    tids = [make_ticket(ws_id, f"task {i}") for i in range(3)]
    # Finish them out of creation order, with a gap so timestamps differ.
    for tid in (tids[1], tids[2], tids[0]):
        patch_ticket(tid, {"status": "finished"})
        time.sleep(0.02)

    tickets = {t["id"]: t for t in board_tickets(ws_id)}
    for tid in tids:
        assert tickets[tid]["status"] == "finished"
        assert tickets[tid]["finished_at"], f"finished_at missing on {tid}"

    # Most recently finished first (what the SPA renders).
    ordered = sorted(tids, key=lambda tid: -tickets[tid]["finished_at"])
    assert ordered == [tids[0], tids[2], tids[1]], ordered
    print("ok: finished_at stamped, most-recent-first ordering possible")


def test_refinish_is_idempotent_but_reopen_refreshes():
    ws_id = seed_workspace()
    tid = make_ticket(ws_id, "task")
    first = patch_ticket(tid, {"status": "finished"})["finished_at"]
    assert first
    # Re-PATCHing finished (idempotent manager retry) keeps the timestamp.
    again = patch_ticket(tid, {"status": "finished"})["finished_at"]
    assert again == first, (again, first)
    # Reopen then finish again -> fresh timestamp.
    patch_ticket(tid, {"status": "pending"})
    time.sleep(0.02)
    refreshed = patch_ticket(tid, {"status": "finished"})["finished_at"]
    assert refreshed > first, (refreshed, first)
    print("ok: idempotent re-finish keeps finished_at; re-finish after reopen refreshes it")


def test_migration_backfills_existing_rows():
    old_db = TMP / "old.db"
    conn = sqlite3.connect(old_db)
    # Pre-finished_at schema (verified_at/title already migrated in).
    conn.execute(
        """CREATE TABLE tickets(
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', title TEXT,
            sort_order REAL NOT NULL DEFAULT 0, conversation_id TEXT,
            pr_url TEXT, manager_note TEXT,
            dispatched_entry_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            verified_at REAL)"""
    )
    conn.execute(
        "INSERT INTO tickets(id, workspace_id, status, created_at, updated_at) "
        "VALUES('fin1','ws','finished',100.0,200.0)"
    )
    conn.execute(
        "INSERT INTO tickets(id, workspace_id, status, created_at, updated_at, verified_at) "
        "VALUES('ver1','ws','verified',100.0,300.0,250.0)"
    )
    conn.execute(
        "INSERT INTO tickets(id, workspace_id, status, created_at, updated_at) "
        "VALUES('pen1','ws','pending',100.0,400.0)"
    )
    conn.commit()
    conn.close()

    orig = vibe_app.DB_PATH
    try:
        vibe_app.DB_PATH = old_db
        vibe_app.init_db()
        with vibe_app.db() as c:
            rows = {r["id"]: r["finished_at"] for r in c.execute("SELECT id, finished_at FROM tickets")}
    finally:
        vibe_app.DB_PATH = orig

    assert rows["fin1"] == 200.0, rows  # falls back to updated_at
    assert rows["ver1"] == 250.0, rows  # prefers verified_at
    assert rows["pen1"] is None, rows   # non-finished rows untouched
    print("ok: migration backfills finished_at for existing finished/verified rows")


if __name__ == "__main__":
    test_finished_at_stamped_and_ordered()
    test_refinish_is_idempotent_but_reopen_refreshes()
    test_migration_backfills_existing_rows()
    print("all finished-order tests passed")
