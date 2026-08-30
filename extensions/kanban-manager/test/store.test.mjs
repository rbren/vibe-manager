/* Store tests against the REAL agent-server file API.

   A fake host is used only to supply the session key that Canvas would
   normally attach; every request goes to the live file API, so these tests
   exercise the actual upload/download/enumerate behaviour rather than a mock
   of it. The store root is redirected to a temp dir via VIBE_TEST_ROOT.
*/

import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, writeFileSync, rmSync, mkdtempSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { Store, safeFilename, newId, isNotFound } from "../src/store.js";

/** Repo root, so the tests can drive the automation's store code as well. */
const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

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

  assert.equal(t1.llm_profile, null, "no model chosen means the manager picks");
  assert.equal(t1.max_budget, 10, "requests default to a $10 budget");

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

test("createTicket records the request's agent and budget", async () => {
  const { store, wsId } = scratchStore();

  const t = await store.createTicket(wsId, "spare no expense", {
    llm_profile: "opus",
    max_budget: 42,
  });
  assert.equal(t.llm_profile, "opus", "the requested model is recorded");
  assert.equal(t.max_budget, 42);

  const onDisk = (await store.readBoard(wsId)).tickets[0];
  assert.equal(onDisk.llm_profile, "opus", "and survives the round trip to disk");
  assert.equal(onDisk.max_budget, 42);

  await assert.rejects(
    () => store.createTicket(wsId, "free lunch", { max_budget: 0 }),
    /max_budget/,
  );
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

  const store = new Store(caching, ROOT);
  const ws = await store.selectWorkspace(`/git/cache-${newId()}`);

  await store.createTicket(ws.id, "first");
  await store.createTicket(ws.id, "second");

  // Read through a non-caching store so this asserts what is on disk.
  const { tickets } = await new Store(real, ROOT).getBoard(ws.id);
  const bodies = tickets.map((t) => t.entries[0].body).sort();
  assert.deepEqual(bodies, ["first", "second"], "neither write clobbered the other");
  assert.equal(served, 0, "board reads bypass the cache");
});

/* Every write is a read-modify-write of the whole board, and a board upload
   takes long enough that a second write started meanwhile (a second submit,
   an attachment, a drag-reorder) reads the board from before the first one
   landed and then uploads that stale document over it. The first ticket is
   gone: it never appears in pending. */
test("overlapping writes cannot lose a ticket", async () => {
  const { store, wsId } = scratchStore();

  await Promise.all([
    store.createTicket(wsId, "first"),
    store.createTicket(wsId, "second"),
    store.createTicket(wsId, "third"),
  ]);

  const { tickets } = await store.getBoard(wsId);
  assert.deepEqual(
    tickets.map((t) => t.entries[0].body).sort(),
    ["first", "second", "third"],
    "no concurrent write clobbered another",
  );
  assert.deepEqual(
    tickets.map((t) => t.sort_order).sort(),
    [1, 2, 3],
    "each ticket got its own slot at the bottom of pending",
  );
});

/* Same hazard across ticket operations: an entry appended (or a verify)
   while a submit is in flight must not resurrect the pre-submit board. */
test("a write overlapping a submit keeps both changes", async () => {
  const { store, wsId } = scratchStore();
  const existing = await store.createTicket(wsId, "already here");

  const [, created] = await Promise.all([
    store.appendEntry(wsId, existing.id, "more context"),
    store.createTicket(wsId, "brand new"),
  ]);

  const { tickets } = await store.getBoard(wsId);
  assert.equal(tickets.length, 2, "the new ticket survived the concurrent append");
  assert.ok(tickets.some((t) => t.id === created.id), "created ticket is on the board");
  assert.equal(
    tickets.find((t) => t.id === existing.id).entries.length,
    2,
    "the appended entry survived the concurrent create",
  );
});
/* The browser is not the only writer: the automation's mechanical transitions
   and the manager's `vibectl patch` write the same board.json from the shell,
   and no lock can span the file API. A shell write that lands inside the
   browser's read-modify-write window used to disappear — and so did a ticket
   the user had just added, when the shell writer was the one to write last.

   Both interleavings are driven here by writing board.json directly, which is
   exactly what the shell side does. */

function shellWrite(wsId, mutate) {
  const path = `${ROOT}/workspaces/${wsId}/board.json`;
  const board = JSON.parse(readFileSync(path, "utf8"));
  mutate(board);
  board.rev = (board.rev || 0) + 1;
  board.writer = "shell";
  writeFileSync(path, JSON.stringify(board, null, 2));
}

