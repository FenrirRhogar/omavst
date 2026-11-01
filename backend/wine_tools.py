"""Wine prefix backups and plugin installers.

These are slow (minutes), so the daemon runs them as background jobs; each
function here just does the work and returns a result dict or raises
:class:`ToolError`.

Backups are ``tar --zstd`` archives of the whole prefix, written next to a
small JSON sidecar. A prefix holds licences and logins, so archives and
sidecars are always mode 0600 and the default folder 0700, whatever the umask
or the prefix's own permissions. Only files this user owns are listed or
restored, because a restore trusts the sidecar for where the prefix goes. Restoring never deletes anything: the current prefix is
renamed to ``<prefix>.before-restore-<time>`` first, so a bad restore can be
undone by renaming it back.

Both backup and restore refuse to run while REAPER or a yabridge plugin host
is running. Otherwise they first stop the prefix's background services
(``wineserver -k``): iLok/PACE, UA Connect and similar services keep a
wineserver alive all the time, and a live prefix copies in an inconsistent
state. Wine starts those services again the next time anything uses it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

try:
    from . import reaper
    from .common import DATA_DIR, CommandError, private_dir, run_command, write_private
except ImportError:  # executed as a script
    import reaper
    from common import DATA_DIR, CommandError, private_dir, run_command, write_private

DEFAULT_SNAPSHOT_DIR = DATA_DIR / "snapshots"
_NAME_RE = re.compile(r"^wine-[A-Za-z0-9._-]+-\d{8}-\d{6}\.tar\.zst$")


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def snapshot_dir(config: dict[str, Any]) -> Path:
    raw = config.get("snapshots", {}).get("dir")
    return Path(os.path.expanduser(raw)) if raw else DEFAULT_SNAPSHOT_DIR


def _running(*pgrep_args: str) -> bool:
    try:
        run_command(["pgrep", *pgrep_args], timeout=5)
        return True
    except CommandError:
        return False  # pgrep exits 1 when nothing matches


def _require_wine_idle(prefix: str, what: str, progress: Callable[[str], None]) -> None:
    if reaper.is_running():
        raise ToolError("reaper_running", f"Close REAPER before {what}")
    # Live hosts only: zombies don't count, and "-x" on the 15-char process
    # name skips WebView2 helpers whose arguments merely mention yabridge-host.
    if _running("--runstates", "R,S,D,T", "-x", "yabridge-host.e"):
        raise ToolError("host_running", f"A yabridge plugin host is still running; close the program using it before {what}")
    progress("Stopping Wine's background services…")
    env = dict(os.environ, WINEPREFIX=prefix)
    for args in (["wineserver", "-k"], ["wineserver", "-w"]):
        try:
            subprocess.run(args, env=env, timeout=60, capture_output=True, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            raise ToolError("wineserver", "couldn't stop the Wine prefix's background services")


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


# --- snapshots ---------------------------------------------------------------

def _prepare_snapshot_dir(config: dict[str, Any]) -> Path:
    d = snapshot_dir(config)
    if d == DEFAULT_SNAPSHOT_DIR:
        private_dir(DATA_DIR)
        return private_dir(d)
    # A folder the user chose may be shared on purpose: leave an existing one
    # alone. The files in it are 0600 either way.
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _owned_file(path: Path) -> bool:
    """A regular file (not a symlink) owned by this user; tightened to 0600,
    which also covers backups made by versions before this rule existed."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return False
        if st.st_mode & 0o077:
            os.chmod(path, 0o600)
        return True
    except OSError:
        return False


def _read_sidecar(archive: Path) -> dict[str, Any]:
    sidecar = archive.with_name(archive.name + ".json")
    if not _owned_file(sidecar):
        return {}
    try:
        return json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return {}


