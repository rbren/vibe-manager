/* vibe — work manager SPA */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const state = {
  workspaces: { available: [], selected: [] },
  ws: null,              // selected workspace object
  tickets: [],
  drawerTicketId: null,
  pollTimer: null,
  automation: null,      // manager automation status for the selected workspace
  automationTimer: null,
  dragging: null,        // { id, status }
  returnFocus: null,     // element focused before the drawer opened
  showVerified: localStorage.getItem("vibe.showVerified") === "1",
  newTicketFiles: [],    // File objects staged for the next ticket
  theme: localStorage.getItem("vibe.theme") === "light" ? "light" : "dark",
};

const STATUS_LABEL = {
  pending: "pending",
  in_progress: "in progress",
  needs_input: "needs you",
  finished: "finished",
  verified: "verified",
};

// An empty lane is an invitation to act, not a blank box.
const LANE_EMPTY = {
  pending: "Nothing queued. Send a request above.",
  in_progress: "No agent is working right now.",
  needs_input: "Nothing is waiting on you.",
  finished: "Finished work lands here for a look.",
  verified: "Verified work is filed here.",
};

/* ------------------------------------------------------------------ api */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

/* ------------------------------------------------------------ attachments */

// Raw-body upload (no multipart): file bytes as the POST body, name in query.
async function uploadAttachment(ticketId, file) {
  const res = await fetch(
    `/api/tickets/${ticketId}/attachments?filename=${encodeURIComponent(file.name)}`,
    {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    },
  );
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(`${file.name}: ${detail}`);
  }
  return res.json();
}

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function isImage(contentType) {
  return (contentType || "").startsWith("image/");
}

function attachmentEl(a) {
  const link = document.createElement("a");
  link.href = a.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.title = `${a.filename} · ${fmtSize(a.size)}`;
  if (isImage(a.content_type)) {
    link.className = "att att-thumb";
    const img = document.createElement("img");
    img.src = a.url;
    img.alt = a.filename;
    img.loading = "lazy";
    link.appendChild(img);
  } else {
    link.className = "att att-file";
    link.textContent = `📎 ${a.filename}`;
  }
  const size = document.createElement("span");
  size.className = "att-size";
  size.textContent = isImage(a.content_type) ? "" : ` ${fmtSize(a.size)}`;
  link.appendChild(size);
  return link;
}

/* ----------------------------------------------------------- workspaces */

