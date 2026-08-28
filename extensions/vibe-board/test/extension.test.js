/* Tests run against the BUILT bundle (dist/extension.js), not the sources, so
   they also cover the build: CSS scoping, the rem rescale, and the absence of
   bare imports are all properties of the artifact Canvas actually loads.

   Run: npm test  (after npm run build)
*/

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { after, before, describe, it } from "node:test";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseHTML } from "linkedom";

const here = dirname(fileURLToPath(import.meta.url));
const DIST = join(here, "..", "dist", "extension.js");
const MANIFEST = JSON.parse(
  readFileSync(join(here, "..", "canvas-extension.json"), "utf8"),
);

/* linkedom gives us a DOM without a browser. The bundle reads these as
   globals at module scope, so they must exist before it is imported. */
function installDom() {
  const { window, document } = parseHTML(
    "<!doctype html><html><head></head><body></body></html>",
  );
  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
  /* linkedom exposes HTMLSelectElement.value as a getter only; real browsers
     have a setter that selects the matching <option>. The extension assigns
     select.value (as any DOM code would), so add the missing setter rather
     than contort the extension to suit the test double. */
  const selectValue = Object.getOwnPropertyDescriptor(
    window.HTMLSelectElement.prototype,
    "value",
  );
  if (selectValue && !selectValue.set) {
    Object.defineProperty(window.HTMLSelectElement.prototype, "value", {
      get: selectValue.get,
      set(next) {
        for (const option of this.querySelectorAll("option")) {
          if (option.getAttribute("value") === next) option.setAttribute("selected", "");
          else option.removeAttribute("selected");
        }
      },
      configurable: true,
    });
  }

  globalThis.window = window;
  globalThis.document = document;
  globalThis.localStorage = localStorage;
  globalThis.HTMLElement = window.HTMLElement;
  if (!window.location) window.location = { pathname: "/" };
  return { window, document, store };
}

const dom = installDom();
const { activate, mountBoard } = await import(`file://${DIST}`);

/** Minimal host double matching the v1 ABI. */
function makeHost(overrides = {}) {
  const registered = [];
  return {
    apiVersion: "1",
    extension: { name: "vibe-board", version: "0.1.0", resolvedRef: "test" },
    backend: { id: "test-backend", kind: "local", orgId: null },
    registerPage(id, mount) {
      registered.push({ id, mount });
      return () => registered.splice(0, registered.length);
    },
    navigate() {},
    agentServer: { request: async () => ({}) },
    registered,
    ...overrides,
  };
}

function makeContainer() {
  const el = dom.document.createElement("div");
  dom.document.body.appendChild(el);
  return el;
}

/** Swap in a fetch stub; returns the recorded calls. */
function stubFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url: String(url), opts });
    const result = await handler(String(url), opts);
    return {
      ok: result.ok !== false,
      status: result.status ?? 200,
      statusText: result.statusText ?? "OK",
      json: async () => result.body ?? {},
    };
  };
  return calls;
}

const EMPTY_WORKSPACES = { available: [], selected: [], canvas_base: "https://canvas.test" };

describe("activate", () => {
  it("registers exactly the pages declared in the manifest", () => {
    const host = makeHost();
    activate(host);
    assert.equal(host.registered.length, 1);
    assert.deepEqual(
      host.registered.map((r) => r.id),
      MANIFEST.contributes.pages.map((p) => p.id),
    );
  });

  it("returns the unregister function so Canvas can dispose activation", () => {
    const unregister = () => {};
    const host = makeHost({ registerPage: () => unregister });
    assert.equal(activate(host), unregister);
  });

  it("fails clearly on an unsupported host API version", () => {
    const host = makeHost({ apiVersion: "2" });
    assert.throws(() => activate(host), /requires Canvas host API 1/);
  });
});

