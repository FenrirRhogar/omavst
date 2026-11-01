"""Shared helpers for the OmaVST backend.

Every probe in this package returns plain dicts that serialise straight to
JSON. Failures never raise out of a probe: they are reported as an
``{"ok": false, "error": {...}}`` payload so the UI can show *why* something
is unavailable instead of an empty widget.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PLUGIN_ROOT / "config" / "defaults.json"
USER_CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "omavst"
    / "config.json"
)

# Everything OmaVST stores for the user. Nothing here is meant for other local
# accounts, and default umasks would leave it readable, so both are kept 0700.
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "omavst"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "omavst"

# yabridgectl colours its output when it thinks it is on a terminal; strip any
# escape sequences defensively so parsers only ever see plain text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def private_dir(path: Path) -> Path:
    """Create ``path`` as 0700, or tighten it to 0700. Refuses a directory
    owned by someone else. Follows symlinks, so it may live on another disk."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.stat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PermissionError(f"{path} is not a directory owned by this user")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    """Atomically replace ``path``. The temporary file is created with ``mode``
    from the start, so the content is never briefly readable by others."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)  # undo the umask for modes like 0644
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Build the standard error object embedded in probe results."""
    return {"code": code, "message": message, **extra}


class CommandError(Exception):
    """Raised by :func:`run_command`; carries a JSON-ready error object."""

    def __init__(self, err: dict[str, Any]):
        super().__init__(err["message"])
        self.err = err


def run_command(argv: list[str], timeout: float = 10.0) -> str:
    """Run ``argv`` and return stdout, or raise :class:`CommandError`.

    Distinguishes "not installed", "timed out" and "non-zero exit" so the UI
    can tell the user what to fix.
    """
    exe = shutil.which(argv[0])
    if exe is None:
        raise CommandError(error("not_installed", f"'{argv[0]}' was not found on PATH", command=argv[0]))
    env = dict(os.environ, NO_COLOR="1", LC_ALL="C")
    try:
        proc = subprocess.run(
            [exe, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise CommandError(error("timeout", f"'{argv[0]}' did not finish within {timeout:g}s", command=argv[0]))
    except OSError as exc:
        raise CommandError(error("exec_failed", f"could not run '{argv[0]}': {exc}", command=argv[0]))
    if proc.returncode != 0:
        raise CommandError(
            error(
                "command_failed",
                f"'{' '.join(argv)}' exited with status {proc.returncode}",
                command=argv[0],
                returncode=proc.returncode,
                stderr=strip_ansi(proc.stderr).strip()[-2000:],
            )
        )
    return strip_ansi(proc.stdout)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load defaults, then layer the user's config (or ``path``) on top.

    A broken user config is ignored rather than fatal; the daemon must still
    start so the UI can report the problem.
    """
    config = json.loads(DEFAULT_CONFIG.read_text())
    user_path = path or USER_CONFIG
    if user_path.is_file():
        try:
            config = _deep_merge(config, json.loads(user_path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            config["configError"] = error("bad_config", f"ignored {user_path}: {exc}")
    return config
