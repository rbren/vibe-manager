"""Unfinished-project indicator tests — plain script, no pytest dependency.

Unfinished means every active board status: pending, in_progress, or
needs_input. Finished and verified cards are terminal and do not contribute to
the number shown beside the workspace picker.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

TMP = Path(tempfile.mkdtemp(prefix="vibe-test-"))
os.environ["VIBE_DB_PATH"] = str(TMP / "vibe.db")
os.environ["VIBE_DATA_DIR"] = str(TMP / "data")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

read_text = Path.read_text


def read_test_keys(path: Path, *args, **kwargs) -> str:
    if path.name in (".session-key", ".automation-key"):
        return "test-key"
    return read_text(path, *args, **kwargs)


with patch.object(Path, "read_text", read_test_keys):
    import app as vibe_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(vibe_app.app)
vibe_app.agent_get = lambda _path: {"workspaceParents": [], "workspaces": []}


def seed_workspace(ws_id: str, name: str) -> None:
    with vibe_app.db() as conn:
        conn.execute(
            "INSERT INTO workspaces(id, path, name, created_at) VALUES(?,?,?,?)",
            (ws_id, f"/tmp/{name}", name, time.time()),
        )


def seed_ticket(ws_id: str, ticket_id: str, status: str) -> None:
    now = time.time()
    with vibe_app.db() as conn:
        conn.execute(
            "INSERT INTO tickets(id, workspace_id, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?)",
            (ticket_id, ws_id, status, now, now),
        )


def test_workspace_list_counts_only_unfinished_cards():
    seed_workspace("alpha", "Alpha")
    seed_workspace("beta", "Beta")
    for i, status in enumerate(
        ("pending", "in_progress", "needs_input", "finished", "verified")
    ):
        seed_ticket("alpha", f"alpha-{i}", status)
    seed_ticket("beta", "beta-finished", "finished")

    response = client.get("/api/workspaces")
    assert response.status_code == 200, response.text
    listed = {ws["id"]: ws for ws in response.json()["selected"]}
    assert listed["alpha"]["unfinished_count"] == 3, listed["alpha"]
    assert listed["beta"]["unfinished_count"] == 0, listed["beta"]
    print("ok: workspace listing counts active statuses and excludes terminal cards")


if __name__ == "__main__":
    test_workspace_list_counts_only_unfinished_cards()
    print("all workspace-indicator tests passed")
