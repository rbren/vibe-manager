#!/usr/bin/env python3
"""Migrate the vibe-manager SQLite database into the JSON filesystem store.

Idempotent: rewrites index.json and every board.json from the database, and
copies attachment bytes that are not already present. Safe to re-run.

Usage:
    python3 scripts/migrate_to_fs.py [--db vibe.db] [--root ~/.openhands/vibe-manager]
                                     [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "vibe.db"
DEFAULT_SRC_ATTACHMENTS = REPO_ROOT / "data" / "attachments"

TICKET_COLUMNS = (
    "id", "status", "title", "sort_order", "conversation_id", "pr_url",
    "manager_note", "dispatched_entry_count", "created_at", "updated_at",
)
WORKSPACE_COLUMNS = (
    "id", "path", "name", "max_concurrent", "push_mode", "automation_id",
    "created_at",
)


def default_root() -> Path:
    env = os.environ.get("VIBE_STORE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".openhands" / "vibe-manager"


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def pick(row: sqlite3.Row, columns, available: set[str]) -> dict:
    """Project a row onto the known columns, tolerating schema drift."""
    return {c: (row[c] if c in available else None) for c in columns}


def build_ticket(conn, row, available, att_available, attachments_dir: Path) -> dict:
    ticket = pick(row, TICKET_COLUMNS, available)
    # Columns added by later migrations; absent in older databases.
    for optional in ("finished_at", "verified_at"):
        ticket[optional] = row[optional] if optional in available else None

    ticket["entries"] = [
        {
            "id": e["id"],
            "author": e["author"],
            "body": e["body"],
            "created_at": e["created_at"],
        }
        for e in conn.execute(
            "SELECT id, author, body, created_at FROM entries "
            "WHERE ticket_id=? ORDER BY created_at",
            (row["id"],),
        )
    ]

    ticket["attachments"] = []
    for a in conn.execute(
        "SELECT * FROM attachments WHERE ticket_id=? ORDER BY created_at",
        (row["id"],),
    ):
        att = {
            "id": a["id"],
            "filename": a["filename"],
            "content_type": a["content_type"] if "content_type" in att_available else None,
            "size": a["size"] if "size" in att_available else None,
            "created_at": a["created_at"],
        }
        att["path"] = str(attachments_dir / a["id"] / a["filename"])
        ticket["attachments"].append(att)

    return ticket


def write_json(path: Path, payload: dict, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(path)


def migrate(db_path: Path, root: Path, src_attachments: Path, dry_run: bool) -> int:
    if not db_path.exists():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    ws_available = table_columns(conn, "workspaces")
    ticket_available = table_columns(conn, "tickets")
    att_available = table_columns(conn, "attachments")

    dest_attachments = root / "attachments"

    workspaces = []
    total_tickets = total_entries = total_attachments = 0

    for ws in conn.execute("SELECT * FROM workspaces ORDER BY created_at"):
        entry = pick(ws, WORKSPACE_COLUMNS, ws_available)
        entry["manager_conversation_id"] = (
            ws["manager_conversation_id"]
            if "manager_conversation_id" in ws_available
            else None
        )
        # Added by a later migration; databases without it get the default theme.
        entry["accent"] = ws["accent"] if "accent" in ws_available else "ember"
        workspaces.append(entry)

        tickets = [
            build_ticket(conn, t, ticket_available, att_available, dest_attachments)
            for t in conn.execute(
                "SELECT * FROM tickets WHERE workspace_id=? ORDER BY sort_order, created_at",
                (ws["id"],),
            )
        ]
        total_tickets += len(tickets)
        total_entries += sum(len(t["entries"]) for t in tickets)
        total_attachments += sum(len(t["attachments"]) for t in tickets)

        write_json(
            root / "workspaces" / ws["id"] / "board.json",
            {"version": 1, "workspace_id": ws["id"], "tickets": tickets},
            dry_run,
        )
        print(f"  {ws['name']:<16} {len(tickets):>4} tickets")

    write_json(root / "index.json", {"version": 1, "workspaces": workspaces}, dry_run)

    copied = missing = 0
    for a in conn.execute("SELECT id, filename FROM attachments"):
        src = src_attachments / a["id"] / a["filename"]
        dest = dest_attachments / a["id"] / a["filename"]
        if not src.exists():
            # Row without bytes on disk: the ticket keeps the metadata chip,
            # so surface it rather than failing the whole migration.
            print(f"  warning: attachment bytes missing: {src}", file=sys.stderr)
            missing += 1
            continue
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            continue
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        copied += 1

    # Rows whose parent was deleted were already unreachable through the API;
    # report them so the migrated counts can be reconciled against the DB.
    orphan_entries = conn.execute(
        "SELECT count(*) FROM entries e LEFT JOIN tickets t ON t.id = e.ticket_id "
        "WHERE t.id IS NULL"
    ).fetchone()[0]

    conn.close()

    print(
        f"\n{'(dry run) ' if dry_run else ''}"
        f"{len(workspaces)} workspaces, {total_tickets} tickets, "
        f"{total_entries} entries, {total_attachments} attachments "
        f"({copied} files copied, {missing} missing)"
    )
    if orphan_entries:
        print(
            f"skipped {orphan_entries} orphaned entr"
            f"{'y' if orphan_entries == 1 else 'ies'} (parent ticket deleted)"
        )
    print(f"store root: {root}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--src-attachments", type=Path, default=DEFAULT_SRC_ATTACHMENTS)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    root = (args.root or default_root()).expanduser()
    return migrate(args.db.expanduser(), root, args.src_attachments.expanduser(), args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
