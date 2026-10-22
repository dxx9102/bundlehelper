from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from bundlehelper.errors import CancelledError, CommandError
from bundlehelper.process_runner import CommandRunner, ensure_directory_writable, safe_remove_dir


def test_runner_streams_and_redacts_output() -> None:
    seen: list[str] = []
    result = CommandRunner(poll_interval=0.02).run(
        [sys.executable, "-u", "-c", "print('visible TOP-SECRET')"],
        on_output=seen.append,
        secrets=["TOP-SECRET"],
    )
    assert result.ok
    assert "TOP-SECRET" not in result.output
    assert "***" in result.output
    assert seen == ["visible ***"]


def test_runner_reports_failure_with_sanitized_tail() -> None:
    with pytest.raises(CommandError) as caught:
        CommandRunner(poll_interval=0.02).run(
            [sys.executable, "-u", "-c", "print('failed TOP-SECRET'); raise SystemExit(7)"],
            secrets=["TOP-SECRET"],
        )
    assert caught.value.returncode == 7
    assert "TOP-SECRET" not in str(caught.value)
    assert "failed ***" in str(caught.value)


def test_runner_cancels_running_process() -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.25, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(CancelledError):
            CommandRunner(poll_interval=0.02).run(
                [sys.executable, "-u", "-c", "import time; print('started', flush=True); time.sleep(20)"],
                cancel_event=cancel,
            )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 6


def test_ensure_directory_writable_creates_nested_unicode_directory(tmp_path: Path) -> None:
    target = tmp_path / "\u4e2d\u6587 \u76ee\u5f55" / "nested"
    resolved = ensure_directory_writable(target, "bundle")
    assert resolved == target.resolve()
    assert resolved.is_dir()


def test_ensure_directory_writable_rejects_file_path(tmp_path: Path) -> None:
    target = tmp_path / "not-a-directory"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(CommandError):
        ensure_directory_writable(target, "bundle")


def test_safe_remove_dir_only_removes_named_child_and_preserves_siblings(tmp_path: Path) -> None:
    root = tmp_path / "configured"
    operation = root / "operation123"
    operation.mkdir(parents=True)
    (operation / "bundle.bin").write_bytes(b"data")
    unrelated = root / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")
    assert safe_remove_dir(root, operation, "operation123") is True
    assert not operation.exists()
    assert unrelated.exists()
    assert safe_remove_dir(root, operation, "operation123") is False


@pytest.mark.parametrize(
    ("allowed_root", "target", "expected"),
    [
        (Path("root"), Path("root"), "root"),
        (Path("root"), Path("outside"), "outside"),
        (Path("root"), Path("root") / "operation123-copy", "operation123"),
    ],
)
def test_safe_remove_dir_rejects_unsafe_targets(
    tmp_path: Path,
    allowed_root: Path,
    target: Path,
    expected: str,
) -> None:
    root = tmp_path / allowed_root
    root.mkdir()
    candidate = tmp_path / target if not target.is_absolute() else target
    candidate.mkdir(parents=True, exist_ok=True)
    with pytest.raises(CommandError):
        safe_remove_dir(root, candidate, expected)
    assert candidate.exists()
