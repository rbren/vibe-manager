#!/usr/bin/env python3
"""Tests for the automation-side board store and git helpers.

Board logic is checked against a temp store; git helpers run against real
throwaway repositories (no mocks — the whole point is that they drive git
correctly). Conversation dispatch is not exercised here: it would create real
agent conversations.

Run: python3 tests/test_vibestore.py   (pure stdlib)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "automation"))

failures = []


def check(label, actual, expected):
    if actual == expected:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
        failures.append(label)


def check_true(label, value):
    check(label, bool(value), True)


def check_raises(label, fn, exc_type):
    try:
        fn()
    except exc_type:
        print(f"  ok   {label}")
        return
    except Exception as exc:  # noqa: BLE001 - reporting an unexpected type
        print(f"  FAIL {label}: raised {type(exc).__name__}, expected {exc_type.__name__}")
        failures.append(label)
        return
    print(f"  FAIL {label}: did not raise {exc_type.__name__}")
    failures.append(label)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


def make_origin(tmp: Path) -> Path:
    """A real bare origin with one commit on master."""
    work = tmp / "seed"
    work.mkdir()
    git(work, "init", "-q", "-b", "master")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hello\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "initial")

    origin = tmp / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(origin)], check=True)
    # The seed is used to advance origin later, so it needs a remote pointing
    # at it — a bare clone does not give the source repo one.
    git(work, "remote", "add", "origin", str(origin))
    return origin


def advance_origin(tmp: Path, filename: str) -> None:
    """Add a commit to origin's master via the seed checkout."""
    seed = tmp / "seed"
    (seed / filename).write_text("more\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", f"add {filename}")
    push = git(seed, "push", "-q", "origin", "master")
    assert push.returncode == 0, f"seed push failed: {push.stderr}"


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ["VIBE_STORE_DIR"] = str(tmp / "store")
        import vibestore  # imported after VIBE_STORE_DIR is set

        print("store root")
        check("honours VIBE_STORE_DIR", vibestore.store_root(), tmp / "store")

        print("\nindex + workspaces")
        check("empty index when absent", vibestore.read_index(), {"version": 1, "workspaces": []})
        check("missing workspace is None", vibestore.get_workspace("nope"), None)

        ws_id = "w1"
        vibestore.write_index({"workspaces": [
            {"id": ws_id, "path": "/tmp/proj", "name": "proj", "max_concurrent": 2,
             "push_mode": "main", "automation_id": None,
             "manager_conversation_id": None, "created_at": 1.0},
        ]})
        check("workspace round-trips", vibestore.get_workspace(ws_id)["name"], "proj")
        vibestore.update_workspace(ws_id, manager_conversation_id="conv-9")
        check("update_workspace persists",
              vibestore.get_workspace(ws_id)["manager_conversation_id"], "conv-9")
        check_raises("unknown workspace raises",
                     lambda: vibestore.update_workspace("nope", a=1), KeyError)

        print("\nboard + tickets")
        check("empty board when absent", vibestore.read_board(ws_id)["tickets"], [])
        board = vibestore.read_board(ws_id)
        board["tickets"] = [{
            "id": "t1", "status": "pending", "title": None, "sort_order": 1.0,
            "conversation_id": None, "pr_url": None, "manager_note": None,
            "dispatched_entry_count": 0, "created_at": 1.0, "updated_at": 1.0,
            "finished_at": None, "verified_at": None,
            "entries": [{"id": "e1", "author": "user", "body": "do it", "created_at": 1.0}],
            "attachments": [],
        }]
        vibestore.write_board(ws_id, board)
        check("board round-trips", len(vibestore.read_board(ws_id)["tickets"]), 1)

        snap = vibestore.snapshot(ws_id)
        check("snapshot has workspace", snap["workspace"]["id"], ws_id)
        check("snapshot has tickets", len(snap["tickets"]), 1)

        print("\npatch_ticket")
        t = vibestore.patch_ticket(ws_id, "t1", status="in_progress",
                                   conversation_id="c1", title="  🐛 Fix login  ")
        check("status set", t["status"], "in_progress")
        check("conversation_id set", t["conversation_id"], "c1")
        check("title trimmed", t["title"], "🐛 Fix login")
        check_true("updated_at bumped", t["updated_at"] > 1.0)
        check("finished_at not set yet", t["finished_at"], None)

        t = vibestore.patch_ticket(ws_id, "t1", status="finished")
        stamped = t["finished_at"]
        check_true("finished_at stamped on transition", stamped is not None)
        t = vibestore.patch_ticket(ws_id, "t1", manager_note="Landed abc123")
        check("idempotent re-patch keeps finished_at", t["finished_at"], stamped)
        check("manager_note set", t["manager_note"], "Landed abc123")

        t = vibestore.patch_ticket(ws_id, "t1", title="   ")
        check("blank title clears", t["title"], None)

        check_raises("bad status rejected",
                     lambda: vibestore.patch_ticket(ws_id, "t1", status="bogus"), ValueError)
        check_raises("unknown ticket rejected",
                     lambda: vibestore.patch_ticket(ws_id, "zz", status="pending"), KeyError)

        print("\ndispatched_entry_count absorbs manager notes")
        t = vibestore.patch_ticket(ws_id, "t1", dispatched_entry_count=1)
        check("count set", t["dispatched_entry_count"], 1)
        t = vibestore.patch_ticket(ws_id, "t1", append_entry="Worker dispatched")
        check("manager entry appended", len(t["entries"]), 2)
        check("author is manager", t["entries"][-1]["author"], "manager")
        check("trailing manager note absorbed", t["dispatched_entry_count"], 2)

        # A user entry after the dispatched point must NOT be absorbed.
        b = vibestore.read_board(ws_id)
        b["tickets"][0]["entries"].append(
            {"id": "e3", "author": "user", "body": "more", "created_at": 9.0})
        vibestore.write_board(ws_id, b)
        t = vibestore.patch_ticket(ws_id, "t1", append_entry="Another note")
        check("count stops at the user entry", t["dispatched_entry_count"], 2)
        check("entries appended in order", t["entries"][-1]["body"], "Another note")

        t = vibestore.patch_ticket(ws_id, "t1", append_entry="   ")
        check("blank append is ignored", len(t["entries"]), 4)

        print("\natomic writes")
        check("no .tmp files left", list((tmp / "store").rglob("*.tmp")), [])
        raw = json.loads((tmp / "store" / "workspaces" / ws_id / "board.json").read_text())
        check("board file has version", raw["version"], 1)
        check("board file has workspace_id", raw["workspace_id"], ws_id)

        print("\ncorrupt file surfaces loudly")
        bad = tmp / "store" / "workspaces" / "bad" / "board.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{not json")
        check_raises("corrupt board raises", lambda: vibestore.read_board("bad"), RuntimeError)

        print("\ngit helpers (real repositories)")
        origin = make_origin(tmp)
        project = tmp / "proj"
        subprocess.run(["git", "clone", "-q", str(origin), str(project)], check=True)
        git(project, "config", "user.email", "t@example.com")
        git(project, "config", "user.name", "t")

        ref = vibestore.origin_default_ref(project)
        check("origin default ref resolved", ref, "origin/master")

        no_remote = tmp / "noremote"
        no_remote.mkdir()
        git(no_remote, "init", "-q")
        check("no origin -> None", vibestore.origin_default_ref(no_remote), None)

        # A single-branch clone has no origin/HEAD until it is set.
        single = tmp / "single"
        subprocess.run(["git", "clone", "-q", "--single-branch", "--branch", "master",
                        str(origin), str(single)], check=True)
        git(single, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        check("origin/HEAD auto-set when missing",
              vibestore.origin_default_ref(single), "origin/master")

        # Advance origin, then prove the checkout fast-forwards.
        advance_origin(tmp, "next.txt")
        before = git(project, "rev-parse", "HEAD").stdout.strip()
        check("sync returns the default ref", vibestore.sync_project_checkout(project), "origin/master")
        after = git(project, "rev-parse", "HEAD").stdout.strip()
        check_true("clean checkout fast-forwarded", before != after)

        # A dirty checkout must be left alone.
        advance_origin(tmp, "third.txt")
        (project / "dirty.txt").write_text("local work\n")
        git(project, "add", "-A")
        dirty_head = git(project, "rev-parse", "HEAD").stdout.strip()
        vibestore.sync_project_checkout(project)
        check("dirty checkout untouched",
              git(project, "rev-parse", "HEAD").stdout.strip(), dirty_head)
        git(project, "reset", "-q", "--hard")

        # A non-default branch must be left alone.
        git(project, "checkout", "-q", "-b", "feature")
        feat_head = git(project, "rev-parse", "HEAD").stdout.strip()
        vibestore.sync_project_checkout(project)
        check("non-default branch untouched",
              git(project, "rev-parse", "HEAD").stdout.strip(), feat_head)
        git(project, "checkout", "-q", "master")

        # A broken remote must raise rather than silently use a stale base.
        broken = tmp / "broken"
        subprocess.run(["git", "clone", "-q", str(origin), str(broken)], check=True)
        git(broken, "remote", "set-url", "origin", str(tmp / "does-not-exist.git"))
        check_raises("failed fetch raises",
                     lambda: vibestore.sync_project_checkout(broken), RuntimeError)

        print("\nworktree provisioning")
        vibestore.WORKTREE_ROOT = tmp / "worktrees"
        wt = vibestore.provision_worker_worktree(str(project), "conv-abc")
        check("branch name", wt["branch"], "openhands/conv-abc")
        check("start ref", wt["start"], "origin/master")
        check_true("worktree exists", Path(wt["path"]).is_dir())
        check("worktree path layout",
              Path(wt["path"]).parent.name, "conv-abc")
        head = git(Path(wt["path"]), "rev-parse", "HEAD").stdout.strip()
        origin_head = git(project, "rev-parse", "origin/master").stdout.strip()
        check("worktree based on origin default", head, origin_head)

        guidance = vibestore.worktree_guidance(str(project), wt)
        check_true("guidance names the worktree", wt["path"] in guidance)
        check_true("guidance names the branch", wt["branch"] in guidance)

        check_raises("duplicate worktree raises",
                     lambda: vibestore.provision_worker_worktree(str(project), "conv-abc"),
                     RuntimeError)

        print("\nCLI install + invocation in a bare environment")
        cli = vibestore.install_cli(ws_id, str(project))
        check("cli installed under the store", Path(cli).parent, tmp / "store" / "bin")
        for name in ("vibestore.py", "vibectl.py", "config.json"):
            check_true(f"{name} installed", (Path(cli).parent / name).exists())
        check_true("cli is executable", os.access(cli, os.X_OK))
        installed_cfg = json.loads((Path(cli).parent / "config.json").read_text())
        check("config records the workspace", installed_cfg["workspace_id"], ws_id)
        check("config records the store", installed_cfg["store_dir"], str(tmp / "store"))

        # The manager's shell inherits none of the automation's environment,
        # so the CLI must work with an empty env.
        run = subprocess.run([sys.executable, cli, "snapshot"],
                             capture_output=True, text=True, env={})
        check("snapshot exits 0 with no env", run.returncode, 0)
        if run.returncode == 0:
            payload = json.loads(run.stdout)
            check("snapshot finds the workspace", payload["workspace"]["id"], ws_id)
            check("snapshot finds the ticket", payload["tickets"][0]["id"], "t1")
        else:
            print(f"       stderr: {run.stderr.strip()[:200]}")

        run = subprocess.run(
            [sys.executable, cli, "patch", "t1", "--manager-note", "via cli"],
            capture_output=True, text=True, env={})
        check("patch exits 0", run.returncode, 0)
        check("patch persisted",
              vibestore.read_board(ws_id)["tickets"][0]["manager_note"], "via cli")

        run = subprocess.run([sys.executable, cli, "patch", "nosuch", "--status", "pending"],
                             capture_output=True, text=True, env={})
        check("unknown ticket exits non-zero", run.returncode, 1)
        check_true("error is JSON on stderr", "error" in json.loads(run.stderr))

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
