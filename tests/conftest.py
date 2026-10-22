from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


GIT_ENV = {
    "GIT_AUTHOR_NAME": "BundleHelper Test",
    "GIT_AUTHOR_EMAIL": "bundlehelper@example.invalid",
    "GIT_COMMITTER_NAME": "BundleHelper Test",
    "GIT_COMMITTER_EMAIL": "bundlehelper@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def run_git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(GIT_ENV)
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def make_commit(cwd: Path, filename: str, content: str, message: str) -> str:
    target = cwd / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    run_git(cwd, "add", filename)
    run_git(cwd, "commit", "-m", message)
    return run_git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    run_git(repo, "init", "-b", "main")
    make_commit(repo, "README.md", "main\n", "initial")
    run_git(repo, "branch", "dev")
    run_git(repo, "tag", "-a", "v1.0.0", "-m", "release 1")
    return repo