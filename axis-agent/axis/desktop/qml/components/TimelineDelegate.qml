import QtQuick
import QtQuick.Layouts

// One activity-log line in the chat thread (status changes, plan steps,
// approvals, acceptance results). Color-coded dot + muted text.
RowLayout {
    id: root
    required property var theme
    required property string kind
    required property string text

    width: ListView.view ? ListView.view.width : implicitWidth
    spacing: theme.space2

    Rectangle {
        width: 7
        height: 7
        radius: 3.5
        color: {
            switch (root.kind) {
            case "approval": return theme.warning
            case "question": return theme.warning
            case "rebind": return theme.error
            case "acceptance": return theme.success
            case "plan": return theme.accent
            case "status": return theme.muted
            default: return theme.muted
            }
        }
        Layout.alignment: Qt.AlignTop
        Layout.topMargin: 6
        Layout.leftMargin: theme.space2
    }

    Text {
        Layout.fillWidth: true
        text: root.text
        wrapMode: Text.WordWrap
        color: theme.textSecondary
        font.family: theme.fontFamily
        font.pixelSize: theme.fontSizeSmall
    }
}
