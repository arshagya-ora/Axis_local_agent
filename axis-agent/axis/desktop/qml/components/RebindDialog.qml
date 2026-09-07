import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

// Candidate rows never show raw Chrome/bridge identifiers — only the
// bounded, sanitized projection the controller already redacted
// (see axis.desktop.presentation.DesktopRebindCandidate).
Dialog {
    id: root
    required property var theme
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    visible: axisController.rebindRequired
    anchors.centerIn: parent
    width: 480
    padding: theme.space4

    property bool actionInFlight: false

    Connections {
        target: axisController
        function onIsBusyChanged() {
            if (!axisController.isBusy)
                root.actionInFlight = false
        }
    }

    background: Rectangle {
        radius: theme.radiusXLarge
        color: theme.surfaceRaised
        border.color: theme.border
        border.width: 1
    }

    contentItem: ColumnLayout {
        spacing: theme.space3

        Text {
            text: "Browser attention required"
            color: theme.textPrimary
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSizeTitle
            font.weight: Font.DemiBold
        }

        Text {
            Layout.fillWidth: true
            text: "AXIS lost track of the intended browser tab and needs to reconnect before it can continue."
            wrapMode: Text.WordWrap
            color: theme.textSecondary
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSizeBody
        }

        ColumnLayout {
            visible: axisController.rebindCandidateCount > 0
            Layout.fillWidth: true
            spacing: theme.space2

            Text {
                text: "Choose the tab to continue with:"
                color: theme.textSecondary
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSizeSmall
            }

            Repeater {
                model: axisController.rebindCandidateModel
                delegate: Rectangle {
                    id: candidateDelegate
                    required property int index
                    required property string label
                    required property string title
                    required property string sanitizedUrl
                    Layout.fillWidth: true
                    radius: theme.radiusMedium
                    color: candidateArea.containsMouse ? theme.surfaceSunken : "transparent"
                    border.color: theme.border
                    border.width: 1
                    implicitHeight: candidateCol.implicitHeight + theme.space3 * 2
                    opacity: root.actionInFlight ? 0.5 : 1.0

                    Accessible.role: Accessible.Button
                    Accessible.name: "Select " + candidateDelegate.label

                    MouseArea {
                        id: candidateArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        enabled: !root.actionInFlight
                        onClicked: {
                            root.actionInFlight = true
                            axisController.selectRebindCandidate(candidateDelegate.index)
                        }
                    }

                    ColumnLayout {
                        id: candidateCol
                        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; margins: theme.space3 }
                        spacing: 2
                        Text {
                            Layout.fillWidth: true
                            text: candidateDelegate.label + (candidateDelegate.title ? (" — " + candidateDelegate.title) : "")
                            elide: Text.ElideRight
                            color: theme.textPrimary
                            font.family: theme.fontFamily
                            font.pixelSize: theme.fontSizeBody
                            font.weight: Font.DemiBold
                        }
                        Text {
                            Layout.fillWidth: true
                            visible: candidateDelegate.sanitizedUrl !== ""
                            text: candidateDelegate.sanitizedUrl
                            elide: Text.ElideRight
                            color: theme.textSecondary
                            font.family: theme.fontFamily
                            font.pixelSize: theme.fontSizeSmall
                        }
                    }
                }
            }
        }

        RowLayout {
            visible: axisController.rebindCandidateCount === 0
            Layout.alignment: Qt.AlignRight
            PillButton {
                theme: root.theme
                text: "Try to reconnect"
                kind: "primary"
                enabled: !root.actionInFlight
                onClicked: {
                    root.actionInFlight = true
                    axisController.requestBrowserRebind()
                }
            }
        }
    }
}
