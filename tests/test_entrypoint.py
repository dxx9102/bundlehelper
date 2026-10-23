from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_entrypoint_can_run_without_package_context() -> None:
    root = Path(__file__).resolve().parents[1]
    entry = root / "src" / "bundlehelper" / "__main__.py"
    script = (
        "import bundlehelper.ui as ui\n"
        "ui.main = lambda: 17\n"
        "import runpy\n"
        f"runpy.run_path({str(entry)!r}, run_name='__main__')\n"
    )
    env = os.environ.copy()
    old_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(root / "src") + (os.pathsep + old_path if old_path else "")

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 17, result.stdout + result.stderr