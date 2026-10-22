from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Callable, Sequence

from .errors import CancelledError, CommandError, PreflightError
from .logging_service import OperationLogger
from .models import RefManifest, normalize_github_url
from .process_runner import CommandResult, CommandRunner, require_executable


class GitService:
    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or CommandRunner()

    def check_available(self) -> str:
        executable = require_executable("git")
        result = self.runner.run([executable, "--version"], on_output=None)
        return result.output.strip()

    def _run(
        self,
        args: Sequence[str],
        *,
        repo: str | Path | None = None,
        logger: OperationLogger | None = None,
        on_output: Callable[[str], None] | None = None,
        env: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        check: bool = True,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        executable = require_executable("git")
        argv = [executable]
        if repo is not None:
            argv.extend(["-C", os.fspath(repo)])
        argv.extend(args)
        if logger:
            logger.command(argv)
        callback = on_output
        if logger and on_output:
            callback = lambda line: (on_output(line), logger.info(line))  # noqa: E731
        elif logger:
            callback = logger.info
        return self.runner.run(
            argv,
            env=env,
            on_output=callback,
            cancel_event=cancel_event,
            check=check,
            secrets=secrets,
        )

    def _askpass_env(self, temp_dir: Path, token: str) -> dict[str, str]:
        temp_dir.mkdir(parents=True, exist_ok=True)
        helper = temp_dir / "bundlehelper-askpass.cmd"
        helper.write_text(
            "@echo off\r\n"
            "echo %BUNDLE_HELPER_GITHUB_TOKEN%\r\n",
            encoding="ascii",
        )
        return {
            "GIT_ASKPASS": str(helper),
            "GIT_TERMINAL_PROMPT": "0",
            "BUNDLE_HELPER_GITHUB_TOKEN": token,
        }

    @staticmethod
    def _credential_url(url: str) -> str:
        normalized = normalize_github_url(url)
        return normalized.replace("https://", "https://x-access-token@", 1)

    def clone_repository(
        self,
        url: str,
        destination: str | Path,
        *,
        token: str | None,
        temp_dir: Path,
        logger: OperationLogger,
        on_output: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        dest = Path(destination).expanduser()
        env = self._askpass_env(temp_dir, token) if token else {"GIT_TERMINAL_PROMPT": "0"}
        clone_url = self._credential_url(url) if token else normalize_github_url(url)
        secrets = [token] if token else []
        self._run(
            ["clone", "--no-single-branch", clone_url, str(dest)],
            logger=logger,
            on_output=on_output,
            env=env,
            cancel_event=cancel_event,
            secrets=secrets,
        )
        self._run(
            ["remote", "set-url", "origin", normalize_github_url(url)],
            repo=dest,
            logger=logger,
            secrets=secrets,
        )
        self.create_local_branches(dest, logger=logger, remote="origin")
        self.assert_no_unsupported_features(dest)

    def create_local_branches(
        self,
        repo: str | Path,
        *,
        logger: OperationLogger | None = None,
        remote: str = "origin",
    ) -> list[str]:
        refs = self.runner.run(
            [require_executable("git"), "-C", os.fspath(repo), "for-each-ref", "--format=%(refname:strip=3)", f"refs/remotes/{remote}"],
            check=False,
        )
        if not refs.ok:
            raise CommandError(refs.output or "无法读取远端分支", command=" ".join(refs.argv), returncode=refs.returncode)
        local_result = self.runner.run(
            [require_executable("git"), "-C", os.fspath(repo), "for-each-ref", "--format=%(refname:strip=2)", "refs/heads"],
            check=False,
        )
        if not local_result.ok:
            raise CommandError(local_result.output or "无法读取本地分支", command=" ".join(local_result.argv), returncode=local_result.returncode)
        local_names = {line.strip() for line in local_result.output.splitlines() if line.strip()}
        created: list[str] = []
        for raw_name in refs.output.splitlines():
            name = raw_name.strip()
            if not name or name == "HEAD" or name in local_names:
                continue
            self._run(
                ["branch", "--track", name, f"{remote}/{name}"],
                repo=repo,
                logger=logger,
            )
            created.append(name)
        return created

    def assert_no_unsupported_features(self, repo: str | Path) -> None:
        git = require_executable("git")
        modules = self.runner.run(
            [git, "-C", os.fspath(repo), "ls-files", "--", ".gitmodules"],
            check=False,
        )
        if modules.ok and modules.output.strip():
            raise PreflightError("检测到 submodule（.gitmodules）；首版不支持 submodule")
        attributes = self.runner.run(
            [git, "-C", os.fspath(repo), "ls-files", "--", "*gitattributes", ".gitattributes"],
            check=False,
        )
        if not attributes.ok:
            return
        for name in attributes.output.splitlines():
            path = Path(repo) / name.strip()
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"(?i)filter\s*=\s*lfs", content):
                raise PreflightError(f"检测到 Git LFS 配置（{name}）；首版不支持 LFS")

    @staticmethod
    def porcelain_changes(output: str) -> list[str]:
        # Git diagnostics may share the merged command stream with stdout. Only
        # XY-prefixed porcelain records represent working-tree changes.
        valid = set(" MADRCU?!T")
        changes: list[str] = []
        for line in output.splitlines():
            if len(line) >= 3 and line[0] in valid and line[1] in valid and line[2] == " ":
                changes.append(line.rstrip())
        return changes

    def is_clean(self, repo: str | Path) -> tuple[bool, str]:
        result = self._run(
            ["status", "--porcelain=v1", "--untracked-files=normal"],
            repo=repo,
            check=False,
        )
        if not result.ok:
            raise CommandError(result.output.strip() or "无法检查工作区状态", command=" ".join(result.argv))
        changes = self.porcelain_changes(result.output)
        return not changes, "\n".join(changes)

    def assert_single_worktree(self, repo: str | Path) -> None:
        result = self._run(["worktree", "list", "--porcelain"], repo=repo, check=False)
        if not result.ok:
            raise CommandError(result.output.strip() or "无法读取 worktree 列表", command=" ".join(result.argv))
        count = sum(1 for line in result.output.splitlines() if line.startswith("worktree "))
        if count > 1:
            raise PreflightError("检测到多个 Git worktree；为避免更新其他工作区，请先移除关联 worktree")

    def head_branch(self, repo: str | Path) -> str:
        result = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"], repo=repo, check=False)
        name = result.output.strip() if result.ok else ""
        if not name:
            raise PreflightError("当前仓库处于 detached HEAD，请先切换到一个分支")
        return name

    def rev_parse(self, repo: str | Path, rev: str) -> str:
        result = self._run(["rev-parse", "--verify", rev], repo=repo)
        return result.output.strip().splitlines()[0]

    def manifest(self, repo: str | Path) -> RefManifest:
        branch = self.head_branch(repo)
        head_oid = self.rev_parse(repo, "HEAD")
        heads = self._refs(repo, "refs/heads/")
        tags = self._refs(repo, "refs/tags/")
        return RefManifest(head_oid=head_oid, head_branch=branch, heads=heads, tags=tags)

    def _refs(self, repo: str | Path, prefix: str) -> dict[str, str]:
        result = self._run(
            ["for-each-ref", "--format=%(objectname) %(refname)", prefix],
            repo=repo,
        )
        output: dict[str, str] = {}
        for line in result.output.splitlines():
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            oid, refname = parts
            output[refname[len(prefix) :]] = oid
        return output

    def origin_url(self, repo: str | Path) -> str:
        result = self._run(["remote", "get-url", "origin"], repo=repo, check=False)
        if not result.ok:
            raise PreflightError("本地仓库没有 origin 远端")
        return result.output.strip()

    def create_full_bundle(self, repo: str | Path, destination: Path, *, logger: OperationLogger | None = None) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ["bundle", "create", str(destination), "--branches", "--tags", "HEAD"],
            repo=repo,
            logger=logger,
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise CommandError(f"bundle 未成功生成：{destination}")

    def create_incremental_bundle(
        self,
        repo: str | Path,
        destination: Path,
        *,
        old_manifest: RefManifest,
        new_manifest: RefManifest,
        logger: OperationLogger | None = None,
    ) -> list[str]:
        revisions: list[str] = []
        for name, oid in sorted(new_manifest.heads.items()):
            old_oid = old_manifest.heads.get(name)
            if old_oid == oid:
                continue
            revisions.append(f"{old_oid}..refs/heads/{name}" if old_oid else f"refs/heads/{name}")
        for name, oid in sorted(new_manifest.tags.items()):
            old_oid = old_manifest.tags.get(name)
            if old_oid != oid:
                revisions.append(f"refs/tags/{name}")
        if not revisions:
            raise PreflightError("服务器仓库自上次同步后没有引用变化")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(["bundle", "create", str(destination), *revisions], repo=repo, logger=logger)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise CommandError(f"增量 bundle 未成功生成：{destination}")
        return revisions

    def verify_bundle(self, repo: str | Path, bundle: Path, *, logger: OperationLogger | None = None) -> None:
        result = self._run(["bundle", "verify", str(bundle)], repo=repo, logger=logger)
        text = result.output
        if "does not contain" in text.lower() and "prerequisite" in text.lower():
            raise PreflightError("bundle 缺少本地仓库中的 prerequisite 提交")

    def fetch_bundle(self, repo: str | Path, bundle: Path, *, logger: OperationLogger | None = None) -> None:
        self._run(
            [
                "fetch",
                "--no-tags",
                str(bundle),
                "+refs/heads/*:refs/bundlehelper/incoming/*",
                "+refs/tags/*:refs/bundlehelper/incoming-tags/*",
            ],
            repo=repo,
            logger=logger,
        )

    def incoming_refs(self, repo: str | Path) -> tuple[dict[str, str], dict[str, str]]:
        return self._refs(repo, "refs/bundlehelper/incoming/"), self._refs(repo, "refs/bundlehelper/incoming-tags/")

    def is_ancestor(self, repo: str | Path, ancestor: str, descendant: str) -> bool:
        result = self._run(["merge-base", "--is-ancestor", ancestor, descendant], repo=repo, check=False)
        if result.returncode not in (0, 1):
            raise CommandError(result.output.strip() or "无法检查提交祖先关系", command=" ".join(result.argv))
        return result.returncode == 0

    def update_ref(self, repo: str | Path, ref: str, new_oid: str, old_oid: str | None = None, *, logger: OperationLogger | None = None) -> None:
        args = ["update-ref", ref, new_oid]
        if old_oid:
            args.append(old_oid)
        self._run(args, repo=repo, logger=logger)

    def delete_ref(self, repo: str | Path, ref: str, old_oid: str | None = None) -> None:
        args = ["update-ref", "-d", ref]
        if old_oid:
            args.append(old_oid)
        self._run(args, repo=repo, check=False)

    def merge_ff_only(self, repo: str | Path, incoming_ref: str, *, logger: OperationLogger | None = None) -> None:
        self._run(["merge", "--ff-only", "--no-edit", incoming_ref], repo=repo, logger=logger)

    def reset_hard(self, repo: str | Path, oid: str, *, logger: OperationLogger | None = None) -> None:
        self._run(["reset", "--hard", oid], repo=repo, logger=logger)

    def cleanup_incoming_refs(self, repo: str | Path) -> None:
        refs = self._refs(repo, "refs/bundlehelper/")
        for name in refs:
            self.delete_ref(repo, f"refs/bundlehelper/{name}")

    def push_all(
        self,
        repo: str | Path,
        remote_url: str,
        *,
        token: str | None,
        temp_dir: Path,
        logger: OperationLogger,
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        env = self._askpass_env(temp_dir, token) if token else {"GIT_TERMINAL_PROMPT": "0"}
        secrets = [token] if token else []
        destination = self._credential_url(remote_url) if token else remote_url
        self._run(
            ["push", destination, "--all"],
            repo=repo,
            logger=logger,
            on_output=on_output,
            env=env,
            secrets=secrets,
        )
        self._run(
            ["push", destination, "--tags"],
            repo=repo,
            logger=logger,
            on_output=on_output,
            env=env,
            secrets=secrets,
        )

    def branches_equal(self, left: RefManifest, right: RefManifest) -> bool:
        return left.heads == right.heads and left.tags == right.tags