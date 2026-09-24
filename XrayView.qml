import QtQuick
import QtQuick.Controls
import qs.Commons
import "Model.js" as Model

// One data source and one ranking, shared with the compact budget popup.
Rectangle {
    id: root
    color: Color.background
    readonly property color secondary: Qt.alpha(Color.foreground, 0.68)
    property var providers: []
    property string selectedId: ""
    property double nowMs: Date.now()
    Timer { interval: 30000; repeat: true; running: root.visible; onTriggered: root.nowMs = Date.now() }
    signal backRequested()
    readonly property var selected: providers.filter(p => p.id === selectedId)[0] || null
    onProvidersChanged: if (selectedId && !selected) selectedId = ""
    function metric(v) { return typeof v === "number" && isFinite(v) ? v.toLocaleString(Qt.locale(), 'f', v % 1 ? 2 : 0) : "—" }
    function timestamp(v) {
        if (typeof v !== 'number' || !isFinite(v)) return 'No timestamp';
        var d = new Date(v);
        return isNaN(d.getTime()) ? 'No timestamp' : d.toISOString();
    }
    function offerStatus(o) {
        var observed = Date.parse(o.observed_at);
        if (!isFinite(observed) || observed > root.nowMs || root.nowMs - observed > o.fresh_for_seconds * 1000)
            return 'STALE — recheck';
        if (o.expires_date && new Date(root.nowMs).toISOString().slice(0,10) >= o.expires_date)
            return 'EXPIRY DATE REACHED — recheck timezone and terms';
        return 'Observed ' + o.observed_at;
    }
    function value(p) { return p.tasksPerDollar === null ? "—" : metric(p.tasksPerDollar) }
    function series(p) { return ['5h', 'week', 'month'].map(k => p.windows[k] ? p.windows[k].remainingPct : -1) }

    component Label: Text {
        textFormat: Text.PlainText
        color: Color.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
    }
    component Action: Rectangle {
        property string title: ""
        property bool chosen: false
        signal activated()
        implicitWidth: actionText.implicitWidth + Style.space(22)
        implicitHeight: Style.space(32)
        color: chosen ? Qt.alpha(Color.accent, 0.16) : "transparent"
        border.color: chosen ? Color.accent : Color.muted
        Label { id: actionText; anchors.centerIn: parent; text: parent.title; font.pixelSize: Style.font.caption }
        MouseArea { anchors.fill: parent; onClicked: parent.activated(); cursorShape: Qt.PointingHandCursor }
    }
    ScrollView {
        id: scroll
        anchors.fill: parent
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        Column {
            width: scroll.availableWidth
            spacing: Style.space(16)
            Row {
                spacing: Style.space(16)
                Action { title: "‹ Budget"; onActivated: root.backRequested() }
                Label { text: "INFERENCE X-RAY"; font.pixelSize: Style.font.subtitle; font.bold: true; color: Color.accent }
            }
            Flow {
                width: parent.width
                spacing: Style.space(6)
                Action { title: "Overview"; chosen: root.selectedId === ""; onActivated: root.selectedId = "" }
                Repeater {
                    model: root.providers
                    Action {
                        required property var modelData
                        title: modelData.valueLabel + " · " + modelData.name
                        chosen: root.selectedId === modelData.id
                        onActivated: root.selectedId = modelData.id
                    }
                }
            }
            Label {
                width: parent.width
                text: root.selected ? root.selected.name + "  /  " + root.selected.plan : "All subscriptions · validated task value comparison"
                font.pixelSize: Style.font.subtitle
            }
            Label {
                width: parent.width
                color: root.secondary
                text: "Ranks require validated outcomes and recognized spend for the same period and workload cohort. Unknown evidence stays unranked; free usage is listed separately."
            }
            Column {
                visible: !root.selected
                width: parent.width
                spacing: Style.space(8)
                Label { text: "CAPACITY OVERLAY · % REMAINING"; color: Color.accent; font.bold: true }
                Canvas {
                    id: overlay
                    width: parent.width
                    height: Style.space(180)
                    property var rows: root.providers
                    property color ink: Color.foreground
                    property color accent: Color.accent
                    property color muted: root.secondary
                    onRowsChanged: requestPaint()
                    onInkChanged: requestPaint()
                    onAccentChanged: requestPaint()
                    onMutedChanged: requestPaint()
                    onWidthChanged: requestPaint()
                    onPaint: {
                        var ctx = getContext('2d'); ctx.reset();
                        var left = 38, right = width - 20, top = 20, bottom = height - 30;
                        ctx.font = '12px ' + Style.font.family;
                        ctx.strokeStyle = muted; ctx.fillStyle = ink;
                        for (var v = 0; v <= 100; v += 50) {
                            var y = bottom - v / 100 * (bottom - top);
                            ctx.globalAlpha = 0.35; ctx.beginPath(); ctx.moveTo(left,y); ctx.lineTo(right,y); ctx.stroke();
                            ctx.globalAlpha = 1; ctx.fillText(String(v), 0, y + 4);
                        }
                        ['5h','Week','Month'].forEach(function(k,i) { ctx.fillText(k,left+i*(right-left)/2-8,height-6) });
                        rows.forEach(function(p,index) {
                            ctx.strokeStyle = index % 2 ? ink : accent;
                            ctx.fillStyle = ctx.strokeStyle;
                            ctx.globalAlpha = 1 - (index % 4)*0.16;
                            ctx.setLineDash(index < 2 ? [] : [3+index*2,4]);
                            ctx.beginPath(); var active = false;
                            root.series(p).forEach(function(n,i) {
                                if (n < 0) { active = false; return; }
                                var x=left+i*(right-left)/2, y=bottom-Math.max(0,Math.min(100,n))/100*(bottom-top);
                                if (active) ctx.lineTo(x,y); else ctx.moveTo(x,y);
                                active=true;
                            }); ctx.stroke(); ctx.setLineDash([]);
                            root.series(p).forEach(function(n,i) { if(n>=0) {ctx.beginPath();ctx.arc(left+i*(right-left)/2,bottom-Math.max(0,Math.min(100,n))/100*(bottom-top),3,0,Math.PI*2);ctx.fill()} });
                        }); ctx.globalAlpha = 1;
                    }
                }
                Repeater {
                    model: root.providers
                    Row {
                        required property var modelData
                        required property int index
                        width: parent.width
                        spacing: Style.space(10)
                        Rectangle { width: Style.space(22); height: 3; anchors.verticalCenter: parent.verticalCenter; color: index % 2 ? Color.foreground : Color.accent; opacity: 1-(index%4)*0.16 }
                        Label { width: parent.width * 0.4; text: modelData.valueLabel + " · " + modelData.name }
                        Label { text: root.series(modelData).map(n => n < 0 ? '—' : Math.round(n)+'%').join('  /  '); color: root.secondary }
                    }
                }
                Label { width: parent.width; color: root.secondary; text: "5h / week / month headroom; missing limits have gaps. Each subscription has its own reset schedule. Capacity is separate from task value." }
            }
            Rectangle { width: parent.width; height: 1; color: root.secondary; opacity: 0.4 }
            Row {
                width: parent.width
                Label { width: parent.width * 0.35; text: "SUBSCRIPTION"; color: Color.accent }
                Label { width: parent.width * 0.22; text: "TASKS / $"; color: Color.accent }
                Label { width: parent.width * 0.22; text: "VALIDATED"; color: Color.accent }
                Label { width: parent.width * 0.21; text: "SPEND (USD)"; color: Color.accent }
            }
            Repeater {
                model: root.selected ? [root.selected] : root.providers
                Column {
                    required property var modelData
                    width: parent.width
                    spacing: Style.space(8)
                    Row {
                        width: parent.width
                        Label { width: parent.width * 0.35; text: modelData.valueLabel + " · " + modelData.name }
                        Label { width: parent.width * 0.22; text: root.value(modelData) }
                        Label { width: parent.width * 0.22; text: root.metric(modelData.valueStatus !== "unranked" ? modelData.economics.validated_tasks : null) }
                        Label { width: parent.width * 0.21; text: root.metric(modelData.valueStatus !== "unranked" ? modelData.economics.spend_usd : null) }
                    }
                    Label { width: parent.width; color: root.secondary; text: modelData.stats.todayText + (modelData.stats.todayText ? ' · ' : '') + (modelData.note || modelData.statusLabel || 'Provider quota available') }
                }
            }
            Column {
                visible: root.selected !== null
                width: parent.width
                spacing: Style.space(12)
                Label { text: "REQUEST HEALTH & COVERAGE"; color: Color.accent; font.bold: true }
                Label { width: parent.width; text: root.selected ? (root.selected.report.source || "No detailed provider report") + (root.selected.report.observed_at ? ' · '+root.selected.report.observed_at : '') : '' }
                Label { width: parent.width; color: root.secondary; text: root.selected ? (root.selected.report.note || "Detailed request health is unavailable for this source.") : '' }
                Label {
                    width: parent.width
                    visible: root.selected !== null && !!root.selected.report.coverage
                    color: Color.accent
                    text: {
                        if (!root.selected || !root.selected.report.coverage) return '';
                        var c = root.selected.report.coverage;
                        return (c.contains_truncated_export ? 'TRUNCATED EXPORT' : 'PARTIAL IMPORT')
                            + ' · ' + root.timestamp(c.first_started_at_ms)
                            + ' → ' + root.timestamp(c.last_started_at_ms);
                    }
                }
                Repeater {
                    model: root.selected ? Object.keys(root.selected.report.metrics || {}) : []
                    Row {
                        required property string modelData
                        width: parent.width
                        Label { width: parent.width * 0.65; text: modelData }
                        Label { text: root.metric(root.selected.report.metrics[modelData]) }
                    }
                }
                Repeater {
                    model: root.selected ? root.selected.report.models || [] : []
                    Label {
                        required property var modelData
                        width: parent.width
                        text: modelData.name + ' · ' + root.metric(modelData.requests) + ' requests · input ' + root.metric(modelData.input)
                            + ' · output ' + root.metric(modelData.output) + ' · cache read ' + root.metric(modelData.cache_read)
                    }
                }
                Label { text: "PUBLISHED DEALS"; color: Color.accent; font.bold: true }
                Label {
                    width: parent.width
                    color: root.secondary
                    text: "Dated provider observations, separate from measured spend. Recheck availability before routing; this view does not change routing or enable paid fallback."
                }
                Repeater {
                    model: root.selected ? root.selected.offers : []
                    Column {
                        required property var modelData
                        width: parent.width
                        spacing: Style.space(4)
                        Label {
                            width: parent.width
                            text: modelData.model + ' · ' + root.offerStatus(modelData)
                            color: Color.accent
                        }
                        Label { width: parent.width; text: modelData.description + ' ' + modelData.terms }
                        Label { width: parent.width; text: modelData.source; color: root.secondary }
                    }
                }
                Label { text: "LOCAL TRANSCRIPT TOKENS BY MODEL"; color: Color.accent; font.bold: true }
                Label { width: parent.width; color: root.secondary; text: root.selected ? root.selected.stats.sourceText || root.selected.stats.note : '' }
                Repeater {
                    model: root.selected ? root.selected.stats.models : []
                    Column {
                        required property var modelData
                        width: parent.width
                        spacing: Style.space(4)
                        Label { width: parent.width; text: modelData.id + ' · ' + modelData.tokenText }
                        Rectangle { width: parent.width; height: Style.space(4); color: Qt.alpha(Color.muted, 0.2); Rectangle { width: parent.width * modelData.share; height: parent.height; color: Color.accent } }
                        Label { width: parent.width; text: modelData.detailText; color: root.secondary; font.pixelSize: Style.font.caption }
                    }
                }
                Label {
                    width: parent.width
                    visible: root.selected !== null && root.selected.valueStatus !== 'unranked'
                    text: root.selected && root.selected.economics ? 'Period: ' + root.selected.economics.period + ' · Cohort: ' + root.selected.economics.cohort
                        + ' · Turns: ' + root.metric(root.selected.economics.turns) + ' · Reworked tasks: ' + root.metric(root.selected.economics.reworked_tasks)
                        + ' · Failed tasks: ' + root.metric(root.selected.economics.failed_tasks) : ''
                }
                Label { width: parent.width; text: "TASK OUTCOMES · Turns, validated completions and rework require an outcome ledger. Provider request success alone cannot supply these metrics."; color: root.secondary }
            }
        }
    }
}
