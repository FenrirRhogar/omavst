"""REAPER integration: plugin cache reading and cleanup.

REAPER has no action that rescans plugins (only F5 inside its FX browser), so
OSC can't trigger one. What we can do reliably:

* New or updated plugins: REAPER picks them up by itself at startup
  ("Scan for new/updated VST plug-ins on startup", on by default).
* Disabled or removed plugins: delete their entries from REAPER's plugin
  cache (reaper-vstplugins64.ini), so REAPER forgets them instead of keeping a
  stale entry. Only while REAPER is closed: it rewrites the cache on exit.

The cache also gives us each plugin's display name and vendor, e.g.
``ABPL.vst3=<filetime>,<uid>{<guid>,Ample Bass P Lite (Ample Sound)!!!VSTi``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    from .common import CommandError, run_command
except ImportError:  # executed as a script
    from common import CommandError, run_command

REAPER_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "REAPER"
CACHE_FILES = ["reaper-vstplugins64.ini", "reaper-clap-linux-x86_64.ini"]


def is_running() -> bool:
    try:
        run_command(["pgrep", "-x", "reaper"], timeout=5)
        return True
    except CommandError:
        return False  # pgrep exits 1 when nothing matches


def split_vendor(label: str) -> tuple[str, str | None]:
    """'Name (Vendor)' -> ('Name', 'Vendor'), keeping nested brackets in the
    vendor intact: 'PolyMAX Synth (Universal Audio (UADx))'."""
    label = label.strip()
    if not label.endswith(")"):
        return label, None
    depth = 0
    for i in range(len(label) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(label[i], 0)
        if depth == 0:
            name = label[:i].strip()
            return (name, label[i + 1:-1].strip()) if name else (label, None)
    return label, None


def cache_key(path: str) -> str:
    """REAPER keys its cache by file name with spaces turned into underscores."""
    return Path(path).name.replace(" ", "_")


def read_cache(reaper_dir: Path = REAPER_DIR) -> dict[str, dict[str, Any]]:
    """Map cache key -> {displayName, vendor, instrument} for every cached plugin."""
    entries: dict[str, dict[str, Any]] = {}
    for name in CACHE_FILES:
        try:
            lines = (reaper_dir / name).read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.partition("=")
            if not sep or key.startswith("["):
                continue
            fields = value.split(",", 2)
            if len(fields) < 3 or not fields[2]:
                continue  # failed scans have no name
            label = fields[2]
            instrument = label.endswith("!!!VSTi")
            label = label.removesuffix("!!!VSTi")
            display, vendor = split_vendor(label)
            entries[key] = {
                "displayName": display,
                "vendor": vendor,
                "instrument": instrument,
            }
    return entries


def forget(paths: list[str], reaper_dir: Path = REAPER_DIR) -> dict[str, Any]:
    """Drop cache entries for ``paths``. Returns a note for the UI."""
    if is_running():
        return {"reaperNote": "REAPER is open: press F5 in its FX browser to refresh the plugin list"}
    keys = {cache_key(p) for p in paths}
    removed = 0
    for name in CACHE_FILES:
        f = reaper_dir / name
        try:
            lines = f.read_text(errors="replace").splitlines(keepends=True)
        except OSError:
            continue
        kept = [ln for ln in lines if ln.partition("=")[0] not in keys]
        if len(kept) != len(lines):
            tmp = f.with_suffix(f.suffix + ".omavst-tmp")
            tmp.write_text("".join(kept))
            tmp.replace(f)
            removed += len(lines) - len(kept)
    return {"reaperForgot": removed}


def startup_note() -> dict[str, Any]:
    """After adding plugins: REAPER only scans on startup, or on F5 if open."""
    if is_running():
        return {"reaperNote": "REAPER is open: press F5 in its FX browser to see new plugins"}
    return {}
