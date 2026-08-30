"""Per-workspace UI settings tests — plain script, no pytest dependency.

Every preference the board exposes (theme, show verified, the default agent
and budget for new requests, push mode) is stored on the workspace row, not in
the browser: a board looks and behaves the same from any browser.

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_workspace_settings.py
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
    with vibe_app.db() as conn:
        conn.execute(
            "INSERT INTO workspaces(id, path, name, created_at) VALUES(?,?,?,?)",
            (ws_id, f"/tmp/ws-{ws_id}", "testws", time.time()),
        )
    return ws_id


def patch(ws_id: str, body: dict):
    return client.patch(f"/api/workspaces/{ws_id}", json=body)


def board_workspace(ws_id: str) -> dict:
    r = client.get(f"/api/workspaces/{ws_id}/board")
    assert r.status_code == 200, r.text
    return r.json()["workspace"]


def test_defaults():
    ws = seed_workspace()
    w = board_workspace(ws)
    assert w["theme"] == vibe_app.DEFAULT_THEME, w
    assert w["show_verified"] is False, w
    assert w["llm_profile"] is None, w
    assert w["max_budget"] == vibe_app.DEFAULT_TICKET_BUDGET, w
    print("ok: a fresh workspace has sane setting defaults")


def test_settings_round_trip():
    ws = seed_workspace()
    r = patch(ws, {"theme": "light", "show_verified": True,
                   "llm_profile": "opus", "max_budget": 25,
                   "push_mode": "main"})
    assert r.status_code == 200, r.text
    w = r.json()
    assert (w["theme"], w["show_verified"], w["llm_profile"], w["max_budget"]) == (
        "light", True, "opus", 25,
    ), w
    # And they survive as workspace state, not per-request state.
    assert board_workspace(ws)["theme"] == "light"
    assert board_workspace(ws)["push_mode"] == "main"
    print("ok: theme/verified/agent/budget persist on the workspace")


def test_settings_are_per_workspace():
    a, b = seed_workspace(), seed_workspace()
    patch(a, {"theme": "light", "max_budget": 3})
    assert board_workspace(b)["theme"] == vibe_app.DEFAULT_THEME
    assert board_workspace(b)["max_budget"] == vibe_app.DEFAULT_TICKET_BUDGET
    print("ok: one workspace's settings leave the others alone")


def test_blank_profile_clears_to_managers_choice():
    ws = seed_workspace()
    patch(ws, {"llm_profile": "opus"})
    assert patch(ws, {"llm_profile": ""}).json()["llm_profile"] is None
    print("ok: an empty agent means manager's choice again")


def test_validation():
    ws = seed_workspace()
    assert patch(ws, {"theme": "neon"}).status_code == 400
    assert patch(ws, {"max_budget": 0}).status_code == 400
    assert patch(ws, {"max_budget": -5}).status_code == 400
    assert board_workspace(ws)["theme"] == vibe_app.DEFAULT_THEME
    print("ok: bad theme/budget rejected, workspace untouched")


def test_new_tickets_inherit_the_workspace_settings():
    ws = seed_workspace()
    patch(ws, {"llm_profile": "opus", "max_budget": 42})
    r = client.post(f"/api/workspaces/{ws}/tickets", json={"body": "do a thing"})
    assert r.status_code == 200, r.text
    t = r.json()
    assert (t["llm_profile"], t["max_budget"]) == ("opus", 42), t
    # An explicit per-request value still wins.
    r = client.post(
        f"/api/workspaces/{ws}/tickets",
        json={"body": "cheaper", "max_budget": 5},
    )
    assert r.json()["max_budget"] == 5, r.text
    print("ok: new tickets inherit the workspace's agent and budget")


if __name__ == "__main__":
    test_defaults()
    test_settings_round_trip()
    test_settings_are_per_workspace()
    test_blank_profile_clears_to_managers_choice()
    test_validation()
    test_new_tickets_inherit_the_workspace_settings()
    print("\nall workspace-settings tests passed")
