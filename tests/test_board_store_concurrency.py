#!/usr/bin/env python3
"""The board survives concurrent writers.

board.json is written from three places: the browser (through the agent
server's file API), the automation's mechanical transitions, and the manager
agent's `vibectl patch`. Every one of them is a read-modify-write of the whole
document, so a write that lands between another writer's read and its write
used to vanish — including a ticket the user had just added, which is why
cards went missing from "pending".

Pure stdlib. Run with `python tests/test_board_store_concurrency.py`.
"""

from __future__ import annotations

import json
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
    """What the extension does: read the board, append a ticket, write it back.

    Deliberately NOT through vibestore — the browser writes through the file
    API and cannot take a local lock, so this is the writer the store has to
    tolerate.
    """
    path = vibestore.board_path(WS)
    board = json.loads(path.read_text())
    board["tickets"].append(ticket(tid))
    board["rev"] = (board.get("rev") or 0) + 1
    path.write_text(json.dumps(board, indent=2))


print("a ticket added between another writer's read and write survives")
vibestore.write_board(WS, {"tickets": [ticket("aaa")]})
fired = threading.Event()
original_read = vibestore.read_board


def read_then_browser_write(ws_id):
    board = original_read(ws_id)
    if not fired.is_set():  # only race the first read, like a real interleaving
        fired.set()
        browser_creates("bbb")
    return board


vibestore.read_board = read_then_browser_write
try:
    vibestore.patch_ticket(WS, "aaa", status="in_progress", manager_note="Worker dispatched")
finally:
    vibestore.read_board = original_read

ids = board_ids()
check("bbb" in ids, "the ticket created mid-patch is still on the board")
check("aaa" in ids, "the patched ticket is still on the board")
after = {t["id"]: t for t in vibestore.read_board(WS)["tickets"]}
check(after["aaa"]["status"] == "in_progress", "the patch was re-applied on the fresh board")
check(after["aaa"]["manager_note"] == "Worker dispatched", "all patched fields survived")


print("mutate_board hands the mutation a board that already has the racing write")
vibestore.write_board(WS, {"tickets": [ticket("ccc")]})
seen: list[set[str]] = []
raced = threading.Event()


def mutate(board):
    seen.append({t["id"] for t in board["tickets"]})
    if not raced.is_set():
        raced.set()
        browser_creates("ddd")
    for t in board["tickets"]:
        if t["id"] == "ccc":
            t["status"] = "finished"


vibestore.mutate_board(WS, mutate)
check(len(seen) > 1, "the mutation is retried when the board changed underneath it")
check("ddd" in seen[-1], "the retry sees the racing ticket")
ids = board_ids()
check({"ccc", "ddd"} <= ids, "both tickets are on the board")


print("concurrent patches from several processes/threads all land")
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


print("the board revision advances on every write, so writers can detect races")
rev_before = vibestore.read_board(WS).get("rev")
vibestore.patch_ticket(WS, "t000", manager_note="note")
rev_after = vibestore.read_board(WS).get("rev")
check(isinstance(rev_before, int) and rev_after == rev_before + 1, "rev is bumped once per write")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all board concurrency checks passed")
