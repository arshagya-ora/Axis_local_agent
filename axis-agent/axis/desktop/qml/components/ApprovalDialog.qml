import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

// An inline chat card, not a modal — approval requests live in the same
// message thread as everything else. A decision requires an explicit
// Approve/Deny click; the card renders for as long as the controller's
// authoritative `hasPendingApproval` is true.
Rectangle {
    id: root
    required property var theme
    visible: axisController.hasPendingApproval
    implicitHeight: visible ? content.implicitHeight + theme.space4 * 2 : 0
    radius: theme.radiusLarge
    color: theme.surface
    border.color: Qt.rgba(theme.warning.r, theme.warning.g, theme.warning.b, 0.55)
    border.width: 1

    property bool decisionInFlight: false

    Connections {
        target: axisController
        function onIsBusyChanged() {
            if (!axisController.isBusy)
                root.decisionInFlight = false
        }
    }

    Rectangle {
        // Warm accent stripe on the left edge, like a callout.
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

        RowLayout {
            Layout.fillWidth: true
            spacing: theme.space2
            Text {
                text: "Approval required"
                color: theme.textPrimary
                font.family: theme.fontFamily
                font.pixelSize: theme.fontSizeTitle
                font.weight: Font.DemiBold
            }
            Item { Layout.fillWidth: true }
            Rectangle {
                radius: theme.radiusPill
                color: Qt.rgba(theme.riskColor(axisController.approvalRisk).r,
                                theme.riskColor(axisController.approvalRisk).g,
                                theme.riskColor(axisController.approvalRisk).b, theme.isDark ? 0.20 : 0.12)
                implicitWidth: riskLabel.implicitWidth + theme.space3 * 2
                implicitHeight: riskLabel.implicitHeight + theme.space1 * 2
                Text {
                    id: riskLabel
                    anchors.centerIn: parent
                    text: axisController.approvalRisk
                    color: theme.riskColor(axisController.approvalRisk)
                    font.family: theme.fontFamily
                    font.pixelSize: theme.fontSizeSmall
                    font.weight: Font.DemiBold
                }
            }
        }

        Text {
            Layout.fillWidth: true
            text: axisController.approvalSummary
            wrapMode: Text.WordWrap
            color: theme.textPrimary
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSizeBody
        }

        Text {
            Layout.fillWidth: true
            visible: axisController.approvalReason !== ""
            text: axisController.approvalReason
            wrapMode: Text.WordWrap
            color: theme.textSecondary
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSizeSmall
        }

        Text {
            Layout.fillWidth: true
            text: "Approving permits only this one attempt. Browser scope, firewall, and policy checks still apply."
            wrapMode: Text.WordWrap
            color: theme.muted
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSizeSmall
            font.italic: true
        }

        RowLayout {
            Layout.alignment: Qt.AlignRight
            Layout.topMargin: theme.space2
            spacing: theme.space2

            PillButton {
                theme: root.theme
                text: "Deny"
                kind: "danger"
                enabled: !root.decisionInFlight
                Accessible.name: "Deny this action"
                onClicked: {
                    root.decisionInFlight = true
                    axisController.denyCurrentEffect()
                }
            }
            PillButton {
                theme: root.theme
                text: "Approve once"
                kind: "primary"
                enabled: !root.decisionInFlight
                Accessible.name: "Approve this action once"
                onClicked: {
                    root.decisionInFlight = true
                    axisController.approveCurrentEffect()
                }
            }
        }
    }
}
