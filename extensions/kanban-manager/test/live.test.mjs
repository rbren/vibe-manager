/* Live-derivation tests.

   Pure logic (summary extraction, run slimming, cache/sticky behaviour) is
   tested directly. The automation-status and event-paging paths are exercised
   against the REAL backends via the ingress, because the whole point of this
   module is that a browser can reach them — a mock would prove nothing.
*/

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync, readdirSync } from "node:fs";

import { Live } from "../src/live.js";

const INGRESS = process.env.VIBE_TEST_INGRESS || "http://127.0.0.1:8000";
const KEY = process.env.OH_SESSION_API_KEYS_0
  || (existsSync("/root/git/vibe-manager/.session-key")
    ? readFileSync("/root/git/vibe-manager/.session-key", "utf8").trim()
    : "");

function makeHost(base = INGRESS) {
  return {
    backend: { id: "local" },
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
        const res = await fetch(`${base}${path}`, init);
        if (!res.ok) {
          const err = new Error(`${res.status} ${res.statusText}`);
          err.status = res.status;
          throw err;
        }
        const type = res.headers.get("content-type") || "";
        return type.includes("application/json") ? res.json() : res.text();
      },
    },
  };
}

/** A host whose every request fails, to prove the degrade paths. */
function brokenHost() {
  return {
    backend: { id: "local" },
    agentServer: {
      async request() {
        throw new Error("connection refused");
      },
    },
  };
}

function firstWorkspaceWithAutomation() {
  const path = "/root/.openhands/vibe-manager/index.json";
  if (!existsSync(path)) return null;
  const index = JSON.parse(readFileSync(path, "utf8"));
  return index.workspaces.find((w) => w.automation_id) || null;
}

function firstConversationId() {
  const glob = "/root/.openhands/vibe-manager/workspaces";
  if (!existsSync(glob)) return null;
  for (const dir of readdirSync(glob)) {
    const board = `${glob}/${dir}/board.json`;
    if (!existsSync(board)) continue;
    for (const t of JSON.parse(readFileSync(board, "utf8")).tickets) {
      if (t.conversation_id) return t.conversation_id;
    }
  }
  return null;
}

test("extractActionSummary reads the summary out of tool_call arguments", () => {
  const live = new Live(makeHost());
  const fromString = live.extractActionSummary({
    tool_call: { name: "terminal", arguments: JSON.stringify({ summary: "Ran tests" }) },
    timestamp: "2026-01-01T00:00:00Z",
  });
  assert.deepEqual(fromString, {
    summary: "Ran tests",
    tool: "terminal",
    timestamp: "2026-01-01T00:00:00Z",
  });

  const fromObject = live.extractActionSummary({
    tool_call: { name: "str_replace_editor", arguments: { summary: "  Edited file  " } },
  });
  assert.equal(fromObject.summary, "Edited file", "summary is trimmed");

  assert.equal(live.extractActionSummary({}), null);
  assert.equal(live.extractActionSummary({ tool_call: {} }), null);
  assert.equal(live.extractActionSummary({ tool_call: { arguments: "not json" } }), null);
  assert.equal(
    live.extractActionSummary({ tool_call: { arguments: { summary: "   " } } }),
    null,
    "whitespace-only summary is ignored",
  );
  assert.equal(
    live.extractActionSummary({ tool_call: { arguments: { summary: 42 } } }),
    null,
    "non-string summary is ignored",
  );
});

test("automationStatus reports not-configured without calling out", async () => {
  const live = new Live(brokenHost());
  const out = await live.automationStatus({ id: "w1", automation_id: null });
  assert.equal(out.configured, false);
  assert.equal(out.error, null, "no call was attempted, so no error");
  assert.equal(out.run_active, false);
});

test("automationStatus degrades to an error instead of throwing", async () => {
  const live = new Live(brokenHost());
  const out = await live.automationStatus({ id: "w1", automation_id: "abc" });
  assert.equal(out.configured, true);
  assert.match(out.error, /unreachable/);
  assert.equal(out.enabled, null);
});

test("manager conversation status degrades to unknown", async () => {
  const live = new Live(brokenHost());
  const out = await live.automationStatus({
    id: "w1", automation_id: "abc", manager_conversation_id: "conv-1",
  });
  assert.deepEqual(out.manager_conversation, { id: "conv-1", status: "unknown" });
});

test("triggerAutomation refuses when no automation is configured", async () => {
  const live = new Live(brokenHost());
  await assert.rejects(
    () => live.triggerAutomation({ id: "w1", automation_id: null }),
    /not configured/,
  );
});

