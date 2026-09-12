"""Focused manager prompt policy regressions — pure stdlib.

Run:
    python3 tests/test_manager_prompt_policies.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def load_automation():
    root = Path(tempfile.mkdtemp(prefix="vibe-manager-prompt-test-"))
    automation = root / "automation"
    automation.mkdir()
    for name in ("main.py", "vibestore.py", "vibectl.py"):
        shutil.copy(REPO / "automation" / name, automation / name)
    (automation / "config.json").write_text(json.dumps({
        "workspace_id": "ws-test",
        "workspace_path": "/tmp/ws-test",
        "workspace_name": "testws",
        "agent_server": "http://127.0.0.1:1",
        "canvas_base": "http://127.0.0.1:1",
        "store_dir": str(root / "store"),
    }))
    os.environ["SESSION_API_KEY"] = "test-key"
    os.environ["VIBE_STORE_DIR"] = str(root / "store")
    spec = importlib.util.spec_from_file_location("automation_prompt_policy", automation / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manager_prompt() -> str:
    module = load_automation()
    return module.build_manager_prompt({"max_concurrent": 2, "push_mode": "main"}, [])


def test_report_back_and_information_tickets_wait_for_user():
    prompt = manager_prompt()
    assert "report back" in prompt.lower()
    assert "asks for information" in prompt.lower()
    assert "status needs_input" in prompt
    assert "even in push-to-main mode" in prompt
    assert "do not mark it finished" in prompt.lower()


def test_same_ticket_followups_do_not_replay_history():
    prompt = manager_prompt()
    assert "same ticket's existing conversation already contains its history" in prompt.lower()
    assert "only the new user request" in prompt.lower()
    assert "do not replay" in prompt.lower()
    assert "full prior ticket history" in prompt.lower()
    assert "One conversation per ticket" in prompt


if __name__ == "__main__":
    test_report_back_and_information_tickets_wait_for_user()
    test_same_ticket_followups_do_not_replay_history()
    print("all manager prompt policy tests passed")
