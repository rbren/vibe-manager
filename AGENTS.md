# Kanban Manager

Agent dispatch board for OpenHands Agent Canvas. Source repository:
`rbren/vibe-manager`; standalone App publication: `OpenHands/canvas-apps/kanban-manager`.
Work only in the conversation's assigned worktree; never update the original
checkout as part of deployment.

## Architecture

- **Canvas protocol:** manifest schema 1, host API 1, one Blob-importable ESM
  `extension.js` exporting `activate(host)`. React and a backend do not require
  a new manifest protocol: the App explicitly installs a **FastAPI sidecar**
  during onboarding. Reference: DevinVinson/skills `canvas-extension-api`.
- `extensions/kanban-manager/src/app.jsx`: React App, onboarding, backend
  configuration, consent, start/stop/repair, migration and runtime warnings.
  Unknown readiness renders only loading/recheck, never onboarding. HTTP runtime
  includes safe installation metadata and migration state in one snapshot. Old
  backends remain inspectable/repairable through explicit Backend setup.
  React owns the lifecycle of the existing board interaction controller in
  `src/extension.js`; retain its behavior and scoped styles during refactors.
- `src/api.js`: data and startup readiness use only `host.agentServer.request`
  at `/kanban-manager/api/...`, an explicitly provisioned deployment gateway.
  Canvas supplies the selected backend's auth; no credential extraction or
  origin inference. HTTP/auth/HTML failures never fall back to command data RPCs.
  Only explicit setup/lifecycle and installation progress use `/api/file/home`
  and fixed base64/JSON commands. Approved installs stage bounded chunks before
  SHA-256 verification; no full-package shell argument or file-API persistence.
- `app.py`: shared FastAPI board/settings/ticket/manager/chat API and SQLite
  transactions. `sidecar/server.py` is the production sidecar entrypoint: random
  loopback port, per-process bearer token, authenticated health/shutdown, chunked
  attachments and migration/cutover routes. `sidecar/runtime.py` is the stdlib
  installer/lifecycle/allowlisted HTTP bridge. Tokens never leave the backend.
- State belongs under the owning Agent Server user's
  `~/.openhands/apps/kanban-manager/`: `vibe.db` (WAL), `attachments/`, `backups/`,
  `bin/<workspace-id>/`, `releases/<archive-sha256>/`, `venv/`, `current.json`,
  `integration.json`, `run.json`, `cutover.json`, and install/service logs.
  Root is private (0700), runtime/config files are 0600. Never put state beside
  the installed manifest. Stop/repair/disable do not delete data.
- Integration URLs and **credential file paths**, not secret values, are
  configured during onboarding. Backend credentials can alternatively be
  inherited (`SESSION_API_KEY`/`OH_SESSION_API_KEYS_0`,
  `OPENHANDS_AUTOMATION_API_KEY`). No guessed Automation service URL. Install
  creates a private venv from pinned requirements on official PyPI; no global
  packages, systemd modifications, or automatic system-level setup.
- `automation/main.py` remains the deterministic minute poller: budget stops,
  status reconciliation, fingerprints and capped manager retries. `vibestore.py`
  redirects to the sidecar when `VIBE_SIDECAR_ROOT` or the legacy store's
  `sidecar.json` marker is present. Existing per-workspace `vibectl.py` locations
  are refreshed at cutover, so agents do not keep writing retired JSON.
- `static/` and the legacy standalone FastAPI entrypoint remain for compatibility
  with the separately deployed website. Its SQLite DB is separate from the old
  Canvas JSON store. Do not overwrite or silently merge these databases; retain
  the standalone database/site unless an explicit separate cutover is requested.

## Portability and local policy

- Generic manager prompts discover profiles through `vibectl.py profiles`, select
  by capability/cost/effort, and preserve explicit user choices. No provider or
  profile allowlist belongs in published source. Empty/unknown discovery uses the
  active default for new workers and retains the current model for follow-ups.
- `automation.vibestore.manager_skill_prompt` loads optional operator context
  from `<sidecar-root>/skills/manager/SKILL.md` (default App data root), or
  `VIBE_MANAGER_SKILL_FILE`; empty override disables it. Only manager/manager_chat
  creation loads it, not workers or follow-up messages. Explicit unreadable paths fail. Local skill
  text is never packaged or uploaded in automation tarballs. Keep secrets out.
- This instance's model preferences and HTTP gateway/deployment runbook live in
  that private local skill. Read it when deploying here. Other installations
  provide their own gateway adapter; never infer its hostname, ports or auth.
- Service URLs and credential paths are explicit backend configuration. CLI
  configs carry URLs/paths, not keys. Profile discovery exposes name/model only;
  settings/profile credentials remain backend-only. Conversation summaries use
  the shared allowlisted API; manager chat never fetches credential exports.
- Persisted `push_mode="main"` is a compatibility value meaning direct push to
  the actual remote default branch, not a branch named main. Discover the
  remote HEAD at delivery time. Operator-rendered nginx/systemd examples are
  source-only, not installed by onboarding or published with the App.

## Migration and recovery

