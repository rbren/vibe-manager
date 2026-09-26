"""Distinct agent/LLM profile tests — plain script, no pytest.

Run with the service venv:
    python3 tests/test_llm_profiles.py
Stubs the agent server via VIBE_AGENT_SERVER so nothing live is touched.
Covers separate discovery, agent_profile_id launches, LLM settings/switching,
ticket precedence, compatibility aliases and manager guidance.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote

TMP = Path(tempfile.mkdtemp(prefix="vibe-llmprof-test-"))
os.environ["VIBE_DB_PATH"] = str(TMP / "vibe.db")
os.environ["VIBE_DATA_DIR"] = str(TMP / "data")

PROFILES = {
    "profiles": [
        {"name": "gpt-6-astra", "model": "openai/gpt-6-astra", "api_key_set": True, "api_key": "must-not-leak", "config": {"secret": "must-not-leak"}},
        {"name": "gpt-5.6-sol", "model": "openai/gpt-5.6-sol", "api_key_set": True},
        {"name": "gpt-5.6-terra", "model": "openai/gpt-5.6-terra", "api_key_set": True},
        {"name": "legacy-anthropic", "model": "anthropic/claude-opus-5", "api_key_set": True},
    ],
    "active_profile": "gpt-5.6-sol",
}
CODEX_PROFILE_ID = "c2394b3d-cdc2-4550-a238-510c8bff6011"
OPENHANDS_PROFILE_ID = "6070d578-5c17-47e3-ad29-50b510fe885d"
AGENT_PROFILES = {
    "profiles": [
        {"id": CODEX_PROFILE_ID, "name": "codex", "agent_kind": "acp", "revision": 1,
         "llm_profile_ref": None, "mcp_server_refs": None},
        {"id": OPENHANDS_PROFILE_ID, "name": "openhands-custom", "agent_kind": "openhands",
         "revision": 2, "llm_profile_ref": "gpt-5.6-sol", "mcp_server_refs": []},
    ],
    "active_agent_profile_id": CODEX_PROFILE_ID,
}
SOL_CONFIG = {
    "model": "openai/gpt-5.6-sol",
    "api_key": "gAAAAA-encrypted",
    "base_url": None,
    "usage_id": "default",
}
SETTINGS = {
    "agent_settings": {
        "llm": {"model": "openai/gpt-5.6-sol", "usage_id": "agent",
                "api_key": "gAAAAA-settings"},
        "tools": [],
    }
}
AGENT_REQUESTS = []


class StubAgentServer(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/api/profiles":
            body = PROFILES
        elif self.path == "/api/agent-profiles":
            body = AGENT_PROFILES
        elif self.path == "/api/profiles/gpt-5.6-sol":
            body = {"name": "gpt-5.6-sol", "config": dict(SOL_CONFIG)}
        elif self.path == "/api/profiles/" + quote("custom alias #?", safe=""):
            body = {"name": "custom alias #?", "config": {**SOL_CONFIG, "model": "other-provider/custom"}}
        elif self.path.startswith("/api/profiles/"):
            self.send_response(404)
            self.end_headers()
            return
        elif self.path == "/api/settings":
            body = json.loads(json.dumps(SETTINGS))  # deep copy per request
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

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        AGENT_REQUESTS.append((self.path, body))
        if self.path == "/api/conversations":
            response = {"id": body["conversation_id"]}
        elif self.path.endswith("/switch_profile") or self.path.endswith("/events"):
            response = {"ok": True}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence
        pass


server = HTTPServer(("127.0.0.1", 0), StubAgentServer)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["VIBE_AGENT_SERVER"] = f"http://127.0.0.1:{server.server_port}"

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import app as vibe_app  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(vibe_app.app)


def test_llm_profiles_endpoint_proxies_agent_server():
    r = client.get("/api/manager/llm-profiles")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["active_profile"] == "gpt-5.6-sol"
    assert [p["name"] for p in d["profiles"]] == [
        "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "legacy-anthropic",
    ]
    assert "must-not-leak" not in json.dumps(d)
    assert "gAAAAA" not in json.dumps(d)  # never leaks secrets
    print("ok: /api/manager/llm-profiles proxies the agent server profile list")


def test_agent_profiles_endpoint_proxies_agent_server():
    r = client.get("/api/manager/agent-profiles")
    assert r.status_code == 200, r.text
    assert r.json() == AGENT_PROFILES
    print("ok: /api/manager/agent-profiles proxies distinct agent choices")


def test_agent_settings_default_untouched():
    s = vibe_app._agent_settings_payload()
    assert s["llm"]["model"] == "openai/gpt-5.6-sol"
    assert s["llm"]["usage_id"] == "agent"
    assert s["tools"] is None
    print("ok: no llm_profile keeps the active settings llm")


def test_agent_settings_profile_injection():
    s = vibe_app._agent_settings_payload("gpt-5.6-sol")
    assert s["llm"]["model"] == "openai/gpt-5.6-sol"
    assert s["llm"]["api_key"] == "gAAAAA-encrypted"  # profile's own (encrypted) key
    assert s["llm"]["usage_id"] == "agent"  # preserved from settings, not the dump
    assert s["tools"] is None
    print("ok: llm_profile injects the profile llm config, preserving usage_id")


def test_agent_settings_unknown_profile_400():
    try:
        vibe_app._agent_settings_payload("nope")
    except HTTPException as e:
        assert e.status_code == 400
        assert "gpt-6-astra" in str(e.detail) and "gpt-5.6-terra" in str(e.detail)
        print("ok: unknown llm_profile raises 400 listing available profiles")
        return
    raise AssertionError("expected HTTPException for unknown profile")


def _load_automation(agent_server: str):
    mod_dir = TMP / "automation"
    if mod_dir.exists():
        shutil.rmtree(mod_dir)
    mod_dir.mkdir(parents=True)
    # main.py imports vibestore from its own directory and installs the CLI
    # next to it, as it does when the tarball is unpacked on the agent server.
    for name in ("main.py", "vibestore.py", "vibectl.py"):
        shutil.copy(REPO / "automation" / name, mod_dir / name)
    # The profile list comes from the agent server, not the old service.
    os.environ["AGENT_SERVER_URL"] = agent_server
    os.environ["SESSION_API_KEY"] = "test-key"
    # Importing main.py installs the workspace's CLI; keep that out of the
    # real store under $HOME.
    os.environ["VIBE_STORE_DIR"] = str(TMP / "store")
    (mod_dir / "config.json").write_text(json.dumps({
        "workspace_id": "ws-test",
        "workspace_path": "/tmp/ws-test",
        "workspace_name": "testws",
        "vibe_api": agent_server,
        "canvas_base": "http://127.0.0.1:1/",
        "agent_server": "http://127.0.0.1:1/",
    }))
    spec = importlib.util.spec_from_file_location(
        f"automation_main_{abs(hash(agent_server))}", mod_dir / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_manager_prompt_discovers_profiles():
    mod = _load_automation("http://127.0.0.1:1")
    ws = {"max_concurrent": 2, "push_mode": "main"}
    prompt = mod.build_manager_prompt(ws, [])
    assert "## Profile selection for workers" in prompt
    assert f"{mod.VIBECTL} llm-profiles" in prompt
    assert f"{mod.VIBECTL} agent-profiles" in prompt
    assert "high effort" in prompt and "low effort" in prompt
    assert "launch-only" in prompt
    assert "never invent" in prompt.lower()
    assert "user's choice wins" in prompt.lower()
    assert "gpt-" not in prompt and "Anthropic" not in prompt
    assert "--agent-profile" in prompt and "--llm-profile" in prompt and "--profile" in prompt
    print("ok: manager distinguishes live agent and LLM profile catalogs")


def test_cli_discovers_arbitrary_and_changing_profiles():
    mod = _load_automation(os.environ["VIBE_AGENT_SERVER"])
    listed = mod.vibestore.llm_profiles()
    assert listed == vibe_app._llm_profiles()
    assert "must-not-leak" not in json.dumps(listed)
    assert mod.vibestore.agent_profiles() == AGENT_PROFILES

    for command in ("llm-profiles", "profiles"):
        result = subprocess.run([sys.executable, mod.VIBECTL, command],
                                capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == listed
    result = subprocess.run([sys.executable, mod.VIBECTL, "agent-profiles"],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == AGENT_PROFILES

    original = json.loads(json.dumps(AGENT_PROFILES))
    try:
        AGENT_PROFILES.update(
            profiles=[{"id": OPENHANDS_PROFILE_ID, "name": "nightly-agent",
                       "agent_kind": "openhands", "revision": 9,
                       "llm_profile_ref": None, "mcp_server_refs": None}],
            active_agent_profile_id=OPENHANDS_PROFILE_ID,
        )
        result = subprocess.run([sys.executable, mod.VIBECTL, "agent-profiles"],
                                capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == AGENT_PROFILES
    finally:
        AGENT_PROFILES.update(original)


def test_cli_dispatch_keeps_agent_and_llm_profiles_distinct():
    mod = _load_automation(os.environ["VIBE_AGENT_SERVER"])
    mod.vibestore.write_board("ws-test", {"tickets": [
        {"id": "ticket-choice", "llm_profile": "gpt-5.6-sol"},
    ]})

    def dispatch(*extra, ok=True):
        before = len(AGENT_REQUESTS)
        result = subprocess.run(
            [sys.executable, mod.VIBECTL, "dispatch", "--prompt", "do it",
             "--no-worktree", *extra], capture_output=True, text=True,
        )
        if not ok:
            assert result.returncode != 0
            assert len(AGENT_REQUESTS) == before
            return result.stderr
        assert result.returncode == 0, result.stderr
        path, body = AGENT_REQUESTS[-1]
        assert path == "/api/conversations"
        return body

    body = dispatch("--agent-profile", "codex")
    assert body["agent_profile_id"] == CODEX_PROFILE_ID
    assert "agent_settings" not in body and "secrets_encrypted" not in body

    body = dispatch("--llm-profile", "custom alias #?")
    assert body["agent_settings"]["llm"]["model"] == "other-provider/custom"
    assert "agent_profile_id" not in body
    body = dispatch("--profile", "custom alias #?")
    assert body["agent_settings"]["llm"]["model"] == "other-provider/custom"

    body = dispatch("--agent-profile", "codex", "--ticket", "ticket-choice")
    assert body["agent_settings"]["llm"]["model"] == "openai/gpt-5.6-sol"
    assert "agent_profile_id" not in body
    body = dispatch()
    assert body["agent_settings"]["llm"]["model"] == SETTINGS["agent_settings"]["llm"]["model"]

    error = dispatch("--agent-profile", "missing-agent", ok=False)
    assert "unknown agent profile" in error and "codex" in error
    error = dispatch("--agent-profile", "codex", "--llm-profile", "gpt-5.6-sol", ok=False)
    assert "not both" in error


def test_manager_api_keeps_agent_and_llm_profiles_distinct():
    AGENT_REQUESTS.clear()
    response = client.post("/api/manager/conversations", json={
        "working_dir": str(TMP), "prompt": "agent", "worktree": False,
        "agent_profile_id": CODEX_PROFILE_ID,
    })
    assert response.status_code == 200, response.text
    _, body = AGENT_REQUESTS[-1]
    assert body["agent_profile_id"] == CODEX_PROFILE_ID
    assert "agent_settings" not in body

    response = client.post("/api/manager/conversations", json={
        "working_dir": str(TMP), "prompt": "llm", "worktree": False,
        "llm_profile": "gpt-5.6-sol",
    })
    assert response.status_code == 200, response.text
    _, body = AGENT_REQUESTS[-1]
    assert body["agent_settings"]["llm"]["model"] == "openai/gpt-5.6-sol"
    assert "agent_profile_id" not in body

    AGENT_REQUESTS.clear()
    response = client.post("/api/manager/conversations", json={
        "working_dir": str(TMP), "prompt": "invalid", "worktree": False,
        "conversation_id": "00000000-0000-0000-0000-000000000001",
        "agent_profile_id": CODEX_PROFILE_ID,
    })
    assert response.status_code == 400 and "launch-only" in response.text
    assert not AGENT_REQUESTS

    response = client.post("/api/manager/conversations", json={
        "working_dir": str(TMP), "prompt": "switch", "worktree": False,
        "conversation_id": "00000000-0000-0000-0000-000000000001",
        "llm_profile": "gpt-5.6-sol",
    })
    assert response.status_code == 200, response.text
    assert AGENT_REQUESTS[0][0].endswith("/switch_profile")
    assert AGENT_REQUESTS[0][1] == {"profile_name": "gpt-5.6-sol"}


def test_cli_followup_switches_only_llm_profiles():
    mod = _load_automation(os.environ["VIBE_AGENT_SERVER"])
    mod.vibestore.write_board("ws-test", {"tickets": [
        {"id": "ticket-choice", "llm_profile": "gpt-5.6-sol"},
    ]})

    def followup(*extra, ok=True):
        AGENT_REQUESTS.clear()
        result = subprocess.run(
            [sys.executable, mod.VIBECTL, "followup", "00000000-0000-0000-0000-000000000001",
             "--prompt", "continue", *extra], capture_output=True, text=True,
        )
        if not ok:
            assert result.returncode != 0 and not AGENT_REQUESTS
            return result.stderr
        assert result.returncode == 0, result.stderr
        return list(AGENT_REQUESTS)

    requests = followup("--llm-profile", "custom alias #?")
    assert requests[0][0].endswith("/switch_profile")
    assert requests[0][1] == {"profile_name": "custom alias #?"}
    assert requests[1][0].endswith("/events")
    requests = followup("--profile", "gpt-5.6-sol")
    assert requests[0][1] == {"profile_name": "gpt-5.6-sol"}
    requests = followup("--ticket", "ticket-choice")
    assert requests[0][1] == {"profile_name": "gpt-5.6-sol"}
    requests = followup()
    assert len(requests) == 1 and requests[0][0].endswith("/events")
    error = followup("--agent-profile", "codex", ok=False)
    assert "launch-only" in error


def test_profile_names_are_encoded_as_path_segments():
    mod = _load_automation(os.environ["VIBE_AGENT_SERVER"])
    assert vibe_app._agent_settings_payload("custom alias #?")["llm"]["model"] == "other-provider/custom"
    assert mod.vibestore.agent_settings_payload("custom alias #?")["llm"]["model"] == "other-provider/custom"


def test_manager_prompt_note_style_rule():
    """Ticket c40ab0776313: card notes must be terse status-only text."""
    mod = _load_automation("http://127.0.0.1:1")
    ws = {"max_concurrent": 2, "push_mode": "main"}
    prompt = mod.build_manager_prompt(ws, [])
    assert "Note style rule" in prompt
    assert "STATUS ONLY" in prompt
    assert "Worker dispatched" in prompt  # canonical example survives edits
    assert "deferral" in prompt.lower()  # deferral-reason exception intact
    print("ok: manager prompt enforces status-only note style with deferral exception")


def test_manager_prompt_one_conversation_per_ticket():
    """New tickets never get grafted onto a finished ticket's conversation."""
    mod = _load_automation("http://127.0.0.1:1")
    ws = {"max_concurrent": 2, "push_mode": "main"}
    prompt = mod.build_manager_prompt(ws, [])
    assert "One conversation per ticket" in prompt
    assert "never graft a new ticket onto another ticket's conversation" in prompt
    assert "finished/verified it is retired" in prompt
    assert "Reuse old conversations when sensible" not in prompt  # retired guidance
    assert "SAME ticket still reuse its own conversation" in prompt
    print("ok: manager prompt retires finished conversations, fresh one per ticket")


if __name__ == "__main__":
    test_llm_profiles_endpoint_proxies_agent_server()
    test_agent_profiles_endpoint_proxies_agent_server()
    test_agent_settings_default_untouched()
    test_agent_settings_profile_injection()
    test_agent_settings_unknown_profile_400()
    test_manager_prompt_discovers_profiles()
    test_cli_discovers_arbitrary_and_changing_profiles()
    test_cli_dispatch_keeps_agent_and_llm_profiles_distinct()
    test_manager_api_keeps_agent_and_llm_profiles_distinct()
    test_cli_followup_switches_only_llm_profiles()
    test_profile_names_are_encoded_as_path_segments()
    test_manager_prompt_note_style_rule()
    test_manager_prompt_one_conversation_per_ticket()
    print("all agent and LLM profile tests passed")
