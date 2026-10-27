import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Plugin manager popup opened from BarWidget.qml. All data comes from
// Service.qml (injected as `service`); this file renders it and forwards
// actions. Anything destructive (remove, restore, delete backup) asks first.
//
// Keys: h/l switch tab · / search · j/k move · Enter details or confirm
//       d disable/enable · x remove · s sync · r rescan · Esc back or close
Panel {
  id: root
  moduleName: "fenrirrhogar.omavst"
  ipcTarget: "fenrirrhogar.omavst"
  manageIpc: false

  property var anchorItem: null
  property var service: null

  // The bar tracks the widget in its slot (BarWidget.qml), not this nested
  // panel, so popout coordination and panel switching use that identity.
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property bool online: service ? service.online : false
  readonly property var inventory: service ? service.plugins : null
  readonly property var rows: inventory && inventory.plugins ? inventory.plugins : []
  readonly property string busyAction: service ? service.busyAction : ""
  readonly property bool jobRunning: service ? service.jobRunning : false
  readonly property bool idle: online && busyAction === "" && !jobRunning
  readonly property var settingsState: inventory && inventory.settings ? inventory.settings : ({})
  readonly property var crashState: inventory && inventory.crashWatch ? inventory.crashWatch : ({})
  readonly property var snapshotState: service && service.snapshots ? service.snapshots : null

  property string tab: "plugins"
  property string query: ""
  readonly property var visibleRows: rows.filter(function(p) { return Model.matches(p, root.query) })

  property int cursor: -1
  property string confirmId: ""         // plugin id awaiting "Move to Trash?"
  property string expandedId: ""        // plugin whose details are shown
  property var detailsCache: ({})       // id -> details from the daemon
  property string confirmSnapshot: ""   // "restore:<name>" or "delete:<name>"

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function toggle() { root.opened ? root.close() : root.open() }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  // Poll faster only while visible; balanced on every open/close transition.
  property bool retained: false
  onOpenedChanged: {
    updateRetain()
    if (!opened) {
      confirmId = ""
      confirmSnapshot = ""
    }
  }
  onServiceChanged: updateRetain()
  function updateRetain() {
    var want = root.opened && root.service !== null
    if (want === retained) return
    if (want) root.service.retain()
    else if (root.service) root.service.release()
    retained = want
  }

  // Keep the cursor valid when the list changes under it.
  onVisibleRowsChanged: {
    if (cursor >= visibleRows.length) cursor = visibleRows.length - 1
    if (confirmId !== "" && !visibleRows.some(function(p) { return p.id === confirmId })) confirmId = ""
  }

  // Details can change only through an action or job; refetch after those,
  // not on every poll (reading a large plugin binary isn't free).
  onBusyActionChanged: if (busyAction === "") resetDetails()
  onJobRunningChanged: if (!jobRunning) resetDetails()
  function resetDetails() {
    detailsCache = ({})
    if (expandedId !== "") loadDetails(expandedId)
  }

  function current() { return cursor >= 0 && cursor < visibleRows.length ? visibleRows[cursor] : null }
  function isBusy(p) { return busyAction !== "" && busyAction.indexOf(":" + p.id) !== -1 }

  function refresh() { if (service) service.refresh(true) }
  function sync() { if (service) service.syncYabridge() }

  function toggleDisabled(p) {
    if (!service || !p) return
    confirmId = ""
    service.pluginAction(p.status === "disabled" ? "enable" : "disable", p.id)
  }

  function askRemove(p) { if (p) confirmId = p.id }

  function confirmRemove() {
    if (!service || confirmId === "") return
    service.pluginAction("remove", confirmId)
    confirmId = ""
  }

  function loadDetails(id) {
    if (!service) return
    service.details(id, function(d) {
      var next = {}
      for (var k in root.detailsCache) next[k] = root.detailsCache[k]
      next[id] = d || { missing: false, path: "details unavailable" }
      root.detailsCache = next
    })
  }

  function toggleDetails(p) {
    if (!p) return
    if (expandedId === p.id) {
      expandedId = ""
      return
    }
    expandedId = p.id
    if (!detailsCache[p.id]) loadDetails(p.id)
  }

  function chooseInstaller() {
    if (!service) return
    root.close()  // the picker would open underneath this overlay
    service.pickInstaller()
  }

  function runSnapshotAction() {
    if (!service || confirmSnapshot === "") return
    var parts = confirmSnapshot.split(":")
    var action = parts.shift()
    var name = parts.join(":")
    if (action === "restore") service.restore(name)
    else service.deleteSnapshot(name)
    confirmSnapshot = ""
  }

  function moveCursor(dy) {
    if (tab !== "plugins" || visibleRows.length === 0) return
    confirmId = ""
    cursor = cursor < 0 ? 0 : Math.max(0, Math.min(visibleRows.length - 1, cursor + dy))
    var item = rowRepeater.itemAt(cursor)
    if (!item) return
    var y = item.mapToItem(scroll.contentItem, 0, 0).y
    if (y < scroll.contentY) scroll.contentY = y
    else if (y + item.height > scroll.contentY + scroll.height)
      scroll.contentY = y + item.height - scroll.height
  }

  function daemonText() {
    if (!service) return "Service not loaded. Enable the plugin with: omarchy plugin enable " + moduleName
    var mode = String(service.daemonMode)
    if (!online) return "Daemon " + mode + (service.lastError ? ": " + service.lastError : "")
    var who = mode === "external" ? "running externally (e.g. systemd)" : "running (started by the shell)"
    var updated = service.lastUpdated ? Qt.formatTime(service.lastUpdated, "HH:mm:ss") : "never"
    return "Daemon " + who + " · updated " + updated
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(480))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: searchField.activeFocus
      onCloseRequested: {
        if (root.confirmId !== "") root.confirmId = ""
        else if (root.confirmSnapshot !== "") root.confirmSnapshot = ""
        else if (root.expandedId !== "") root.expandedId = ""
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onMoveRequested: function(dx, dy) {
        if (dx !== 0) root.tab = dx > 0 ? "tools" : "plugins"
        else root.moveCursor(dy)
      }
      onActivateRequested: {
        if (root.confirmId !== "") root.confirmRemove()
        else if (root.confirmSnapshot !== "") root.runSnapshotAction()
        else if (root.tab === "plugins") root.toggleDetails(root.current())
      }
      onDeleteRequested: if (root.tab === "plugins") root.askRemove(root.current())
      onTextKey: function(t) {
        if (t === "r") root.refresh()
        else if (t === "s") root.sync()
        else if (t === "d" && root.tab === "plugins") root.toggleDisabled(root.current())
        else if (t === "/") {
          root.tab = "plugins"
          searchField.forceActiveFocus()
        }
      }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        ColumnLayout {
          id: column
          width: scroll.width
          spacing: Style.space(6)

          // ---- header
          RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(6)
            spacing: Style.space(8)

            Text {
              textFormat: Text.PlainText
              text: root.online ? Model.GLYPH_OK : Model.GLYPH_OFFLINE
              color: Model.needsAttention(root.inventory) ? root.urgent : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.heading
            }

            ColumnLayout {
              Layout.fillWidth: true
              spacing: 0

              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: "Plugins"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }

              Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: root.online && root.inventory ? Model.summary(root.inventory) : "Waiting for daemon"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            PanelActionButton {
              iconText: Model.GLYPH_REFRESH
              tooltipText: "Rescan (r)"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: root.service !== null
              onClicked: root.refresh()
            }

            ActionButton {
              label: root.busyAction === "sync" ? "Syncing…" : "Sync yabridge"
              iconText: Model.GLYPH_SYNC
              enabled: root.idle
              onClicked: root.sync()
            }
          }

          ButtonGroup {
            Layout.leftMargin: Style.space(6)
            options: [{ value: "plugins", label: "Plugins" }, { value: "tools", label: "Install & backups" }]
            value: root.tab
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            focusable: false
            onChanged: function(v) { root.tab = v }
          }

          // ---- status lines shared by both tabs
          InfoLine {
            text: Model.attentionText(root.inventory)
            color: root.urgent
          }
          InfoLine {
            text: root.service ? Model.jobText(root.service.job) : ""
            color: root.service && root.service.job && root.service.job.state === "failed" ? root.urgent
              : (root.jobRunning ? root.foreground : root.dim)
          }
          InfoLine {
            text: root.service ? Model.resultText(root.service.actionResult) : ""
            color: root.service && root.service.actionResult && !root.service.actionResult.ok ? root.urgent : root.dim
          }
          InfoLine {
            text: root.service ? Model.resultNote(root.service.actionResult) : ""
            color: root.foreground
          }

          PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }

          // ================= Plugins tab
          TextField {
            id: searchField
            visible: root.tab === "plugins"
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(6)
            Layout.rightMargin: Style.space(6)
            placeholderText: "Search plugins or vendors  ( / )"
            foreground: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            onTextChanged: {
              root.query = text
              root.cursor = text === "" ? -1 : 0
            }
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) {
                searchField.text = ""
                keyCatcher.forceActiveFocus()
                event.accepted = true
              } else if (event.key === Qt.Key_Down || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                keyCatcher.forceActiveFocus()
                root.cursor = 0
                event.accepted = true
              }
            }
          }

          Repeater {
            id: rowRepeater
            model: root.tab === "plugins" ? root.visibleRows : []

            PluginRow {
              required property var modelData
              required property int index
              plugin: modelData
              rowIndex: index
            }
          }

          InfoLine {
            visible: root.tab === "plugins" && root.online && root.visibleRows.length === 0
            text: root.query !== "" ? "No plugins match \"" + root.query + "\"" : "No plugins found"
            color: root.dim
          }

          // ================= Install & backups tab
          SectionTitle { visible: root.tab === "tools"; text: "INSTALL A PLUGIN" }

          InfoLine {
            visible: root.tab === "tools"
            text: "Runs a Windows installer (.exe or .msi) in your Wine prefix, then syncs yabridge and lists what was added."
            color: root.dim
          }

          RowLayout {
            visible: root.tab === "tools"
            Layout.leftMargin: Style.space(6)
            spacing: Style.space(8)

            ActionButton {
              label: root.service && root.service.job && root.service.job.kind === "install" && root.jobRunning ? "Installing…" : "Choose installer…"
              iconText: Model.GLYPH_INSTALL
              enabled: root.idle
              onClicked: root.chooseInstaller()
            }
          }

          Toggle {
            visible: root.tab === "tools"
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(6)
            Layout.rightMargin: Style.space(6)
            label: "Back up the Wine prefix before installing"
            description: "Safer, but adds a few minutes to each install."
            checked: root.settingsState.backupBeforeInstall === true
            foreground: root.foreground
            fontFamily: root.fontFamily
            titleSize: Style.font.bodySmall
            onClicked: if (root.idle) root.service.setSetting("backupBeforeInstall", !checked)
          }

          SectionTitle { visible: root.tab === "tools"; text: "AUTOMATION" }

          Toggle {
            visible: root.tab === "tools"
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(6)
            Layout.rightMargin: Style.space(6)
            label: "Auto-sync new Windows plugins"
            description: "Runs yabridgectl sync on its own when a new plugin appears in a watched folder."
            checked: root.settingsState.autoSync === true
            foreground: root.foreground
            fontFamily: root.fontFamily
            titleSize: Style.font.bodySmall
            onClicked: if (root.idle) root.service.setSetting("autoSync", !checked)
          }

          Toggle {
            visible: root.tab === "tools"
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(6)
            Layout.rightMargin: Style.space(6)
            label: "Crash watch"
            description: root.crashState.blocked
              ? "You already have your own REAPER launcher entry, so this can't be switched on automatically."
              : "Logs yabridge output when REAPER is started from the app launcher, and flags plugins whose Wine host crashes."
            checked: root.crashState.enabled === true
            enabled: !root.crashState.blocked
            foreground: root.foreground
            fontFamily: root.fontFamily
            titleSize: Style.font.bodySmall
            onClicked: if (root.idle) root.service.crashWatch(checked ? "disable" : "enable")
          }

          RowLayout {
            visible: root.tab === "tools" && (root.crashState.logSize || 0) > 0
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(6)
            spacing: Style.space(8)

            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              text: "Crash log: " + Model.formatSize(root.crashState.logSize)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            ActionButton {
              label: "Clear log"
              enabled: root.idle && root.inventory && !root.inventory.reaperRunning
              onClicked: root.service.crashWatch("clear")
            }
          }

          SectionTitle { visible: root.tab === "tools"; text: "WINE PREFIX BACKUPS" }

          RowLayout {
            visible: root.tab === "tools"
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(6)
            Layout.rightMargin: Style.space(6)
            spacing: Style.space(8)

            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              text: root.snapshotState ? root.snapshotState.prefix + " → " + root.snapshotState.dir : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WrapAnywhere
            }

            ActionButton {
              label: root.service && root.service.job && root.service.job.kind === "backup" && root.jobRunning ? "Backing up…" : "Back up now"
              iconText: Model.GLYPH_BACKUP
              enabled: root.idle
              onClicked: root.service.backup()
            }
          }

          InfoLine {
            visible: root.tab === "tools"
            text: "REAPER must be closed. Wine's background services (iLok, UA Connect) are stopped for the copy and start again by themselves."
            color: root.dim
          }

          Repeater {
            model: root.tab === "tools" && root.snapshotState ? root.snapshotState.snapshots : []

            SnapshotRow {
              required property var modelData
              snapshot: modelData
            }
          }

          InfoLine {
            visible: root.tab === "tools" && root.snapshotState !== null && root.snapshotState.snapshots.length === 0
            text: "No backups yet"
            color: root.dim
          }

          PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }

          // ---- footer
          InfoLine {
            text: root.daemonText()
            color: root.service && !root.online ? root.urgent : root.dim
          }

          InfoLine {
            text: root.tab === "plugins"
              ? "/ search · j/k move · Enter details · d disable · x remove · s sync · l next tab · esc close"
              : "h previous tab · s sync · r rescan · esc close"
            color: root.dim
          }
        }
      }
    }
  }

  // One wrapped caption line, hidden when empty.
  component InfoLine: Text {
    textFormat: Text.PlainText
    Layout.fillWidth: true
    Layout.leftMargin: Style.space(6)
    Layout.rightMargin: Style.space(6)
    visible: text !== ""
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    wrapMode: Text.Wrap
  }

  component SectionTitle: PanelSectionHeader {
    Layout.leftMargin: Style.space(6)
    Layout.topMargin: Style.space(4)
    foreground: root.foreground
    fontFamily: root.fontFamily
  }

  // One plugin. Actions show on the row under the cursor; a pending removal
  // swaps them for an explicit Move to Trash / Cancel pair. Clicking the row
  // (or Enter) shows details.
  component PluginRow: CursorSurface {
    id: row
    property var plugin: ({})
    property int rowIndex: 0
    readonly property bool disabledPlugin: plugin.status === "disabled"
    readonly property bool confirming: root.confirmId === plugin.id
    readonly property bool expanded: root.expandedId === plugin.id
    readonly property bool busy: root.isBusy(plugin)
    readonly property bool showActions: (hasCursor || confirming) && !busy

    Layout.fillWidth: true
    foreground: root.foreground
    hasCursor: root.cursor === rowIndex
    implicitHeight: rowColumn.implicitHeight + Style.space(8)

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: if (root.confirmId === "") root.cursor = row.rowIndex
      onClicked: root.toggleDetails(row.plugin)
    }

    ColumnLayout {
      id: rowColumn
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(2)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(8)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: Model.title(row.plugin) + (row.plugin.vendor ? "  · " + row.plugin.vendor : "")
          color: row.disabledPlugin ? root.dim : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.strikeout: row.confirming
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          visible: (row.plugin.crashes || 0) > 0
          text: Model.GLYPH_CRASH + " " + row.plugin.crashes + "×"
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Text {
          textFormat: Text.PlainText
          visible: text !== ""
          text: row.busy ? "working…" : Model.statusLabel(row.plugin)
          color: row.plugin.status === "unsynced" ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Text {
          textFormat: Text.PlainText
          visible: !row.showActions
          text: String(row.plugin.format || "") + " · " + Model.sourceLabel(row.plugin)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        PanelActionButton {
          visible: row.showActions && !row.confirming
          iconText: row.disabledPlugin ? Model.GLYPH_ENABLE : Model.GLYPH_DISABLE
          tooltipText: row.disabledPlugin ? "Enable (d)" : "Disable (d)"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.body
          enabled: root.idle
          onClicked: root.toggleDisabled(row.plugin)
        }

        PanelActionButton {
          visible: row.showActions && !row.confirming
          iconText: Model.GLYPH_TRASH
          tooltipText: "Remove (x)"
          foreground: root.urgent
          fontFamily: root.fontFamily
          fontSize: Style.font.body
          enabled: root.idle
          onClicked: root.askRemove(row.plugin)
        }

        ActionButton {
          visible: row.confirming
          label: "Move to Trash"
          iconText: Model.GLYPH_TRASH
          labelColor: root.urgent
          onClicked: root.confirmRemove()
        }

        ActionButton {
          visible: row.confirming
          label: "Cancel"
          onClicked: root.confirmId = ""
        }
      }

      Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        visible: row.confirming
        text: Model.removeWarning(row.plugin)
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.Wrap
      }

      Repeater {
        model: row.expanded && !row.confirming ? Model.detailLines(row.plugin, root.detailsCache[row.plugin.id]) : []

        Text {
          required property var modelData
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: modelData
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WrapAnywhere
        }
      }
    }
  }

  // One backup with Restore / Delete, both confirmed inline.
  component SnapshotRow: CursorSurface {
    id: snap
    property var snapshot: ({})
    readonly property string pending: root.confirmSnapshot.indexOf(":" + snapshot.name) !== -1
      ? root.confirmSnapshot.split(":")[0] : ""

    Layout.fillWidth: true
    foreground: root.foreground
    hasCursor: snapMouse.containsMouse
    implicitHeight: snapColumn.implicitHeight + Style.space(8)

    MouseArea {
      id: snapMouse
      anchors.fill: parent
      hoverEnabled: true
    }

    ColumnLayout {
      id: snapColumn
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(2)

      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(8)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: Model.formatDate(snap.snapshot.created) + (snap.snapshot.label ? "  · " + snap.snapshot.label : "")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          visible: snap.pending === ""
          text: Model.formatSize(snap.snapshot.size)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        ActionButton {
          visible: snap.pending === "" && snap.hasCursor
          label: "Restore"
          enabled: root.idle
          onClicked: root.confirmSnapshot = "restore:" + snap.snapshot.name
        }

        ActionButton {
          visible: snap.pending === "" && snap.hasCursor
          label: "Delete"
          labelColor: root.urgent
          enabled: root.idle
          onClicked: root.confirmSnapshot = "delete:" + snap.snapshot.name
        }

        ActionButton {
          visible: snap.pending !== ""
          label: snap.pending === "restore" ? "Restore this backup" : "Delete this backup"
          labelColor: root.urgent
          onClicked: root.runSnapshotAction()
        }

        ActionButton {
          visible: snap.pending !== ""
          label: "Cancel"
          onClicked: root.confirmSnapshot = ""
        }
      }

      Text {
        textFormat: Text.PlainText
        Layout.fillWidth: true
        visible: snap.pending !== ""
        text: snap.pending === "restore"
          ? "Replaces " + (snap.snapshot.prefix || "the prefix") + " with this backup. The current prefix is kept next to it as .before-restore-…, not deleted."
          : "Deletes the backup file permanently."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.Wrap
      }
    }
  }

  // Labelled button; PanelActionButton is icon-only.
  component ActionButton: CursorSurface {
    id: actionButton
    property string label: ""
    property string iconText: ""
    property color labelColor: root.foreground
    signal clicked()

    foreground: root.foreground
    hasCursor: buttonMouse.containsMouse && enabled
    bordered: true
    opacity: enabled ? 1.0 : 0.5
    implicitWidth: buttonRow.implicitWidth + Style.space(16)
    implicitHeight: buttonRow.implicitHeight + Style.space(8)

    Row {
      id: buttonRow
      anchors.centerIn: parent
      spacing: Style.space(6)

      Text {
        textFormat: Text.PlainText
        visible: text !== ""
        text: actionButton.iconText
        color: actionButton.labelColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      Text {
        textFormat: Text.PlainText
        text: actionButton.label
        color: actionButton.labelColor
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
    }

    MouseArea {
      id: buttonMouse
      anchors.fill: parent
      hoverEnabled: true
      enabled: actionButton.enabled
      cursorShape: Qt.PointingHandCursor
      onClicked: actionButton.clicked()
    }
  }
}
