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


def stub_fetches(record: list | None = None):
    """Replace the conversation-metadata fetch so tests never hit the agent server."""
    def _stub(conv_id: str) -> None:
        if record is not None:
            record.append(conv_id)
        now = time.time()
        with vibe_app._conv_lock:
            status = (vibe_app._status_cache.get(conv_id) or {}).get("status")
            vibe_app._model_cache[conv_id] = {"model": None, "fetched_at": now}
            vibe_app._status_cache[conv_id] = {"status": status, "fetched_at": now}
            vibe_app._conv_inflight.discard(conv_id)
    vibe_app._fetch_conversation = _stub


stub_fetches()


def prime_status(conv_id: str, status: str) -> None:
    with vibe_app._conv_lock:
        vibe_app._status_cache[conv_id] = {"status": status, "fetched_at": time.time()}


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


def test_board_carries_conversation_status():
    """The SPA needs the worker's execution_status to stop the pulsing dot."""
    ws_id = seed_workspace()
    conv_running, conv_done, conv_other = (str(uuid.uuid4()) for _ in range(3))
    t_running = make_ticket(ws_id, "in_progress", conv_running)
    t_done = make_ticket(ws_id, "in_progress", conv_done)
    t_finished = make_ticket(ws_id, "finished", conv_other)

    prime_status(conv_running, "running")
    prime_status(conv_done, "finished")
    prime_status(conv_other, "finished")

    r = client.get(f"/api/workspaces/{ws_id}/board")
    by_id = {t["id"]: t for t in r.json()["tickets"]}
    assert by_id[t_running]["conversation_status"] == "running"
    # The screenshot case: card still in_progress, worker conversation done.
    assert by_id[t_done]["conversation_status"] == "finished"
    # Only in_progress cards render the indicator, so nothing else carries it.
    assert by_id[t_finished]["conversation_status"] is None
    print("ok: board exposes conversation_status on in_progress tickets")


def test_conversation_status_refreshes_when_stale():
    ws_id = seed_workspace()
    conv = str(uuid.uuid4())
    tid = make_ticket(ws_id, "in_progress", conv)
    with vibe_app._conv_lock:
        vibe_app._status_cache[conv] = {"status": "running", "fetched_at": 0.0}

    fetched: list = []
    stub_fetches(fetched)
    r = client.get(f"/api/workspaces/{ws_id}/board")
    t = next(t for t in r.json()["tickets"] if t["id"] == tid)
    assert t["conversation_status"] == "running"  # last known until the fetch lands
    deadline = time.time() + 2
    while conv not in fetched and time.time() < deadline:
        time.sleep(0.01)
    assert fetched == [conv], fetched
    print("ok: an expired status triggers exactly one background refresh")


if __name__ == "__main__":
    test_extract_summary_from_tool_call_arguments()
    test_extract_summary_prefers_action_field()
    test_extract_ignores_non_action_and_missing_summary()
    test_board_carries_latest_action_for_in_progress_only()
    test_in_progress_without_cache_is_none()
    test_board_carries_conversation_status()
    test_conversation_status_refreshes_when_stale()
    print("all activity summary tests passed")
