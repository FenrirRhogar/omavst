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
import stat
from pathlib import Path

HEADER = "X-OmaVST-Token"


def token_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path.home() / ".local" / "state"
    return base / "omavst"


def token_path() -> Path:
    return token_dir() / "token"


def _private_dir(path: Path) -> None:
    """Create ``path`` as 0700 and refuse one we don't own or others can enter."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PermissionError(f"{path} is not a directory owned by this user")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


def create() -> str:
    """Make a fresh token and publish it (atomically, mode 0600)."""
    token = secrets.token_hex(32)
    d = token_dir()
    _private_dir(d)
    tmp = d / f".token.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    os.replace(tmp, token_path())
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
