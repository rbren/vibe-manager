#!/usr/bin/env python3
"""The board survives concurrent writers.

Tickets are written from three places: the browser (through the agent server's
file API), the automation's mechanical transitions, and the manager agent's
`vibectl patch`. While the whole board was one document, a write landing
between another writer's read and its write vanished — including a ticket the
user had just added, which is why cards went missing from "pending".

Each ticket now has its own file, so writers touching different tickets share
no document and cannot lose each other's work. Writers touching the SAME
ticket still share one, and are covered by the rev/writer check.

Pure stdlib. Run with `python tests/test_board_store_concurrency.py`.
"""

from __future__ import annotations

import json
import shutil
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation"))

TMP = tempfile.mkdtemp(prefix="vibe-store-")
os.environ["VIBE_STORE_DIR"] = TMP

import vibestore  # noqa: E402 - VIBE_STORE_DIR must be set first

WS = "wsconc123456"
failures: list[str] = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL: {msg}")


def ticket(tid: str, status: str = "pending") -> dict:
    return {
        "id": tid, "status": status, "title": None, "sort_order": 1,
        "conversation_id": None, "pr_url": None, "manager_note": None,
        "dispatched_entry_count": 0, "created_at": 1.0, "updated_at": 1.0,
        "finished_at": None, "verified_at": None,
        "entries": [{"id": "e" + tid, "author": "user", "body": tid, "created_at": 1.0}],
        "attachments": [],
    }


def board_ids() -> set[str]:
    return {t["id"] for t in vibestore.read_board(WS)["tickets"]}


def browser_creates(tid: str) -> None:
    """What the extension does: write a new ticket file.

    Deliberately NOT through vibestore's locking — the browser writes through
    the file API and cannot take a local lock, so this is the writer the store
    has to tolerate.
    """
    path = vibestore.ticket_path(WS, tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ticket(tid), indent=2))


print("a ticket added between another writer's read and write survives")
vibestore.write_board(WS, {"tickets": [ticket("aaa")]})
fired = threading.Event()
original_read = vibestore.read_ticket


def read_then_browser_write(ws_id, ticket_id):
    found = original_read(ws_id, ticket_id)
    if not fired.is_set():  # only race the first read, like a real interleaving
        fired.set()
        browser_creates("bbb")
    return found


vibestore.read_ticket = read_then_browser_write
try:
    vibestore.patch_ticket(WS, "aaa", status="in_progress", manager_note="Worker dispatched")
finally:
    vibestore.read_ticket = original_read

ids = board_ids()
check("bbb" in ids, "the ticket created mid-patch is still on the board")
check("aaa" in ids, "the patched ticket is still on the board")
after = {t["id"]: t for t in vibestore.read_board(WS)["tickets"]}
check(after["aaa"]["status"] == "in_progress", "the patch landed")
check(after["aaa"]["manager_note"] == "Worker dispatched", "all patched fields survived")


print("a write to one ticket cannot disturb another")
vibestore.write_board(WS, {"tickets": [ticket("ccc")]})
raced = threading.Event()
original_mutate_read = vibestore.read_ticket


def mutate_read(ws_id, ticket_id):
    found = original_mutate_read(ws_id, ticket_id)
    if not raced.is_set():
        raced.set()
        browser_creates("ddd")  # a different ticket, mid-cycle
    return found


vibestore.read_ticket = mutate_read
try:
    vibestore.patch_ticket(WS, "ccc", status="finished")
finally:
    vibestore.read_ticket = original_mutate_read

ids = board_ids()
check({"ccc", "ddd"} <= ids, "both tickets are on the board")
final = {t["id"]: t for t in vibestore.read_board(WS)["tickets"]}
check(final["ccc"]["status"] == "finished", "the patched ticket kept its change")


print("concurrent patches from several processes/threads all land")
# Each ticket is its own file, so seeding no longer replaces the board:
# start from a clean workspace to count exactly these tickets.
shutil.rmtree(vibestore.tickets_dir(WS), ignore_errors=True)
tickets = [ticket(f"t{i:03d}") for i in range(12)]
vibestore.write_board(WS, {"tickets": tickets})
errors: list[Exception] = []


def patch(tid):
    try:
        time.sleep(0.001)
        vibestore.patch_ticket(WS, tid, status="in_progress", conversation_id=f"c-{tid}")
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


threads = [threading.Thread(target=patch, args=(t["id"],)) for t in tickets]
for th in threads:
    th.start()
for th in threads:
    th.join()
check(not errors, f"no writer failed ({errors[:1]})")
final = {t["id"]: t for t in vibestore.read_board(WS)["tickets"]}
check(len(final) == len(tickets), "no ticket was dropped by concurrent writers")
lost = [tid for tid in final if final[tid]["status"] != "in_progress"]
check(not lost, f"every concurrent patch landed (lost: {lost})")


print("a ticket's revision advances on every write, so writers can detect races")
rev_before = vibestore.read_ticket(WS, "t000").get("rev")
vibestore.patch_ticket(WS, "t000", manager_note="note")
rev_after = vibestore.read_ticket(WS, "t000").get("rev")
check(isinstance(rev_before, int) and rev_after == rev_before + 1, "rev is bumped once per write")

print("patching one ticket leaves its neighbours' revisions untouched")
neighbour_before = vibestore.read_ticket(WS, "t001").get("rev")
vibestore.patch_ticket(WS, "t000", manager_note="another")
check(vibestore.read_ticket(WS, "t001").get("rev") == neighbour_before,
      "an unrelated ticket is not rewritten")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all board concurrency checks passed")
