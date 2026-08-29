#!/usr/bin/env python3
"""Package the automation modules and push them to the automation backend.

Talks only to the automation API and the native JSON store, so it works
without the legacy FastAPI service running. Stdlib only.

  python3 scripts/push_automation.py            # every workspace with an automation
  python3 scripts/push_automation.py dj-station # one workspace, by name or id
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ("main.py", "vibestore.py", "vibectl.py")
AUTOMATION_API = os.environ.get("VIBE_AUTOMATION_API", "http://127.0.0.1:18001/api/automation")
AGENT_SERVER = os.environ.get("VIBE_AGENT_SERVER", "http://127.0.0.1:18000")
CANVAS_BASE = os.environ.get("VIBE_CANVAS_BASE", "http://127.0.0.1:8000")
STORE = Path(os.environ.get("VIBE_STORE_ROOT", Path.home() / ".openhands/vibe-manager"))


def api_key() -> str:
    key = os.environ.get("OPENHANDS_AUTOMATION_API_KEY")
    if key:
        return key
    return (ROOT / ".automation-key").read_text().strip()


def request(method: str, path: str, *, body=None, content=None, ctype=None):
    url = f"{AUTOMATION_API}{path}"
    data = content if content is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"X-Session-API-Key": api_key()}
    if ctype:
        headers["Content-Type"] = ctype
    elif body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            raw = res.read().decode()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> {exc.code} {exc.read().decode()[:300]}") from exc
    return json.loads(raw) if raw else None


def build_tarball(ws: dict) -> bytes:
    """Mirror of app.build_manager_tarball, kept stdlib-only."""
    config = json.dumps(
        {
            "workspace_id": ws["id"],
            "workspace_path": ws["path"],
            "workspace_name": ws["name"],
            "agent_server": AGENT_SERVER,
            "canvas_base": CANVAS_BASE,
        },
        indent=2,
    ).encode()
    members = [(n, (ROOT / "automation" / n).read_bytes()) for n in MODULES]
    members.append(("config.json", config))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def main(argv: list[str]) -> int:
    index = json.loads((STORE / "index.json").read_text())
    wanted = set(argv)
    targets = [
        w for w in index["workspaces"]
        if w.get("automation_id") and (not wanted or w["name"] in wanted or w["id"] in wanted)
    ]
    if not targets:
        print("no matching workspace with an automation", file=sys.stderr)
        return 1

    for ws in targets:
        tarball = build_tarball(ws)
        up = request(
            "POST",
            f"/v1/uploads?name=vibe-manager-{ws['id']}",
            content=tarball,
            ctype="application/gzip",
        )
        request("PATCH", f"/v1/{ws['automation_id']}", body={
            "tarball_path": up["tarball_path"], "enabled": True,
        })
        print(f"pushed {len(MODULES) + 1} files ({len(tarball)} B) -> {ws['name']} ({ws['automation_id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
