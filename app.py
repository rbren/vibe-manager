"""Vibe — vibecoding work manager.

FastAPI backend serving the SPA, the ticket store, and the manager-facing API.
A per-workspace "manager" automation (see automation/main.py) polls the queue
every minute and drives worker conversations on the agent server.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import tarfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
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


def _agent_settings_payload() -> dict:
    """Active agent settings with encrypted secrets, tools resolved to defaults.

    The canvas UI stores tools=[] and injects the exec set client-side; for
    server-side conversations we need tools=None so the SDK resolves the
    standard exec set (terminal, file_editor, task_tracker).
    """
    r = httpx.get(
        f"{AGENT_SERVER}/api/settings",
        headers={"X-Session-API-Key": SESSION_KEY, "X-Expose-Secrets": "encrypted"},
        timeout=15,
    )
    r.raise_for_status()
    settings = r.json()["agent_settings"]
    settings["tools"] = None
    return settings


@app.post("/api/manager/conversations")
def manager_start_conversation(req: StartConversation):
    """Start a worker/manager conversation (or send a follow-up to an existing one).

    Centralizes agent config so callers (automation script, Manager agent)
    get the active LLM profile + default exec tools without handling secrets.
    """
    headers = {"X-Session-API-Key": SESSION_KEY, "Content-Type": "application/json"}
    message = {"role": "user", "content": [{"text": req.prompt}], "run": True}
    with httpx.Client(timeout=120) as client:
        if req.conversation_id:
            r = client.post(
                f"{AGENT_SERVER}/api/conversations/{req.conversation_id}/events",
                headers=headers, json=message,
            )
            r.raise_for_status()
            return {"id": req.conversation_id, "followup": True,
                    "conversation_url": f"{CANVAS_BASE}/conversations/{req.conversation_id}"}
        # `workspace` tag = the project path (not the worktree path) so the
        # canvas UI groups the conversation under the right workspace.
        tags = {
            "workspace": req.working_dir,
            "viberole": req.role,
            **(req.tags or {}),
        }
        body = {
            "workspace": {"kind": "LocalWorkspace", "working_dir": req.working_dir},
            "worktree": req.worktree,
            "agent_settings": _agent_settings_payload(),
            "secrets_encrypted": True,
            "initial_message": message,
            "max_iterations": req.max_iterations,
            "autotitle": not req.title,
            "tags": tags,
        }
        r = client.post(f"{AGENT_SERVER}/api/conversations", headers=headers, json=body)
        r.raise_for_status()
        conv_id = r.json()["id"]
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
