"""Manager automation status endpoint tests — plain script, no pytest.

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_automation_status.py
Uses a temp DB via VIBE_DB_PATH and a local stub automation backend via
VIBE_AUTOMATION_API so the live vibe.db / automation backend are never touched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="vibe-test-"))
os.environ["VIBE_DB_PATH"] = str(TMP / "vibe.db")
os.environ["VIBE_DATA_DIR"] = str(TMP / "data")

AUTOMATION_ID = "auto-123"


class StubAutomation(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith(f"/api/automation/v1/{AUTOMATION_ID}/runs"):
            body = {
                "runs": [
                    {
                        "id": "run-2",
                        "status": "RUNNING",
                        "error_detail": None,
                        "created_at": "2026-05-21T19:00:00Z",
                        "started_at": "2026-05-21T19:00:10Z",
                        "completed_at": None,
                    },
                    {
                        "id": "run-1",
                        "status": "FAILED",
                        "error_detail": "boom",
                        "created_at": "2026-05-21T18:59:00Z",
                        "started_at": "2026-05-21T18:59:10Z",
                        "completed_at": "2026-05-21T18:59:20Z",
                    },
                ],
                "total": 2,
            }
        elif self.path == f"/api/automation/v1/{AUTOMATION_ID}":
            body = {
                "id": AUTOMATION_ID,
                "enabled": True,
                "last_triggered_at": "2026-05-21T19:00:00Z",
            }
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence
        pass


server = HTTPServer(("127.0.0.1", 0), StubAutomation)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["VIBE_AUTOMATION_API"] = f"http://127.0.0.1:{server.server_port}/api/automation"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as vibe_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(vibe_app.app)


def seed_workspace(automation_id: str | None) -> str:
    ws_id = uuid.uuid4().hex[:12]
    with vibe_app.db() as conn:
        conn.execute(
            "INSERT INTO workspaces(id, path, name, created_at) VALUES(?,?,?,?)",
            (ws_id, f"/tmp/ws-{ws_id}", "testws", time.time()),
        )
        if automation_id:
            conn.execute("UPDATE workspaces SET automation_id=? WHERE id=?", (automation_id, ws_id))
        conn.commit()
    return ws_id


def test_unknown_workspace():
    assert client.get("/api/workspaces/nope/automation").status_code == 404


def test_unconfigured():
    ws_id = seed_workspace(None)
    r = client.get(f"/api/workspaces/{ws_id}/automation")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["configured"] is False and d["automation_id"] is None
    assert d["last_run"] is None and d["run_active"] is False


def test_configured_with_runs():
    ws_id = seed_workspace(AUTOMATION_ID)
    r = client.get(f"/api/workspaces/{ws_id}/automation")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["configured"] is True and d["automation_id"] == AUTOMATION_ID
    assert d["enabled"] is True
    assert d["last_triggered_at"] == "2026-05-21T19:00:00Z"
    assert d["run_active"] is True  # run-2 has no completed_at
    assert d["last_run"]["status"] == "RUNNING"
    assert d["error"] is None
    assert d["manager_conversation"] is None  # no manager_conversation_id seeded


def test_backend_unreachable():
    ws_id = seed_workspace("some-other-automation")
    # stub 404s for unknown ids -> raise_for_status -> httpx.HTTPError branch
    r = client.get(f"/api/workspaces/{ws_id}/automation")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["configured"] is True
    assert d["error"] is not None
    assert d["last_run"] is None


if __name__ == "__main__":
    for fn in (test_unknown_workspace, test_unconfigured, test_configured_with_runs, test_backend_unreachable):
        fn()
        print(f"ok: {fn.__name__}")
    server.shutdown()
    print("all automation-status tests passed")
