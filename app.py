"""Vibe — vibecoding work manager.

FastAPI backend serving the SPA, the ticket store, and the manager-facing API.
A per-workspace "manager" automation (see automation/main.py) polls the queue
every minute and drives worker conversations on the agent server.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import tarfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

log = logging.getLogger("vibe")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("VIBE_DB_PATH", str(ROOT / "vibe.db")))
DATA_DIR = Path(os.environ.get("VIBE_DATA_DIR", str(ROOT / "data")))
ATTACHMENTS_DIR = DATA_DIR / "attachments"
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
AGENT_SERVER = os.environ.get("VIBE_AGENT_SERVER", "http://127.0.0.1:18000")
AUTOMATION_API = os.environ.get("VIBE_AUTOMATION_API", "http://127.0.0.1:18001/api/automation")
VIBE_API = os.environ.get("VIBE_SELF_URL", "http://127.0.0.1:18300")
CANVAS_BASE = os.environ.get("VIBE_CANVAS_BASE", "https://canvas.rbren.io")

SESSION_KEY = (ROOT / ".session-key").read_text().strip()
AUTOMATION_KEY = (ROOT / ".automation-key").read_text().strip()

STATUSES = ["pending", "in_progress", "needs_input", "finished"]
# Terminal state outside the main board; reached only via the verify endpoint.
VERIFIED = "verified"

app = FastAPI(title="Vibe Work Manager")


# --------------------------------------------------------------------------- db

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspaces(
                id TEXT PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                max_concurrent INTEGER NOT NULL DEFAULT 3,
                push_mode TEXT NOT NULL DEFAULT 'pr',
                automation_id TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets(
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                status TEXT NOT NULL DEFAULT 'pending',
                title TEXT,
                sort_order REAL NOT NULL DEFAULT 0,
                conversation_id TEXT,
                pr_url TEXT,
                manager_note TEXT,
                dispatched_entry_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries(
                id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL REFERENCES tickets(id),
                author TEXT NOT NULL DEFAULT 'user',
                body TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attachments(
                id TEXT PRIMARY KEY,
                ticket_id TEXT NOT NULL REFERENCES tickets(id),
                filename TEXT NOT NULL,
                content_type TEXT,
                size INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(workspaces)")}
        if "manager_conversation_id" not in cols:
            conn.execute("ALTER TABLE workspaces ADD COLUMN manager_conversation_id TEXT")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
        if "verified_at" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN verified_at REAL")
        if "title" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN title TEXT")
        if "finished_at" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN finished_at REAL")
            # Backfill: best guess for rows finished before this column existed.
            conn.execute(
                "UPDATE tickets SET finished_at=COALESCE(verified_at, updated_at) "
                "WHERE status IN ('finished', 'verified') AND finished_at IS NULL"
            )


init_db()


# ------------------------------------------------------------------ agent server

def agent_get(path: str, timeout: float = 15.0) -> dict:
    r = httpx.get(
        f"{AGENT_SERVER}{path}",
        headers={"X-Session-API-Key": SESSION_KEY},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


# ------------------------------------------------- live activity (summaries)
#
# For in_progress tickets the backend watches the worker conversation's event
# stream server-side (websocket to the agent server's /sockets/events/<id>)
# and caches the most recent ActionEvent summary. The board payload carries it
# as `latest_action`, so the SPA (which polls the board) shows live activity
# without the session API key ever reaching the browser.

ACTIVITY_IDLE_TTL = 300.0  # stop watching a conversation the board stopped asking about
_activity_lock = threading.Lock()
_activity: dict[str, dict] = {}          # conv_id -> {"summary","tool","timestamp"}
_activity_wanted: dict[str, float] = {}  # conv_id -> last time the board asked
_activity_tasks: dict[str, object] = {}  # conv_id -> asyncio.Task (or True placeholder)
_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def _capture_event_loop() -> None:
    global _loop
    _loop = asyncio.get_running_loop()


def extract_action_summary(event: dict) -> dict | None:
    """Latest-activity info from an agent-server event, or None.

    ActionEvents carry the LLM-predicted `summary` field either on the parsed
    `action` or (more commonly) inside the raw `tool_call.arguments` JSON.
    """
    if event.get("kind") != "ActionEvent":
        return None
    summary = None
    action = event.get("action")
    if isinstance(action, dict):
        summary = action.get("summary")
    if not summary:
        args = (event.get("tool_call") or {}).get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        if isinstance(args, dict):
            summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    return {
        "summary": summary.strip(),
        "tool": event.get("tool_name"),
        "timestamp": event.get("timestamp"),
    }


def get_activity(conv_id: str) -> dict | None:
    with _activity_lock:
        info = _activity.get(conv_id)
        return dict(info) if info else None


def _set_activity(conv_id: str, info: dict) -> None:
    with _activity_lock:
        _activity[conv_id] = info


def _activity_stale(conv_id: str) -> bool:
    with _activity_lock:
        wanted = _activity_wanted.get(conv_id, 0.0)
    return time.time() - wanted > ACTIVITY_IDLE_TTL


async def _seed_latest_summary(conv_id: str) -> None:
    """Prime the cache with the newest ActionEvent summary via the REST API."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            page_id: str | None = None
            for _ in range(5):
                params: dict = {"sort_order": "TIMESTAMP_DESC", "limit": 100}
                if page_id:
                    params["page_id"] = page_id
                r = await client.get(
                    f"{AGENT_SERVER}/api/conversations/{conv_id}/events/search",
                    headers={"X-Session-API-Key": SESSION_KEY},
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
                for event in data.get("items", []):
                    info = extract_action_summary(event)
                    if info:
                        _set_activity(conv_id, info)
                        return
                page_id = data.get("next_page_id")
                if not page_id:
                    return
    except Exception as exc:
        log.warning("activity seed failed for %s: %s", conv_id, exc)


async def _stream_events(conv_id: str) -> None:
    """Hold a websocket to the conversation event stream, caching summaries."""
    ws_base = AGENT_SERVER.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{ws_base}/sockets/events/{conv_id}"
    async with websockets.connect(
        url, additional_headers={"X-Session-API-Key": SESSION_KEY}
    ) as ws:
        while not _activity_stale(conv_id):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                continue
            try:
                info = extract_action_summary(json.loads(raw))
            except ValueError:
                continue
            if info:
                _set_activity(conv_id, info)


async def _watch_conversation(conv_id: str) -> None:
    try:
        await _seed_latest_summary(conv_id)
        while not _activity_stale(conv_id):
            try:
                await _stream_events(conv_id)
            except Exception as exc:
                log.info("activity stream for %s ended: %s", conv_id, exc)
            if not _activity_stale(conv_id):
                await asyncio.sleep(5)
    finally:
        with _activity_lock:
            _activity_tasks.pop(conv_id, None)


def note_activity_interest(conv_ids: list[str]) -> None:
    """Called from board requests: keep watchers alive for these conversations."""
    if not conv_ids or _loop is None:
        return
    now = time.time()
    with _activity_lock:
        missing = []
        for cid in conv_ids:
            _activity_wanted[cid] = now
            if cid not in _activity_tasks:
                _activity_tasks[cid] = True  # placeholder until the task exists
                missing.append(cid)

    def _start(cid: str) -> None:
        task = asyncio.get_running_loop().create_task(_watch_conversation(cid))
        with _activity_lock:
            _activity_tasks[cid] = task

    for cid in missing:
        _loop.call_soon_threadsafe(_start, cid)


# ---------------------------------------------------- conversation LLM model
#
# Each ticket with a conversation shows the model that conversation runs on
# (from the agent server's conversation metadata: agent.llm.model). Fetches
# happen in background threads and results are cached, so the SPA's 5s board
# poll never blocks on — or hammers — the agent server, and the session API
# key stays server-side.

MODEL_CACHE_TTL = 300.0
_model_lock = threading.Lock()
_model_cache: dict[str, dict] = {}  # conv_id -> {"model": str|None, "fetched_at": float}
_model_inflight: set[str] = set()


def extract_conversation_model(meta: dict) -> str | None:
    """The LLM model name from conversation metadata, or None."""
    agent = meta.get("agent")
    llm = agent.get("llm") if isinstance(agent, dict) else None
    model = llm.get("model") if isinstance(llm, dict) else None
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _prime_model_cache(conv_id: str, model: str | None) -> None:
    if not model:
        return
    with _model_lock:
        _model_cache[conv_id] = {"model": model, "fetched_at": time.time()}


def _invalidate_model_cache(conv_id: str) -> None:
    with _model_lock:
        _model_cache.pop(conv_id, None)


def _fetch_model(conv_id: str) -> None:
    model = None
    try:
        meta = agent_get(f"/api/conversations/{conv_id}?include_skills=false", timeout=30)
        model = extract_conversation_model(meta)
    except Exception as exc:
        log.info("model fetch failed for %s: %s", conv_id, exc)
    with _model_lock:
        prev = _model_cache.get(conv_id)
        if model is None and prev and prev.get("model"):
            model = prev["model"]  # keep last known on transient failures
        _model_cache[conv_id] = {"model": model, "fetched_at": time.time()}
        _model_inflight.discard(conv_id)


def get_conversation_model(conv_id: str, sticky: bool = False) -> str | None:
    """Cached model for a conversation; refreshes in the background when stale.

    `sticky` (terminal-status tickets): a known model never expires — the
    conversation won't switch models anymore, so don't re-poll the agent
    server for it.
    """
    now = time.time()
    with _model_lock:
        entry = _model_cache.get(conv_id)
        model = entry["model"] if entry else None
        fresh = entry is not None and now - entry["fetched_at"] < MODEL_CACHE_TTL
        if (sticky and model) or fresh or conv_id in _model_inflight:
            return model
        _model_inflight.add(conv_id)
    threading.Thread(target=_fetch_model, args=(conv_id,), daemon=True).start()
    return model


# --------------------------------------------------------------------- models

class SelectWorkspace(BaseModel):
    path: str


class WorkspaceSettings(BaseModel):
    max_concurrent: int | None = None
    push_mode: str | None = None  # 'pr' | 'main'


class NewTicket(BaseModel):
    body: str


class NewEntry(BaseModel):
    body: str
    author: str = "user"


class Reorder(BaseModel):
    status: str
    ordered_ids: list[str]


class ManagerPatch(BaseModel):
    status: str | None = None
    title: str | None = None
    conversation_id: str | None = None
    pr_url: str | None = None
    manager_note: str | None = None
    dispatched_entry_count: int | None = None
    append_entry: str | None = None  # convenience: manager appends a note entry


class StartConversation(BaseModel):
    working_dir: str
    prompt: str
    worktree: bool = True
    conversation_id: str | None = None  # when set, send follow-up instead of starting new
    title: str | None = None
    max_iterations: int = 500
    role: str = "worker"  # worker | manager — recorded in conversation tags
    tags: dict[str, str] | None = None
    # Agent-server LLM profile name (GET /api/manager/llm-profiles). On create
    # the conversation starts on that model; on follow-up the conversation is
    # switched to it first. None = the server's active default profile.
    llm_profile: str | None = None


# ---------------------------------------------------------------- attachments

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()\[\]-]+")


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip()
    name = _FILENAME_UNSAFE.sub("_", name)[:120].strip("._ ")
    return name or "file"


