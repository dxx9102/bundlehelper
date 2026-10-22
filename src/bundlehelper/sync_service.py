from __future__ import annotations

import posixpath
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .errors import CancelledError, CommandError, PreflightError, ValidationError
from .git_service import GitService
from .logging_service import OperationLogger
from .models import AppSettings, ProjectConfig, RefManifest, normalize_github_url
from .process_runner import ensure_directory_writable, safe_remove_dir, sha256_file
from .ssh_service import RemoteGitService, SshService

ProgressCallback = Callable[[int, str], None]


@dataclass(slots=True)
class DeltaPlan:
    revisions: list[str] = field(default_factory=list)
    changed_heads: dict[str, str] = field(default_factory=dict)
    new_heads: dict[str, str] = field(default_factory=dict)
    new_tags: dict[str, str] = field(default_factory=dict)

    @property
    def has_ref_changes(self) -> bool:
        return bool(self.changed_heads or self.new_heads or self.new_tags)

class SyncService:
    def __init__(self, git: GitService | None = None, known_hosts_path: str | Path | None = None):
        self.git = git or GitService()
        self.known_hosts_path = Path(known_hosts_path) if known_hosts_path else Path.home() / ".ssh" / "bundlehelper_known_hosts"

    @staticmethod
    def _check_cancel(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("操作已取消")

    @staticmethod
    def _progress(progress: ProgressCallback | None, value: int, message: str) -> None:
        if progress:
            progress(max(0, min(100, value)), message)

    @staticmethod
    def _local_repo(path: str) -> Path:
        repo = Path(path).expanduser().resolve()
        if repo == Path(repo.anchor):
            raise ValidationError("不能将 Git 仓库路径设置为磁盘根目录")
        return repo

    @staticmethod
    def _validate_server_repo_path(path: str) -> str:
        normalized = SshService.validate_remote_absolute_path(path, "服务器仓库目录")
        if normalized == "/":
            raise ValidationError("服务器仓库目录不能是根目录")
        return normalized

    def _ssh(self, config: ProjectConfig, settings: AppSettings, logger: OperationLogger) -> tuple[SshService, RemoteGitService]:
        ssh = SshService(config.ssh, self.known_hosts_path)
        ssh.check_available()
        ssh.check_server(logger=logger)
        ssh.ensure_remote_directory(config.resolved_remote_temp_dir(settings), logger=logger)
        return ssh, RemoteGitService(ssh)

    def _local_context(self, config: ProjectConfig, settings: AppSettings) -> tuple[Path, Path, Path]:
        repo = self._local_repo(config.local_repo_path)
        base = ensure_directory_writable(
            config.resolved_bundle_dir(settings),
            "本地 bundle 临时目录",
        )
        project_root = ensure_directory_writable(
            base / config.id,
            "项目 bundle 目录",
        )
        return repo, base, project_root

    def _assert_origin(self, config: ProjectConfig, repo: Path) -> None:
        actual = self.git.origin_url(repo)
        if normalize_github_url(actual) != normalize_github_url(config.github_url):
            raise PreflightError(f"本地仓库 origin 与项目地址不一致：{actual}")

    def _validate_existing_local(self, config: ProjectConfig, repo: Path) -> bool:
        if not repo.exists():
            return False
        if not repo.is_dir():
            raise ValidationError(f"本地仓库路径不是目录：{repo}")
        entries = list(repo.iterdir())
        if not entries:
            return False
        if not (repo / ".git").exists():
            raise ValidationError(f"本地目录非空且不是 Git 仓库：{repo}")
        self._assert_origin(config, repo)
        clean, detail = self.git.is_clean(repo)
        if not clean:
            raise PreflightError("本地现有仓库工作区不干净，拒绝继续：\n" + detail[:4000])
        self.git.assert_no_unsupported_features(repo)
        return True
    def bootstrap_full(
        self,
        config: ProjectConfig,
        settings: AppSettings,
        *,
        token: str | None,
        logger: OperationLogger,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RefManifest:
        config.validate()
        settings.validate()
        self._check_cancel(cancel_event)
        self._progress(progress, 2, "检查本地与服务器环境")
        self.git.check_available()
        repo, _base, project_root = self._local_context(config, settings)
        ensure_directory_writable(repo.parent, "本地仓库父目录")
        if repo.exists():
            ensure_directory_writable(repo, "本地仓库目录")
        server_repo = self._validate_server_repo_path(config.server_repo_path)
        ssh, remote_git = self._ssh(config, settings, logger)
        ssh.ensure_remote_directory(posixpath.dirname(server_repo), logger=logger)

        self._check_cancel(cancel_event)
        self._progress(progress, 8, "准备本地仓库")
        repo.parent.mkdir(parents=True, exist_ok=True)
        exists = self._validate_existing_local(config, repo)
        if not exists:
            if repo.exists() and any(repo.iterdir()):
                raise ValidationError(f"本地目录非空，拒绝覆盖：{repo}")
            self.git.clone_repository(
                config.github_url,
                repo,
                token=token,
                temp_dir=project_root,
                logger=logger,
                cancel_event=cancel_event,
            )
        else:
            self.git.create_local_branches(repo, logger=logger, remote="origin")
            self.git.assert_no_unsupported_features(repo)

        self._progress(progress, 24, "读取本地引用")
        local_manifest = self.git.manifest(repo)
        operation_id = uuid.uuid4().hex[:12]
        local_op = project_root / operation_id
        local_op.mkdir(parents=True, exist_ok=False)
        remote_base = config.resolved_remote_temp_dir(settings)
        remote_op = ssh.make_remote_operation_dir(remote_base, config.id, operation_id)
        remote_op_name = posixpath.basename(remote_op)
        bundle = local_op / "full.bundle"
        remote_bundle = posixpath.join(remote_op, "full.bundle")
        success = False
        try:
            self._check_cancel(cancel_event)
            self._progress(progress, 32, "生成全量 bundle")
            self.git.create_full_bundle(repo, bundle, logger=logger)
            local_hash = sha256_file(bundle)
            logger.info(f"本地 bundle SHA-256: {local_hash}")
            self._progress(progress, 44, "上传全量 bundle")
            ssh.upload(bundle, remote_bundle, logger=logger, cancel_event=cancel_event)
            remote_hash = remote_git.sha256(remote_bundle)
            if local_hash != remote_hash:
                raise CommandError(f"全量 bundle 传输校验失败：本地 {local_hash}，服务器 {remote_hash}")
            logger.info("服务器 bundle SHA-256 校验通过")
            self._check_cancel(cancel_event)
            self._progress(progress, 62, "在服务器克隆仓库")
            if ssh.remote_path_exists(server_repo):
                remote_git.validate_repository(server_repo)
                remote_git.assert_clean(server_repo)
                existing_manifest = remote_git.manifest(server_repo)
                if not self.git.branches_equal(existing_manifest, local_manifest):
                    raise PreflightError("服务器目标目录已存在且引用与本地不一致，拒绝覆盖")
                server_manifest = existing_manifest
                logger.info("检测到上次未完成部署的服务器仓库，引用一致，继续复用")
            else:
                remote_git.clone_from_bundle(remote_bundle, server_repo, logger)
                server_manifest = remote_git.manifest(server_repo)

            remote_git.assert_no_unsupported_features(server_repo)
            if server_manifest.heads != local_manifest.heads:
                raise PreflightError("服务器分支引用与本地不一致，首次部署未完成")
            if server_manifest.tags != local_manifest.tags:
                raise PreflightError("服务器 Tag 引用与本地不一致，首次部署未完成")
            self._check_cancel(cancel_event)
            self._progress(progress, 86, "校验服务器仓库")
            remote_git.assert_clean(server_repo)
            remote_git.assert_single_worktree(server_repo)
            success = True
            self._progress(progress, 94, "清理临时文件")
            return server_manifest
        finally:
            if success:
                try:
                    remote_git.remove_operation_dir(remote_base, remote_op, remote_op_name)
                except Exception as exc:
                    logger.error(f"清理服务器临时目录失败：{exc}")
                try:
                    safe_remove_dir(project_root, local_op, operation_id)
                except Exception as exc:
                    logger.error(f"清理本地临时目录失败：{exc}")
            else:
                logger.error(f"操作失败，保留本地临时目录：{local_op}")
                logger.error(f"操作失败，保留服务器临时目录：{remote_op}")
    def _build_delta_plan(
        self,
        baseline: RefManifest,
        server: RefManifest,
        remote_git: RemoteGitService,
        server_repo: str,
    ) -> DeltaPlan:
        deleted_heads = sorted(set(baseline.heads) - set(server.heads))
        if deleted_heads:
            raise PreflightError("服务器删除了分支，首版不允许同步分支删除：" + ", ".join(deleted_heads))
        changed_tags: list[str] = []
        for name, old_oid in baseline.tags.items():
            if name not in server.tags:
                changed_tags.append(f"{name}（已删除）")
            elif server.tags[name] != old_oid:
                changed_tags.append(f"{name}（已移动）")
        if changed_tags:
            raise PreflightError("服务器修改了已有 Tag，首版不允许：" + ", ".join(changed_tags))

        plan = DeltaPlan()
        for name, new_oid in sorted(server.heads.items()):
            old_oid = baseline.heads.get(name)
            if old_oid == new_oid:
                continue
            if old_oid:
                if not remote_git.is_ancestor(server_repo, old_oid, new_oid):
                    raise PreflightError(f"服务器分支 {name} 被改写或强制重置，拒绝增量同步")
                plan.changed_heads[name] = new_oid
                plan.revisions.append(f"{old_oid}..refs/heads/{name}")
            else:
                plan.new_heads[name] = new_oid
                plan.revisions.append(f"refs/heads/{name}")
        for name, new_oid in sorted(server.tags.items()):
            if name not in baseline.tags:
                plan.new_tags[name] = new_oid
                plan.revisions.append(f"refs/tags/{name}")
        return plan

    def _apply_delta(
        self,
        repo: Path,
        baseline: RefManifest,
        target: RefManifest,
        plan: DeltaPlan,
        incoming_heads: dict[str, str],
        incoming_tags: dict[str, str],
        logger: OperationLogger,
    ) -> None:
        current_branch = self.git.head_branch(repo)
        for name, expected in {**plan.changed_heads, **plan.new_heads}.items():
            actual = incoming_heads.get(name)
            if actual != expected:
                raise PreflightError(f"bundle 中的分支 {name} 与服务器引用不一致")
            old_oid = baseline.heads.get(name)
            if old_oid and actual and not self.git.is_ancestor(repo, old_oid, actual):
                raise PreflightError(f"分支 {name} 不是快进更新")
        for name, expected in plan.new_tags.items():
            if incoming_tags.get(name) != expected:
                raise PreflightError(f"bundle 中的 Tag {name} 与服务器引用不一致")

        updated: list[tuple[str, str, str]] = []
        created: list[str] = []
        tags_created: list[str] = []
        merged_current = False
        current_old_oid = baseline.heads.get(current_branch)
        try:
            for name, new_oid in sorted(plan.changed_heads.items()):
                if name == current_branch:
                    continue
                ref = f"refs/heads/{name}"
                old_oid = baseline.heads[name]
                self.git.update_ref(repo, ref, new_oid, old_oid, logger=logger)
                updated.append((ref, new_oid, old_oid))
            for name, new_oid in sorted(plan.new_heads.items()):
                ref = f"refs/heads/{name}"
                self.git.update_ref(repo, ref, new_oid, logger=logger)
                created.append(ref)
            for name, new_oid in sorted(plan.new_tags.items()):
                ref = f"refs/tags/{name}"
                self.git.update_ref(repo, ref, new_oid, logger=logger)
                tags_created.append(ref)
            if current_branch in plan.changed_heads:
                incoming = f"refs/bundlehelper/incoming/{current_branch}"
                self.git.merge_ff_only(repo, incoming, logger=logger)
                merged_current = True
            actual = self.git.manifest(repo)
            if actual.heads != target.heads or actual.tags != target.tags:
                raise PreflightError("应用增量后本地引用与服务器目标引用不一致")
        except Exception:
            for name in tags_created:
                self.git.delete_ref(repo, name)
            for name in created:
                self.git.delete_ref(repo, name)
            for ref, _new_oid, old_oid in reversed(updated):
                self.git.update_ref(repo, ref, old_oid)
            if merged_current and current_old_oid:
                self.git.reset_hard(repo, current_old_oid, logger=logger)
            raise
    def sync_server_delta(
        self,
        config: ProjectConfig,
        settings: AppSettings,
        *,
        logger: OperationLogger,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RefManifest:
        config.validate()
        settings.validate()
        if not config.setup_complete or not config.baseline:
            raise PreflightError("项目尚未完成首次全量部署")
        self._check_cancel(cancel_event)
        self._progress(progress, 2, "检查本地与服务器状态")
        self.git.check_available()
        repo, _base, project_root = self._local_context(config, settings)
        factory = self._validate_server_repo_path(config.server_repo_path)
        if not repo.is_dir() or not (repo / ".git").exists():
            raise PreflightError(f"本地 Git 仓库不存在：{repo}")
        self._assert_origin(config, repo)
        self.git.assert_no_unsupported_features(repo)
        self.git.assert_single_worktree(repo)
        clean, detail = self.git.is_clean(repo)
        if not clean:
            raise PreflightError("本地工作区有未提交或未跟踪文件：\n" + detail[:4000])
        local_manifest = self.git.manifest(repo)
        if not self.git.branches_equal(local_manifest, config.baseline):
            raise PreflightError("本地分支或 Tag 已偏离上次同步基线，请先处理本地变化")
        ssh, remote_git = self._ssh(config, settings, logger)
        remote_git.validate_repository(factory)
        remote_git.assert_no_unsupported_features(factory)
        remote_git.assert_single_worktree(factory)
        remote_git.assert_clean(factory)
        server_manifest = remote_git.manifest(factory)
        plan = self._build_delta_plan(config.baseline, server_manifest, remote_git, factory)
        if not plan.has_ref_changes:
            logger.info("服务器没有新的分支或 Tag 变化，仅更新同步记录")
            self._progress(progress, 95, "没有引用变化")
            return server_manifest

        operation_id = uuid.uuid4().hex[:12]
        local_op = project_root / operation_id
        local_op.mkdir(parents=True, exist_ok=False)
        remote_base = config.resolved_remote_temp_dir(settings)
        remote_op = ssh.make_remote_operation_dir(remote_base, config.id, operation_id)
        remote_op_name = posixpath.basename(remote_op)
        remote_bundle = posixpath.join(remote_op, "delta.bundle")
        local_bundle = local_op / "delta.bundle"
        success = False
        try:
            self._check_cancel(cancel_event)
            self._progress(progress, 28, "生成服务器增量 bundle")
            remote_git.create_incremental_bundle(factory, remote_bundle, plan.revisions, logger)
            remote_hash = remote_git.sha256(remote_bundle)
            self._progress(progress, 46, "下载增量 bundle")
            ssh.download(remote_bundle, local_bundle, logger=logger, cancel_event=cancel_event)
            local_hash = sha256_file(local_bundle)
            if local_hash != remote_hash:
                raise CommandError(f"增量 bundle 传输校验失败：服务器 {remote_hash}，本地 {local_hash}")
            logger.info("增量 bundle SHA-256 校验通过")
            self._check_cancel(cancel_event)
            self._progress(progress, 62, "验证并应用增量")
            self.git.verify_bundle(repo, local_bundle, logger=logger)
            self.git.fetch_bundle(repo, local_bundle, logger=logger)
            incoming_heads, incoming_tags = self.git.incoming_refs(repo)
            self._apply_delta(repo, config.baseline, server_manifest, plan, incoming_heads, incoming_tags, logger)
            self.git.cleanup_incoming_refs(repo)
            success = True
            self._progress(progress, 94, "清理临时文件")
            return server_manifest
        finally:
            if success:
                try:
                    remote_git.remove_operation_dir(remote_base, remote_op, remote_op_name)
                except Exception as exc:
                    logger.error(f"清理服务器临时目录失败：{exc}")
                try:
                    safe_remove_dir(project_root, local_op, operation_id)
                except Exception as exc:
                    logger.error(f"清理本地临时目录失败：{exc}")
            else:
                try:
                    self.git.cleanup_incoming_refs(repo)
                except Exception:
                    pass
                logger.error(f"操作失败，保留本地临时目录：{local_op}")
                logger.error(f"操作失败，保留服务器临时目录：{remote_op}")

    def push_github(
        self,
        config: ProjectConfig,
        settings: AppSettings,
        *,
        token: str | None,
        logger: OperationLogger,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        config.validate()
        settings.validate()
        self._check_cancel(cancel_event)
        self.git.check_available()
        repo, _base, project_root = self._local_context(config, settings)
        if not repo.is_dir() or not (repo / ".git").exists():
            raise ValidationError(f"本地 Git 仓库不存在：{repo}")
        self._assert_origin(config, repo)
        self._progress(progress, 18, "推送全部分支")
        operation_id = uuid.uuid4().hex[:12]
        local_op = project_root / operation_id
        local_op.mkdir(parents=True, exist_ok=False)
        success = False
        try:
            self.git.push_all(
                repo,
                config.github_url,
                token=token,
                temp_dir=local_op,
                logger=logger,
            )
            success = True
            self._progress(progress, 96, "推送完成")
        finally:
            if success:
                try:
                    safe_remove_dir(project_root, local_op, operation_id)
                except Exception as exc:
                    logger.error(f"清理本地临时目录失败：{exc}")
            else:
                logger.error(f"操作失败，保留本地临时目录：{local_op}")