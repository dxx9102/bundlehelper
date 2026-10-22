from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import make_commit, run_git
from bundlehelper.config import ConfigStore
from bundlehelper.git_service import GitService
from bundlehelper.logging_service import OperationLogger
from bundlehelper.models import AppSettings, AppState, ProjectConfig
from bundlehelper.process_runner import CommandResult
from bundlehelper.sync_service import SyncService


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv, **_kwargs):
        values = [str(value) for value in argv]
        self.calls.append(values)
        return CommandResult(values, 0, "")


def test_project_directory_overrides_and_local_context(tmp_path: Path) -> None:
    global_bundle = tmp_path / "global"
    project_bundle = tmp_path / "\u9879\u76ee bundle \u76ee\u5f55"
    settings = AppSettings(
        bundle_dir=str(global_bundle),
        remote_temp_dir="/var/tmp/bh",
        log_dir=str(tmp_path / "global-logs"),
    )
    project = ProjectConfig(
        id="project-id",
        name="demo",
        local_repo_path=str(tmp_path / "local repo"),
        bundle_dir_override=str(project_bundle),
        remote_temp_dir_override="/srv/tmp/bh",
        log_dir_override=str(tmp_path / "project logs"),
    )
    assert project.resolved_bundle_dir(settings) == str(project_bundle)
    assert project.resolved_remote_temp_dir(settings) == "/srv/tmp/bh"
    assert project.resolved_log_dir(settings) == str(tmp_path / "project logs")

    service = SyncService(GitService())
    repo, base, project_root = service._local_context(project, settings)
    assert repo == (tmp_path / "local repo").resolve()
    assert base == project_bundle.resolve()
    assert project_root == (project_bundle / "project-id").resolve()
    assert base.is_dir()
    assert project_root.is_dir()


def test_config_store_is_versioned_atomic_and_token_free(tmp_path: Path) -> None:
    path = tmp_path / "\u914d\u7f6e\u7236\u76ee\u5f55" / "projects.json"
    store = ConfigStore(path)
    state = AppState()
    state.settings = AppSettings(
        bundle_dir=str(tmp_path / "temp"),
        remote_temp_dir="/tmp/bh",
        log_dir=str(tmp_path / "logs"),
        log_retention_days=17,
    )
    state.projects.append(
        ProjectConfig(
            name="demo",
            github_url="https://github.com/acme/demo",
            local_repo_path=str(tmp_path / "local"),
            server_repo_path="/srv/demo",
            bundle_dir_override=str(tmp_path / "override"),
            remote_temp_dir_override="/srv/tmp",
            log_dir_override=str(tmp_path / "oplogs"),
        )
    )
    store.save(state)
    loaded = store.load()
    assert loaded.schema_version == 1
    assert loaded.projects[0].bundle_dir_override == str(tmp_path / "override")
    assert loaded.settings.log_retention_days == 17
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert not path.with_name(path.name + ".tmp").exists()
    assert "secret-token" not in path.read_text(encoding="utf-8")


def test_push_all_pushes_every_branch_and_tag_to_bare_repository(source_repo: Path, tmp_path: Path) -> None:
    git = GitService()
    bare = tmp_path / "backup bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    run_git(source_repo, "checkout", "-b", "feature/push-all")
    make_commit(source_repo, "feature.txt", "feature\n", "feature")
    run_git(source_repo, "tag", "v2.0.0")
    logger = OperationLogger(tmp_path / "logs", "push", "push", "op")
    git.push_all(source_repo, str(bare), token=None, temp_dir=tmp_path / "temp", logger=logger)
    refs = subprocess.run(
        ["git", "--git-dir", str(bare), "for-each-ref", "--format=%(refname)"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert "refs/heads/main" in refs
    assert "refs/heads/dev" in refs
    assert "refs/heads/feature/push-all" in refs
    assert "refs/tags/v1.0.0" in refs
    assert "refs/tags/v2.0.0" in refs


def test_push_uses_no_force(tmp_path: Path) -> None:
    runner = RecordingRunner()
    git = GitService(runner)  # type: ignore[arg-type]
    logger = OperationLogger(tmp_path / "logs", "push", "push", "op")
    git.push_all(tmp_path, "https://github.com/acme/demo.git", token=None, temp_dir=tmp_path, logger=logger)
    assert len(runner.calls) == 2
    for argv in runner.calls:
        assert "--all" in argv or "--tags" in argv
        assert not any(value == "--force" or value.startswith("--force-with-lease") for value in argv)
