"""Crash watch: find out which bridged plugins crash their Wine host.

yabridge writes its messages (and Wine's output) to the file named by
``YABRIDGE_DEBUG_FILE`` when that variable is set in the DAW's environment.
Turning crash watch on installs a user copy of REAPER's menu entry that sets
it; turning it off removes that copy again (only if we wrote it). It takes
effect the next time REAPER is started from the app launcher.

Every yabridge line carries a ``[<plugin>-<id>]`` tag for the plugin instance
that wrote it. When a host dies, yabridge prints "The Wine host process has
exited unexpectedly"; the tag on that line tells us which plugin it was.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

try:
    from .common import STATE_DIR, private_dir
except ImportError:  # executed as a script
    from common import STATE_DIR, private_dir

LOG_FILE = STATE_DIR / "yabridge.log"
APPS_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "applications"
SYSTEM_ENTRY = Path("/usr/share/applications/cockos-reaper.desktop")
USER_ENTRY = APPS_DIR / "cockos-reaper.desktop"
MARKER = "X-OmaVST=crash-watch"

CRASH_TEXT = "The Wine host process has exited unexpectedly"
# Optional timestamp, then the instance tag: "12:34:56 [Archetype Gojira X-a1b2c3] ..."
_TAG_RE = re.compile(r"^(?:\S*\d\d:\d\d:\d\d\S*\s+)?\[(?P<tag>[^\]]+)\]\s?(?P<msg>.*)$")
READ_LIMIT = 8 * 1024 * 1024  # only the newest 8 MB of the log is parsed


def _is_ours(path: Path) -> bool:
    try:
        return MARKER in path.read_text(errors="replace")
    except OSError:
        return False


def status() -> dict[str, Any]:
    enabled = _is_ours(USER_ENTRY)
    blocked = USER_ENTRY.exists() and not enabled
    size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    return {
        "enabled": enabled,
        # A REAPER entry the user made themselves: never overwrite it.
        "blocked": blocked,
        "logFile": str(LOG_FILE),
        "logSize": size,
        "crashes": crashes() if enabled or size else {},
    }


def enable() -> dict[str, Any]:
    if USER_ENTRY.exists() and not _is_ours(USER_ENTRY):
        return {"ok": False, "error": {"code": "exists", "message":
                f"{USER_ENTRY} already exists and wasn't made by this plugin; add YABRIDGE_DEBUG_FILE={LOG_FILE} to it by hand"}}
    try:
        base = SYSTEM_ENTRY.read_text()
    except OSError:
        return {"ok": False, "error": {"code": "no_reaper", "message": f"{SYSTEM_ENTRY} not found; is REAPER installed?"}}
    private_dir(STATE_DIR)  # the log carries everything Wine prints
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    for line in base.splitlines():
        if line.startswith("Exec="):
            line = f"Exec=env YABRIDGE_DEBUG_FILE={LOG_FILE} " + line[len("Exec="):]
        lines.append(line)
        if line.strip() == "[Desktop Entry]":
            lines.append(MARKER)
    USER_ENTRY.write_text("\n".join(lines) + "\n")
    return {"ok": True, "message": "Crash watch on. Start REAPER from the app launcher for it to take effect."}


def disable() -> dict[str, Any]:
    if _is_ours(USER_ENTRY):
        USER_ENTRY.unlink()
    return {"ok": True, "message": "Crash watch off (from the next REAPER start)."}


def clear_log() -> dict[str, Any]:
    try:
        LOG_FILE.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "message": "Crash log cleared"}


def _plugin_name(tag: str) -> str:
    # The instance id is the part after the last '-'; plugin names can contain '-'.
    head, sep, _ = tag.rpartition("-")
    return head if sep else tag


def crashes() -> dict[str, dict[str, Any]]:
    """Plugin name -> {count, last} parsed from the newest part of the log."""
    try:
        size = LOG_FILE.stat().st_size
        with LOG_FILE.open("rb") as f:
            if size > READ_LIMIT:
                f.seek(size - READ_LIMIT)
                f.readline()  # drop the partial first line
            text = f.read().decode("utf-8", errors="replace")
        mtime = LOG_FILE.stat().st_mtime
    except OSError:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        if CRASH_TEXT not in line:
            continue
        m = _TAG_RE.match(line)
        name = _plugin_name(m["tag"]) if m else "unknown plugin"
        entry = found.setdefault(name, {"count": 0, "last": None})
        entry["count"] += 1
        # The log has times of day only; the file's mtime is the best date we have.
        entry["last"] = mtime
    return found


if __name__ == "__main__":
    import json
    print(json.dumps(status(), indent=2, default=lambda o: time.ctime(o)))
