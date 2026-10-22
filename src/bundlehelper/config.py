from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from .errors import BundleHelperError
from .models import AppState, app_data_dir

SERVICE_NAME = "BundleHelper"


class CredentialStore(Protocol):
    def get_token(self) -> str | None: ...
    def set_token(self, token: str | None) -> None: ...


class WindowsCredentialStore:
    def __init__(self, keyring_module=None):
        if keyring_module is None:
            import keyring

            keyring_module = keyring
        self._keyring = keyring_module

    def get_token(self) -> str | None:
        value = self._keyring.get_password(SERVICE_NAME, "github-token")
        return value or None

    def set_token(self, token: str | None) -> None:
        if token:
            self._keyring.set_password(SERVICE_NAME, "github-token", token)
            return
        try:
            self._keyring.delete_password(SERVICE_NAME, "github-token")
        except Exception:
            # A missing credential is already the desired state.
            pass


class InMemoryCredentialStore:
    def __init__(self, token: str | None = None):
        self.token = token

    def get_token(self) -> str | None:
        return self.token

    def set_token(self, token: str | None) -> None:
        self.token = token or None


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_data_dir() / "projects.json"

    def load(self) -> AppState:
        if not self.path.exists():
            return AppState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("root must be an object")
            return AppState.from_dict(raw)
        except BundleHelperError:
            raise
        except Exception as exc:
            raise BundleHelperError(f"无法读取配置文件 {self.path}: {exc}") from exc

    def save(self, state: AppState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)

    @property
    def known_hosts_path(self) -> Path:
        return self.path.parent / "known_hosts"