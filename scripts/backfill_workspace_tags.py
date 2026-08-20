#!/usr/bin/env python3
"""Backfill `workspace`/`viberole` tags on conversations referenced by vibe.db.

The canvas UI groups conversations by `selected_workspace`, falling back to
the `workspace` conversation tag; conversations created before app.py tagged
them show under "no workspace". This retro-tags every ticket conversation and
manager conversation for ALL workspaces in the DB (vibe-manager, dj-station,
...). Idempotent; stdlib-only. Run from the repo root:

    python3 scripts/backfill_workspace_tags.py
"""
import json
import os
import sqlite3
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_SERVER = os.environ.get("VIBE_AGENT_SERVER", "http://127.0.0.1:18000")
DB_PATH = os.environ.get("VIBE_DB_PATH", str(ROOT / "vibe.db"))
SESSION_KEY = (ROOT / ".session-key").read_text().strip()


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{AGENT_SERVER}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Session-API-Key": SESSION_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    ws_paths = {r[0]: r[1] for r in conn.execute("SELECT id, path FROM workspaces")}
    targets: dict[str, tuple[str, str]] = {}  # conv_id -> (workspace path, role)
    for conv_id, ws_id in conn.execute(
        "SELECT conversation_id, workspace_id FROM tickets WHERE conversation_id IS NOT NULL"
    ):
        targets[conv_id] = (ws_paths[ws_id], "worker")
    for ws_id, conv_id in conn.execute(
        "SELECT id, manager_conversation_id FROM workspaces "
        "WHERE manager_conversation_id IS NOT NULL"
    ):
        targets[conv_id] = (ws_paths[ws_id], "manager")

    for conv_id, (path, role) in targets.items():
        try:
            tags = api(f"/api/conversations/{conv_id}?include_skills=false").get("tags") or {}
        except Exception as e:
            print(f"{conv_id}: SKIP ({e})")
            continue
        if tags.get("workspace"):
            print(f"{conv_id}: ok ({tags['workspace']})")
            continue
        merged = {"workspace": path, "viberole": role, **tags}
        api(f"/api/conversations/{conv_id}", "PATCH", {"tags": merged})
        print(f"{conv_id}: tagged workspace={path} viberole={role}")


if __name__ == "__main__":
    main()
