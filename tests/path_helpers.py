"""Path helpers for shell-interop tests."""
from __future__ import annotations

import os
from pathlib import Path


def bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)
    drive = resolved.drive.rstrip(":").lower()
    if not drive:
        return str(resolved)
    return f"/mnt/{drive}{resolved.as_posix()[2:]}"