def attachment_disk_path(att_id: str, filename: str) -> Path:
    return ATTACHMENTS_DIR / att_id / filename


def attachment_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["url"] = f"/api/attachments/{row['id']}"
    # Stable absolute path on this machine — workers in worktrees can read it.
    d["path"] = str(attachment_disk_path(row["id"], row["filename"]))
    return d


# ------------------------------------------------------------------ serializers

def ticket_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    entries = [
        dict(e)
        for e in conn.execute(
            "SELECT id, author, body, created_at FROM entries WHERE ticket_id=? ORDER BY created_at",
            (row["id"],),
        )
    ]
    attachments = [
        attachment_dict(a)
        for a in conn.execute(
            "SELECT id, filename, content_type, size, created_at FROM attachments "
            "WHERE ticket_id=? ORDER BY created_at",
            (row["id"],),
        )
    ]
    d = dict(row)
    d["entries"] = entries
    d["attachments"] = attachments
    d["conversation_url"] = (
        f"{CANVAS_BASE}/conversations/{row['conversation_id']}" if row["conversation_id"] else None
    )
    d["latest_action"] = (
        get_activity(row["conversation_id"])
        if row["status"] == "in_progress" and row["conversation_id"]
        else None
    )
    d["llm_model"] = (
        get_conversation_model(
            row["conversation_id"],
            sticky=row["status"] in ("finished", VERIFIED),
        )
        if row["conversation_id"]
        else None
    )
    return d


