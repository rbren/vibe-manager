"""Manager append_entry must not create "undispatched entries" — plain script.

Regression test for the 2026-08-21 overnight loop: manager status comments
appended via PATCH append_entry pushed len(entries) past
dispatched_entry_count, which the automation read as a new-entries signal and
kept re-summoning the manager. The PATCH endpoint now absorbs trailing
manager-authored entries into dispatched_entry_count (never advancing past a
user/agent entry).

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_dispatched_count_absorbs_manager_notes.py
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


def patch(tid: str, **body) -> dict:
    r = client.patch(f"/api/manager/tickets/{tid}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def user_entry(tid: str) -> dict:
    r = client.post(f"/api/tickets/{tid}/entries", json={"body": "one more thing", "author": "user"})
    assert r.status_code == 200, r.text
    return r.json()


def test_append_entry_absorbed_when_fully_dispatched():
    # Manager dispatched the single user entry, then leaves status comments:
    # each append_entry must ride along in dispatched_entry_count.
    ws = seed_workspace()
    tid = make_ticket(ws)
    patch(tid, status="in_progress", conversation_id="c1", dispatched_entry_count=1)
    t = patch(tid, status="finished", append_entry="Worker finished")
    assert len(t["entries"]) == 2, t["entries"]
    assert t["dispatched_entry_count"] == 2, t
    t = patch(tid, append_entry="Follow-up comment")
    assert t["dispatched_entry_count"] == 3, t


def test_append_entry_never_swallows_user_entry():
    # A pending user entry beyond dispatched_entry_count must stay visible
    # even when the manager comments after it (e.g. a needs_input question).
    ws = seed_workspace()
    tid = make_ticket(ws)
    patch(tid, status="in_progress", conversation_id="c1", dispatched_entry_count=1)
    user_entry(tid)  # entries: [user, user], dispatched=1
    t = patch(tid, status="needs_input", append_entry="What color should it be?")
    assert len(t["entries"]) == 3, t["entries"]
    assert t["dispatched_entry_count"] == 1, t


def test_append_entry_without_prior_dispatch():
    # Manager comments before dispatching anything: the user's original entry
    # (index 0) must not be absorbed.
    ws = seed_workspace()
    tid = make_ticket(ws)
    t = patch(tid, manager_note="queued behind t0", append_entry="Deferred")
    assert len(t["entries"]) == 2, t["entries"]
    assert t["dispatched_entry_count"] == 0, t


def test_explicit_count_plus_append_entry():
    # Manager bumps dispatched_entry_count and appends in the same PATCH:
    # the new comment is absorbed on top of the explicit value.
    ws = seed_workspace()
    tid = make_ticket(ws)
    user_entry(tid)  # entries: [user, user]
    t = patch(tid, status="in_progress", conversation_id="c1",
              dispatched_entry_count=2, append_entry="Follow-up relayed")
    assert len(t["entries"]) == 3, t["entries"]
    assert t["dispatched_entry_count"] == 3, t


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
