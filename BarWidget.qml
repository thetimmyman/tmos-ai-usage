// BarWidget.qml — the bar entry point (manifest entryPoints.barWidget).
//
// Two jobs, and nothing else:
//
//   1. OWN THE DATA. One `UsageSource` (the file watch on the collector's cache), one 30-second
//      clock, and the provider rotation that decides which subscription is currently on the bar.
//      The panel is handed a reference to this widget (`hostWidget`) rather than starting its own,
//      so the cache file is watched once and the clock ticks once.
//   2. FORWARD THE PANEL LIFECYCLE. This is the contract the Omarchy shell expects for a bar widget
//      with a panel: `opened`, `open()`, `close()`, `toggle()`, `closeForPopoutSwitch()` and
//      `injectPanel()` are routed to Panel.qml through a Loader, which is what makes
//      `omarchy-shell shell summon tmos.usage '{}'` and Tab-to-neighbour work.
//
// Every colour, size and font is a qs.Commons token (gate N3). No network, no credentials, no
// endpoints in this process — UsageSource.qml reads a file the collector wrote.
import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model

BarWidget {
    id: root

    moduleName: "tmos.usage"

    readonly property color foreground: bar ? bar.barForeground : Color.foreground
    readonly property color dim: Qt.darker(root.foreground, 1.35)

    // ---- forwarded panel lifecycle -------------------------------------------------------------
    readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
    readonly property bool popoutSwitchClosing: panelLoader.item
        ? panelLoader.item.popoutSwitchClosing === true
        : false

    function open() { if (panelLoader.item) panelLoader.item.open() }
    function close() { if (panelLoader.item) panelLoader.item.close() }
    function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
    function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

    function injectPanel() {
        if (!panelLoader.item) return;
        panelLoader.item.bar = root.bar;
        panelLoader.item.anchorItem = button;
        panelLoader.item.hostWidget = root;
    }

    // ---- data ----------------------------------------------------------------------------------
    readonly property var usageSource: source

    readonly property int warnHeadroomPct: Number(root.setting("warnHeadroomPct", 35))
    readonly property int lowHeadroomPct: Number(root.setting("lowHeadroomPct", 15))
    readonly property int staleAfterMin: Number(root.setting("staleAfterMin", 20))
    // Seconds each provider stays on the bar before the next one rotates in. 0 pins the single
    // tightest provider (the old "AI N%" behaviour). One provider never needs to rotate.
    readonly property int rotateSecs: Number(root.setting("rotateSecs", 5))

    property double nowMs: Date.now()
    property int rotateIndex: 0

    readonly property var providers: source.providers
    readonly property var totals: source.totals
    readonly property bool stale: Model.isStale(source.observedAt, root.nowMs, root.staleAfterMin)

    // The provider currently on the bar. Clamped so a shrink in the list between ticks can never
    // point past the end.
    readonly property var focusedProvider: {
        if (root.providers.length === 0) return null;
        if (root.rotateSecs === 0) {
            var tight = Model.tightest(root.providers);
            return tight ? tight.provider : root.providers[0];
        }
        return root.providers[Math.min(root.rotateIndex, root.providers.length - 1)];
    }

    onProvidersChanged: rotateIndex = 0

    // The panel asks for a fresh reading when it opens, so the age it shows is the age of what is
    // on screen rather than of whenever the last tick happened.
    function touch() { root.nowMs = Date.now() }

    function refresh() { source.refresh() }

    // One token per tone. Omarchy publishes foreground/accent/urgent/muted; "warn" borrows accent
    // rather than inventing a colour the theme never chose.
    function toneColor(tone) {
        if (tone === "urgent") return Color.urgent;
        if (tone === "warn") return Color.accent;
        if (tone === "muted") return root.dim;
        return root.foreground;
    }

    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    onBarChanged: injectPanel()

    // The clock that makes "resets in 1d 20h" tick without re-reading the file.
    Timer {
        interval: 30000
        repeat: true
        running: true
        onTriggered: root.nowMs = Date.now()
    }

    // The rotation clock: one provider at a time, most-urgent window first.
    Timer {
        interval: Math.max(1, root.rotateSecs) * 1000
        repeat: true
        running: root.rotateSecs > 0 && root.providers.length > 1
        onTriggered: root.rotateIndex = (root.rotateIndex + 1) % root.providers.length
    }

    UsageSource { id: source }

    Loader {
        id: panelLoader
        active: true
        source: Qt.resolvedUrl("Panel.qml")
        visible: false
        onLoaded: {
            root.injectPanel();
            Qt.callLater(root.injectPanel);
        }
    }

    WidgetButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: Model.barTextFor(root.focusedProvider)
        tooltipText: Model.tooltip(root.providers, source.observedAt, root.nowMs)
        active: root.opened
        foreground: root.toneColor(Model.barToneFor(root.focusedProvider, root.warnHeadroomPct, root.lowHeadroomPct))
        onPressed: function(buttonCode) {
            if (buttonCode === Qt.RightButton || buttonCode === Qt.MiddleButton) root.refresh();
            else root.toggle();
        }
    }
}
