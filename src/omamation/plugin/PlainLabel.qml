import QtQuick
import qs.Commons

// Plain-text label that does not interpret markup.
Text {
  property bool muted: false

  textFormat: Text.PlainText
  font.family: Style.font.family
  font.pixelSize: Style.font.body
  color: muted ? Util.alpha(Color.popups.text, 0.55) : Color.popups.text
  renderType: Text.NativeRendering
  elide: Text.ElideRight
  verticalAlignment: Text.AlignVCenter
}
