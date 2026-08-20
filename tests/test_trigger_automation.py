"""Manual manager-trigger endpoint tests — plain script, no pytest.

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_trigger_automation.py
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
dispatches: list[str] = []


class StubAutomation(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        if self.path == f"/api/automation/v1/{AUTOMATION_ID}/dispatch":
            dispatches.append(self.headers.get("X-Session-API-Key", ""))
            body = {
                "id": "run-99",
                "automation_id": AUTOMATION_ID,
                "status": "PENDING",
                "created_at": "2026-05-21T19:05:00Z",
                "started_at": None,
                "completed_at": None,
            }
            data = json.dumps(body).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

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
    assert client.post("/api/workspaces/nope/automation/trigger").status_code == 404


def test_unconfigured():
    ws_id = seed_workspace(None)
    assert client.post(f"/api/workspaces/{ws_id}/automation/trigger").status_code == 409


def test_dispatch():
    ws_id = seed_workspace(AUTOMATION_ID)
    r = client.post(f"/api/workspaces/{ws_id}/automation/trigger")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dispatched"] is True
    assert d["automation_id"] == AUTOMATION_ID
    assert d["run"] == {"id": "run-99", "status": "PENDING", "created_at": "2026-05-21T19:05:00Z"}
    assert len(dispatches) == 1
    assert dispatches[0] == vibe_app.AUTOMATION_KEY  # auth header forwarded


def test_backend_error():
    ws_id = seed_workspace("some-other-automation")
    # stub 404s for unknown ids -> raise_for_status -> httpx.HTTPError -> 502
    r = client.post(f"/api/workspaces/{ws_id}/automation/trigger")
    assert r.status_code == 502, r.text


if __name__ == "__main__":
    for fn in (test_unknown_workspace, test_unconfigured, test_dispatch, test_backend_error):
        fn()
        print(f"ok: {fn.__name__}")
    server.shutdown()
    print("all trigger-automation tests passed")
