"""Vibe Manager automation — runs every minute per workspace.

Deterministic phase (pure stdlib, no LLM):
  1. Pull the board snapshot from the vibe API.
  2. Enrich with live conversation execution statuses (agent server) and
     PR states (GitHub API).
  3. Apply mechanical transitions: PR open -> needs_input, PR merged -> finished.
  4. Fingerprint the enriched state; compare with the KV store; compute
     actionable "signals" (new entries, dispatchable tickets, finished workers).

Only when the fingerprint changed AND something is actionable does it kick off
the Manager agent conversation, which does the smart work: dispatching /
reusing worker conversations (always in git worktrees), serializing
conflicting tickets, updating card statuses, and maintaining AGENTS.md.
The manager runs asynchronously; subsequent cycles skip while it is running.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = json.loads((Path(__file__).parent / "config.json").read_text())
WORKSPACE_ID = CONFIG["workspace_id"]
WORKSPACE_PATH = CONFIG["workspace_path"]
WORKSPACE_NAME = CONFIG["workspace_name"]
VIBE_API = CONFIG["vibe_api"].rstrip("/")
CANVAS_BASE = CONFIG["canvas_base"].rstrip("/")

AGENT_SERVER = os.environ.get("AGENT_SERVER_URL", CONFIG["agent_server"]).rstrip("/")
SESSION_KEY = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")

_KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
_KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")
_STATE_KEY = "state"

MANAGER_STALE_SECONDS = 45 * 60  # give up on a manager conversation after this
RETRY_INTERVAL_SECONDS = 10 * 60  # re-kick manager if signals persist without board change
TERMINAL_CONV_STATUSES = {"finished", "idle", "error", "stuck", "deleted", "paused"}


# ------------------------------------------------------------------- http utils

def _request(url: str, method: str = "GET", data: dict | None = None,
             headers: dict | None = None, timeout: int = 30):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def vibe(path: str, method: str = "GET", data: dict | None = None):
    return _request(f"{VIBE_API}{path}", method, data)


def agent(path: str, method: str = "GET", data: dict | None = None, timeout: int = 60):
    return _request(
        f"{AGENT_SERVER}{path}", method, data,
        headers={"X-Session-API-Key": SESSION_KEY}, timeout=timeout,
    )


def fire_callback(status: str = "COMPLETED", error: str | None = None) -> None:
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    try:
        _request(url, "POST", body, headers={
            "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
        })
    except Exception as exc:  # noqa: BLE001
        print(f"callback error (non-fatal): {exc}")


# --------------------------------------------------------------------- kv store

def kv_available() -> bool:
    return bool(_KV_TOKEN and _KV_BASE)


def _kv(path: str, method: str = "GET", data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{path}", data=body, method=method,
        headers={"Authorization": f"Bearer {_KV_TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def _state_file() -> Path:
    d = Path.home() / ".openhands" / "vibe-manager-state"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{WORKSPACE_ID}.json"


def load_state() -> dict:
    if kv_available():
        try:
            return _kv(_STATE_KEY).get("value") or {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            raise
    f = _state_file()
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(state: dict) -> None:
    if kv_available():
        _kv(_STATE_KEY, "PUT", state)
    else:
        _state_file().write_text(json.dumps(state, indent=2))


# ------------------------------------------------------------- status gathering

def get_secret(name: str) -> str:
    try:
        req = urllib.request.Request(
            f"{AGENT_SERVER}/api/settings/secrets/{name}",
            headers={"X-Session-API-Key": SESSION_KEY},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode().strip().strip('"')
    except Exception:  # noqa: BLE001
        return ""


def conversation_info(conv_id: str) -> tuple[str, dict]:
    """Return (execution_status, tags) for a conversation."""
    try:
        d = agent(f"/api/conversations/{conv_id}?include_skills=false")
        return d.get("execution_status", "unknown"), d.get("tags") or {}
    except urllib.error.HTTPError as exc:
        return ("deleted" if exc.code == 404 else f"error_{exc.code}"), {}
    except Exception:  # noqa: BLE001
        return "unreachable", {}


def conversation_status(conv_id: str) -> str:
    return conversation_info(conv_id)[0]


def find_running_manager(state: dict, ws: dict) -> str | None:
    """Return the id of a still-running manager conversation for this workspace.

    Checks the KV-tracked id first, then the id recorded on the workspace row
    (survives KV state loss). Tags verify it really is this workspace's manager.
    """
    candidates = []
    if state.get("manager_conversation_id"):
        candidates.append(state["manager_conversation_id"])
    row_conv = ws.get("manager_conversation_id")
    if row_conv and row_conv not in candidates:
        candidates.append(row_conv)
    for conv_id in candidates:
        status, tags = conversation_info(conv_id)
        if status != "running":
            continue
        # The KV-tracked id is trusted; a row-recorded id must carry our tags.
        if conv_id == state.get("manager_conversation_id") or (
            tags.get("viberole") == "manager" and tags.get("workspace") == WORKSPACE_PATH
        ):
            return conv_id
    return None


_PR_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def pr_state(pr_url: str, token: str) -> str:
    """Return 'open' | 'merged' | 'closed' | 'unknown'."""
    m = _PR_RE.search(pr_url or "")
    if not m:
        return "unknown"
    owner, repo, num = m.groups()
    try:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "vibe-manager"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        d = _request(f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}", headers=headers)
        if d.get("merged") or d.get("merged_at"):
            return "merged"
        return d.get("state", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def snapshot() -> dict:
    return vibe(f"/api/manager/workspaces/{WORKSPACE_ID}/snapshot")


def enrich(board: dict) -> tuple[dict, list[dict]]:
    """Return (workspace, enriched tickets)."""
    ws = board["workspace"]
    gh_token = None
    tickets = []
    for t in board["tickets"]:
        conv_status = conversation_status(t["conversation_id"]) if t.get("conversation_id") else None
        prs = None
        if t.get("pr_url") and t["status"] != "finished":
            if gh_token is None:
                gh_token = get_secret("GITHUB_PERSONAL_ACCESS_TOKEN")
            prs = pr_state(t["pr_url"], gh_token)
        t["conv_status"] = conv_status
        t["pr_state"] = prs
        tickets.append(t)
    return ws, tickets


def apply_mechanical_transitions(tickets: list[dict]) -> None:
    """PR open -> needs_input, PR merged -> finished. Deterministic, no LLM."""
    for t in tickets:
        prs = t.get("pr_state")
        if not prs:
            continue
        new_status = None
        note = None
        if prs == "merged" and t["status"] != "finished":
            new_status, note = "finished", "PR merged — ticket finished."
        elif prs == "open" and t["status"] not in ("needs_input", "finished"):
            # Only park the card once the worker is done pushing commits.
            if (t.get("conv_status") or "") in TERMINAL_CONV_STATUSES or not t.get("conversation_id"):
                new_status, note = "needs_input", "PR is open and awaiting review."
        if new_status:
            print(f"mechanical: ticket {t['id']} {t['status']} -> {new_status} (pr {prs})")
            vibe(f"/api/manager/tickets/{t['id']}", "PATCH", {"status": new_status, "manager_note": note})
            t["status"] = new_status


def fingerprint(ws: dict, tickets: list[dict]) -> str:
    parts = [f"settings:{ws['max_concurrent']}:{ws['push_mode']}"]
    for t in sorted(tickets, key=lambda x: x["id"]):
        parts.append(
            ":".join(str(x) for x in (
                t["id"], t["status"], len(t["entries"]), t.get("dispatched_entry_count", 0),
                t.get("conversation_id"), t.get("conv_status"), t.get("pr_url"), t.get("pr_state"),
            ))
        )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def compute_signals(ws: dict, tickets: list[dict]) -> tuple[list[str], list[str]]:
    """Actionable conditions that justify invoking the Manager agent.

    Returns (signals, retry_safe). `signals` gate kickoff on a board change;
    `retry_safe` additionally allow the slow-cadence retry. One-shot signals
    (e.g. a user appending to a deliberately deferred ticket) are excluded
    from retry_safe so a standing manager decision doesn't re-fire forever.

    Contract with the manager: deliberately deferring a pending ticket
    requires setting a manager_note, which suppresses re-kicks.
    """
    signals: list[str] = []
    retry_safe: list[str] = []
    running = sum(
        1 for t in tickets
        if t.get("conversation_id") and t.get("conv_status") == "running"
    )
    for t in tickets:
        undispatched = len(t["entries"]) > t.get("dispatched_entry_count", 0)
        if undispatched:
            sig = f"new-entries:{t['id']}"
            signals.append(sig)
            if t.get("conversation_id"):
                retry_safe.append(sig)  # relayable follow-up; safe to retry
        if (
            t["status"] == "pending"
            and not t.get("conversation_id")
            and not t.get("manager_note")
            and running < ws["max_concurrent"]
        ):
            sig = f"dispatchable:{t['id']}"
            signals.append(sig)
            retry_safe.append(sig)
        if (
            t["status"] == "in_progress"
            and t.get("conversation_id")
            and (t.get("conv_status") or "") in TERMINAL_CONV_STATUSES
        ):
            sig = f"worker-done:{t['id']}"
            signals.append(sig)
            retry_safe.append(sig)
    return signals, retry_safe


# ------------------------------------------------------------- manager kickoff

def build_manager_prompt(ws: dict, tickets: list[dict]) -> str:
    board_json = json.dumps(
        [
            {
                "id": t["id"],
                "status": t["status"],
                "priority_rank": t["sort_order"],
                "conversation_id": t.get("conversation_id"),
                "conversation_status": t.get("conv_status"),
                "pr_url": t.get("pr_url"),
                "pr_state": t.get("pr_state"),
                "manager_note": t.get("manager_note"),
                "dispatched_entry_count": t.get("dispatched_entry_count", 0),
                "entries": [
                    {"author": e["author"], "body": e["body"], "created_at": e["created_at"]}
                    for e in t["entries"]
                ],
            }
            for t in tickets
        ],
        indent=1,
    )
    push_instructions = (
        "Push mode is **pull request**: workers must create a feature branch in their worktree, commit, "
        "push the branch, and open a PR against the default branch (gh CLI is available; "
        "GITHUB_PERSONAL_ACCESS_TOKEN is auto-injected into their terminal). When a worker finishes and "
        "reports a PR URL, PATCH the ticket with pr_url and set status to needs_input. "
        "(PR merged -> finished is handled automatically; don't merge PRs yourself.)"
        if ws["push_mode"] == "pr"
        else
        "Push mode is **push to main**: workers must commit in their worktree and push directly to the "
        "default branch (main). When a worker finishes and its commits are on main, set the ticket status "
        "to finished. Verify with `git log origin/main` if unsure."
    )
    return f"""You are the **Vibe Manager** for the project at `{WORKSPACE_PATH}` (workspace id `{WORKSPACE_ID}`).
