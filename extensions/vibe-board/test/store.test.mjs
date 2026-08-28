/* Store tests against the REAL agent-server file API.

   A fake host is used only to supply the session key that Canvas would
   normally attach; every request goes to the live file API, so these tests
   exercise the actual upload/download/enumerate behaviour rather than a mock
   of it. The store root is redirected to a temp dir via VIBE_TEST_ROOT.
*/

import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, rmSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Store, safeFilename, newId, isNotFound } from "../src/store.js";

const AGENT_SERVER = process.env.VIBE_TEST_AGENT_SERVER || "http://127.0.0.1:18000";
const KEY = process.env.OH_SESSION_API_KEYS_0
  || readFileSync("/root/git/vibe-manager/.session-key", "utf8").trim();

let ROOT;

/* Mirrors what Canvas's host.agentServer.request does: root-relative path
   against the backend host, session key attached, FormData passed through. */
function makeHost() {
  return {
    agentServer: {
      async request({ method = "GET", path, body, headers = {} }) {
        const init = {
          method,
          headers: {
            ...(body instanceof FormData ? {} : { "Content-Type": "application/json" }),
            "X-Session-API-Key": KEY,
            ...headers,
          },
        };
        if (body !== undefined && method !== "GET") {
          init.body = body instanceof FormData ? body : JSON.stringify(body);
        }
        const res = await fetch(`${AGENT_SERVER}${path}`, init);
        if (!res.ok) {
          const err = new Error(`${res.status} ${res.statusText}`);
          err.status = res.status;
          throw err;
        }
        // Mirrors the real client's parseResponse: only application/json is
        // parsed, everything else (incl. the file API's octet-stream) is text.
        const type = res.headers.get("content-type") || "";
        if (type.includes("application/json")) return res.json();
        return res.text();
      },
    },
  };
}

/** A host that cannot answer /api/file/home, to prove the root is not fetched. */
function brokenHomeHost() {
  return {
    backend: { id: "local" },
    agentServer: { async request() { throw new Error("home should not be requested"); } },
  };
}

/* A store rooted at the temp dir. The root is injected via the constructor,
   so the real path helpers (and therefore the real file layout) are exercised
   rather than being stubbed out. */
function scratchStore() {
  const store = new Store(makeHost(), ROOT);
  return { store, wsId: newId() };
}

before(() => {
  ROOT = mkdtempSync(join(tmpdir(), "vibe-store-"));
});

after(() => {
  try { rmSync(ROOT, { recursive: true, force: true }); } catch { /* best effort */ }
});

test("safeFilename strips paths and unsafe characters", () => {
  assert.equal(safeFilename("../../etc/passwd"), "passwd");
  assert.equal(safeFilename("a b(1).png"), "a b(1).png");
  assert.equal(safeFilename("we!rd$$name.txt"), "we_rd_name.txt");
  assert.equal(safeFilename(""), "file");
  assert.equal(safeFilename("..."), "file");
  assert.equal(safeFilename("x".repeat(200)).length, 120);
});

test("newId is unique and hex", () => {
  const ids = new Set(Array.from({ length: 500 }, () => newId()));
  assert.equal(ids.size, 500);
  assert.match([...ids][0], /^[0-9a-f]{12}$/);
});

test("isNotFound recognises 404 by status and message", () => {
  assert.equal(isNotFound({ status: 404 }), true);
  assert.equal(isNotFound(new Error("404 Not Found")), true);
  assert.equal(isNotFound({ status: 500 }), false);
  assert.equal(isNotFound(new Error("boom")), false);
});

test("storeRoot resolves the real home directory from the agent server", async () => {
  const store = new Store(makeHost());
  const root = await store.storeRoot();
  assert.match(root, /^\/.*\/\.openhands\/vibe-manager$/, `unexpected root ${root}`);
  assert.ok(!root.startsWith("~"), "tilde must be expanded, the file API cannot");
  // Matches the home the agent server actually reports, not a hardcoded user.
  const home = await makeHost().agentServer.request({ path: "/api/file/home" });
  const parsed = typeof home === "string" ? JSON.parse(home) : home;
  assert.equal(root, `${parsed.home}/.openhands/vibe-manager`);
});

