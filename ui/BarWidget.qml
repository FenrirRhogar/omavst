import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Bar icon for the plugin manager; highlights when plugins need a sync.
// Click opens Panel.qml.
// Follows the first-party Weather layout: the panel is a hidden Loader child,
// and this root forwards opened/open/close so shell summon/hide/toggle routes
// (which look for those on the bar-widget root) reach it.
BarWidget {
  id: root
  moduleName: "fenrirrhogar.omavst"

  // Found through the host facade; it can appear after this widget does
  // (service start order isn't guaranteed), so look again until it exists.
  property var service: null

  function findService() {
    if (service) return
    var host = bar ? bar.shell : null
    if (host && typeof host.serviceFor === "function") {
      try { service = host.serviceFor(moduleName) } catch (e) { service = null }
    }
    if (service) injectPanel()
  }

  readonly property bool online: service ? service.online : false
  readonly property var inventory: service ? service.plugins : null
  readonly property bool attention: online && Model.needsAttention(inventory)
  readonly property string tooltip: {
    if (!service) return "Plugins: service not running"
    if (!online) return "Plugins: daemon offline"
    return attention ? Model.attentionText(inventory) : "Plugins: " + Model.summary(inventory)
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
    if ("service" in target) target.service = root.service
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  function toggle() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  // Lets this widget stand in for the panel as the bar's popout identity.
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: { findService(); injectPanel() }
  onSettingsChanged: injectPanel()
  Component.onCompleted: findService()

  Timer {
    interval: 2000
    running: root.service === null
    repeat: true
    onTriggered: root.findService()
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.online ? Model.GLYPH_OK : Model.GLYPH_OFFLINE
    slotSize: Style.bar.statusSlot
    active: root.attention
    dimmed: !root.online
    tooltipText: root.opened ? "" : root.tooltip

    onPressed: function(b) {
      if (b === Qt.MiddleButton) {
        if (root.service) root.service.refresh(true)
      } else {
        root.toggle()
      }
    }
  }
}
