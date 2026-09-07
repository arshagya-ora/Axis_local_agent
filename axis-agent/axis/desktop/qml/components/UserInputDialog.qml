import QtQuick
import QtQuick.Layouts

// An inline chat card, not a modal. AXIS's question is answered through
// the one message composer at the bottom of the window — there is
// deliberately no second text field here.
Rectangle {
    id: root
    required property var theme
    visible: axisController.hasPendingQuestion
    implicitHeight: visible ? content.implicitHeight + theme.space4 * 2 : 0
    radius: theme.radiusLarge
    color: theme.surface
    border.color: Qt.rgba(theme.warning.r, theme.warning.g, theme.warning.b, 0.55)
    border.width: 1

    Rectangle {
        width: 4
        radius: 2
        color: theme.warning
        anchors { left: parent.left; top: parent.top; bottom: parent.bottom; margins: theme.space3 }
    }

    ColumnLayout {
        id: content
        anchors.fill: parent
        anchors.margins: theme.space4
        anchors.leftMargin: theme.space4 + theme.space2
        spacing: theme.space2

        Text {
            text: "AXIS is asking a question"
            color: theme.textPrimary
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSizeTitle
            font.weight: Font.DemiBold
        }

        Text {
            Layout.fillWidth: true
            text: axisController.questionText
            wrapMode: Text.WordWrap
            color: theme.textPrimary
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSizeBody
        }

        Text {
            Layout.fillWidth: true
            text: "Type your answer in the message box below and send it."
            wrapMode: Text.WordWrap
            color: theme.muted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSizeSmall
            font.italic: true
        }
    }
}
