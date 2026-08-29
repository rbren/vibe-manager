"""Automation conversation-state tests — plain script, no pytest dependency.

Run with:
    python tests/test_automation_conv_state.py

Covers the deterministic poller logic in automation/main.py around tracked
conversation execution statuses: the agent-resumed signal, fingerprint
sensitivity to conv_status changes, the conv_statuses state map, and its
round-trip through the file-based state store. Pure stdlib; the module is
imported from a temp dir with a fake config.json and a temp HOME so nothing
live is touched.
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
TMP = Path(tempfile.mkdtemp(prefix="vibe-automation-test-"))
os.environ["HOME"] = str(TMP / "home")  # sandbox the file-based state store
for var in ("AUTOMATION_KV_TOKEN", "AUTOMATION_API_URL"):
    os.environ.pop(var, None)  # force the file fallback

mod_dir = TMP / "automation"
mod_dir.mkdir(parents=True)
for _name in ("main.py", "vibestore.py", "vibectl.py"):
    shutil.copy(REPO / "automation" / _name, mod_dir / _name)
(mod_dir / "config.json").write_text(json.dumps({
    "workspace_id": "ws-test",
    "workspace_path": "/tmp/ws-test",
    "workspace_name": "testws",
    "vibe_api": "http://127.0.0.1:1/",
    "canvas_base": "http://127.0.0.1:1/",
    "agent_server": "http://127.0.0.1:1/",
}))

spec = importlib.util.spec_from_file_location("automation_main", mod_dir / "main.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

WS = {"max_concurrent": 2, "push_mode": "main"}


def ticket(tid: str, status: str, conv_id: str | None, conv_status: str | None,
           entries: int = 1, dispatched: int = 1) -> dict:
    return {
        "id": tid,
        "status": status,
        "entries": [{}] * entries,
        "dispatched_entry_count": dispatched,
        "conversation_id": conv_id,
        "conv_status": conv_status,
        "manager_note": None,
        "attachments": [],
    }


def test_agent_resumed_signal():
    # needs_input ticket whose conversation the user manually resumed
    t = ticket("t1", "needs_input", "c1", "running")
    signals, retry_safe = m.compute_signals(WS, [t])
    assert "agent-resumed:t1" in signals, signals
    assert "agent-resumed:t1" in retry_safe, retry_safe

    # same for finished tickets
    signals, _ = m.compute_signals(WS, [ticket("t2", "finished", "c2", "running")])
    assert "agent-resumed:t2" in signals, signals

    # a running in_progress ticket is the normal case — no signal
    signals, retry_safe = m.compute_signals(WS, [ticket("t3", "in_progress", "c3", "running")])
    assert signals == [] and retry_safe == [], (signals, retry_safe)

    # a needs_input ticket with an idle conversation is also normal
    signals, _ = m.compute_signals(WS, [ticket("t4", "needs_input", "c4", "idle")])
    assert "agent-resumed:t4" not in signals, signals


def test_fingerprint_tracks_conv_status():
    idle = ticket("t1", "needs_input", "c1", "idle")
    running = ticket("t1", "needs_input", "c1", "running")
    fp_idle = m.fingerprint(WS, [idle])
    fp_running = m.fingerprint(WS, [running])
    assert fp_idle != fp_running
    assert fp_idle == m.fingerprint(WS, [dict(idle)])  # stable

    # kick condition: manual resume flips the fingerprint AND yields a signal
    signals, _ = m.compute_signals(WS, [running])
    assert signals and fp_running != fp_idle


def test_conv_statuses_map():
    tickets = [
        ticket("t1", "needs_input", "c1", "running"),
        ticket("t2", "in_progress", "c2", "idle"),
        ticket("t3", "pending", None, None),
        ticket("t4", "in_progress", "c4", None),
    ]
    assert m.conv_statuses(tickets) == {"c1": "running", "c2": "idle", "c4": "unknown"}


def test_resumed_agent_counts_toward_concurrency():
    # A manually resumed (running) conversation occupies a concurrency slot,
    # so a pending ticket is not dispatchable at max_concurrent=1.
    ws = {"max_concurrent": 1, "push_mode": "main"}
    tickets = [
        ticket("t1", "needs_input", "c1", "running"),
        ticket("t2", "pending", None, None),
    ]
    signals, _ = m.compute_signals(ws, tickets)
    assert "dispatchable:t2" not in signals, signals
    assert "agent-resumed:t1" in signals, signals


def stub_conversations(statuses: dict[str, str], tags: dict | None = None) -> None:
    """Answer conversation_info() from a dict instead of the agent server."""
    def _info(conv_id: str) -> dict:
        return {
            "status": statuses.get(conv_id, "unknown"),
            "tags": tags if tags is not None else {
                "viberole": "manager", "workspace": m.WORKSPACE_PATH,
            },
            "created_at_ts": 1000.0,
        }
    m.conversation_info = _info


def test_worker_done_signal():
    # The reported bug: the card is still in_progress but its worker
    # conversation has ended — the manager must be summoned to reconcile it.
    for status in sorted(m.TERMINAL_CONV_STATUSES):
        signals, retry_safe = m.compute_signals(WS, [ticket("t1", "in_progress", "c1", status)])
        assert "worker-done:t1" in signals, (status, signals)
        assert "worker-done:t1" in retry_safe, (status, retry_safe)


def test_only_a_running_manager_blocks_a_new_one():
    state = {"manager_conversation_id": "mgr", "manager_started_at": 500.0}
    ws = {"manager_conversation_id": "mgr"}

    stub_conversations({"mgr": "running"})
    assert m.manager_conversation_state(state, ws)["active"]

    # Everything else means the manager is done — including the failure states
    # that used to leave the board frozen behind a dead manager.
    for status in ("error", "stuck", "paused", "idle", "finished", "deleted", "unreachable"):
        stub_conversations({"mgr": status})
        mgr = m.manager_conversation_state(state, ws)
        assert not mgr["active"], status
        assert mgr["status"] == status
        assert mgr["failed"] == (status in ("error", "stuck")), status


def test_manager_state_falls_back_to_the_workspace_row():
    stub_conversations({"row-mgr": "running"})
    mgr = m.manager_conversation_state({}, {"manager_conversation_id": "row-mgr"})
    assert mgr["id"] == "row-mgr" and mgr["active"]
    assert mgr["started_at"] == 1000.0  # conversation's own created_at

    # A row-recorded id that isn't tagged as this workspace's manager is ignored.
    stub_conversations({"row-mgr": "running"}, tags={})
    mgr = m.manager_conversation_state({}, {"manager_conversation_id": "row-mgr"})
    assert mgr["id"] is None and not mgr["active"]

    stub_conversations({})
    assert m.manager_conversation_state({}, {})["id"] is None


def test_state_round_trips_conv_statuses():
    assert not m.kv_available()
    state = {"fingerprint": "abc", "conv_statuses": {"c1": "running", "c2": "idle"}}
    m.save_state(state)
    assert m.load_state() == state


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
