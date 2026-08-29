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
  workspaces/<ws_id>/tickets/<ticket_id>/ticket.json   # one file per ticket
  attachments/<att_id>/<filename> # attachment bytes (unchanged layout)
  bin/<ws_id>/                    # that workspace's manager CLI + config.json
```

## Why one file per ticket

The board was originally one `board.json` per workspace, which made a poll a
single request. That shape could not be made safe. Several actors write
concurrently (see Concurrency), every write was a read-modify-write of the
whole board, and the file API has no conditional upload to build a
compare-and-swap on — an upload carrying a stale `If-Match` is accepted, not
rejected. Measured on a 40-run interleaving sweep: the manager's edit was
silently lost 35/40 times, and adding `rev`/`writer` tokens only brought that
to 8/40, because a writer can confirm its own write landed without ever
noticing it destroyed someone else's.

Per-ticket files remove the shared document, so writers touching different
tickets cannot interfere at all — the same sweep now loses 0/40. The cost is
that a poll is one listing plus one read per ticket. Measured on the largest
real board (88 tickets): ~850 ms worst case, ~17% of the 5s poll interval.

Each ticket is a **directory** containing `ticket.json`, not a bare file,
because the file API can enumerate subdirectories (`search_subdirs`) but has
no endpoint that lists files. The directory shape is what makes the board
reconstructible.

Boards written before the split are still read from `board.json` when
`tickets/` is absent, so an unmigrated workspace keeps working;
`scripts/migrate_per_ticket.py` performs the split and renames the old
document to `board.json.migrated`.

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

## workspaces/<ws_id>/tickets/<ticket_id>/ticket.json

One ticket per file. The board is the set of these files, ordered by
`sort_order` with `created_at` breaking ties.

```json
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
```

Entries and attachments are embedded rather than kept in side tables: they are
only ever read as part of a ticket, and embedding keeps a ticket read to one
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
No lock reaches across the file API, so the layout does the work: **the primary
defence is that concurrent writers no longer share a document.** A card the
user added used to disappear because the manager patched a ticket from a board
it had read a moment before the card existed, and wrote the whole thing back.
With one file per ticket that write cannot touch the new card at all.

Two documents are still genuinely shared — `index.json`, and a single ticket
when two writers both target it — so those carry:

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

This protocol is a backstop, not a guarantee: without a conditional upload it
detects a race rather than preventing one, which is why the layout — not the
tokens — is what keeps tickets safe. Untracked writers must still preserve
`rev`/`writer` and bump `rev`.

Reads are cache-busted (a `_` query param plus `Cache-Control: no-cache`) on
both the ticket downloads and the directory listing. The file API sends
`ETag`/`Last-Modified` but no `Cache-Control`, so a cached listing would omit
a ticket directory created moments earlier — the "my card never showed up"
symptom.
