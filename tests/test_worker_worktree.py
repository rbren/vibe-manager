"""Tests for app._provision_worker_worktree — worker isolation worktrees.

Run: .venv/bin/python tests/test_worker_worktree.py (plain script, temp repos).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("VIBE_DB_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("VIBE_DATA_DIR", tempfile.mkdtemp())

import app  # noqa: E402


def sh(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def make_repo(path: Path) -> None:
    path.mkdir(parents=True)
    sh(path, "init", "-b", "master")
    sh(path, "config", "user.email", "t@t")
    sh(path, "config", "user.name", "t")
    (path / "a.txt").write_text("one\n")
    sh(path, "add", "-A")
    sh(path, "commit", "-m", "c1")


def cleanup(project: Path, conv_id: str, wt: dict) -> None:
    subprocess.run(["git", "-C", str(project), "worktree", "remove", "--force", wt["path"]],
                   capture_output=True)
    subprocess.run(["git", "-C", str(project), "branch", "-D", wt["branch"]],
                   capture_output=True)
    shutil.rmtree(Path(wt["path"]).parent, ignore_errors=True)


def test_worktree_from_local_head():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "proj"
        make_repo(repo)
        conv_id = f"test-{uuid.uuid4().hex[:8]}"
        wt = app._provision_worker_worktree(str(repo), conv_id)
        try:
            assert wt["path"] == f"/tmp/conversation-worktrees/{conv_id}/proj", wt
            assert wt["branch"] == f"openhands/{conv_id}"
            assert wt["start"] == "HEAD"  # no origin remote
            assert (Path(wt["path"]) / "a.txt").read_text() == "one\n"
            assert sh(wt["path"], "rev-parse", "--abbrev-ref", "HEAD") == wt["branch"]
        finally:
            cleanup(repo, conv_id, wt)
    print("ok: worktree created from local HEAD without origin")


def test_worktree_based_on_origin_default_branch():
    with tempfile.TemporaryDirectory() as td:
        upstream = Path(td) / "up"
        make_repo(upstream)
        clone = Path(td) / "clone"
        subprocess.run(["git", "clone", str(upstream), str(clone)],
                       capture_output=True, check=True)
        # Advance upstream past the clone's stale local master.
        (upstream / "b.txt").write_text("two\n")
        sh(upstream, "add", "-A")
        sh(upstream, "commit", "-m", "c2")
        conv_id = f"test-{uuid.uuid4().hex[:8]}"
        wt = app._provision_worker_worktree(str(clone), conv_id)
        try:
            assert wt["start"] == "origin/master", wt
            # fetch ran, so the worktree carries the NEW upstream commit.
            assert (Path(wt["path"]) / "b.txt").exists(), "worktree missing fetched commit"
        finally:
            cleanup(clone, conv_id, wt)
    print("ok: worktree based on fetched origin default branch")


def test_worktree_fresh_without_origin_head_ref():
    """origin/HEAD is absent in --single-branch clones; resolve it anyway."""
    with tempfile.TemporaryDirectory() as td:
        upstream = Path(td) / "up"
        make_repo(upstream)
        clone = Path(td) / "clone"
        subprocess.run(["git", "clone", "--single-branch", str(upstream), str(clone)],
                       capture_output=True, check=True)
        sh(clone, "update-ref", "-d", "refs/remotes/origin/HEAD")
        (upstream / "b.txt").write_text("two\n")
        sh(upstream, "add", "-A")
        sh(upstream, "commit", "-m", "c2")
        conv_id = f"test-{uuid.uuid4().hex[:8]}"
        wt = app._provision_worker_worktree(str(clone), conv_id)
        try:
            assert wt["start"] == "origin/master", wt
            assert (Path(wt["path"]) / "b.txt").exists(), "worktree missing fetched commit"
        finally:
            cleanup(clone, conv_id, wt)
    print("ok: origin default branch resolved when origin/HEAD is missing")


def test_provisioning_fast_forwards_project_checkout():
    with tempfile.TemporaryDirectory() as td:
        upstream = Path(td) / "up"
        make_repo(upstream)
        clone = Path(td) / "clone"
        subprocess.run(["git", "clone", str(upstream), str(clone)],
                       capture_output=True, check=True)
        (upstream / "b.txt").write_text("two\n")
        sh(upstream, "add", "-A")
        sh(upstream, "commit", "-m", "c2")
        conv_id = f"test-{uuid.uuid4().hex[:8]}"
        wt = app._provision_worker_worktree(str(clone), conv_id)
        try:
            assert (clone / "b.txt").exists(), "project checkout still behind origin"
            assert sh(clone, "rev-parse", "HEAD") == sh(upstream, "rev-parse", "HEAD")
        finally:
            cleanup(clone, conv_id, wt)
    print("ok: project checkout fast-forwarded to origin during provisioning")


def test_dirty_project_checkout_is_left_alone():
    with tempfile.TemporaryDirectory() as td:
        upstream = Path(td) / "up"
        make_repo(upstream)
        clone = Path(td) / "clone"
        subprocess.run(["git", "clone", str(upstream), str(clone)],
                       capture_output=True, check=True)
        stale_head = sh(clone, "rev-parse", "HEAD")
        (clone / "a.txt").write_text("local edit\n")
        (upstream / "b.txt").write_text("two\n")
        sh(upstream, "add", "-A")
        sh(upstream, "commit", "-m", "c2")
        conv_id = f"test-{uuid.uuid4().hex[:8]}"
        wt = app._provision_worker_worktree(str(clone), conv_id)
        try:
            assert sh(clone, "rev-parse", "HEAD") == stale_head, "dirty checkout was moved"
            assert (clone / "a.txt").read_text() == "local edit\n"
            # The worktree is still based on the freshest origin commit.
            assert (Path(wt["path"]) / "b.txt").exists()
        finally:
            cleanup(clone, conv_id, wt)
    print("ok: dirty project checkout untouched, worktree still fresh")


def test_unreachable_origin_fails_loudly():
    with tempfile.TemporaryDirectory() as td:
        upstream = Path(td) / "up"
        make_repo(upstream)
        clone = Path(td) / "clone"
        subprocess.run(["git", "clone", str(upstream), str(clone)],
                       capture_output=True, check=True)
        shutil.rmtree(upstream)
        conv_id = f"test-{uuid.uuid4().hex[:8]}"
        try:
            app._provision_worker_worktree(str(clone), conv_id)
        except app.HTTPException as exc:
            assert exc.status_code == 502, exc
            assert "fetch" in exc.detail, exc.detail
        else:
            raise AssertionError("expected provisioning to fail on unreachable origin")
        assert not (Path("/tmp/conversation-worktrees") / conv_id).exists()
    print("ok: unreachable origin raises instead of using a stale local base")


def test_refresh_workspace_checkout_never_raises():
    with tempfile.TemporaryDirectory() as td:
        upstream = Path(td) / "up"
        make_repo(upstream)
        clone = Path(td) / "clone"
        subprocess.run(["git", "clone", str(upstream), str(clone)],
                       capture_output=True, check=True)
        (upstream / "b.txt").write_text("two\n")
        sh(upstream, "add", "-A")
        sh(upstream, "commit", "-m", "c2")
        app._refresh_workspace_checkout(str(clone))
        assert (clone / "b.txt").exists(), "manager workspace not brought up to date"
        shutil.rmtree(upstream)
        app._refresh_workspace_checkout(str(clone))  # unreachable origin: no raise
    print("ok: manager workspace refresh updates the checkout and swallows failures")


def test_guidance_mentions_worktree():
    g = app._worktree_guidance("/root/git/proj", {
        "path": "/tmp/conversation-worktrees/x/proj",
        "branch": "openhands/x",
        "start": "origin/master",
    })
    assert "/tmp/conversation-worktrees/x/proj" in g
    assert "/root/git/proj" in g
    assert "openhands/x" in g
    print("ok: guidance includes worktree path, project path, branch")


if __name__ == "__main__":
    test_worktree_from_local_head()
    test_worktree_based_on_origin_default_branch()
    test_worktree_fresh_without_origin_head_ref()
    test_provisioning_fast_forwards_project_checkout()
    test_dirty_project_checkout_is_left_alone()
    test_unreachable_origin_fails_loudly()
    test_refresh_workspace_checkout_never_raises()
    test_guidance_mentions_worktree()
    print("all worker worktree tests passed")