- `sidecar/migrate.py` imports the active legacy store
  `~/.openhands/vibe-manager/`: either per-ticket directories or `board.json`.
  Preserve workspace/ticket/entry/attachment IDs, timestamps, settings, ordering,
  conversation/automation links and immutable attachment bytes. Unknown JSON
  fields survive in `legacy_records`; original files are untouched.
- Onboarding requires closing old App tabs and approving migration. Cutover
  persists automation enabled states, pauses crons, waits for active runs and
  managers, backs up JSON and the destination SQLite DB, imports transactionally,
  redirects CLIs, refreshes automation tarballs and restores enabled states.
  Active runs produce an actionable retry message, not an unsafe force stop.
- Retrying an unchanged import never overwrites SQLite edits. Source changes
  during import roll it back. Changes after import are surfaced every 30s;
  reconcile preserved legacy records before importing again. Do not delete the
  source or backups or reset the import ledger to suppress a conflict.
- A cutover failure keeps its checkpoint and paused managers for a retry via
  **Migrate / finish cutover**. Keep old source and backups for manual recovery;
  do not resume old JSON writers against an already-migrated board.

## Behavior contracts

- Lanes: pending, in_progress, needs_input, finished, verified. User entries on
  finished/needs_input reopen to pending; verified is terminal. Entries append,
  never replace. Finished/verified sort newest first and are not draggable.
- Unfinished-project circles sit LEFT of the picker, exclude the active project,
  use each source workspace's primary colour, and show a real hover/focus tooltip
  (never native title). Preserve this correction from origin/master.
- Preserve workspace picker names-only, unfinished-project circles, URL deep
  links, keyboard drawer controls, 25 MiB attachments, spend/model/action chips,
  max agents, request agent/budget settings, manager start/stop/trigger and chat.
  Do not restore the obsolete top backend connected/setup status strip.
- All preferences are workspace fields. Browser storage holds only paint/route
  hints, namespaced by backend; legacy hints are read for continuity.
- `static/style.css` is the design source; build scopes it under `.vibe-ext`.
  Ten primary palettes, dark/light themes, neutral controls and lane hues remain.
  Use theme tokens and rem units; no global Canvas CSS or toast notifications.
- Pass `/conversations/<encoded-id>` to Canvas navigate, never a backend-returned
  absolute URL (React Router treats it as a relative path). Keep native link hrefs
  and modified/middle-click behavior intact.
- Conversations carry `workspace: {kind: LocalWorkspace, working_dir: PROJECT}`
  plus `workspace` and `viberole` tags. Workers get dedicated git worktrees;
  `worktree:false` prevents Agent Server rewriting their project association.
- One conversation per ticket; follow-ups reuse only that ticket's conversation
  and include new context, not its whole history. Manager-chat has its own
  `manager_chat` role, records requests under one `## Manager` section in project
  AGENTS.md, and never dispatches workers or claims the cron manager slot.
- User-selected models win. Budget stops occur once per
  conversation; manager comments never count as new user requests. Failed manager
  retries are capped at three per unchanged fingerprint.
- Manager notes are status-only one-liners (except deferral reasons/questions).
  Ticket titles: emoji plus one or two words. Report-back tickets go to
  needs_input for user review, including direct-push mode.

## Checks and delivery

- `python tests/test_sidecar.py`: real migrations, SQLite concurrency, HTTP auth,
  binary uploads/range downloads, restart persistence, legacy CLI redirection.
- Run each `tests/test_*.py` separately, setting `VIBE_STORE_DIR` to a temporary
  directory. Use synthetic `VIBE_SESSION_KEY=test-key` and
  `VIBE_AUTOMATION_KEY=test-key` for the legacy HTTP fixture tests; never copy
  real credentials into the worktree or send them to test services.
  `tests/test_portability.py` checks isolated defaults, local skill loading and
  package boundaries; rebuild the checked-in bundle before this test.
- `cd extensions/kanban-manager && npm ci && npm run check` (set `CHROME_PATH` to
  a real Chrome binary). Includes artifact validation, real bridge/backend UI
  tests and Chromium Blob smoke. The minimal Canvas host substitute executes
  real code, not simulated HTTP responses. Python dependencies must be available.
- Build first verifies `dist/extension.js`, then copies it to checked-in
  `extension.js`. `dist/` and node_modules are build-only, never published.
  `scripts/package_app.py <new-directory>` stages an independent App using an
  explicit source allowlist (no DBs, credentials, caches or environment files).
- Install staged App via the owning Agent Server's
  `POST /api/canvas-extensions/install` (`source`, `force:true`). A fresh install
  is disabled; reinstall preserves existing enabled state. Route stays
  `/extensions/kanban-manager/board[/<workspace-name>]`. Use App onboarding to
  install/update the backend; installing the UI alone does not start FastAPI.
- Rebuild/reinstall and repair backend after Python changes; refresh automation
  tarballs without enabling paused automations. Never restart Agent Server to
  deploy this App: that interrupts worker tool calls.
- Before direct push: fetch origin, resolve its actual remote default branch,
  rebase onto the latest tip, rerun relevant checks, push `HEAD:<default>`.
  Publish canvas-apps changes on a feature branch and exactly one PR per session;
  check any existing PR is open before updating it. Include AI-agent disclosure.
