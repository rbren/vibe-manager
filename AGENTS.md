# vibe-manager

Vibecoding work manager: kanban SPA + per-workspace "Manager" automation that
dispatches OpenHands worker agent conversations.

Live at https://vibe.apps.canvas.rbren.io (nginx + Let's Encrypt + basic auth
via /etc/nginx/.htpasswd).

## Architecture

- `app.py` â€” FastAPI backend on 127.0.0.1:18300 (systemd: `vibe-manager.service`).
  SQLite at `./vibe.db`. Serves the SPA from `static/` under `/assets`.
  - Public API (`/api/...`): workspace picker, settings (max_concurrent,
    push_mode pr|main), ticket CRUD (append-only entries), priority reorder.
  - Manager API (`/api/manager/...`): board snapshot, ticket PATCH
    (status/conversation_id/pr_url/manager_note/dispatched_entry_count/
    append_entry), `POST /api/manager/conversations` to start/follow-up worker
    conversations, agent-credentials.
  - Selecting a workspace bootstraps (or refreshes, incl. tarball) a per-
    workspace cron automation named `vibe-manager:<id>:<name>` in the
    automation backend (every minute).
- `automation/main.py` â€” deterministic poller, packaged into the automation
  tarball with a per-workspace `config.json`. Each run: gathers board +
  conversation statuses, applies mechanical transitions (PR merged â†’ finished,
  push-to-main verified â†’ finished), computes a fingerprint + actionable
  signals, and only kicks off a Manager agent conversation when the
  fingerprint changed (or a retry-safe signal persists >10 min). State lives
  in the automation KV store, including a `conv_statuses` map (conversation_id
  → execution_status) for every tracked ticket conversation; a conversation
  running while its ticket is NOT in_progress (e.g. the user manually messaged
  a needs_input worker) raises an `agent-resumed` signal so the manager moves
  the card back to in_progress. Tests:
  `python tests/test_automation_conv_state.py` (pure stdlib).
  - Deferral contract: if the Manager deliberately does NOT dispatch a pending
    ticket it must set `manager_note`, which suppresses the dispatchable
    signal until the board changes.
- **Ticket attachments**: users can attach files/images to tickets (paperclip
  button on the new-ticket form and in the drawer's append form; drawer shows
  image thumbnails + file chips, cards show a count chip). `attachments`
  table (id, ticket_id, filename, content_type, size, created_at); bytes on
  disk at `data/attachments/<att_id>/<filename>` (gitignored; env overrides
  `VIBE_DATA_DIR` / `VIBE_DB_PATH` exist for tests).
  - Upload is a **raw-body POST** `/api/tickets/<id>/attachments?filename=...`
    (Content-Type header = file type, 25 MB cap) - deliberately NOT multipart
    so python-multipart isn't a dependency of the service venv. Download:
    `GET /api/attachments/<att_id>` (inline disposition). Filenames are
    sanitized (basename + safe-char whitelist).
  - Every ticket dict (board AND manager snapshot) carries `attachments`,
    each with `url` (relative API URL) and `path` - a **stable absolute
    path** under the vibe-manager checkout. Manager-to-worker handoff design:
    workers run on the same machine, so the manager just lists each
    attachment's `path` in the worker prompt (automation/main.py prompt item
    5); workers read/view the files in place and only `cp` one into their
    worktree if it should become part of the repo. Attachment ids feed the
    automation fingerprint, so an upload re-triggers the manager (an
    attachment-only change still needs an actionable signal, e.g. an
    undispatched pending ticket or a new entry).
  - Tests: `tests/test_attachments.py` (plain script, run with
    `.venv/bin/python tests/test_attachments.py`; uses a temp DB/data dir).
  - nginx: `client_max_body_size 26m` was added to the site config for
    uploads - re-copy `nginx/vibe.apps.canvas.rbren.io` to sites-available
    and reload nginx when deploying this feature.