You manage a kanban queue of vibecoding tickets and a pool of worker agent conversations. You do NOT write feature code yourself — you dispatch and coordinate workers.

## Current board (tickets with full history)
```json
{board_json}
```

## Settings
- max concurrent worker conversations: **{ws['max_concurrent']}**
- {push_instructions}

## APIs at your disposal (use curl from the terminal)

Vibe ticket API (no auth needed from this machine): base `{VIBE_API}`
- `PATCH {VIBE_API}/api/manager/tickets/<ticket_id>` with JSON body; fields (all optional): `status` (pending|in_progress|needs_input|finished), `conversation_id`, `pr_url`, `manager_note` (short one-liner shown on the card; ALSO the contract for deferrals — see below), `dispatched_entry_count` (int — set to the number of entries you have relayed to the worker so far), `append_entry` (string — appends a visible manager comment to the ticket thread).
- `GET {VIBE_API}/api/manager/workspaces/{WORKSPACE_ID}/snapshot` to re-read the board.

Worker dispatch (via the vibe API — it handles agent config; workers ALWAYS run in git worktrees, never in the main checkout):
- **Start a worker conversation**:
  `POST {VIBE_API}/api/manager/conversations` with JSON:
  `{{"working_dir": "{WORKSPACE_PATH}", "prompt": "<self-contained task prompt>", "title": "🎫 <short task summary>"}}`
  Response: `{{"id": "<conversation_id>", "conversation_url": ...}}` — immediately PATCH the id onto the ticket along with status in_progress.
