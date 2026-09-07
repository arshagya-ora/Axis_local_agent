import QtQuick
import QtQuick.Layouts

// Status pill: soft tinted background, colored dot, bold label. Status is
// always communicated with a dot AND text, never color alone
// (accessibility requirement).
Rectangle {
    id: root
    required property var theme
    property string label: ""
    property string kind: "neutral" // matches theme.statusColor kinds
    property bool animated: false

    readonly property color tint: theme.statusColor(kind)

    radius: theme.radiusPill
    color: Qt.rgba(tint.r, tint.g, tint.b, theme.isDark ? 0.18 : 0.10)
    border.color: Qt.rgba(tint.r, tint.g, tint.b, 0.35)
    border.width: 1
    implicitHeight: row.implicitHeight + theme.space2 * 2
    implicitWidth: row.implicitWidth + theme.space3 * 2

    Accessible.role: Accessible.Indicator
    Accessible.name: label

    RowLayout {
        id: row
        anchors.centerIn: parent
        spacing: theme.space2

        Rectangle {
            width: 8
            height: 8
            radius: 4
            color: tint
            Layout.alignment: Qt.AlignVCenter

            SequentialAnimation on opacity {
                running: root.animated
                loops: Animation.Infinite
                NumberAnimation { from: 1.0; to: 0.35; duration: 700 }
                NumberAnimation { from: 0.35; to: 1.0; duration: 700 }
            }
        }

        Text {
            text: root.label
            color: tint
            font.family: theme.fontFamily
            font.pixelSize: theme.fontSizeSmall
            font.weight: Font.DemiBold
        }
    }
}
