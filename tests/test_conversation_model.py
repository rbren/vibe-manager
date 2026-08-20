"""Per-ticket LLM model tests — plain script, no pytest dependency.

Every ticket with a worker conversation exposes `llm_model` (the model from
the agent server's conversation metadata, agent.llm.model), cached server-side
so the 5s board poll never hammers the agent server. These tests cover the
metadata extraction and the cache/board serialization; the live fetch itself
is exercised in prod.

Run with the service venv:
    /root/git/vibe-manager/.venv/bin/python tests/test_conversation_model.py
Uses a temp DB + data dir via VIBE_DB_PATH / VIBE_DATA_DIR so the live
vibe.db and data/ are never touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="vibe-test-"))
os.environ["VIBE_DB_PATH"] = str(TMP / "vibe.db")
os.environ["VIBE_DATA_DIR"] = str(TMP / "data")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as vibe_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(vibe_app.app)


def seed_workspace() -> str:
    ws_id = uuid.uuid4().hex[:12]
    now = time.time()
    with vibe_app.db() as conn:
        conn.execute(
            "INSERT INTO workspaces(id, path, name, created_at) VALUES(?,?,?,?)",
            (ws_id, f"/tmp/ws-{ws_id}", "testws", now),
        )
    return ws_id


def make_ticket(ws_id: str, status: str, conversation_id: str | None = None) -> str:
    r = client.post(f"/api/workspaces/{ws_id}/tickets", json={"body": "build the thing"})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    patch: dict = {}
    if status != "pending":
        patch["status"] = status
    if conversation_id:
        patch["conversation_id"] = conversation_id
    if patch:
        r = client.patch(f"/api/manager/tickets/{tid}", json=patch)
        assert r.status_code == 200, r.text
    return tid


# The real fetch hits the agent server; capture it before stubbing so the
# failure-path test can still exercise it with agent_get patched.
REAL_FETCH_MODEL = vibe_app._fetch_model


def stub_fetches(record: list | None = None):
    """Replace the background fetch so tests never hit the agent server."""
    def _stub(conv_id: str) -> None:
        if record is not None:
            record.append(conv_id)
        with vibe_app._model_lock:
            vibe_app._model_inflight.discard(conv_id)
    vibe_app._fetch_model = _stub


stub_fetches()


def conversation_meta(model="anthropic/claude-fable-5") -> dict:
    """A realistic GET /api/conversations/<id> payload (trimmed)."""
    return {
        "id": str(uuid.uuid4()),
        "agent": {
            "kind": "Agent",
            "llm": {"model": model, "usage_id": "default", "api_key": "****"},
            "tools": None,
        },
        "execution_status": "running",
    }


def test_extract_model_from_metadata():
    assert (
        vibe_app.extract_conversation_model(conversation_meta())
        == "anthropic/claude-fable-5"
    )
    assert vibe_app.extract_conversation_model(conversation_meta("  gpt-5  ")) == "gpt-5"
    print("ok: model extracted (and trimmed) from agent.llm.model")


def test_extract_model_handles_missing_fields():
    assert vibe_app.extract_conversation_model({}) is None
    assert vibe_app.extract_conversation_model({"agent": None}) is None
    assert vibe_app.extract_conversation_model({"agent": {"llm": None}}) is None
    assert vibe_app.extract_conversation_model({"agent": {"llm": {}}}) is None
    assert vibe_app.extract_conversation_model({"agent": {"llm": {"model": "   "}}}) is None
    assert vibe_app.extract_conversation_model({"agent": {"llm": {"model": 3}}}) is None
    print("ok: missing/blank/non-string model handled")


def test_board_carries_llm_model_from_cache():
    ws_id = seed_workspace()
    conv_a, conv_b = str(uuid.uuid4()), str(uuid.uuid4())
    t_progress = make_ticket(ws_id, "in_progress", conv_a)
    t_finished = make_ticket(ws_id, "finished", conv_b)
    t_pending = make_ticket(ws_id, "pending")

    vibe_app._prime_model_cache(conv_a, "anthropic/claude-fable-5")
    vibe_app._prime_model_cache(conv_b, "openai/gpt-5")

    r = client.get(f"/api/workspaces/{ws_id}/board")
    assert r.status_code == 200, r.text
    by_id = {t["id"]: t for t in r.json()["tickets"]}
    assert by_id[t_progress]["llm_model"] == "anthropic/claude-fable-5"
    assert by_id[t_finished]["llm_model"] == "openai/gpt-5"
    assert by_id[t_pending]["llm_model"] is None
    print("ok: board exposes llm_model for tickets with conversations")


def test_uncached_conversation_triggers_one_background_fetch():
    ws_id = seed_workspace()
    conv = str(uuid.uuid4())
    tid = make_ticket(ws_id, "in_progress", conv)

    fetched: list = []
    stub_fetches(fetched)
    r = client.get(f"/api/workspaces/{ws_id}/board")
    by_id = {t["id"]: t for t in r.json()["tickets"]}
    assert by_id[tid]["llm_model"] is None  # not known yet, fetch is async
    deadline = time.time() + 2
    while conv not in fetched and time.time() < deadline:
        time.sleep(0.01)
    assert fetched == [conv], fetched
    print("ok: stale cache kicks off exactly one background fetch")


def test_fresh_cache_and_sticky_terminal_skip_refetch():
    ws_id = seed_workspace()
    conv_fresh, conv_done = str(uuid.uuid4()), str(uuid.uuid4())
    make_ticket(ws_id, "in_progress", conv_fresh)
    make_ticket(ws_id, "finished", conv_done)

    vibe_app._prime_model_cache(conv_fresh, "model-a")
    # Terminal-status ticket with a long-expired entry: model is sticky.
    with vibe_app._model_lock:
        vibe_app._model_cache[conv_done] = {"model": "model-b", "fetched_at": 0.0}

    fetched: list = []
    stub_fetches(fetched)
    r = client.get(f"/api/workspaces/{ws_id}/board")
    models = {t["llm_model"] for t in r.json()["tickets"]}
    assert models == {"model-a", "model-b"}, models
    time.sleep(0.05)
    assert fetched == [], fetched
    print("ok: fresh cache and terminal-status tickets don't re-poll the agent server")


def test_switch_invalidation_and_failure_keeps_last_known():
    conv = str(uuid.uuid4())
    vibe_app._prime_model_cache(conv, "model-a")
    vibe_app._invalidate_model_cache(conv)
    with vibe_app._model_lock:
        assert conv not in vibe_app._model_cache

    # A failed refetch must keep the last known model (transient agent-server
    # errors shouldn't blank chips on the board).
    vibe_app._prime_model_cache(conv, "model-a")
    with vibe_app._model_lock:
        prev_fetched = vibe_app._model_cache[conv]["fetched_at"]

    real_agent_get = vibe_app.agent_get
    def boom(*a, **k):
        raise RuntimeError("boom")
    vibe_app.agent_get = boom
    try:
        REAL_FETCH_MODEL(conv)
    finally:
        vibe_app.agent_get = real_agent_get
    with vibe_app._model_lock:
        entry = vibe_app._model_cache[conv]
    assert entry["model"] == "model-a"
    assert entry["fetched_at"] >= prev_fetched
    print("ok: invalidation clears cache; failed refetch keeps last known model")


if __name__ == "__main__":
    test_extract_model_from_metadata()
    test_extract_model_handles_missing_fields()
    test_board_carries_llm_model_from_cache()
    test_uncached_conversation_triggers_one_background_fetch()
    test_fresh_cache_and_sticky_terminal_skip_refetch()
    test_switch_invalidation_and_failure_keeps_last_known()
    print("all conversation-model tests passed")