def workspace_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# -------------------------------------------------------------------- workspaces

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/workspaces")
def list_workspaces():
    """Available workspaces (from agent-server parents) + already-selected ones."""
    available: list[dict] = []
    try:
        data = agent_get("/api/workspaces")
        parents = data.get("workspaceParents", [])
        for parent in parents:
            ppath = Path(parent["path"])
            if not ppath.is_dir():
                continue
            for child in sorted(ppath.iterdir()):
                if child.is_dir() and not child.name.startswith((".", "_")):
                    available.append(
                        {
                            "path": str(child),
                            "name": child.name,
                            "is_git": (child / ".git").exists(),
                        }
                    )
        for ws in data.get("workspaces", []):
            p = ws.get("path")
            if p and not any(a["path"] == p for a in available):
                available.append({"path": p, "name": ws.get("name") or Path(p).name, "is_git": (Path(p) / ".git").exists()})
    except Exception as exc:  # agent server unreachable — still show selected ones
        log.warning("agent-server workspace listing failed: %s", exc)
    with db() as conn:
        selected = [workspace_dict(r) for r in conn.execute("SELECT * FROM workspaces ORDER BY created_at")]
    return {"available": available, "selected": selected, "canvas_base": CANVAS_BASE}


@app.post("/api/workspaces")
def select_workspace(req: SelectWorkspace):
    path = str(Path(req.path).resolve())
    if not Path(path).is_dir():
        raise HTTPException(400, f"not a directory: {path}")
    ws_id = hashlib.sha1(path.encode()).hexdigest()[:12]
    name = Path(path).name
    with db() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO workspaces(id, path, name, created_at) VALUES(?,?,?,?)",
                (ws_id, path, name, time.time()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    automation_id = ensure_manager_automation(ws_id)
    with db() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    d = workspace_dict(row)
    d["automation_id"] = automation_id
    return d


@app.patch("/api/workspaces/{ws_id}")
def update_workspace(ws_id: str, req: WorkspaceSettings):
    with db() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        if not row:
            raise HTTPException(404, "workspace not found")
        if req.max_concurrent is not None:
            if req.max_concurrent < 1 or req.max_concurrent > 20:
                raise HTTPException(400, "max_concurrent must be 1-20")
            conn.execute("UPDATE workspaces SET max_concurrent=? WHERE id=?", (req.max_concurrent, ws_id))
        if req.push_mode is not None:
            if req.push_mode not in ("pr", "main"):
                raise HTTPException(400, "push_mode must be 'pr' or 'main'")
            conn.execute("UPDATE workspaces SET push_mode=? WHERE id=?", (req.push_mode, ws_id))
        conn.commit()
        return workspace_dict(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone())


@app.get("/api/workspaces/{ws_id}/automation")
def automation_status(ws_id: str):
    """Status of the workspace's manager cron automation (+ manager conversation)."""
    with db() as conn:
        ws_row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if not ws_row:
        raise HTTPException(404, "workspace not found")
    ws = workspace_dict(ws_row)
    automation_id = ws.get("automation_id")
    out: dict = {
        "automation_id": automation_id,
        "configured": bool(automation_id),
        "enabled": None,
        "last_triggered_at": None,
        "run_active": False,
        "last_run": None,
        "last_finished_run": None,
        "consecutive_failures": 0,
        "manager_conversation": None,
        "error": None,
    }
    if not automation_id:
        return out

    def run_slim(run: dict) -> dict:
        return {k: run.get(k) for k in ("status", "error_detail", "created_at", "started_at", "completed_at")}

    try:
        with httpx.Client(base_url=AUTOMATION_API, headers=_automation_headers(), timeout=10) as client:
            r = client.get(f"/v1/{automation_id}")
            r.raise_for_status()
            auto = r.json()
            out["enabled"] = auto.get("enabled")
            out["last_triggered_at"] = auto.get("last_triggered_at")
            runs = client.get(f"/v1/{automation_id}/runs", params={"limit": 10}).json().get("runs", [])
            out["run_active"] = any(run.get("completed_at") is None for run in runs)
            if runs:
                out["last_run"] = run_slim(runs[0])
            # A cron retry in flight must not mask failures: report the outcome
            # of the most recent *finished* run + the current failure streak.
            finished = [run for run in runs if run.get("completed_at") is not None]
            if finished:
                out["last_finished_run"] = run_slim(finished[0])
                streak = 0
                for run in finished:
                    if run.get("status") == "COMPLETED":
                        break
                    streak += 1
                out["consecutive_failures"] = streak
    except httpx.HTTPError as exc:
        out["error"] = f"automation backend unreachable: {exc}"
    conv_id = ws.get("manager_conversation_id")
    if conv_id:
        try:
            conv = agent_get(f"/api/conversations/{conv_id}?include_skills=false", timeout=10)
            conv_status = conv.get("execution_status", "unknown")
        except Exception:
            conv_status = "unknown"
        out["manager_conversation"] = {"id": conv_id, "status": conv_status}
    return out


@app.post("/api/workspaces/{ws_id}/automation/trigger")
def trigger_automation(ws_id: str):
    """Manually dispatch a manager automation run (e.g. from the status badge)."""
    with db() as conn:
        ws_row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if not ws_row:
        raise HTTPException(404, "workspace not found")
    automation_id = workspace_dict(ws_row).get("automation_id")
    if not automation_id:
        raise HTTPException(409, "manager automation not configured for this workspace")
    try:
        with httpx.Client(base_url=AUTOMATION_API, headers=_automation_headers(), timeout=10) as client:
            r = client.post(f"/v1/{automation_id}/dispatch")
            r.raise_for_status()
            run = r.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"automation dispatch failed: {exc}")
    log.info("manually dispatched manager automation %s (run %s)", automation_id, run.get("id"))
    return {
        "dispatched": True,
        "automation_id": automation_id,
        "run": {k: run.get(k) for k in ("id", "status", "created_at")},
    }


