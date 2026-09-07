import QtQuick
import QtQuick.Layouts

// Vertical plan checklist with tick marks — the Claude-Code-style todo
// view of the current job's plan (completed ✓, active ring, pending dot).
ColumnLayout {
    id: root
    required property var theme
    property alias model: repeater.model
    spacing: theme.space2

    Repeater {
        id: repeater
        delegate: RowLayout {
            id: stepDelegate
            required property int index
            required property string content
            required property string status
            required property bool active
            Layout.fillWidth: true
            spacing: theme.space2

            Rectangle {
                width: 20
                height: 20
                radius: 10
                Layout.alignment: Qt.AlignTop
                color: stepDelegate.status === "completed" ? theme.success
                       : stepDelegate.status === "cancelled" ? theme.surfaceSunken
                       : "transparent"
                border.color: stepDelegate.status === "completed" ? theme.success
                              : stepDelegate.active ? theme.accent
                              : theme.borderStrong
                border.width: stepDelegate.active ? 2 : 1

                Text {
                    anchors.centerIn: parent
                    visible: stepDelegate.status === "completed"
                    text: "✓"
                    color: theme.textOnAccent
                    font.pixelSize: 11
                    font.bold: true
                }
                Rectangle {
                    anchors.centerIn: parent
                    visible: stepDelegate.active
                    width: 8; height: 8; radius: 4
                    color: theme.accent
                    SequentialAnimation on opacity {
                        running: stepDelegate.active && axisController.animationsEnabled
                        loops: Animation.Infinite
                        NumberAnimation { from: 1.0; to: 0.35; duration: 700 }
                        NumberAnimation { from: 0.35; to: 1.0; duration: 700 }
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Text {
                    Layout.fillWidth: true
                    text: stepDelegate.content
                    wrapMode: Text.WordWrap
                    color: stepDelegate.status === "completed" || stepDelegate.status === "cancelled"
                           ? theme.textSecondary : theme.textPrimary
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSizeBody
                    font.weight: stepDelegate.active ? Font.DemiBold : Font.Normal
                    font.strikeout: stepDelegate.status === "cancelled"
                }
                Text {
                    visible: stepDelegate.active
                    text: "In progress"
                    color: theme.accent
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSizeSmall
                }
            }
        }
    }
}
