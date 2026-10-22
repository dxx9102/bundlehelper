from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .errors import CancelledError, CommandError
from .logging_service import sanitize_text


@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner:
    def __init__(self, poll_interval: float = 0.15):
        self.poll_interval = poll_interval

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        on_output: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
        check: bool = True,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        args = [os.fspath(value) for value in argv]
        merged_env = os.environ.copy()
        merged_env.update({str(key): str(value) for key, value in (env or {}).items()})
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                args,
                cwd=os.fspath(cwd) if cwd else None,
                env=merged_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise CommandError(f"无法启动命令 {args[0]}：{exc}", command=" ".join(args)) from exc

        chunks: list[str] = []
        reader_error: list[BaseException] = []

        def read_output() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    clean = sanitize_text(line.rstrip("\r\n"), secrets)
                    chunks.append(clean)
                    if on_output:
                        on_output(clean)
            except BaseException as exc:  # pragma: no cover - defensive thread guard
                reader_error.append(exc)

        reader = threading.Thread(target=read_output, name="command-output", daemon=True)
        reader.start()
        cancelled = False
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            try:
                process.wait(timeout=self.poll_interval)
            except subprocess.TimeoutExpired:
                continue
        reader.join(timeout=5)
        returncode = process.wait()
        output = "\n".join(chunks)
        if reader_error:
            raise CommandError(f"读取命令输出失败：{reader_error[0]}", command=" ".join(args))
        if cancelled:
            raise CancelledError("操作已取消")
        result = CommandResult(args, returncode, output)
        if check and returncode != 0:
            tail = output[-4000:].strip()
            message = f"命令执行失败（退出码 {returncode}）"
            if tail:
                message += f"\n{tail}"
            raise CommandError(message, command=" ".join(args), returncode=returncode)
        return result


def require_executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise CommandError(f"未找到 {name}，请先安装并加入 PATH")
    return path


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def posix_quote(value: str) -> str:
    return shlex.quote(value)


def ensure_directory_writable(path: str | Path, label: str) -> Path:
    directory = Path(path).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix=".bundlehelper-write-test-",
            delete=True,
        ):
            pass
    except OSError as exc:
        raise CommandError(f"{label}不可写：{directory}（{exc}）") from exc
    return directory.resolve()


def safe_remove_dir(allowed_root: str | Path, target: str | Path, expected_name: str) -> bool:
    root = Path(allowed_root).expanduser().resolve()
    candidate = Path(target).expanduser().resolve()
    if candidate == root or root not in candidate.parents:
        raise CommandError(f"拒绝删除不在配置目录内的路径：{candidate}")
    if candidate.name != expected_name:
        raise CommandError(f"拒绝删除目录名不符合预期的路径：{candidate}")
    if not candidate.exists():
        return False
    if not candidate.is_dir():
        raise CommandError(f"预期是目录，但实际不是：{candidate}")
    shutil.rmtree(candidate)
    return True