import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "components"

ApplicationWindow {
    id: window
    visible: true
    width: 1360
    height: 880
    minimumWidth: 1000
    minimumHeight: 680
    title: "AXIS"
    color: Theme.background

    Binding { target: Theme; property: "mode"; value: axisController.theme }

    property bool sessionsPanelOpen: true
    readonly property bool heroMode: axisController.chatCount === 0 && axisController.currentJobStatus === ""

    onClosing: function(close) {
        if (axisController.currentJobStatus !== "" && !axisController.hasResult
                && axisController.currentJobStatus !== "cancelled") {
            close.accepted = false
            closeConfirmDialog.open()
        }
    }

    Shortcut { sequence: "Ctrl+N"; onActivated: axisController.newSession() }
    Shortcut { sequence: "Ctrl+R"; onActivated: axisController.refreshCurrentJob() }
    Shortcut { sequence: "Ctrl+Shift+R"; onActivated: reconnectDialog.open() }
    Shortcut {
        sequence: "Ctrl+P"
        onActivated: {
            if (axisController.canPause) axisController.pauseCurrentJob()
            else if (axisController.canResume) axisController.resumeCurrentJob()
        }
    }

    // A successfully started job or a submitted answer clears the composer;
    // failures deliberately leave the text in place for retry.
    Connections {
        target: axisController
        function onCurrentJobIdChanged() {
            if (axisController.currentJobId !== "") composer.text = ""
        }
        function onPendingQuestionChanged() {
            if (!axisController.hasPendingQuestion) composer.text = ""
        }
        function onChatChanged() {
            Qt.callLater(chatView.scrollToBottom)
        }
    }

    // ---- Inline components ------------------------------------------------

    component RailButton: Rectangle {
        id: railBtn
        property string iconName: ""
        property string tip: ""
        property bool active: false
        signal clicked()
        width: 44
        height: 44
        radius: Theme.radiusMedium
        color: active ? Theme.accentSoft : railArea.containsMouse ? Theme.surfaceSunken : "transparent"
        Accessible.role: Accessible.Button
        Accessible.name: tip
        AxisIcon {
            anchors.centerIn: parent
            width: 19
            height: 19
            name: railBtn.iconName
            color: railBtn.active ? Theme.accent : Theme.textSecondary
        }
        MouseArea {
            id: railArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: railBtn.clicked()
        }
        ToolTip.visible: railArea.containsMouse && railBtn.tip !== ""
        ToolTip.text: railBtn.tip
        ToolTip.delay: 600
    }

    component SuggestionRow: Rectangle {
        id: suggestion
        property string iconName: ""
        property string label: ""
        height: 52
        color: suggestionArea.containsMouse ? Theme.surfaceSunken : "transparent"
        radius: Theme.radiusMedium
        Accessible.role: Accessible.Button
        Accessible.name: suggestion.label
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: Theme.space3
            anchors.rightMargin: Theme.space3
            spacing: Theme.space3
            AxisIcon {
                width: 18
                height: 18
                name: suggestion.iconName
                color: Theme.accent
            }
            Text {
                Layout.fillWidth: true
                text: suggestion.label
                color: Theme.textPrimary
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBody
            }
            AxisIcon {
                width: 15
                height: 15
                name: "chevron"
                color: Theme.muted
            }
        }
        Rectangle { height: 1; color: Theme.border; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top }
        MouseArea {
            id: suggestionArea
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: { composer.text = suggestion.label; composer.forceActiveFocus() }
        }
    }

    // ---- Dialogs ----------------------------------------------------------

    component ThemedDialog: Dialog {
        modal: true
        anchors.centerIn: parent
        padding: Theme.space4
        standardButtons: Dialog.NoButton
        background: Rectangle {
            radius: Theme.radiusXLarge
            color: Theme.surfaceRaised
            border.color: Theme.border
            border.width: 1
        }
    }

    ThemedDialog {
        id: closeConfirmDialog
        width: 440
        contentItem: ColumnLayout {
            spacing: Theme.space3
            Text {
                text: "AXIS job still running"
                color: Theme.textPrimary
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeTitle
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "The durable AXIS job will keep running even if you close this window. What would you like to do?"
                color: Theme.textSecondary
                font.family: Theme.fontFamily
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                spacing: Theme.space2
                PillButton { theme: Theme; text: "Keep AXIS open"; onClicked: closeConfirmDialog.close() }
                PillButton {
                    theme: Theme
                    text: "Leave running and close"
                    kind: "primary"
                    onClicked: { closeConfirmDialog.close(); Qt.callLater(window.close) }
                }
            }
        }
    }

    ThemedDialog {
        id: cancelConfirmDialog
        width: 440
        contentItem: ColumnLayout {
            spacing: Theme.space3
            Text {
                text: "Cancel this task?"
                color: Theme.textPrimary
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeTitle
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "This stops the durable job. This cannot be undone."
                color: Theme.textSecondary
                font.family: Theme.fontFamily
            }
            TextField {
                id: cancelReasonField
                Layout.fillWidth: true
                placeholderText: "Optional reason"
                maximumLength: 300
                font.family: Theme.fontFamily
                color: Theme.textPrimary
                background: Rectangle {
                    radius: Theme.radiusMedium
                    color: Theme.surfaceSunken
                    border.color: cancelReasonField.activeFocus ? Theme.accent : Theme.border
                    border.width: 1
                }
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                spacing: Theme.space2
                PillButton { theme: Theme; text: "Keep running"; onClicked: cancelConfirmDialog.close() }
                PillButton {
                    theme: Theme
                    text: "Cancel task"
                    kind: "primary"
                    onClicked: {
                        axisController.requestCancel(cancelReasonField.text)
                        cancelReasonField.text = ""
                        cancelConfirmDialog.close()
                    }
                }
            }
        }
    }

    ThemedDialog {
        id: reconnectDialog
        width: 440
        contentItem: ColumnLayout {
            spacing: Theme.space3
            Text {
                text: "Reconnect to job"
                color: Theme.textPrimary
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeTitle
                font.weight: Font.DemiBold
            }
            TextField {
                id: reconnectField
                Layout.fillWidth: true
                placeholderText: "AXIS job ID"
                font.family: Theme.fontFamily
                color: Theme.textPrimary
                background: Rectangle {
                    radius: Theme.radiusMedium
                    color: Theme.surfaceSunken
                    border.color: reconnectField.activeFocus ? Theme.accent : Theme.border
                    border.width: 1
                }
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                spacing: Theme.space2
                PillButton { theme: Theme; text: "Cancel"; onClicked: reconnectDialog.close() }
                PillButton {
                    theme: Theme
                    text: "Reconnect"
                    kind: "primary"
                    enabled: reconnectField.text.trim().length > 0
                    onClicked: {
                        axisController.reconnectJob(reconnectField.text.trim())
                        reconnectField.text = ""
                        reconnectDialog.close()
                    }
                }
            }
        }
    }

    RebindDialog { theme: Theme }

    Popup {
        id: settingsPopup
        x: 72
        y: window.height - height - Theme.space4
        padding: Theme.space4
        background: Rectangle {
            radius: Theme.radiusLarge
            color: Theme.surfaceRaised
            border.color: Theme.border
            border.width: 1
        }
        contentItem: ColumnLayout {
            spacing: Theme.space3
            Text {
                text: "Settings"
                color: Theme.textPrimary
                font.family: Theme.fontFamily
                font.weight: Font.DemiBold
            }
            RowLayout {
                spacing: Theme.space2
                Text {
                    text: "Theme"
                    color: Theme.textSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSmall
                }
                ComboBox {
                    id: themeCombo
                    Layout.preferredWidth: 160
                    model: ["system", "light", "dark"]
                    currentIndex: model.indexOf(axisController.theme)
                    onActivated: axisController.setTheme(model[currentIndex])
                }
            }
            CheckBox {
                text: "Animations"
                checked: axisController.animationsEnabled
                onToggled: axisController.setAnimationsEnabled(checked)
            }
            PillButton {
                theme: Theme
                text: "Reconnect to job…"
                onClicked: { settingsPopup.close(); reconnectDialog.open() }
            }
        }
    }

    Popup {
        id: errorPopup
        visible: axisController.safeErrorCode !== ""
        x: (window.width - width) / 2
        y: Theme.space4
        padding: Theme.space3
        background: Rectangle {
            radius: Theme.radiusLarge
            color: Theme.surfaceRaised
            border.color: Theme.error
            border.width: 1
        }
        contentItem: RowLayout {
            spacing: Theme.space3
            Text {
                text: axisController.safeErrorMessage
                color: Theme.textPrimary
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeSmall
                wrapMode: Text.WordWrap
                Layout.preferredWidth: 360
            }
            PillButton { theme: Theme; text: "Dismiss"; onClicked: axisController.clearSafeError() }
        }
    }

    // ---- Layout -----------------------------------------------------------

    RowLayout {
        anchors.fill: parent
        spacing: 0

        // -- Icon rail ------------------------------------------------------
        Rectangle {
            Layout.preferredWidth: 68
            Layout.fillHeight: true
            color: Theme.background

            ColumnLayout {
                anchors.fill: parent
                anchors.topMargin: Theme.space3
                anchors.bottomMargin: Theme.space3
                spacing: Theme.space2

                Image {
                    Layout.alignment: Qt.AlignHCenter
                    source: "../assets/axis_icon.svg"
                    sourceSize.width: 30
                    sourceSize.height: 30
                }

                Item { Layout.preferredHeight: Theme.space3 }

                RailButton {
                    Layout.alignment: Qt.AlignHCenter
                    iconName: "plus"
                    tip: "New task (Ctrl+N)"
                    active: window.heroMode
                    onClicked: { axisController.newSession(); composer.forceActiveFocus() }
                }
                RailButton {
                    iconName: "chat"
                    tip: "Chats"
                    Layout.alignment: Qt.AlignHCenter
                    active: window.sessionsPanelOpen
                    onClicked: window.sessionsPanelOpen = !window.sessionsPanelOpen
                }

                Item { Layout.fillHeight: true }

                RailButton {
                    iconName: Theme.isDark ? "sun" : "moon"
                    tip: "Toggle theme"
                    Layout.alignment: Qt.AlignHCenter
                    onClicked: axisController.setTheme(Theme.isDark ? "light" : "dark")
                }
                RailButton {
                    iconName: "settings"
                    tip: "Settings"
                    Layout.alignment: Qt.AlignHCenter
                    onClicked: settingsPopup.open()
                }
            }

            Rectangle { width: 1; color: Theme.border; anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom }
        }

        // -- Sessions panel -------------------------------------------------
        Rectangle {
            Layout.preferredWidth: window.sessionsPanelOpen ? 276 : 0
            Layout.fillHeight: true
            visible: window.sessionsPanelOpen
            clip: true
            color: Theme.background

            Behavior on Layout.preferredWidth {
                enabled: axisController.animationsEnabled
                NumberAnimation { duration: Theme.durationNormal; easing.type: Easing.OutCubic }
            }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: Theme.space3
                spacing: Theme.space3

                RowLayout {
                    Layout.fillWidth: true
                    spacing: Theme.space2
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0
                        Text {
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                            text: "AXIS"
                            color: Theme.textPrimary
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeHeading
                            font.weight: Font.DemiBold
                            font.letterSpacing: 3
                        }
                        Text {
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                            text: "Your browser works for you"
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSmall
                        }
                    }
                }

                PillButton {
                    theme: Theme
                    Layout.fillWidth: true
                    text: "New task"
                    icon_: "plus"
                    kind: "primary"
                    Accessible.name: "Start a new task"
                    onClicked: { axisController.newSession(); composer.forceActiveFocus() }
                }

                Text {
                    text: "Chats"
                    color: Theme.muted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSmall
                    font.weight: Font.DemiBold
                    font.capitalization: Font.AllUppercase
                    font.letterSpacing: 1
                    Layout.topMargin: Theme.space2
                }

                Text {
                    visible: sessionListView.count === 0
                    text: "No chats yet — start your first task."
                    color: Theme.muted
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeSmall
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                }

                ListView {
                    id: sessionListView
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: Theme.space1
                    model: axisController.sessionListModel
                    delegate: Rectangle {
                        id: sessionDelegate
                        required property string sessionId
                        required property string title
                        required property string statusLabel
                        required property string timeLabel
                        required property bool active
                        width: ListView.view.width
                        height: sessionCol.implicitHeight + Theme.space3 * 2
                        radius: Theme.radiusMedium
                        color: sessionDelegate.active ? Theme.accentSoft
                               : sessionArea.containsMouse ? Theme.surfaceSunken : "transparent"

                        Accessible.role: Accessible.Button
                        Accessible.name: "Open chat " + sessionDelegate.title

                        MouseArea {
                            id: sessionArea
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: axisController.openSession(sessionDelegate.sessionId)
                        }

                        ColumnLayout {
                            id: sessionCol
                            anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; margins: Theme.space3 }
                            spacing: 3
                            Text {
                                Layout.fillWidth: true
                                text: sessionDelegate.title
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                                color: Theme.textPrimary
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBody
                                font.weight: sessionDelegate.active ? Font.DemiBold : Font.Medium
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.space1
                                Text {
                                    visible: sessionDelegate.statusLabel !== ""
                                    text: sessionDelegate.statusLabel
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: sessionDelegate.timeLabel
                                    color: Theme.muted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                }
                            }
                        }
                    }
                }
            }

            Rectangle { width: 1; color: Theme.border; anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom }
        }

        // -- Center: hero or chat -------------------------------------------
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            // Header
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 60
                visible: !window.heroMode

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: Theme.space4
                    anchors.rightMargin: Theme.space4
                    spacing: Theme.space3

                    Text {
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                        text: axisController.sessionTitle !== "" ? axisController.sessionTitle : "AXIS"
                        color: Theme.textPrimary
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeTitle
                        font.weight: Font.DemiBold
                    }

                    StatusBadge {
                        theme: Theme
                        visible: axisController.currentJobStatus !== ""
                        label: axisController.pausePending ? "Pausing…" : axisController.currentJobStatusLabel
                        kind: axisController.pausePending ? "warning" : axisController.currentJobStatusKind
                        animated: axisController.animationsEnabled
                                  && (axisController.currentJobStatus === "running" || axisController.pausePending)
                    }

                    Rectangle {
                        width: 8; height: 8; radius: 4
                        color: axisController.connectionState === "connected" ? Theme.success : Theme.muted
                        Layout.alignment: Qt.AlignVCenter
                    }
                    Text {
                        text: axisController.connectionState === "connected" ? "Connected"
                              : axisController.connectionState === "connecting" ? "Connecting…" : "Disconnected"
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeSmall
                    }

                    PillButton {
                        theme: Theme; text: "Pause"; icon_: "pause"
                        visible: axisController.canPause && !axisController.pausePending
                        onClicked: axisController.pauseCurrentJob()
                    }
                    PillButton {
                        theme: Theme; text: "Resume"; icon_: "play"; kind: "primary"
                        visible: axisController.canResume
                        onClicked: axisController.resumeCurrentJob()
                    }
                    PillButton {
                        theme: Theme; text: "Cancel"; kind: "danger"
                        visible: axisController.canCancel
                        onClicked: cancelConfirmDialog.open()
                    }
                }

                Rectangle { height: 1; color: Theme.border; anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom }
            }

            // Hero empty state
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: window.heroMode

                ColumnLayout {
                    anchors.centerIn: parent
                    width: Math.min(640, parent.width - Theme.space5 * 2)
                    spacing: Theme.space3

                    Image {
                        Layout.alignment: Qt.AlignHCenter
                        source: "../assets/axis_icon.svg"
                        sourceSize.width: 72
                        sourceSize.height: 72
                    }

                    Text {
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        text: "What can AXIS do for you?"
                        color: Theme.textPrimary
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeHero
                        font.weight: Font.DemiBold
                    }
                    Text {
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        text: "Your browser works for you."
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeTitle
                    }

                    Item { Layout.preferredHeight: Theme.space4 }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0

                        SuggestionRow { Layout.fillWidth: true; iconName: "document"; label: "Summarize the page I'm viewing" }
                        SuggestionRow { Layout.fillWidth: true; iconName: "bolt"; label: "Complete a repetitive browser task" }
                        SuggestionRow { Layout.fillWidth: true; iconName: "sliders"; label: "Verify a configuration change" }
                    }
                }
            }

            // Chat thread
            Flickable {
                id: chatView
                Layout.fillWidth: true
                Layout.fillHeight: true
                visible: !window.heroMode
                clip: true
                contentWidth: width
                contentHeight: chatColumn.implicitHeight + Theme.space5 * 2
                boundsBehavior: Flickable.StopAtBounds

                function scrollToBottom() {
                    if (contentHeight > height)
                        contentY = contentHeight - height
                }

                ScrollBar.vertical: ScrollBar { }

                ColumnLayout {
                    id: chatColumn
                    x: Math.max(Theme.space4, (chatView.width - width) / 2)
                    y: Theme.space4
                    width: Math.min(760, chatView.width - Theme.space4 * 2)
                    spacing: Theme.space3

                    ListView {
                        id: chatListView
                        Layout.fillWidth: true
                        Layout.preferredHeight: Math.max(0, contentHeight)
                        interactive: false
                        model: axisController.chatModel
                        spacing: Theme.space3

                        delegate: Column {
                            id: chatDelegate
                            required property string kind
                            required property string text
                            required property string meta
                            width: ListView.view.width

                            // User bubble (right-aligned, accent)
                            Rectangle {
                                visible: chatDelegate.kind === "user"
                                anchors.right: parent.right
                                width: Math.min(userText.implicitWidth + Theme.space4 * 2, chatDelegate.width * 0.8)
                                height: visible ? userText.implicitHeight + Theme.space3 * 2 : 0
                                radius: Theme.radiusLarge
                                color: Theme.accent
                                Text {
                                    id: userText
                                    anchors.fill: parent
                                    anchors.margins: Theme.space3
                                    text: chatDelegate.text
                                    wrapMode: Text.WordWrap
                                    color: Theme.textOnAccent
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeBody
                                }
                            }

                            // Result card
                            Rectangle {
                                visible: chatDelegate.kind === "result"
                                width: parent.width
                                height: visible ? resultCol.implicitHeight + Theme.space4 * 2 : 0
                                radius: Theme.radiusLarge
                                color: Theme.surface
                                border.color: chatDelegate.meta === "completed"
                                              ? Qt.rgba(Theme.success.r, Theme.success.g, Theme.success.b, 0.5)
                                              : chatDelegate.meta === "failed"
                                                ? Qt.rgba(Theme.error.r, Theme.error.g, Theme.error.b, 0.5)
                                                : Theme.border
                                border.width: 1
                                ColumnLayout {
                                    id: resultCol
                                    anchors.fill: parent
                                    anchors.margins: Theme.space4
                                    spacing: Theme.space2
                                    RowLayout {
                                        spacing: Theme.space2
                                        AxisIcon {
                                            width: 17
                                            height: 17
                                            name: chatDelegate.meta === "completed" ? "check"
                                                  : chatDelegate.meta === "failed" ? "alert" : "chat"
                                            color: chatDelegate.meta === "completed" ? Theme.success
                                                   : chatDelegate.meta === "failed" ? Theme.error : Theme.muted
                                        }
                                        Text {
                                            text: chatDelegate.meta === "completed" ? "Task completed"
                                                  : chatDelegate.meta === "failed" ? "Task failed"
                                                  : "Result: " + chatDelegate.meta
                                            color: Theme.textPrimary
                                            font.family: Theme.fontFamily
                                            font.pixelSize: Theme.fontSizeBody
                                            font.weight: Font.DemiBold
                                        }
                                    }
                                    Text {
                                        Layout.fillWidth: true
                                        text: chatDelegate.text
                                        wrapMode: Text.WordWrap
                                        color: Theme.textSecondary
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSizeBody
                                    }
                                }
                            }

                            // Everything else: activity log line
                            RowLayout {
                                visible: chatDelegate.kind !== "user" && chatDelegate.kind !== "result"
                                width: parent.width
                                height: visible ? implicitHeight : 0
                                spacing: Theme.space2
                                Rectangle {
                                    width: 7; height: 7; radius: 3.5
                                    Layout.alignment: Qt.AlignTop
                                    Layout.topMargin: 6
                                    Layout.leftMargin: Theme.space2
                                    color: {
                                        switch (chatDelegate.kind) {
                                        case "approval": return Theme.warning
                                        case "question": return Theme.warning
                                        case "rebind": return Theme.error
                                        case "acceptance": return Theme.success
                                        case "plan": return Theme.accent
                                        default: return Theme.muted
                                        }
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: chatDelegate.text
                                    wrapMode: Text.WordWrap
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSmall
                                }
                            }
                        }
                    }

                    // Live job card: status + plan checklist while a job is active.
                    Rectangle {
                        visible: axisController.currentJobStatus !== "" && !axisController.hasResult
                        Layout.fillWidth: true
                        implicitHeight: liveCol.implicitHeight + Theme.space4 * 2
                        radius: Theme.radiusLarge
                        color: Theme.surface
                        border.color: Theme.border
                        border.width: 1

                        ColumnLayout {
                            id: liveCol
                            anchors.fill: parent
                            anchors.margins: Theme.space4
                            spacing: Theme.space3

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: Theme.space2
                                Image {
                                    source: "../assets/axis_icon.svg"
                                    sourceSize.width: 18
                                    sourceSize.height: 18
                                }
                                Text {
                                    text: "AXIS"
                                    color: Theme.textPrimary
                                    font.family: Theme.fontFamily
                                    font.weight: Font.DemiBold
                                }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: axisController.pausePending ? "Pausing after the current step…"
                                          : axisController.currentJobStatusLabel
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSizeSmall
                                }
                            }

                            PlanChecklist {
                                theme: Theme
                                Layout.fillWidth: true
                                visible: axisController.planCount > 0
                                model: axisController.planModel
                            }

                            Text {
                                visible: axisController.planCount === 0
                                text: "Working…"
                                color: Theme.muted
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeSmall
                                font.italic: true
                            }
                        }
                    }

                    ApprovalDialog { theme: Theme; Layout.fillWidth: true }
                    UserInputDialog { theme: Theme; Layout.fillWidth: true }
                }
            }

            // Composer
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: composerCard.height + Theme.space4 * 2

                Rectangle {
                    id: composerCard
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: Theme.space4
                    width: Math.min(760, parent.width - Theme.space4 * 2)
                    height: Math.max(64, composer.implicitHeight + Theme.space3 * 2)
                    radius: Theme.radiusXLarge
                    color: Theme.surface
                    border.color: composer.activeFocus ? Theme.accent : Theme.border
                    border.width: composer.activeFocus ? 2 : 1

                    Behavior on border.color { enabled: axisController.animationsEnabled; ColorAnimation { duration: Theme.durationFast } }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: Theme.space4
                        anchors.rightMargin: Theme.space2
                        spacing: Theme.space2

                        TextArea {
                            id: composer
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                            placeholderText: axisController.hasPendingQuestion ? "Type your answer…"
                                             : axisController.canStart ? "Ask AXIS to handle something…"
                                             : "AXIS is working…"
                            placeholderTextColor: Theme.muted
                            wrapMode: TextArea.Wrap
                            enabled: axisController.canStart || axisController.hasPendingQuestion
                            color: Theme.textPrimary
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeBody
                            background: null
                            Accessible.name: "Message AXIS"
                            readonly property int maxLength: axisController.hasPendingQuestion ? 2000 : 500
                            onTextChanged: if (text.length > maxLength) text = text.slice(0, maxLength)
                            Keys.onPressed: function(event) {
                                if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !(event.modifiers & Qt.ShiftModifier)) {
                                    event.accepted = true
                                    sendButton.trigger()
                                }
                            }
                        }

                        Text {
                            visible: composer.text.length > composer.maxLength - 100
                            text: composer.text.length + " / " + composer.maxLength
                            color: Theme.muted
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSizeSmall
                        }

                        Rectangle {
                            id: sendButton
                            Layout.alignment: Qt.AlignVCenter
                            width: 42
                            height: 42
                            radius: 21
                            readonly property bool ready: axisController.hasPendingQuestion
                                                          ? composer.text.trim().length > 0
                                                          : (axisController.canStart && !axisController.isBusy && composer.text.trim().length > 0)
                            color: ready ? (sendArea.containsMouse ? Theme.accentHover : Theme.accent) : Theme.surfaceSunken

                            function trigger() {
                                if (!ready)
                                    return
                                if (axisController.hasPendingQuestion)
                                    axisController.submitUserAnswer(composer.text)
                                else
                                    axisController.startTask(composer.text)
                            }

                            Accessible.role: Accessible.Button
                            Accessible.name: axisController.hasPendingQuestion ? "Send answer" : "Start task"

                            AxisIcon {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                name: "send"
                                color: sendButton.ready ? Theme.textOnAccent : Theme.muted
                            }
                            MouseArea {
                                id: sendArea
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: sendButton.ready ? Qt.PointingHandCursor : Qt.ArrowCursor
                                onClicked: sendButton.trigger()
                            }
                        }
                    }
                }
            }
        }
    }
}
