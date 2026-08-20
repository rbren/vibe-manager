"""Ticket attachment tests — plain script, no pytest dependency.

Run with the service venv:
    VIBE_TEST=1 /root/git/vibe-manager/.venv/bin/python tests/test_attachments.py
Uses a temp DB + data dir via VIBE_DB_PATH / VIBE_DATA_DIR so the live
vibe.db and data/ are never touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="vibe-test-"))
os.environ["VIBE_DB_PATH"] = str(TMP / "vibe.db")
os.environ["VIBE_DATA_DIR"] = str(TMP / "data")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as vibe_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(vibe_app.app)


def seed_workspace_and_ticket() -> tuple[str, str]:
    ws_id = uuid.uuid4().hex[:12]
    now = time.time()
    with vibe_app.db() as conn:
        conn.execute(
            "INSERT INTO workspaces(id, path, name, created_at) VALUES(?,?,?,?)",
            (ws_id, f"/tmp/ws-{ws_id}", "testws", now),
        )
    r = client.post(f"/api/workspaces/{ws_id}/tickets", json={"body": "build the thing"})
    assert r.status_code == 200, r.text
    return ws_id, r.json()["id"]


def test_upload_and_download():
    ws_id, tid = seed_workspace_and_ticket()
    png = b"\x89PNG\r\n\x1a\nfakeimagebytes"
    r = client.post(
        f"/api/tickets/{tid}/attachments",
        params={"filename": "screen shot.png"},
        content=png,
        headers={"Content-Type": "image/png"},
    )
    assert r.status_code == 200, r.text
    att = r.json()
    assert att["filename"] == "screen shot.png"
    assert att["content_type"] == "image/png"
    assert att["size"] == len(png)
    assert att["url"] == f"/api/attachments/{att['id']}"
    assert Path(att["path"]).read_bytes() == png

    r = client.get(att["url"])
    assert r.status_code == 200
    assert r.content == png
    assert r.headers["content-type"].startswith("image/png")

    # attachment shows up on the ticket in board + manager snapshot
    for url in (f"/api/workspaces/{ws_id}/board", f"/api/manager/workspaces/{ws_id}/snapshot"):
        board = client.get(url).json()
        t = next(x for x in board["tickets"] if x["id"] == tid)
        assert len(t["attachments"]) == 1
        a = t["attachments"][0]
        assert a["id"] == att["id"] and a["path"] == att["path"] and a["url"] == att["url"]


def test_filename_sanitization():
    _, tid = seed_workspace_and_ticket()
    r = client.post(
        f"/api/tickets/{tid}/attachments",
        params={"filename": "../../etc/passwd"},
        content=b"data",
    )
    assert r.status_code == 200, r.text
    att = r.json()
    assert att["filename"] == "passwd"
    p = Path(att["path"]).resolve()
    assert str(p).startswith(str(Path(os.environ["VIBE_DATA_DIR"]).resolve()))

    r = client.post(f"/api/tickets/{tid}/attachments", params={"filename": "///"}, content=b"x")
    assert r.status_code == 200
    assert r.json()["filename"] == "file"


def test_errors():
    _, tid = seed_workspace_and_ticket()
    r = client.post(f"/api/tickets/{tid}/attachments", params={"filename": "a.txt"}, content=b"")
    assert r.status_code == 400
    r = client.post("/api/tickets/nope/attachments", params={"filename": "a.txt"}, content=b"x")
    assert r.status_code == 404
    r = client.get("/api/attachments/nonexistent")
    assert r.status_code == 404
    big = b"x" * (vibe_app.MAX_ATTACHMENT_BYTES + 1)
    r = client.post(f"/api/tickets/{tid}/attachments", params={"filename": "big.bin"}, content=big)
    assert r.status_code == 413


def test_multiple_attachments_ordering():
    ws_id, tid = seed_workspace_and_ticket()
    for name in ("one.txt", "two.txt"):
        r = client.post(f"/api/tickets/{tid}/attachments", params={"filename": name}, content=b"c")
        assert r.status_code == 200
        time.sleep(0.01)
    snap = client.get(f"/api/manager/workspaces/{ws_id}/snapshot").json()
    t = next(x for x in snap["tickets"] if x["id"] == tid)
    assert [a["filename"] for a in t["attachments"]] == ["one.txt", "two.txt"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