- **Send a follow-up message to an existing conversation** (same endpoint):
  `{{"working_dir": "{WORKSPACE_PATH}", "prompt": "<message>", "conversation_id": "<conv_id>"}}`

Agent server API (read-only inspection): base `{AGENT_SERVER}`, header `X-Session-API-Key` (get the key with: `curl -s {VIBE_API}/api/manager/agent-credentials` → field `session_api_key`).
- **Check a conversation**: `GET /api/conversations/<conv_id>?include_skills=false` → `execution_status` (running|idle|finished|error|stuck|paused).
- **Read a worker's final report**: `GET /api/conversations/<conv_id>/agent_final_response`

## Your job this run
1. Read `AGENTS.md` in `{WORKSPACE_PATH}` (create it if missing) so you understand the project architecture. Keep it up to date: when finished tickets reveal new architecture/conventions, append concise notes so future workers benefit.
2. For every ticket whose worker conversation just ended (conversation_status finished/idle but ticket still in_progress): read its final response. If it opened a PR, PATCH pr_url + status needs_input. In push-to-main mode verify the push landed on main and mark finished. If the worker failed or stalled (error/stuck), decide: send a corrective follow-up message, or mark needs_input with an explanatory append_entry asking the user for guidance.
3. For tickets with NEW user entries beyond dispatched_entry_count:
   - If the ticket already has a conversation, prefer **reusing it**: send the new request as a follow-up message to that conversation, bump dispatched_entry_count to the current entry count, and ensure status is in_progress.
   - A finished/needs_input ticket that receives a new entry moves back to in_progress once you dispatch the follow-up.
