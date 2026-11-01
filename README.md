# OmaVST

A plugin manager for Linux music producers, built as an
[Omarchy](https://omarchy.org) shell plugin. Windows plugins bridged with
[yabridge](https://github.com/robbert-vdh/yabridge) and native Linux plugins
(VST3, CLAP, LV2, VST2) appear in one list in a bar panel, where you can:

- **search** by name or vendor, and open a plugin's **details** (vendor,
  version, size, install path)
- see which Windows plugins still need a yabridge sync (the bar icon
  highlights when any do), sync with one click, or let **auto-sync** do it
- **disable** a plugin so your DAW stops seeing it, and **enable** it again
- **remove** a plugin by moving it to the Trash, with a confirmation step first
- **install** a plugin from a Windows installer (.exe or .msi): it runs in
  your Wine prefix, then yabridge syncs and the panel lists what was added
- **back up and restore** the Wine prefix
- turn on **crash watch** to see which bridged plugins crash their Wine host
- keep REAPER's plugin list tidy: disabled and removed plugins are dropped
  from REAPER's plugin cache

## Layout

```
manifest.json        Omarchy plugin manifest (bar-widget + service)
ui/
  Service.qml        headless: owns the daemon, polls it, shares the data
  BarWidget.qml      bar icon; highlights when plugins need a sync
  Panel.qml          the plugin list and its actions
  Model.js           formatting helpers
backend/
  daemon.py          loopback JSON HTTP server
  plugin_manager.py  unified list, details, disable / enable / remove
  wine_tools.py      prefix backups and installers
  reaper.py          REAPER plugin cache (names, vendors, cleanup)
  crash_watch.py     yabridge crash log and REAPER launcher entry
  vst_scanner.py     yabridgectl status parser + native plugin scan
  auth.py            per-session API token
  common.py          command runner, config loading, error format
systemd/             optional user unit for running the daemon standalone
config/defaults.json default settings
```

## Requirements

- Omarchy (omarchy-shell / Quickshell)
- Python 3.10+ (standard library only)
- `gio` (from glib2) for moving plugins to the Trash
- `zenity` for the installer file picker, `tar` with zstd for backups
- Optional: `yabridgectl` for Windows plugins. If it's missing, the panel says
  so and still lists native plugins.

## Installation

```sh
omarchy plugin add https://github.com/FenrirRhogar/omavst.git
omarchy plugin enable fenrirrhogar.omavst
```

The icon lands in the right section of the bar. Click it (or run
`omarchy-shell shell toggle fenrirrhogar.omavst`) to open the
panel. Middle-click rescans.

## Using the panel

The panel has two tabs: **Plugins** and **Install & backups** (`h`/`l`
switch between them).

**Plugins.** Type `/` to search by name or vendor. Hover a plugin, or move to
it with `j`/`k`, to show its actions; click it or press `Enter` for details.

| Key / button | Does |
|---|---|
| `d` / 󰒲 | disable the plugin (󰐊 enables it again) |
| `x` / 󰩺 | ask to remove it; `Enter` or **Move to Trash** confirms, `Esc` or **Cancel** backs out |
| `s` / **Sync yabridge** | run `yabridgectl sync` |
| `r` / 󰑐 | rescan |
| `Tab` | next bar panel |
| `Esc` | back out of a confirmation or details, then close |

Versions come from each plugin's own version info. iLok-protected plugins
whose files are fully wrapped by PACE (Archetype Gojira X, for example)
show no version, because the file only carries PACE's.

**Install & backups.**

- **Choose installer…** opens a file picker, then runs the installer in your
  Wine prefix. Finish the installer in its own window; when it closes, the
  panel syncs yabridge and names the plugins that appeared. Turn on *Back up
  the Wine prefix before installing* to take a backup first.
- **Auto-sync** (on by default) runs `yabridgectl sync` by itself within
  about 20 seconds of a new Windows plugin appearing in a watched folder. If a
  sync doesn't fix a plugin, it isn't retried until something changes.
- **Crash watch** installs a copy of REAPER's launcher entry in
  `~/.local/share/applications/` that sets `YABRIDGE_DEBUG_FILE`, so yabridge
  logs to `~/.local/state/omavst/yabridge.log`. Plugins whose
  Wine host died get a 󰈸 count in the list. It only applies when REAPER is
  started from the app launcher. Turning it off removes the entry again; an
  entry you made yourself is never touched.
- **Wine prefix backups** are `tar.zst` archives in
  `~/.local/share/omavst/snapshots/` (set `snapshots.dir` in
  the config to put them elsewhere). REAPER must be closed. The prefix's
  background services (iLok/PACE, UA Connect, ...) are stopped for the copy
  and start again the next time Wine is used. **Restore** never deletes your
  current prefix: it is renamed to `.wine.before-restore-<time>` first, so you
  can switch back by renaming. Delete that copy yourself once all is well.

### REAPER

REAPER has no action that rescans plugins (only F5 inside its FX browser), so
the panel can't make a running REAPER refresh. What it does instead:

- New and updated plugins: REAPER finds them by itself at its next start
  ("Scan for new/updated VST plug-ins on startup" is on by default).
- Disabled and removed plugins: their entries are deleted from REAPER's
  plugin cache (`reaper-vstplugins64.ini`), so REAPER forgets them.
- While REAPER is open it rewrites that cache on exit, so the panel leaves it
  alone and tells you to press F5 in the FX browser instead.

### What disable and remove actually do

**Native plugins**

- *Disable* moves the plugin to
  `~/.local/share/omavst/disabled/`. *Enable* moves it back.
- *Remove* moves it to the Trash, so you can restore it from there.

**Windows plugins**

- *Disable* adds the plugin to yabridge's blacklist
  (`yabridgectl blacklist add`), then deletes the bridged copy in
  `~/.vst3/yabridge`. That copy is only yabridge's loader plus a link, and a
  sync recreates it. The plugin installed in Wine is not touched.
- *Enable* takes the plugin off the blacklist and runs a sync.
- *Remove* moves the plugin file inside the Wine prefix to the Trash and
  deletes the bridged copy. Presets, licences and registry entries stay, so the
  vendor's own uninstaller is still the cleaner way to uninstall completely.

Disabled plugins are recorded in
`~/.local/share/omavst/disabled.json`. Blacklisted plugins
vanish from `yabridgectl status`, so without this file they would disappear
from the list.

The panel only ever sends a plugin's ID. The daemon looks the plugin up in its
own fresh scan and refuses to touch anything outside your plugin folders.

## How the daemon runs

You don't start anything by hand. The plugin's `service` entry point
(`ui/Service.qml`, kept loaded across plugin reloads) manages the daemon:

1. It pings `/api/v1/ping`. If a daemon already answers, it uses that one.
2. Otherwise it starts `backend/daemon.py` as a child of the shell. The daemon
   asks the kernel to stop it if the shell dies, so no orphan keeps the port.
3. If the daemon crashes, it is restarted with backoff (1 s up to 30 s).
4. If the port answers with something that isn't this daemon, the panel shows
   a port conflict instead of fighting over it.

Disabling the plugin stops the daemon. After editing any file in `ui/`, run
`omarchy restart shell`. The service is never hot-reloaded, and the panel
sometimes keeps the old code after a hot reload.

### Optional: systemd user unit

```sh
mkdir -p ~/.config/systemd/user
cp systemd/omavst.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now omavst.service
```

The shell service detects it and doesn't start a second daemon. Installers
open windows, so the daemon needs your session's display: run
`systemctl --user import-environment WAYLAND_DISPLAY DISPLAY` once per login
if you use the unit.

## Backend API

```sh
python3 backend/daemon.py                 # http://127.0.0.1:47811/api/v1/
python3 backend/daemon.py --once plugins  # one-shot JSON dump, no server
```

| Endpoint | Does |
|---|---|
| `GET /api/v1/ping` | liveness, plugin version, uptime |
| `GET /api/v1/plugins` | `plugins`, `counts`, `job`, `settings`, `crashWatch`, `reaperRunning`, raw scans |
| `GET /api/v1/plugins/details?id=` | vendor, version, size, paths for one plugin |
| `GET /api/v1/snapshots` | Wine prefix backups |
| `POST /api/v1/yabridge/sync` | run `yabridgectl sync` |
| `POST /api/v1/plugins/{disable,enable,remove}` | body `{"id": "..."}` |
| `POST /api/v1/install` | body `{"path": "..."}`, background job |
| `POST /api/v1/wine/backup` | background job |
| `POST /api/v1/wine/restore` | body `{"name": "..."}`, background job |
| `POST /api/v1/wine/delete` | body `{"name": "..."}` |
| `POST /api/v1/settings` | body `{"autoSync": bool, "backupBeforeInstall": bool}` |
| `POST /api/v1/crashwatch/{enable,disable,clear}` | |

Quick actions return their result plus a fresh `plugins` payload. Background
jobs return at once and report progress in `job`. Only one action or job runs
at a time.

**Authentication.** Every endpoint except `/ping` needs the header
`X-OmaVST-Token: <token>`, where the token is the contents of
`$XDG_RUNTIME_DIR/omavst/token`:

```sh
printf 'header = "X-OmaVST-Token: %s"\n' "$(cat "$XDG_RUNTIME_DIR/omavst/token")" |
  curl -K - http://127.0.0.1:47811/api/v1/plugins
```

Pass the token to curl on standard input like this, never as a command-line
argument such as `curl -H "X-OmaVST-Token: ..."`: other local users can
usually read a running program's arguments. `printf` is built into the shell,
so it doesn't expose the token either.

The daemon makes a new random token each time it starts, writes it with mode
0600 in your runtime folder (mode 0700, so only you can read it) and deletes
it on exit. Any request carrying an `Origin` header is refused, so browser
pages can't use the API either.

`GET` results are cached for `daemon.cacheSeconds` (default 5 s); add
`?refresh=1` to force a rescan. The daemon binds to loopback only.

### Configuration

Defaults live in `config/defaults.json`. Override any key in
`~/.config/omavst/config.json`, for example:

```json
{ "daemon": { "port": 47900 }, "native": { "scanDirs": ["~/.vst3", "~/plugins"] } }
```

## What it runs, and permissions

Omarchy plugins run unsandboxed with your user permissions. The plugin starts
one long-running process, `python3 backend/daemon.py`, listening on 127.0.0.1
only. Nothing uses sudo.

Other accounts on the same machine can connect to 127.0.0.1 too, so the
daemon doesn't trust the connection itself. Every request except `/ping`
must carry the per-session token, which only you can read (see
[Authentication](#backend-api)). As a second layer, the installer action only
runs files that you own and that no one else can write, in a folder no one
else can swap files in.

Everything OmaVST stores is private to you, whatever your umask:
`~/.local/share/omavst/` and `~/.local/state/omavst/` are mode 0700, and
existing ones are tightened when the daemon starts. That covers backups,
disabled plugins, settings and the crash log. Backup archives hold your whole
Wine prefix, including licence data, so they are mode 0600 even if you point
`snapshots.dir` at a shared folder, and OmaVST only lists or restores backups
you own.

- **Reading:** `yabridgectl status`, `yabridgectl --version`, your plugin
  folders, REAPER's plugin cache and, with crash watch on, the yabridge log.
- **Automatically:** `yabridgectl sync` when auto-sync is on and new
  Windows plugins appear.
- **Only when you click an action:**
  - `yabridgectl sync`. It never passes `--prune`, because that deletes every
    unmanaged file in the yabridge folders, including native plugins put there
    by mistake.
  - `yabridgectl blacklist add/rm`
  - moving plugin files to `~/.local/share/omavst/disabled/`
    and back
  - `gio trash`
  - deleting bridged copies under the yabridge install folders
  - editing REAPER's plugin cache, only while REAPER is closed
  - `zenity` (file picker), then `wine <installer>` or `wine msiexec /i`
  - `wineserver -k`, then `tar` for backups and restores
  - writing or removing `~/.local/share/applications/cockos-reaper.desktop`
    for crash watch

Nothing uses sudo.

## License

MIT. See [LICENSE](LICENSE).