async function loadWorkspaces() {
  const data = await api("/api/workspaces");
  state.workspaces = data;
  const sel = $("#workspace-select");
  const current = sel.value;
  sel.innerHTML = '<option value="">Choose a workspace</option>';

  const selectedPaths = new Set(data.selected.map((w) => w.path));
  if (data.selected.length) {
    const og = document.createElement("optgroup");
    og.label = "active";
    for (const w of data.selected) {
      const o = document.createElement("option");
      o.value = w.path;
      o.textContent = w.name;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  const avail = data.available.filter((a) => !selectedPaths.has(a.path));
  if (avail.length) {
    const og = document.createElement("optgroup");
    og.label = "available";
    for (const a of avail) {
      const o = document.createElement("option");
      o.value = a.path;
      o.textContent = a.name;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  sel.value = current || (localStorage.getItem("vibe.workspace") ?? "");
}

/* URL scheme: /workspace/<name> deep-links to a workspace (SPA route). */
function workspaceURL(name) {
  return name ? `/workspace/${encodeURIComponent(name)}` : "/";
}

function workspaceNameFromURL() {
  const m = location.pathname.match(/^\/workspace\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

function workspacePathFromName(name) {
  const all = [...(state.workspaces.selected || []), ...(state.workspaces.available || [])];
  return all.find((w) => w.name === name)?.path ?? null;
}

function syncURL(mode) {
  if (mode === "none") return;
  const url = workspaceURL(state.ws?.name);
  if (mode === "replace") history.replaceState({}, "", url);
  else if (location.pathname !== url) history.pushState({}, "", url);
}

async function selectWorkspace(path, { historyMode = "push" } = {}) {
  if (!path) {
    state.ws = null;
    localStorage.removeItem("vibe.workspace");
    syncURL(historyMode);
    render();
    return;
  }
  try {
    const ws = await api("/api/workspaces", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    state.ws = ws;
    state.automation = null;
    localStorage.setItem("vibe.workspace", path);
    syncURL(historyMode);
    await refreshBoard();
    startPolling();
    await loadWorkspaces();
    $("#workspace-select").value = path;
  } catch (e) {
    console.error(`workspace error: ${e.message}`);
  }
  render();
}

/* ---------------------------------------------------------------- board */

async function refreshBoard() {
  if (!state.ws) return;
  try {
    const data = await api(`/api/workspaces/${state.ws.id}/board`);
    state.ws = data.workspace;
    state.tickets = data.tickets;
    renderBoard();
    renderSettings();
    if (state.drawerTicketId) renderDrawer();
  } catch (e) {
    console.error(e);
  }
}

function startPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(refreshBoard, 5000);
  clearInterval(state.automationTimer);
  state.automationTimer = setInterval(refreshAutomation, 15000);
  refreshAutomation();
}

/* ------------------------------------------------- manager automation badge */

async function refreshAutomation() {
  if (!state.ws) return;
  try {
    state.automation = await api(`/api/workspaces/${state.ws.id}/automation`);
  } catch (e) {
    console.error(e);
  }
  renderMgrBadge();
}

function fmtAgo(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

const TRIGGER_HINT = "Click to run the manager now";

async function triggerManager() {
  if (!state.ws || !state.ws.automation_id) return;
  const badge = $("#mgr-badge");
  if (badge.classList.contains("triggering")) return;
  badge.classList.add("triggering");
  $("#mgr-text").textContent = "manager: triggering…";
  try {
    await api(`/api/workspaces/${state.ws.id}/automation/trigger`, { method: "POST" });
  } catch (e) {
    console.error(`manager trigger failed: ${e.message}`);
  }
  badge.classList.remove("triggering");
  refreshAutomation();
}

function renderMgrBadge() {
  const badge = $("#mgr-badge");
  if (!state.ws || !state.ws.automation_id) { badge.hidden = true; return; }
  badge.hidden = false;
  badge.classList.remove("ok", "err", "paused");
  const a = state.automation;
  const text = $("#mgr-text");
  if (badge.classList.contains("triggering")) return;
  if (!a || a.automation_id !== state.ws.automation_id) {
    text.textContent = "manager";
    badge.title = `Manager automation is watching this workspace\n${TRIGGER_HINT}`;
    return;
  }
  // Judge health by the last *finished* run so an in-flight retry (with a
  // once-a-minute cron, one is almost always in flight while runs keep
  // failing) can't mask a failure streak behind a neutral "polling" state.
  const fin = a.last_finished_run;
  const lr = fin || a.last_run;
  const lastWhen = lr ? fmtAgo(lr.completed_at || lr.started_at || lr.created_at) : "";
  const runsFailing = !!(fin && fin.status !== "COMPLETED");
  const convStatus = a.manager_conversation?.status;
  const convRunning = convStatus === "running";
  const convFailing = convStatus === "error" || convStatus === "stuck";
  const tip = [];
  let label, cls = "";
  if (a.error) {
    label = "manager: unknown"; cls = "err"; tip.push(a.error);
  } else if (a.enabled === false) {
    label = "manager: paused"; cls = "paused"; tip.push("Automation is disabled");
  } else if (runsFailing) {
    label = `manager ✗ ${lastWhen}`; cls = "err";
    if (a.consecutive_failures > 1) tip.push(`${a.consecutive_failures} runs failed in a row`);
    if (a.run_active) tip.push("retry run in progress");
    if (convRunning) tip.push("manager agent conversation still running");
  } else if (convFailing) {
    label = `manager: agent ${convStatus}`; cls = "err";
    tip.push(`Manager agent conversation is ${convStatus}`);
  } else if (convRunning) {
    label = "manager: working";
    tip.push("Manager agent conversation is running right now");
  } else if (a.run_active) {
    label = "manager: polling";
    tip.push("Automation run in progress");
  } else if (lr) {
    label = `manager ✓ ${lastWhen}`;
    cls = "ok";
  } else {
    label = "manager";
  }
  if (lr) {
    tip.push(`last run: ${(lr.status || "?").toLowerCase()} ${lastWhen}`.trim());
    if (lr.error_detail) tip.push(`error: ${lr.error_detail}`);
  }
  if (a.last_triggered_at) tip.push(`last triggered ${fmtAgo(a.last_triggered_at)}`);
  if (a.manager_conversation) tip.push(`manager agent: ${a.manager_conversation.status}`);
  tip.push(TRIGGER_HINT);
  text.textContent = label;
  if (cls) badge.classList.add(cls);
  badge.title = tip.join("\n");
}

function render() {
  const has = !!state.ws;
  $("#empty-state").hidden = has;
  $("#board-wrap").hidden = !has;
  $("#ctl-concurrency").hidden = !has;
  $("#ctl-pushmode").hidden = !has;
  $("#show-verified").hidden = !has;
  renderMgrBadge();
  if (has) { renderBoard(); renderSettings(); }
}

function renderSettings() {
  if (!state.ws) return;
  const mc = $("#max-concurrent");
  if (document.activeElement !== mc) mc.value = state.ws.max_concurrent;
  $$("#push-mode .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === state.ws.push_mode));
  renderMgrBadge();
}

function shortModel(model) {
  // "anthropic/claude-fable-5" -> "claude-fable-5"; keep the full name in the tooltip.
  return model.split("/").pop();
}

function modelChip(model) {
  const chip = document.createElement("span");
  chip.className = "chip model";
  chip.title = model;
  chip.textContent = `◆ ${shortModel(model)}`;
  return chip;
}

function cardEl(t) {
  const firstEntry = t.entries[0]?.body ?? "";
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.id = t.id;
  // finished + verified columns are time-ordered (most recent first), not
  // priority-ordered, so their cards aren't draggable.
  el.draggable = t.status !== "verified" && t.status !== "finished";
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", `Open request ${t.title || t.entries[0]?.body?.slice(0, 60) || t.id.slice(0, 6)}`);

  if (t.title) {
    const title = document.createElement("div");
    title.className = "card-title";
    title.textContent = t.title;
    el.appendChild(title);
  }

  const body = document.createElement("div");
  body.className = "card-body";
  body.textContent = firstEntry;
  el.appendChild(body);

  if (t.manager_note) {
    const note = document.createElement("div");
    note.className = "card-note";
    note.textContent = `⚑ ${t.manager_note}`;
    el.appendChild(note);
  }

  if (t.status === "in_progress" && t.latest_action?.summary) {
    el.classList.add("live");
    const act = document.createElement("div");
    act.className = "card-activity";
    act.title = t.latest_action.tool ? `tool: ${t.latest_action.tool}` : "";
    act.innerHTML = `<span class="activity-dot"></span>`;
    const text = document.createElement("span");
    text.className = "activity-text";
    text.textContent = t.latest_action.summary;
    act.appendChild(text);
    el.appendChild(act);
  }

  const meta = document.createElement("div");
  meta.className = "card-meta";
  meta.innerHTML = `<span>#${t.id.slice(0, 6)}</span>`;
  if (t.llm_model) meta.appendChild(modelChip(t.llm_model));
  if (t.status === "verified" && t.verified_at) {
    const when = document.createElement("span");
    when.className = "chip verified";
    when.textContent = `✓ ${fmtTime(t.verified_at)}`;
    meta.appendChild(when);
  }
  if (t.entries.length > 1) {
    const chip = document.createElement("span");
    chip.className = "chip entries";
    chip.textContent = `${t.entries.length} entries`;
    meta.appendChild(chip);
  }
  if ((t.attachments || []).length) {
    const chip = document.createElement("span");
    chip.className = "chip attachments";
    chip.textContent = `📎 ${t.attachments.length}`;
    meta.appendChild(chip);
  }
  if (t.conversation_url) {
    const a = document.createElement("a");
    a.className = "chip convo";
    a.href = t.conversation_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "↗ conversation";
    a.addEventListener("click", (e) => e.stopPropagation());
    meta.appendChild(a);
  }
  if (t.pr_url) {
    const a = document.createElement("a");
    a.className = "chip pr";
    a.href = t.pr_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "↗ pull request";
    a.addEventListener("click", (e) => e.stopPropagation());
    meta.appendChild(a);
  }
  el.appendChild(meta);

  if (t.status === "finished") {
    const btn = document.createElement("button");
    btn.className = "verify-btn";
    btn.textContent = "Mark verified";
    btn.addEventListener("click", (e) => { e.stopPropagation(); verifyTicket(t.id); });
    el.appendChild(btn);
  }

  if (el.draggable) {
    const handle = document.createElement("span");
    handle.className = "drag-handle";
    handle.textContent = "⋮⋮";
    el.appendChild(handle);
  }

  el.addEventListener("click", () => openDrawer(t.id));
  el.addEventListener("keydown", (e) => {
    if (e.target !== el) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDrawer(t.id); }
  });
  el.addEventListener("dragstart", (e) => {
    state.dragging = { id: t.id, status: t.status };
    el.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", t.id);
  });
  el.addEventListener("dragend", () => {
    el.classList.remove("dragging");
    $$(".card").forEach((c) => c.classList.remove("drop-above", "drop-below"));
    state.dragging = null;
  });
  el.addEventListener("dragover", (e) => {
    if (!state.dragging || state.dragging.id === t.id || state.dragging.status !== t.status) return;
    e.preventDefault();
    const rect = el.getBoundingClientRect();
    const above = e.clientY < rect.top + rect.height / 2;
    el.classList.toggle("drop-above", above);
    el.classList.toggle("drop-below", !above);
  });
  el.addEventListener("dragleave", () => el.classList.remove("drop-above", "drop-below"));
  el.addEventListener("drop", async (e) => {
    e.preventDefault();
    if (!state.dragging || state.dragging.status !== t.status) return;
    const rect = el.getBoundingClientRect();
    const above = e.clientY < rect.top + rect.height / 2;
    el.classList.remove("drop-above", "drop-below");
    await reorderWithin(t.status, state.dragging.id, t.id, above);
  });
  return el;
}

function renderBoard() {
  $(`.col[data-status="verified"]`).hidden = !state.showVerified;
  $("#board").classList.toggle("show-verified", state.showVerified);
  for (const status of Object.keys(STATUS_LABEL)) {
    const container = $(`.col-cards[data-status="${status}"]`);
    container.innerHTML = "";
    const tickets = state.tickets
      .filter((t) => t.status === status)
      .sort((a, b) =>
        status === "verified"
          ? (b.verified_at ?? b.updated_at) - (a.verified_at ?? a.updated_at)
          : status === "finished"
            ? (b.finished_at ?? b.updated_at) - (a.finished_at ?? a.updated_at)
            : a.sort_order - b.sort_order || a.created_at - b.created_at);
    if (tickets.length) {
      for (const t of tickets) container.appendChild(cardEl(t));
    } else {
      const empty = document.createElement("p");
      empty.className = "lane-empty";
      empty.textContent = LANE_EMPTY[status];
      container.appendChild(empty);
    }
    $(`.col[data-status="${status}"] .col-count`).textContent = tickets.length || "";
  }
}

async function reorderWithin(status, draggedId, targetId, above) {
  const ordered = state.tickets
    .filter((t) => t.status === status)
    .sort((a, b) => a.sort_order - b.sort_order || a.created_at - b.created_at)
    .map((t) => t.id)
    .filter((id) => id !== draggedId);
  const idx = ordered.indexOf(targetId);
  ordered.splice(above ? idx : idx + 1, 0, draggedId);
  // optimistic update
  ordered.forEach((id, i) => {
    const t = state.tickets.find((x) => x.id === id);
    if (t) t.sort_order = i;
  });
  renderBoard();
  try {
    await api(`/api/workspaces/${state.ws.id}/reorder`, {
      method: "POST",
      body: JSON.stringify({ status, ordered_ids: ordered }),
    });
  } catch (e) {
    console.error(`reorder failed: ${e.message}`);
    refreshBoard();
  }
}

/* --------------------------------------------------------------- tickets */

async function submitTicket() {
  const ta = $("#new-ticket-body");
  const body = ta.value.trim();
  if (!body || !state.ws) return;
  const btn = $("#new-ticket-submit");
  btn.disabled = true;
  try {
    const ticket = await api(`/api/workspaces/${state.ws.id}/tickets`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
    ta.value = "";
    const files = state.newTicketFiles.splice(0);
    renderNewTicketFiles();
    const failed = [];
    for (const f of files) {
      try { await uploadAttachment(ticket.id, f); }
      catch (e) { failed.push(e.message); }
    }
    if (failed.length) console.error(`ticket submitted, but upload failed: ${failed.join("; ")}`);
    await refreshBoard();
  } catch (e) {
    console.error(`submit failed: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
}

function renderNewTicketFiles() {
  const wrap = $("#new-ticket-files");
  wrap.innerHTML = "";
  wrap.hidden = !state.newTicketFiles.length;
  state.newTicketFiles.forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "file-chip";
    chip.textContent = `${isImage(f.type) ? "🖼" : "📎"} ${f.name} · ${fmtSize(f.size)}`;
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "file-chip-rm";
    rm.textContent = "✕";
    rm.title = "remove";
    rm.addEventListener("click", () => {
      state.newTicketFiles.splice(i, 1);
      renderNewTicketFiles();
    });
    chip.appendChild(rm);
    wrap.appendChild(chip);
  });
}

async function uploadDrawerFiles(files) {
  if (!state.drawerTicketId || !files.length) return;
  const failed = [];
  for (const f of files) {
    try { await uploadAttachment(state.drawerTicketId, f); }
    catch (e) { failed.push(e.message); }
  }
  if (failed.length) console.error(`upload failed: ${failed.join("; ")}`);
  await refreshBoard();
}

async function verifyTicket(id) {
  try {
    await api(`/api/tickets/${id}/verify`, { method: "POST" });
    await refreshBoard();
  } catch (e) {
    console.error(`verify failed: ${e.message}`);
  }
}

function toggleVerified() {
  state.showVerified = !state.showVerified;
  localStorage.setItem("vibe.showVerified", state.showVerified ? "1" : "0");
  renderVerifiedToggle();
  renderBoard();
}

function renderVerifiedToggle() {
  const btn = $("#show-verified");
  btn.textContent = state.showVerified ? "Hide verified" : "Show verified";
  btn.classList.toggle("active", state.showVerified);
}

/* ---------------------------------------------------------------- drawer */

function openDrawer(ticketId) {
  state.returnFocus = document.activeElement;
  state.drawerTicketId = ticketId;
  $("#drawer").hidden = false;
  renderDrawer();
  $("#append-body").focus();
}

function closeDrawer() {
  if (!state.drawerTicketId) return;
  state.drawerTicketId = null;
  $("#drawer").hidden = true;
  const back = state.returnFocus;
  state.returnFocus = null;
  if (back?.isConnected) back.focus();
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function renderDrawer() {
  const t = state.tickets.find((x) => x.id === state.drawerTicketId);
  if (!t) { closeDrawer(); return; }
  const st = $("#drawer-status");
  st.textContent = STATUS_LABEL[t.status];
  st.dataset.s = t.status;
  $("#drawer-id").textContent = `#${t.id}`;
  const title = $("#drawer-title");
  title.hidden = !t.title;
  title.textContent = t.title || "";

  const links = $("#drawer-links");
  links.innerHTML = "";
  if (t.conversation_url) {
    const a = document.createElement("a");
    a.className = "chip convo";
    a.href = t.conversation_url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = "↗ open conversation";
    links.appendChild(a);
  }
  if (t.pr_url) {
    const a = document.createElement("a");
    a.className = "chip pr";
    a.href = t.pr_url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = "↗ open pull request";
    links.appendChild(a);
  }
  if (t.llm_model) links.appendChild(modelChip(t.llm_model));

  const note = $("#drawer-note");
  note.hidden = !t.manager_note;
  note.textContent = t.manager_note ? `⚑ manager: ${t.manager_note}` : "";

  const activity = $("#drawer-activity");
  const act = t.status === "in_progress" ? t.latest_action : null;
  activity.hidden = !act?.summary;
  activity.innerHTML = "";
  if (act?.summary) {
    activity.title = act.tool ? `tool: ${act.tool}` : "";
    activity.innerHTML = `<span class="activity-dot"></span>`;
    const text = document.createElement("span");
    text.className = "activity-text";
    text.textContent = act.summary;
    activity.appendChild(text);
  }

  const atts = $("#drawer-attachments");
  atts.innerHTML = "";
  atts.hidden = !(t.attachments || []).length;
  for (const a of t.attachments || []) atts.appendChild(attachmentEl(a));

  const thread = $("#drawer-thread");
  const atBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 40;
  thread.innerHTML = "";
  for (const e of t.entries) {
    const div = document.createElement("div");
    div.className = "entry";
    const head = document.createElement("div");
    head.className = "entry-head";
    head.innerHTML = `<span class="entry-author-${e.author}">${e.author}</span><span>${fmtTime(e.created_at)}</span>`;
    const body = document.createElement("div");
    body.className = "entry-body";
    body.textContent = e.body;
    div.append(head, body);
    thread.appendChild(div);
  }
  if (atBottom) thread.scrollTop = thread.scrollHeight;
}

async function appendEntry() {
  const ta = $("#append-body");
  const body = ta.value.trim();
  if (!body || !state.drawerTicketId) return;
  try {
    await api(`/api/tickets/${state.drawerTicketId}/entries`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
    ta.value = "";
    await refreshBoard();
  } catch (e) {
    console.error(`append failed: ${e.message}`);
  }
}

/* -------------------------------------------------------------- settings */

async function patchWorkspace(patch) {
  if (!state.ws) return;
  try {
    state.ws = await api(`/api/workspaces/${state.ws.id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    renderSettings();
  } catch (e) {
    console.error(`settings failed: ${e.message}`);
  }
}

/* ------------------------------------------------------------------ init */

function ticketKeydown(submit) {
  return (e) => {
    if (e.key !== "Enter") return;
    // Shift+Enter inserts a newline; Enter and Cmd/Ctrl+Enter submit.
    if (e.shiftKey) return;
    e.preventDefault();
    submit();
  };
}

function wire() {
  $("#workspace-select").addEventListener("change", (e) => selectWorkspace(e.target.value));

  $("#new-ticket-form").addEventListener("submit", (e) => { e.preventDefault(); submitTicket(); });
  $("#new-ticket-body").addEventListener("keydown", ticketKeydown(submitTicket));

  $("#new-ticket-attach").addEventListener("click", () => $("#new-ticket-file-input").click());
  $("#new-ticket-file-input").addEventListener("change", (e) => {
    state.newTicketFiles.push(...e.target.files);
    e.target.value = "";
    renderNewTicketFiles();
  });

  $("#append-form").addEventListener("submit", (e) => { e.preventDefault(); appendEntry(); });
  $("#append-body").addEventListener("keydown", ticketKeydown(appendEntry));

  $("#drawer-attach").addEventListener("click", () => $("#drawer-file-input").click());
  $("#drawer-file-input").addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    await uploadDrawerFiles(files);
  });

  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  $("#max-concurrent").addEventListener("change", (e) => {
    const v = parseInt(e.target.value, 10);
    if (v >= 1 && v <= 20) patchWorkspace({ max_concurrent: v });
  });
  $$("#push-mode .seg-btn").forEach((b) =>
    b.addEventListener("click", () => patchWorkspace({ push_mode: b.dataset.mode })));

  $("#show-verified").addEventListener("click", toggleVerified);
  renderVerifiedToggle();

  $("#theme-toggle").addEventListener("click", toggleTheme);
  $("#mgr-badge").addEventListener("click", triggerManager);
  $("#mgr-badge").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); triggerManager(); }
  });
  applyTheme();
}

/* ------------------------------------------------------------------ theme */

function applyTheme() {
  const light = state.theme === "light";
  if (light) document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  const btn = $("#theme-toggle");
  btn.textContent = light ? "Dark" : "Light";
  btn.title = light ? "Switch to dark mode" : "Switch to light mode";
}

function toggleTheme() {
  state.theme = state.theme === "light" ? "dark" : "light";
  localStorage.setItem("vibe.theme", state.theme);
  applyTheme();
}

async function init() {
  wire();
  window.addEventListener("popstate", async () => {
    const name = workspaceNameFromURL();
    const path = name ? workspacePathFromName(name) : null;
    $("#workspace-select").value = path || "";
    await selectWorkspace(path || "", { historyMode: "none" });
  });
  await loadWorkspaces();

  const urlName = workspaceNameFromURL();
  const urlPath = urlName ? workspacePathFromName(urlName) : null;
  const saved = localStorage.getItem("vibe.workspace");
  const savedOk = saved && [...$("#workspace-select").options].some((o) => o.value === saved);
  const target = urlPath || (savedOk ? saved : null);

  if (target) {
    $("#workspace-select").value = target;
    await selectWorkspace(target, { historyMode: "replace" });
  } else {
    if (urlName) console.error(`unknown workspace: ${urlName}`);
    history.replaceState({}, "", "/");
    render();
  }
}

init();
