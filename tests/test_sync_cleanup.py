from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import make_commit, run_git
from bundlehelper.errors import CommandError, PreflightError
from bundlehelper.git_service import GitService
from bundlehelper.logging_service import OperationLogger
from bundlehelper.models import AppSettings, ProjectConfig
from bundlehelper.sync_service import SyncService


def make_project(tmp_path: Path) -> tuple[GitService, ProjectConfig, AppSettings]:
    git = GitService()
    repo = tmp_path / "local repo"
    repo.mkdir()
    run_git(repo, "init", "-b", "main")
    make_commit(repo, "README.md", "initial\n", "initial")
    run_git(repo, "remote", "add", "origin", "https://github.com/acme/demo.git")
    project = ProjectConfig(
        id="project123",
        name="demo",
        github_url="https://github.com/acme/demo",
        local_repo_path=str(repo),
        server_repo_path="/srv/demo",
        bundle_dir_override=str(tmp_path / "bundles"),
        log_dir_override=str(tmp_path / "logs"),
    )
    project.ssh.host = "example.com"
    project.ssh.user = "dev"
    settings = AppSettings(
        bundle_dir=str(tmp_path / "global bundles"),
        remote_temp_dir="/tmp/bh",
        log_dir=str(tmp_path / "global logs"),
    )
    return git, project, settings


def test_push_success_removes_only_operation_directory(tmp_path: Path, monkeypatch) -> None:
    git, project, settings = make_project(tmp_path)
    service = SyncService(git)
    logger = OperationLogger(tmp_path / "logs", project.name, "push", "success")
    monkeypatch.setattr(git, "push_all", lambda *args, **kwargs: None)
    service.push_github(project, settings, token="secret", logger=logger)
    project_root = Path(project.resolved_bundle_dir(settings)) / project.id
    assert project_root.is_dir()
    assert list(project_root.iterdir()) == []


def test_push_failure_retains_operation_directory(tmp_path: Path, monkeypatch) -> None:
    git, project, settings = make_project(tmp_path)
    service = SyncService(git)
    logger = OperationLogger(tmp_path / "logs", project.name, "push", "failure")

    def fail(*args, **kwargs):
        raise RuntimeError("simulated push failure")

    monkeypatch.setattr(git, "push_all", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        service.push_github(project, settings, token="secret", logger=logger)

    project_root = Path(project.resolved_bundle_dir(settings)) / project.id
    children = [path for path in project_root.iterdir() if path.is_dir()]
    assert len(children) == 1
    assert re.fullmatch(r"[0-9a-f]{12}", children[0].name)


def test_dirty_local_workspace_is_rejected_before_ssh(tmp_path: Path, monkeypatch) -> None:
    git, project, settings = make_project(tmp_path)
    project.baseline = git.manifest(Path(project.local_repo_path))
    project.setup_complete = True
    (Path(project.local_repo_path) / "dirty.txt").write_text("dirty", encoding="utf-8")
    service = SyncService(git)
    logger = OperationLogger(tmp_path / "logs", project.name, "delta", "dirty")
    ssh_called = False

    def forbidden_ssh(*args, **kwargs):
        nonlocal ssh_called
        ssh_called = True
        raise AssertionError("SSH should not be used before local validation")

    monkeypatch.setattr(service, "_ssh", forbidden_ssh)
    with pytest.raises(PreflightError):
        service.sync_server_delta(project, settings, logger=logger)
    assert ssh_called is False


def test_unwritable_local_bundle_path_is_rejected_before_ssh(tmp_path: Path, monkeypatch) -> None:
    git, project, settings = make_project(tmp_path)
    invalid = tmp_path / "bundle-path-is-a-file"
    invalid.write_text("not a directory", encoding="utf-8")
    project.bundle_dir_override = str(invalid)
    service = SyncService(git)
    logger = OperationLogger(tmp_path / "logs", project.name, "full", "badpath")
    ssh_called = False

    def forbidden_ssh(*args, **kwargs):
        nonlocal ssh_called
        ssh_called = True
        raise AssertionError("SSH should not be used before local directory validation")

    monkeypatch.setattr(service, "_ssh", forbidden_ssh)
    with pytest.raises(CommandError):
        service.bootstrap_full(project, settings, token=None, logger=logger)
    assert ssh_called is False
