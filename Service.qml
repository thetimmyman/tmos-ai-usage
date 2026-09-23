// Service.qml — the collector's schedule, and the only process in this plugin that touches the
// network.
//
// A plugin's `service` kind is a headless singleton: one instance for the whole shell, not one per
// monitor. That matters here, because the collector writes a single cache document — scheduling it
// per bar would mean several writers for one file.
//
// What it does, and nothing else:
//
//   * runs the bundled collector (`collector/usage_collector.py`, python3 standard library only)
//     shortly after the shell starts, then every `intervalSec`
//   * hands it this plugin's own state directory, so nothing here depends on where the plugin was
//     installed from, or on any other application being present
//   * records the last failure in `lastError` rather than dying quietly
//
// The bar widget reads the file this writes. The widget never runs a network client, and this
// service never draws anything.
import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: root
    width: 0
    height: 0
    visible: false

    // The collector ships inside this plugin folder. Resolving it relative to this file means the
    // service is correct both when the plugin was installed by `omarchy plugin add` into
    // ~/.config/omarchy/plugins/ and when it is being run straight from a checkout.
    readonly property string collectorPath: Qt.resolvedUrl("collector/usage_collector.py")
        .toString().replace(/^file:\/\//, "")

    // The plugin's own state, not another application's: removing the plugin can remove it too.
    readonly property string stateDir: {
        var override = Quickshell.env("TMOS_USAGE_STATE_DIR");
        return (override && override.length > 0) ? override : (Quickshell.env("HOME") + "/.local/state/tmos-ai-usage");
    }

    // Five minutes. The providers' windows move on the scale of hours, and every run is a request to
    // an endpoint the user already pays for, so there is no reason to poll harder than this.
    readonly property int intervalSec: 300
    // Let the shell finish starting before spending a request on it.
    readonly property int firstRunDelayMs: 5000

    property bool running: false
    property double lastRunAt: 0
    property int lastExitCode: 0
    property string lastError: ""

    function run() {
        if (collector.running) return;
        collector.command = ["python3", root.collectorPath, "--once", "--state-dir", root.stateDir];
        collector.running = true;
    }

    Timer {
        interval: root.firstRunDelayMs
        repeat: false
        running: true
        onTriggered: root.run()
    }

    Timer {
        interval: root.intervalSec * 1000
        repeat: true
        running: true
        onTriggered: root.run()
    }

    Process {
        id: collector
        command: []
        stderr: StdioCollector { id: collectorErr; waitForEnd: true }
        onExited: function(exitCode) {
            root.running = false;
            root.lastRunAt = Date.now();
            root.lastExitCode = exitCode;
            if (exitCode === 0) {
                root.lastError = "";
                return;
            }
            var detail = String(collectorErr.text || "").replace(/\s+/g, " ").trim();
            if (detail.length > 0) root.lastError = detail.slice(0, 240);
            else if (exitCode === 127) root.lastError = "python3 not found — the collector needs python3 (" + root.collectorPath + ")";
            else root.lastError = "collector exited " + exitCode;
        }
    }
}
