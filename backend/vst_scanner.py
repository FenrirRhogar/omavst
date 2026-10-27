"""Plugin inventory: yabridge (Windows plugins) plus native Linux plugins.

``yabridgectl status`` has no machine-readable mode, so this module parses its
human output. The format (yabridgectl 5.x) looks like::

    yabridge path: <auto>
    VST2 location: '/home/u/.vst/yabridge'
    VST3 location: '/home/u/.vst3/yabridge'

    libyabridge-chainloader-vst3.so: '/home/u/.local/share/yabridge/...' (64-bit)

    yabridge-host.exe: '/home/u/.local/share/yabridge/yabridge-host.exe'

    /home/u/.wine/drive_c/Program Files/Common Files/VST3/
      Foo.vst3 :: VST3, bundle, 64-bit, synced
      Vendor/Bar.vst3 :: VST3, legacy, 64-bit, not yet synced

The parser is deliberately tolerant: unknown header lines are kept verbatim
and unknown plugin attributes land in ``extra`` instead of being dropped.

Run directly for a JSON dump:  ``python3 backend/vst_scanner.py``
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from .common import CommandError, error, load_config, run_command
except ImportError:  # executed as a script
    from common import CommandError, error, load_config, run_command

_FORMATS = {"VST2", "VST3", "CLAP"}
_KINDS = {"bundle", "legacy"}
_ARCH_RE = re.compile(r"^(32|64)-bit$")
# "name: 'value' (64-bit)" / "name: <auto>" / "name: value"
_HEADER_RE = re.compile(r"^(?P<key>[^:]+):\s*(?P<value>.*?)\s*(?:\((?P<arch>(?:32|64)-bit)\))?$")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _parse_plugin_line(line: str, directory: str) -> dict[str, Any]:
    """Parse ``  Rel/Path.vst3 :: VST3, bundle, 64-bit, synced``."""
    rel, _, attrs = line.strip().partition(" :: ")
    plugin: dict[str, Any] = {
        "name": Path(rel).stem,
        "relativePath": rel,
        "path": os.path.join(directory, rel),
        "format": None,
        "kind": None,
        "arch": None,
        "status": "unknown",
        "synced": False,
    }
    fields = [f.strip() for f in attrs.split(",") if f.strip()]
    extra = []
    for i, field in enumerate(fields):
        if field in _FORMATS:
            plugin["format"] = field
        elif field in _KINDS:
            plugin["kind"] = field
        elif _ARCH_RE.match(field):
            plugin["arch"] = field
        elif i == len(fields) - 1:
            # Status is always the trailing field ("synced", "not yet synced", ...).
            plugin["status"] = field
            plugin["synced"] = field == "synced"
        else:
            extra.append(field)
    if extra:
        plugin["extra"] = extra
    return plugin


def _prefix_for(directory: str) -> str | None:
    """Wine prefix that owns a watched directory, e.g. ``~/.wine``."""
    head, sep, _ = directory.partition("/drive_c/")
    return head if sep else None


def parse_status(text: str) -> dict[str, Any]:
    """Turn ``yabridgectl status`` output into a structured dict."""
    settings: dict[str, Any] = {}
    install_dirs: dict[str, str] = {}
    chainloaders: dict[str, Any] = {}
    hosts: dict[str, str] = {}
    directories: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith((" ", "\t")):
            if current is None:
                continue  # indented line outside a directory block: nothing to attach it to
            if " :: " in raw:
                current["plugins"].append(_parse_plugin_line(raw, current["path"]))
            else:
                current.setdefault("notes", []).append(raw.strip())
            continue
        if raw.startswith("/"):
            path = raw.rstrip()
            current = {
                "path": path.rstrip("/") or "/",
                "exists": os.path.isdir(path),
                "prefix": _prefix_for(path),
                "plugins": [],
            }
            directories.append(current)
            continue

        current = None
        match = _HEADER_RE.match(raw.strip())
        if not match:
            settings.setdefault("unparsed", []).append(raw.strip())
            continue
        key, value, arch = match["key"].strip(), _unquote(match["value"]), match["arch"]
        if key.endswith(" location"):
            install_dirs[key.split()[0]] = value
        elif key.startswith("libyabridge"):
            chainloaders[key] = {"path": value, "arch": arch, "found": value.startswith("/")}
        elif key.endswith(".exe"):
            hosts[key] = value
        else:
            settings[key] = value

    plugins = [p for d in directories for p in d["plugins"]]
    unsynced = [p["relativePath"] for p in plugins if not p["synced"]]
    for d in directories:
        d["pluginCount"] = len(d["plugins"])
        d["syncedCount"] = sum(p["synced"] for p in d["plugins"])

    return {
        "settings": settings,
        "installDirs": install_dirs,
        "chainloaders": chainloaders,
        "hosts": hosts,
        "watchedDirectories": directories,
        "prefixes": sorted({d["prefix"] for d in directories if d["prefix"]}),
        "sync": {
            "total": len(plugins),
            "synced": len(plugins) - len(unsynced),
            "unsynced": unsynced,
            "inSync": not unsynced,
        },
    }


def yabridge_status(timeout: float = 10.0) -> dict[str, Any]:
    """Run and parse ``yabridgectl status``; never raises."""
    try:
        text = run_command(["yabridgectl", "status"], timeout=timeout)
    except CommandError as exc:
        return {"ok": False, "error": exc.err}
    try:
        version = run_command(["yabridgectl", "--version"], timeout=timeout).split()[-1]
    except (CommandError, IndexError):
        version = None
    try:
        parsed = parse_status(text)
    except Exception as exc:  # a format change must not take the daemon down
        return {"ok": False, "error": error("parse_failed", f"could not parse yabridgectl status: {exc}")}
    return {"ok": True, "version": version, **parsed}


def yabridge_sync(timeout: float = 120.0) -> dict[str, Any]:
    """Run ``yabridgectl sync``; never raises.

    Never pass ``--prune``: it deletes *every* unmanaged file in the yabridge
    install dirs, including native plugins a user dropped there by mistake.
    """
    try:
        output = run_command(["yabridgectl", "sync"], timeout=timeout)
    except CommandError as exc:
        return {"ok": False, "error": exc.err}
    return {"ok": True, "output": output.strip()}


# Native plugin bundles are identified by suffix; ".so" only counts as a VST2
# plugin when it sits under a VST2 directory.
_NATIVE_SUFFIXES = {".vst3": "VST3", ".clap": "CLAP", ".lv2": "LV2"}


def native_plugins(scan_dirs: list[str], ignore_subdirs: list[str]) -> dict[str, Any]:
    """List native Linux plugins, skipping yabridge's bridged copies."""
    plugins: list[dict[str, Any]] = []
    scanned: list[dict[str, Any]] = []
    for raw_dir in scan_dirs:
        root = Path(os.path.expanduser(raw_dir))
        entry = {"path": str(root), "exists": root.is_dir()}
        scanned.append(entry)
        if not entry["exists"]:
            continue
        is_vst2_dir = root.name == ".vst"
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                here = Path(dirpath)
                if here == root:
                    dirnames[:] = [d for d in dirnames if d not in ignore_subdirs]
                keep = []
                for d in dirnames:
                    fmt = _NATIVE_SUFFIXES.get(Path(d).suffix)
                    if fmt:  # a bundle: record it, don't descend
                        plugins.append({"name": Path(d).stem, "format": fmt, "path": str(here / d)})
                    else:
                        keep.append(d)
                dirnames[:] = keep
                for f in filenames:
                    suffix = Path(f).suffix
                    fmt = "VST2" if (suffix == ".so" and is_vst2_dir) else _NATIVE_SUFFIXES.get(suffix)
                    if fmt:
                        plugins.append({"name": Path(f).stem, "format": fmt, "path": str(here / f)})
        except OSError as exc:
            entry["error"] = error("scan_failed", str(exc))
    plugins.sort(key=lambda p: (p["format"], p["name"].lower()))
    return {"ok": True, "scannedDirectories": scanned, "plugins": plugins, "count": len(plugins)}


def scan(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full inventory: yabridge status plus native plugins."""
    config = config or load_config()
    native_cfg = config.get("native", {})
    return {
        "yabridge": yabridge_status(timeout=config.get("commands", {}).get("timeoutSeconds", 10)),
        "native": native_plugins(native_cfg.get("scanDirs", []), native_cfg.get("ignoreSubdirs", [])),
    }


if __name__ == "__main__":
    print(json.dumps(scan(), indent=2))
