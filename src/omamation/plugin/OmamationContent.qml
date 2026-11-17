import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Ui

// Popup body: every automation with on/off, its last run, Run now, and the
// readable view of what it does (when / if / do / on error). Concepts are
// red with an Approve button, which asks for approval with a notification.
// Nothing here edits an automation; that is the job of the CLI and agents.
Item {
  id: root

  property bool panelOpen: false
  property string expanded: ""          // id of the automation whose steps are shown
  property string message: ""
  property bool messageIsError: false
  implicitHeight: Math.max(list.implicitHeight, Style.space(120))

  onPanelOpenChanged: if (!root.panelOpen) { root.message = ""; root.expanded = "" }

  Connections {
    target: OmamationStore
    function onChangeFailed(message) { root.message = message; root.messageIsError = true }
    function onChangeDone(message) { root.message = message; root.messageIsError = false }
  }

  function when(iso) {
    if (!iso) return ""
    var d = new Date(iso)
    return isNaN(d) ? iso : d.toLocaleString(Qt.locale(), Locale.ShortFormat)
  }

  function resultText(a) {
    if (a.running) return qsTr("running…")
    var r = a.last_result || a.last_run
    if (!r) return qsTr("never ran")
    var label = r.result === "success" ? qsTr("ok") : r.result === "failed" ? qsTr("failed")
      : r.result === "skipped" ? qsTr("skipped") : r.result
    return label + (r.dry_run ? " " + qsTr("(dry run)") : "") + " · " + root.when(r.started)
  }

  Flickable {
    anchors.fill: parent
    contentHeight: list.implicitHeight
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

    ColumnLayout {
      id: list
      width: parent.width
      spacing: Style.spacing.rowGap

      PanelSectionHeader { text: qsTr("Omamation"); Layout.fillWidth: true }

      PlainLabel {
        visible: !OmamationStore.daemonRunning
        Layout.fillWidth: true
        color: Color.urgent
        wrapMode: Text.WordWrap
        elide: Text.ElideNone
        text: qsTr("The engine is not running. Start it with: systemctl --user start omamation")
      }

      PlainLabel {
        visible: OmamationStore.dryRun
        Layout.fillWidth: true
        muted: true
        wrapMode: Text.WordWrap
        elide: Text.ElideNone
        text: qsTr("Dry run: actions are only written to the trace, nothing is done.")
      }

      Repeater {
        model: OmamationStore.daemonErrors
        delegate: PlainLabel {
          required property var modelData
          Layout.fillWidth: true
          color: Color.urgent
          wrapMode: Text.WordWrap
          elide: Text.ElideNone
          text: modelData
        }
      }

      PlainLabel {
        visible: OmamationStore.error !== ""
        Layout.fillWidth: true
        color: Color.urgent
        wrapMode: Text.WordWrap
        elide: Text.ElideNone
        text: OmamationStore.error
      }

      PlainLabel {
        visible: OmamationStore.daemonRunning && OmamationStore.automations.length === 0
        Layout.fillWidth: true
        muted: true
        text: qsTr("No automations yet. Ask an agent, or use: omamation save <file>")
      }

      Repeater {
        model: OmamationStore.automations
        delegate: ColumnLayout {
          id: card
          required property var modelData
          readonly property var a: modelData
          readonly property bool concept: a.status === "concept"
          readonly property bool invalid: a.status === "invalid"
          readonly property bool failed: !!(a.last_result && a.last_result.result === "failed")
          Layout.fillWidth: true
          spacing: Style.spacing.sm

          PanelSeparator { Layout.fillWidth: true }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.md

            ToggleSwitch {
              checked: !!card.a.enabled
              busy: OmamationStore.busy
              interactive: !card.invalid
              onToggled: OmamationStore.change([card.a.enabled ? "disable" : "enable", card.a.id, "--by", "harm"])
            }
            PlainLabel {
              text: card.a.name
              font.bold: true
              color: (card.concept || card.invalid || card.failed) ? Color.urgent : Color.popups.text
            }
            PlainLabel {
              Layout.fillWidth: true
              muted: true
              text: card.invalid ? qsTr("invalid") : card.concept ? qsTr("waiting for approval") : root.resultText(card.a)
            }
            Button {
              visible: card.concept
              text: qsTr("Approve"); bordered: true; selected: true
              enabled: !OmamationStore.busy
              tooltipText: qsTr("Shows a notification; click Approve there")
              onClicked: OmamationStore.change(["approve", card.a.id], qsTr("Click Approve in the notification."))
            }
            Button {
              text: qsTr("Run now"); fontSize: Style.font.bodySmall
              enabled: !OmamationStore.busy && !card.concept && !card.invalid && !card.a.running
              tooltipText: qsTr("Runs it now; its conditions are still checked")
              onClicked: OmamationStore.change(["run", card.a.id, "--source", "bar"])
            }
            Button {
              text: root.expanded === card.a.id ? qsTr("Hide") : qsTr("Steps"); fontSize: Style.font.bodySmall
              onClicked: root.expanded = root.expanded === card.a.id ? "" : card.a.id
            }
          }

          PlainLabel {
            visible: card.failed && root.expanded !== card.a.id
            Layout.fillWidth: true
            color: Color.urgent
            wrapMode: Text.WordWrap
            elide: Text.ElideNone
            font.pixelSize: Style.font.bodySmall
            text: card.failed ? (card.a.last_result.error.step + ": " + card.a.last_result.error.message) : ""
          }

          PlainLabel {
            visible: card.invalid
            Layout.fillWidth: true
            color: Color.urgent
            wrapMode: Text.WordWrap
            elide: Text.ElideNone
            font.pixelSize: Style.font.bodySmall
            text: card.a.problem || ""
          }

          // The readable view: the same rows `omamation show` prints.
          ColumnLayout {
            visible: root.expanded === card.a.id
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(8)
            spacing: Style.spacing.xs

            PlainLabel {
              visible: !!card.a.description
              Layout.fillWidth: true
              muted: true
              wrapMode: Text.WordWrap
              elide: Text.ElideNone
              text: card.a.description || ""
            }

            Repeater {
              model: card.a.lines || []
              delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                spacing: Style.spacing.md
                PlainLabel {
                  text: modelData.label
                  muted: true
                  Layout.preferredWidth: Style.space(72)
                }
                PlainLabel {
                  text: modelData.block
                  font.bold: true
                }
                PlainLabel {
                  Layout.fillWidth: true
                  text: modelData.text
                  wrapMode: Text.WrapAnywhere
                  elide: Text.ElideNone
                  font.family: "monospace"
                  font.pixelSize: Style.font.bodySmall
                }
              }
            }
          }
        }
      }

      PlainLabel {
        visible: root.message !== ""
        Layout.fillWidth: true
        color: root.messageIsError ? Color.urgent : Util.alpha(Color.popups.text, 0.55)
        wrapMode: Text.WordWrap
        elide: Text.ElideNone
        text: root.message
      }
    }
  }
}
