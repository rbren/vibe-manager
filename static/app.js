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
  dragging: null,        // { id, status }
};

const STATUS_LABEL = {
  pending: "pending",
  in_progress: "in progress",
  needs_input: "needs input",
  finished: "finished",
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

/* ---------------------------------------------------------------- toast */

let toastTimer;
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 3500);
}

/* ----------------------------------------------------------- workspaces */

async function loadWorkspaces() {
  const data = await api("/api/workspaces");
  state.workspaces = data;
  const sel = $("#workspace-select");
  const current = sel.value;
  sel.innerHTML = '<option value="">— pick a workspace —</option>';

  const selectedPaths = new Set(data.selected.map((w) => w.path));
  if (data.selected.length) {
    const og = document.createElement("optgroup");
    og.label = "active";
    for (const w of data.selected) {
      const o = document.createElement("option");
      o.value = w.path;
      o.textContent = `${w.name}  ·  ${w.path}`;
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
      o.textContent = `${a.name}${a.is_git ? "" : "  (not git)"}  ·  ${a.path}`;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  sel.value = current || (localStorage.getItem("vibe.workspace") ?? "");
}

async function selectWorkspace(path) {
  if (!path) {
    state.ws = null;
    localStorage.removeItem("vibe.workspace");
    render();
    return;
  }
  try {
    const ws = await api("/api/workspaces", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    state.ws = ws;
    localStorage.setItem("vibe.workspace", path);
    if (ws.automation_id) toast("manager automation active ✓");
    else toast("workspace selected — manager automation could not be created", true);
    await refreshBoard();
    startPolling();
    await loadWorkspaces();
    $("#workspace-select").value = path;
  } catch (e) {
    toast(`workspace error: ${e.message}`, true);
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
}

function render() {
  const has = !!state.ws;
  $("#empty-state").hidden = has;
  $("#board-wrap").hidden = !has;
  $("#ctl-concurrency").hidden = !has;
  $("#ctl-pushmode").hidden = !has;
  $("#mgr-badge").hidden = !(has && state.ws.automation_id);
  if (has) { renderBoard(); renderSettings(); }
}

function renderSettings() {
  if (!state.ws) return;
  const mc = $("#max-concurrent");
  if (document.activeElement !== mc) mc.value = state.ws.max_concurrent;
  $$("#push-mode .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === state.ws.push_mode));
  $("#mgr-badge").hidden = !state.ws.automation_id;
}

function cardEl(t) {
  const firstEntry = t.entries[0]?.body ?? "";
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.id = t.id;
  el.draggable = true;

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

  const meta = document.createElement("div");
  meta.className = "card-meta";
  meta.innerHTML = `<span>#${t.id.slice(0, 6)}</span>`;
  if (t.entries.length > 1) {
    const chip = document.createElement("span");
    chip.className = "chip entries";
    chip.textContent = `✎ ${t.entries.length}`;
    meta.appendChild(chip);
  }
  if (t.conversation_url) {
    const a = document.createElement("a");
    a.className = "chip convo";
    a.href = t.conversation_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "⇢ convo";
    a.addEventListener("click", (e) => e.stopPropagation());
    meta.appendChild(a);
  }
  if (t.pr_url) {
    const a = document.createElement("a");
    a.className = "chip pr";
    a.href = t.pr_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "⇡ PR";
    a.addEventListener("click", (e) => e.stopPropagation());
    meta.appendChild(a);
  }
  el.appendChild(meta);

  const handle = document.createElement("span");
  handle.className = "drag-handle";
  handle.textContent = "⋮⋮";
  el.appendChild(handle);

  el.addEventListener("click", () => openDrawer(t.id));
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
  for (const status of Object.keys(STATUS_LABEL)) {
    const container = $(`.col-cards[data-status="${status}"]`);
    container.innerHTML = "";
    const tickets = state.tickets
      .filter((t) => t.status === status)
      .sort((a, b) => a.sort_order - b.sort_order || a.created_at - b.created_at);
    for (const t of tickets) container.appendChild(cardEl(t));
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
    toast(`reorder failed: ${e.message}`, true);
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
    await api(`/api/workspaces/${state.ws.id}/tickets`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
    ta.value = "";
    toast("ticket submitted — manager will pick it up");
    await refreshBoard();
  } catch (e) {
    toast(`submit failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- drawer */

function openDrawer(ticketId) {
  state.drawerTicketId = ticketId;
  $("#drawer").hidden = false;
  renderDrawer();
  $("#append-body").focus();
}

function closeDrawer() {
  state.drawerTicketId = null;
  $("#drawer").hidden = true;
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

  const links = $("#drawer-links");
  links.innerHTML = "";
  if (t.conversation_url) {
    const a = document.createElement("a");
    a.className = "chip convo";
    a.href = t.conversation_url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = "⇢ open conversation";
    links.appendChild(a);
  }
  if (t.pr_url) {
    const a = document.createElement("a");
    a.className = "chip pr";
    a.href = t.pr_url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = "⇡ open pull request";
    links.appendChild(a);
  }

  const note = $("#drawer-note");
  note.hidden = !t.manager_note;
  note.textContent = t.manager_note ? `⚑ manager: ${t.manager_note}` : "";

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
    toast("request appended");
    await refreshBoard();
  } catch (e) {
    toast(`append failed: ${e.message}`, true);
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
    toast("settings saved");
  } catch (e) {
    toast(`settings failed: ${e.message}`, true);
  }
}

/* ------------------------------------------------------------------ init */

function wire() {
  $("#workspace-select").addEventListener("change", (e) => selectWorkspace(e.target.value));

  $("#new-ticket-form").addEventListener("submit", (e) => { e.preventDefault(); submitTicket(); });
  $("#new-ticket-body").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); submitTicket(); }
  });

  $("#append-form").addEventListener("submit", (e) => { e.preventDefault(); appendEntry(); });
  $("#append-body").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); appendEntry(); }
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
}

async function init() {
  wire();
  await loadWorkspaces();
  const saved = localStorage.getItem("vibe.workspace");
  if (saved && [...$("#workspace-select").options].some((o) => o.value === saved)) {
    $("#workspace-select").value = saved;
    await selectWorkspace(saved);
  } else {
    render();
  }
}

init();