4. For pending tickets with no conversation: dispatch workers, **highest priority first** (lower priority_rank = higher priority; the user orders cards within columns).
   - Respect the concurrency cap: count worker conversations currently in execution_status "running" on this board; never exceed **{ws['max_concurrent']}**.
   - **Avoid conflicts**: think about which tickets touch the same files/subsystems. Serialize tickets that would collide — run only independent tickets concurrently.
   - **Deferral contract**: if you deliberately leave a pending ticket undispatched (conflict serialization, capacity, needs another ticket first), you MUST set a manager_note on it (e.g. "queued behind a1b2c3 to avoid conflicts in the audio engine"). This suppresses re-invocation loops. Clear/replace the note when you later dispatch it.
   - **Reuse old conversations when sensible**: if a new ticket clearly refines or extends work a recent conversation did (check other tickets' conversation ids and topics), send it as a follow-up to that conversation instead of starting fresh — the accumulated context helps. Then PATCH that conversation id onto the new ticket.
5. Worker task prompts must be self-contained: the full ticket text (all user entries), the project path, a reminder to read AGENTS.md first, the push-mode instructions above (branch+PR, or push directly to main), and — in PR mode — to report the PR URL in their final message.
6. Update every card you acted on: status, conversation_id, manager_note (short, user-facing), dispatched_entry_count, and append_entry comments where the user needs context. Every ticket you dispatch MUST get its conversation_id set so the card links to its conversation.
7. Do not wait for workers to finish — dispatch and exit. You will be re-invoked automatically when statuses change.

Be decisive and terse. When done, summarize what you dispatched/updated in one short final message."""


def start_manager_conversation(prompt: str) -> str:
    d = _request(
        f"{VIBE_API}/api/manager/conversations", "POST",
        {
            "working_dir": WORKSPACE_PATH,
            "prompt": prompt,
            "worktree": False,  # the manager only reads/updates AGENTS.md; workers get worktrees
            "title": f"🧠 Vibe Manager — {WORKSPACE_NAME} {time.strftime('%m-%d %H:%M')}",
            "max_iterations": 200,
            "role": "manager",
        },
        timeout=150,
    )
    return d["id"]


# ------------------------------------------------------------------------ main

def main() -> None:
    state = load_state()
    board = snapshot()

    # If a manager conversation is already running for this workspace, bail
    # out. Checks both the KV-tracked id and the workspace-row id (tag-verified),
    # so a lost KV state can't cause overlapping managers.
    running_mgr = find_running_manager(state, board["workspace"])
    if running_mgr:
        started = state.get("manager_started_at") or 0
        if time.time() - started < MANAGER_STALE_SECONDS:
            print(f"manager conversation {running_mgr} still running — skipping")
            fire_callback()
            return
        print(f"manager conversation {running_mgr} exceeded stale limit — proceeding")
    prev_conv = state.get("manager_conversation_id")
    if prev_conv and prev_conv != running_mgr:
        print(f"previous manager conversation {prev_conv} ended")
        state["last_manager_finished_at"] = time.time()
    state["manager_conversation_id"] = None

    ws, tickets = enrich(board)
    apply_mechanical_transitions(tickets)
    fp = fingerprint(ws, tickets)
    signals, retry_safe = compute_signals(ws, tickets)

    changed = fp != state.get("fingerprint")
    # Safety net: if retry-safe signals persist but nothing changed (e.g. a
    # previous manager run crashed before acting), retry on a slow cadence.
    stale_retry = bool(retry_safe) and (
        time.time() - (state.get("manager_started_at") or 0) > RETRY_INTERVAL_SECONDS
    )
    print(f"fingerprint changed: {changed}; signals: {signals or 'none'}; stale_retry: {stale_retry}")

    if (signals and changed) or stale_retry:
        prompt = build_manager_prompt(ws, tickets)
        conv_id = start_manager_conversation(prompt)
        print(f"manager kicked off: {CANVAS_BASE}/conversations/{conv_id}")
        state.update({
            "manager_conversation_id": conv_id,
            "manager_started_at": time.time(),
            "fingerprint": fp,
        })
    else:
        state["fingerprint"] = fp

    state["last_checked_at"] = time.time()
    save_state(state)
    fire_callback()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        fire_callback("FAILED", str(exc))
        raise
