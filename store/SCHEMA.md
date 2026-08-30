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
  bin/<ws_id>/                    # that workspace's manager CLI + config.json
```

## Why one document per workspace

The board is always read whole (the SPA polls the full board every 5s). A
single document makes a read one request instead of one-per-ticket, and leaves
no partial-board state to observe. Largest real board is 84.6 KB; a full
round-trip through the file API measures ~38 ms, well inside the 5s poll. The
cost is that every write is a read-modify-write shared by several actors — see
Concurrency.

## index.json

```json
{
  "version": 1,
  "workspaces": [
    {"id": "...", "path": "/root/git/foo", "name": "foo",
     "max_concurrent": 2, "push_mode": "main", "accent": "ember",
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

Writers are the extension (user actions, through the file API), the manager
automation (once a minute per workspace) and the manager agent's `vibectl`.
Every one of them read-modify-writes a whole document, and no lock reaches
across the file API, so `index.json` and every `board.json` carry two extra
fields:

- `rev` — incremented on every write.
- `writer` — a random token identifying the write that produced this state.

Both writers follow the same protocol (`Store.mutateDoc` in
`extensions/kanban-manager/src/store.js`, `_mutate_document` in
`automation/vibestore.py`): read, apply the mutation, re-read and abandon the
attempt if `rev` moved, write, then re-read and re-apply the mutation on the
fresh document if someone else's write won. The shell side additionally holds
an `flock` on `<document>.lock`, which serializes the automation against the
manager CLI, and the extension chains its own cycles (`Store.serialize`) so
one tab's actions queue instead of racing each other.

Untracked writers must preserve `rev`/`writer` and bump `rev`, or they will be
treated as a lost write and retried over. A card added by the user used to
disappear here: the manager patched a ticket from a board it had read a moment
before the card existed, and wrote it straight back.
