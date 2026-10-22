from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import make_commit, run_git
from bundlehelper.errors import PreflightError
from bundlehelper.git_service import GitService
from bundlehelper.logging_service import OperationLogger
from bundlehelper.models import RefManifest
from bundlehelper.process_runner import sha256_file
from bundlehelper.sync_service import DeltaPlan, SyncService


class RemoteAncestor:
    def __init__(self, git: GitService, repo: Path):
        self.git = git
        self.repo = repo

    def is_ancestor(self, _repo: str, ancestor: str, descendant: str) -> bool:
        return self.git.is_ancestor(self.repo, ancestor, descendant)


def clone_repo(source: Path, destination: Path) -> None:
    result = subprocess.run(
        ["git", "clone", "--no-single-branch", str(source), str(destination)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=None,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_tag_move_and_delete_are_blocked(source_repo: Path, tmp_path: Path) -> None:
    git = GitService()
    local = tmp_path / "local"
    clone_repo(source_repo, local)
    git.create_local_branches(local)
    baseline = git.manifest(local)
    service = SyncService(git)

    run_git(local, "tag", "-d", "v1.0.0")
    target = git.manifest(local)
    with pytest.raises(PreflightError, match="Tag"):
        service._build_delta_plan(baseline, target, RemoteAncestor(git, local), str(local))

    run_git(local, "tag", "-a", "v1.0.0", "-m", "restored")
    run_git(local, "tag", "-f", "v1.0.0", "HEAD")
    target = git.manifest(local)
    with pytest.raises(PreflightError, match="Tag"):
        service._build_delta_plan(baseline, target, RemoteAncestor(git, local), str(local))


def test_history_rewrite_is_blocked(source_repo: Path, tmp_path: Path) -> None:
    git = GitService()
    local = tmp_path / "local"
    clone_repo(source_repo, local)
    git.create_local_branches(local)
    baseline = git.manifest(local)
    old_oid = baseline.heads["main"]
    tree = run_git(local, "rev-parse", "HEAD^{tree}")
    new_oid = run_git(local, "commit-tree", tree, "-m", "rewritten")
    run_git(local, "update-ref", "refs/heads/main", new_oid, old_oid)
    target = git.manifest(local)
    service = SyncService(git)
    with pytest.raises(PreflightError, match="改写"):
        service._build_delta_plan(baseline, target, RemoteAncestor(git, local), str(local))


def test_non_fast_forward_incoming_branch_is_blocked(source_repo: Path, tmp_path: Path) -> None:
    git = GitService()
    local = tmp_path / "local"
    clone_repo(source_repo, local)
    git.create_local_branches(local)
    baseline = git.manifest(local)
    tree = run_git(local, "rev-parse", "HEAD^{tree}")
    unrelated = run_git(local, "commit-tree", tree, "-m", "unrelated")
    incoming_ref = "refs/bundlehelper/incoming/main"
    git.update_ref(local, incoming_ref, unrelated)
    plan = DeltaPlan(changed_heads={"main": unrelated})
    target = RefManifest(
        head_oid=unrelated,
        head_branch="main",
        heads={"main": unrelated},
        tags=baseline.tags,
    )
    service = SyncService(git)
    logger = OperationLogger(tmp_path / "logs", "non-ff", "delta", "nonff")
    with pytest.raises(PreflightError, match="\u5feb\u8fdb"):
        service._apply_delta(local, baseline, target, plan, {"main": unrelated}, {}, logger)


def test_dirty_workspace_and_extra_worktree_are_detected(source_repo: Path, tmp_path: Path) -> None:
    git = GitService()
    local = tmp_path / "local"
    clone_repo(source_repo, local)
    git.create_local_branches(local)
    (local / "untracked.txt").write_text("dirty", encoding="utf-8")
    clean, detail = git.is_clean(local)
    assert not clean
    assert "untracked.txt" in detail

    linked = tmp_path / "linked"
    run_git(local, "worktree", "add", "-b", "linked-worktree", str(linked), "dev")
    with pytest.raises(PreflightError):
        git.assert_single_worktree(local)


def test_full_bundle_with_unicode_and_space_paths(source_repo: Path, tmp_path: Path) -> None:
    git = GitService()
    logger = OperationLogger(tmp_path / "\u65e5\u5fd7 \u76ee\u5f55", "demo", "full", "unicode")
    local = tmp_path / "\u672c\u5730 \u4ed3\u5e93"
    clone_repo(source_repo, local)
    git.create_local_branches(local)
    manifest = git.manifest(local)
    bundle = tmp_path / "\u589e\u91cf \u6587\u4ef6" / "full.bundle"
    git.create_full_bundle(local, bundle, logger=logger)
    assert bundle.is_file()
    restored = tmp_path / "restored repo"
    clone_repo(bundle, restored)
    git.create_local_branches(restored)
    assert git.manifest(restored).heads == manifest.heads
    assert git.manifest(restored).tags == manifest.tags


def test_sha256_detects_bundle_content_change(tmp_path: Path) -> None:
    first = tmp_path / "first.bundle"
    second = tmp_path / "second.bundle"
    first.write_bytes(b"bundle-one")
    second.write_bytes(b"bundle-two")
    assert sha256_file(first) != sha256_file(second)
    assert sha256_file(first) == sha256_file(first)