# ----------------------------------------------------------------------- board

@app.get("/api/workspaces/{ws_id}/board")
def get_board(ws_id: str):
    with db() as conn:
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        if not ws:
            raise HTTPException(404, "workspace not found")
        tickets = [
            ticket_dict(conn, r)
            for r in conn.execute(
                "SELECT * FROM tickets WHERE workspace_id=? ORDER BY sort_order, created_at",
                (ws_id,),
            )
        ]
    note_activity_interest(
        [t["conversation_id"] for t in tickets
         if t["status"] == "in_progress" and t["conversation_id"]]
    )
    return {"workspace": workspace_dict(ws), "tickets": tickets, "statuses": STATUSES}


@app.post("/api/workspaces/{ws_id}/tickets")
def create_ticket(ws_id: str, req: NewTicket):
    body = req.body.strip()
    if not body:
        raise HTTPException(400, "empty ticket body")
    now = time.time()
    tid = uuid.uuid4().hex[:12]
    with db() as conn:
        if not conn.execute("SELECT 1 FROM workspaces WHERE id=?", (ws_id,)).fetchone():
            raise HTTPException(404, "workspace not found")
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM tickets WHERE workspace_id=? AND status='pending'",
            (ws_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO tickets(id, workspace_id, status, sort_order, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (tid, ws_id, "pending", max_order + 1, now, now),
        )
        conn.execute(
            "INSERT INTO entries(id, ticket_id, author, body, created_at) VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex[:12], tid, "user", body, now),
        )
        conn.commit()
        return ticket_dict(conn, conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone())


@app.post("/api/tickets/{ticket_id}/entries")
def append_entry(ticket_id: str, req: NewEntry):
    body = req.body.strip()
    if not body:
        raise HTTPException(400, "empty entry body")
    if req.author not in ("user", "manager", "agent"):
        raise HTTPException(400, "bad author")
    now = time.time()
    with db() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(404, "ticket not found")
        conn.execute(
            "INSERT INTO entries(id, ticket_id, author, body, created_at) VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex[:12], ticket_id, req.author, body, now),
        )
        if req.author == "user" and row["status"] in ("finished", "needs_input"):
            # A new user request reopens the ticket immediately (bottom of the
            # pending column) instead of waiting for the manager cycle.
            # in_progress tickets are left alone (worker already running);
            # verified is terminal and deliberately not reopened.
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM tickets WHERE workspace_id=? AND status='pending'",
                (row["workspace_id"],),
            ).fetchone()[0]
            conn.execute(
                "UPDATE tickets SET status='pending', sort_order=?, updated_at=? WHERE id=?",
                (max_order + 1, now, ticket_id),
            )
        else:
            conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, ticket_id))
        conn.commit()
        return ticket_dict(conn, conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())


