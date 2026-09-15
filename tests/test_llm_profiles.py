"""Model-selection (LLM profile) tests — plain script, no pytest.

Run with the service venv:
    python3 tests/test_llm_profiles.py
Stubs the agent server via VIBE_AGENT_SERVER so nothing live is touched.
Covers: the /api/manager/llm-profiles proxy, agent_settings llm injection for
a chosen profile (with usage_id preservation), unknown-profile 400s, and the
model-selection section of the manager prompt in automation/main.py.
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


class StubAgentServer(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/api/profiles":
            body = PROFILES
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
    assert "## Model selection for workers" in prompt
    assert f"{mod.VIBECTL} profiles" in prompt
    assert "high effort" in prompt and "low effort" in prompt
    assert "active default" in prompt and "omit `--profile`" in prompt
    assert "never invent" in prompt.lower()
    assert "user's choice wins" in prompt.lower()
    assert "gpt-" not in prompt and "Anthropic" not in prompt
    assert "--profile" in prompt
    print("ok: manager discovers live profiles without provider-specific tiers")


def test_cli_discovers_arbitrary_and_changing_profiles():
    mod = _load_automation(os.environ["VIBE_AGENT_SERVER"])
    listed = mod.vibestore.llm_profiles()
    assert listed == vibe_app._llm_profiles()
    assert "must-not-leak" not in json.dumps(listed)
    original = json.loads(json.dumps(PROFILES))
    try:
        PROFILES.update(profiles=[{"name": "custom / local alias", "model": "another-provider/model"}],
                        active_profile="custom / local alias")
        result = subprocess.run([sys.executable, mod.VIBECTL, "profiles"],
                                capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == PROFILES
        PROFILES.update(profiles=[], active_profile=None)
        assert mod.vibestore.llm_profiles() == PROFILES
    finally:
        PROFILES.update(original)


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
    test_agent_settings_default_untouched()
    test_agent_settings_profile_injection()
    test_agent_settings_unknown_profile_400()
    test_manager_prompt_discovers_profiles()
    test_cli_discovers_arbitrary_and_changing_profiles()
    test_profile_names_are_encoded_as_path_segments()
    test_manager_prompt_note_style_rule()
    test_manager_prompt_one_conversation_per_ticket()
    print("all llm profile tests passed")
