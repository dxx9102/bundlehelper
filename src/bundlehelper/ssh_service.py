from __future__ import annotations

import os
import posixpath
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .errors import CommandError, PreflightError, ValidationError
from .git_service import GitService
from .logging_service import OperationLogger
from .models import RefManifest, SshConfig
from .process_runner import CommandResult, CommandRunner, posix_quote


@dataclass(slots=True)
class HostKeyOption:
    raw: str
    fingerprint: str
    key_type: str


@dataclass(frozen=True, slots=True)
class OpenSshTools:
    directory: Path
    ssh: str
    scp: str
    keyscan: str
    keygen: str


_OPENSSH_TOOL_NAMES = ("ssh", "scp", "ssh-keyscan", "ssh-keygen")


def _executable_in(directory: Path, name: str) -> str | None:
    filenames = (f"{name}.exe", name) if os.name == "nt" else (name, f"{name}.exe")
    for filename in filenames:
        candidate = directory / filename
        if candidate.is_file():
            return str(candidate)
    return None


def _tools_in_directory(directory: Path) -> OpenSshTools | None:
    resolved = directory.expanduser().resolve()
    tools = {name: _executable_in(resolved, name) for name in _OPENSSH_TOOL_NAMES}
    if not all(tools.values()):
        return None
    return OpenSshTools(
        directory=resolved,
        ssh=tools["ssh"],
        scp=tools["scp"],
        keyscan=tools["ssh-keyscan"],
        keygen=tools["ssh-keygen"],
    )


