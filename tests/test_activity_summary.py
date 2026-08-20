"""Live action-summary tests — plain script, no pytest dependency.

In-progress tickets expose `latest_action` (the worker conversation's most
recent ActionEvent summary, cached server-side from the agent-server event
websocket). These tests cover the summary extraction from raw event JSON and
the board serialization; the websocket relay itself is exercised in prod.

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_activity_summary.py
Uses a temp DB + data dir via VIBE_DB_PATH / VIBE_DATA_DIR so the live
vibe.db and data/ are never touched.
"""

from __future__ import annotations

import json
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


def make_ticket(ws_id: str, status: str, conversation_id: str | None = None) -> str:
    r = client.post(f"/api/workspaces/{ws_id}/tickets", json={"body": "build the thing"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    patch: dict = {}
    if status != "pending":
        patch["status"] = status
    if conversation_id:
        patch["conversation_id"] = conversation_id
    if patch:
        r = client.patch(f"/api/manager/tickets/{tid}", json=patch)
        assert r.status_code == 200, r.text
    return tid


def action_event(**overrides) -> dict:
    """A realistic agent-server ActionEvent (summary in tool_call.arguments)."""
    event = {
        "kind": "ActionEvent",
        "id": "evt-1",
        "timestamp": "2026-05-21T18:15:28.755360",
        "source": "agent",
        "tool_name": "terminal",
        "action": {"command": "ls", "kind": "ExecuteBashAction"},
        "tool_call": {
            "id": "toolu_123",
            "name": "terminal",
            "arguments": json.dumps(
                {"command": "ls", "security_risk": "LOW", "summary": "List repo files"}
            ),
        },
    }
    event.update(overrides)
    return event


def test_extract_summary_from_tool_call_arguments():
    info = vibe_app.extract_action_summary(action_event())
    assert info == {
        "summary": "List repo files",
        "tool": "terminal",
        "timestamp": "2026-05-21T18:15:28.755360",
    }, info
    print("ok: summary extracted from tool_call.arguments")


def test_extract_summary_prefers_action_field():
    ev = action_event(action={"command": "ls", "summary": "  From action field  "})
    info = vibe_app.extract_action_summary(ev)
    assert info and info["summary"] == "From action field", info
    print("ok: action.summary preferred and trimmed")


def test_extract_ignores_non_action_and_missing_summary():
    assert vibe_app.extract_action_summary({"kind": "ObservationEvent"}) is None
    assert vibe_app.extract_action_summary(
        action_event(tool_call={"arguments": json.dumps({"command": "ls"})})
    ) is None
    assert vibe_app.extract_action_summary(
        action_event(tool_call={"arguments": "not-json{"})
    ) is None
    assert vibe_app.extract_action_summary(
        action_event(tool_call={"arguments": json.dumps({"summary": "   "})})
    ) is None
    assert vibe_app.extract_action_summary(action_event(tool_call=None, action=None)) is None
    print("ok: non-actions / missing / blank / bad-json summaries ignored")


def test_board_carries_latest_action_for_in_progress_only():
    ws_id = seed_workspace()
    conv_a = str(uuid.uuid4())
    conv_b = str(uuid.uuid4())
    t_progress = make_ticket(ws_id, "in_progress", conv_a)
    t_finished = make_ticket(ws_id, "finished", conv_b)
    t_pending = make_ticket(ws_id, "pending")

    info = vibe_app.extract_action_summary(action_event())
    vibe_app._set_activity(conv_a, info)
    vibe_app._set_activity(conv_b, {"summary": "stale", "tool": "x", "timestamp": None})

    r = client.get(f"/api/workspaces/{ws_id}/board")
    assert r.status_code == 200, r.text
    by_id = {t["id"]: t for t in r.json()["tickets"]}
    assert by_id[t_progress]["latest_action"]["summary"] == "List repo files"
    assert by_id[t_progress]["latest_action"]["tool"] == "terminal"
    # Only in_progress tickets surface activity; others always None.
    assert by_id[t_finished]["latest_action"] is None
    assert by_id[t_pending]["latest_action"] is None
    print("ok: board exposes latest_action only on in_progress tickets")


def test_in_progress_without_cache_is_none():
    ws_id = seed_workspace()
    tid = make_ticket(ws_id, "in_progress", str(uuid.uuid4()))
    r = client.get(f"/api/workspaces/{ws_id}/board")
    t = next(t for t in r.json()["tickets"] if t["id"] == tid)
    assert t["latest_action"] is None
    print("ok: uncached in_progress ticket has latest_action=None")


if __name__ == "__main__":
    test_extract_summary_from_tool_call_arguments()
    test_extract_summary_prefers_action_field()
    test_extract_ignores_non_action_and_missing_summary()
    test_board_carries_latest_action_for_in_progress_only()
    test_in_progress_without_cache_is_none()
    print("all activity summary tests passed")
