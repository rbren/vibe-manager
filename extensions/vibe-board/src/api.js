/* Talking to the vibe-manager service from inside Canvas.

   The standalone SPA is served BY the vibe-manager service, so it can use
   same-origin relative paths. The extension runs on the Canvas origin, so every
   call needs an absolute base URL and credentials (the public vhost sits behind
   nginx basic auth; `credentials: "include"` reuses the browser's existing
   session rather than asking the user for it again).

   The base URL is remembered per Canvas backend, so pointing Canvas at a
   different machine doesn't silently reuse the previous machine's board. */

const STORAGE_PREFIX = "vibe.ext.apiBase";

export function storageKey(backendId) {
  return `${STORAGE_PREFIX}:${backendId || "unknown"}`;
}

export function normalizeBase(raw) {
  const trimmed = (raw || "").trim();
  if (!trimmed) return "";
  const withScheme = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
  try {
    const url = new URL(withScheme);
    return url.origin + url.pathname.replace(/\/+$/, "");
  } catch {
    return "";
  }
}

export function loadSavedBase(backendId) {
  try {
    return localStorage.getItem(storageKey(backendId)) || "";
  } catch {
    return "";
  }
}

export function saveBase(backendId, base) {
  try {
    if (base) localStorage.setItem(storageKey(backendId), base);
    else localStorage.removeItem(storageKey(backendId));
  } catch {
    /* private mode / storage disabled - the board still works for this session */
  }
}

export class VibeApi {
  constructor(base) {
    this.base = base;
  }

  url(path) {
    return `${this.base}${path}`;
  }

  async request(path, opts = {}) {
    const res = await fetch(this.url(path), {
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch {
        /* non-JSON error body - keep the status text */
      }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res.json();
  }

  // Raw-body upload (no multipart): file bytes as the POST body, name in query.
  async uploadAttachment(ticketId, file) {
    const res = await fetch(
      this.url(`/api/tickets/${ticketId}/attachments?filename=${encodeURIComponent(file.name)}`),
      {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      },
    );
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch {
        /* non-JSON error body */
      }
      throw new Error(`${file.name}: ${detail}`);
    }
    return res.json();
  }
}

/** Probe a candidate base URL so setup fails loudly instead of on first poll. */
export async function probeBase(base) {
  const res = await fetch(`${base}/api/health`, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}
