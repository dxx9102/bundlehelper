from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ValidationError

SCHEMA_VERSION = 1
_GITHUB_HTTPS = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_GITHUB_SSH = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def _local_app_data() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    return Path(value) if value else Path.home() / "AppData" / "Local"


def _roaming_app_data() -> Path:
    value = os.environ.get("APPDATA")
    return Path(value) if value else Path.home() / "AppData" / "Roaming"


def default_bundle_dir() -> str:
    return str(_local_app_data() / "BundleHelper" / "temp")


def default_log_dir() -> str:
    return str(_local_app_data() / "BundleHelper" / "logs")


def normalize_github_url(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValidationError("请输入 GitHub 仓库地址")
    match = _GITHUB_HTTPS.match(text) or _GITHUB_SSH.match(text)
    if not match:
        raise ValidationError("仅支持 https://github.com/<owner>/<repo> 形式的地址")
    owner = match.group("owner")
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"https://github.com/{owner}/{repo}.git"


def repository_name_from_url(url: str) -> str:
    normalized = normalize_github_url(url)
    return normalized.rsplit("/", 1)[-1].removesuffix(".git")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(slots=True)
class RefManifest:
    head_oid: str
    head_branch: str
    heads: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    captured_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "head_oid": self.head_oid,
            "head_branch": self.head_branch,
            "heads": dict(sorted(self.heads.items())),
            "tags": dict(sorted(self.tags.items())),
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> RefManifest | None:
        if not raw:
            return None
        return cls(
            head_oid=str(raw.get("head_oid", "")),
            head_branch=str(raw.get("head_branch", "")),
            heads={str(k): str(v) for k, v in raw.get("heads", {}).items()},
            tags={str(k): str(v) for k, v in raw.get("tags", {}).items()},
            captured_at=str(raw.get("captured_at", utc_now_iso())),
        )

    def refs_equal(self, other: RefManifest) -> bool:
        # HEAD is informational: users may check out different branches on each side.
        return self.heads == other.heads and self.tags == other.tags


@dataclass(slots=True)
class SshConfig:
    host: str = ""
    port: int = 22
    user: str = ""
    key_path: str = ""

    def validate(self) -> None:
        if not self.host.strip():
            raise ValidationError("请输入服务器地址")
        if not self.user.strip():
            raise ValidationError("请输入 SSH 用户名")
        if not 1 <= int(self.port) <= 65535:
            raise ValidationError("SSH 端口必须在 1-65535 之间")
        if self.key_path.strip() and not Path(self.key_path).expanduser().is_file():
            raise ValidationError(f"SSH 私钥不存在：{self.key_path}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host.strip(),
            "port": int(self.port),
            "user": self.user.strip(),
            "key_path": self.key_path.strip(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SshConfig:
        raw = raw or {}
        try:
            port = int(raw.get("port", 22))
        except (TypeError, ValueError):
            port = 22
        return cls(
            host=str(raw.get("host", "")),
            port=port,
            user=str(raw.get("user", "")),
            key_path=str(raw.get("key_path", "")),
        )


@dataclass(slots=True)
class AppSettings:
    bundle_dir: str = field(default_factory=default_bundle_dir)
    remote_temp_dir: str = "/tmp/bundlehelper"
    log_dir: str = field(default_factory=default_log_dir)
    log_retention_days: int = 30

    def validate(self) -> None:
        if not self.bundle_dir.strip():
            raise ValidationError("本地 bundle 临时目录不能为空")
        if not self.remote_temp_dir.strip():
            raise ValidationError("服务器临时目录不能为空")
        if not self.log_dir.strip():
            raise ValidationError("日志目录不能为空")
        if not 1 <= int(self.log_retention_days) <= 3650:
            raise ValidationError("日志保留天数必须在 1-3650 之间")

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_dir": self.bundle_dir.strip(),
            "remote_temp_dir": self.remote_temp_dir.strip(),
            "log_dir": self.log_dir.strip(),
            "log_retention_days": int(self.log_retention_days),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> AppSettings:
        raw = raw or {}
        return cls(
            bundle_dir=str(raw.get("bundle_dir", default_bundle_dir())),
            remote_temp_dir=str(raw.get("remote_temp_dir", "/tmp/bundlehelper")),
            log_dir=str(raw.get("log_dir", default_log_dir())),
            log_retention_days=int(raw.get("log_retention_days", 30)),
        )


@dataclass(slots=True)
class ProjectConfig:
    id: str = field(default_factory=new_id)
    name: str = "新项目"
    github_url: str = ""
    local_repo_path: str = ""
    server_repo_path: str = ""
    ssh: SshConfig = field(default_factory=SshConfig)
    bundle_dir_override: str | None = None
    remote_temp_dir_override: str | None = None
    log_dir_override: str | None = None
    baseline: RefManifest | None = None
    setup_complete: bool = False

    def validate(self) -> None:
        if not self.name.strip():
            raise ValidationError("项目名称不能为空")
        self.github_url = normalize_github_url(self.github_url)
        if not self.local_repo_path.strip():
            raise ValidationError("请选择本地仓库目录")
        if not self.server_repo_path.strip():
            raise ValidationError("请输入服务器仓库目录")
        self.ssh.validate()
        for value, label in (
            (self.bundle_dir_override, "本地 bundle 临时目录"),
            (self.remote_temp_dir_override, "服务器临时目录"),
            (self.log_dir_override, "日志目录"),
        ):
            if value is not None and not value.strip():
                raise ValidationError(f"{label}覆盖值不能为空；如需继承全局设置请留空")

    def resolved_bundle_dir(self, settings: AppSettings) -> str:
        return (self.bundle_dir_override or settings.bundle_dir).strip()

    def resolved_remote_temp_dir(self, settings: AppSettings) -> str:
        return (self.remote_temp_dir_override or settings.remote_temp_dir).strip()

    def resolved_log_dir(self, settings: AppSettings) -> str:
        return (self.log_dir_override or settings.log_dir).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name.strip(),
            "github_url": self.github_url,
            "local_repo_path": self.local_repo_path.strip(),
            "server_repo_path": self.server_repo_path.strip(),
            "ssh": self.ssh.to_dict(),
            "bundle_dir_override": self.bundle_dir_override,
            "remote_temp_dir_override": self.remote_temp_dir_override,
            "log_dir_override": self.log_dir_override,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "setup_complete": self.setup_complete,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProjectConfig:
        return cls(
            id=str(raw.get("id") or new_id()),
            name=str(raw.get("name", "新项目")),
            github_url=str(raw.get("github_url", "")),
            local_repo_path=str(raw.get("local_repo_path", "")),
            server_repo_path=str(raw.get("server_repo_path", "")),
            ssh=SshConfig.from_dict(raw.get("ssh")),
            bundle_dir_override=raw.get("bundle_dir_override"),
            remote_temp_dir_override=raw.get("remote_temp_dir_override"),
            log_dir_override=raw.get("log_dir_override"),
            baseline=RefManifest.from_dict(raw.get("baseline")),
            setup_complete=bool(raw.get("setup_complete", False)),
        )


@dataclass(slots=True)
class AppState:
    settings: AppSettings = field(default_factory=AppSettings)
    projects: list[ProjectConfig] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "settings": self.settings.to_dict(),
            "projects": [project.to_dict() for project in self.projects],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppState:
        version = int(raw.get("schema_version", 0))
        if version > SCHEMA_VERSION:
            raise ValidationError("配置文件由更高版本的 BundleHelper 创建，无法安全读取")
        return cls(
            settings=AppSettings.from_dict(raw.get("settings")),
            projects=[ProjectConfig.from_dict(item) for item in raw.get("projects", [])],
            schema_version=SCHEMA_VERSION,
        )


def app_data_dir() -> Path:
    return _roaming_app_data() / "BundleHelper"