def list_snapshots(config: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    d = snapshot_dir(config)
    if not d.is_dir():
        return out
    for f in sorted(d.iterdir(), reverse=True):
        if not _NAME_RE.match(f.name) or not _owned_file(f):
            continue
        meta = _read_sidecar(f)
        st = f.stat()
        out.append({
            "name": f.name,
            "size": st.st_size,
            "created": meta.get("created", st.st_mtime),
            "prefix": meta.get("prefix"),
            "label": meta.get("label", ""),
        })
    return out


def create_snapshot(config: dict[str, Any], prefix: str, label: str = "",
                    progress: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    src = Path(prefix)
    if not (src / "system.reg").is_file():
        raise ToolError("not_prefix", f"{prefix} doesn't look like a Wine prefix")
    _require_wine_idle(prefix, "backing up the Wine prefix", progress)
    dest_dir = _prepare_snapshot_dir(config)

    progress("Measuring the prefix…")
    needed = _dir_size(src)
    free = shutil.disk_usage(dest_dir).free
    if free < needed:  # compressed is smaller, but don't gamble on how much
        raise ToolError("no_space", f"Not enough space in {dest_dir}: need up to {needed >> 20} MB, {free >> 20} MB free")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.name.lstrip(".")) or "prefix"
    dest = dest_dir / f"wine-{safe}-{stamp}.tar.zst"
    tmp = dest.with_name(dest.name + ".partial")
    progress(f"Backing up {src} ({needed >> 20} MB)…")
    proc = subprocess.run(
        ["tar", "--zstd", "-cf", str(tmp), "-C", str(src.parent), src.name],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, umask=0o077,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ToolError("tar_failed", f"tar failed: {proc.stderr.strip()[-500:]}")
    tmp.replace(dest)
    write_private(dest.with_name(dest.name + ".json"), json.dumps(
        {"prefix": str(src), "created": time.time(), "label": label, "sourceBytes": needed}))
    return {"ok": True, "message": f"Backed up {src.name} ({dest.stat().st_size >> 20} MB)", "snapshot": dest.name}


def _snapshot_path(config: dict[str, Any], name: str) -> Path:
    if not _NAME_RE.match(name):
        raise ToolError("bad_name", "not a snapshot name")
    p = snapshot_dir(config) / name
    if not _owned_file(p):
        raise ToolError("not_found", f"snapshot {name} not found")
    return p


def delete_snapshot(config: dict[str, Any], name: str) -> dict[str, Any]:
    p = _snapshot_path(config, name)
    p.unlink()
    p.with_name(p.name + ".json").unlink(missing_ok=True)
    return {"ok": True, "message": f"Deleted backup {name}"}


def restore_snapshot(config: dict[str, Any], name: str,
                     progress: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    archive = _snapshot_path(config, name)
    try:
        prefix = Path(_read_sidecar(archive)["prefix"])
    except (KeyError, TypeError):
        raise ToolError("no_meta", "this backup has no record of which prefix it came from")
    _require_wine_idle(str(prefix), "restoring a backup", progress)

    # Every member must live under the prefix's folder name: no absolute
    # paths, no '..', nothing that would land outside the parent directory.
    progress("Checking the backup…")
    listing = subprocess.run(["tar", "--zstd", "-tf", str(archive)], capture_output=True, text=True)
    if listing.returncode != 0:
        raise ToolError("tar_failed", f"can't read backup: {listing.stderr.strip()[-300:]}")
    for member in listing.stdout.splitlines():
        parts = Path(member).parts
        if member.startswith("/") or ".." in parts or not parts or parts[0] != prefix.name:
            raise ToolError("unsafe_archive", f"backup contains an unexpected path: {member}")

    aside = None
    if prefix.exists():
        aside = prefix.with_name(f"{prefix.name}.before-restore-{time.strftime('%Y%m%d-%H%M%S')}")
        prefix.rename(aside)
    progress(f"Restoring {prefix}…")
    proc = subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(prefix.parent)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        if aside:  # put the original back
            shutil.rmtree(prefix, ignore_errors=True)
            aside.rename(prefix)
        raise ToolError("tar_failed", f"restore failed, original kept: {proc.stderr.strip()[-300:]}")
    note = f" Your previous prefix is at {aside}; delete it once everything works." if aside else ""
    return {"ok": True, "message": f"Restored {prefix.name} from {name}.{note}"}


# --- installers --------------------------------------------------------------

INSTALLER_SUFFIXES = (".exe", ".msi")


def check_installer(installer: str) -> Path:
    """Only run installers the desktop user controls.

    The file (after resolving symlinks) must be a regular file owned by this
    user and not writable by anyone else, in a folder nobody else can swap
    files in. That rules out running a file another account planted, e.g.
    in /tmp, even if a request did reach the daemon.
    """
    path = Path(os.path.realpath(installer))
    if path.suffix.lower() not in INSTALLER_SUFFIXES:
        raise ToolError("bad_installer", "pick a Windows installer (.exe or .msi)")
    try:
        st = os.stat(path)
        parent = os.stat(path.parent)
    except OSError:
        raise ToolError("bad_installer", f"{installer} not found")
    uid = os.getuid()
    if not stat.S_ISREG(st.st_mode):
        raise ToolError("bad_installer", "the installer must be a regular file")
    if st.st_uid != uid or st.st_mode & 0o022:
        raise ToolError("untrusted_installer",
                        f"{path.name} isn't owned by you or can be changed by other users; copy it to your Downloads folder first")
    shared_dir = parent.st_mode & 0o002 and not parent.st_mode & stat.S_ISVTX
    if (parent.st_uid not in (uid, 0)) or (parent.st_mode & 0o020 and parent.st_uid != uid) or shared_dir:
        raise ToolError("untrusted_installer",
                        f"{path.parent} can be changed by other users; move the installer to your Downloads folder first")
    return path


def run_installer(prefix: str, installer: str,
                  progress: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Run a Windows installer in ``prefix`` and wait for it to exit."""
    path = check_installer(installer)
    if not shutil.which("wine"):
        raise ToolError("not_installed", "wine is not installed")
    cmd = ["wine", "msiexec", "/i", str(path)] if path.suffix.lower() == ".msi" else ["wine", str(path)]
    progress(f"Installer open: {path.name}. Finish it in its window.")
    proc = subprocess.run(cmd, cwd=str(path.parent), env=dict(os.environ, WINEPREFIX=prefix),
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return {"exitCode": proc.returncode, "stderrTail": proc.stderr.strip()[-300:]}
