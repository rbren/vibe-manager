"""'Talk to the manager' chat tests — plain script, no pytest.

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_manager_chat.py
Stubs the agent server via VIBE_AGENT_SERVER and uses a temp DB via
VIBE_DB_PATH, so neither the live board nor real conversations are touched.

Covers: the pre-loaded manager skill, conversation creation through the same
path the manager uses (workspace field + tags, no worktree, and NOT recorded as
the workspace's dispatching manager), sending a message, and replaying the chat
from the conversation's events.
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
from urllib.parse import parse_qs, urlparse

TMP = Path(tempfile.mkdtemp(prefix="vibe-chat-test-"))
os.environ["VIBE_DB_PATH"] = str(TMP / "vibe.db")
os.environ["VIBE_DATA_DIR"] = str(TMP / "data")

CONV_ID = "11111111-2222-3333-4444-555555555555"
SETTINGS = {"agent_settings": {"llm": {"model": "anthropic/claude-fable-5"}, "tools": []}}

created: list[dict] = []   # POST /api/conversations bodies
follow_ups: list[dict] = []  # POST /api/conversations/<id>/events bodies
events: list[dict] = []    # what the stub replays for the chat window


def message_event(event_id: str, role: str, text: str, ts: str) -> dict:
    return {
        "id": event_id,
        "kind": "MessageEvent",
        "timestamp": ts,
        "source": "user" if role == "user" else "agent",
        "llm_message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


class StubAgentServer(BaseHTTPRequestHandler):
    def _json(self, body: dict, status: int = 200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/api/settings":
            self._json(json.loads(json.dumps(SETTINGS)))
        elif url.path == f"/api/conversations/{CONV_ID}/events/search":
            q = parse_qs(url.query)
            since = q.get("timestamp__gte", [None])[0]
            items = [e for e in events if since is None or e["timestamp"] >= since]
            self._json({"items": items, "next_page_id": None})
        elif url.path == f"/api/conversations/{CONV_ID}":
            self._json({"id": CONV_ID, "execution_status": "running",
                        "agent": {"llm": {"model": "anthropic/claude-fable-5"}}})
        else:
            self._json({"detail": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/api/conversations":
            created.append(self._body())
            self._json({"id": CONV_ID})
        elif url.path == f"/api/conversations/{CONV_ID}/events":
            follow_ups.append(self._body())
            self._json({"ok": True})
        else:
            self._json({"detail": "not found"}, 404)

    def do_PATCH(self):  # noqa: N802
        self._json({"ok": True})

    def log_message(self, *args):  # silence
        pass


server = HTTPServer(("127.0.0.1", 0), StubAgentServer)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["VIBE_AGENT_SERVER"] = f"http://127.0.0.1:{server.server_port}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as vibe_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(vibe_app.app)


def seed_workspace() -> tuple[str, str]:
    """A workspace row with its own project directory (paths are unique)."""
    ws_id = uuid.uuid4().hex[:12]
    path = TMP / f"project-{ws_id}"
    path.mkdir()
    with vibe_app.db() as conn:
        conn.execute(
            "INSERT INTO workspaces(id, path, name, created_at) VALUES(?,?,?,?)",
            (ws_id, str(path), path.name, time.time()),
        )
        conn.commit()
    return ws_id, str(path)


def test_skill_prompt():
    ws = {"id": "ws123", "path": "/root/git/foo", "name": "foo", "push_mode": "main"}
    skill = vibe_app.manager_chat_skill(ws)
    assert skill.startswith(vibe_app.MANAGER_CHAT_MARKER)
    # (a) records requests in AGENTS.md under a Manager section
    assert "AGENTS.md" in skill and "## Manager" in skill
    # (b) knows how to read the board and the conversations on it
    assert "/api/manager/workspaces/ws123/snapshot" in skill
    assert "/api/workspaces/ws123/automation" in skill
    assert "/api/manager/agent-credentials" in skill
    assert "execution_status" in skill
    assert "/root/git/foo" in skill
    # dispatching stays with the cron manager
    assert "do NOT dispatch workers" in skill


def test_start_chat():
    ws_id, ws_path = seed_workspace()
    created.clear()
    r = client.post(f"/api/workspaces/{ws_id}/manager-chat")
    assert r.status_code == 200, r.text
    assert r.json()["conversation_id"] == CONV_ID
    assert r.json()["conversation_url"].endswith(f"/conversations/{CONV_ID}")

    body = created[0]
    # The workspace association must stay on the PROJECT dir (AGENTS.md).
    assert body["workspace"] == {"kind": "LocalWorkspace", "working_dir": ws_path}
    assert body["worktree"] is False
    assert body["tags"] == {"workspace": ws_path, "viberole": vibe_app.MANAGER_CHAT_ROLE}
    assert body["initial_message"]["content"][0]["text"].startswith(
        vibe_app.MANAGER_CHAT_MARKER
    )
    assert body["agent_settings"]["tools"] is None

    # The chat is not the dispatching manager: the badge and the automation's
    # overlap guard must keep tracking the cron manager only.
    with vibe_app.db() as conn:
        row = conn.execute(
            "SELECT manager_conversation_id FROM workspaces WHERE id=?", (ws_id,)
        ).fetchone()
    assert row["manager_conversation_id"] is None


def test_start_chat_unknown_workspace():
    assert client.post("/api/workspaces/nope/manager-chat").status_code == 404


def test_send_message():
    ws_id, _ = seed_workspace()
    follow_ups.clear()
    r = client.post(
        f"/api/workspaces/{ws_id}/manager-chat/{CONV_ID}/messages",
        json={"body": "  please prioritise the login bug  "},
    )
    assert r.status_code == 200, r.text
    assert follow_ups[0]["content"][0]["text"] == "please prioritise the login bug"
    assert follow_ups[0]["run"] is True

    assert client.post(
        f"/api/workspaces/{ws_id}/manager-chat/{CONV_ID}/messages", json={"body": " "}
    ).status_code == 400


def test_messages():
    ws_id, ws_path = seed_workspace()
    events[:] = [
        message_event("e1", "user", vibe_app.manager_chat_skill(
            {"id": ws_id, "path": ws_path, "name": "project", "push_mode": "main"}),
            "2026-05-21T10:00:00"),
        {"id": "e2", "kind": "ActionEvent", "timestamp": "2026-05-21T10:00:01",
         "tool_name": "terminal",
         "tool_call": {"arguments": json.dumps({"summary": "Reading AGENTS.md"})}},
        message_event("e3", "assistant", "3 queued, 1 agent working.", "2026-05-21T10:00:02"),
        message_event("e4", "user", "prioritise the login bug", "2026-05-21T10:01:00"),
    ]
    r = client.get(f"/api/workspaces/{ws_id}/manager-chat/{CONV_ID}/messages")
    assert r.status_code == 200, r.text
    d = r.json()
    # The pre-loaded skill is not a chat turn the user typed.
    assert [(m["role"], m["text"]) for m in d["messages"]] == [
        ("assistant", "3 queued, 1 agent working."),
        ("user", "prioritise the login bug"),
    ]
    assert d["latest_action"]["summary"] == "Reading AGENTS.md"
    assert d["cursor"] == "2026-05-21T10:01:00"

    # `after` is exclusive: polling with the cursor returns only what is new.
    r2 = client.get(
        f"/api/workspaces/{ws_id}/manager-chat/{CONV_ID}/messages",
        params={"after": d["cursor"]},
    )
    assert r2.json()["messages"] == []
    assert r2.json()["cursor"] == d["cursor"]

    events.append(message_event("e5", "assistant", "Recorded it.", "2026-05-21T10:02:00"))
    r3 = client.get(
        f"/api/workspaces/{ws_id}/manager-chat/{CONV_ID}/messages",
        params={"after": d["cursor"]},
    )
    assert [m["text"] for m in r3.json()["messages"]] == ["Recorded it."]


def test_messages_unknown_workspace():
    assert client.get(f"/api/workspaces/nope/manager-chat/{CONV_ID}/messages").status_code == 404


if __name__ == "__main__":
    for fn in (
        test_skill_prompt,
        test_start_chat,
        test_start_chat_unknown_workspace,
        test_send_message,
        test_messages,
        test_messages_unknown_workspace,
    ):
        fn()
        print(f"ok: {fn.__name__}")
    server.shutdown()
    print("all manager-chat tests passed")