@app.post("/api/tickets/{ticket_id}/attachments")
async def upload_attachment(ticket_id: str, request: Request, filename: str = "file"):
    """Attach a file to a ticket. Raw request body = file bytes (no multipart,
    so no python-multipart dependency); original filename via ?filename=."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty attachment")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, f"attachment too large (max {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB)")
    content_type = request.headers.get("content-type") or "application/octet-stream"
    now = time.time()
    att_id = uuid.uuid4().hex[:12]
    fname = safe_filename(filename)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM tickets WHERE id=?", (ticket_id,)).fetchone():
            raise HTTPException(404, "ticket not found")
        path = attachment_disk_path(att_id, fname)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        conn.execute(
            "INSERT INTO attachments(id, ticket_id, filename, content_type, size, created_at) VALUES(?,?,?,?,?,?)",
            (att_id, ticket_id, fname, content_type, len(data), now),
        )
        conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, ticket_id))
        conn.commit()
        return attachment_dict(
            conn.execute(
                "SELECT id, filename, content_type, size, created_at FROM attachments WHERE id=?",
                (att_id,),
            ).fetchone()
        )


@app.get("/api/attachments/{att_id}")
def download_attachment(att_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    if not row:
        raise HTTPException(404, "attachment not found")
    path = attachment_disk_path(row["id"], row["filename"])
    if not path.is_file():
        raise HTTPException(404, "attachment file missing on disk")
    return FileResponse(
        path,
        media_type=row["content_type"] or "application/octet-stream",
        filename=row["filename"],
        content_disposition_type="inline",
    )


@app.post("/api/tickets/{ticket_id}/verify")
def verify_ticket(ticket_id: str):
    """User marks a finished ticket as verified — it leaves the main board."""
    now = time.time()
    with db() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(404, "ticket not found")
        if row["status"] != "finished":
            raise HTTPException(400, "only finished tickets can be verified")
        conn.execute(
            "UPDATE tickets SET status=?, verified_at=?, updated_at=? WHERE id=?",
            (VERIFIED, now, now, ticket_id),
        )
        conn.commit()
        return ticket_dict(conn, conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())


@app.post("/api/workspaces/{ws_id}/reorder")
def reorder(ws_id: str, req: Reorder):
    if req.status not in STATUSES:
        raise HTTPException(400, "bad status")
    with db() as conn:
        for idx, tid in enumerate(req.ordered_ids):
            conn.execute(
                "UPDATE tickets SET sort_order=? WHERE id=? AND workspace_id=? AND status=?",
                (float(idx), tid, ws_id, req.status),
            )
        conn.commit()
    return {"ok": True}


# ------------------------------------------------------------------ manager API

@app.get("/api/manager/workspaces/{ws_id}/snapshot")
def manager_snapshot(ws_id: str):
    """Full queue snapshot for the manager automation."""
    return get_board(ws_id)


@app.get("/api/manager/agent-credentials")
def manager_agent_credentials():
    """Credentials the Manager agent uses to drive the agent server + vibe API.

    Only reachable through localhost or the basic-auth'd nginx frontend.
    """
    return {
        "agent_server": AGENT_SERVER,
        "session_api_key": SESSION_KEY,
        "vibe_api": VIBE_API,
        "canvas_base": CANVAS_BASE,
    }


def _llm_profiles() -> dict:
    """Available LLM profiles on the agent server (no secrets).

    Shape: {"profiles": [{"name", "model", ...}], "active_profile": <name>}.
    """
    r = httpx.get(
        f"{AGENT_SERVER}/api/profiles",
        headers={"X-Session-API-Key": SESSION_KEY},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


@app.get("/api/manager/llm-profiles")
def manager_llm_profiles():
    """Model choices for the Manager: the agent server's configured profiles."""
    return _llm_profiles()


