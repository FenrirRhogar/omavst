"""Per-session API token.

The daemon listens on 127.0.0.1, which every local user can reach, so a fixed
header is no protection: another account on the machine could ask it to run
an installer or remove plugins as the desktop user. Instead the daemon makes
a random token at startup and writes it to a file only the desktop user can
read ($XDG_RUNTIME_DIR is mode 0700). Every request except /ping must carry
it in the ``X-OmaVST-Token`` header. The token changes on every daemon start
and the file is removed on exit.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

try:
    from .common import private_dir, write_private
except ImportError:  # executed as a script
    from common import private_dir, write_private

HEADER = "X-OmaVST-Token"


def token_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path.home() / ".local" / "state"
    return base / "omavst"


def token_path() -> Path:
    return token_dir() / "token"


def create() -> str:
    """Make a fresh token and publish it (atomically, mode 0600)."""
    token = secrets.token_hex(32)
    private_dir(token_dir())
    write_private(token_path(), token)
    return token


def remove(token: str) -> None:
    """Delete the token file, but only if it is still ours (another daemon
    instance may have replaced it)."""
    try:
        if hmac.compare_digest(token_path().read_text().strip(), token):
            token_path().unlink()
    except OSError:
        pass


def check(expected: str, presented: str | None) -> bool:
    return presented is not None and hmac.compare_digest(expected.encode(), presented.strip().encode())
