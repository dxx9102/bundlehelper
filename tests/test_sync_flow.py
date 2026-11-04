from __future__ import annotations

import posixpath
import shutil
import subprocess
from pathlib import Path

from conftest import make_commit, run_git
from bundlehelper.git_service import GitService
from bundlehelper.logging_service import OperationLogger
from bundlehelper.models import AppSettings, ProjectConfig
from bundlehelper.process_runner import safe_remove_dir, sha256_file
from bundlehelper.sync_service import SyncService


class FakeSsh:
    def __init__(self, root: Path) -> None:
        self.root = root

    def local(self, remote: str) -> Path:
        assert remote.startswith("/")
        return self.root / remote.lstrip("/")

    def ensure_remote_directory(self, path: str, **_kwargs) -> None:
        self.local(path).mkdir(parents=True, exist_ok=True)

    def remote_path_exists(self, path: str) -> bool:
        return self.local(path).exists()

    def make_remote_operation_dir(self, base: str, project_id: str, operation_id: str) -> str:
        path = posixpath.join(base, f"bundlehelper-{project_id}-{operation_id}")
        self.ensure_remote_directory(path)
        return path

    def upload(self, local_path, remote_path: str, **_kwargs) -> None:
        target = self.local(remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, target)

    def download(self, remote_path: str, local_path, **_kwargs) -> None:
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.local(remote_path), target)

    def sha256_remote(self, remote_path: str) -> str:
        return sha256_file(self.local(remote_path))

    def remove_remote_operation_dir(self, base: str, path: str, expected_name: str) -> None:
        safe_remove_dir(self.local(base), self.local(path), expected_name)


class FakeRemoteGit:
    def __init__(self, ssh: FakeSsh, git: GitService) -> None:
        self.ssh = ssh
        self.git = git

    def validate_repository(self, repo: str) -> None:
        local = self.ssh.local(repo)
        if not (local / ".git").is_dir():
            raise RuntimeError(f"not a repository: {repo}")

    def assert_clean(self, repo: str) -> None:
        clean, detail = self.git.is_clean(self.ssh.local(repo))
        if not clean:
            raise RuntimeError(detail)

    def assert_single_worktree(self, repo: str) -> None:
        self.git.assert_single_worktree(self.ssh.local(repo))

    def assert_no_unsupported_features(self, repo: str) -> None:
        self.git.assert_no_unsupported_features(self.ssh.local(repo))

    def manifest(self, repo: str):
        return self.git.manifest(self.ssh.local(repo))

    def is_bare(self, repo: str) -> bool:
        return self.git.is_bare(self.ssh.local(repo))

    def commit_exists(self, repo: str, oid: str) -> bool:
        return self.git.commit_exists(self.ssh.local(repo), oid)

    def is_ancestor(self, repo: str, ancestor: str, descendant: str) -> bool:
        return self.git.is_ancestor(self.ssh.local(repo), ancestor, descendant)

    def clone_from_bundle(self, bundle: str, destination: str, logger: OperationLogger) -> None:
        target = self.ssh.local(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--no-single-branch", str(self.ssh.local(bundle)), str(target)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        self.git.create_local_branches(target, logger=logger, remote="origin")
        run_git(target, "remote", "remove", "origin")

    def sha256(self, remote_path: str) -> str:
        return self.ssh.sha256_remote(remote_path)

    def create_incremental_bundle(
        self,
        repo: str,
        bundle: str,
        revisions,
        logger: OperationLogger,
    ) -> None:
        target = self.ssh.local(bundle)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.git._run(
            ["bundle", "create", str(target), *revisions],
            repo=self.ssh.local(repo),
            logger=logger,
        )

    def remove_operation_dir(self, base: str, path: str, expected_name: str) -> None:
        safe_remove_dir(self.ssh.local(base), self.ssh.local(path), expected_name)


def test_full_bootstrap_then_multi_branch_delta_end_to_end(tmp_path: Path, monkeypatch, source_repo: Path) -> None:
    git = GitService()
    local = tmp_path / "local repository"
    subprocess.run(
        ["git", "clone", "--no-single-branch", str(source_repo), str(local)],
        check=True,
        capture_output=True,
    )
    git.create_local_branches(local)
    run_git(local, "remote", "set-url", "origin", "https://github.com/acme/demo.git")

    remote_root = tmp_path / "remote fs"
    fake_ssh = FakeSsh(remote_root)
    fake_remote = FakeRemoteGit(fake_ssh, git)
    service = SyncService(git)
    monkeypatch.setattr(service, "_ssh", lambda *args, **kwargs: (fake_ssh, fake_remote))

    project = ProjectConfig(
        id="flowproject",
        name="flow",
        github_url="https://github.com/acme/demo",
        local_repo_path=str(local),
        server_repo_path="/remote/server/repo",
        bundle_dir_override=str(tmp_path / "bundles"),
        remote_temp_dir_override="/remote/temp",
        log_dir_override=str(tmp_path / "logs"),
    )
    project.ssh.host = "example.com"
    project.ssh.user = "dev"
    settings = AppSettings(
        bundle_dir=str(tmp_path / "global bundles"),
        remote_temp_dir="/tmp/bh",
        log_dir=str(tmp_path / "global logs"),
    )
    logger = OperationLogger(tmp_path / "logs", project.name, "full", "flow")
    server_baseline = service.bootstrap_full(project, settings, token=None, logger=logger)
    assert server_baseline.heads == git.manifest(local).heads
    assert (remote_root / "remote/server/repo/.git").is_dir()
    project_root = tmp_path / "bundles" / project.id
    assert list(project_root.iterdir()) == []
    assert list((remote_root / "remote/temp").iterdir()) == []

    server_local = fake_ssh.local(project.server_repo_path)
    run_git(server_local, "checkout", "main")
    make_commit(server_local, "main.txt", "main\n", "main update")
    run_git(server_local, "checkout", "dev")
    make_commit(server_local, "dev.txt", "dev\n", "dev update")
    run_git(server_local, "checkout", "-b", "feature/flow")
    make_commit(server_local, "feature.txt", "feature\n", "feature update")
    run_git(server_local, "tag", "v2.0.0")

    project.baseline = server_baseline
    project.setup_complete = True
    delta_logger = OperationLogger(tmp_path / "logs", project.name, "delta", "flow")
    target = service.sync_server_delta(project, settings, logger=delta_logger)
    local_manifest = git.manifest(local)
    assert local_manifest.heads == target.heads
    assert local_manifest.tags == target.tags
    assert "feature/flow" in target.heads
    assert "v2.0.0" in target.tags
    assert list(project_root.iterdir()) == []
    assert list((remote_root / "remote/temp").iterdir()) == []
