import QtQuick
import Quickshell
import Quickshell.Io

// Headless owner of the backend daemon and the single source of data for the
// bar widget and panel (they find it with bar.shell.serviceFor()).
//
// Lifecycle:
//   1. Ping /api/v1/ping. If a OmaVST daemon answers (for
//      example the optional systemd unit), use it and spawn nothing.
//   2. If nothing listens, spawn backend/daemon.py as a child process. It is
//      started with --exit-with-parent, so even a crashed shell can't leave an
//      orphan holding the port.
//   3. If our child exits, retry with exponential backoff (1 s .. 30 s). Each
//      retry pings first, so an external daemon that appeared meanwhile wins.
//   4. If the port answers but isn't our daemon, stop and report a conflict
//      instead of fighting over it.
//
// The manifest sets keepLoaded, so this instance (and the daemon) survives
// plugin hot-reload. Edits to this file need `omarchy restart shell`.
Item {
  id: root

  visible: false
  width: 0
  height: 0

  // Injected by the host.
  property var shell: null
  property var manifest: null
  property var pluginRegistry: null

  readonly property string pluginDir: {
    var dir = manifest && manifest.__sourceDir ? String(manifest.__sourceDir)
      : decodeURIComponent(Qt.resolvedUrl("..").toString().replace(/^file:\/\//, ""))
    return dir.replace(/\/+$/, "")
  }
  readonly property string configHome: Quickshell.env("XDG_CONFIG_HOME") || (Quickshell.env("HOME") + "/.config")

  // Port comes from the same config files the daemon reads, so a port set in
  // ~/.config/omavst/config.json applies to both sides.
  readonly property int port: {
    var user = parseJson(userConfig.text())
    var defaults = parseJson(defaultConfig.text())
    var p = (user && user.daemon && user.daemon.port) || (defaults && defaults.daemon && defaults.daemon.port)
    return parseInt(p, 10) || 47811
  }
  readonly property string baseUrl: "http://127.0.0.1:" + port + "/api/v1/"

  // ---- public state -------------------------------------------------------

  // starting | owned | external | restarting | conflict | stopped
  property string daemonMode: "starting"
  property bool online: false
  property string lastError: ""
  property var plugins: null       // /api/v1/plugins (plugin_manager.inventory)
  property var lastUpdated: null
  property string busyAction: ""   // "sync" or "<action>:<plugin id>" while running
  property var actionResult: null
  property var snapshots: null     // /api/v1/snapshots

  // Background job (install / backup / restore) as reported by the daemon.
  readonly property var job: plugins ? plugins.job : null
  readonly property bool jobRunning: job !== null && job !== undefined && job.state === "running"
  onJobRunningChanged: if (!jobRunning) { fetchSnapshots(); fetchPlugins(true) }

  // Panels call retain() while visible to poll faster.
  property int activeViews: 0
  function retain() { activeViews++; refresh(true); fetchSnapshots() }
  function release() { activeViews = Math.max(0, activeViews - 1) }

  // ---- internals ----------------------------------------------------------

  property bool stopping: false
  property int crashCount: 0
  property int pollFailures: 0
  property int startupProbes: 0

  function parseJson(text) {
    try { return text ? JSON.parse(text) : null } catch (e) { return null }
  }

  // done(status, body): status 0 means nothing answered (refused / timeout).
  function request(method, path, timeoutMs, done, body) {
    var xhr = new XMLHttpRequest()
    xhr.timeout = timeoutMs
    xhr.onreadystatechange = function() {
      if (xhr.readyState !== XMLHttpRequest.DONE) return
      done(xhr.status, xhr.status === 0 ? null : root.parseJson(xhr.responseText))
    }
    xhr.open(method, root.baseUrl + path)
    if (method === "POST") {
      xhr.setRequestHeader("X-OmaVST", "1")
      xhr.setRequestHeader("Content-Type", "application/json")
    }
    xhr.send(body === undefined ? null : JSON.stringify(body))
  }

  function ensureDaemon() {
    if (stopping) return
    request("GET", "ping", 1500, function(status, body) {
      if (body && body.ok === true && body.version !== undefined) {
        root.online = true
        root.lastError = ""
        root.daemonMode = daemonProc.running ? "owned" : "external"
        root.fetchPlugins(false)
      } else if (status !== 0) {
        // Something else owns the port. Don't spawn a daemon that would just
        // fail to bind; tell the user instead.
        root.online = false
        root.daemonMode = "conflict"
        root.lastError = "port " + root.port + " is used by another program"
      } else if (!daemonProc.running && !restartTimer.running) {
        root.startDaemon()
      }
    })
  }

  function startDaemon() {
    console.log("[omavst] starting daemon on port " + port)
    daemonMode = crashCount > 0 ? "restarting" : "starting"
    daemonProc.command = ["python3", pluginDir + "/backend/daemon.py", "--exit-with-parent", "--port", String(port)]
    daemonProc.running = true
    startupProbes = 0
    startupTimer.restart()
  }

  function fetchPlugins(force) {
    request("GET", "plugins" + (force ? "?refresh=1" : ""), 15000, function(status, body) {
      if (body && body.plugins) {
        root.plugins = body
        root.lastUpdated = new Date()
        root.online = true
        root.pollFailures = 0
        if (daemonProc.running) root.crashCount = 0  // healthy again
        return
      }
      root.online = false
      root.pollFailures++
      root.lastError = status === 0 ? "daemon not responding" : "bad reply from daemon (HTTP " + status + ")"
      if (root.pollFailures >= 2) root.ensureDaemon()
    })
  }

  function refresh(force) {
    if (online) fetchPlugins(force === true)
    else ensureDaemon()
  }

  // Every action answers with its result plus a fresh plugin list.
  function post(path, key, body) {
    if (busyAction !== "" || !online) return
    busyAction = key
    actionResult = null
    request("POST", path, 180000, function(status, reply) {
      root.busyAction = ""
      if (!reply) {
        root.actionResult = { ok: false, error: { message: status === 0 ? "daemon not responding" : "HTTP " + status } }
        return
      }
      // Jobs report through `job`; only quick actions leave a result line.
      if (!reply.job || !reply.ok) root.actionResult = reply
      if (reply.plugins) {
        root.plugins = reply.plugins
        root.lastUpdated = new Date()
      } else {
        root.fetchPlugins(false)
      }
      if (path.indexOf("wine/") === 0) root.fetchSnapshots()
    }, body)
  }

  function syncYabridge() { post("yabridge/sync", "sync", {}) }

  // name: disable | enable | remove
  function pluginAction(name, id) { post("plugins/" + name, name + ":" + id, { id: id }) }

  function install(path) { post("install", "install", { path: path }) }
  function backup() { post("wine/backup", "backup", {}) }
  function restore(name) { post("wine/restore", "restore:" + name, { name: name }) }
  function deleteSnapshot(name) { post("wine/delete", "delete:" + name, { name: name }) }
  function setSetting(key, value) { var b = {}; b[key] = value; post("settings", "settings", b) }
  // action: enable | disable | clear
  function crashWatch(action) { post("crashwatch/" + action, "crashwatch", {}) }

  function fetchSnapshots() {
    if (!online) return
    request("GET", "snapshots", 10000, function(status, body) {
      if (body && body.ok) root.snapshots = body
    })
  }

  // done(details|null); details are computed on demand (size, version).
  function details(id, done) {
    request("GET", "plugins/details?id=" + encodeURIComponent(id), 20000, function(status, body) {
      done(body && body.ok ? body : null)
    })
  }

  // The picker is a separate window, so callers close their panel first
  // (it sits on the overlay layer and would cover the dialog).
  function pickInstaller() {
    if (picker.running || jobRunning) return
    picker.running = true
  }

  Process {
    id: picker
    command: ["zenity", "--file-selection", "--title=Choose a Windows plugin installer",
      "--file-filter=Windows installers (.exe, .msi) | *.exe *.EXE *.msi *.MSI"]
    stdout: StdioCollector {
      id: pickerOut
      onStreamFinished: {
        var path = pickerOut.text.trim()
        if (path !== "") root.install(path)
      }
    }
    onExited: (exitCode, exitStatus) => {
      if (exitCode > 1) root.actionResult = { ok: false, error: { message: "Couldn't open the file picker (is zenity installed?)" } }
    }
  }

  FileView {
    id: defaultConfig
    path: root.pluginDir + "/config/defaults.json"
    blockLoading: true
    printErrors: false
  }

  FileView {
    id: userConfig
    path: root.configHome + "/omavst/config.json"
    blockLoading: true
    printErrors: false
    watchChanges: true
    onFileChanged: reload()
  }

  // A changed port only matters for a daemon we own: restart it there.
  onPortChanged: {
    if (daemonProc.running) daemonProc.running = false
    else Qt.callLater(ensureDaemon)
  }

  Process {
    id: daemonProc
    running: false

    stderr: SplitParser {
      onRead: data => console.log("[omavst] " + data)
    }

    onExited: (exitCode, exitStatus) => {
      if (root.stopping) return
      root.online = false
      root.crashCount++
      var delay = Math.min(30000, 1000 * Math.pow(2, root.crashCount - 1))
      root.daemonMode = "restarting"
      root.lastError = "daemon exited (code " + exitCode + "), retrying in " + Math.round(delay / 1000) + " s"
      console.warn("[omavst] " + root.lastError)
      restartTimer.interval = delay
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: root.ensureDaemon()
  }

  // Poll ping until a freshly spawned daemon is listening (max ~10 s).
  Timer {
    id: startupTimer
    interval: 500
    repeat: true
    onTriggered: {
      if (root.online || !daemonProc.running || ++root.startupProbes > 20) {
        stop()
        return
      }
      root.ensureDaemon()
    }
  }

  Timer {
    interval: root.jobRunning ? 1500 : (root.activeViews > 0 ? 3000 : 15000)
    running: !root.stopping && root.daemonMode !== "conflict"
    repeat: true
    onTriggered: root.refresh(false)
  }

  // A conflict may clear (the other program quits); re-check slowly.
  Timer {
    interval: 30000
    running: root.daemonMode === "conflict"
    repeat: true
    onTriggered: root.ensureDaemon()
  }

  Component.onCompleted: Qt.callLater(ensureDaemon)

  Component.onDestruction: {
    stopping = true
    daemonProc.running = false  // SIGTERM; the daemon shuts down cleanly
  }
}
