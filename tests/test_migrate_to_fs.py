#!/usr/bin/env python3
"""Verify the SQLite -> JSON migration is faithful and idempotent.

Builds a synthetic database covering the awkward cases (missing optional
columns, orphaned entry, attachment without bytes), migrates it, and asserts
the JSON store matches the database row for row.

Run: python3 tests/test_migrate_to_fs.py   (pure stdlib)
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "migrate_to_fs.py"

failures = []


def check(label, actual, expected):
    if actual == expected:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
        failures.append(label)


def build_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT, name TEXT,
            max_concurrent INTEGER, push_mode TEXT, automation_id TEXT,
            manager_conversation_id TEXT, created_at REAL);
        CREATE TABLE tickets (id TEXT PRIMARY KEY, workspace_id TEXT, status TEXT,
            title TEXT, sort_order REAL, conversation_id TEXT, pr_url TEXT,
            manager_note TEXT, dispatched_entry_count INTEGER,
            created_at REAL, updated_at REAL, finished_at REAL, verified_at REAL);
        CREATE TABLE entries (id TEXT PRIMARY KEY, ticket_id TEXT, author TEXT,
            body TEXT, created_at REAL);
        CREATE TABLE attachments (id TEXT PRIMARY KEY, ticket_id TEXT,
            filename TEXT, content_type TEXT, size INTEGER, created_at REAL);
        """
    )
    conn.execute(
        "INSERT INTO workspaces VALUES ('w1','/root/git/foo','foo',2,'main','a1','c1',100.0)"
    )
    conn.execute("INSERT INTO workspaces VALUES ('w2','/root/git/bar','bar',1,'pr',NULL,NULL,200.0)")
    # Deliberately inserted out of sort order to prove ordering is applied.
    conn.execute(
        "INSERT INTO tickets VALUES ('t2','w1','pending','B',2.0,NULL,NULL,NULL,0,10.0,11.0,NULL,NULL)"
    )
    conn.execute(
        "INSERT INTO tickets VALUES ('t1','w1','finished','A',1.0,'conv1','http://pr',"
        "'note',3,1.0,2.0,50.0,NULL)"
    )
    conn.execute(
        "INSERT INTO tickets VALUES ('t3','w2','verified',NULL,1.0,NULL,NULL,NULL,0,"
        "5.0,6.0,60.0,70.0)"
    )
    conn.execute("INSERT INTO entries VALUES ('e1','t1','user','first',1.0)")
    conn.execute("INSERT INTO entries VALUES ('e2','t1','manager','second',2.0)")
    conn.execute("INSERT INTO entries VALUES ('e3','t2','user','other',3.0)")
    conn.execute("INSERT INTO entries VALUES ('orphan','gone','user','dangling',4.0)")
    conn.execute("INSERT INTO attachments VALUES ('a1','t1','shot.png','image/png',3,1.0)")
    conn.execute("INSERT INTO attachments VALUES ('a2','t1','gone.bin','application/octet-stream',9,2.0)")
    conn.commit()
    conn.close()


