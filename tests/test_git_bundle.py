from __future__ import annotations

import os
import subprocess
from pathlib import Path

from conftest import make_commit, run_git
from bundlehelper.git_service import GitService
from bundlehelper.logging_service import OperationLogger
from bundlehelper.sync_service import SyncService


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
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_full_bundle_contains_all_branches_and_tags(source_repo: Path, tmp_path: Path) -> None:
    git = GitService()
    logger = OperationLogger(tmp_path / "logs", "full", "bootstrap", "op1")
    local = tmp_path / "local"
    clone_repo(source_repo, local)
    git.create_local_branches(local)
    local_manifest = git.manifest(local)
    assert set(local_manifest.heads) == {"main", "dev"}

    bundle = tmp_path / "full.bundle"
    git.create_full_bundle(local, bundle, logger=logger)
    assert bundle.is_file()

    cloned = tmp_path / "server"
    clone_repo(bundle, cloned)
    git.create_local_branches(cloned)
    server_manifest = git.manifest(cloned)
    assert server_manifest.heads == local_manifest.heads
    assert server_manifest.tags == local_manifest.tags


def test_incremental_bundle_multi_branch_and_new_branch_applies(source_repo: Path, tmp_path: Path) -> None:
    git = GitService()
    logger = OperationLogger(tmp_path / "logs", "delta", "sync", "op2")
    local = tmp_path / "local"
    server = tmp_path / "server"
    clone_repo(source_repo, local)
    git.create_local_branches(local)
    baseline = git.manifest(local)
    clone_repo(local, server)
    git.create_local_branches(server)
    run_git(server, "remote", "remove", "origin")

    run_git(server, "checkout", "main")
    make_commit(server, "main.txt", "main update\n", "main update")
    run_git(server, "checkout", "dev")
    make_commit(server, "dev.txt", "dev update\n", "dev update")
    run_git(server, "checkout", "-b", "feature/x")
    make_commit(server, "feature.txt", "feature\n", "feature")
    run_git(server, "tag", "v2.0.0")
    target = git.manifest(server)

    service = SyncService(git)
    plan = service._build_delta_plan(baseline, target, RemoteAncestor(git, server), str(server))
    assert set(plan.changed_heads) == {"main", "dev"}
    assert set(plan.new_heads) == {"feature/x"}
    assert set(plan.new_tags) == {"v2.0.0"}

    bundle = tmp_path / "delta.bundle"
    git.create_incremental_bundle(server, bundle, old_manifest=baseline, new_manifest=target, logger=logger)
    git.verify_bundle(local, bundle, logger=logger)
    git.fetch_bundle(local, bundle, logger=logger)
    incoming_heads, incoming_tags = git.incoming_refs(local)
    service._apply_delta(local, baseline, target, plan, incoming_heads, incoming_tags, logger)
    git.cleanup_incoming_refs(local)

    applied = git.manifest(local)
    assert applied.heads == target.heads
    assert applied.tags == target.tags
    assert git._refs(local, "refs/bundlehelper/") == {}


def test_deleted_branch_is_blocked(source_repo: Path, tmp_path: Path) -> None:
    git = GitService()
    local = tmp_path / "local"
    clone_repo(source_repo, local)
    git.create_local_branches(local)
    baseline = git.manifest(local)
    run_git(local, "branch", "-D", "dev")
    target = git.manifest(local)
    service = SyncService(git)
    try:
        service._build_delta_plan(baseline, target, RemoteAncestor(git, local), str(local))
    except Exception as exc:
        assert "dev" in str(exc)
    else:
        raise AssertionError("deleted branch should be blocked")
def test_porcelain_parser_ignores_git_warnings() -> None:
    assert GitService.porcelain_changes("warning: unable to access ignore file: Permission denied") == []
    assert GitService.porcelain_changes("?? dirty.txt") == ["?? dirty.txt"]
    assert GitService.porcelain_changes(" M tracked.txt\nwarning: diagnostic") == [" M tracked.txt"]
