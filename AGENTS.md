# vibe-manager

> **Workspace assignment correction (2026-05-21, RESOLVED):** the tag/backfill
> approach (ede340a) was rejected by the user; conversations must instead be
> created with the dedicated `workspace` field (`{"kind": "LocalWorkspace",
> "working_dir": <project path>}`) kept pointing at the PROJECT directory —
> see "Conversation ↔ workspace association" under Key facts for how app.py
> now does this for every conversation (workers included) on all workspaces.

Vibecoding work manager: kanban SPA + per-workspace "Manager" automation that
dispatches OpenHands worker agent conversations.

Live at https://vibe.apps.canvas.rbren.io (nginx + Let's Encrypt + basic auth
via /etc/nginx/.htpasswd).

## Architecture

- `app.py` ├óŌé¼ŌĆØ FastAPI backend on 127.0.0.1:18300 (systemd: `vibe-manager.service`).
  SQLite at `./vibe.db`. Serves the SPA from `static/` under `/assets`.
  - Public API (`/api/...`): workspace picker, settings (max_concurrent,
    push_mode pr|main), ticket CRUD (append-only entries), priority reorder.
  - Workspace picker (`GET /api/workspaces`): `available` = children of the
    agent-server workspace parents (e.g. /root/git/*) + agent-server-
    registered workspaces; `selected` = already-onboarded ones from vibe.db.
    The SPA picker deliberately shows ONLY the workspace name (no directory
    path like /root/git/foo ŌĆö user request 2026-05); option values are still
    full paths.
  - Manager API (`/api/manager/...`): board snapshot, ticket PATCH
    (status/conversation_id/pr_url/manager_note/dispatched_entry_count/
    append_entry), `POST /api/manager/conversations` to start/follow-up worker
    conversations, agent-credentials.
  - Selecting a workspace bootstraps (or refreshes, incl. tarball) a per-
    workspace cron automation named `vibe-manager:<id>:<name>` in the
    automation backend (every minute).
- `automation/main.py` ├óŌé¼ŌĆØ deterministic poller, packaged into the automation
  tarball with a per-workspace `config.json`. Each run: gathers board +
  conversation statuses, applies mechanical transitions (PR merged ├óŌĆĀŌĆÖ finished,
  push-to-main verified ├óŌĆĀŌĆÖ finished), computes a fingerprint + actionable
  signals, and only kicks off a Manager agent conversation when the
  fingerprint changed (or a retry-safe signal persists >10 min). State lives
  in the automation KV store, including a `conv_statuses` map (conversation_id
  ŌåÆ execution_status) for every tracked ticket conversation; a conversation
  running while its ticket is NOT in_progress (e.g. the user manually messaged
  a needs_input worker) raises an `agent-resumed` signal so the manager moves
  the card back to in_progress. Tests:
  `python tests/test_automation_conv_state.py` (pure stdlib).
  - Deferral contract: if the Manager deliberately does NOT dispatch a pending
    ticket it must set `manager_note`, which suppresses the dispatchable
    signal until the board changes.
  - **Re-invocation loop guards** (regression fix for the 2026-08-21
    overnight loop � 50 no-op manager runs on the openhands workspace, one
    every ~10 min): (1) `has_undispatched_entries` only counts non-manager
    entries beyond `dispatched_entry_count`, so the manager's own
    `append_entry` status comments can't raise a `new-entries` signal;
    (2) the stale-retry safety net is capped at `MAX_RETRY_ATTEMPTS` (3)
    kickoffs per unchanged fingerprint (`retry_count` in KV state, reset on
    any board change); (3) app.py's manager PATCH auto-absorbs trailing
    manager-authored entries into `dispatched_entry_count` after an
    `append_entry` (never advancing past a user/agent entry); (4) the manager
    prompt tells the manager to bump the count itself when the only
    "new" entries are its own comments. Tests:
    `python tests/test_manager_loop_guard.py` (pure stdlib) and
    `.venv/bin/python tests/test_dispatched_count_absorbs_manager_notes.py`.
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

- **Manager note style** (user request, ticket c40ab0776313): manager_note
  and append_entry are STATUS-ONLY one-liners — e.g. "Worker dispatched",
  "Landed 37d9ab on origin/master", "Worker restarted for fix" — never a
  description of the task itself (the card already shows it). Exceptions:
  deferral notes keep their short reason (deferral contract), and a
  needs_input append_entry may carry the specific question for the user.
  Enforced via the "Note style rule" in the manager prompt
  (automation/main.py item 6).
- **Ticket titles**: tickets have a nullable `title` column, included in every
  ticket dict (board AND manager snapshot) and settable via the manager PATCH
  endpoint (`/api/manager/tickets/<id>`, trimmed; blank clears it). The SPA
  renders it as a bold heading on kanban cards and in the drawer. Convention
  (enforced via the Manager prompt in automation/main.py, "Title rule"): one
  emoji prefix + ONE or TWO words, NEVER more than two words (e.g.
  "­¤Éø Login fix"); the manager titles every untitled ticket it touches.
  Tests: `tests/test_ticket_title.py`.
- **Reopen on new request**: `POST /api/tickets/<id>/entries` with
  author=user on a finished/needs_input ticket immediately sets it back to
  pending (bottom of the pending column) so the manager picks it up ŌĆö
  in_progress and verified tickets are untouched, as are manager/agent
  entries. The automation's mechanical "PR open ŌåÆ needs_input" transition
  skips tickets with undispatched entries so it can't undo a reopen. Tests:
  `tests/test_reopen_on_entry.py`.
- **Live action summaries**: in_progress tickets show the worker agent's most
  recent action summary (kanban card + drawer, pulsing dot). `app.py` watches
  each in_progress conversation server-side: seeds via the agent server's
  `GET /api/conversations/<id>/events/search` (TIMESTAMP_DESC), then holds a
  websocket to `ws://ŌĆ”:18000/sockets/events/<id>`; the LLM-predicted `summary`
  lives inside each ActionEvent's `tool_call.arguments` JSON (see
  `extract_action_summary`). The cached value rides on the board payload as
  `latest_action` ({summary, tool, timestamp}) only for in_progress tickets,
  so the SPA's 5s board poll updates it live and the session API key never
  reaches the browser. Watchers start on board requests and stop after 5 min
  without one (`ACTIVITY_IDLE_TTL`). Requires the `websockets` package in the
  service venv (`.venv/bin/pip install websockets`). Tests:
  `tests/test_activity_summary.py`.
- **Per-ticket LLM model chip**: every ticket dict with a `conversation_id`
  carries `llm_model` (from the agent server's conversation metadata,
  `agent.llm.model` on `GET /api/conversations/<id>`). app.py caches it
  (`_model_cache`, 5 min TTL) and refreshes in background threads so the 5s
  board poll never blocks on or hammers the agent server; terminal-status
  tickets (finished/verified) are sticky — a known model never re-polls. The
  cache is primed on conversation create and invalidated on follow-up
  `switch_profile`; a failed refetch keeps the last known model. SPA renders
  a `.chip.model` (violet tokens) on cards and in the drawer links row —
  short name (provider prefix stripped), full name in the tooltip. Tests:
  `tests/test_conversation_model.py`.
- **Manager automation status badge**: `GET /api/workspaces/<id>/automation`
  proxies the automation backend (GET `/v1/<automation_id>` +
  `/v1/<automation_id>/runs?limit=5`) and returns {configured, enabled,
  last_triggered_at, run_active (any run without completed_at), last_run
  {status/error_detail/timestamps}, manager_conversation {id, status} from the
  agent server via `workspaces.manager_conversation_id`, error}. Automation
  backend failures degrade to `error` (still 200) so the UI can show
  "unknown" instead of breaking. The SPA polls it every 15s and renders the
  topbar `#mgr-badge` as: working (manager conversation running, pulsing) /
  polling (automation run active) / Ō£ō|Ō£Ś + relative last-run time / paused
  (disabled) / unknown (backend error); details in the badge tooltip. CSS
  variants `.mgr-badge.ok|.err|.paused`. Tests:
  `tests/test_automation_status.py` (stub automation backend on a local
  port via VIBE_AUTOMATION_API; run with the service venv ŌĆö note tests
  import app.py, which needs `.session-key`/`.automation-key` in the repo
  root, so run them from the main checkout).
  - **Manual trigger**: clicking the badge (or Enter/Space, it's a
    role=button) POSTs `/api/workspaces/<id>/automation/trigger`, which
    proxies POST `/v1/<automation_id>/dispatch` on the automation backend
    (creates a PENDING run picked up immediately by the dispatcher) ŌĆö 404
    unknown workspace, 409 if no automation configured, 502 if the backend
    call fails. The SPA shows "manager: triggering" + `.mgr-badge.triggering`
    (disabled) while in flight, then refreshes the badge.
    Tests: `tests/test_trigger_automation.py`.
- **No toasts** (user request 2026-05-21): the SPA has NO toast notifications.
  The `#toast` element/CSS and `toast()` helper were removed; error paths use
  `console.error` and success is conveyed by the board refresh itself. Don't
  reintroduce toasts for user feedback.
- **URL routing**: selecting a workspace pushes `/workspace/<name>` (name =
  directory basename, encodeURIComponent'd) via history.pushState; popstate
  re-selects. On load the SPA prefers the URL's workspace over
  localStorage (`vibe.workspace`), resolving name ŌåÆ path from the
  /api/workspaces lists. `app.py` serves the SPA index for `GET
  /workspace/{path:path}` so deep links work (nginx proxies everything, no
  nginx change needed). Asset URLs are absolute (`/assets/...`) so nested
  paths render fine.
- **Theming (light/dark)**: dark is the default; light mode is a full CSS-
  variable override under `html[data-theme="light"]` in static/style.css. ALL
  colors in the stylesheet must be var() tokens defined in `:root` (incl.
  border/glow/backdrop tokens like `--amber-line`, `--accent-glow`,
  `--topbar-bg`, `--accent-contrast` for text on solid accent buttons) ŌĆö
  never hardcode a hex/rgba outside the two token blocks, or light mode
  breaks. The topbar `#theme-toggle` button flips the theme
  (`applyTheme`/`toggleTheme` in app.js), persisted in localStorage
  `vibe.theme`; an inline `<script>` in index.html's head applies the saved
  theme before first paint to avoid a flash.
  - Light palette was deliberately softened (d7a46d5): warm off-white
    surfaces (no pure #fff cards), low-opacity shadows/glows, eased
    near-black text, desaturated accents. Keep new light-mode tokens muted
    to match — "less jarring" is a user requirement.
- `static/` ├óŌé¼ŌĆØ vanilla JS SPA (no build step). Kanban columns: pending,
  in_progress, needs_input, finished. Drag vertically to reprioritize; click
  a card for the append-only drawer; each card links to its conversation.
  Finished cards have a "mark verified" button (`POST
  /api/tickets/<id>/verify` sets status=verified + verified_at); verified
  tickets leave the board and appear in a fifth column (most recently
  verified first) toggled by the top-bar "show verified" button. The
  automation ignores verified tickets entirely (terminal state).

## Key facts / gotchas

- **Conversation Ōåö workspace association**: every conversation the manager
  creates is POSTed with the dedicated `workspace` option ŌĆö
  `{"kind": "LocalWorkspace", "working_dir": "<project path>"}` ŌĆö exactly how
  the canvas UI attaches a conversation to a picked workspace. Workers still
  get an isolation git worktree under
  /tmp/conversation-worktrees/<conv_id>/<project>, but app.py provisions it
  itself (`_provision_worker_worktree`, branch `openhands/<conv_id>` based on
  origin default branch, guidance appended to the prompt) and passes
  `worktree: false` ŌĆö the agent server's `worktree: true` would REWRITE
  `workspace.working_dir` to the worktree path, dissociating the conversation
  from the workspace (that's what put everything under "no workspace").
  Manager conversations run directly in the workspace to maintain AGENTS.md.
  Tests: `tests/test_worker_worktree.py`.
- Every conversation started via `/api/manager/conversations` gets tags:
  `workspace=<project path>` (so the canvas UI groups it under the right
  workspace ŌĆö the worktree working_dir alone is NOT enough) and
  `viberole=worker|manager`. Follow-ups to an existing conversation retro-tag
  it if the `workspace` tag is missing (self-heals pre-tagging conversations).
  Manager conversation ids are also recorded on
  `workspaces.manager_conversation_id`; the cron bails out if that
  conversation is still running (tag-verified), so overlapping managers can't
  happen even if the automation KV state is lost.
- **How canvas workspace grouping actually works** (verified 2026-05-21
  against openhands/openhands main 550fc28a4 — the canvas UI source; the
  agent-canvas repo is archived): the sidebar groups by
  `selected_workspace`, resolved in `toAppConversation`
  (src/api/agent-server-adapter.ts) from **localStorage metadata ONLY**
  (written by `setStoredConversationMetadata` when a human launches from
  the workspace picker). `workspaceGroup()` in
  conversation-panel-list-helpers.ts deliberately does NOT use
  `workspace.working_dir` (worktree paths would fragment groups). So NO
  version — 1.14.0 installed here IS current — groups API-created
  conversations under a workspace; they always render as "No workspace".
  This is an upstream gap; the fix belongs in openhands/openhands (e.g.
  fall back to `tags.workspace` in toAppConversation), NOT here.
  **Do NOT patch the canvas install**: local edits to the minified bundles
  were attempted twice (tags fallback in
  `agent-server-conversation-service.api-*.js`, then an import-map
  cache-bust in index.html + an nginx alias) and the user rejected live-app
  modification both times; everything was reverted 2026-05-21 (bundle,
  index.html, nginx canvas.rbren.io, /var/www/canvas-patches all pristine).
  Keep vibe-manager's side server-side only: conversations carry
  `tags.workspace` and `workspace.working_dir` (already correct for both
  managers). Also note canvas assets are served `Cache-Control: immutable,
  max-age=1y`, so any in-place asset edit is invisible to browsers anyway.
  `PATCH /api/conversations/<id> {"tags": {...}}` REPLACES all tags ŌĆö always
  merge with the existing ones. `scripts/backfill_workspace_tags.py`
  (stdlib-only, idempotent, run from the repo root) retro-tags every
  conversation referenced by vibe.db across ALL workspaces. Tagging happens
  server-side in app.py, so it covers every workspace's manager (vibe-manager
  AND dj-station) without touching their automation tarballs.
- Conversation creation must use `agent_settings` from
  `GET /api/settings` with header `X-Expose-Secrets: encrypted` +
  `secrets_encrypted: true`, and `tools` forced to `null` (the stored value
  `[]` means bare agent; `null` resolves the default exec toolset). Using
  `agent_profile_id` yields an agent with NO tools ├óŌé¼ŌĆØ don't.
- **Per-task model selection**: the Manager picks a model per worker task.
  Available models = the agent server's LLM profiles
  (`GET /api/profiles`, proxied without secrets at
  `GET /api/manager/llm-profiles` → {profiles: [{name, model}],
  active_profile}). `POST /api/manager/conversations` accepts
  `llm_profile: <name>`: on create, app.py fetches the profile's full LLM
  config via `GET /api/profiles/<name>` with `X-Expose-Secrets: encrypted`
  (profile carries its OWN encrypted api_key) and swaps it into
  `agent_settings.llm`, preserving the settings llm's `usage_id`; on
  follow-up it calls `POST /api/conversations/<id>/switch_profile` first
  (so the manager can escalate a stuck worker to a stronger model). Unknown
  names → 400 listing available profiles, validated BEFORE the worker
  worktree is provisioned. The manager prompt's "Model selection" section
  (automation/main.py `model_selection_instructions()`) inlines the live
  profile list at prompt-build time and degrades to a self-serve
  GET instruction if the vibe API is unreachable. Guidance is
  capability-based (strongest ↔ gnarly work, default ↔ routine, cheapest ↔
  chores) because profile names change. Tests: `tests/test_llm_profiles.py`.
- Auth keys are read at service start from `.session-key` / `.automation-key`
  in the repo root (falling back to env). These are extracted from the live
  agent-server process env; static copies go stale.
- Automation backend: 127.0.0.1:18001, header X-Session-API-Key. Agent
  server: 127.0.0.1:18000. Conversation search endpoint is unusably slow ├óŌé¼ŌĆØ
  always GET conversations by ID.
- systemd unit: `systemd/vibe-manager.service` (copy to /etc/systemd/system
  and `systemctl daemon-reload` after edits). nginx site:
  `nginx/vibe.apps.canvas.rbren.io` (copy to sites-available).
- After editing `automation/main.py`, re-select the workspace in the UI (or
  POST /api/workspaces) to re-upload the tarball to existing automations.

- Git remote: `origin` is https://github.com/rbren/vibe-manager (PRIVATE
  repo; push-to-master allowed). The remote URL embeds a GitHub token pulled
  from the agent-server secrets store (`GET
  /api/settings/secrets/GITHUB_PERSONAL_ACCESS_TOKEN` with the session key) ŌĆö
  never commit the token; refresh the URL from the secrets store if it stops
  authenticating.
- The default branch is `master`, not `main`.
- History was rewritten (git filter-repo, 2026-05-21) to purge
  `.session-key` / `.automation-key`, which are now gitignored and exist only
  on disk (the service reads them from the repo root at startup ŌĆö do not
  delete them). Never commit these files again.
- Push-to-main mode (current): workers commit on their worktree branch and
  push directly to the default branch with `git push origin HEAD:master`
  (this works from a worktree even though `master` is checked out in the main
  clone ŌĆö only a local checkout of `master` is blocked). Deploy = in the main
  checkout `git pull --ff-only && systemctl restart vibe-manager.service`,
  then verify `/api/health`. Workers should fetch+rebase onto latest
  origin/master right before pushing.
- Agent-server restarts kill in-flight worker tool calls and leave those
  conversations in `error` state ("A restart occurred while this tool was in
  progress"). The worktree survives with uncommitted changes; the Manager
  should check `git log origin/master` / the worktree before deciding ŌĆö the
  work may already be landed+deployed (only the final report was lost), or a
  resume follow-up message restarts the worker where it left off.

- History-rewrite serialization: while a git history rewrite (filter-repo) is
  in flight (ticket 93b58130), do NOT dispatch any other workers ŌĆö worktree
  branches based on pre-rewrite hashes get orphaned and merging them would
  reintroduce the purged secret blobs. Queue everything behind the rewrite.

- Manager scheduling note: the SPA is a single static/app.js (no build step), so
  frontend tickets almost always collide there ŌĆö the Manager serializes app.js-
  heavy tickets and tells concurrent workers to fetch+rebase onto latest master
  before landing.

- Text sizing (cf7840e, 2026-05): `static/style.css` sets `html { font-size:
  120%; }` and all font-sizes are in `rem` (base 16px). Change the root
  percentage to rescale all text; keep new font-size declarations in rem.