- **Reopen on new request**: `POST /api/tickets/<id>/entries` with
  author=user on a finished/needs_input ticket immediately sets it back to
  pending (bottom of the pending column) so the manager picks it up —
  in_progress and verified tickets are untouched, as are manager/agent
  entries. The automation's mechanical "PR open → needs_input" transition
  skips tickets with undispatched entries so it can't undo a reopen. Tests:
  `tests/test_reopen_on_entry.py`.
- `static/` â€” vanilla JS SPA (no build step). Kanban columns: pending,
  in_progress, needs_input, finished. Drag vertically to reprioritize; click
  a card for the append-only drawer; each card links to its conversation.
  Finished cards have a "mark verified" button (`POST
  /api/tickets/<id>/verify` sets status=verified + verified_at); verified
  tickets leave the board and appear in a fifth column (most recently
  verified first) toggled by the top-bar "show verified" button. The
  automation ignores verified tickets entirely (terminal state).

## Key facts / gotchas

- Worker conversations are ALWAYS started with `worktree: true` (git worktree
  under /tmp/conversation-worktrees/<conv_id>/<project>). Manager
  conversations run directly in the workspace (worktree: false) to maintain
  AGENTS.md.
- Every conversation started via `/api/manager/conversations` gets tags:
  `workspace=<project path>` (so the canvas UI groups it under the right
  workspace — the worktree working_dir alone is NOT enough) and
  `viberole=worker|manager`. Manager conversation ids are also recorded on
  `workspaces.manager_conversation_id`; the cron bails out if that
  conversation is still running (tag-verified), so overlapping managers can't
  happen even if the automation KV state is lost.
- Conversation creation must use `agent_settings` from
  `GET /api/settings` with header `X-Expose-Secrets: encrypted` +
  `secrets_encrypted: true`, and `tools` forced to `null` (the stored value
  `[]` means bare agent; `null` resolves the default exec toolset). Using
  `agent_profile_id` yields an agent with NO tools â€” don't.
- Auth keys are read at service start from `.session-key` / `.automation-key`
  in the repo root (falling back to env). These are extracted from the live
  agent-server process env; static copies go stale.
- Automation backend: 127.0.0.1:18001, header X-Session-API-Key. Agent
  server: 127.0.0.1:18000. Conversation search endpoint is unusably slow â€”
  always GET conversations by ID.
- systemd unit: `systemd/vibe-manager.service` (copy to /etc/systemd/system
  and `systemctl daemon-reload` after edits). nginx site:
  `nginx/vibe.apps.canvas.rbren.io` (copy to sites-available).
- After editing `automation/main.py`, re-select the workspace in the UI (or
  POST /api/workspaces) to re-upload the tarball to existing automations.

- Git remote: `origin` is https://github.com/rbren/vibe-manager (PRIVATE
  repo; push-to-master allowed). The remote URL embeds a GitHub token pulled
  from the agent-server secrets store (`GET
  /api/settings/secrets/GITHUB_PERSONAL_ACCESS_TOKEN` with the session key) —
  never commit the token; refresh the URL from the secrets store if it stops
  authenticating.
- The default branch is `master`, not `main`.
- History was rewritten (git filter-repo, 2026-05-21) to purge
  `.session-key` / `.automation-key`, which are now gitignored and exist only
  on disk (the service reads them from the repo root at startup — do not
  delete them). Never commit these files again.
- Workers in worktrees cannot push to the checked-out `master` branch, so the
  convention remains: workers commit on a feature branch in their worktree
  and report branch+commit; the Manager merges that branch into `master` in
  the main checkout, pushes to origin, and restarts `vibe-manager.service`
  to deploy.

- History-rewrite serialization: while a git history rewrite (filter-repo) is
  in flight (ticket 93b58130), do NOT dispatch any other workers — worktree
  branches based on pre-rewrite hashes get orphaned and merging them would
  reintroduce the purged secret blobs. Queue everything behind the rewrite.

- Manager scheduling note: the SPA is a single static/app.js (no build step), so
  frontend tickets almost always collide there — the Manager serializes app.js-
  heavy tickets and tells concurrent workers to fetch+rebase onto latest master
  before landing.
