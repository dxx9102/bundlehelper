from __future__ import annotations

import os
from pathlib import Path

import pytest

from bundlehelper import ssh_service
from bundlehelper.errors import PreflightError
from bundlehelper.models import SshConfig
from bundlehelper.process_runner import CommandResult
from bundlehelper.ssh_service import SshService


def make_tool_directory(root: Path) -> Path:
    root.mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    for name in ("ssh", "scp", "ssh-keyscan", "ssh-keygen"):
        (root / f"{name}{suffix}").write_text("", encoding="ascii")
    return root


def test_resolve_openssh_tools_prefers_git_bundled_suite(tmp_path: Path, monkeypatch) -> None:
    git_root = tmp_path / "Git"
    git_cmd = git_root / "cmd" / "git.exe"
    git_cmd.parent.mkdir(parents=True)
    git_cmd.write_text("", encoding="ascii")
    bundled = make_tool_directory(git_root / "usr" / "bin")
    monkeypatch.delenv("BUNDLEHELPER_SSH_DIR", raising=False)
    monkeypatch.setattr(
        ssh_service.shutil,
        "which",
        lambda name: str(git_cmd) if name == "git" else None,
    )

    tools = ssh_service.resolve_openssh_tools()

    assert tools.directory == bundled.resolve()
    assert Path(tools.ssh).parent == bundled.resolve()


def test_resolve_openssh_tools_honors_explicit_directory(tmp_path: Path, monkeypatch) -> None:
    explicit = make_tool_directory(tmp_path / "custom openssh")
    monkeypatch.setenv("BUNDLEHELPER_SSH_DIR", str(explicit))
    monkeypatch.setattr(ssh_service.shutil, "which", lambda _name: None)

    tools = ssh_service.resolve_openssh_tools()

    assert tools.directory == explicit.resolve()


class FakeRunner:
    def __init__(self, keyscan_output: str, keyscan_code: int = 0) -> None:
        self.keyscan_output = keyscan_output
        self.keyscan_code = keyscan_code
        self.calls: list[list[str]] = []

    def run(self, argv, **_kwargs) -> CommandResult:
        args = [os.fspath(value) for value in argv]
        self.calls.append(args)
        if Path(args[0]).name.lower().startswith("ssh-keyscan"):
            return CommandResult(args, self.keyscan_code, self.keyscan_output)
        return CommandResult(args, 0, "256 SHA256:test-fingerprint comment (ED25519)")


def test_scan_host_keys_uses_resolved_tool_set(tmp_path: Path, monkeypatch) -> None:
    tools = make_tool_directory(tmp_path / "openssh")
    monkeypatch.setenv("BUNDLEHELPER_SSH_DIR", str(tools))
    runner = FakeRunner("[example.com]:8001 ssh-ed25519 AAAATESTKEY")
    service = SshService(
        SshConfig(host="example.com", port=8001, user="dev"),
        tmp_path / "known_hosts",
        runner=runner,
    )

    options = service.scan_host_keys()

    assert options[0].fingerprint == "SHA256:test-fingerprint"
    assert options[0].raw == "[example.com]:8001 ssh-ed25519 AAAATESTKEY"
    assert Path(runner.calls[0][0]).parent == tools.resolve()
    assert Path(runner.calls[1][0]).parent == tools.resolve()


def test_scan_host_keys_explains_kex_mismatch(tmp_path: Path, monkeypatch) -> None:
    tools = make_tool_directory(tmp_path / "openssh")
    monkeypatch.setenv("BUNDLEHELPER_SSH_DIR", str(tools))
    runner = FakeRunner("choose_kex: unsupported KEX method sntrup761x25519-sha512@openssh.com", 1)
    service = SshService(
        SshConfig(host="example.com", port=8001, user="dev"),
        tmp_path / "known_hosts",
        runner=runner,
    )

    with pytest.raises(PreflightError, match="没有共同的密钥交换算法"):
        service.scan_host_keys()