def _agent_settings_payload(llm_profile: str | None = None) -> dict:
    """Active agent settings with encrypted secrets, tools resolved to defaults.

    The canvas UI stores tools=[] and injects the exec set client-side; for
    server-side conversations we need tools=None so the SDK resolves the
    standard exec set (terminal, file_editor, task_tracker).

    When `llm_profile` is given, the profile's LLM config (fetched with
    encrypted secrets, same round-trip scheme as the settings) replaces
    `agent_settings.llm`, so the worker runs on that model instead of the
    active default.
    """
    headers = {"X-Session-API-Key": SESSION_KEY, "X-Expose-Secrets": "encrypted"}
    r = httpx.get(f"{AGENT_SERVER}/api/settings", headers=headers, timeout=15)
    r.raise_for_status()
    settings = r.json()["agent_settings"]
    settings["tools"] = None
    if llm_profile:
        pr = httpx.get(
            f"{AGENT_SERVER}/api/profiles/{llm_profile}", headers=headers, timeout=15
        )
        if pr.status_code == 404:
            names = [p["name"] for p in _llm_profiles().get("profiles", [])]
            raise HTTPException(
                400, f"unknown llm_profile {llm_profile!r}; available: {names}"
            )
        pr.raise_for_status()
        config = pr.json()["config"]
        # Keep the usage_id the settings LLM carries — profile dumps default it.
        config["usage_id"] = (settings.get("llm") or {}).get("usage_id") or "default"
        settings["llm"] = config
    return settings


def _ensure_workspace_tags(client: httpx.Client, headers: dict, req: StartConversation) -> None:
    """Self-heal `workspace`/`viberole` tags on a reused conversation.

    Conversations created before tagging existed show under "no workspace" in
    the canvas UI; when the manager sends them a follow-up we retro-tag them.
    PATCH replaces ALL tags, so merge with whatever is already set.
    """
    try:
        r = client.get(
            f"{AGENT_SERVER}/api/conversations/{req.conversation_id}?include_skills=false",
            headers=headers,
        )
        r.raise_for_status()
        tags = r.json().get("tags") or {}
        if tags.get("workspace"):
            return
        tags = {"workspace": req.working_dir, "viberole": req.role, **tags}
        client.patch(
            f"{AGENT_SERVER}/api/conversations/{req.conversation_id}",
            headers=headers, json={"tags": tags},
        ).raise_for_status()
    except httpx.HTTPError:
        pass  # tagging is cosmetic; never fail the follow-up over it


WORKTREE_ROOT = Path("/tmp/conversation-worktrees")


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True, text=True, timeout=120,
    )