function shellTicket(id) {
  return {
    id, status: "pending", title: null, sort_order: 99, conversation_id: null,
    pr_url: null, manager_note: null, dispatched_entry_count: 0,
    created_at: 1, updated_at: 1, finished_at: null, verified_at: null,
    entries: [{ id: `e${id}`, author: "user", body: id, created_at: 1 }],
    attachments: [],
  };
}

/** A host that runs `hook` once, right after the first board.json read
    (`after-read`) or right after the first board.json write (`after-write`). */
function racingHost(when, hook) {
  const real = makeHost();
  let fired = false;
  return {
    agentServer: {
      async request(opts) {
        const board = opts.path.includes("board.json");
        const kind = opts.path.startsWith("/api/file/upload") ? "after-write" : "after-read";
        const res = await real.agentServer.request(opts);
        if (board && !fired && kind === when) {
          fired = true;
          hook();
        }
        return res;
      },
    },
  };
}

test("a shell write during a browser mutation is not clobbered", async () => {
  const wsId = newId();
  await new Store(makeHost(), ROOT).createTicket(wsId, "existing");

  // The manager patches a ticket while we are preparing our own write.
  const host = racingHost("after-read", () =>
    shellWrite(wsId, (b) => {
      b.tickets[0].status = "in_progress";
      b.tickets.push(shellTicket("frommanager"));
    }));

  const created = await new Store(host, ROOT).createTicket(wsId, "mine");

  const board = await new Store(makeHost(), ROOT).readBoard(wsId);
  const ids = board.tickets.map((t) => t.id);
  assert.ok(ids.includes(created.id), "the browser's ticket is on the board");
  assert.ok(ids.includes("frommanager"), "the shell's ticket was not clobbered");
  assert.equal(board.tickets[0].status, "in_progress", "the shell's patch survived");
  assert.equal(
    ids.filter((id) => id === created.id).length, 1,
    "the retried mutation is applied exactly once",
  );
});

test("a browser mutation clobbered by a shell write is re-applied", async () => {
  const wsId = newId();
  const store = new Store(makeHost(), ROOT);
  await store.createTicket(wsId, "existing");
  const stale = await store.readBoard(wsId);

  // The manager writes a board it read BEFORE our upload: our ticket is gone
  // from disk the instant after we wrote it.
  const host = racingHost("after-write", () => {
    const path = `${ROOT}/workspaces/${wsId}/board.json`;
    stale.tickets[0].status = "finished";
    stale.rev = (stale.rev || 0) + 1;
    stale.writer = "shell";
    writeFileSync(path, JSON.stringify(stale, null, 2));
  });

  const created = await new Store(host, ROOT).createTicket(wsId, "mine");

  const board = await store.readBoard(wsId);
  const ids = board.tickets.map((t) => t.id);
  assert.ok(ids.includes(created.id), "the ticket the user added survives the clobber");
  assert.equal(board.tickets[0].status, "finished", "the shell's change is kept too");
});

/* The other writer in production is automation/vibestore.py, so pair the two
   real implementations: they have to agree on the rev/writer protocol or a
   card added while the manager patches disappears again. */
test("the automation's own store code cannot drop a ticket added mid-patch", async () => {
  const wsId = newId();
  const store = new Store(makeHost(), ROOT);
  const existing = await store.createTicket(wsId, "existing");

  const managerPatch = () => execFileSync(
    "python3",
    ["-c", [
      "import sys; sys.path.insert(0, sys.argv[1])",
      "import vibestore",
      "vibestore.patch_ticket(sys.argv[2], sys.argv[3], status='in_progress',"
      + " manager_note='Worker dispatched')",
    ].join("\n"), `${REPO}/automation`, wsId, existing.id],
    { env: { ...process.env, VIBE_STORE_DIR: ROOT }, stdio: "pipe" },
  );

  const created = await new Store(racingHost("after-read", managerPatch), ROOT)
    .createTicket(wsId, "mine");

  const board = await store.readBoard(wsId);
  const byId = Object.fromEntries(board.tickets.map((t) => [t.id, t]));
  assert.ok(byId[created.id], "the ticket the user added survives the manager patch");
  assert.equal(byId[existing.id].status, "in_progress", "the manager's patch survives too");
  assert.equal(byId[existing.id].manager_note, "Worker dispatched");
});

test("board writes advance a revision so racing writers can be detected", async () => {
  const wsId = newId();
  const store = new Store(makeHost(), ROOT);
  await store.createTicket(wsId, "one");
  const first = await store.readBoard(wsId);
  await store.createTicket(wsId, "two");
  const second = await store.readBoard(wsId);
  assert.equal(second.rev, first.rev + 1, "each write bumps rev once");
});