def run(tmp: Path, db: Path, root: Path, src_att: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--root", str(root),
         "--src-attachments", str(src_att)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = tmp / "vibe.db"
        root = tmp / "store"
        src_att = tmp / "src-attachments"
        build_db(db)

        (src_att / "a1").mkdir(parents=True)
        (src_att / "a1" / "shot.png").write_bytes(b"png")
        # a2 has a row but no bytes on disk.

        proc = run(tmp, db, root, src_att)
        check("exit code", proc.returncode, 0)
        if proc.returncode != 0:
            print(proc.stdout, proc.stderr)
            return 1

        print("\nindex.json")
        index = json.loads((root / "index.json").read_text())
        check("version", index["version"], 1)
        check("workspace count", len(index["workspaces"]), 2)
        w1 = index["workspaces"][0]
        check("workspace id", w1["id"], "w1")
        check("workspace name", w1["name"], "foo")
        check("max_concurrent", w1["max_concurrent"], 2)
        check("push_mode", w1["push_mode"], "main")
        check("automation_id", w1["automation_id"], "a1")
        check("manager_conversation_id", w1["manager_conversation_id"], "c1")
        check("null automation_id preserved", index["workspaces"][1]["automation_id"], None)

        print("\nboard.json (w1)")
        board = json.loads((root / "workspaces" / "w1" / "board.json").read_text())
        check("workspace_id", board["workspace_id"], "w1")
        check("ticket count", len(board["tickets"]), 2)
        check("sorted by sort_order", [t["id"] for t in board["tickets"]], ["t1", "t2"])

        t1 = board["tickets"][0]
        check("status", t1["status"], "finished")
        check("title", t1["title"], "A")
        check("conversation_id", t1["conversation_id"], "conv1")
        check("pr_url", t1["pr_url"], "http://pr")
        check("manager_note", t1["manager_note"], "note")
        check("dispatched_entry_count", t1["dispatched_entry_count"], 3)
        check("finished_at", t1["finished_at"], 50.0)
        check("verified_at", t1["verified_at"], None)
        check("entries embedded", [e["id"] for e in t1["entries"]], ["e1", "e2"])
        check("entry author", t1["entries"][1]["author"], "manager")
        check("entries ordered by created_at", t1["entries"][0]["body"], "first")

        check("attachments embedded", [a["id"] for a in t1["attachments"]], ["a1", "a2"])
        check("attachment filename", t1["attachments"][0]["filename"], "shot.png")
        check("attachment content_type", t1["attachments"][0]["content_type"], "image/png")
        check("attachment size", t1["attachments"][0]["size"], 3)
        check(
            "attachment path is absolute under store root",
            t1["attachments"][0]["path"],
            str(root / "attachments" / "a1" / "shot.png"),
        )
        check("derived url not stored", "url" in t1["attachments"][0], False)
        check("latest_action not stored", "latest_action" in t1, False)
        check("llm_model not stored", "llm_model" in t1, False)

        print("\nboard.json (w2)")
        b2 = json.loads((root / "workspaces" / "w2" / "board.json").read_text())
        check("verified ticket kept", b2["tickets"][0]["status"], "verified")
        check("verified_at", b2["tickets"][0]["verified_at"], 70.0)
        check("null title preserved", b2["tickets"][0]["title"], None)
        check("ticket with no entries", b2["tickets"][0]["entries"], [])

        print("\nattachment bytes")
        check(
            "bytes copied",
            (root / "attachments" / "a1" / "shot.png").read_bytes(),
            b"png",
        )
        check(
            "missing bytes not fabricated",
            (root / "attachments" / "a2" / "gone.bin").exists(),
            False,
        )
        check("missing reported", "missing" in proc.stderr or "missing" in proc.stdout, True)
        check("orphan reported", "orphan" in proc.stdout.lower(), True)
        check("orphan excluded from boards", "dangling" not in (root / "workspaces" / "w1" / "board.json").read_text(), True)

        print("\nidempotence")
        first = (root / "workspaces" / "w1" / "board.json").read_text()
        proc2 = run(tmp, db, root, src_att)
        check("second run exit code", proc2.returncode, 0)
        check("board unchanged", (root / "workspaces" / "w1" / "board.json").read_text(), first)
        check("no .tmp files left", list(root.rglob("*.tmp")), [])

        print("\nfidelity vs database")
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        db_tickets = conn.execute("SELECT count(*) FROM tickets").fetchone()[0]
        db_entries = conn.execute(
            "SELECT count(*) FROM entries e JOIN tickets t ON t.id=e.ticket_id"
        ).fetchone()[0]
        db_atts = conn.execute("SELECT count(*) FROM attachments").fetchone()[0]
        conn.close()

        boards = [json.loads(p.read_text()) for p in root.glob("workspaces/*/board.json")]
        got_tickets = sum(len(b["tickets"]) for b in boards)
        got_entries = sum(len(t["entries"]) for b in boards for t in b["tickets"])
        got_atts = sum(len(t["attachments"]) for b in boards for t in b["tickets"])
        check("all tickets migrated", got_tickets, db_tickets)
        check("all reachable entries migrated", got_entries, db_entries)
        check("all attachment rows migrated", got_atts, db_atts)

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
