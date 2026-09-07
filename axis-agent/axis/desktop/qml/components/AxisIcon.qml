import QtQuick
import QtQuick.Shapes

// Vector icons drawn with QtQuick.Shapes (ships with the pinned PySide6).
//
// Deliberately NOT an icon font: a private-use-area glyph depends on a
// specific Windows font being installed and on Qt resolving the family
// exactly, and silently degrades to a tofu box when either assumption
// fails. These paths always render identically everywhere.
//
// Each icon is one SVG path string on a 24x24 grid; multiple "M"
// subpaths inside a single string keep this to one ShapePath (a Repeater
// cannot be used here — its delegate must be an Item, and ShapePath is
// not one).
Item {
    id: root
    property string name: ""
    property color color: "black"
    property real strokeWidth: 1.8

    implicitWidth: 20
    implicitHeight: 20

    readonly property var _paths: ({
        "plus":     "M12 5 L12 19 M5 12 L19 12",
        "chat":     "M20 14 C20 15.1 19.1 16 18 16 L8 16 L4 20 L4 6 C4 4.9 4.9 4 6 4 L18 4 C19.1 4 20 4.9 20 6 Z",
        "history":  "M3.6 12 A8.4 8.4 0 1 1 6.2 18.2 M3.6 12 L3.6 7.6 M3.6 12 L8 12 M12 7.5 L12 12 L15.4 14",
        "search":   "M11 4 A7 7 0 1 1 10.99 4 M16 16 L20.5 20.5",
        "sun":      "M12 8.2 A3.8 3.8 0 1 1 11.99 8.2 M12 2.6 L12 4.6 M12 19.4 L12 21.4 M4.4 4.4 L5.8 5.8 M18.2 18.2 L19.6 19.6 M2.6 12 L4.6 12 M19.4 12 L21.4 12 M4.4 19.6 L5.8 18.2 M18.2 5.8 L19.6 4.4",
        "moon":     "M20 14.6 A8.5 8.5 0 1 1 9.4 4 A6.8 6.8 0 0 0 20 14.6 Z",
        "settings": "M5 7 L19 7 M5 12 L19 12 M5 17 L19 17 M9 5.6 A1.4 1.4 0 1 1 8.99 5.6 M15 10.6 A1.4 1.4 0 1 1 14.99 10.6 M8 15.6 A1.4 1.4 0 1 1 7.99 15.6",
        "send":     "M21 3 L10.5 13.5 M21 3 L14.5 21 L10.5 13.5 L3 9.5 Z",
        "pause":    "M9.5 5 L9.5 19 M14.5 5 L14.5 19",
        "play":     "M7.5 4.8 L18.5 12 L7.5 19.2 Z",
        "check":    "M4.8 12.6 L9.6 17.4 L19.2 6.8",
        "close":    "M6 6 L18 18 M18 6 L6 18",
        "alert":    "M12 4 L21 19.5 L3 19.5 Z M12 10 L12 14.2 M12 16.8 L12 16.82",
        "document": "M6 3 L14 3 L19 8 L19 21 L6 21 Z M14 3 L14 8 L19 8 M9 12.5 L16 12.5 M9 16.5 L16 16.5",
        "bolt":     "M13 2.5 L4 13.5 L11.5 13.5 L10.5 21.5 L20 10 L12.5 10 Z",
        "chevron":  "M9.5 5.5 L16 12 L9.5 18.5",
        "sliders":  "M4 8 L20 8 M4 16 L20 16 M9.6 6.6 A1.4 1.4 0 1 1 9.59 6.6 M15 14.6 A1.4 1.4 0 1 1 14.99 14.6"
    })

    readonly property string _path: _paths[name] !== undefined ? _paths[name] : ""

    // The paths are authored on a fixed 24x24 grid and the whole Shape is
    // scaled to the requested size, so stroke weight stays proportional.
    Shape {
        width: 24
        height: 24
        antialiasing: true
        visible: root._path !== ""

        transform: Scale {
            xScale: root.width / 24
            yScale: root.height / 24
        }

        ShapePath {
            strokeColor: root.color
            strokeWidth: root.strokeWidth
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            PathSvg { path: root._path }
        }
    }
}