describe("mount", () => {
  let originalFetch;
  before(() => {
    originalFetch = globalThis.fetch;
  });
  after(() => {
    globalThis.fetch = originalFetch;
  });

  it("shows the API setup screen when no base URL is stored", () => {
    dom.store.clear();
    stubFetch(async () => ({ body: {} }));
    const container = makeContainer();
    const dispose = mountBoard({
      container,
      path: "",
      navigate: () => {},
      host: makeHost(),
    });

    const setup = container.querySelector("#api-setup");
    assert.ok(setup, "setup section rendered");
    assert.equal(setup.hasAttribute("hidden"), false, "setup is visible");
    assert.equal(
      container.querySelector("#board-wrap").hasAttribute("hidden"),
      true,
      "board hidden until connected",
    );
    dispose();
  });

  it("probes health then loads workspaces from the configured base", async () => {
    dom.store.clear();
    dom.store.set("vibe.ext.apiBase:test-backend", "https://vibe.example");
    const calls = stubFetch(async (url) => {
      if (url.endsWith("/api/health")) return { body: { ok: true } };
      if (url.endsWith("/api/workspaces")) return { body: EMPTY_WORKSPACES };
      return { body: {} };
    });

    const container = makeContainer();
    const dispose = mountBoard({
      container,
      path: "",
      navigate: () => {},
      host: makeHost(),
    });

    await waitFor(() => calls.some((c) => c.url.endsWith("/api/workspaces")));

    assert.equal(calls[0].url, "https://vibe.example/api/health");
    const wsCall = calls.find((c) => c.url.endsWith("/api/workspaces"));
    assert.ok(wsCall, "workspaces fetched");
    assert.equal(wsCall.opts.credentials, "include", "sends credentials for basic auth");
    dispose();
  });

  it("renders all five lanes", async () => {
    dom.store.clear();
    dom.store.set("vibe.ext.apiBase:test-backend", "https://vibe.example");
    stubFetch(async (url) => {
      if (url.endsWith("/api/health")) return { body: { ok: true } };
      if (url.endsWith("/api/workspaces")) return { body: EMPTY_WORKSPACES };
      return { body: {} };
    });
    const container = makeContainer();
    const dispose = mountBoard({
      container,
      path: "",
      navigate: () => {},
      host: makeHost(),
    });
    await waitFor(() => container.querySelector("#empty-state"));
    assert.equal(container.querySelectorAll(".col").length, 5);
    dispose();
  });

  it("surfaces an unreachable API instead of failing silently", async () => {
    dom.store.clear();
    dom.store.set("vibe.ext.apiBase:test-backend", "https://vibe.example");
    stubFetch(async () => ({ ok: false, status: 502, statusText: "Bad Gateway" }));
    const container = makeContainer();
    const dispose = mountBoard({
      container,
      path: "",
      navigate: () => {},
      host: makeHost(),
    });

    await waitFor(() => {
      const err = container.querySelector("#api-setup-error");
      return err && !err.hasAttribute("hidden");
    });
    const err = container.querySelector("#api-setup-error");
    assert.match(err.textContent, /Couldn't reach the vibe-manager API/);
    dispose();
  });

  it("tolerates a malformed board response without throwing", async () => {
    dom.store.clear();
    dom.store.set("vibe.ext.apiBase:test-backend", "https://vibe.example");
    stubFetch(async (url) => {
      if (url.endsWith("/api/health")) return { body: { ok: true } };
      if (url.endsWith("/api/workspaces")) return { body: { available: [], selected: [] } };
      return { body: { nonsense: true } };
    });
    const container = makeContainer();
    const dispose = mountBoard({
      container,
      path: "",
      navigate: () => {},
      host: makeHost(),
    });
    await waitFor(() => container.querySelector("#workspace-select"));
    dispose();
  });

  it("disposer removes all DOM and stops polling", async () => {
    dom.store.clear();
    dom.store.set("vibe.ext.apiBase:test-backend", "https://vibe.example");
    const calls = stubFetch(async (url) => {
      if (url.endsWith("/api/health")) return { body: { ok: true } };
      return { body: EMPTY_WORKSPACES };
    });
    const container = makeContainer();
    const dispose = mountBoard({
      container,
      path: "",
      navigate: () => {},
      host: makeHost(),
    });
    await waitFor(() => calls.length > 0);

    dispose();
    assert.equal(container.childElementCount, 0, "container emptied");

    const afterDispose = calls.length;
    await sleep(60);
    assert.equal(calls.length, afterDispose, "no further requests after dispose");
  });

  it("removes its stylesheet only when the last mount is disposed", () => {
    dom.store.clear();
    stubFetch(async () => ({ body: {} }));
    const a = makeContainer();
    const b = makeContainer();
    const disposeA = mountBoard({ container: a, path: "", navigate: () => {}, host: makeHost() });
    const disposeB = mountBoard({ container: b, path: "", navigate: () => {}, host: makeHost() });

    assert.ok(dom.document.getElementById("vibe-ext-style"), "style injected");
    disposeA();
    assert.ok(
      dom.document.getElementById("vibe-ext-style"),
      "style survives while a mount remains",
    );
    disposeB();
    assert.equal(
      dom.document.getElementById("vibe-ext-style"),
      null,
      "style removed with the last mount",
    );
  });

  it("scopes the light theme to its own root, never the host document", () => {
    dom.store.clear();
    dom.store.set("vibe.theme", "light");
    stubFetch(async () => ({ body: {} }));
    const container = makeContainer();
    const dispose = mountBoard({
      container,
      path: "",
      navigate: () => {},
      host: makeHost(),
    });
    const root = container.querySelector(".vibe-ext");
    assert.equal(root.getAttribute("data-theme"), "light");
    assert.equal(
      dom.document.documentElement.getAttribute("data-theme"),
      null,
      "host <html> untouched",
    );
    dispose();
  });

  it("treats the route remainder as the workspace name", async () => {
    dom.store.clear();
    dom.store.set("vibe.ext.apiBase:test-backend", "https://vibe.example");
    const workspace = {
      id: "w1",
      name: "demo",
      path: "/git/demo",
      max_concurrent: 3,
      push_mode: "main",
    };
    const calls = stubFetch(async (url, opts) => {
      if (url.endsWith("/api/health")) return { body: { ok: true } };
      if (url.endsWith("/api/workspaces") && opts.method === "POST") return { body: workspace };
      if (url.endsWith("/api/workspaces"))
        return { body: { available: [], selected: [workspace] } };
      if (url.includes("/board")) return { body: { workspace, tickets: [] } };
      return { body: {} };
    });

    const container = makeContainer();
    const dispose = mountBoard({
      container,
      path: "demo",
      navigate: () => {},
      host: makeHost(),
    });

    await waitFor(() => calls.some((c) => c.url.includes("/board")));
    const post = calls.find((c) => c.opts.method === "POST");
    assert.equal(JSON.parse(post.opts.body).path, "/git/demo");
    dispose();
  });
});

describe("bundle", () => {
  const source = readFileSync(DIST, "utf8");

  it("is loadable as browser ESM with no bare or remote imports", () => {
    const bare = source.match(/^\s*import\s+[^"']*["'][^./][^"']*["']/gm) || [];
    assert.deepEqual(bare, [], "no bare imports");
    assert.equal(/from\s*["']https?:/.test(source), false, "no remote imports");
    assert.equal(/\brequire\(/.test(source), false, "no CJS require");
  });

  it("exports activate", () => {
    assert.match(source, /export\s*\{[\s\S]*?\bactivate\b/);
  });

  it("embeds the stylesheet with every selector scoped", () => {
    // Guards the build's scoping transform: a page-level selector escaping
    // into the bundle would restyle Canvas itself.
    assert.match(source, /\.vibe-ext \{ --vibe-rem/);
    assert.equal(
      /\n(html|body|:root)\s*[,{]/.test(source),
      false,
      "no unscoped page-level selectors",
    );
  });

  it("only registers page ids declared in the manifest", () => {
    const ids = MANIFEST.contributes.pages.map((p) => p.id);
    const registered = [...source.matchAll(/registerPage\(\s*"([^"]+)"/g)].map((m) => m[1]);
    assert.ok(registered.length > 0, "registerPage called with a literal id");
    for (const id of registered) {
      assert.ok(ids.includes(id), `${id} is declared in the manifest`);
    }
  });
});

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(predicate, timeout = 2000) {
  const started = Date.now();
  for (;;) {
    if (predicate()) return;
    if (Date.now() - started > timeout) throw new Error("waitFor timed out");
    await sleep(10);
  }
}
