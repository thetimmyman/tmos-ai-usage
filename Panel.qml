// Panel.qml — the surface: the whole dashboard, and which row is open.
//
// Loaded by BarWidget.qml (the manifest entry point) through a Loader, which is the contract the
// Omarchy shell expects for a bar widget with a panel: the bar entry owns the IPC lifecycle and
// forwards open()/close()/toggle() here, injecting the bar, the anchor item and the host widget.
// The data stays in BarWidget.qml (one UsageSource, one clock), and this file reads it back through
// `hostWidget`, so nothing is collected or watched twice.
//
// Layout rule this file exists to keep: EVERY subscription is on the first page, in the same order,
// with no switcher. A row expands underneath itself for depth; opening one never hides another.
//
// Every colour, size and font here is a qs.Commons token (gate N3), and tone names come from
// Model.js so the arithmetic stays out of the view. No network, no credentials, no endpoints in
// this process.
import QtQuick
import QtQuick.Controls
import Quickshell
import qs.Commons
import qs.Ui as Ui
import "Model.js" as Model

Ui.Panel {
    id: root

    moduleName: "tmos.usage"
    // The bar entry point owns the IPC lifecycle; the shell routes summon/hide to BarWidget.qml,
    // which forwards them here.
    manageIpc: false

    property var anchorItem: null
    property var hostWidget: null

    readonly property var providers: hostWidget ? hostWidget.providers : []
    readonly property var totals: hostWidget ? hostWidget.totals : Model.emptyTotals()
    readonly property bool stale: hostWidget ? hostWidget.stale : false
    readonly property double nowMs: hostWidget ? hostWidget.nowMs : 0
    readonly property int warnHeadroomPct: hostWidget ? hostWidget.warnHeadroomPct : 35
    readonly property int lowHeadroomPct: hostWidget ? hostWidget.lowHeadroomPct : 15

    // Scalar aliases with type-safe defaults: a `Text` or a `bool` must never be handed `undefined`,
    // which is a warning in the shell log even for a section that is not drawn.
    readonly property bool refreshing: hostWidget ? hostWidget.usageSource.refreshing : false
    readonly property double observedAt: hostWidget ? hostWidget.usageSource.observedAt : 0
    readonly property string cachePath: hostWidget ? hostWidget.usageSource.cachePath : ""
    readonly property string loadError: hostWidget ? hostWidget.usageSource.loadError : ""
    readonly property string refreshError: hostWidget ? hostWidget.usageSource.refreshError : ""

    readonly property color foreground: bar ? bar.barForeground : Color.foreground
    readonly property color dim: Qt.darker(root.foreground, 1.35)
    readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

    // Which provider's depth is open, by id ("" = none). One at a time: the point of the panel is
    // the list, and two open rows would push the other subscriptions off the page.
    property string expandedId: ""

    // ---------------------------------------------------------------- companions
    //
    // TMOS plugins stand alone: the marketplace has no dependency mechanism and `omarchy plugin add`
    // takes one repository, so a companion is never *required*. This is the honest version of that —
    // a notice that appears ONLY when a companion would add a capability this plugin does not have
    // itself, and only while that companion is not installed.
    //
    // The list stays empty until such a companion exists. A decorative "install our other plugin"
    // line is advertising, not information, and it ages badly. When one lands, add:
    //
    //   { "id": "tmos.cost", "name": "TMOS Cost", "why": "adds cost per model and cheaper-provider
    //     suggestions", "url": "https://github.com/thetimmyman/tmos-cost", "detect": "~/.local/state/tmos/cost.json" }
    //
    // `detect` is a path the companion writes; a path that cannot be read means "not installed".
    // `url` may be "" while a companion is unreleased, and the notice then says "link TBD".
    readonly property var companions: []

    function open() { root.controller.show() }
    function close() { root.controller.hide() }
    function toggle() { if (root.opened) root.close(); else root.open() }

    function switchPanel(direction) {
        if (root.bar && typeof root.bar.switchPanelFrom === "function")
            return root.bar.switchPanelFrom(root.hostWidget || root, direction)
        return false
    }

    function refresh() { if (root.hostWidget) root.hostWidget.refresh() }

    // One token per tone. Omarchy publishes foreground/accent/urgent/muted; "warn" borrows accent
    // rather than inventing a colour the theme never chose.
    function toneColor(tone) {
        if (tone === "urgent") return Color.urgent;
        if (tone === "warn") return Color.accent;
        if (tone === "muted") return root.dim;
        return root.foreground;
    }

    function clamp01(value) {
        var n = Number(value);
        if (!isFinite(n)) return 0;
        return Math.max(0, Math.min(1, n));
    }

    function alpha(color, a) { return Qt.rgba(color.r, color.g, color.b, a) }

    function hasProvider(id) {
        for (var i = 0; i < root.providers.length; i++) if (root.providers[i].id === id) return true;
        return false;
    }

    function toggleExpanded(id) {
        if (!id) return;
        root.expandedId = (root.expandedId === id) ? "" : id;
    }

    // j/k walk the list, opening each row as it goes, so the keyboard can read the panel without a
    // pointer. There is deliberately no left/right action: nothing gets switched away from.
    function moveExpansion(delta) {
        var i, current, next;
        if (root.providers.length === 0) return;
        current = -1;
        for (i = 0; i < root.providers.length; i++) if (root.providers[i].id === root.expandedId) current = i;
        next = current < 0 ? (delta > 0 ? 0 : root.providers.length - 1) : (current + delta);
        next = Math.max(0, Math.min(root.providers.length - 1, next));
        root.expandedId = root.providers[next].id;
    }

    // Enter opens the selected row rather than toggling it: a key that closes what you were reading
    // is a trap, and the shell delivers activation on plain Return.
    function toggleCurrent() {
        if (root.providers.length === 0) return;
        if (root.expandedId === "") root.expandedId = root.providers[0].id;
        else root.moveExpansion(1);
    }

    onProvidersChanged: if (expandedId !== "" && !root.hasProvider(expandedId)) expandedId = ""

    onOpenedChanged: if (opened) {
        if (root.hostWidget) root.hostWidget.touch();
        if (root.stale) root.refresh();
        Qt.callLater(function() { catcher.forceActiveFocus() });
    }

    Ui.KeyboardPanel {
        id: panel
        anchorItem: root.anchorItem
        owner: root.hostWidget || root
        bar: root.bar
        open: root.opened
        focusTarget: catcher
        // Wide enough for three window chips and a chevron on one line; tall enough that all five
        // subscriptions are on screen at once with no scrolling (the cap only bites once a row is
        // expanded, and then the expansion is what scrolls).
        contentWidth: panel.fittedContentWidth(Style.space(430))
        contentHeight: panel.fittedContentHeight(body.implicitHeight, Style.space(780))

        Ui.PanelKeyCatcher {
            id: catcher
            anchors.fill: parent
            onCloseRequested: root.close()
            onTextKey: function(text) {
                if (text === "r" || text === "R") root.refresh();
                else if (text === "j" || text === "J") root.moveExpansion(1);
                else if (text === "k" || text === "K") root.moveExpansion(-1);
            }
            onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveExpansion(dy) }
            onActivateRequested: root.toggleCurrent()
            onTabRequested: function(direction) { root.switchPanel(direction) }

            ScrollView {
                id: scroll
                anchors.fill: parent
                clip: true
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: body.implicitHeight > height ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff

                Column {
                    id: body
                    width: scroll.availableWidth
                    spacing: Style.space(8)

                    // ---- header: what this is, and how fresh it is -------------------------
                    Item {
                        width: parent.width
                        implicitHeight: Math.max(headerTitle.implicitHeight, headerMeta.implicitHeight)

                        Text {
                            id: headerTitle
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            textFormat: Text.PlainText
                            text: "AI budget"
                            color: root.foreground
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.subtitle
                            font.bold: true
                        }

                        Text {
                            id: headerMeta
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            textFormat: Text.PlainText
                            text: (root.refreshing ? "reading…" : Model.ageText(root.observedAt, root.nowMs)) + " · R refresh"
                                
                            color: root.stale && !root.refreshing ? Color.accent : root.dim
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.caption
                        }
                    }

                    // ---- the whole machine in one line, when any provider has history ------
                    Text {
                        width: parent.width
                        visible: root.totals.available && root.totals.text !== ""
                        textFormat: Text.PlainText
                        text: root.totals.text
                        color: root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                    }

                    Text {
                        width: parent.width
                        visible: root.loadError !== "" || root.refreshError !== ""
                        textFormat: Text.PlainText
                        text: root.refreshError !== "" ? root.refreshError : root.loadError
                        color: Color.urgent
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        wrapMode: Text.WordWrap
                    }

                    Text {
                        width: parent.width
                        visible: root.providers.length === 0
                        textFormat: Text.PlainText
                        text: "No reading yet. The collector runs a few seconds after the shell starts, then every five minutes — press R to refresh now."
                        color: root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        wrapMode: Text.WordWrap
                    }

                    Ui.PanelSeparator { width: parent.width; foreground: root.foreground }

                    // ---- every subscription, on the page at once --------------------------
                    Repeater {
                        model: root.providers

                        delegate: Column {
                            id: providerRow
                            required property var modelData
                            readonly property var provider: providerRow.modelData
                            readonly property bool expanded: root.expandedId === providerRow.provider.id
                            width: body.width
                            spacing: Style.spacing.xs

                            // The row surface owns the click and the hover. It is a plain
                            // MouseArea because it sits inside the panel, where the bar's
                            // registered-click-target rule does not apply.
                            Rectangle {
                                id: rowSurface
                                width: parent.width
                                height: rowContent.implicitHeight + Style.spacing.sm * 2
                                radius: Style.cornerRadius
                                color: providerRow.expanded
                                    ? Style.selectedFill
                                    : (rowHover.containsMouse ? Style.normalFill : "transparent")

                                MouseArea {
                                    id: rowHover
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    onClicked: root.toggleExpanded(providerRow.provider.id)
                                }

                                Column {
                                    id: rowContent
                                    anchors.fill: parent
                                    anchors.margins: Style.spacing.sm
                                    spacing: Style.spacing.xs

                                    // name · plan ........................................ today · status · chevron
                                    Item {
                                        width: parent.width
                                        implicitHeight: Math.max(providerName.implicitHeight, rightCluster.implicitHeight)

                                        Text {
                                            id: providerName
                                            anchors.left: parent.left
                                            anchors.right: rightCluster.left
                                            anchors.rightMargin: Style.spacing.sm
                                            anchors.verticalCenter: parent.verticalCenter
                                            textFormat: Text.PlainText
                                            text: providerRow.provider.name + (providerRow.provider.plan ? ("  " + providerRow.provider.plan) : "")
                                            color: root.foreground
                                            font.family: root.fontFamily
                                            font.pixelSize: Style.font.body
                                            font.bold: true
                                            elide: Text.ElideRight
                                        }

                                        Row {
                                            id: rightCluster
                                            anchors.right: parent.right
                                            anchors.verticalCenter: parent.verticalCenter
                                            spacing: Style.spacing.sm

                                            // Today's activity belongs on the collapsed row: it is the
                                            // one number that changes during the day.
                                            Text {
                                                textFormat: Text.PlainText
                                                text: providerRow.provider.stats.available ? providerRow.provider.stats.todayText : ""
                                                visible: text !== ""
                                                color: root.dim
                                                font.family: root.fontFamily
                                                font.pixelSize: Style.font.caption
                                            }

                                            Text {
                                                textFormat: Text.PlainText
                                                text: providerRow.provider.statusLabel
                                                visible: text !== ""
                                                color: root.toneColor(providerRow.provider.statusTone)
                                                font.family: root.fontFamily
                                                font.pixelSize: Style.font.caption
                                            }

                                            Text {
                                                textFormat: Text.PlainText
                                                text: providerRow.expanded ? "▾" : "▸"
                                                color: root.dim
                                                font.family: root.fontFamily
                                                font.pixelSize: Style.font.caption
                                            }
                                        }
                                    }

                                    // Three windows, always in the same order and on ONE line, so the eye
                                    // can compare providers down a column without opening anything. A
                                    // window a provider does not publish stays a dash, never a full bar.
                                    Row {
                                        id: chipRow
                                        width: parent.width
                                        spacing: Style.spacing.sm
                                        readonly property real chipWidth: (width - spacing * 2) / 3

                                        Repeater {
                                            model: ["5h", "week", "month"]

                                            delegate: Item {
                                                id: chip
                                                required property string modelData
                                                readonly property var win: providerRow.provider.windows[chip.modelData]
                                                readonly property string tone: chip.win
                                                    ? Model.tone(chip.win.remainingPct, root.warnHeadroomPct, root.lowHeadroomPct)
                                                    : "muted"
                                                width: chipRow.chipWidth
                                                implicitHeight: Math.max(chipLabel.implicitHeight, Style.space(12))

                                                Text {
                                                    id: chipLabel
                                                    anchors.left: parent.left
                                                    anchors.verticalCenter: parent.verticalCenter
                                                    width: Style.space(38)
                                                    textFormat: Text.PlainText
                                                    text: chip.win ? chip.win.label : Model.WINDOW_LABEL[chip.modelData]
                                                    color: root.dim
                                                    font.family: root.fontFamily
                                                    font.pixelSize: Style.font.caption
                                                    elide: Text.ElideRight
                                                }

                                                // The meter shows what is LEFT, because that is the
                                                // question being asked: "how much have I got?"
                                                Rectangle {
                                                    id: chipTrack
                                                    anchors.left: chipLabel.right
                                                    anchors.right: chipPct.left
                                                    anchors.leftMargin: Style.space(4)
                                                    anchors.rightMargin: Style.space(4)
                                                    anchors.verticalCenter: parent.verticalCenter
                                                    height: Style.space(4)
                                                    radius: height / 2
                                                    color: Style.normalFill
                                                    visible: !!chip.win

                                                    Rectangle {
                                                        anchors.left: parent.left
                                                        anchors.verticalCenter: parent.verticalCenter
                                                        height: parent.height
                                                        radius: parent.radius
                                                        width: chip.win ? parent.width * root.clamp01(chip.win.remainingPct / 100) : 0
                                                        color: root.toneColor(chip.tone)
                                                    }
                                                }

                                                Text {
                                                    id: chipPct
                                                    anchors.right: parent.right
                                                    anchors.verticalCenter: parent.verticalCenter
                                                    width: Style.space(34)
                                                    horizontalAlignment: Text.AlignRight
                                                    textFormat: Text.PlainText
                                                    text: Model.percentText(chip.win)
                                                    color: root.toneColor(chip.tone)
                                                    font.family: root.fontFamily
                                                    font.pixelSize: Style.font.caption
                                                }
                                            }
                                        }
                                    }
                                }
                            }

                            // ---- the expansion: depth for the row you opened --------------
                            Column {
                                id: detail
                                visible: providerRow.expanded
                                width: parent.width
                                spacing: Style.spacing.sm

                                // Limits, full width, with the reset countdown the chips cannot fit.
                                Repeater {
                                    model: ["5h", "week", "month"]

                                    delegate: Item {
                                        id: windowRow
                                        required property string modelData
                                        readonly property var win: providerRow.provider.windows[windowRow.modelData]
                                        readonly property string tone: windowRow.win
                                            ? Model.tone(windowRow.win.remainingPct, root.warnHeadroomPct, root.lowHeadroomPct)
                                            : "muted"

                                        width: detail.width
                                        implicitHeight: Math.max(windowLabel.implicitHeight, Style.space(14))
                                        visible: providerRow.provider.hasWindows

                                        Text {
                                            id: windowLabel
                                            anchors.left: parent.left
                                            anchors.verticalCenter: parent.verticalCenter
                                            width: Style.space(40)
                                            textFormat: Text.PlainText
                                            text: windowRow.win ? windowRow.win.label : Model.WINDOW_LABEL[windowRow.modelData]
                                            color: root.dim
                                            font.family: root.fontFamily
                                            font.pixelSize: Style.font.caption
                                        }

                                        Rectangle {
                                            id: track
                                            anchors.left: windowLabel.right
                                            anchors.leftMargin: Style.space(4)
                                            anchors.verticalCenter: parent.verticalCenter
                                            width: Math.max(Style.space(60), parent.width - windowLabel.width - percentLabel.width - resetLabel.width - Style.space(20))
                                            height: Style.space(4)
                                            radius: height / 2
                                            color: Style.normalFill
                                            visible: !!windowRow.win

                                            Rectangle {
                                                anchors.left: parent.left
                                                anchors.verticalCenter: parent.verticalCenter
                                                height: parent.height
                                                radius: parent.radius
                                                width: windowRow.win ? parent.width * root.clamp01(windowRow.win.remainingPct / 100) : 0
                                                color: root.toneColor(windowRow.tone)
                                            }
                                        }

                                        Text {
                                            id: percentLabel
                                            anchors.right: resetLabel.left
                                            anchors.rightMargin: Style.space(6)
                                            anchors.verticalCenter: parent.verticalCenter
                                            textFormat: Text.PlainText
                                            text: Model.percentText(windowRow.win)
                                            color: root.toneColor(windowRow.tone)
                                            font.family: root.fontFamily
                                            font.pixelSize: Style.font.caption
                                        }

                                        Text {
                                            id: resetLabel
                                            anchors.right: parent.right
                                            anchors.verticalCenter: parent.verticalCenter
                                            width: Style.space(96)
                                            horizontalAlignment: Text.AlignRight
                                            textFormat: Text.PlainText
                                            text: Model.resetText(windowRow.win)
                                            color: root.dim
                                            font.family: root.fontFamily
                                            font.pixelSize: Style.font.caption
                                            elide: Text.ElideRight
                                        }
                                    }
                                }

                                // Prepaid ledger, only for a provider that has one. The meter drains
                                // toward empty, the opposite of a limit filling toward its cap.
                                Column {
                                    id: balanceSection
                                    visible: providerRow.provider.balance.available
                                    width: parent.width
                                    spacing: Style.spacing.xs

                                    Item {
                                        width: parent.width
                                        implicitHeight: Math.max(balanceLabel.implicitHeight, balanceValue.implicitHeight)

                                        Text {
                                            id: balanceLabel
                                            anchors.left: parent.left
                                            anchors.verticalCenter: parent.verticalCenter
                                            textFormat: Text.PlainText
                                            text: "Prepaid credits"
                                            color: root.foreground
                                            font.family: root.fontFamily
                                            font.pixelSize: Style.font.body
                                        }

                                        Text {
                                            id: balanceValue
                                            anchors.right: parent.right
                                            anchors.verticalCenter: parent.verticalCenter
                                            textFormat: Text.PlainText
                                            text: providerRow.provider.balance.remainingText
                                            color: providerRow.provider.balance.alarming ? Color.urgent : root.foreground
                                            font.family: root.fontFamily
                                            font.pixelSize: Style.font.body
                                            font.bold: true
                                        }
                                    }

                                    Rectangle {
                                        width: parent.width
                                        height: Style.space(4)
                                        radius: height / 2
                                        color: Style.normalFill
                                        visible: providerRow.provider.balance.ratio >= 0

                                        Rectangle {
                                            anchors.left: parent.left
                                            anchors.verticalCenter: parent.verticalCenter
                                            height: parent.height
                                            radius: parent.radius
                                            width: parent.width * root.clamp01(providerRow.provider.balance.ratio)
                                            color: providerRow.provider.balance.alarming ? Color.urgent : root.foreground
                                        }
                                    }

                                    Text {
                                        width: parent.width
                                        textFormat: Text.PlainText
                                        text: providerRow.provider.balance.detailText
                                        visible: text !== ""
                                        color: root.dim
                                        font.family: root.fontFamily
                                        font.pixelSize: Style.font.caption
                                    }
                                }

                                // Token history, only when TMOS reads a transcript for this provider.
                                Column {
                                    id: historySection
                                    visible: providerRow.provider.stats.available
                                    width: parent.width
                                    spacing: Style.spacing.xs

                                    Ui.PanelSectionHeader {
                                        width: parent.width
                                        text: "TOKENS BY DAY"
                                        foreground: root.foreground
                                        fontFamily: root.fontFamily
                                    }

                                    Repeater {
                                        model: providerRow.provider.stats.days

                                        delegate: DayBar {
                                            required property var modelData
                                            width: historySection.width
                                            day: modelData
                                        }
                                    }
                                }

                                Column {
                                    id: modelSection
                                    visible: providerRow.provider.stats.available && providerRow.provider.stats.models.length > 0
                                    width: parent.width
                                    spacing: Style.spacing.xs

                                    Ui.PanelSectionHeader {
                                        width: parent.width
                                        text: "TOKENS BY MODEL"
                                        foreground: root.foreground
                                        fontFamily: root.fontFamily
                                    }

                                    Repeater {
                                        model: providerRow.provider.stats.models.slice(0, 4)

                                        delegate: ModelBar {
                                            required property var modelData
                                            width: modelSection.width
                                            row: modelData
                                        }
                                    }
                                }

                                // Where the history came from, or why there is none.
                                Text {
                                    width: parent.width
                                    textFormat: Text.PlainText
                                    text: providerRow.provider.stats.available
                                        ? providerRow.provider.stats.sourceText
                                        : providerRow.provider.stats.note
                                    visible: text !== ""
                                    color: root.dim
                                    font.family: root.fontFamily
                                    font.pixelSize: Style.font.caption
                                    wrapMode: Text.WordWrap
                                }

                                // The honest line: where the number came from, or why there is none.
                                Text {
                                    width: parent.width
                                    visible: providerRow.provider.note !== "" || !providerRow.provider.hasWindows
                                    textFormat: Text.PlainText
                                    text: providerRow.provider.note !== "" ? providerRow.provider.note : "no reading"
                                    color: root.dim
                                    font.family: root.fontFamily
                                    font.pixelSize: Style.font.caption
                                    wrapMode: Text.WordWrap
                                }
                            }

                            Item { width: 1; height: Style.space(2) }
                        }
                    }

                    Ui.PanelSeparator { width: parent.width; visible: root.providers.length > 0; foreground: root.foreground }

                    Ui.Button {
                        width: parent.width
                        bordered: true
                        text: root.refreshing ? "Refreshing…" : "Refresh now  (R)"
                        enabled: !root.refreshing
                        foreground: root.foreground
                        fontFamily: root.fontFamily
                        onClicked: root.refresh()
                    }

                    Text {
                        width: parent.width
                        textFormat: Text.PlainText
                        text: "Read from " + root.cachePath + " · no network in this widget"
                        color: root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
    }

    // One day of token history: label, bar scaled to the week's busiest day, count. Today is
    // picked out in full foreground so the week reads as a run-up to right now.
    component DayBar: Item {
        id: dayBar
        property var day: null

        implicitHeight: Math.max(dayLabel.implicitHeight, dayValue.implicitHeight) + Style.spacing.xs

        Text {
            id: dayLabel
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(52)
            textFormat: Text.PlainText
            text: dayBar.day ? dayBar.day.label : ""
            color: (dayBar.day && dayBar.day.today) ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: dayBar.day ? dayBar.day.today : false
        }

        Rectangle {
            id: dayTrack
            anchors.left: dayLabel.right
            anchors.right: dayValue.left
            anchors.leftMargin: Style.space(6)
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            height: Style.space(4)
            radius: height / 2
            color: Style.normalFill

            Rectangle {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                height: parent.height
                radius: parent.radius
                width: parent.width * root.clamp01(dayBar.day ? dayBar.day.ratio : 0)
                color: (dayBar.day && dayBar.day.today) ? root.foreground : root.alpha(root.foreground, 0.55)
            }
        }

        Text {
            id: dayValue
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(56)
            horizontalAlignment: Text.AlignRight
            textFormat: Text.PlainText
            text: dayBar.day ? dayBar.day.tokenText : ""
            color: (dayBar.day && dayBar.day.today) ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
        }

        MouseArea {
            id: dayHover
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.NoButton
        }

        Ui.PanelToolTip {
            visible: dayHover.containsMouse
            text: Model.dayTooltip(dayBar.day)
            fontFamily: root.fontFamily
        }
    }

    // One model's share of the window. The bar fills the row behind the label rather than stacking
    // under it, which is what keeps a provider's whole history on one screen.
    component ModelBar: Item {
        id: modelBar
        property var row: null

        implicitHeight: Math.max(modelName.implicitHeight, modelValue.implicitHeight) + Style.spacing.xs

        Rectangle {
            anchors.fill: parent
            radius: Style.cornerRadius
            color: root.alpha(root.foreground, 0.05)
        }

        Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: parent.width * root.clamp01(modelBar.row ? modelBar.row.share : 0)
            radius: Style.cornerRadius
            color: root.alpha(root.foreground, 0.12)
        }

        Text {
            id: modelName
            anchors.left: parent.left
            anchors.leftMargin: Style.spacing.sm
            anchors.right: modelValue.left
            anchors.verticalCenter: parent.verticalCenter
            textFormat: Text.PlainText
            text: modelBar.row ? modelBar.row.id : ""
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
        }

        Text {
            id: modelValue
            anchors.right: parent.right
            anchors.rightMargin: Style.spacing.sm
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(56)
            horizontalAlignment: Text.AlignRight
            textFormat: Text.PlainText
            text: modelBar.row ? modelBar.row.tokenText : ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
        }

        MouseArea {
            id: modelHover
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.NoButton
        }

        Ui.PanelToolTip {
            visible: modelHover.containsMouse
            text: modelBar.row ? modelBar.row.detailText : ""
            fontFamily: root.fontFamily
        }
    }
}