def _unique_directories(directories: Sequence[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        key = os.path.normcase(str(directory.expanduser().resolve()))
        if key not in seen:
            seen.add(key)
            result.append(directory)
    return result


def resolve_openssh_tools() -> OpenSshTools:
    """Resolve one consistent OpenSSH tool set, preferring Git for Windows.

    Windows System OpenSSH may be older than the OpenSSH shipped with Git for
    Windows and can therefore lack post-quantum KEX algorithms required by a
    server. Keeping all four binaries from the same installation avoids mixed
    known_hosts/key parsing behavior.
    """

    candidates: list[Path] = []
    override = os.environ.get("BUNDLEHELPER_SSH_DIR", "").strip()
    if override:
        candidates.append(Path(override))
    git_executable = shutil.which("git")
    if git_executable:
        git_path = Path(git_executable).resolve()
        for parent in git_path.parents:
            candidates.append(parent / "usr" / "bin")
    for directory in _unique_directories(candidates):
        tools = _tools_in_directory(directory)
        if tools:
            return tools

    resolved = {name: shutil.which(name) for name in _OPENSSH_TOOL_NAMES}
    missing = [name for name, value in resolved.items() if not value]
    if not missing:
        return OpenSshTools(
            directory=Path(resolved["ssh"]).resolve().parent,
            ssh=str(resolved["ssh"]),
            scp=str(resolved["scp"]),
            keyscan=str(resolved["ssh-keyscan"]),
            keygen=str(resolved["ssh-keygen"]),
        )
    raise CommandError(
        "未找到完整的 OpenSSH 工具集（ssh、scp、ssh-keyscan、ssh-keygen）："
        + ", ".join(missing)
        + "。请安装 Git for Windows，或通过 BUNDLEHELPER_SSH_DIR 指定包含这些程序的目录"
    )


class SshService:
    def __init__(self, config: SshConfig, known_hosts_path: str | Path, runner: CommandRunner | None = None):
        self.config = config
        self.known_hosts_path = Path(known_hosts_path).expanduser()
        self.runner = runner or CommandRunner()
        self._tools: OpenSshTools | None = None

    @property
    def tools(self) -> OpenSshTools:
        if self._tools is None:
            self._tools = resolve_openssh_tools()
        return self._tools

    def check_available(self) -> OpenSshTools:
        return self.tools

    @property
    def target(self) -> str:
        return f"{self.config.user}@{self.config.host}"

    @property
    def host_pattern(self) -> str:
        return self.config.host if self.config.port == 22 else f"[{self.config.host}]:{self.config.port}"

    def _common_options(self) -> list[str]:
        options = [
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts_path}",
            "-o", "GlobalKnownHostsFile=NUL",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
        ]
        key_path = self.config.key_path.strip()
        if key_path:
            options.extend(["-i", str(Path(key_path).expanduser()), "-o", "IdentitiesOnly=yes"])
        return options

    def _ssh_argv(self) -> list[str]:
        return [self.tools.ssh, "-p", str(self.config.port), *self._common_options(), self.target]

    def _scp_argv(self) -> list[str]:
        return [self.tools.scp, "-P", str(self.config.port), *self._common_options()]

    def is_host_known(self) -> bool:
        if not self.known_hosts_path.is_file():
            return False
        pattern = self.host_pattern
        try:
            for line in self.known_hosts_path.read_text(encoding="utf-8", errors="replace").splitlines():
                fields = line.split()
                if len(fields) >= 2 and pattern in fields[0].split(","):
                    return True
        except OSError:
            return False
        return False

    def scan_host_keys(self) -> list[HostKeyOption]:
        tools = self.tools
        result = self.runner.run(
            [tools.keyscan, "-T", "10", "-p", str(self.config.port), self.config.host],
            check=False,
        )
        if not result.ok or not result.output.strip():
            detail = result.output.strip() or "没有返回密钥"
            hint = ""
            if "unsupported kex method" in detail.lower():
                hint = (
                    "\n当前 OpenSSH 客户端与服务器没有共同的密钥交换算法。"
                    "请升级 Git for Windows，或通过 BUNDLEHELPER_SSH_DIR 指定兼容的 OpenSSH 目录。"
                )
            raise PreflightError(
                f"无法读取服务器主机密钥（客户端：{tools.keyscan}）：{detail}{hint}"
            )
        key_lines = [
            line.strip()
            for line in result.output.splitlines()
            if re.match(r"^\S+\s+(ssh-|ecdsa-|sk-ssh-|sk-ecdsa-)", line.strip())
        ]
        if not key_lines:
            raise PreflightError("服务器没有返回可识别的 SSH 主机密钥")
        options: list[HostKeyOption] = []
        for line in key_lines:
            fields = line.split()
            key_type = fields[1] if len(fields) > 1 else "unknown"
            normalized = f"{self.host_pattern} " + " ".join(fields[1:])
            with tempfile.NamedTemporaryFile("w", encoding="ascii", delete=False, suffix=".pub") as temp:
                temp.write(normalized + "\n")
                temp_path = temp.name
            try:
                fingerprint_result = self.runner.run([tools.keygen, "-lf", temp_path], check=False)
                if fingerprint_result.ok and fingerprint_result.output.strip():
                    fingerprint = fingerprint_result.output.strip().split()[1]
                    options.append(HostKeyOption(raw=normalized, fingerprint=fingerprint, key_type=key_type))
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        if not options:
            raise PreflightError("无法计算服务器主机密钥指纹")
        return options

    def trust_host_keys(self, options: Sequence[HostKeyOption]) -> None:
        if not options:
            raise ValidationError("没有可保存的主机密钥")
        self.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if self.known_hosts_path.exists():
            existing = set(self.known_hosts_path.read_text(encoding="utf-8", errors="replace").splitlines())
        with self.known_hosts_path.open("a", encoding="utf-8", newline="\n") as stream:
            for option in options:
                if option.raw not in existing:
                    stream.write(option.raw + "\n")

    def clear_host_key(self) -> None:
        if not self.known_hosts_path.exists():
            return
        pattern = self.host_pattern
        kept = []
        for line in self.known_hosts_path.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split()
            if len(fields) >= 2 and pattern in fields[0].split(","):
                continue
            kept.append(line)
        self.known_hosts_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    def _ensure_known_host(self) -> None:
        if not self.is_host_known():
            raise PreflightError("首次连接该服务器，请先点击“检查 SSH”并确认主机指纹")
    def run(
        self,
        remote_command: str,
        *,
        logger: OperationLogger | None = None,
        check: bool = True,
        cancel_event: threading.Event | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> CommandResult:
        self._ensure_known_host()
        argv = [*self._ssh_argv(), "--", remote_command]
        if logger:
            logger.command([argv[0], *argv[1:-1], remote_command])
        callback = on_output
        if logger and on_output:
            callback = lambda line: (on_output(line), logger.info(line))  # noqa: E731
        elif logger:
            callback = logger.info
        return self.runner.run(argv, on_output=callback, cancel_event=cancel_event, check=check)

    def check_server(self, *, logger: OperationLogger | None = None) -> str:
        result = self.run("git --version && command -v sha256sum >/dev/null && echo BUNDLEHELPER_OK", logger=logger)
        if "BUNDLEHELPER_OK" not in result.output:
            raise PreflightError("服务器必须安装 Git 和 sha256sum")
        return result.output.strip()

    @staticmethod
    def validate_remote_absolute_path(path: str, label: str) -> str:
        value = path.strip()
        if not value.startswith("/"):
            raise ValidationError(f"{label}必须使用 Linux 绝对路径")
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValidationError(f"{label}包含无效字符")
        return posixpath.normpath(value)

    def ensure_remote_directory(self, path: str, *, logger: OperationLogger | None = None) -> None:
        directory = self.validate_remote_absolute_path(path, "服务器目录")
        quoted = posix_quote(directory)
        self.run(f"mkdir -p -- {quoted} && test -d {quoted} && test -w {quoted}", logger=logger)

    def remote_path_exists(self, path: str) -> bool:
        quoted = posix_quote(path)
        result = self.run(f"test -e {quoted}", check=False)
        if result.returncode not in (0, 1):
            raise CommandError(result.output.strip() or f"无法检查服务器路径：{path}")
        return result.returncode == 0

    def upload(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        logger: OperationLogger | None = None,
        cancel_event: threading.Event | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        self._ensure_known_host()
        remote = f"{self.target}:{posix_quote(remote_path)}"
        argv = [*self._scp_argv(), os.fspath(local_path), remote]
        if logger:
            logger.command(argv)
        self.runner.run(argv, on_output=on_output, cancel_event=cancel_event)

    def download(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        logger: OperationLogger | None = None,
        cancel_event: threading.Event | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> None:
        self._ensure_known_host()
        remote = f"{self.target}:{posix_quote(remote_path)}"
        argv = [*self._scp_argv(), remote, os.fspath(local_path)]
        if logger:
            logger.command(argv)
        self.runner.run(argv, on_output=on_output, cancel_event=cancel_event)

    def sha256_remote(self, remote_path: str) -> str:
        result = self.run(f"sha256sum -- {posix_quote(remote_path)}")
        match = re.search(r"\b([0-9a-fA-F]{64})\b", result.output)
        if not match:
            raise CommandError(f"无法解析服务器 SHA-256：{result.output.strip()}")
        return match.group(1).lower()

    def make_remote_operation_dir(self, base_dir: str, project_id: str, operation_id: str) -> str:
        base = self.validate_remote_absolute_path(base_dir, "服务器临时目录")
        name = f"bundlehelper-{project_id}-{operation_id}"
        path = posixpath.join(base, name)
        self.ensure_remote_directory(path)
        return path

    def remove_remote_operation_dir(self, base_dir: str, path: str, expected_name: str) -> None:
        base = self.validate_remote_absolute_path(base_dir, "服务器临时目录")
        normalized_base = posixpath.normpath(base)
        normalized_path = posixpath.normpath(path)
        if normalized_path == normalized_base or not normalized_path.startswith(normalized_base.rstrip("/") + "/"):
            raise CommandError(f"拒绝删除服务器临时目录之外的路径：{normalized_path}")
        if posixpath.basename(normalized_path) != expected_name:
            raise CommandError(f"拒绝删除名称不符合预期的服务器目录：{normalized_path}")
        self.run(f"rm -rf -- {posix_quote(normalized_path)}")

class RemoteGitService:
    def __init__(self, ssh: SshService):
        self.ssh = ssh

    @staticmethod
    def _git(repo: str, *args: str) -> str:
        return " ".join(["git", "-C", posix_quote(repo), *[posix_quote(arg) for arg in args]])

    def run(self, repo: str, args: Sequence[str], *, logger: OperationLogger | None = None, check: bool = True) -> CommandResult:
        return self.ssh.run(self._git(repo, *args), logger=logger, check=check)

    def validate_repository(self, repo: str) -> None:
        self.ssh.run(self._git(repo, "rev-parse", "--git-dir"))

    def assert_clean(self, repo: str) -> None:
        result = self.run(repo, ["status", "--porcelain=v1", "--untracked-files=normal"], check=False)
        if not result.ok:
            raise CommandError(result.output.strip() or "无法检查服务器工作区")
        changes = GitService.porcelain_changes(result.output)
        if changes:
            raise PreflightError("服务器仓库有未提交或未跟踪文件，请先提交清理后重试\n" + "\n".join(changes)[:4000])

    def assert_single_worktree(self, repo: str) -> None:
        result = self.run(repo, ["worktree", "list", "--porcelain"], check=False)
        if not result.ok:
            raise CommandError(result.output.strip() or "无法读取服务器 worktree 列表")
        count = sum(1 for line in result.output.splitlines() if line.startswith("worktree "))
        if count > 1:
            raise PreflightError("服务器仓库存在多个 worktree；请先移除关联 worktree")

    def head_branch(self, repo: str) -> str:
        result = self.run(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        name = result.output.strip() if result.ok else ""
        if not name:
            raise PreflightError("服务器仓库处于 detached HEAD")
        return name

    def rev_parse(self, repo: str, rev: str) -> str:
        return self.run(repo, ["rev-parse", "--verify", rev]).output.strip().splitlines()[0]

    def _refs(self, repo: str, prefix: str) -> dict[str, str]:
        result = self.run(repo, ["for-each-ref", "--format=%(objectname) %(refname)", prefix])
        refs: dict[str, str] = {}
        for line in result.output.splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2:
                refs[parts[1][len(prefix) :]] = parts[0]
        return refs

    def manifest(self, repo: str) -> RefManifest:
        return RefManifest(
            head_oid=self.rev_parse(repo, "HEAD"),
            head_branch=self.head_branch(repo),
            heads=self._refs(repo, "refs/heads/"),
            tags=self._refs(repo, "refs/tags/"),
        )

    def is_bare(self, repo: str) -> bool:
        result = self.run(repo, ["rev-parse", "--is-bare-repository"], check=False)
        return result.ok and result.output.strip().lower() == "true"

    def commit_exists(self, repo: str, oid: str) -> bool:
        result = self.run(repo, ["cat-file", "-e", f"{oid}^{{commit}}"], check=False)
        return result.ok

    def is_ancestor(self, repo: str, ancestor: str, descendant: str) -> bool:
        result = self.run(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False)
        if result.returncode not in (0, 1):
            raise CommandError(result.output.strip() or "无法检查服务器提交关系")
        return result.returncode == 0

    def assert_no_unsupported_features(self, repo: str) -> None:
        modules = self.run(repo, ["ls-files", "--", ".gitmodules"], check=False)
        if modules.ok and modules.output.strip():
            raise PreflightError("服务器仓库包含 submodule；首版不支持")
        lfs = self.run(repo, ["grep", "-n", "-I", "-E", "filter\\s*=\\s*lfs", "--", "*gitattributes", ".gitattributes"], check=False)
        if lfs.ok and lfs.output.strip():
            raise PreflightError("服务器仓库包含 Git LFS 配置；首版不支持")

    def create_incremental_bundle(self, repo: str, bundle_path: str, revisions: Sequence[str], logger: OperationLogger) -> None:
        self.run(repo, ["bundle", "create", bundle_path, *revisions], logger=logger)

    def sha256(self, remote_path: str) -> str:
        return self.ssh.sha256_remote(remote_path)
    def clone_from_bundle(self, bundle_path: str, destination: str, logger: OperationLogger) -> None:
        dest_quoted = posix_quote(destination)
        bundle_quoted = posix_quote(bundle_path)
        self.ssh.run(f"git clone --no-single-branch {bundle_quoted} {dest_quoted}", logger=logger)
        refs = self.run(destination, ["for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/origin"])
        locals_result = self.run(destination, ["for-each-ref", "--format=%(refname:strip=2)", "refs/heads"])
        local_names = {line.strip() for line in locals_result.output.splitlines() if line.strip()}
        for raw_name in refs.output.splitlines():
            name = raw_name.strip()
            if not name or name == "HEAD" or name in local_names:
                continue
            self.run(destination, ["branch", "--track", name, f"origin/{name}"], logger=logger)
        self.run(destination, ["remote", "remove", "origin"], logger=logger)

    def remove_operation_dir(self, base_dir: str, path: str, expected_name: str) -> None:
        self.ssh.remove_remote_operation_dir(base_dir, path, expected_name)
