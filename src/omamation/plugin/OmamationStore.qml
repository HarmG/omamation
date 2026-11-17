pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io

// All Omamation state, shared by every bar copy of the widget. The daemon
// rewrites $XDG_RUNTIME_DIR/omamation/state.json after every change; this
// store only reads that file and runs the omamation CLI for actions, so the
// widget never changes an automation itself.
//
// Note: plugin hot-reload does not recreate this singleton; changes to this
// file only take effect after `omarchy restart shell`.
Singleton {
  id: root

  readonly property string stateFile: (Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/1000") + "/omamation/state.json"

  property var automations: []
  property var cli: ["omamation"]
  property var daemonErrors: []
  property bool attention: false
  property bool daemonRunning: false
  property bool dryRun: false
  property string error: ""
  property bool busy: false

  signal changeFailed(string message)
  signal changeDone(string message)

  readonly property int conceptCount: root.automations.filter(function(a) { return a.status === "concept" }).length
  readonly property bool needsAttention: root.attention || root.conceptCount > 0 || !root.daemonRunning

  function read(text) {
    if (String(text || "").trim() === "") {
      root.daemonRunning = false
      root.automations = []
      return
    }
    try {
      var data = JSON.parse(String(text))
      root.automations = data.automations || []
      root.cli = data.cli || ["omamation"]
      root.daemonErrors = (data.daemon && data.daemon.errors) || []
      root.dryRun = !!(data.daemon && data.daemon.dry_run)
      root.attention = !!data.attention
      root.daemonRunning = true
      root.error = ""
    } catch (e) {
      root.error = qsTr("Could not read the Omamation state: %1").arg(e)
    }
  }

  // run / enable / disable / approve: one at a time; the daemon rewrites the state afterwards.
  function change(args, doneMessage) {
    if (changeProc.running) return false
    root.busy = true
    changeProc.doneMessage = doneMessage || ""
    changeProc.command = root.cli.concat(args)
    changeProc.running = true
    return true
  }

  FileView {
    id: stateView
    path: root.stateFile
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.read(stateView.text())
    onLoadFailed: root.read("")
  }

  // The daemon replaces the file on every write; reading again now and then
  // also notices a daemon that stopped and removed it.
  Timer {
    interval: 15000
    running: true
    repeat: true
    onTriggered: stateView.reload()
  }

  Process {
    id: changeProc
    property string doneMessage: ""
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      root.busy = false
      if (exitCode === 0) {
        root.changeDone(changeProc.doneMessage || String(stdout.text).trim())
      } else {
        var text = String(stderr.text || "").trim()
        root.changeFailed(text !== "" ? text.split("\n").pop() : qsTr("Failed (exit code %1)").arg(exitCode))
      }
      stateView.reload()
    }
  }
}
