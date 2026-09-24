import QtQuick
import QtQuick.Controls
import qs.Commons

// First-run guide. It reports readiness and emits explicit actions; it never starts
// a login shell, opens a credential flow, or installs optional capture by itself.
Rectangle {
    id: root
    objectName: "usageSetupView"
    color: Color.background
    readonly property color secondary: Qt.alpha(Color.foreground, 0.68)
    property var providers: []
    property var setupState: ({})
    property bool busy: false
    property string errorText: ""
    property string statusText: ""

    signal actionRequested(string action)
    signal refreshRequested()
    signal providerRequested(string id)
    signal closeRequested()

    function providerRows() {
        var ids = ["claude-code", "codex", "clinepass", "command-code", "opencode-go"]
        var labels = ["Claude Code", "Codex", "ClinePass", "Command Code", "OpenCode Go"]
        var rows = [], i, row
        for (i = 0; i < root.providers.length; i++) rows.push(root.providers[i])
        for (i = 0; i < ids.length; i++) {
            if (!rows.some(function(item) { return item && item.id === ids[i] })) {
                rows.push({ id: ids[i], name: labels[i], status: "unknown", statusLabel: "unknown", activity: {} })
            }
        }
        return rows
    }
    function providerStatus(row) {
        var status = String(row && row.status || "unknown")
        if (status === "ok") return "Detected · using existing sign-in"
        if (status === "unauthenticated") return "Sign-in required"
        if (status === "estimate") return "Estimate available · verify the reported data"
        if (status === "error") return "Collection issue · check the provider's own setup"
        return "Not detected · sign in first"
    }
    function stepState(key) {
        var value = root.setupState && root.setupState[key]
        return value && typeof value === "object" ? value : ({})
    }
    function stepLine(key) {
        var state = root.stepState(key)
        if (state.installed === true) return "Enabled" + (state.reason ? " · " + String(state.reason) : "")
        if (state.available === true) return "Available · off"
        return state.reason ? String(state.reason) : "Not available"
    }
    function canReviewPrice(row) {
        return !!row && root.providers.some(function(item) { return item && item.id === row.id; });
    }
    function priceLine(row) {
        var price = row && row.activity ? row.activity.price : null
        if (!price || price.amount_usd === null || price.amount_usd === undefined) return "Price not recorded"
        var basis = String(price.basis || "")
        var label = price.estimated === true
            ? (basis.indexOf("provider plan quote") >= 0 ? "Provider plan quote estimate" : "Published price estimate")
            : (price.source_kind === "receipt" ? "Last invoice amount · future charge may differ" : "Saved subscription fee")
        return label + ": $" + Number(price.amount_usd).toFixed(2) + " / "
            + (price.cycle === "year" ? "year" : "month")
    }

    ScrollView {
        id: scroll
        objectName: "setupScroll"
        anchors.fill: parent
        anchors.margins: Style.space(12)
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: content.implicitHeight > availableHeight ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff

        Column {
            id: content
            width: scroll.availableWidth
            spacing: Style.space(10)

            Row {
                width: parent.width
                spacing: Style.space(8)
                Column {
                    width: parent.width - closeButton.width - Style.space(8)
                    spacing: Style.space(2)
                    Text {
                        textFormat: Text.PlainText
                        text: "TMOS AI USAGE · SETUP"
                        color: Color.accent
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                        font.bold: true
                    }
                    Text {
                        textFormat: Text.PlainText
                        width: parent.width
                        text: "A quick guide to collection, optional task capture, and imports."
                        color: root.secondary
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        wrapMode: Text.WordWrap
                    }
                }
                Rectangle {
                    id: closeButton
                    objectName: "closeSetup"
                    width: Style.space(56)
                    height: Style.space(30)
                    color: "transparent"
                    border.color: Color.muted
                    Text { anchors.centerIn: parent; textFormat: Text.PlainText; text: "Close"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption }
                    MouseArea { anchors.fill: parent; enabled: !root.busy; onClicked: root.closeRequested(); cursorShape: Qt.PointingHandCursor }
                }
            }

            Text {
                textFormat: Text.PlainText
                visible: root.errorText !== ""
                width: parent.width
                text: root.errorText
                color: Color.urgent
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
            }
            Text {
                textFormat: Text.PlainText
                visible: root.statusText !== ""
                width: parent.width
                text: root.statusText
                color: Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
            }

            Rectangle {
                width: parent.width
                implicitHeight: collectionColumn.implicitHeight + Style.space(16)
                color: Qt.alpha(Color.foreground, 0.035)
                border.color: Qt.alpha(Color.muted, 0.35)
                radius: Style.cornerRadius
                Column {
                    id: collectionColumn
                    anchors.left: parent.left; anchors.right: parent.right
                    anchors.top: parent.top; anchors.margins: Style.space(8)
                    spacing: Style.space(5)
                    Text { textFormat: Text.PlainText; text: "1 · COLLECTION STATUS"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true }
                    Text {
                        textFormat: Text.PlainText
                        width: parent.width
                        text: "The collector checks supported providers using CLIs that are already signed in. It does not launch a shell or display credentials. Sign in with a provider's own app, then refresh here."
                        color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap
                    }
                    Repeater {
                        model: root.providerRows()
                        Row {
                            required property var modelData
                            width: collectionColumn.width
                            spacing: Style.space(6)
                            Text {
                                textFormat: Text.PlainText
                                width: Math.max(Style.space(105), parent.width * 0.31)
                                text: modelData.name || modelData.id || "Provider"
                                color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption; elide: Text.ElideRight
                            }
                            Text {
                                textFormat: Text.PlainText
                                width: parent.width - Math.max(Style.space(105), parent.width * 0.31) - Style.space(6)
                                text: root.providerStatus(modelData)
                                color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap
                            }
                        }
                    }
                    Text {
                        textFormat: Text.PlainText
                        visible: false
                        width: parent.width
                        text: "Provider status is not available yet. Refresh to read the local collection cache."
                        color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap
                    }
                    Row {
                        spacing: Style.space(8)
                        Rectangle {
                            objectName: "refreshSetupProviders"
                            width: refreshText.implicitWidth + Style.space(18); height: Style.space(30)
                            color: "transparent"; border.color: Color.muted
                            Text { id: refreshText; anchors.centerIn: parent; textFormat: Text.PlainText; text: root.busy ? "Working…" : "Refresh providers"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption }
                            MouseArea { anchors.fill: parent; enabled: !root.busy; onClicked: root.refreshRequested(); cursorShape: Qt.PointingHandCursor }
                        }
                    }
                }
            }

            Rectangle {
                width: parent.width
                implicitHeight: billingColumn.implicitHeight + Style.space(16)
                color: Qt.alpha(Color.foreground, 0.035); border.color: Qt.alpha(Color.muted, 0.35); radius: Style.cornerRadius
                Column {
                    id: billingColumn
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: Style.space(8)
                    spacing: Style.space(5)
                    Text { textFormat: Text.PlainText; text: "2 · SUBSCRIPTION PRICES"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true }
                    Text { textFormat: Text.PlainText; width: parent.width; text: "Confirm each displayed subscription amount against your own billing page. Provider plan quotes are estimates; saved prices remain your input and should be checked if they change. No billing page is opened here."; color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap }
                    Repeater {
                        model: root.providerRows()
                        Row {
                            required property var modelData
                            width: billingColumn.width; spacing: Style.space(6)
                            Column {
                                width: parent.width - priceButton.width - Style.space(6)
                                spacing: Style.space(2)
                                Text {
                                    textFormat: Text.PlainText
                                    width: parent.width
                                    text: modelData.name || modelData.id || "Provider"
                                    color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap
                                }
                                Text {
                                    textFormat: Text.PlainText
                                    width: parent.width
                                    text: root.priceLine(modelData)
                                    color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap
                                }
                            }
                            Rectangle {
                                id: priceButton
                                width: confirmText.implicitWidth + Style.space(18); height: Style.space(30)
                                color: "transparent"; border.color: root.canReviewPrice(modelData) ? Color.accent : Color.muted
                                Text { id: confirmText; anchors.centerIn: parent; textFormat: Text.PlainText; text: root.canReviewPrice(modelData) ? "Review price" : "Sign in first"; color: root.canReviewPrice(modelData) ? Color.accent : root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption }
                                MouseArea { anchors.fill: parent; enabled: !root.busy && root.canReviewPrice(modelData); onClicked: root.providerRequested(modelData.id); cursorShape: Qt.PointingHandCursor }
                            }
                        }
                    }
                }
            }

            Rectangle {
                width: parent.width
                implicitHeight: captureColumn.implicitHeight + Style.space(16)
                color: Qt.alpha(Color.foreground, 0.035); border.color: Qt.alpha(Color.muted, 0.35); radius: Style.cornerRadius
                Column {
                    id: captureColumn
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: Style.space(8)
                    spacing: Style.space(6)
                    Text { textFormat: Text.PlainText; text: "3 · OPTIONAL TASK CAPTURE"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true }
                    Text { textFormat: Text.PlainText; width: parent.width; text: "These opt-in integrations record task activity for review. A successful command or passing checks alone never marks work as validated. Disable removes only this plugin's matching link; copied files are left unchanged."; color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap }
                    Row {
                        width: parent.width; spacing: Style.space(8)
                        Column {
                            width: parent.width - taskButton.width - Style.space(8); spacing: Style.space(2)
                            Text { textFormat: Text.PlainText; text: "Task command · " + root.stepLine("task_command"); color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap }
                            Text { textFormat: Text.PlainText; width: parent.width; text: root.stepState("task_command").reason || "Adds an explicit task-run wrapper; existing commands remain unchanged."; color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap }
                        }
                        Rectangle {
                            id: taskButton
                            objectName: "installTaskCommand"
                            width: taskAction.implicitWidth + Style.space(18); height: Style.space(30)
                            color: "transparent"; border.color: root.stepState("task_command").installed === true ? Color.muted : Color.accent
                            Text { id: taskAction; anchors.centerIn: parent; textFormat: Text.PlainText; text: root.stepState("task_command").installed === true ? "Disable" : root.stepState("task_command").available === true ? "Enable" : "Unavailable"; color: root.stepState("task_command").installed === true || root.stepState("task_command").available === true ? Color.accent : root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption }
                            MouseArea { anchors.fill: parent; enabled: !root.busy && (root.stepState("task_command").installed === true || root.stepState("task_command").available === true); onClicked: root.actionRequested(root.stepState("task_command").installed === true ? "remove-task-command" : "install-task-command"); cursorShape: Qt.PointingHandCursor }
                        }
                    }
                    Row {
                        width: parent.width; spacing: Style.space(8)
                        Column {
                            width: parent.width - piButton.width - Style.space(8); spacing: Style.space(2)
                            Text { textFormat: Text.PlainText; text: "Pi observer · " + root.stepLine("pi_observer"); color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap }
                            Text { textFormat: Text.PlainText; width: parent.width; text: (root.stepState("pi_observer").reason || "Optional capture for Pi coding sessions.") + " The observer takes effect in a new Pi session or after reloading the session."; color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap }
                        }
                        Rectangle {
                            id: piButton
                            objectName: "installPiObserver"
                            width: piAction.implicitWidth + Style.space(18); height: Style.space(30)
                            color: "transparent"; border.color: root.stepState("pi_observer").installed === true ? Color.muted : Color.accent
                            Text { id: piAction; anchors.centerIn: parent; textFormat: Text.PlainText; text: root.stepState("pi_observer").installed === true ? "Disable" : root.stepState("pi_observer").available === true ? "Enable" : "Unavailable"; color: root.stepState("pi_observer").installed === true || root.stepState("pi_observer").available === true ? Color.accent : root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption }
                            MouseArea { anchors.fill: parent; enabled: !root.busy && (root.stepState("pi_observer").installed === true || root.stepState("pi_observer").available === true); onClicked: root.actionRequested(root.stepState("pi_observer").installed === true ? "remove-pi-observer" : "install-pi-observer"); cursorShape: Qt.PointingHandCursor }
                        }
                    }
                }
            }

            Rectangle {
                width: parent.width
                implicitHeight: importColumn.implicitHeight + Style.space(16)
                color: Qt.alpha(Color.foreground, 0.035); border.color: Qt.alpha(Color.muted, 0.35); radius: Style.cornerRadius
                Column {
                    id: importColumn
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: Style.space(8)
                    spacing: Style.space(5)
                    Text { textFormat: Text.PlainText; text: "4 · REPORT IMPORTS & HUMAN REVIEW"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true }
                    Text { textFormat: Text.PlainText; width: parent.width; text: "Provider reports can be imported automatically from supported export files. Place exports in this incoming folder, then refresh. Billing documents and task outcomes require your manual import and review; they are never inferred from usage."; color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap }
                    TextEdit {
                        textFormat: Text.PlainText
                        width: parent.width
                        visible: !!(root.setupState && (root.setupState.incoming_dir || root.setupState.reports_dir))
                        text: "Incoming folder: " + (root.setupState.incoming_dir || root.setupState.reports_dir)
                        color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WrapAnywhere
                        readOnly: true
                        selectByMouse: true
                        height: contentHeight
                    }
                    Text {
                        textFormat: Text.PlainText
                        width: parent.width
                        visible: !root.setupState || !(root.setupState.incoming_dir || root.setupState.reports_dir)
                        text: "Incoming folder location is not available yet. Refresh setup status to read it."
                        color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap
                    }
                }
            }

            Row {
                width: parent.width
                spacing: Style.space(8)
                Rectangle {
                    objectName: "finishSetup"
                    width: finishText.implicitWidth + Style.space(24); height: Style.space(34)
                    color: Color.accent; radius: Style.space(2)
                    Text { id: finishText; anchors.centerIn: parent; textFormat: Text.PlainText; text: "Finish setup"; color: Color.background; font.family: Style.font.family; font.pixelSize: Style.font.caption; font.bold: true }
                    MouseArea { anchors.fill: parent; enabled: !root.busy; onClicked: root.actionRequested("finish"); cursorShape: Qt.PointingHandCursor }
                }
                Text {
                    textFormat: Text.PlainText
                    width: parent.width - finishText.implicitWidth - Style.space(40)
                    text: root.busy ? "Updating setup status…" : "Setup can be revisited later. Optional capture stays off until you choose Enable."
                    color: root.secondary; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap
                    anchors.verticalCenter: parent.verticalCenter
                }
            }
        }
    }
}