test("storeRoot is resolved once and reused", async () => {
  let calls = 0;
  const host = makeHost();
  const inner = host.agentServer.request.bind(host.agentServer);
  host.agentServer.request = async (opts) => {
    if (opts.path === "/api/file/home") calls += 1;
    return inner(opts);
  };
  const store = new Store(host);
  await Promise.all([store.storeRoot(), store.storeRoot(), store.storeRoot()]);
  await store.storeRoot();
  assert.equal(calls, 1, "home is fetched once even under concurrent callers");
});

test("storeRoot retries after a failure instead of caching it", async () => {
  let fail = true;
  const host = makeHost();
  const inner = host.agentServer.request.bind(host.agentServer);
  host.agentServer.request = async (opts) => {
    if (opts.path === "/api/file/home" && fail) throw new Error("boom");
    return inner(opts);
  };
  const store = new Store(host);
  await assert.rejects(() => store.storeRoot(), /boom/);
  fail = false;
  assert.match(await store.storeRoot(), /\.openhands\/vibe-manager$/);
});

test("an explicit root overrides home resolution", async () => {
  const store = new Store(brokenHomeHost(), "/custom/root");
  assert.equal(await store.storeRoot(), "/custom/root");
  assert.equal(await store.indexPath(), "/custom/root/index.json");
  assert.equal(await store.boardPath("w1"), "/custom/root/workspaces/w1/board.json");
  assert.equal(
    await store.attachmentPath("a1", "x.png"),
    "/custom/root/attachments/a1/x.png",
  );
});

test("readJson returns the fallback for a missing file (real 404)", async () => {
  const store = new Store(makeHost());
  const got = await store.readJson(`${ROOT}/definitely-absent.json`, { empty: true });
  assert.deepEqual(got, { empty: true });
});

test("readJson rethrows when no fallback is given", async () => {
  const store = new Store(makeHost());
  await assert.rejects(() => store.readJson(`${ROOT}/absent2.json`));
});

test("writeJson then readJson round-trips through the file API", async () => {
  const store = new Store(makeHost());
  const path = `${ROOT}/round/trip.json`;
  const payload = { hello: "world", nested: { n: 1 }, unicode: "café ✓" };
  await store.writeJson(path, payload);
  assert.deepEqual(await store.readJson(path), payload);
});

test("writeFile creates missing parent directories", async () => {
  const store = new Store(makeHost());
  const path = `${ROOT}/deep/a/b/c/file.json`;
  await store.writeJson(path, { ok: true });
  assert.deepEqual(await store.readJson(path), { ok: true });
});

/* The remaining tests drive a store whose STORE_ROOT is the temp dir. The
   module-level constant is baked into the path helpers, so exercise the
   workspace/ticket logic through a store pointed at a scratch board file. */

test("board mutations: create, append, reopen, verify, reorder", async () => {
  const { store, wsId } = scratchStore();

  const t1 = await store.createTicket(wsId, "  first ticket  ");
  assert.equal(t1.status, "pending");
  assert.equal(t1.entries[0].body, "first ticket", "body is trimmed");
  assert.equal(t1.entries[0].author, "user");
  assert.equal(t1.sort_order, 1);
  assert.equal(t1.dispatched_entry_count, 0);

  const t2 = await store.createTicket(wsId, "second");
  assert.equal(t2.sort_order, 2, "new tickets land at the bottom of pending");

  await assert.rejects(() => store.createTicket(wsId, "   "), /empty/);

  // A user entry on a finished ticket reopens it at the bottom of pending.
  await store.mutateBoard(wsId, (b) => {
    const t = b.tickets.find((x) => x.id === t1.id);
    t.status = "finished";
    t.finished_at = 1;
  });
  const reopened = await store.appendEntry(wsId, t1.id, "please also do X");
  assert.equal(reopened.status, "pending");
  assert.equal(reopened.sort_order, 3, "reopened ticket goes to the bottom");
  assert.equal(reopened.entries.length, 2);

  // in_progress must NOT be reopened.
  await store.mutateBoard(wsId, (b) => {
    b.tickets.find((x) => x.id === t2.id).status = "in_progress";
  });
  const busy = await store.appendEntry(wsId, t2.id, "extra context");
  assert.equal(busy.status, "in_progress");

  // verified is terminal and must NOT be reopened.
  const t3 = await store.createTicket(wsId, "third");
  await store.verifyTicket(wsId, t3.id);
  const verified = await store.appendEntry(wsId, t3.id, "another note");
  assert.equal(verified.status, "verified");

  // Manager entries never reopen.
  await store.mutateBoard(wsId, (b) => {
    b.tickets.find((x) => x.id === t1.id).status = "needs_input";
  });
  const mgr = await store.appendEntry(wsId, t1.id, "status", "manager");
  assert.equal(mgr.status, "needs_input", "manager entries do not reopen");

  await assert.rejects(() => store.appendEntry(wsId, "nope", "x"), /not found/);
  await assert.rejects(() => store.appendEntry(wsId, t1.id, "  "), /empty/);

  // Reorder only touches tickets in the named lane.
  const board = await store.readBoard(wsId);
  const pending = board.tickets.filter((t) => t.status === "pending").map((t) => t.id);
  await store.reorder(wsId, "pending", [...pending].reverse());
  const after = await store.readBoard(wsId);
  [...pending].reverse().forEach((id, idx) => {
    assert.equal(after.tickets.find((t) => t.id === id).sort_order, idx);
  });
  await assert.rejects(() => store.reorder(wsId, "bogus", []), /bad status/);
});

