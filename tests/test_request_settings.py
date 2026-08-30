"""New-request settings (agent + budget) — plain script, no pytest.

Run with the service venv (the app half needs fastapi):
    /root/git/vibe-manager/.venv/bin/python tests/test_request_settings.py

Covers the ⚙ settings a user sets on a request: which agent runs it and what
it may spend. The app half checks the ticket columns and the dispatch
override; the automation half checks the same override in the JSON store and
the budget stop that pauses a worker which has spent its cap. Everything runs
against temp dirs — no live service, no live store.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="vibe-reqsettings-test-"))
os.environ["VIBE_DB_PATH"] = str(TMP / "vibe.db")
os.environ["VIBE_DATA_DIR"] = str(TMP / "data")
os.environ["VIBE_STORE_DIR"] = str(TMP / "store")
os.environ["HOME"] = str(TMP / "home")  # sandbox the automation's state store
for _var in ("AUTOMATION_KV_TOKEN", "AUTOMATION_API_URL"):
    os.environ.pop(_var, None)

sys.path.insert(0, str(REPO))

import app as vibe_app  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(vibe_app.app)


def make_workspace() -> str:
    path = TMP / "project"
    path.mkdir(exist_ok=True)
    r = client.post("/api/workspaces", json={"path": str(path)})
    assert r.status_code == 200, r.text
    return r.json()["id"]


WS_ID = make_workspace()


def board_ticket(ticket_id: str) -> dict:
    r = client.get(f"/api/workspaces/{WS_ID}/board")
    assert r.status_code == 200, r.text
    return next(t for t in r.json()["tickets"] if t["id"] == ticket_id)


# ------------------------------------------------------------------- the app


def test_defaults_are_managers_choice_and_ten_dollars():
    r = client.post(f"/api/workspaces/{WS_ID}/tickets", json={"body": "plain request"})
    assert r.status_code == 200, r.text
    t = board_ticket(r.json()["id"])
    assert t["llm_profile"] is None, "no model chosen means the manager picks"
    assert t["max_budget"] == 10.0
    print("ok: a request with no settings defaults to manager's choice and $10")


def test_chosen_agent_and_budget_are_recorded():
    r = client.post(
        f"/api/workspaces/{WS_ID}/tickets",
        json={"body": "spare no expense", "llm_profile": "opus", "max_budget": 42},
    )
    assert r.status_code == 200, r.text
    t = board_ticket(r.json()["id"])
    assert t["llm_profile"] == "opus"
    assert t["max_budget"] == 42.0
    print("ok: the chosen agent and budget land on the ticket")


def test_budget_must_be_positive():
    r = client.post(
        f"/api/workspaces/{WS_ID}/tickets", json={"body": "free lunch", "max_budget": 0},
    )
    assert r.status_code == 400, r.text
    print("ok: a non-positive budget is rejected")


def test_ticket_profile_overrides_the_managers_pick():
    chosen = client.post(
        f"/api/workspaces/{WS_ID}/tickets",
        json={"body": "on opus please", "llm_profile": "opus"},
    ).json()["id"]
    managers = client.post(
        f"/api/workspaces/{WS_ID}/tickets", json={"body": "whatever you think"},
    ).json()["id"]

    assert vibe_app.ticket_llm_profile(chosen) == "opus"
    assert vibe_app.ticket_llm_profile(managers) is None, "manager keeps choosing"
    assert vibe_app.ticket_llm_profile(None) is None, "no ticket, no override"
    try:
        vibe_app.ticket_llm_profile("nosuchticket")
    except HTTPException as e:
        assert e.status_code == 404
    else:
        raise AssertionError("expected 404 for an unknown ticket")
    print("ok: a ticket's own profile overrides what the manager picked")


# ------------------------------------------------------- the automation half


def load_automation():
    mod_dir = TMP / "automation"
    if mod_dir.exists():
        shutil.rmtree(mod_dir)
    mod_dir.mkdir(parents=True)
    for name in ("main.py", "vibestore.py", "vibectl.py"):
        shutil.copy(REPO / "automation" / name, mod_dir / name)
    (mod_dir / "config.json").write_text(json.dumps({
        "workspace_id": "ws-test",
        "workspace_path": str(TMP / "project"),
        "workspace_name": "testws",
        "vibe_api": "http://127.0.0.1:1/",
        "canvas_base": "http://127.0.0.1:1/",
        "agent_server": "http://127.0.0.1:1/",
    }))
    spec = importlib.util.spec_from_file_location("automation_reqsettings", mod_dir / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = load_automation()
vibestore = m.vibestore


def seed_board(tickets: list[dict]) -> None:
    vibestore.write_board("ws-test", {"tickets": tickets})


def store_ticket(tid: str, **fields) -> dict:
    return {
        "id": tid,
        "status": "in_progress",
        "sort_order": 1,
        "conversation_id": "c1",
        "pr_url": None,
        "manager_note": None,
        "dispatched_entry_count": 1,
        "llm_profile": None,
        "max_budget": 10.0,
        "entries": [{"id": "e1", "author": "user", "body": "do it", "created_at": 1.0}],
        "attachments": [],
        **fields,
    }


def test_store_ticket_profile_overrides_the_managers_pick():
    seed_board([
        store_ticket("t-opus", llm_profile="opus"),
        store_ticket("t-free"),
    ])
    assert vibestore.ticket_llm_profile("ws-test", "t-opus") == "opus"
    assert vibestore.ticket_llm_profile("ws-test", "t-free") is None
    assert vibestore.ticket_llm_profile("ws-test", None) is None
    try:
        vibestore.ticket_llm_profile("ws-test", "gone")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for an unknown ticket")
    print("ok: vibectl --ticket resolves the model the user requested")


def test_conversation_spend_sums_every_llm():
    conv = {"stats": {"usage_to_metrics": {
        "default": {"accumulated_cost": 3.5},
        "condenser": {"accumulated_cost": 0.25},
    }}}
    assert m.conversation_spend(conv) == 3.75
    assert m.conversation_spend({}) == 0.0, "no stats yet is not a spend"
    print("ok: conversation spend sums the accumulated cost of every LLM")


def test_budget_stop_pauses_the_worker_once():
    seed_board([store_ticket("t1", max_budget=10.0)])
    paused: list[str] = []
    m.agent = lambda path, method="GET", *a, **kw: paused.append(f"{method} {path}")

    under = [{**store_ticket("t1"), "conv_spend": 4.0, "conv_status": "running"}]
    state: dict = {}
    m.apply_budget_stops(under, state)
    assert paused == [], "a worker inside its budget is left alone"
    assert under[0]["status"] == "in_progress"

    over = [{**store_ticket("t1"), "conv_spend": 10.42, "conv_status": "running"}]
    m.apply_budget_stops(over, state)
    assert paused == ["POST /api/conversations/c1/pause"], paused
    assert over[0]["status"] == "needs_input", "the card parks for the user to decide"
    on_disk = vibestore.get_ticket("ws-test", "t1")
    assert on_disk["status"] == "needs_input"
    assert "10.42" in on_disk["manager_note"] and "10.00" in on_disk["manager_note"]
    assert state["budget_stopped"] == ["c1"]

    # A resumed conversation is not stopped again: the user (or the manager
    # relaying their follow-up) decided the work was worth more.
    again = [{**store_ticket("t1"), "conv_spend": 20.0, "conv_status": "running"}]
    m.apply_budget_stops(again, state)
    assert paused == ["POST /api/conversations/c1/pause"], paused
    assert again[0]["status"] == "in_progress"
    print("ok: a worker over budget is paused once and parked in needs_input")


def test_budget_stop_ignores_finished_and_budgetless_workers():
    seed_board([store_ticket("t1")])
    calls: list[str] = []
    m.agent = lambda path, method="GET", *a, **kw: calls.append(path)

    done = [{**store_ticket("t1"), "conv_spend": 99.0, "conv_status": "finished"}]
    m.apply_budget_stops(done, {})
    assert calls == [], "a worker that already stopped needs no pausing"

    # Tickets created before budgets existed carry none and are never stopped.
    legacy = [{**store_ticket("t1", max_budget=None), "conv_spend": 99.0,
               "conv_status": "running"}]
    m.apply_budget_stops(legacy, {})
    assert calls == [], "a ticket with no budget has no cap to hit"
    print("ok: finished workers and budget-less tickets are left alone")


def test_manager_prompt_explains_ticket_settings():
    ws = {"max_concurrent": 2, "push_mode": "main"}
    tickets = [{**store_ticket("t1", llm_profile="opus", max_budget=25.0),
                "conv_status": "running", "pr_state": None}]
    prompt = m.build_manager_prompt(ws, tickets)
    assert '"requested_model": "opus"' in prompt
    assert '"budget_usd": 25.0' in prompt
    assert "--ticket <ticket_id>" in prompt
    assert "The user's choice wins" in prompt
    print("ok: the manager prompt carries each ticket's requested model and budget")


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("all request settings tests passed")
