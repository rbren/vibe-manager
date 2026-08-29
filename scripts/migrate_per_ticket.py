#!/usr/bin/env python3
"""Split each workspace's board.json into per-ticket files.

The board used to be one document written by the browser, the automation and
vibectl alike, so any two concurrent writers silently lost one of the edits.
Per-ticket files remove the shared document; see store.js for the measurements.

Idempotent: re-running only writes tickets that are missing or stale. The
original board.json is renamed to board.json.migrated rather than deleted, so
the pre-split state is recoverable.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "automation"))

import vibestore  # noqa: E402


def migrate_workspace(ws_id: str, *, apply: bool) -> tuple[int, int]:
    """Return (tickets written, tickets already present)."""
    legacy = vibestore.board_path(ws_id)
    if not legacy.is_file():
        return (0, 0)

    board = json.loads(legacy.read_text())
    written = skipped = 0
    for ticket in board.get("tickets", []):
        target = vibestore.ticket_path(ws_id, ticket["id"])
        if target.is_file() and json.loads(target.read_text()).get("rev"):
            skipped += 1
            continue
        if apply:
            vibestore.write_ticket(ws_id, ticket)
        written += 1

    if apply:
        # Keep the pre-split document; read_board only consults it when
        # tickets/ is absent, so leaving it in place changes nothing.
        legacy.rename(legacy.with_suffix(".json.migrated"))
    return (written, skipped)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the files (default is a dry run)")
    args = parser.parse_args()

    root = vibestore.store_root()
    workspaces = sorted(p.name for p in (root / "workspaces").iterdir() if p.is_dir())
    print(f"store root: {root}")
    print(f"{'APPLY' if args.apply else 'DRY RUN'} - {len(workspaces)} workspace(s)\n")

    total = 0
    for ws_id in workspaces:
        written, skipped = migrate_workspace(ws_id, apply=args.apply)
        total += written
        if written or skipped:
            print(f"  {ws_id}: {written} to write, {skipped} already migrated")
        else:
            print(f"  {ws_id}: nothing to do (no board.json)")

    print(f"\n{total} ticket file(s) {'written' if args.apply else 'would be written'}")
    if not args.apply:
        print("re-run with --apply to perform the migration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
