// shell/plugins/tmos.usage/UsageSource.qml — the plugin's read side.
//
// NOT an entry point (manifest.json declares only barWidget): it is a local component BarWidget.qml
// instantiates, the same shape ardfard/omarchy-opencode-usage uses for its Service.qml.
//
// Two jobs, both narrow:
//   1. Watch ~/.local/state/tmos/usage.json (the tmosd state-path contract) with a FileView and
//      re-parse it whenever it changes. The collector writes atomically (os.replace), so a reload
//      never observes half a document.
//   2. On an explicit refresh, run the collector once as a subprocess. That is the ONLY way a
//      number changes from inside the shell: this plugin performs no network I/O of its own, holds
//      no credential, and knows no endpoint — constitution.md's separation of an observer from a
//      display, and the reason the widget stays installable without secrets.
//
// Collector path contract (same as tmosd's, see ../../tmosd/ipc-contract.md): third-party plugins
// never see `__sourceDir`, so the installed copy lives at ~/.config/tmos/usage_collector.py, with
// $TMOS_USAGE_COLLECTOR as the override for a checkout.
import QtQuick
import Quickshell
import Quickshell.Io as QSIo
import "Model.js" as Model

Item {
    id: root

    readonly property string stateDir: {
        var dir = Quickshell.env("TMOS_STATE_DIR");
        return (dir && dir.length > 0) ? dir : (Quickshell.env("HOME") + "/.local/state/tmos");
    }
    readonly property string cachePath: stateDir + "/usage.json"
    readonly property string collectorPath: {
        var override = Quickshell.env("TMOS_USAGE_COLLECTOR");
        return (override && override.length > 0) ? override : (Quickshell.env("HOME") + "/.config/tmos/usage_collector.py");
    }

    property var providers: []
    // Document-level rollup (today's tokens across every subscription, how many reported).
    // Parsed here so the view still derives nothing of its own; the initial value is the module's
    // empty shape rather than `({})`, so the view never binds a string to an undefined field.
    property var totals: Model.emptyTotals()
    property double observedAt: 0
    property string loadError: ""
    property bool refreshing: false
    property string refreshError: ""

    signal reloaded()

    function apply(text) {
        var parsed = Model.parseDocument(text);
        root.providers = parsed.providers;
        root.totals = parsed.totals;
        root.observedAt = parsed.observedAt;
        root.loadError = parsed.ok ? "" : parsed.error;
        root.reloaded();
    }

    // Manual refresh. The collector writes the cache; the FileView below notices and re-parses, so
    // nothing here parses stdout — one parse path, one truth.
    function refresh() {
        if (root.refreshing || collector.running) return;
        root.refreshing = true;
        root.refreshError = "";
        collector.command = ["python3", root.collectorPath, "--once"];
        collector.running = true;
    }

    QSIo.FileView {
        id: cache
        path: root.cachePath
        watchChanges: true
        printErrors: false
        // `text()` is stale inside onFileChanged, so both paths route through reload -> onLoaded.
        onFileChanged: reload()
        onLoaded: root.apply(text())
        onLoadFailed: root.apply("")
    }

    QSIo.Process {
        id: collector
        command: []
        stderr: QSIo.StdioCollector { id: collectorErr; waitForEnd: true }
        onExited: function(exitCode) {
            root.refreshing = false;
            if (exitCode !== 0) {
                var detail = String(collectorErr.text || "").replace(/\s+/g, " ").trim();
                root.refreshError = detail.length > 0
                    ? detail.slice(0, 160)
                    : ("collector exited " + exitCode + " (" + root.collectorPath + ")");
            } else {
                cache.reload();
            }
        }
    }
}