test("verifyTicket stamps verified_at and is rejected for unknown ids", async () => {
  const { store, wsId } = scratchStore();

  const t = await store.createTicket(wsId, "done thing");
  const before = Date.now() / 1000;
  const v = await store.verifyTicket(wsId, t.id);
  assert.equal(v.status, "verified");
  assert.ok(v.verified_at >= before - 1, "verified_at is stamped");
  await assert.rejects(() => store.verifyTicket(wsId, "missing"), /not found/);
});

test("attachment bytes survive a real upload and download", async () => {
  const store = new Store(makeHost());
  const attId = newId();
  const path = `${ROOT}/attachments/${attId}/hello.txt`;
  const content = "attachment payload ✓";
  await store.writeFile(path, new Blob([content], { type: "text/plain" }), "hello.txt");

  const raw = await store.host.agentServer.request({
    path: `/api/file/download?path=${encodeURIComponent(path)}`,
  });
  assert.equal(typeof raw === "string" ? raw : JSON.stringify(raw), content);
});

test("binary attachment bytes round-trip without corruption", async () => {
  const store = new Store(makeHost());
  const attId = newId();
  const path = `${ROOT}/attachments/${attId}/blob.bin`;
  // A PNG header - bytes that are not valid UTF-8 text.
  const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0xff]);
  await store.writeFile(path, new Blob([bytes]), "blob.bin");

  const res = await fetch(
    `${AGENT_SERVER}/api/file/download?path=${encodeURIComponent(path)}`,
    { headers: { "X-Session-API-Key": KEY } },
  );
  const got = new Uint8Array(await res.arrayBuffer());
  assert.deepEqual([...got], [...bytes], "binary bytes are preserved");
});


/* The file API sends ETag/Last-Modified but no Cache-Control, so a browser may
   reuse a stale board within its heuristic freshness window. Because every
   write is a read-modify-write, a stale read silently drops the previously
   created ticket. Reproduced with a host that caches by URL, like a browser. */
test("a caching client cannot lose a ticket between writes", async () => {
  const real = makeHost();
  const cache = new Map();
  let served = 0;
  const caching = {
    agentServer: {
      async request(opts) {
        const isRead = (opts.method || "GET") === "GET";
        if (isRead && cache.has(opts.path)) {
          served++;
          return cache.get(opts.path);
        }
        const res = await real.agentServer.request(opts);
        if (isRead) cache.set(opts.path, res);
        return res;
      },
    },
  };

  const store = new Store(caching);
  const ws = await store.selectWorkspace(`/git/cache-${newId()}`);

  await store.createTicket(ws.id, "first");
  await store.createTicket(ws.id, "second");

  // Read through a non-caching store so this asserts what is on disk.
  const { tickets } = await new Store(real).getBoard(ws.id);
  const bodies = tickets.map((t) => t.entries[0].body).sort();
  assert.deepEqual(bodies, ["first", "second"], "neither write clobbered the other");
  assert.equal(served, 0, "board reads bypass the cache");
});
