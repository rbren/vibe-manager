# kanban-manager — Agent Canvas extension

A kanban board for agent work, as a page inside Agent Canvas. Queue requests in
`pending`, let a **manager agent** dispatch worker conversations for them, and
watch the cards move through `in progress`, `needs you`, `finished` and
`verified`.

Formerly `vibe-board`.

## What it is (and isn't)

Canvas Extensions are **frontend only** — there is no hook for extension-owned
server code. So the board keeps its state as JSON on the agent server's disk
(`~/.openhands/vibe-manager/index.json` plus `workspaces/<id>/board.json`),
read and written through the agent server's file API. There is no second
service to run and nothing to configure: the extension uses the backend Canvas
is already connected to.

The work itself is done by two kinds of agent conversation, both started
against your real workspace:

- a **manager**, run once a minute by an automation, which decides what to
  dispatch, keeps card status current and titles tickets;
- **workers**, one conversation per ticket, each in its own git worktree.

## The manager

The top-right control owns the manager automation for the selected workspace:

- **Start manager** (orange) — no automation exists yet, or it was stopped.
  Clicking it packs the automation's python sources (compiled into this bundle)
  and a per-workspace `config.json` into a tar.gz, uploads it to
  `POST /api/automation/v1/uploads`, and creates a cron automation that runs
  `python3 main.py` every minute. The id is recorded on the workspace.
- **manager ✓ / working / ✗** — the automation is running; the badge reports
  the last run and the manager conversation, and clicking it dispatches a run
  right now.
- **Stop manager** — `PATCH {"enabled": false}`. The automation and its run
  history are kept, so starting again is a re-enable (with a fresh tarball).

The tarball upload is a raw `fetch` rather than `host.agentServer.request`: the
host client JSON-stringifies every body that isn't `FormData`, which would
corrupt the gzip bytes. Everything else goes through the host client.

## Build

Build tooling lives one level up, in `extensions/`, **not** in this package
root: installing from a local path copies the package directory verbatim, so a
`node_modules/` here would be copied into the agent-server install (38 MB of
build-only dependencies for a ~150 KB bundle).

```sh
cd ..             # extensions/
npm install
npm run build     # -> kanban-manager/dist/extension.js (self-contained browser ESM)
npm test          # builds, then runs the tests against the built bundle
npm run validate  # builds, then runs the Canvas extension validator
```

`npm test` covers the bundle only. The store, live-derivation and manager
suites talk to the real agent server and automation backend, so they are run on
their own:

```sh
node --test kanban-manager/test/store.test.mjs \
            kanban-manager/test/live.test.mjs \
            kanban-manager/test/manager.test.mjs
```

`dist/extension.js` is committed, because the Agent Server installs an
extension by copying files from a path or git ref and never runs a build.

## How it relates to `static/` and `automation/`

The bundle vendors two things from the vibe-manager repo at build time, so
those stay the single source of truth:

- `static/style.css`, with two transforms. **Scoping**: every selector is
  prefixed with `.vibe-ext` and the page-level selectors (`:root`, `html`,
  `body`, `*`) are rewritten onto that root, so the light-mode toggle can never
  restyle Canvas; a test asserts that zero selectors escape the scope.
  **Text scale**: the SPA sets `html { font-size: 120% }` to size itself, which
  an extension must not do to the host page, so every `rem` becomes
  `calc(N * var(--vibe-rem))` with `--vibe-rem: 1.2rem` on our own root.
- `automation/*.py`, the manager automation, because "Start manager" uploads it
  from the browser and the Canvas machine has no checkout to read it from.

`static/index.html`'s body is ported to `src/markup.js`. When you change the
markup in one, change it in the other. Two controls are extension-only: the
`#api-setup` screen and `#mgr-stop`. `src/setup.css` holds their styles and is
written pre-scoped.

## Install

Installation leaves an extension **disabled**; enable it from
Customize → Extensions after reviewing it.

```jsonc
// POST /api/canvas-extensions/install   (agent server, X-Session-API-Key)
{ "source": "/root/git/vibe-manager/extensions/kanban-manager" }
```

The path is resolved **on the Agent Server machine**; add `"force": true` to
reinstall. It lands in `~/.openhands/canvas-extensions/installed/kanban-manager`
and is served at `/api/canvas-extensions/installed/kanban-manager/bundle`.

## Known limitations

- **Deep links** are `/extensions/kanban-manager/board/<workspace-name>`; the
  standalone SPA's `/workspace/<name>` URLs don't carry over.
- The store path stays `~/.openhands/vibe-manager` under the new name, so
  boards written by earlier installs — and by the automation, which writes the
  same files from the shell — are still found.
