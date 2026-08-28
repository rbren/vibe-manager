"""Manager re-invocation loop guards — plain script, no pytest dependency.

Regression tests for the 2026-08-21 overnight loop (50 no-op manager runs on
the openhands workspace): the manager's own append_entry comments counted as
"undispatched entries", producing a retry-safe signal that the uncapped
stale-retry re-fired every 10 minutes forever.

Run with:
    python tests/test_manager_loop_guard.py

Pure stdlib; automation/main.py is imported from a temp dir with a fake
config.json and a temp HOME so nothing live is touched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="vibe-loop-guard-test-"))
os.environ["HOME"] = str(TMP / "home")
for var in ("AUTOMATION_KV_TOKEN", "AUTOMATION_API_URL"):
    os.environ.pop(var, None)

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
           entry_authors: list[str], dispatched: int) -> dict:
    return {
        "id": tid,
        "status": status,
        "entries": [{"author": a} for a in entry_authors],
        "dispatched_entry_count": dispatched,
        "conversation_id": conv_id,
        "conv_status": conv_status,
        "manager_note": None,
        "attachments": [],
    }


def test_manager_comments_are_not_undispatched():
    # The overnight-loop shape: 1 user entry + 3 manager comments, dispatched=2.
    t = ticket("t1", "finished", "c1", "finished",
               ["user", "manager", "manager", "manager"], dispatched=2)
    assert not m.has_undispatched_entries(t)
    signals, retry_safe = m.compute_signals(WS, [t])
    assert signals == [] and retry_safe == [], (signals, retry_safe)


def test_user_entry_beyond_dispatched_still_signals():
    t = ticket("t2", "pending", "c2", "finished",
               ["user", "manager", "user"], dispatched=2)
    assert m.has_undispatched_entries(t)
    signals, retry_safe = m.compute_signals(WS, [t])
    assert "new-entries:t2" in signals, signals
    assert "new-entries:t2" in retry_safe, retry_safe


def test_user_entry_hidden_behind_manager_comment_still_signals():
    # A user entry after a manager comment must not be masked by it.
    t = ticket("t3", "pending", "c3", "finished",
               ["user", "manager", "user", "manager"], dispatched=1)
    assert m.has_undispatched_entries(t)


def test_authorless_entries_count_as_undispatched():
    # Entries without an author field (older payloads) stay conservative.
    t = ticket("t4", "pending", "c4", "finished", ["user"], dispatched=1)
    t["entries"].append({})
    assert m.has_undispatched_entries(t)


def test_kickoff_on_change_with_signals():
    kick, retries = m.kickoff_decision({}, True, ["new-entries:t"], ["new-entries:t"])
    assert kick and retries == 0, (kick, retries)


def test_no_kickoff_without_signals():
    kick, _ = m.kickoff_decision({}, True, [], [])
    assert not kick


def test_stale_retry_is_capped():
    # Unchanged fingerprint + persistent retry-safe signal: the retry fires
    # MAX_RETRY_ATTEMPTS times, then goes quiet.
    state = {"manager_started_at": time.time() - m.RETRY_INTERVAL_SECONDS - 1}
    kicks = 0
    for _ in range(m.MAX_RETRY_ATTEMPTS + 5):
        kick, retry_count = m.kickoff_decision(state, False, ["sig"], ["sig"])
        if kick:
            kicks += 1
            state["retry_count"] = retry_count + 1
            state["manager_started_at"] = time.time() - m.RETRY_INTERVAL_SECONDS - 1
        else:
            state["retry_count"] = retry_count
    assert kicks == m.MAX_RETRY_ATTEMPTS, kicks


def test_retry_count_resets_on_change():
    state = {
        "retry_count": m.MAX_RETRY_ATTEMPTS,
        "manager_started_at": time.time() - m.RETRY_INTERVAL_SECONDS - 1,
    }
    # Exhausted retries, no change: no kick.
    kick, _ = m.kickoff_decision(state, False, ["sig"], ["sig"])
    assert not kick
    # Board changed: kicks again and the count resets.
    kick, retry_count = m.kickoff_decision(state, True, ["sig"], ["sig"])
    assert kick and retry_count == 0, (kick, retry_count)


def test_no_stale_retry_within_interval():
    state = {"retry_count": 0, "manager_started_at": time.time()}
    kick, _ = m.kickoff_decision(state, False, ["sig"], ["sig"])
    assert not kick


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
