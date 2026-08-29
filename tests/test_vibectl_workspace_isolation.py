#!/usr/bin/env python3
"""Each workspace's manager CLI resolves its OWN workspace.

Every workspace's automation runs once a minute and re-installs the CLI the
manager agent drives. When all of them shared one config.json, whichever cron
ran last decided which board `vibectl.py snapshot|patch|dispatch` acted on —
so a manager could read another project's board, and patches against its own
tickets failed with "ticket not found".

Pure stdlib. Run with `python tests/test_vibectl_workspace_isolation.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation"))

TMP = tempfile.mkdtemp(prefix="vibe-cli-")
os.environ["VIBE_STORE_DIR"] = TMP

import vibestore  # noqa: E402 - VIBE_STORE_DIR must be set first

failures: list[str] = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL: {msg}")


def ticket(tid: str) -> dict:
    return {
        "id": tid, "status": "pending", "title": None, "sort_order": 1,
        "conversation_id": None, "pr_url": None, "manager_note": None,
        "dispatched_entry_count": 0, "created_at": 1.0, "updated_at": 1.0,
        "finished_at": None, "verified_at": None,
        "entries": [{"id": "e" + tid, "author": "user", "body": tid, "created_at": 1.0}],
        "attachments": [],
    }


def run(cli: str, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, cli, *args], capture_output=True, text=True,
        # The manager's shell does not inherit the automation run's env: the
        # CLI has to know its workspace without VIBE_WORKSPACE_ID.
        env={"PATH": os.environ["PATH"], "HOME": TMP},
    )
    out = proc.stdout or proc.stderr
    return {"code": proc.returncode, "json": json.loads(out) if out.strip() else {}}


vibestore.write_index({"workspaces": [
    {"id": "wsaaa", "path": "/git/alpha", "name": "alpha", "max_concurrent": 2,
     "push_mode": "main", "automation_id": None, "manager_conversation_id": None},
    {"id": "wsbbb", "path": "/git/beta", "name": "beta", "max_concurrent": 2,
     "push_mode": "main", "automation_id": None, "manager_conversation_id": None},
]})
vibestore.write_board("wsaaa", {"tickets": [ticket("alpha1")]})
vibestore.write_board("wsbbb", {"tickets": [ticket("beta1")]})

print("two workspaces installed in a row keep separate CLIs")
cli_a = vibestore.install_cli("wsaaa", "/git/alpha")
cli_b = vibestore.install_cli("wsbbb", "/git/beta")
check(cli_a != cli_b, "each workspace gets its own CLI path")

snap_a = run(cli_a, "snapshot")
snap_b = run(cli_b, "snapshot")
check(snap_a["code"] == 0 and snap_b["code"] == 0, "both CLIs run")
check(
    [t["id"] for t in snap_a["json"].get("tickets", [])] == ["alpha1"],
    f"alpha's CLI reads alpha's board (got {snap_a['json'].get('workspace')})",
)
check(
    [t["id"] for t in snap_b["json"].get("tickets", [])] == ["beta1"],
    f"beta's CLI reads beta's board (got {snap_b['json'].get('workspace')})",
)

print("re-installing one workspace does not repoint another's CLI")
vibestore.install_cli("wsbbb", "/git/beta")
snap_a = run(cli_a, "snapshot")
check(
    [t["id"] for t in snap_a["json"].get("tickets", [])] == ["alpha1"],
    "alpha's CLI still reads alpha's board after beta re-installed",
)
patched = run(cli_a, "patch", "alpha1", "--status", "in_progress")
check(patched["code"] == 0, f"alpha's CLI can patch its own ticket ({patched['json']})")
check(
    vibestore.read_board("wsaaa")["tickets"][0]["status"] == "in_progress",
    "the patch landed on alpha's board",
)
check(
    vibestore.read_board("wsbbb")["tickets"][0]["status"] == "pending",
    "beta's board is untouched",
)

print("a CLI without a workspace refuses to guess one")
legacy = Path(TMP) / "bin" / "config.json"
check(not legacy.exists(), "no shared config is left for a CLI to fall back on")
loose = Path(TMP) / "bin" / "vibectl.py"
if loose.exists():
    check(run(str(loose), "snapshot")["code"] != 0, "the un-scoped CLI errors instead of guessing")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all vibectl workspace isolation checks passed")
