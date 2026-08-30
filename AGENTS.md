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
    push_mode pr|main, accent = the workspace's primary colour), ticket CRUD
    (append-only entries), priority reorder.
  - Workspace picker (`GET /api/workspaces`): `available` = children of the
    agent-server workspace parents (e.g. /root/git/*) + agent-server-
    registered workspaces; `selected` = already-onboarded ones from vibe.db.
    The SPA picker deliberately shows ONLY the workspace name (no directory
    path like /root/git/foo ŌĆö user request 2026-05, no `(not git)` annotation
    ŌĆö user request 2026-05-21); option values are still full paths. Both
    pickers (static/app.js and the extension) must stay in sync. The extension
    never had the `is_git` flag at all, so every candidate there was labelled
    "(not git)".
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
  the card back to in_progress; the converse — an in_progress ticket whose
  conversation reached a `TERMINAL_CONV_STATUSES` value — raises
  `worker-done`, which is what gets the card reconciled after a worker ends.
  Tests: `python tests/test_automation_conv_state.py` (pure stdlib).
  - **Manager-conversation guard**: `manager_conversation_state()` reports
    {id, status, started_at, active, failed} for the manager conversation the
    workspace started last (KV id first, then the tag-verified workspace-row
    id). Only `MANAGER_ACTIVE_STATUS` ("running") suppresses a new kickoff —
    error/stuck/paused/idle/finished all mean the manager is done. A manager
    that ended in `MANAGER_FAILED_STATUSES` (error/stuck) never touched the
    board, so the fingerprint is unchanged and the retry-safe signals are
    still there: `kickoff_decision(..., manager_failed=True)` then restarts it
    immediately instead of waiting out `RETRY_INTERVAL_SECONDS` (still capped
    by `MAX_RETRY_ATTEMPTS` per unchanged fingerprint, so a manager that keeps
    dying can't loop). Without this a crashed manager froze the board for
    10 minutes at a time and then gave up entirely.
  - Deferral contract: if the Manager deliberately does NOT dispatch a pending
    ticket it must set `manager_note`, which suppresses the dispatchable
    signal until the board changes.
  - **Re-invocation loop guards** (regression fix for the 2026-08-21
    overnight loop — 50 no-op manager runs on the openhands workspace, one
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
- **"Talk to the manager" chat** (user request 2026-05-21): a `Talk to the
  manager` button sits in the topbar (moved there 2026-05-21, was next to
  `Send request` on the new-request form) and opens a modal chat window backed
  by a conversation of its own, pre-loaded with a manager skill. The skill
  (app.py `manager_chat_skill`, mirrored for the
  extension in `src/managerchat.js`) tells the agent to (a) record every
  request the user makes in the project's `AGENTS.md` under a single top-level
  `## Manager` section — dated, in the user's words, committed so the checkout
  stays clean — and (b) how to read the board and the conversations running on
  it. It deliberately does NOT dispatch workers: the cron manager owns
  concurrency, conflict serialization and one-conversation-per-ticket, none of
  which a chat agent can see.
  - The conversation is created through the SAME path as every other one
    (`POST /api/manager/conversations` internally, so `workspace.working_dir`
    stays the PROJECT dir and the tags are set), with `worktree: false` (it
    edits AGENTS.md in the checkout) and **`viberole: manager_chat`** — a third
    role next to worker/manager. The distinct role matters: `role == "manager"`
    is what writes `workspaces.manager_conversation_id`, which drives the
    topbar badge and the automation's "a manager is already running" guard, so
    a chat must never claim it.
  - SPA endpoints: `POST /api/workspaces/<id>/manager-chat` (start),
    `POST|GET /api/workspaces/<id>/manager-chat/<conv_id>/messages` (send /
    poll). The GET replays the conversation's `MessageEvent`s as
    {role, text, timestamp} plus `status` and `latest_action`; the session key
    never reaches the browser. The pre-loaded skill starts with
    `MANAGER_CHAT_MARKER` (`<!-- vibe-manager-skill -->`) purely so that first
    turn can be hidden — it is not something the user typed. Polling is
    incremental via `cursor` = the newest EVENT seen (not the newest message),
    echoed back as `after`; `timestamp__gte` on the agent server is inclusive,
    so the boundary event is dropped client-side. Note the events/search `kind`
    filter returns nothing on this agent server — filter by kind in code.
  - A sent message is NOT rendered optimistically: posting it creates the event
    on the conversation, and the immediate re-poll brings it back, so there is
    no local copy to reconcile.
  - The chat is per workspace and per session (in `state.chat`); switching
    workspaces closes it and drops it, so a reload starts a fresh conversation.
  - The extension has the same button and modal. It has no server, so
    `src/managerchat.js` creates the conversation itself (raw `fetch` for
    `GET /api/settings` with `X-Expose-Secrets: encrypted`, which the host
    client cannot send, then `POST /api/conversations` through the host). Its
    copy of the skill differs in ONE section: this board lives in the JSON
    store, so the manager reads it with `<store>/bin/<ws_id>/vibectl.py`
    instead of curling the vibe API. Keep the two skills in step otherwise.
  - Tests: `tests/test_manager_chat.py` and the "talk to the manager" cases in
    `extensions/kanban-manager/test/extension.test.js`.
- **Workspace settings live in the filestore, never localStorage** (user
  request 2026-05-21). Every preference the board exposes is a field on the
  workspace record — `max_concurrent`, `push_mode`, `accent`, `theme`,
  `show_verified`, `llm_profile`, `max_budget` — in the `workspaces` table
  (app.py, migrations add the last four; `THEMES`/`DEFAULT_THEME` validate the
  theme, blank `llm_profile` clears it to manager's choice) and on the
  index.json record for the extension (`src/store.js` defaults). One PATCH
  endpoint writes them all: `PATCH /api/workspaces/<id>` in the SPA,
  `Store.updateWorkspace` in the extension.
  - The UI adopts them on every board payload (`adoptWorkspacePrefs` in both
    static/app.js and src/extension.js), so a workspace looks the same from any
    browser and switching workspaces switches theme/verified/agent/budget.
  - localStorage keeps only *hints*, never state: `vibe.theme.hint` and
    `vibe.accent` are read by index.html's head script to avoid a first-paint
    flash, and `vibe.workspace` remembers which board to reopen. Don't add
    preferences to it. A leftover `vibe.theme` (the key from before the move)
    is handed to the first workspace that opens and then deleted, so an
    existing light-mode browser doesn't snap back to dark.
  - Topbar layout (same in static/index.html and `src/markup.js`): workspace
    picker, accent dot, Max agents, **Talk to the manager**, theme toggle
    (SPA only), **⚙ settings popover** (agent / budget / where changes land),
    **manager group** (`#mgr-group` — the status badge plus, in the extension,
    the Stop segment; see "The extension creates its own manager automation").
    The new-request form is just textarea + 📎 + Send.
  - **Show verified is not in the topbar** (user request 2026-05-21): it is an
    icon-only chip (`#show-verified`, an eye) on the **Finished** column's
    header, beside the lane it reveals. No text label — the "Show verified" /
    "Hide verified" wording lives in `title`/`aria-label` and the on state is
    `aria-pressed` + `.active` (lit in the verified lane's colour), all set by
    `renderVerifiedToggle`, which must therefore never touch `textContent`.
  - Tests: `tests/test_workspace_settings.py` (run with `.venv/bin/python`),
    and in `extensions/kanban-manager/test/extension.test.js` the
    "takes the theme and the verified toggle from the workspace record" and
    "stores the agent and budget on the workspace" cases.
- **Request settings** (⚙ in the TOPBAR since 2026-05-21, was on the
  new-request form): the agent that runs a ticket and the budget it may spend.
  Each ticket still records the choice (`llm_profile` / `max_budget` columns +
  the same fields in the JSON store), but the ⚙ now edits the WORKSPACE's
  defaults and every new ticket inherits them (see Workspace settings).
  - Defaults are **"manager's choice"** (`llm_profile` null — the manager keeps
    picking a model per task, see Model selection) and **$10**
    (`DEFAULT_TICKET_BUDGET` in app.py, `DEFAULT_BUDGET` in static/app.js and
    `src/store.js` — keep the three in step, and the `value` on the input).
    The picker is filled from the agent server's profiles
    (`GET /api/manager/llm-profiles` in the SPA, `/api/profiles` via the host
    in the extension), fetched once per session with the active profile
    labelled "(default)".
  - An explicit profile wins at dispatch: `vibectl dispatch/followup --ticket
    <id>` resolves it (`vibestore.ticket_llm_profile`) and overrides the
    manager's `--profile`; `POST /api/manager/conversations` does the same
    with `ticket_id` (`app.ticket_llm_profile`). The manager prompt carries
    each ticket's `requested_model`/`budget_usd` and is told the user's choice
    wins.
  - The agent server has **no max-spend option**, so the budget is enforced by
    the poller: `conversation_spend()` sums `stats.usage_to_metrics[*]
    .accumulated_cost`, and `apply_budget_stops()` pauses a worker at or over
    its ticket's budget, moves the card to needs_input and leaves a
    manager_note. Each conversation is stopped **once** (`budget_stopped` in
    the KV state) so a deliberate resume is not immediately undone.
  - Tests: `tests/test_request_settings.py` (run with
    `.venv/bin/python`), the "submitting a new request" cases in
    `extensions/kanban-manager/test/extension.test.js`, `createTicket` in
    `test/store.test.mjs`, and `llmProfiles` in `test/live.test.mjs`.
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
- **One conversation per ticket** (user request 2026-05-21): the Manager must
  never graft a NEW ticket onto another ticket's conversation — once a ticket
  is finished/verified its conversation is retired. Follow-up entries on the
  SAME ticket still reuse that ticket's own conversation. Enforced via the
  "One conversation per ticket" rule in the manager prompt (automation/main.py
  item 4, replacing the old "Reuse old conversations when sensible"
  guidance). Tests: `tests/test_llm_profiles.py`.
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
  websocket to `ws://…:18000/sockets/events/<id>`; the LLM-predicted `summary`
  lives inside each ActionEvent's `tool_call.arguments` JSON (see
  `extract_action_summary`). The cached value rides on the board payload as
  `latest_action` ({summary, tool, timestamp}) only for in_progress tickets,
  so the SPA's 5s board poll updates it live and the session API key never
  reaches the browser. Watchers start on board requests and stop after 5 min
  without one (`ACTIVITY_IDLE_TTL`). Requires the `websockets` package in the
  service venv (`.venv/bin/pip install websockets`). Tests:
  `tests/test_activity_summary.py`.
  - **Live vs. done**: a summary is only *live* while the worker conversation
    is. in_progress tickets therefore also carry `conversation_status` (the
    agent server's `execution_status`, `_status_cache` with a 10 s
    `CONV_STATUS_TTL` — same background fetch as `llm_model`, one GET feeds
    both caches). `DONE_CONV_STATUSES` in app.js (finished/idle/error/stuck/
    paused/deleted) switches the card from the pulsing `.activity-dot` +
    `.card.live` rail to a static `.activity-check` ✓ (`.card-activity.done`
    / `.drawer-activity.done`, caret suppressed). Without it a card whose
    worker had finished kept blinking as if work were still happening.
    **The Canvas extension mirrors this** (that is the board the user
    actually looks at): `Live.conversationStatus` caches `execution_status`
    for 10 s and `Live.decorate` puts it on in_progress tickets, so
    `extension.js` (`workerDone` / `activityNodes`) makes the same choice.
    One `refreshConversation` GET fills the model and status caches together —
    do not add a second request for a derived field. Tests: the
    "worker activity indicator" case in `test/extension.test.js` renders the
    built bundle with running/finished/error workers, and `test/live.test.mjs`
    covers the shared fetch. The CSS needs nothing: the bundle builds from
    `static/style.css`.
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
  `/v1/<automation_id>/runs?limit=10`) and returns {configured, enabled,
  last_triggered_at, run_active (any run without completed_at), last_run
  {status/error_detail/timestamps}, last_finished_run (most recent run WITH
  completed_at), consecutive_failures (streak of non-COMPLETED finished
  runs), manager_conversation {id, status} from the agent server via
  `workspaces.manager_conversation_id`, error}. Automation backend failures
  degrade to `error` (still 200) so the UI can show "unknown" instead of
  breaking. The SPA polls it every 15s and renders the topbar `#mgr-badge`.
  Precedence (failures FIRST — health is judged by `last_finished_run`, NOT
  `last_run`, because failing runs take ~70s each on a 1-min cron so a retry
  is almost always in flight and would otherwise mask the red state behind
  "polling"): unknown (backend error, red) / paused (disabled) / ✗ + time
  (last finished run failed, red, failure streak in the tooltip) / agent
  error|stuck (manager conversation execution_status, red) / working
  (manager conversation running, pulsing) / polling (run active) / ✓ + time
  (healthy). CSS variants `.mgr-badge.ok|.err|.paused`. Tests:
  `tests/test_automation_status.py` (stub automation backend on a local
  port via VIBE_AUTOMATION_API; run with the service venv — note tests
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
- **Visual language ("agent dispatch", 2026-05-21 overhaul)**: the board is
  designed as a dispatch console. The governing rule is **hue encodes lane,
  controls stay neutral** — each column owns a signal colour
  (`--lane-pending` steel / `--lane-progress` teal / `--lane-input` amber /
  `--lane-done` iris / `--lane-verified` green) exposed to its cards through
  the inherited `--lane` custom property set on `.col[data-status=...]`, so a
  card's left rail, the column's top rail and its count all share one hue.
  Primary buttons are neutral chalk-on-ink (`--btn-bg`/`--btn-text`), and the
  single `--flare` (the workspace's primary colour, orange by default) is
  reserved for focus rings, the brand mark and drag drop indicators.
  Signature element: `.card.live` (added by app.js when
  an in_progress ticket has a `latest_action`) animates a light travelling
  down the lane rail while the telemetry line types out with a caret.
  Type: Archivo (variable `wdth` axis, used expanded + uppercase for lane
  signage, brand and buttons), Public Sans for body copy, Azeret Mono for
  ids/timestamps/telemetry. Spacing uses the `--s1..--s7` scale.
  Non-negotiables: font sizes stay in `rem`, `[hidden] { display: none
  !important }` sits near the top of style.css (many components are
  `display:flex`, which otherwise defeats the `hidden` attribute the SPA
  toggles), empty lanes render `.lane-empty` invite copy from `LANE_EMPTY`
  in app.js, cards are keyboard-operable (`role=button` + tabindex, Enter/
  Space opens the drawer, focus returns to the card on close) and
  `prefers-reduced-motion` kills the animations.
  - User trims (2026-05-21, same ticket): the page background is FLAT
    `var(--ink)` — no radial-gradient glows (the `--glow-*` tokens are gone);
    the topbar carries no "Workspace" or "Landing" labels (those controls are
    self-evident and keep only `aria-label`s), the only visible topbar label
    is "Max agents", set inline to the LEFT of the number input; and the
    composer shows no "Enter sends…" keyboard hint. Don't reintroduce any of
    them.
- **Theming (light/dark)**: dark is the default; light mode is a full CSS-
  variable override under `html[data-theme="light"]` in static/style.css. ALL
  colors in the stylesheet must be var() tokens defined in `:root` (incl.
  border/glow/backdrop/lane tokens like `--lane-input`, `--flare-ring`,
  `--topbar-bg`, `--btn-text` for text on solid neutral buttons) ŌĆö
  never hardcode a hex/rgba outside the two token blocks, or light mode
  breaks. The topbar `#theme-toggle` button flips the theme
  (`applyTheme`/`toggleTheme` in app.js), persisted as the workspace's `theme`
  setting (see Workspace settings); localStorage `vibe.theme.hint` is only the
  paint-time mirror an inline `<script>` in index.html's head applies before
  first paint to avoid a flash.
  - Light palette is deliberately soft (d7a46d5, restated in the 2026-05-21
    overhaul): paper surfaces tinted with the workspace's primary rather than
    white (no pure #fff cards or fields), low-opacity shadows/glows, eased
    near-black text, desaturated lane colours. Keep new light-mode tokens
    muted to match — "less jarring" is a user requirement.
- **Primary colour per workspace (2026-05-21)**: each workspace picks one of
  exactly TEN primaries — ember, amber, citron, jade, teal, azure, iris,
  orchid, rose, slate — and the whole theme shifts with it, in both modes.
  - The primary IS the background, not a tint on top of one (2026-08-30: the
    first cut washed a fixed neutral base with the accent and the user could
    not tell the palettes apart). `:root` declares `--accent-<name>` for all
    ten plus `--accent` (default ember), and EVERY surface/line/text/control
    token is that hue pinned to a step of a tonal ramp:
    `oklch(from var(--accent) <L> <C> h)` — only the hue comes from the
    primary, so all ten palettes are one design in ten hues and no palette is
    lighter or louder than another. Dark runs L .205 (app background) → .42
    (borders) with chalk text at L .95; light mirrors it, L .935 paper → .30
    text. A palette switch changes one declaration,
    `html[data-accent="<name>"] { --accent: var(--accent-<name>); }`, and the
    light block restates the ten primaries darker so they still carry as a
    flare on paper (only their hue reaches the surfaces).
    - Keep `--tone-bg` (the app-background step, one per mode) as a token: the
      picker swatches fill themselves with `oklch(from var(--swatch)
      var(--tone-bg) h)`, so a swatch is a preview of the background it gives
      you rather than a sticker that might drift from it.
    - Relative colour syntax (`oklch(from …)`) is required; it is a hard
      dependency of the theme, not a progressive enhancement. Baseline since
      2023 (Chrome 119, Safari 16.4, Firefox 128).
    - Lanes (`--lane-*`) are deliberately NOT accent-derived: hue encodes
      status and must not collide with the primary. `--flare` IS the accent
      (it used to be a fixed orange; ember reproduces it).
    - When retuning the ramp, check all twenty combinations at once rather
      than eyeballing one: a throwaway headless-Chromium script that resolves
      every token to sRGB per accent and prints WCAG ratios is the fast loop
      (the shipped ramp holds text/ink ≈15 dark and ≈11 light, dim/card ≥5,
      lane rails ≥3.7 on card). Uniformity across the ten is the point.
  - Storage is per workspace, next to the other workspace settings:
    `workspaces.accent` in vibe.db (migration adds the column, default
    `ember`, PATCH `/api/workspaces/<id>` validates against `ACCENTS` in
    app.py → 400) and the `accent` field on the index.json workspace record
    for the Canvas extension (`DEFAULT_ACCENT` in `src/store.js`).
  - UI: a `.control-accent` popover (`#accent-toggle` + `#accent-menu`, ten
    `.accent-swatch` buttons, role=menuitemradio, Escape/outside click closes)
    sitting immediately after `.control-workspace` in the topbar — the colour
    belongs to the workspace, so it lives next to it — in BOTH
    static/index.html and the extension's `src/markup.js`. The toggle is a
    bare dot in the current primary with NO text label (user asked for the dot
    alone); its accessible name comes from `aria-label`, and the palette name
    from `title`. `applyAccent()` writes `data-accent` — on `<html>` in the
    SPA, on the `.vibe-ext` root in the extension (same scoping rule as the
    theme). The SPA also mirrors the choice into localStorage `vibe.accent`
    purely as a paint-time hint for the head script.
  - The ten names live in three places that must stay in step: `ACCENTS` in
    app.py (validation), `ACCENTS` in static/app.js and extension.js (the
    picker), and the `--accent-<name>` tokens + `.accent-swatch[data-accent]`
    rules in style.css. Tests: `tests/test_workspace_accent.py` and the three
    primary-colour cases in `extensions/kanban-manager/test/extension.test.js`
    (theme applied and written back, default fallback, picker shape/position).
- `static/` ├óŌé¼ŌĆØ vanilla JS SPA (no build step). Kanban columns: pending,
  in_progress, needs_input, finished. Drag vertically to reprioritize; click
  a card for the append-only drawer; each card links to its conversation.
  Finished cards have a "mark verified" button (`POST
  /api/tickets/<id>/verify` sets status=verified + verified_at); verified
  tickets leave the board and appear in a fifth column (most recently
  verified first) toggled by the top-bar "show verified" button. The
  automation ignores verified tickets entirely (terminal state).
- **Finished column ordering**: tickets carry a `finished_at` timestamp,
  stamped by the manager PATCH endpoint on the transition INTO finished
  (idempotent re-PATCH keeps it; a reopen + re-finish refreshes it; the
  migration backfilled existing rows with COALESCE(verified_at, updated_at)).
  The SPA sorts the finished column by finished_at desc — most recently
  finished first, like verified — and finished/verified cards are NOT
  draggable (priority drag applies only to pending/in_progress/needs_input).
  Tests: `tests/test_finished_order.py`.

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
  - **Freshness (2026-05-21)**: `_sync_project_checkout` runs before every
    worktree is added — it resolves origin's default branch (`origin/HEAD`,
    auto-set via `git remote set-head origin --auto` when missing, e.g.
    `--single-branch` clones), `git fetch --prune origin`, and then
    fast-forwards the PROJECT checkout (`_fast_forward_project`) so it stops
    drifting behind origin. The fast-forward is skipped when the checkout is
    dirty, detached, or on a non-default branch — never forced. A failed
    fetch now raises 502 instead of silently falling back to local HEAD, so a
    worker can't start from a stale base. Manager conversations
    (`worktree: false`) get the same refresh best-effort via
    `_refresh_workspace_checkout` (failures logged, never block dispatch).
    The manager prompt also tells workers to fetch+rebase right before
    pushing (automation/main.py `push_instructions`).
- Every conversation started via `/api/manager/conversations` gets tags:
  `workspace=<project path>` (so the canvas UI groups it under the right
  workspace ŌĆö the worktree working_dir alone is NOT enough) and
  `viberole=worker|manager|manager_chat`. Follow-ups to an existing
  conversation retro-tag it if the `workspace` tag is missing (self-heals
  pre-tagging conversations).
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
  chores) because profile names change. A model the user picked on the request
  (see Request settings) overrides all of this. Tests:
  `tests/test_llm_profiles.py`.
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
  POST /api/workspaces) to re-upload the tarball to existing automations —
  and re-run the extension build, whose bundle embeds `automation/*.py` too
  (see Canvas Extensions).

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

## Canvas Extensions

The board now also ships as a Canvas Extension in `extensions/kanban-manager`
(see that package's README). Canvas extensions are **frontend only** (host
API = `registerPage` / `navigate` / `agentServer.request`; no extension-owned
server code), which is why the FastAPI service could not come along.

> **Renamed 2026-05-21 (user request): `vibe-board` → `kanban-manager`.**
> Manifest name, package dir, page route (`/extensions/kanban-manager/board`)
> and install path (`~/.openhands/canvas-extensions/installed/kanban-manager`)
> all moved; the upstream copy at DevinVinson/canvas-extensions was renamed in
> the same change. What deliberately did NOT move: the store root
> (`~/.openhands/vibe-manager`, shared with the automation's shell writers),
> the `.vibe-ext` CSS scope class, and the automation name
> (`Vibe Manager — <ws> (<id>)`, which app.py also bootstraps). Renaming any of
> those orphans live boards or duplicates automations.

**The extension no longer talks to app.py at all.** Board state is JSON on
disk under `~/.openhands/vibe-manager/` (`index.json` +
`workspaces/<id>/tickets/<ticket_id>/ticket.json`), read and written through
the agent server's file API — so there is nothing to configure and no second
service to run:

- `src/store.js` (`Store`) owns all reads/writes. It resolves the store root
  at runtime from `GET /api/file/home` (field is **`home`**) — never hardcode
  `/root`. Paths:
  `<home>/.openhands/vibe-manager/workspaces/<id>/tickets/<tid>/ticket.json`.
- **One file per ticket** (2026-08-29, ticket 3f191ab9a7ff). There is no
  `board.json` any more: `readBoard` lists `tickets/` via
  `GET /api/file/search_subdirs` and reads each ticket in parallel. A ticket
  is a DIRECTORY holding `ticket.json` because the file API can enumerate
  subdirectories but has no endpoint that lists files — the directory is what
  makes the board reconstructible. Workspaces predating the split still read
  the legacy `board.json` when `tickets/` 404s;
  `scripts/migrate_per_ticket.py` performs the split (idempotent, renames the
  old file to `board.json.migrated`). See `store/SCHEMA.md`.
- `src/live.js` (`Live`) decorates tickets with `latest_action` / `llm_model`
  and builds `conversation_url` as an **in-app relative** `/conversations/<id>`;
  the UI routes those through the host's `navigate` rather than a new tab.
- Shell-only work (dispatch, git worktrees, mutating the store from the
  manager) lives in the automation: `automation/vibestore.py` +
  `automation/vibectl.py`, which must run in a completely bare environment.
  - **The CLI is installed per workspace**, at
    `<store>/bin/<ws_id>/vibectl.py` with its own `config.json`
    (`install_cli`). It used to be one shared `bin/config.json`, which every
    workspace's cron rewrote once a minute: a manager's `snapshot` then
    returned another project's board and `patch` failed with "ticket not
    found" (observed live 2026-08-29). The manager's shell inherits no
    environment, so the workspace has to come from a file — it just must not
    be a file the other workspaces share. Nothing may resolve "the current
    workspace" from a shared default; `install_cli` deletes the legacy shared
    config so an old CLI errors out instead of guessing. Tests:
    `python3 tests/test_vibectl_workspace_isolation.py`.
- Ticket text is in `entries[0].body`; there is no top-level `body` field.
- **`/api/file/download` sends ETag/Last-Modified but no `Cache-Control`**, so
  browsers heuristically cache it (~10% of the file's age). Both `readJson`
  AND the `search_subdirs` listing defeat this per read with a `_=<now>` param
  + `Cache-Control: no-cache`; keep it that way. A cached LISTING is the worse
  of the two — it omits a ticket directory created moments earlier, i.e. the
  card never appears at all. Attachment blobs are immutable and stay cacheable.
- **The layout, not a locking protocol, is what keeps tickets safe.** The
  whole-board `board.json` could not be made safe: every actor (extension,
  the once-a-minute automation, the manager's `vibectl`) read-modify-wrote it,
  and the file API has no conditional upload to build a compare-and-swap on —
  an upload carrying a stale precondition is accepted, not rejected. Measured
  over a 40-run interleaving sweep, the manager's edit was lost 35/40 times;
  adding `rev`/`writer` only got that to 8/40, because a writer can confirm
  its own write landed without noticing it destroyed someone else's. Per-ticket
  files remove the shared document entirely: 0/40. **Do not reintroduce any
  whole-board write.**
- **`rev`/`writer` remain as a backstop** for the two documents still shared —
  `index.json`, and a single ticket two writers both target. `Store.mutateDoc`
  and `vibestore._mutate_document` read → mutate → re-read and start over if
  `rev` moved → write → re-read and re-apply if someone else's write won; the
  shell side also flocks `<document>.lock`. Any new writer MUST keep both
  fields and bump `rev`. Treat this as detection, not prevention.
- **Writes are still serialized within a tab** (`Store.serialize`, a promise
  chain) so two of this tab's own cycles queue instead of racing — notably so
  concurrent submits pick distinct `sort_order` slots. Anything a mutation
  calls must therefore NOT call `serialize` again: `createTicket` delegates to
  an unserialized `createTicketNow`, and `appendEntry` reads the pending tail
  BEFORE entering `mutateTicket`, or it would deadlock on its own queue.
  Tests: `node --test extensions/kanban-manager/test/store.test.mjs` (drives the
  real file API AND the real `vibestore.py` as the racing writer) and
  `python3 tests/test_board_store_concurrency.py`.
- **A board read that spans a write is discarded** (`refreshBoard` compares
  `store.writes` before/after): the 5s poll is usually in flight when the user
  submits, and that response predates the new ticket, so rendering it hides
  the card until the next poll. The write path does its own refresh.
- `vibectl.py`'s `--workspace-id` / `--working-dir` are *global* options, so
  they must come BEFORE the subcommand (`vibectl.py --workspace-id X
  snapshot`, not `vibectl.py snapshot --workspace-id X`, which argparse
  rejects).
- **A `patch` that fails with "ticket not found" can simply mean the ticket
  was deleted between snapshot and patch.** Seen live 2026-08-29 on the
  vibe-manager board: an external e2e probe created ticket d5c2b4fd058e, the
  manager dispatched a worker for it, and the probe deleted
  `tickets/<id>/ticket.json` ~2 min later, leaving an orphan directory holding
  only `ticket.json.lock`. `read_board` skips such directories, so the card
  just disappears. Before assuming a store bug, check whether the ticket
  directory still has a `ticket.json`.

- **The extension creates its own manager automation** (`src/manager.js`,
  user request 2026-05-21) — the last thing that still needed app.py's
  `ensure_manager_automation`. The top-right control is orange **"Start
  manager"** whenever the workspace has no automation, points at one the
  backend 404s on (`missing` in `live.automationStatus`), or has one that is
  disabled; next to a live manager sits **"Stop"**
  (`PATCH {"enabled": false}` — the automation is kept so its run history and
  id survive a restart). Details worth knowing before touching it:
  - Status and start/stop are **one button group** (user request 2026-05-21):
    `#mgr-group` is the pill (border, radius, `overflow: hidden`) and its
    children are segments — `#mgr-badge` (status; click = start or trigger)
    and `#mgr-stop`, which has only a divider for a border. So the group, not
    the badge, carries every border colour (`:has(.mgr-badge.err)`,
    `:has(.mgr-badge.start)`), and hiding the control means hiding
    `#mgr-group` as well as the badge. `#mgr-stop` is extension-only, so its
    segment styles live in `src/setup.css` (pre-scoped) while the group and
    badge are in `static/style.css` with the rest of the design.
  - `build.mjs` compiles `automation/*.py` into the bundle
    (`__VIBE_AUTOMATION__`, same trick as `__VIBE_CSS__`), because the Canvas
    machine has no vibe-manager checkout. `automation/` stays the source of
    truth; the bundle grew ~90 KB → ~151 KB. **Re-run the build after editing
    automation/** or Start manager ships stale code.
  - The tarball is tar+gzip'd in the browser (`tar()` + `CompressionStream`).
    Sizes in the ustar header are BYTE counts — `main.py` is full of em
    dashes, and a character count truncates the file and desynchronises every
    later header. `test/manager.test.mjs` checks the archive with real GNU tar
    and uploads it to the real automation backend.
  - `POST /v1/uploads` must be a **raw `fetch`** with the backend credentials
    from `resolveBackendCredentials`: the host client JSON-stringifies every
    body that isn't `FormData`, which corrupts the gzip bytes. (Its 1 MB cap
    is no problem — the archive is ~15 KB.) The JSON calls around it go
    through `host.agentServer.request` like the rest of `/api/automation/v1`.
  - Start reuses an automation with the same name (`Vibe Manager — <ws>
    (<id>)`, `automationName()`), so a workspace app.py already bootstrapped
    is refreshed rather than duplicated, and always re-uploads the tarball.
  - `config.json` carries `store_dir` (the resolved store root), so the
    automation writes the same board the tab does even if it runs as another
    user. `agent_server` is only a fallback: the run environment's
    `AGENT_SERVER_URL` wins.

Operational notes for the extension:

- **Build tooling lives in `extensions/`, one level ABOVE `kanban-manager/`.**
  Installing from a local path copies the package directory verbatim, so a
  `node_modules/` inside `kanban-manager/` lands in the agent-server install
  (38 MB vs 164 KB — this was hit and fixed). `cd extensions && npm test`.
  `npm test` builds and runs the `*.test.js` bundle tests ONLY; the `*.mjs`
  suites (store, live, manager) hit the real agent server and automation
  backend, so run them explicitly:
  `cd extensions && node --test kanban-manager/test/*.mjs`. They write to a temp
  store root (`mkdtemp`), never the real `~/.openhands/vibe-manager` — keep it
  that way, they create and delete workspaces. A `Store` built without an
  explicit root defaults to the REAL store, so any harness that forgets to
  pass `ROOT` silently pollutes it: nine `cache-*` workspaces with `/git/...`
  paths were found and removed on 2026-08-29. If a workspace shows up whose
  `path` does not exist on disk, that is what it is.
- `extensions/kanban-manager/dist/extension.js` is **committed**: the agent-server
  installs by copying files and never runs a build.
- **`automation/vibestore.py` is copied into three places, and editing the
  repo copy alone changes nothing at runtime.** Deploying a store change means
  all of: (1) reinstall the extension (`POST
  /api/canvas-extensions/install` with `"force": true`, which recopies
  `dist/extension.js` into
  `~/.openhands/canvas-extensions/installed/kanban-manager/`), (2)
  `python3 scripts/push_automation.py` to re-tarball every workspace's
  automation, and (3) refresh `<store>/bin/<ws_id>/vibestore.py`, the
  per-workspace manager CLI copy — it is written at install time and does NOT
  follow the repo. Skipping (3) leaves `vibectl snapshot` crashing on the new
  layout while the automation itself succeeds.
  - **Deploy is standing policy (user request 2026-05-21), not a per-ticket
    ask**: every time a change lands on origin/master the manager runs the
    full deploy below AND republishes the extension to
    DevinVinson/canvas-extensions — push the file copy to the existing open PR
    branch (`rename-to-kanban-manager`), opening a new PR only if that one is
    closed/merged.
  - Full deploy of a store/automation change, in order (2026-08-30): push to
    origin/master → in `/root/git/vibe-manager` `git pull --ff-only` (or reset
    to origin/master once its content is verified contained) and
    `systemctl restart vibe-manager.service`, check `/api/health` → `python3
    scripts/push_automation.py` → `cp automation/vibe{store,ctl}.py` over every
    `<store>/bin/<ws_id>/` (and the legacy shared `<store>/bin/` pair) →
    reinstall the extension. Verify the store end-to-end with
    `python3 <store>/bin/<ws_id>/vibectl.py snapshot`, which reads the LIVE
    board through the deployed CLI.
  - `tests/test_request_settings.py` calls `install_cli` without redirecting
    the store, so it writes a stray `<store>/bin/<random_id>/` (workspace path
    under /tmp) into the REAL store; one was removed 2026-08-30. Harmless but
    delete it, like the `cache-*` workspaces.
  - A first install lands **disabled**, so an install never silently swaps the
    board out from under the user — tell them to enable it in
    Customize → Extensions. A `force` reinstall of an already-enabled
    extension KEEPS it enabled (observed 2026-08-30: the response came back
    `"enabled": true` and the served bundle was the new one immediately), so
    shipping a bundle change needs no action from the user beyond a reload.
    The old `vibe-board` install (still enabled, still reading `board.json`)
    was uninstalled with
    `DELETE /api/canvas-extensions/installed/vibe-board` on 2026-08-30.
  - The GitHub token for upstream pushes comes from
    `GET /api/settings/secrets/GITHUB_PERSONAL_ACCESS_TOKEN` on the agent
    server (session key header) and is returned as **plain text**, not JSON.
- `static/style.css` stays the single source of truth. `build.mjs` scopes every
  selector under `.vibe-ext` and rewrites `html`/`:root`/`body` and
  `html[data-theme="light"]` onto that root, so the extension can't restyle
  Canvas. It also rewrites every `rem` to `calc(N * var(--vibe-rem))` with
  `--vibe-rem: 1.2rem`, replacing the SPA's `html { font-size: 120% }` (an
  extension must not resize the host page). A test asserts 0 unscoped
  selectors — if you add a global selector to style.css, expect it to fail.
- `static/index.html`'s body is duplicated in `extensions/kanban-manager/src/markup.js`.
  **Change both.**
- No CORS is involved any more: every request goes through the host's
  `agentServer.request`, same-origin with Canvas.
- Install: `POST /api/canvas-extensions/install {"source": "<abs path>"}`
  (add `"force": true` to reinstall). Installs always land **disabled**;
  enable from Customize → Extensions. Bundle is served at
  `/api/canvas-extensions/installed/<name>/bundle`. These endpoints live on
  the **agent server** (18000, `X-Session-API-Key`), not the Canvas static
  server — the ingress rejects the agent-server key on its own routes.
  Verify a deploy by diffing the served bundle against `dist/extension.js`.
- **Check the store layout before installing a bundle.** The installed bundle
  is the only reader the user's board has, so one built from a tree that
  predates a store migration empties the board. Compare what is on disk
  (`workspaces/<id>/tickets/` per-ticket files vs. a single `board.json`)
  against the `src/store.js` you are about to ship, and never install from a
  checkout whose extension sources are older than the live data — observed
  2026-05-21, when master's extension still read `board.json` while the
  deployed board had already migrated to `tickets/`.
- Page route is `/extensions/<extension-name>/<page path>`, e.g.
  `/extensions/kanban-manager/board` — NOT the bare `/board` from the manifest.
- Testing the bundle under linkedom: it has no setter for
  `HTMLSelectElement.value`, so `installDom()` adds one; without it the mount
  throws and the board silently never renders. Drive tests through a
  programmable `host.agentServer.request` double (see `hostWithStore`) and
  keep fixtures in the real payload shapes — a wrong fixture here looks
  exactly like a product bug.
- The agent-server manifest schema has no `nav_label`/`description` on pages
  and silently drops them; the frontend falls back to `title`.
- **The extension is also published to DevinVinson/canvas-extensions** (PR from
  the `rbren/canvas-extensions` fork; `vibe-board/` there became
  `kanban-manager/`). That copy must stand alone, so `src/board.css` and
  `src/automation/*.py` are vendored copies of `static/style.css` and
  `automation/`, and it keeps the repo's convention of the bundle at the
  package root with deps in the package itself. To avoid a second build.mjs,
  the one here looks for each input in both places (`vendored()`) and takes
  its output path from the manifest's `entrypoint` — so publishing is a file
  copy plus `entrypoint: "extension.js"` and a package.json, with no code
  edits. Re-publish by copying `src/`, `test/`, `build.mjs` and the manifest
  again; `npm run check` there rebuilds and reruns the suite.
  - Concretely (done 2026-08-30): clone `rbren/canvas-extensions`, check out
    the PR branch `rename-to-kanban-manager` (PR
    DevinVinson/canvas-extensions#2), then copy
    `extensions/kanban-manager/src/*.js` → `kanban-manager/src/`,
    `static/style.css` → `src/board.css`, `automation/{main,vibestore,
    vibectl}.py` → `src/automation/`, `test/{extension.test.js,
    manager.test.mjs}` and `build.mjs`. Push to that branch — never open a
    second PR. Do NOT copy the repo's `canvas-extension.json` (its entrypoint
    is `dist/extension.js`; upstream's is the package root) and do NOT copy the
    repo README — upstream's is the user-facing one, written for publication.
    Only `extension.test.js`/`manager.test.mjs` are published; `store.test.mjs`
    and `live.test.mjs` need a live agent server. Every new `src/` module has
    to be copied too or the bundle won't build (`managerchat.js` was new here).

## Canvas Extensions research (2026-05-21)

Investigated converting vibe-manager into a Canvas Extension (openhands
v1.16.0 `feat: land the Canvas Extensions frontend`, 89dc8bd44). Findings,
verified against source + live API probes on this host:

- **The extension contract**: manifest `canvas-extension.json` (schema_version
  1) + ONE self-contained browser ESM bundle exporting `activate(host)`. Spec:
  `openhands/specs/canvas-extensions.md`; types:
  `openhands/src/types/canvas-extension.ts`; runtime:
  `src/components/features/canvas-extensions/canvas-extensions-runtime.tsx`.
  Backend side lives in software-agent-sdk
  (`canvas_extensions_router.py` + `canvas_extensions/installed.py`),
  installed under `~/.openhands/canvas-extensions/installed/<name>`.
- **Extensions are FRONTEND-ONLY.** The host API is just `registerPage`,
  `navigate`, and `agentServer.request({method, path, body})`. There is NO
  hook to run extension-owned server code, so vibe-manager's FastAPI process
  cannot come along; its logic must move into the bundle or the automation.
- **`agentServer.request` targets the registered backend host**, and only
  root-relative paths (it throws on non-`/` and on `//`). Refined 2026-05-21:
  because that host is the *ingress* (:8000), which routes `/api/automation/*`
  to the automation backend (:18001) and shares one session key with the agent
  server, `/api/automation/v1/...` DOES work through the host client — that is
  how `live.js` and `manager.js` talk to it. The one thing it cannot carry is
  a binary body: `HttpClient` JSON-stringifies anything that isn't `FormData`,
  so tarball uploads and attachment downloads issue their own `fetch` with the
  credentials from `resolveBackendCredentials`.
- **There is no KV/database in the agent-server.** Its only persistence is
  purpose-built file stores (`persistence/store.py`:
  settings/secrets/workspaces/profiles) — no generic key-value endpoint
  (`git grep` for kv/extension_state finds nothing).
- **The automation KV store cannot serve as the extension's database.** It
  exists (`openhands/automation/kv_router.py`, `/v1/kv/...`, Redis-like
  get/put/patch/incr/lpush) but auth is a per-RUN JWT (`AUTOMATION_KV_TOKEN`,
  minted only by `dispatcher.py` when a run starts). A session key is rejected
  (verified: 422 / "Invalid token: Not enough segments"), so the browser can
  never read it. It is also a single encrypted JSON doc per automation with a
  64 KB default value cap — our dj-station board alone is ~87 KB of JSON.
- **The file API is the viable native backend.** Verified working with the
  session key: `POST /api/file/upload?path=` (multipart, absolute path),
  `GET /api/file/download?path=` (404 on missing), `POST
  /api/file/create_directory?path=`, and `GET /api/file/search_subdirs?path=`
  — the last one gives *enumeration*, so a directory-per-record layout
  (`.../tickets/<id>/record.json`) is listable without an index file.
  Gaps: there is no delete and no rename in the file API, and upload
  truncates-then-streams (not atomic). Both are covered by
  `POST /api/bash/execute_bash_command` (verified: `mv` round-trips in ~0.2 s;
  the first call after an idle period takes ~60 s to warm up, so don't put a
  cold bash call on a UI-blocking path).
- **Data is small enough for JSON on disk**: 3 workspaces / 109 tickets /
  203 entries; largest single board is ~87 KB of JSON. Single-user, so
  read-modify-write of a per-workspace document is fine; use write-temp +
  `mv` via the bash API for atomicity.
- The automation half of vibe-manager (`automation/main.py`) needs almost no
  change: it already runs server-side with a KV token and can keep using its
  own KV state, and it can read/write the same board JSON through the file API.