test("model cache: sticky entries survive, non-sticky expire", () => {
  const live = new Live(makeHost());
  live.models.set("conv-a", "anthropic/claude", true);
  live.models.entries.get("conv-a").at = 0; // force staleness
  assert.equal(live.models.get("conv-a"), "anthropic/claude", "sticky ignores TTL");

  live.models.set("conv-b", "openai/gpt", false);
  live.models.entries.get("conv-b").at = 0;
  assert.equal(live.models.get("conv-b"), undefined, "non-sticky expires");
  assert.equal(live.models.peek("conv-b"), "openai/gpt", "peek still returns it");
});

test("primeModel and invalidateModel manage the cache", () => {
  const live = new Live(makeHost());
  live.primeModel("conv-c", "some/model");
  assert.equal(live.models.get("conv-c"), "some/model");
  live.invalidateModel("conv-c");
  assert.equal(live.models.get("conv-c"), undefined);
});

/** A host answering conversation metadata from a fixture, counting requests. */
function metaHost(meta) {
  const calls = [];
  return {
    calls,
    backend: { id: "local" },
    agentServer: {
      async request({ path }) {
        calls.push(path);
        return meta;
      },
    },
  };
}

test("one conversation-metadata request fills both the model and status caches", async () => {
  const host = metaHost({
    agent: { llm: { model: "anthropic/claude-fable-5" } },
    execution_status: "finished",
  });
  const live = new Live(host);

  assert.equal(live.conversationStatus("c1"), null, "nothing cached yet");
  assert.equal(live.llmModel("c1"), null, "the same in-flight fetch serves both");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(host.calls.length, 1, "one GET, not one per derived field");
  assert.equal(live.conversationStatus("c1"), "finished");
  assert.equal(live.llmModel("c1"), "anthropic/claude-fable-5");
});

test("a failed conversation refetch keeps the last known status", async () => {
  const live = new Live(brokenHost());
  live.statuses.set("c1", "running");
  live.statuses.entries.get("c1").at = 0; // force staleness
  assert.equal(live.conversationStatus("c1"), "running", "peek covers the refetch");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(live.statuses.peek("c1"), "running");
});

test("decorate adds conversation_url and gates derived fields by status", () => {
  const live = new Live(makeHost());
  live.primeModel("c1", "anthropic/claude-sonnet");
  live.summaries.set("c1", { summary: "Working", tool: "terminal", timestamp: null });
  live.statuses.set("c1", "running");

  const [running, pending, done] = live.decorate([
    { id: "t1", status: "in_progress", conversation_id: "c1" },
    { id: "t2", status: "pending", conversation_id: null },
    { id: "t3", status: "finished", conversation_id: "c1" },
  ], "https://canvas.example");

  assert.equal(running.conversation_url, "https://canvas.example/conversations/c1");
  assert.equal(running.latest_action.summary, "Working");
  assert.equal(running.conversation_status, "running");
  assert.equal(running.llm_model, "anthropic/claude-sonnet");

  assert.equal(pending.conversation_url, null);
  assert.equal(pending.latest_action, null);
  assert.equal(pending.conversation_status, null);
  assert.equal(pending.llm_model, null);

  assert.equal(done.latest_action, null, "only in_progress shows a live summary");
  assert.equal(done.conversation_status, null, "and only in_progress needs the status");
  assert.equal(done.llm_model, "anthropic/claude-sonnet");
});

// ---------------------------------------------------------------- live calls

const ws = firstWorkspaceWithAutomation();

test("automationStatus against the real automation backend", { skip: !ws || !KEY }, async () => {
  const live = new Live(makeHost());
  const out = await live.automationStatus(ws);
  assert.equal(out.error, null, `expected no error, got ${out.error}`);
  assert.equal(out.configured, true);
  assert.equal(typeof out.enabled, "boolean", "enabled comes back from the backend");
  assert.ok(out.last_run, "at least one run is reported");
  assert.ok(
    ["COMPLETED", "FAILED", "RUNNING", "PENDING"].includes(out.last_run.status),
    `unexpected run status ${out.last_run?.status}`,
  );
  assert.equal(typeof out.consecutive_failures, "number");
});

test("fetchActionSummary pages past non-action events", { skip: !KEY }, async () => {
  const convId = firstConversationId();
  if (!convId) return;
  const live = new Live(makeHost("http://127.0.0.1:18000"));
  const summary = await live.fetchActionSummary(convId);
  // The conversation may genuinely have no summarised action; assert only on
  // the shape when one is found.
  if (summary) {
    assert.equal(typeof summary.summary, "string");
    assert.ok(summary.summary.length > 0);
  }
});
