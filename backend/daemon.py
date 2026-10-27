#!/usr/bin/env python3
"""OmaVST backend daemon.

Serves the probe results as JSON over a loopback-only HTTP server so the
Quickshell UI (running inside omarchy-shell) can poll it with a ``Process``
running curl, the same way other Omarchy plugins talk to local daemons.

Endpoints (GET, JSON)::

    /api/v1/ping                liveness + version
    /api/v1/plugins             plugin list + job, settings, crash watch, REAPER state
    /api/v1/plugins/details?id= size, version, vendor for one plugin
    /api/v1/snapshots           Wine prefix backups

Actions (POST, require the ``X-OmaVST: 1`` header)::

    /api/v1/yabridge/sync       run ``yabridgectl sync``
    /api/v1/plugins/disable     {"id": ...}
    /api/v1/plugins/enable      {"id": ...}
    /api/v1/plugins/remove      {"id": ...}  (moves to Trash)
    /api/v1/install             {"path": installer}  background job
    /api/v1/wine/backup         {}                   background job
    /api/v1/wine/restore        {"name": snapshot}   background job
    /api/v1/wine/delete         {"name": snapshot}
    /api/v1/settings            {"autoSync": bool, "backupBeforeInstall": bool}
    /api/v1/crashwatch/enable | disable | clear

Quick actions answer with their result plus a fresh ``plugins`` payload.
Background jobs answer at once; progress shows up as ``job`` in /plugins.
Only one action or job runs at a time.

Append ``?refresh=1`` to bypass the cache. Results are cached for
``daemon.cacheSeconds`` because ``yabridgectl status`` forks a process and
the bar may poll every few seconds.

Usage::

    python3 backend/daemon.py                # serve on config host/port
    python3 backend/daemon.py --port 47811
    python3 backend/daemon.py --once plugins  # print one result and exit
    python3 backend/daemon.py --exit-with-parent  # used by ui/Service.qml

Only the standard library is used, so no venv is needed.
"""

from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import logging
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crash_watch  # noqa: E402
import plugin_manager  # noqa: E402
import reaper  # noqa: E402
import vst_scanner  # noqa: E402
import wine_tools  # noqa: E402
from common import PLUGIN_ROOT, load_config  # noqa: E402

log = logging.getLogger("omavst")


def _plugin_version() -> str | None:
    try:
        return json.loads((PLUGIN_ROOT / "manifest.json").read_text()).get("version")
    except (OSError, json.JSONDecodeError):
        return None


VERSION = _plugin_version()