def _provision_worker_worktree(working_dir: str, conv_id: str) -> dict:
    """Create an isolation git worktree for a worker conversation.

    We create the worktree ourselves (instead of passing `worktree: true` to
    the agent server) so the conversation's `workspace.working_dir` — the
    dedicated workspace option in POST /api/conversations, and what the canvas
    UI sets to the selected workspace directory — stays the PROJECT path
    instead of being rewritten to the per-conversation worktree path.
    Layout and branch naming mirror the agent server's convention.
    """
    project = Path(working_dir).resolve()
    wt_path = WORKTREE_ROOT / conv_id / project.name
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    # Base the worktree on the freshest origin default branch when there is
    # one, falling back to local HEAD (mirrors agent-server behavior).
    start = "HEAD"
    head_ref = _git(project, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if head_ref.returncode == 0:
        _git(project, "fetch", "origin")  # best-effort freshness
        start = head_ref.stdout.strip().removeprefix("refs/remotes/")
    branch = f"openhands/{conv_id}"
    r = _git(project, "worktree", "add", "-b", branch, str(wt_path), start)
    if r.returncode != 0:
        raise HTTPException(502, f"git worktree add failed: {r.stderr.strip()[:300]}")
    return {"path": str(wt_path), "branch": branch, "start": start}


def _worktree_guidance(working_dir: str, wt: dict) -> str:
    return (
        "\n\nThis conversation uses a dedicated git worktree.\n"
        f"- Original workspace: {working_dir}\n"
        f"- Worktree (do ALL file and git work here): {wt['path']}\n"
        f"- You are on branch `{wt['branch']}` (based on {wt['start']}).\n"
        "cd into the worktree before doing anything else; never modify the "
        "original checkout directly."
    )


@app.post("/api/manager/conversations")
def manager_start_conversation(req: StartConversation):
    """Start a worker/manager conversation (or send a follow-up to an existing one).

    Centralizes agent config so callers (automation script, Manager agent)
    get the active LLM profile + default exec tools without handling secrets.
    """
    headers = {"X-Session-API-Key": SESSION_KEY, "Content-Type": "application/json"}
    prompt = req.prompt
    with httpx.Client(timeout=120) as client:
        if req.conversation_id:
            if req.llm_profile:
                r = client.post(
                    f"{AGENT_SERVER}/api/conversations/{req.conversation_id}/switch_profile",
                    headers=headers, json={"profile_name": req.llm_profile},
                )
                if r.status_code in (400, 404):
                    names = [p["name"] for p in _llm_profiles().get("profiles", [])]
                    raise HTTPException(
                        400,
                        f"switch to llm_profile {req.llm_profile!r} failed: "
                        f"{r.json().get('detail')}; available: {names}",
                    )
                r.raise_for_status()
                # The conversation now runs on a different model.
                _invalidate_model_cache(req.conversation_id)
            message = {"role": "user", "content": [{"text": prompt}], "run": True}
            r = client.post(
                f"{AGENT_SERVER}/api/conversations/{req.conversation_id}/events",
                headers=headers, json=message,
            )
            r.raise_for_status()
            _ensure_workspace_tags(client, headers, req)
            return {"id": req.conversation_id, "followup": True,
                    "conversation_url": f"{CANVAS_BASE}/conversations/{req.conversation_id}"}
        # The conversation's `workspace.working_dir` is the dedicated option
        # the agent server / canvas UI use to associate a conversation with a
        # workspace directory — it must be the PROJECT path. Worker isolation
        # worktrees are provisioned here (see _provision_worker_worktree)
        # instead of via `worktree: true`, which would rewrite working_dir.
        conv_id = str(uuid.uuid4())
        # Resolve settings (and validate llm_profile — 400s on an unknown
        # name) BEFORE provisioning the worktree so we never leak one.
        agent_settings = _agent_settings_payload(req.llm_profile)
        if req.worktree:
            wt = _provision_worker_worktree(req.working_dir, conv_id)
            prompt += _worktree_guidance(req.working_dir, wt)
        tags = {
            "workspace": req.working_dir,
            "viberole": req.role,
            **(req.tags or {}),
        }
        body = {
            "workspace": {"kind": "LocalWorkspace", "working_dir": req.working_dir},
            "worktree": False,
            "conversation_id": conv_id,
            "agent_settings": agent_settings,
            "secrets_encrypted": True,
            "initial_message": {"role": "user", "content": [{"text": prompt}], "run": True},
            "max_iterations": req.max_iterations,
            "autotitle": not req.title,
            "tags": tags,
        }
        try:
            r = client.post(f"{AGENT_SERVER}/api/conversations", headers=headers, json=body)
            r.raise_for_status()
        except httpx.HTTPError:
            if req.worktree:  # don't leak the orphaned isolation worktree
                _git(Path(req.working_dir), "worktree", "remove", "--force", wt["path"])
                _git(Path(req.working_dir), "branch", "-D", wt["branch"])
            raise
        conv_id = r.json()["id"]
        _prime_model_cache(conv_id, (agent_settings.get("llm") or {}).get("model"))
        if req.role == "manager":
            with db() as conn:
                conn.execute(
                    "UPDATE workspaces SET manager_conversation_id=? WHERE path=?",
                    (conv_id, req.working_dir),
                )
        if req.title:
            client.patch(
                f"{AGENT_SERVER}/api/conversations/{conv_id}",
                headers=headers, json={"title": req.title},
            )
    return {"id": conv_id, "followup": False,
            "conversation_url": f"{CANVAS_BASE}/conversations/{conv_id}"}


@app.patch("/api/manager/tickets/{ticket_id}")
def manager_patch_ticket(ticket_id: str, req: ManagerPatch):
    now = time.time()
    with db() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(404, "ticket not found")
        updates: dict = {}
        if req.status is not None:
            if req.status not in STATUSES:
                raise HTTPException(400, "bad status")
            updates["status"] = req.status
            if req.status == "finished" and row["status"] != "finished":
                updates["finished_at"] = now
        if req.title is not None:
            updates["title"] = req.title.strip() or None
        if req.conversation_id is not None:
            updates["conversation_id"] = req.conversation_id
        if req.pr_url is not None:
            updates["pr_url"] = req.pr_url
        if req.manager_note is not None:
            updates["manager_note"] = req.manager_note
        if req.dispatched_entry_count is not None:
            updates["dispatched_entry_count"] = req.dispatched_entry_count
        if updates:
            updates["updated_at"] = now
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE tickets SET {sets} WHERE id=?", (*updates.values(), ticket_id))
        if req.append_entry:
            conn.execute(
                "INSERT INTO entries(id, ticket_id, author, body, created_at) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex[:12], ticket_id, "manager", req.append_entry.strip(), now),
            )
            # Absorb trailing manager comments into dispatched_entry_count so
            # the manager's own notes never read as "undispatched entries" and
            # re-summon it (never advances past a user/agent entry).
            authors = [
                r["author"] for r in conn.execute(
                    "SELECT author FROM entries WHERE ticket_id=? ORDER BY created_at",
                    (ticket_id,),
                )
            ]
            dispatched = conn.execute(
                "SELECT dispatched_entry_count FROM tickets WHERE id=?", (ticket_id,)
            ).fetchone()[0]
            advanced = dispatched
            while advanced < len(authors) and authors[advanced] == "manager":
                advanced += 1
            if advanced != dispatched:
                conn.execute(
                    "UPDATE tickets SET dispatched_entry_count=?, updated_at=? WHERE id=?",
                    (advanced, now, ticket_id),
                )
        conn.commit()
        return ticket_dict(conn, conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone())


# ------------------------------------------------------- automation bootstrap

def _automation_headers() -> dict:
    return {"X-Session-API-Key": AUTOMATION_KEY}


def _automation_name(ws_id: str, ws_name: str) -> str:
    return f"Vibe Manager — {ws_name} ({ws_id})"


def build_manager_tarball(ws: dict) -> bytes:
    """Package automation/main.py + a per-workspace config.json into a tar.gz."""
    main_py = (ROOT / "automation" / "main.py").read_bytes()
    config = json.dumps(
        {
            "workspace_id": ws["id"],
            "workspace_path": ws["path"],
            "workspace_name": ws["name"],
            "vibe_api": VIBE_API,
            "agent_server": AGENT_SERVER,
            "canvas_base": CANVAS_BASE,
        },
        indent=2,
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("main.py", main_py), ("config.json", config)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def ensure_manager_automation(ws_id: str) -> str | None:
    """Create (or reuse) the per-workspace manager automation."""
    with db() as conn:
        ws_row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if not ws_row:
        raise HTTPException(404, "workspace not found")
    ws = workspace_dict(ws_row)
    name = _automation_name(ws["id"], ws["name"])

    try:
        with httpx.Client(base_url=AUTOMATION_API, headers=_automation_headers(), timeout=30) as client:
            existing = client.get("/v1", params={"limit": 100}).json().get("automations", [])
            existing_id = next((a["id"] for a in existing if a.get("name") == name), None)

            tarball = build_manager_tarball(ws)
            up = client.post(
                "/v1/uploads",
                params={"name": f"vibe-manager-{ws['id']}", "description": f"Vibe manager for {ws['path']}"},
                headers={"Content-Type": "application/gzip"},
                content=tarball,
            )
            up.raise_for_status()
            tarball_path = up.json()["tarball_path"]

            if existing_id:
                # Refresh the code so script updates propagate on re-selection.
                client.patch(f"/v1/{existing_id}", json={"tarball_path": tarball_path, "enabled": True}).raise_for_status()
                _store_automation_id(ws_id, existing_id)
                log.info("refreshed manager automation %s for workspace %s", existing_id, ws["path"])
                return existing_id

            created = client.post(
                "/v1",
                json={
                    "name": name,
                    "trigger": {"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
                    "tarball_path": tarball_path,
                    "entrypoint": "python3 main.py",
                    "timeout": 300,
                },
            )
            created.raise_for_status()
            automation_id = created.json()["id"]
            _store_automation_id(ws_id, automation_id)
            log.info("created manager automation %s for workspace %s", automation_id, ws["path"])
            return automation_id
    except httpx.HTTPError as exc:
        log.error("automation bootstrap failed for %s: %s", ws["path"], exc)
        return None


def _store_automation_id(ws_id: str, automation_id: str) -> None:
    with db() as conn:
        conn.execute("UPDATE workspaces SET automation_id=? WHERE id=?", (automation_id, ws_id))
        conn.commit()


# ---------------------------------------------------------------------- static

app.mount("/assets", StaticFiles(directory=ROOT / "static"), name="assets")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/workspace/{_workspace:path}")
def workspace_index(_workspace: str):
    """SPA deep-link: /workspace/<name> serves the index; the JS picks the workspace."""
    return FileResponse(ROOT / "static" / "index.html")
