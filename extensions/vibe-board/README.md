# vibe-board — Agent Canvas extension

The vibe dispatch board as a page inside Agent Canvas, instead of a separate
site. Same board, same API, rendered in Canvas's left-hand nav.

## What it is (and isn't)

Canvas Extensions are **frontend only** — there is no hook for extension-owned
server code. This package is the SPA from `static/`, rebuilt against the
extension ABI. It still talks to the **existing vibe-manager service** for
everything: tickets, workspaces, attachments, the manager badge. Running the
board in Canvas does not remove the need for that service.

## Build

Build tooling lives one level up, in `extensions/`, **not** in this package
root: installing from a local path copies the package directory verbatim, so a
`node_modules/` here would be copied into the agent-server install (38 MB of
build-only dependencies for a 66 KB bundle).

```sh
cd ..             # extensions/
npm install
npm run build     # -> vibe-board/dist/extension.js (self-contained browser ESM)
npm test          # builds, then runs the tests against the built bundle
npm run validate  # builds, then runs the Canvas extension validator
```

`npm test` covers the bundle only. The store and live-derivation tests talk to
the real agent server and automation backend, so they are run on their own:

```sh
node --test vibe-board/test/store.test.mjs vibe-board/test/live.test.mjs
```

`dist/extension.js` is committed, because the Agent Server installs an
extension by copying files from a path or git ref and never runs a build.

## How it relates to `static/`

`static/style.css` stays the single source of truth for the design: the build
reads it directly and applies two transforms so it can live inside Canvas.

1. **Scoping.** Every selector is prefixed with `.vibe-ext`, and the page-level
   selectors (`:root`, `html`, `body`, `*`) are rewritten onto that root. The
   SPA's `html[data-theme="light"]` becomes `.vibe-ext[data-theme="light"]`, so
   the light-mode toggle can never restyle Canvas. A test asserts that zero
   selectors escape the scope.
2. **Text scale.** The SPA sets `html { font-size: 120% }` to size itself; an
   extension must not resize the host page. Every `rem` becomes
   `calc(N * var(--vibe-rem))`, with `--vibe-rem: 1.2rem` on our root —
   the same visual result, contained. Change that one value to rescale.

`static/index.html`'s body is ported to `src/markup.js`. When you change the
markup in one, change it in the other.

`src/setup.css` is extension-only (see below) and is written pre-scoped.

## Talking to the API

The standalone SPA is served *by* vibe-manager, so it uses relative paths. The
extension runs on the Canvas origin, so it needs an absolute base URL:

- On first open the page asks for the vibe-manager base URL and probes
  `/api/health` before accepting it, so a typo fails immediately rather than on
  the first poll.
- The URL is stored per Canvas backend id, so pointing Canvas at another
  machine doesn't silently reuse the previous machine's board.
- Requests use `credentials: "include"` so the nginx basic-auth session rides
  along.

This requires the service to allow the Canvas origin — `app.py` sends CORS
headers for `VIBE_CORS_ORIGINS` (default: `VIBE_CANVAS_BASE` plus localhost).

## Install

Installation leaves an extension **disabled**; enable it from
Customize → Extensions after reviewing it.

```jsonc
// POST /api/canvas-extensions/install
{ "source": "/root/git/vibe-manager/extensions/vibe-board" }
```

The path is resolved **on the Agent Server machine**. To install from git
instead, use `{"source": "github:rbren/vibe-manager", "ref": "master",
"repo_path": "extensions/vibe-board"}` — note the repo is private, so the
backend needs credentials for that.

## Known limitations

- **The service is still required.** Only the UI moved.
- **Live action summaries** still come from the service's websocket watchers,
  polled over the board endpoint every 5s, exactly as before.
- **Deep links** are `/extensions/vibe-board/board/<workspace-name>`; the
  standalone SPA's `/workspace/<name>` URLs don't carry over.
