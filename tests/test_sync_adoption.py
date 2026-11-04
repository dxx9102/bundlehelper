from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import make_commit, run_git
from bundlehelper.errors import CommandError, PreflightError
from bundlehelper.git_service import GitService
from bundlehelper.logging_service import OperationLogger
from bundlehelper.models import AppSettings, ProjectConfig
from bundlehelper.sync_service import SyncService
from test_sync_flow import FakeRemoteGit, FakeSsh


@dataclass
class AdoptionEnv:
    git: GitService
    service: SyncService
    local: Path
    server: Path
    remote_root: Path
    project: ProjectConfig
    settings: AppSettings
    logger: OperationLogger

    @property
    def remote_temp(self) -> Path:
        return self.remote_root / "remote/temp"


@pytest.fixture
def admission_env(tmp_path: Path, source_repo: Path, monkeypatch) -> AdoptionEnv:
    git = GitService()
    local = tmp_path / "local repository"
    remote_root = tmp_path / "remote fs"
    server = remote_root / "remote/server/repo"
    server.parent.mkdir(parents=True)
    run_git(tmp_path, "clone", "--no-single-branch", str(source_repo), str(local))
    run_git(tmp_path, "clone", "--no-single-branch", str(source_repo), str(server))
    git.create_local_branches(local)
    git.create_local_branches(server)
    run_git(local, "remote", "set-url", "origin", "https://github.com/acme/demo.git")

    fake_ssh = FakeSsh(remote_root)
    fake_remote = FakeRemoteGit(fake_ssh, git)
    service = SyncService(git)

    def fake_ssh_factory(config, settings, _logger, *, ensure_temp_dir=True):
        if ensure_temp_dir:
            fake_ssh.ensure_remote_directory(config.resolved_remote_temp_dir(settings))
        return fake_ssh, fake_remote

    monkeypatch.setattr(service, "_ssh", fake_ssh_factory)
    project = ProjectConfig(
        id="adoption-project",
        name="adoption",
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
    logger = OperationLogger(tmp_path / "logs", project.name, "adopt", "flow")
    return AdoptionEnv(
        git=git,
        service=service,
        local=local,
        server=server,
        remote_root=remote_root,
        project=project,
        settings=settings,
        logger=logger,
    )


def test_identical_refs_only_establish_baseline_without_temp_dirs(admission_env: AdoptionEnv) -> None:
    env = admission_env
    before = env.git.manifest(env.local)
    report = env.service.inspect_existing_repos(
        env.project,
        env.settings,
        logger=env.logger,
    )

    assert report.can_adopt
    assert not report.has_ref_changes
    assert report.local_manifest.heads == before.heads
    assert not env.remote_temp.exists()

    result = env.service.adopt_existing_repos(
        env.project,
        env.settings,
        expected=report,
        logger=env.logger,
    )

    assert result.heads == report.server_manifest.heads
    assert env.git.manifest(env.local).heads == report.server_manifest.heads
    assert not env.remote_temp.exists()
    assert not (Path(env.project.resolved_bundle_dir(env.settings)) / env.project.id).exists()


def test_server_fast_forward_new_refs_and_followup_delta(admission_env: AdoptionEnv) -> None:
    env = admission_env
    run_git(env.server, "checkout", "main")
    make_commit(env.server, "server-main.txt", "main update\n", "server main")
    run_git(env.server, "checkout", "dev")
    make_commit(env.server, "server-dev.txt", "dev update\n", "server dev")
    run_git(env.server, "checkout", "-b", "feature/server")
    make_commit(env.server, "server-feature.txt", "feature\n", "server feature")
    run_git(env.server, "tag", "v2.0.0")
    server_manifest = env.git.manifest(env.server)
    local_before = env.git.manifest(env.local)
    project_temp = Path(env.project.resolved_bundle_dir(env.settings)) / env.project.id

    report = env.service.inspect_existing_repos(
        env.project,
        env.settings,
        logger=env.logger,
    )
    assert report.can_adopt
    assert set(report.fast_forward_heads) == {"main", "dev"}
    assert set(report.new_heads) == {"feature/server"}
    assert set(report.new_tags) == {"v2.0.0"}
    preview_local = env.git.manifest(env.local)
    assert preview_local.heads == local_before.heads
    assert preview_local.tags == local_before.tags
    assert not env.remote_temp.exists()
    assert not project_temp.exists()

    baseline = env.service.adopt_existing_repos(
        env.project,
        env.settings,
        expected=report,
        logger=env.logger,
    )
    local_manifest = env.git.manifest(env.local)
    assert local_manifest.heads == server_manifest.heads
    assert local_manifest.tags == server_manifest.tags
    assert local_manifest.head_branch == "main"
    assert (env.local / "server-main.txt").read_text(encoding="utf-8") == "main update\n"
    assert list(env.remote_temp.iterdir()) == []
    project_root = Path(env.project.resolved_bundle_dir(env.settings)) / env.project.id
    assert list(project_root.iterdir()) == []

    env.project.baseline = baseline
    env.project.setup_complete = True
    run_git(env.server, "checkout", "dev")
    make_commit(env.server, "server-dev-2.txt", "dev update 2\n", "server dev 2")
    delta_logger = OperationLogger(Path(env.project.log_dir_override), "adoption", "delta", "followup")
    target = env.service.sync_server_delta(
        env.project,
        env.settings,
        logger=delta_logger,
    )
    after_delta = env.git.manifest(env.local)
    assert after_delta.heads == target.heads
    assert after_delta.tags == target.tags


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("local_ahead", "领先"),
        ("divergent", "分叉"),
        ("local_refs", "服务器缺少本地分支"),
        ("tag_conflict", "Tag v1.0.0"),
    ],
)
def test_adoption_rejects_non_server_leading_refs(
    admission_env: AdoptionEnv,
    mutation: str,
    message: str,
) -> None:
    env = admission_env
    if mutation == "local_ahead":
        make_commit(env.local, "local-main.txt", "local\n", "local ahead")
    elif mutation == "divergent":
        make_commit(env.local, "local-main.txt", "local\n", "local side")
        run_git(env.server, "checkout", "main")
        make_commit(env.server, "server-main.txt", "server\n", "server side")
    elif mutation == "local_refs":
        run_git(env.local, "branch", "local-only")
        run_git(env.local, "tag", "local-only-tag")
    elif mutation == "tag_conflict":
        run_git(env.local, "tag", "-f", "-a", "v1.0.0", "-m", "moved tag")

    report = env.service.inspect_existing_repos(
        env.project,
        env.settings,
        logger=env.logger,
    )

    assert not report.can_adopt
    assert any(message in issue for issue in report.issues)


