import QtQuick
import QtQuick.Controls.Basic

// The one button style used everywhere: a rounded pill with primary /
// ghost / danger variants, replacing the default boxy Controls look.
Button {
    id: root
    required property var theme
    property string kind: "ghost" // "primary" | "ghost" | "danger"
    property string icon_: ""     // optional AxisIcon name (see AxisIcon.qml)

    implicitHeight: 36
    leftPadding: theme.space3
    rightPadding: theme.space3
    hoverEnabled: true

    readonly property color _bg: {
        if (kind === "primary")
            return !enabled ? Qt.rgba(theme.accent.r, theme.accent.g, theme.accent.b, 0.4)
                            : hovered ? theme.accentHover : theme.accent
        if (kind === "danger")
            return hovered ? Qt.rgba(theme.error.r, theme.error.g, theme.error.b, 0.12) : "transparent"
        return hovered ? theme.surfaceSunken : "transparent"
    }
    readonly property color _fg: {
        if (kind === "primary") return theme.textOnAccent
        if (kind === "danger") return theme.error
        return !enabled ? theme.muted : theme.textPrimary
    }

    background: Rectangle {
        radius: theme.radiusPill
        color: root._bg
        border.color: root.kind === "primary" ? "transparent"
                      : root.kind === "danger" ? Qt.rgba(theme.error.r, theme.error.g, theme.error.b, 0.4)
                      : theme.border
        border.width: root.kind === "primary" ? 0 : 1
        Behavior on color { enabled: axisController.animationsEnabled; ColorAnimation { duration: root.theme.durationFast } }
    }

    contentItem: Row {
        spacing: root.icon_ !== "" && root.text !== "" ? root.theme.space2 : 0
        AxisIcon {
            visible: root.icon_ !== ""
            width: 16
            height: 16
            name: root.icon_
            color: root._fg
            anchors.verticalCenter: parent.verticalCenter
        }
        Text {
            visible: root.text !== ""
            text: root.text
            color: root._fg
            font.family: root.theme.fontFamily
            font.pixelSize: root.theme.fontSizeBody
            font.weight: root.kind === "primary" ? Font.DemiBold : Font.Medium
            anchors.verticalCenter: parent.verticalCenter
        }
    }
}
