import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Bar widget: a bolt glyph, red when a run failed, an automation waits for
// approval, or the daemon is not running. Clicking opens the automations list.
Panel {
  id: root

  moduleName: "omamation"
  ipcTarget: "omamation"

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool vertical: bar ? bar.vertical : false

  readonly property string iconBolt: ""

  readonly property int barContentWidth: Style.bar.iconFont
  readonly property int barSlot: barContentWidth + Style.space(10)
  readonly property real openPanelIndicatorWidth: barContentWidth
  readonly property real openPanelIndicatorHeight: barContentWidth

  implicitWidth: root.vertical ? (bar ? bar.barSize : Style.bar.sizeHorizontal) : barSlot
  implicitHeight: root.vertical ? barSlot : (bar ? bar.barSize : Style.bar.sizeHorizontal)

  function tooltip() {
    if (!OmamationStore.daemonRunning) return qsTr("Omamation: the engine is not running")
    if (OmamationStore.attention) return qsTr("Omamation: something went wrong, click for details")
    if (OmamationStore.conceptCount > 0)
      return qsTr("Omamation: %n automation(s) waiting for approval", "", OmamationStore.conceptCount)
    return qsTr("Omamation automations")
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    slotSize: root.barSlot
    opticalSize: root.barContentWidth
    tooltipText: root.tooltip()
    hasVisualContent: true

    // Icon drawn as a direct child of the button.
    Item {
      anchors.fill: parent
      scale: button.pressScale

      Text {
        anchors.centerIn: parent
        text: root.iconBolt
        textFormat: Text.PlainText
        font.family: root.fontFamily
        font.pixelSize: Style.bar.iconFont
        renderType: Text.NativeRendering
        color: OmamationStore.needsAttention ? Color.urgent : root.foreground
      }
    }

    onPressed: function(b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(720))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(760))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      OmamationContent {
        id: content
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: parent.height
        panelOpen: root.opened
      }
    }
  }
}