def test_adoption_rejects_dirty_workspace_and_invalid_origin(admission_env: AdoptionEnv) -> None:
    env = admission_env
    (env.local / "untracked.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(PreflightError, match="未提交或未跟踪"):
        env.service.inspect_existing_repos(env.project, env.settings, logger=env.logger)

    (env.local / "untracked.txt").unlink()
    run_git(env.local, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    with pytest.raises(PreflightError, match="origin"):
        env.service.inspect_existing_repos(env.project, env.settings, logger=env.logger)


@pytest.mark.parametrize("side", ["local", "server"])
def test_adoption_rejects_bare_repositories(
    admission_env: AdoptionEnv,
    side: str,
) -> None:
    env = admission_env
    if side == "local":
        bare = env.local.parent / "local-bare.git"
        run_git(env.local.parent, "clone", "--bare", str(env.local), str(bare))
        env.project.local_repo_path = str(bare)
    else:
        bare = env.server.parent / "server-bare.git"
        run_git(env.server.parent, "clone", "--bare", str(env.server), str(bare))
        env.project.server_repo_path = "/remote/server/server-bare.git"

    with pytest.raises(PreflightError, match="bare"):
        env.service.inspect_existing_repos(env.project, env.settings, logger=env.logger)

    assert not env.remote_temp.exists()


def test_adoption_rejects_refs_changed_after_preview(admission_env: AdoptionEnv) -> None:
    env = admission_env
    report = env.service.inspect_existing_repos(env.project, env.settings, logger=env.logger)
    before = env.git.manifest(env.local)

    run_git(env.server, "checkout", "main")
    make_commit(env.server, "changed.txt", "changed\n", "changed after preview")

    with pytest.raises(PreflightError, match="发生变化"):
        env.service.adopt_existing_repos(
            env.project,
            env.settings,
            expected=report,
            logger=env.logger,
        )

    assert env.git.manifest(env.local).heads == before.heads
    assert not env.remote_temp.exists()


def test_adoption_failure_rolls_back_all_updated_refs(
    admission_env: AdoptionEnv,
    monkeypatch,
) -> None:
    env = admission_env
    run_git(env.server, "checkout", "main")
    make_commit(env.server, "server-main.txt", "main\n", "server main")
    run_git(env.server, "checkout", "dev")
    make_commit(env.server, "server-dev.txt", "dev\n", "server dev")
    run_git(env.server, "checkout", "-b", "feature/rollback")
    make_commit(env.server, "server-feature.txt", "feature\n", "server feature")
    run_git(env.server, "tag", "v3.0.0")
    report = env.service.inspect_existing_repos(env.project, env.settings, logger=env.logger)
    before = env.git.manifest(env.local)

    def fail_merge(*_args, **_kwargs):
        raise CommandError("forced merge failure")

    monkeypatch.setattr(env.service.git, "merge_ff_only", fail_merge)

    with pytest.raises(CommandError, match="forced merge failure"):
        env.service.adopt_existing_repos(
            env.project,
            env.settings,
            expected=report,
            logger=env.logger,
        )

    after = env.git.manifest(env.local)
    assert after.heads == before.heads
    assert after.tags == before.tags
    assert env.git.incoming_refs(env.local) == ({}, {})
