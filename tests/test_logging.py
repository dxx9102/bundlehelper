from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bundlehelper.logging_service import OperationLogger, prune_logs, sanitize_text


def test_sanitize_redacts_token_and_headers() -> None:
    text = "Authorization: Basic abc BUNDLE_HELPER_GITHUB_TOKEN=top-secret https://user:pass@example.com"
    clean = sanitize_text(text, ["top-secret"])
    assert "top-secret" not in clean
    assert "abc" not in clean
    assert "user:pass" not in clean


def test_operation_log_and_prune(tmp_path: Path) -> None:
    logger = OperationLogger(tmp_path, "demo", "sync", "abc123")
    logger.info("hello")
    assert logger.path.is_file()
    old = tmp_path / "bundlehelper_old_20200101_000000_dead.log"
    old.write_text("old", encoding="utf-8")
    old_time = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
    os.utime(old, (old_time, old_time))
    (tmp_path / "unrelated.txt").write_text("keep", encoding="utf-8")
    removed = prune_logs(tmp_path, 30, now=datetime.now(timezone.utc))
    assert removed == 1
    assert not old.exists()
    assert (tmp_path / "unrelated.txt").exists()