class ProbeCache:
    """Time-based cache; one lock per key so a slow probe doesn't block others
    and concurrent requests for the same key run the probe only once."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str, probe: Callable[[], dict[str, Any]], refresh: bool = False) -> dict[str, Any]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            cached = self._entries.get(key)
            now = time.time()
            if cached and not refresh and now - cached[0] < self.ttl:
                return cached[1]
            result = probe()
            result["generatedAt"] = now
            self._entries[key] = (now, result)
            return result

    def invalidate(self, key: str) -> None:
        with self._guard:
            self._entries.pop(key, None)


SETTINGS_FILE = plugin_manager.DATA_DIR / "settings.json"
DEFAULT_SETTINGS = {"autoSync": True, "backupBeforeInstall": False}
AUTOSYNC_SECONDS = 20


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


class Backend:
    """Owns config, cache, settings and the single action/job slot."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.cache = ProbeCache(config["daemon"].get("cacheSeconds", 5))
        self.started = time.time()
        # One lock for everything that changes files: two clicks (or two
        # monitors' panels, or auto-sync) must never overlap.
        self._lock = threading.Lock()
        self.job: dict[str, Any] | None = None
        self.settings = self._load_settings()
        self._autosync_tried: frozenset[str] = frozenset()

    # ---- settings

    def _load_settings(self) -> dict[str, Any]:
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            return {k: bool(saved.get(k, v)) for k, v in DEFAULT_SETTINGS.items()}
        except (OSError, ValueError):
            return dict(DEFAULT_SETTINGS)

    def set_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        for k in DEFAULT_SETTINGS:
            if isinstance(body.get(k), bool):
                self.settings[k] = body[k]
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2))
        self.cache.invalidate("plugins")
        return {"ok": True, "plugins": self.plugins()}

    # ---- reads

    def prefix(self) -> str:
        inv = self.cache.get("plugins", self._inventory)
        prefixes = inv.get("yabridge", {}).get("prefixes") or []
        return prefixes[0] if prefixes else str(Path.home() / ".wine")

    def _inventory(self) -> dict[str, Any]:
        return plugin_manager.inventory(self.config)

    def plugins(self, refresh: bool = False, query: dict[str, str] | None = None) -> dict[str, Any]:
        inv = dict(self.cache.get("plugins", self._inventory, refresh))
        # Live state goes on top of the cached scan so it is never stale.
        inv["job"] = self.job
        inv["settings"] = self.settings
        inv["crashWatch"] = crash_watch.status()
        inv["reaperRunning"] = reaper.is_running()
        return inv

    def details(self, refresh: bool = False, query: dict[str, str] | None = None) -> dict[str, Any]:
        plugin_id = (query or {}).get("id", "")
        return plugin_manager.details(self.config, plugin_id) if plugin_id else _error("bad_request", "missing id")

    def snapshots(self, refresh: bool = False, query: dict[str, str] | None = None) -> dict[str, Any]:
        d = wine_tools.snapshot_dir(self.config)
        return {"ok": True, "dir": str(d), "prefix": self.prefix(), "snapshots": wine_tools.list_snapshots(self.config)}

    def ping(self, refresh: bool = False, query: dict[str, str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "version": VERSION, "uptime": round(time.time() - self.started, 1)}
        if "configError" in self.config:
            result["configError"] = self.config["configError"]
        return result

    def route(self, name: str) -> Callable[..., dict[str, Any]] | None:
        return {"ping": self.ping, "plugins": self.plugins, "plugins/details": self.details,
                "snapshots": self.snapshots}.get(name)

    # ---- quick actions

    def _locked(self, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return _error("busy", "another action is still running")
        try:
            result = work()
        finally:
            self.cache.invalidate("plugins")
            self._lock.release()
        result["plugins"] = self.plugins()
        return result

    def _sync(self) -> dict[str, Any]:
        timeout = max(120, self.config.get("commands", {}).get("timeoutSeconds", 10))
        result = vst_scanner.yabridge_sync(timeout=timeout)
        if result.get("ok"):
            result.update(reaper.startup_note())
        return result

    def yabridge_sync(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._locked(self._sync)

    def plugin_action(self, name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def run(body: dict[str, Any]) -> dict[str, Any]:
            plugin_id = body.get("id")
            if not isinstance(plugin_id, str) or not plugin_id:
                return _error("bad_request", "missing plugin id")
            return self._locked(lambda: plugin_manager.run_action(self.config, name, plugin_id))
        return run

    def delete_snapshot(self, body: dict[str, Any]) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            try:
                return wine_tools.delete_snapshot(self.config, str(body.get("name", "")))
            except wine_tools.ToolError as exc:
                return _error(exc.code, str(exc))
        return self._locked(work)

    def crash_action(self, name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        fn = {"enable": crash_watch.enable, "disable": crash_watch.disable, "clear": crash_watch.clear_log}[name]
        return lambda body: self._locked(fn)

    # ---- background jobs

    def _start_job(self, kind: str, label: str, work: Callable[[Callable[[str], None]], dict[str, Any]]) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return _error("busy", "another action is still running")
        self.job = {"kind": kind, "label": label, "state": "running", "message": label, "startedAt": time.time()}

        def progress(message: str) -> None:
            self.job = {**self.job, "message": message}

        def run() -> None:
            try:
                result = work(progress)
                ok = result.get("ok", True)
                message = result.get("message") or (result.get("error") or {}).get("message", "")
            except wine_tools.ToolError as exc:
                ok, message = False, str(exc)
            except Exception as exc:  # a job must always finish with a state
                log.exception("job %s failed", kind)
                ok, message = False, f"{kind} failed: {exc}"
            finally:
                self.cache.invalidate("plugins")
                self._lock.release()
            self.job = {**self.job, "state": "done" if ok else "failed", "message": message, "finishedAt": time.time()}
            log.info("job %s: %s", kind, message)

        threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
        return {"ok": True, "job": self.job}

    def backup(self, body: dict[str, Any]) -> dict[str, Any]:
        prefix = self.prefix()
        return self._start_job("backup", f"Backing up {prefix}",
                               lambda progress: wine_tools.create_snapshot(self.config, prefix, "manual", progress))

    def restore(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name", ""))
        return self._start_job("restore", f"Restoring {name}",
                               lambda progress: wine_tools.restore_snapshot(self.config, name, progress))

    def install(self, body: dict[str, Any]) -> dict[str, Any]:
        installer = body.get("path")
        if not isinstance(installer, str) or not installer:
            return _error("bad_request", "missing installer path")
        prefix = self.prefix()
        backup_first = self.settings["backupBeforeInstall"]

        def work(progress: Callable[[str], None]) -> dict[str, Any]:
            if backup_first:
                wine_tools.create_snapshot(self.config, prefix, f"before {Path(installer).name}", progress)
            before = {p["id"] for p in self._inventory()["plugins"]}
            ran = wine_tools.run_installer(prefix, installer, progress)
            progress("Installer closed; syncing yabridge…")
            synced = self._sync()
            after = self._inventory()["plugins"]
            new = [p["name"] for p in after if p["id"] not in before]
            if not synced.get("ok"):
                return _error("sync_failed", "Installer finished, but yabridge sync failed: "
                              + synced["error"]["message"])
            if new:
                note = synced.get("reaperNote", "REAPER will pick them up the next time it starts")
                return {"ok": True, "message": f"Installed {', '.join(new)}. {note}"}
            hint = f" (installer exit code {ran['exitCode']})" if ran["exitCode"] else ""
            return {"ok": True, "message": "Installer finished but no new plugins showed up" + hint
                    + ". If it installed somewhere else, add that folder with yabridgectl add."}

        return self._start_job("install", f"Installing {Path(installer).name}", work)

    def action(self, path: str) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
        table: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "yabridge/sync": self.yabridge_sync,
            "install": self.install,
            "wine/backup": self.backup,
            "wine/restore": self.restore,
            "wine/delete": self.delete_snapshot,
            "settings": self.set_settings,
        }
        for name in plugin_manager.ACTIONS:
            table["plugins/" + name] = self.plugin_action(name)
        for name in ("enable", "disable", "clear"):
            table["crashwatch/" + name] = self.crash_action(name)
        return table.get(path)

    # ---- auto-sync

    def autosync_loop(self, stop: threading.Event) -> None:
        """Sync when new unsynced Windows plugins appear. A set that failed to
        sync isn't retried until it changes, so a broken plugin can't loop."""
        while not stop.wait(AUTOSYNC_SECONDS):
            if not self.settings["autoSync"] or self._lock.locked():
                continue
            try:
                inv = self.cache.get("plugins", self._inventory, True)
                pending = frozenset(p["id"] for p in inv["plugins"] if p["status"] == "unsynced")
                if not pending or pending == self._autosync_tried:
                    if not pending:
                        self._autosync_tried = frozenset()
                    continue
                self._autosync_tried = pending
                log.info("auto-sync: %d new plugin(s)", len(pending))
                self._locked(self._sync)
            except Exception:
                log.exception("auto-sync failed")


def make_handler(backend: Backend) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"OmaVST/{VERSION}"

        def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            url = urlsplit(self.path)
            path = url.path.strip("/")
            probe = backend.route(path[len("api/v1/"):]) if path.startswith("api/v1/") else None
            if probe is None:
                self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": {"code": "not_found", "message": url.path}})
                return
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            refresh = qs.get("refresh", "0") in ("1", "true")
            try:
                self._send(HTTPStatus.OK, probe(refresh, qs))
            except Exception as exc:  # probes shouldn't raise, but never drop the connection
                log.exception("probe %s failed", path)
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"ok": False, "error": {"code": "internal", "message": str(exc)}})

        def do_POST(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            path = url.path.strip("/")
            action = backend.action(path[len("api/v1/"):]) if path.startswith("api/v1/") else None
            if action is None:
                self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": {"code": "not_found", "message": url.path}})
                return
            # A web page can fire simple cross-origin POSTs at loopback. Browsers
            # always send Origin on those and cannot add a custom header without
            # a CORS preflight (which we never answer), so both checks block it.
            if self.headers.get("Origin") or self.headers.get("X-OmaVST") != "1":
                self._send(HTTPStatus.FORBIDDEN, {"ok": False, "error": {"code": "forbidden", "message": "missing client header"}})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 65536:
                    raise ValueError("request body too large")
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("request body must be a JSON object")
            except ValueError as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": {"code": "bad_request", "message": str(exc)}})
                return
            try:
                self._send(HTTPStatus.OK, action(body))
            except Exception as exc:
                log.exception("action %s failed", path)
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"ok": False, "error": {"code": "internal", "message": str(exc)}})

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _exit_with_parent() -> None:
    """Ask the kernel to SIGTERM us when the spawning process dies.

    Quickshell stops the child on a clean shell exit, but a crashed shell
    would otherwise leave an orphan holding the port.
    """
    PR_SET_PDEATHSIG = 1
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
            log.warning("prctl(PR_SET_PDEATHSIG) failed: errno %s", ctypes.get_errno())
    except (OSError, AttributeError) as exc:
        log.warning("cannot tie lifetime to parent: %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, help="user config JSON (default: ~/.config/omavst/config.json)")
    parser.add_argument("--host", help="bind address (loopback only)")
    parser.add_argument("--port", type=int, help="bind port")
    parser.add_argument("--once", choices=["ping", "plugins"],
                        help="print one result as JSON and exit instead of serving")
    parser.add_argument("--exit-with-parent", action="store_true",
                        help="terminate when the parent process exits (for the Omarchy service)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.exit_with_parent:
        _exit_with_parent()

    config = load_config(args.config)
    if "configError" in config:
        log.warning(config["configError"]["message"])
    backend = Backend(config)

    if args.once:
        json.dump(backend.route(args.once)(True), sys.stdout, indent=2)
        print()
        return 0

    host = args.host or config["daemon"].get("host", "127.0.0.1")
    port = args.port or config["daemon"].get("port", 47811)
    # The API exposes local system details and will later trigger commands;
    # it must never listen on a network interface.
    if not _is_loopback(host):
        log.error("refusing to bind to non-loopback address %s", host)
        return 2

    try:
        server = ThreadingHTTPServer((host, port), make_handler(backend))
    except OSError as exc:
        log.error("cannot listen on %s:%s: %s (is another instance running?)", host, port, exc)
        return 1
    server.daemon_threads = True

    def stop(signum: int, _frame: Any) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    stop_event = threading.Event()
    threading.Thread(target=backend.autosync_loop, args=(stop_event,), name="autosync", daemon=True).start()

    log.info("OmaVST %s listening on http://%s:%s/api/v1/", VERSION, host, port)
    server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
