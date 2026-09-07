pragma Singleton
import QtQuick

// Centralized design tokens. `mode` is set once from the controller's
// `theme` property ("system" | "light" | "dark"); "system" resolves via
// Qt's own style hints so the app follows the OS without a restart.
//
// Visual language: warm cream canvas, white cards with soft shadows,
// coral accent — an enterprise "calm workspace" aesthetic rather than
// default boxy controls.
QtObject {
    id: theme

    property string mode: "system"
    readonly property bool isDark: mode === "dark" || (mode === "system" && Qt.styleHints.colorScheme === Qt.Dark)

    // Spacing (8px system) and radii.
    readonly property int space1: 4
    readonly property int space2: 8
    readonly property int space3: 16
    readonly property int space4: 24
    readonly property int space5: 32
    readonly property int space6: 48
    readonly property int radiusSmall: 8
    readonly property int radiusMedium: 12
    readonly property int radiusLarge: 16
    readonly property int radiusXLarge: 22
    readonly property int radiusPill: 999

    // Motion.
    readonly property int durationFast: 120
    readonly property int durationNormal: 180
    readonly property int durationSlow: 260

    // Typography.
    // Qt's font.family takes ONE family name — a CSS-style comma list is
    // treated as a single literal family, so it silently falls back.
    // Icons do not use a font at all (see components/AxisIcon.qml): they
    // are vector paths, so no private-use-area glyph can degrade to a
    // tofu box on a machine without the expected icon font installed.
    readonly property string fontFamily: "Segoe UI"
    readonly property int fontSizeSmall: 12
    readonly property int fontSizeBody: 14
    readonly property int fontSizeTitle: 17
    readonly property int fontSizeHeading: 22
    readonly property int fontSizeHero: 34

    // Color roles.
    readonly property color background: isDark ? "#16130f" : "#faf6f1"
    readonly property color surface: isDark ? "#1e1a15" : "#ffffff"
    readonly property color surfaceRaised: isDark ? "#26211b" : "#ffffff"
    readonly property color surfaceSunken: isDark ? "#121009" : "#f4ede5"
    readonly property color border: isDark ? "#37302a" : "#ece3d9"
    readonly property color borderStrong: isDark ? "#4a423a" : "#ddd1c4"
    readonly property color textPrimary: isDark ? "#f3ede6" : "#27201a"
    readonly property color textSecondary: isDark ? "#b0a599" : "#7c7166"
    readonly property color accent: "#c9503f"
    readonly property color accentHover: "#b34433"
    readonly property color accentSoft: isDark ? "#3a241f" : "#faece8"
    readonly property color textOnAccent: "#ffffff"
    readonly property color success: "#2e9e63"
    readonly property color warning: "#d98a12"
    readonly property color error: "#d64541"
    readonly property color muted: isDark ? "#7d7468" : "#a89c8f"
    readonly property color purple: "#8b5cf6"
    readonly property color shadow: isDark ? "#000000" : "#2c1c12"

    // Risk colors.
    readonly property color riskRead: "#4c8fce"
    readonly property color riskReversibleLocal: "#2f9e8f"
    readonly property color riskExternalEffect: "#d98a12"
    readonly property color riskDestructive: "#d64541"

    function riskColor(riskLabel) {
        switch (riskLabel) {
        case "Read": return riskRead
        case "Reversible local": return riskReversibleLocal
        case "External effect": return riskExternalEffect
        case "Destructive/high impact": return riskDestructive
        default: return muted
        }
    }

    function statusColor(kind) {
        switch (kind) {
        case "accent": return success
        case "attention": return warning
        case "muted": return muted
        case "progress": return purple
        case "warning": return warning
        case "success": return success
        case "error": return error
        default: return textSecondary
        }
    }
}
