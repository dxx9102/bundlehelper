from __future__ import annotations

from pathlib import Path

import pytest

from bundlehelper.config import ConfigStore, InMemoryCredentialStore
from bundlehelper.errors import ValidationError
from bundlehelper.models import AppSettings, ProjectConfig, RefManifest, normalize_github_url


def test_normalize_github_url_variants() -> None:
    assert normalize_github_url("https://github.com/acme/demo") == "https://github.com/acme/demo.git"
    assert normalize_github_url("git@github.com:acme/demo.git") == "https://github.com/acme/demo.git"
    assert normalize_github_url("https://github.com/acme/demo.git/") == "https://github.com/acme/demo.git"


def test_normalize_rejects_non_github() -> None:
    with pytest.raises(ValidationError):
        normalize_github_url("https://gitlab.com/acme/demo")


def test_ref_manifest_equality_ignores_informational_head() -> None:
    left = RefManifest("a", "main", {"main": "a"}, {"v1": "t"})
    right = RefManifest("b", "dev", {"main": "a"}, {"v1": "t"})
    assert left.refs_equal(right)


def test_config_store_round_trip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "projects.json")
    state = store.load()
    state.settings = AppSettings(bundle_dir=str(tmp_path / "bundles"), remote_temp_dir="/var/tmp/bh", log_dir=str(tmp_path / "logs"))
    project = ProjectConfig(name="demo", github_url="https://github.com/a/b", local_repo_path=str(tmp_path / "local"), server_repo_path="/srv/repo")
    project.ssh.host = "example.com"
    project.ssh.user = "git"
    state.projects.append(project)
    store.save(state)
    loaded = store.load()
    assert loaded.projects[0].name == "demo"
    assert loaded.settings.remote_temp_dir == "/var/tmp/bh"


def test_credentials_are_not_in_project_dict(tmp_path: Path) -> None:
    credentials = InMemoryCredentialStore()
    credentials.set_token("secret-token")
    project = ProjectConfig(name="demo", github_url="https://github.com/a/b", local_repo_path=str(tmp_path), server_repo_path="/srv/repo")
    assert "secret-token" not in str(project.to_dict())