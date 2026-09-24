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
    signal modeSelected(string mode)
    signal priceSaved(string provider, string amount, string cycle)
    property string priceError: ""
    property string priceStatus: ""
    property bool savingPrice: false
    property bool showImportHelp: false
    readonly property bool activityMode: providers.length ? providers[0].rankingMetric === 'activity' : true
    readonly property var selected: providers.filter(p => p.id === selectedId)[0] || null
    property string editorProviderId: ""
    property bool priceDirty: false
    function loadPriceEditor() {
        // Resolve from the source array: selected's binding may still refer to the
        // previous tab while selectedIdChanged is being delivered.
        var row = providers.filter(p => p.id === selectedId)[0];
        var p = row && row.activity ? row.activity.price : null;
        editorProviderId = row ? row.id : "";
        priceInput.text = p ? String(p.amount_usd) : '';
        cycleInput.currentIndex = p && p.cycle === 'year' ? 1 : 0;
        priceDirty = false;
    }
    onSelectedIdChanged: loadPriceEditor()
    onProvidersChanged: {
        if (selectedId && !providers.some(p => p.id === selectedId)) selectedId = "";
        else if (!priceDirty) loadPriceEditor();
    }
    function savePriceEditor() {
        if (!savingPrice && editorProviderId && editorProviderId === selectedId
                && providers.some(p => p.id === editorProviderId)) {
            priceSaved(editorProviderId, priceInput.text, cycleInput.currentIndex ? 'year' : 'month');
        }
    }
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
    function value(p) { return p.valueScore === null || p.valueScore === undefined ? "—" : metric(p.valueScore) }
    function series(p) { return ['5h', 'week', 'month'].map(k => p.windows[k] ? p.windows[k].remainingPct : -1) }
    function cashText(n) { return n === null || n === undefined ? 'unknown' : '$' + Number(n).toFixed(2) }
    function importStatus(value, label) {
        var parts = []
        if (!value || !value.available) return label + ' import status unavailable' + (value && value.failed ? ' · refresh issue' : '')
        if (value.newCount !== null) parts.push(root.metric(value.newCount) + ' new')
        if (value.unchangedCount !== null && value.unchangedCount > 0) parts.push(root.metric(value.unchangedCount) + ' unchanged')
        if (value.rejectedCount !== null && value.rejectedCount > 0) parts.push(root.metric(value.rejectedCount) + ' rejected')
        if (value.pendingCount !== null && value.pendingCount > 0) parts.push(root.metric(value.pendingCount) + ' waiting')
        return label + ' import: ' + (parts.length ? parts.join(' · ') : 'no files queued')
    }
    function hasComparableActivityAndCash(rows) {
        var hasTurns = false, hasCash = false
        rows.forEach(function(p) {
            if (p.activity && typeof p.activity.observed_turns === 'number' && p.activity.observed_turns > 0) hasTurns = true
            if (p.billing && typeof p.billing.cashPaidUsd === 'number' && p.billing.cashPaidUsd > 0) hasCash = true
        })
        return hasTurns && hasCash
    }

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
            Row {
                spacing: Style.space(8)
                Action { title: "Observed turns / $ · provisional"; chosen: root.activityMode; onActivated: root.modeSelected('activity') }
                Action { title: "Validated tasks / $"; chosen: !root.activityMode; onActivated: root.modeSelected('validated') }
            }
            Label {
                width: parent.width
                text: root.selected ? root.selected.name + "  /  " + root.selected.plan : "All subscriptions · value comparison"
                font.pixelSize: Style.font.subtitle
            }
            Label {
                width: parent.width
                color: root.secondary
                text: root.activityMode
                    ? "* Ranked by observed turns over 30 days ÷ monthly fee. This measures utilization; validated task value needs more evidence."
                    : "Validated ranks require outcomes and recognized spend for the same period and workload cohort. Unknown evidence stays unranked; free usage is separate."
            }
            Rectangle { width: parent.width; height: 1; color: root.secondary; opacity: 0.4 }
            Row {
                width: parent.width
                Label { width: parent.width * 0.35; text: "SUBSCRIPTION"; color: Color.accent }
                Label { horizontalAlignment: Text.AlignRight; width: parent.width * 0.22; text: root.activityMode ? "TURNS / $ *" : "TASKS / $"; color: Color.accent }
                Label { horizontalAlignment: Text.AlignRight; width: parent.width * 0.22; text: root.activityMode ? "TURNS · 30D" : "VALIDATED"; color: Color.accent }
                Label { horizontalAlignment: Text.AlignRight; width: parent.width * 0.21; text: root.activityMode ? "USD / MONTH" : "SPEND (USD)"; color: Color.accent }
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
                        Label { horizontalAlignment: Text.AlignRight; width: parent.width * 0.22; text: root.value(modelData) }
                        Label { horizontalAlignment: Text.AlignRight; width: parent.width * 0.22; text: root.metric(root.activityMode ? modelData.activity.observed_turns : (modelData.economics && modelData.valueStatus !== "unranked" ? modelData.economics.validated_tasks : null)) }
                        Label { horizontalAlignment: Text.AlignRight; width: parent.width * 0.21; text: root.cashText(root.activityMode ? (modelData.activity.price ? modelData.activity.price.monthly_usd : null) : (modelData.economics && modelData.valueStatus !== "unranked" ? modelData.economics.spend_usd : null)) }
                    }
                    Label { visible: root.selected !== null; width: parent.width; color: root.secondary; text: root.activityMode ? (modelData.activity.price ? modelData.activity.price.basis + ' · ' + root.metric(modelData.activity.pi_turns) + ' Pi turns included' : modelData.activity.reason || 'No local activity data') : modelData.economicsReason }
                    Rectangle { width: parent.width; height: 1; color: Qt.alpha(Color.muted, 0.25) }
                }
            }
            Label { visible: !root.selected; width: parent.width; color: root.secondary; text: root.activityMode ? "Fees use saved prices or invoice amounts; annual plans are shown per month. Open a subscription for its source." : "Only comparable tasks and spend receive a validated rank." }
            Column {
                visible: !root.selected
                width: parent.width
                spacing: Style.space(8)
                Label { text: "DATA COVERAGE"; color: Color.accent; font.bold: true }
                Label { width: parent.width; color: root.secondary; text: "What is available for this comparison. Captured records may cover only part of the period." }
                Row {
                    width: parent.width
                    spacing: Style.space(12)
                    readonly property real cellWidth: (width - 3 * spacing) / 4
                    Label { width: parent.cellWidth; text: "SUBSCRIPTION"; color: root.secondary; font.pixelSize: Style.font.caption }
                    Label { width: parent.cellWidth; text: "REQUEST REPORT"; color: root.secondary; font.pixelSize: Style.font.caption }
                    Label { width: parent.cellWidth; text: "TASK VALIDATION"; color: root.secondary; font.pixelSize: Style.font.caption }
                    Label { width: parent.cellWidth; text: "BILLING"; color: root.secondary; font.pixelSize: Style.font.caption }
                }
                Repeater {
                    model: root.providers
                    Column {
                        required property var modelData
                        width: parent.width
                        spacing: Style.space(8)
                        Row {
                            width: parent.width
                            spacing: Style.space(12)
                            readonly property real cellWidth: (width - 3 * spacing) / 4
                            Label { width: parent.cellWidth; text: modelData.name; font.bold: true }
                            Label { width: parent.cellWidth; text: !modelData.report.source ? "Missing" : modelData.report.coverage ? "Partial report" : "Snapshot"; color: modelData.report.source ? Color.foreground : root.secondary }
                            Label { width: parent.cellWidth; text: modelData.outcomes.available ? root.metric(modelData.outcomes.counts.validated) + " validated" : "Not tracked"; color: modelData.outcomes.available ? Color.foreground : root.secondary }
                            Label { width: parent.cellWidth; text: modelData.billing.available ? modelData.billing.invoices.length + " receipt" + (modelData.billing.invoices.length === 1 ? "" : "s") : "Missing"; color: modelData.billing.available ? Color.foreground : root.secondary }
                        }
                        Rectangle { width: parent.width; height: 1; color: Qt.alpha(Color.muted, 0.25) }
                    }
                }
                Row {
                    width: parent.width
                    spacing: Style.space(8)
                    Label {
                        width: parent.width - importHelpAction.implicitWidth - Style.space(8)
                        color: root.secondary
                        text: root.providers.length
                            ? root.importStatus(root.providers[0].reportImport, 'Report') + '  ·  '
                                + root.importStatus(root.providers[0].outcomeImport, 'Outcome')
                            : 'Import status unavailable'
                    }
                    Action {
                        id: importHelpAction
                        title: root.showImportHelp ? 'Hide import help' : 'Import help'
                        onActivated: root.showImportHelp = !root.showImportHelp
                    }
                }
                Label {
                    width: parent.width
                    visible: root.showImportHelp
                    color: root.secondary
                    text: "OpenCode JSON/Console CSV exports: copy into ~/.local/state/tmos-ai-usage/imports/ and refresh (with --state-dir, use that directory's imports/). For billing, run collector/billing_ledger.py --invoice <record.json> --evidence <private-receipt>; keep source material local. For outcome capture, use collector/task_runner.py run/verify/accept or put event JSONL in outcome-events/. Rejected and waiting counts above identify import problems without showing filenames or paths."
                }
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
                        var left = 38, right = width - 45, top = 20, bottom = height - 30;
                        ctx.font = String(Style.font.caption) + 'px ' + Style.font.family;
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
            Column {
                visible: !root.selected && root.hasComparableActivityAndCash(root.providers)
                width: parent.width
                spacing: Style.space(6)
                Label { text: "OBSERVED TURNS AND CASH PAID · INDEPENDENT SCALES"; color: Color.accent; font.bold: true }
                Label { width: parent.width; color: root.secondary; text: "Bars show captured totals only. Turns use the left scale; cash paid uses the right scale. Each value carries its own observation window; these totals do not establish cost per turn and billing does not affect rankings." }
                Canvas {
                    id: activityCashChart
                    width: parent.width
                    height: Math.max(Style.space(100), root.providers.length * Style.space(28) + Style.space(24))
                    property var rows: root.providers
                    property color ink: Color.foreground
                    property color accent: Color.accent
                    property color muted: root.secondary
                    onRowsChanged: requestPaint()
                    onWidthChanged: requestPaint()
                    onPaint: {
                        var ctx = getContext('2d'); ctx.reset();
                        var turns = rows.map(p => p.activity && typeof p.activity.observed_turns === 'number' ? p.activity.observed_turns : null);
                        var cash = rows.map(p => p.billing && typeof p.billing.cashPaidUsd === 'number' ? p.billing.cashPaidUsd : null);
                        var maxTurns = 0, maxCash = 0;
                        turns.forEach(function(n) { if (n !== null && n > maxTurns) maxTurns = n; });
                        cash.forEach(function(n) { if (n !== null && n > maxCash) maxCash = n; });
                        var left = 135, right = width - 115, rowH = Style.space(28), top = 12;
                        ctx.font = String(Style.font.caption) + 'px ' + Style.font.family;
                        ctx.fillStyle = muted; ctx.fillText('turns · max ' + (maxTurns || '—'), left, 10);
                        ctx.fillText('cash · max $' + (maxCash ? maxCash.toFixed(2) : '—'), right + 8, 10);
                        rows.forEach(function(p, i) {
                            var y = top + i * rowH + 8;
                            ctx.fillStyle = ink; ctx.fillText(p.name, 0, y + 10);
                            if (turns[i] !== null && maxTurns > 0) {
                                ctx.fillStyle = accent; ctx.fillRect(left, y, Math.max(2, (right-left) * turns[i] / maxTurns), 7);
                                ctx.fillStyle = ink; ctx.fillText(String(turns[i]) + ' / ' + (p.activity.window_days || '?') + 'd', right + 8, y + 7);
                            } else { ctx.fillStyle = muted; ctx.fillText('—', right + 8, y + 7); }
                            if (cash[i] !== null && maxCash > 0) {
                                ctx.fillStyle = ink; ctx.globalAlpha = 0.72;
                                ctx.fillRect(left, y + 10, Math.max(2, (right-left) * cash[i] / maxCash), 5);
                                ctx.globalAlpha = 1; ctx.fillStyle = ink; ctx.fillText('$' + cash[i].toFixed(2), right + 8, y + 17);
                            }
                        });
                    }
                }
                Label { width: parent.width; color: root.secondary; text: "Upper bar: observed turns (count / observed days). Lower bar: cash paid (USD) in the billing window. Separate scales and potentially different windows; do not read these as a cost comparison." }
            }
            Column {
                visible: root.selected !== null
                width: parent.width
                spacing: Style.space(12)
                Label { text: "SUBSCRIPTION PRICE"; color: Color.accent; font.bold: true }
                Label {
                    width: parent.width
                    color: root.secondary
                    text: root.selected && root.selected.activity.price ? root.selected.activity.price.basis + ' · ' + root.selected.activity.price.source : 'Enter your actual subscription fee to include this provider in the provisional comparison.'
                }
                Row {
                    spacing: Style.space(8)
                    TextField {
                        id: priceInput
                        objectName: "subscriptionPriceInput"
                        width: Style.space(140)
                        placeholderText: "USD amount"
                        onTextEdited: root.priceDirty = true
                        color: Color.foreground
                        placeholderTextColor: root.secondary
                        selectionColor: Color.accent
                        selectedTextColor: Color.background
                        font.family: Style.font.family
                        background: Rectangle { color: Color.background; border.color: Color.muted }
                    }
                    Action {
                        id: cycleInput
                        objectName: "subscriptionCycleInput"
                        property int currentIndex: 0
                        title: currentIndex ? "Annual ↔" : "Monthly ↔"
                        onActivated: { currentIndex = currentIndex ? 0 : 1; root.priceDirty = true }
                    }
                    Action {
                        title: root.savingPrice ? 'Saving…' : 'Save actual fee'
                        onActivated: root.savePriceEditor()
                    }
                }
                Label { width: parent.width; visible: root.priceStatus !== "" && root.priceError === ""; text: root.priceStatus; color: Color.accent }
                Label { width: parent.width; visible: root.priceError !== ''; text: root.priceError; color: Color.urgent }
                Label {
                    width: parent.width
                    color: root.secondary
                    text: root.selected ? (root.selected.activity.source || 'No local transcript source')
                        + ' · observed range ' + (root.selected.activity.first_date || 'unknown') + ' to ' + (root.selected.activity.last_date || 'unknown')
                        + ' · ' + root.metric(root.selected.activity.observed_sessions) + ' local sessions · Pi turns: ' + root.metric(root.selected.activity.pi_turns) + '. Other harnesses may be missing.' : ''
                }
                Label { text: "REQUEST HEALTH & COVERAGE"; color: Color.accent; font.bold: true }
                Label { width: parent.width; text: root.selected ? (root.selected.report.source || "No detailed provider report") + (root.selected.report.observed_at ? ' · '+root.selected.report.observed_at : '') : '' }
                Label { width: parent.width; color: root.secondary; text: root.selected ? (root.selected.report.note || "Detailed request health is unavailable for this source.") : '' }
                Label {
                    width: parent.width
                    color: root.secondary
                    text: root.selected
                        ? root.importStatus(root.selected.reportImport, 'Report') + '  ·  '
                            + root.importStatus(root.selected.outcomeImport, 'Outcome')
                        : ''
                }
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
                Label { text: "BILLING HISTORY · OBSERVED ONLY"; color: Color.accent; font.bold: true }
                Label {
                    width: parent.width
                    color: root.secondary
                    text: root.selected && root.selected.billing.available
                        ? 'Cash paid: ' + root.cashText(root.selected.billing.cashPaidUsd)
                            + ' · known service allocation: ' + root.cashText(root.selected.billing.allocatedKnownUsd)
                            + ' · ' + root.metric(root.selected.billing.unknownPeriods) + ' payment(s) with unknown service period'
                            + (root.selected.billing.periodStart ? ' · ' + root.selected.billing.periodStart + ' → ' + root.selected.billing.periodEnd : '')
                            + ' · coverage incomplete'
                        : 'Billing history unavailable; no amount is assumed.'
                }
                Label { width: parent.width; color: root.secondary; text: root.selected && root.selected.billing.available ? root.selected.billing.note : '' }
                Repeater {
                    model: root.selected && root.selected.billing.available ? root.selected.billing.invoices : []
                    Column {
                        required property var modelData
                        width: parent.width
                        spacing: Style.space(2)
                        Label { width: parent.width; text: modelData.paidAt + ' · paid ' + root.cashText(modelData.paidUsd) + ' · base ' + root.cashText(modelData.baseUsd) + ' · discount ' + root.cashText(modelData.discountUsd) + ' · tax ' + root.cashText(modelData.taxUsd) + ' · fees ' + root.cashText(modelData.feesUsd) }
                        Label { width: parent.width; color: root.secondary; text: modelData.serviceStart && modelData.serviceEnd ? 'Service period: ' + modelData.serviceStart + ' → ' + modelData.serviceEnd : 'Service period unknown' }
                    }
                }
                Label {
                    width: parent.width
                    color: root.secondary
                    text: "Private receipt import: keep the original source receipt locally, prepare its invoice record, then run python3 <tmos.usage>/collector/billing_ledger.py --state-dir ~/.local/state/tmos-ai-usage --invoice <invoice-record.json> --evidence <private-source-receipt>. Enter processing fees in fees_usd separately from tax. The private ledger retains evidence; this view shows only approved billing fields."
                }
                Column {
                    width: parent.width
                    spacing: Style.space(6)
                    visible: root.selected !== null && !!root.selected.consoleReport.note
                    Label { text: "CONSOLE WORKSPACE REPORT"; color: Color.accent; font.bold: true }
                    Label { width: parent.width; color: root.secondary; text: root.selected ? root.selected.consoleReport.note || '' : '' }
                    Label { width: parent.width; text: root.selected ? 'Observed: ' + (root.selected.consoleReport.observed_at || 'unavailable') : '' }
                    Repeater {
                        model: root.selected ? Object.keys(root.selected.consoleReport.metrics || {}) : []
                        Row {
                            required property string modelData
                            width: parent.width
                            Label { width: parent.width * 0.65; text: modelData }
                            Label { text: root.metric(root.selected.consoleReport.metrics[modelData]) }
                        }
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
                Label { text: "TASK OUTCOMES · OBSERVED LEDGER COUNTS"; color: Color.accent; font.bold: true }
                Label {
                    width: parent.width
                    color: root.secondary
                    text: root.selected && root.selected.outcomes.available
                        ? root.selected.outcomes.coverageText
                            + (root.selected.outcomes.interval ? ' · ' + root.selected.outcomes.interval.start + ' → ' + root.selected.outcomes.interval.end : '')
                        : "No outcome ledger summary is available. Provider request success does not establish task validation."
                }
                Label {
                    width: parent.width
                    visible: root.selected !== null && root.selected.outcomes.available
                    text: root.selected && root.selected.outcomes.available
                        ? 'Tasks ' + root.metric(root.selected.outcomes.counts.tasks) + ' · pending ' + root.metric(root.selected.outcomes.counts.pending)
                            + ' · validated ' + root.metric(root.selected.outcomes.counts.validated) + ' · failed ' + root.metric(root.selected.outcomes.counts.failed)
                            + ' · abandoned ' + root.metric(root.selected.outcomes.counts.abandoned) + ' · reworked ' + root.metric(root.selected.outcomes.counts.reworked)
                            + ' · turns ' + root.metric(root.selected.outcomes.counts.turns) + ' · errors ' + root.metric(root.selected.outcomes.counts.errors)
                        : ''
                }
                Repeater {
                    model: root.selected && root.selected.outcomes.available ? root.selected.outcomes.cohorts : []
                    Label {
                        required property var modelData
                        width: parent.width
                        color: root.secondary
                        text: modelData.name + ' · tasks ' + root.metric(modelData.counts.tasks)
                            + ' · validated ' + root.metric(modelData.counts.validated) + ' · failed ' + root.metric(modelData.counts.failed)
                            + ' · turns ' + root.metric(modelData.counts.turns)
                    }
                }
                Label { width: parent.width; color: root.secondary; text: root.selected && root.selected.outcomes.available ? "Observed ledger events are incomplete population coverage. Unrecorded tasks may be missing; these counts do not assert complete coverage." : "No outcome ledger summary is available. Record task lifecycle and turn events with outcome_ledger.py, then refresh the usage report. No counts are inferred from request success." }
            }
        }
    }
}
