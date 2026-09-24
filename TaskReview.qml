import QtQuick
import QtQuick.Controls
import qs.Commons

// Review is an explicit human action over sanitized backend projections. The component never
// launches a command and never promotes request success or a passing check into acceptance.
Column {
    id: root
    objectName: "taskReviewPanel"
    property string provider: ""
    property var tasks: []
    property bool loading: false
    property bool truncated: false
    property int unavailableCount: 0
    property bool deciding: false
    property string errorText: ""
    property string statusText: ""
    property string reviewer: ""
    property string semanticAcceptedKey: ""
    signal refreshRequested()
    signal decisionRequested(string taskRef, string reviewer, string decision,
                             string expectedRun, string expectedVerification, bool semanticAccepted)

    spacing: Style.space(8)
    onProviderChanged: semanticAcceptedKey = ""
    onStatusTextChanged: semanticAcceptedKey = ""
    onTasksChanged: {
        if (semanticAcceptedKey === "") return
        var ref = semanticAcceptedKey.split("|")[0]
        var current = tasks.filter(function(t) { return t.task_ref === ref })[0]
        if (!current || semanticKey(current) !== semanticAcceptedKey) semanticAcceptedKey = ""
    }
    function providerTasks() {
        return root.tasks.filter(function(t) { return t.provider === root.provider })
    }
    function stamp(v) { return v || "time unavailable" }
    function semanticKey(task) {
        return task && task.task_ref && task.run && task.run.execution_id && task.verification
            && task.verification.verification_sha256
            ? task.task_ref + "|" + task.run.execution_id + "|" + (task.verification.verification_sha256 || "")
            : ""
    }
    function hasCompletedRun(task) {
        return !!(task && task.run && task.run.execution_id && task.run.ended_at
            && task.run.exit_code !== null && task.run.exit_code !== undefined)
    }
    function actionEnabled(task, decision) {
        if (root.loading || root.deciding || !root.reviewer.trim() || !root.hasCompletedRun(task)) return false
        if (decision === "accept") return task.can_accept === true && root.semanticKey(task) !== ""
            && root.semanticAcceptedKey === root.semanticKey(task)
        return task.status === "pending"
    }

    Row {
        width: parent.width
        spacing: Style.space(8)
        Text {
            textFormat: Text.PlainText
            width: parent.width - reloadButton.implicitWidth - Style.space(8)
            text: "TASK EVIDENCE REVIEW"
            color: Color.accent
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            anchors.verticalCenter: parent.verticalCenter
        }
        Rectangle {
            id: reloadButton
            implicitWidth: reloadText.implicitWidth + Style.space(18)
            implicitHeight: Style.space(30)
            color: "transparent"
            border.color: Color.muted
            Text { id: reloadText; textFormat: Text.PlainText; anchors.centerIn: parent; text: root.loading ? "Reading…" : "Refresh tasks"; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption }
            MouseArea { anchors.fill: parent; enabled: !root.loading && !root.deciding; onClicked: root.refreshRequested(); cursorShape: Qt.PointingHandCursor }
        }
    }
    Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: root.errorText !== ""
        text: root.errorText
        color: Color.urgent
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
    }
    Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: root.statusText !== ""
        text: root.statusText
        color: Color.accent
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
    }
    Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: root.truncated || root.unavailableCount > 0
        text: (root.truncated ? "Showing the 100 most recently updated task records. " : "")
            + (root.unavailableCount > 0 ? root.unavailableCount + " task record(s) could not be reviewed." : "")
        color: root.secondary
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
    }
    Repeater {
        model: root.providerTasks()
        Column {
            required property var modelData
            readonly property var task: modelData
            width: parent.width
            spacing: Style.space(6)
            Rectangle { width: parent.width; height: 1; color: Qt.alpha(Color.muted, 0.3) }
            Text { textFormat: Text.PlainText; width: parent.width; text: modelData.label + " · " + modelData.status + " · " + String(modelData.task_ref).slice(0, 10); color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.bodySmall; font.bold: true; wrapMode: Text.WordWrap }
            Text {
                textFormat: Text.PlainText
                width: parent.width
                text: "Run " + root.stamp(modelData.run && modelData.run.started_at)
                    + " · " + (modelData.run && modelData.run.exit_code === 0 ? "process exited 0" : modelData.run && modelData.run.exit_code !== null && modelData.run.exit_code !== undefined ? "process exit " + modelData.run.exit_code : "no completed run")
                    + (modelData.run && modelData.run.artifact_bound ? " · output hash " + String(modelData.run.artifact_sha256 || "unavailable").slice(0, 12) : " · no output artifact bound")
                color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption; wrapMode: Text.WordWrap
            }
            Text {
                textFormat: Text.PlainText
                width: parent.width
                text: modelData.verification && modelData.verification.verification_sha256
                    ? (modelData.verification.check_label || "Check command (unlabelled)") + " · "
                        + (modelData.verification.exit_code === 0 ? "check passed" : "check exit " + modelData.verification.exit_code)
                        + " · reviewer " + modelData.verification.reviewer
                        + " · " + root.stamp(modelData.verification.ended_at)
                        + " · check " + String(modelData.verification.verification_sha256 || "").slice(0, 12)
                        + (modelData.run && modelData.run.artifact_bound
                            ? (modelData.verification.artifact_unchanged === true ? " · output unchanged" : " · output changed/unavailable")
                            : "")
                    : "No verification receipt is available."
                color: root.secondary
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
            }
            Text {
                textFormat: Text.PlainText
                width: parent.width
                visible: !root.hasCompletedRun(task) || !task.can_accept
                text: !root.hasCompletedRun(task)
                    ? "No completed run receipt; decisions are disabled until a task run is captured."
                    : task.status !== "pending" ? "Task is already finalized; further decisions are disabled."
                    : "Acceptance status: " + (task.reason || "successful verification of the latest run is required.")
                color: root.secondary
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
            }
            Row {
                width: parent.width
                spacing: Style.space(8)
                TextField {
                    width: Math.min(parent.width * 0.42, Style.space(190))
                    placeholderText: "Reviewer name"
                    maximumLength: 80
                    text: root.reviewer
                    onTextEdited: root.reviewer = text
                    color: Color.foreground
                    placeholderTextColor: root.secondary
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    background: Rectangle { color: Color.background; border.color: Color.muted }
                }
                CheckBox {
                    id: semanticCheck
                    objectName: "semanticConfirmation"
                    width: parent.width - reviewerFieldWidth - Style.space(8)
                    property real reviewerFieldWidth: Math.min(parent.width * 0.42, Style.space(190))
                    checked: root.semanticAcceptedKey !== "" && root.semanticAcceptedKey === root.semanticKey(task)
                    onToggled: root.semanticAcceptedKey = checked ? root.semanticKey(task) : ""
                    text: "I reviewed the result and confirm it meets requirements"
                    enabled: task.can_accept === true && root.hasCompletedRun(task)
                        && !root.loading && !root.deciding
                    contentItem: Text {
                        textFormat: Text.PlainText
                        text: semanticCheck.text
                        color: Color.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        wrapMode: Text.WordWrap
                        leftPadding: semanticCheck.indicator.width + semanticCheck.spacing
                        verticalAlignment: Text.AlignVCenter
                    }
                    indicator: Rectangle {
                        implicitWidth: Style.space(16)
                        implicitHeight: Style.space(16)
                        x: semanticCheck.leftPadding
                        y: semanticCheck.height / 2 - height / 2
                        radius: Style.space(2)
                        color: semanticCheck.checked ? Color.accent : Color.background
                        border.color: semanticCheck.checked ? Color.accent : Color.muted
                        Text {
                            textFormat: Text.PlainText
                            anchors.centerIn: parent
                            visible: semanticCheck.checked
                            text: "✓"
                            color: Color.background
                            font.family: Style.font.family
                            font.pixelSize: Style.font.caption
                        }
                    }
                    Binding {
                        target: semanticCheck
                        property: "checked"
                        value: root.semanticAcceptedKey !== "" && root.semanticAcceptedKey === root.semanticKey(task)
                    }
                }
            }
            Row {
                spacing: Style.space(6)
                Repeater {
                    model: ["accept", "fail", "abandon"]
                    Rectangle {
                        required property string modelData
                        property string caption: modelData === "accept" ? "Accept as validated" : modelData === "fail" ? "Mark failed" : "Abandon"
                        property bool actionable: root.actionEnabled(task, modelData)
                        enabled: actionable
                        opacity: actionable ? 1 : 0.45
                        implicitWidth: actionText.implicitWidth + Style.space(20)
                        implicitHeight: Style.space(30)
                        color: modelData === "accept" && root.semanticAcceptedKey === root.semanticKey(task) ? Qt.alpha(Color.accent, 0.16) : "transparent"
                        border.color: modelData === "accept" ? Color.accent : Color.muted
                        Text { id: actionText; textFormat: Text.PlainText; anchors.centerIn: parent; text: parent.caption; color: Color.foreground; font.family: Style.font.family; font.pixelSize: Style.font.caption }
                        MouseArea {
                            anchors.fill: parent
                            enabled: parent.actionable
                            cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                            onClicked: root.decisionRequested(task.task_ref, root.reviewer.trim(), modelData,
                                task.run && task.run.execution_id ? task.run.execution_id : "",
                                task.verification ? task.verification.verification_sha256 || "" : "",
                                root.semanticAcceptedKey === root.semanticKey(task))
                        }
                    }
                }
            }
        }
    }
    Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: !root.loading && root.providerTasks().length === 0 && root.errorText === ""
        text: "No captured tasks for this provider. Future work must be recorded through task_runner.py run and verified explicitly; past tasks are not inferred."
        color: root.secondary
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
    }
    readonly property color secondary: Qt.alpha(Color.foreground, 0.68)
}
