from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

_LOG_NAME = re.compile(r"^bundlehelper_[A-Za-z0-9_.-]+_\d{8}_\d{6}_[A-Za-z0-9]+\.log$")


def sanitize_text(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    result = re.sub(r"(?i)(authorization\s*:\s*basic\s+)\S+", r"\1***", result)
    result = re.sub(r"(?i)(BUNDLE_HELPER_GITHUB_TOKEN=)\S+", r"\1***", result)
    result = re.sub(r"https://[^\s/@:]+:[^\s/@]+@", "https://***@", result)
    return result


def prune_logs(directory: Path, retention_days: int, now: datetime | None = None) -> int:
    if not directory.exists():
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(1, retention_days))
    removed = 0
    for item in directory.iterdir():
        if not item.is_file() or not _LOG_NAME.match(item.name):
            continue
        try:
            modified = datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class OperationLogger:
    def __init__(
        self,
        directory: str | Path,
        project_name: str,
        operation: str,
        operation_id: str,
        *,
        on_output: Callable[[str], None] | None = None,
        secrets: list[str] | tuple[str, ...] = (),
    ):
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True)
        safe_project = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_name).strip("._") or "project"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = self.directory / f"bundlehelper_{safe_project}_{stamp}_{operation_id}.log"
        self.on_output = on_output
        self.secrets = tuple(secret for secret in secrets if secret)
        self._write(f"=== {operation} / {project_name} / {stamp} ===")

    @property
    def filename(self) -> str:
        return self.path.name

    def _write(self, line: str) -> None:
        clean = sanitize_text(line.rstrip("\r\n"), self.secrets)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(clean + "\n")
        if self.on_output:
            self.on_output(clean)

    def info(self, message: str) -> None:
        self._write(message)

    def command(self, argv: list[str] | tuple[str, ...]) -> None:
        self._write("$ " + " ".join(str(value) for value in argv))

    def error(self, message: str) -> None:
        self._write("ERROR: " + message)