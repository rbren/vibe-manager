# Filesystem store layout

Replaces the SQLite database. All state lives as JSON under a single root so
both the Canvas extension (via the agent-server file API) and
`automation/main.py` (via plain filesystem reads) can use it.

Root: `$HOME/.openhands/vibe-manager/` (override with `VIBE_STORE_DIR`).

The extension resolves `$HOME` at runtime from `GET /api/file/home` rather
than assuming a user: the file API needs absolute paths and does not expand
`~`, and the agent server does not always run as root.

```
<root>/
  index.json                      # workspace registry
  workspaces/<ws_id>/board.json   # one document per workspace
  attachments/<att_id>/<filename> # attachment bytes (unchanged layout)
```

## Why one document per workspace

The board is always read whole (the SPA polls the full board every 5s) and
written by one actor at a time. A single document makes a read one request
instead of one-per-ticket, and makes a write atomic by construction — there is
no partial-board state to observe. Largest real board is 84.6 KB; a full
round-trip through the file API measures ~38 ms, well inside the 5s poll.

## index.json

```json
{
  "version": 1,
  "workspaces": [
    {"id": "...", "path": "/root/git/foo", "name": "foo",
     "max_concurrent": 2, "push_mode": "main",
     "automation_id": "...", "manager_conversation_id": "...",
     "created_at": "..."}
  ]
}
```

## workspaces/<ws_id>/board.json

```json
{
  "version": 1,
  "workspace_id": "...",
  "tickets": [
    {"id": "...", "status": "pending", "title": null, "sort_order": 1.0,
     "conversation_id": null, "pr_url": null, "manager_note": null,
     "dispatched_entry_count": 0,
     "created_at": "...", "updated_at": "...",
     "finished_at": null, "verified_at": null,
     "entries": [{"id": "...", "author": "user", "body": "...",
                  "created_at": "..."}],
     "attachments": [{"id": "...", "filename": "a.png",
                      "content_type": "image/png", "size": 123,
                      "created_at": "...", "path": "/abs/path"}]}
  ]
}
```

Entries and attachments are embedded rather than kept in side tables: they are
only ever read as part of a ticket, and embedding keeps a board read to one
request.

Derived fields that the old API computed per request — `conversation_url`,
`latest_action`, `llm_model` — are NOT stored. The extension derives them
client-side so they cannot go stale on disk.

`attachments[].url` is likewise not stored; the extension builds a blob URL,
because an `<img src>` cannot send the session key. `attachments[].path` stays
absolute on this machine so dispatched workers can read files in place.

## Concurrency

Single-user, low-scale. Writers are the extension (user actions) and the
manager automation (once a minute). Both do read-modify-write of one board
document. A lost update is possible in principle but requires edits inside the
same few-millisecond window; the previous SQLite design serialized these, so
this is a deliberate, documented trade-down rather than an oversight.
