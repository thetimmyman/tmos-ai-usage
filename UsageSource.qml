// Reads the private usage cache and invokes bundled helpers with explicit argv.
// Credentials remain in the collector; setup and review are local user actions.
import QtQuick
import Quickshell
import Quickshell.Io as QSIo
import "Model.js" as Model

Item {
    id: root

    // The plugin's own state, and the collector that ships beside this file: both resolve from this
    // file's location or from HOME, so the widget is correct whether the plugin was installed by
    // `omarchy plugin add` or is running from a checkout.
    readonly property string stateDir: {
        var override = Quickshell.env("TMOS_USAGE_STATE_DIR");
        return (override && override.length > 0) ? override : (Quickshell.env("HOME") + "/.local/state/tmos-ai-usage");
    }
    readonly property string cachePath: stateDir + "/usage.json"
    readonly property string collectorPath: {
        var override = Quickshell.env("TMOS_USAGE_COLLECTOR");
        if (override && override.length > 0) return override;
        return Qt.resolvedUrl("collector/usage_collector.py").toString().replace(/^file:\/\//, "");
    }

    property var setupState: ({})
    property bool setupBusy: false
    property bool setupLoaded: false
    property string setupError: ""
    property string setupStatus: ""
    property string setupRequest: ""
    property bool setupInitializeQueued: false
    signal setupFinished()
    Component.onCompleted: setupAction("status")

    function setupAction(action) {
        if (["status", "initialize", "install-task-command", "install-pi-observer", "remove-task-command", "remove-pi-observer", "finish"].indexOf(action) < 0) return false;
        if (root.setupBusy) {
            if (action === "initialize") root.setupInitializeQueued = true;
            return false;
        }
        setupError = "";
        setupStatus = "";
        setupRequest = action;
        root.setupBusy = true;
        setupProcess.command = ["python3", Qt.resolvedUrl("collector/setup.py").toString().replace(/^file:\/\//, ""),
            action, "--state-dir", root.stateDir];
        setupProcess.running = true;
        return true;
    }
    QSIo.Process {
        id: setupProcess
        stdout: QSIo.StdioCollector { id: setupOutput; waitForEnd: true }
        onExited: function(code) {
            var completedAction = root.setupRequest;
            root.setupBusy = false;
            var response = null;
            try { response = JSON.parse(String(setupOutput.text || "")); } catch (e) {}
            if (code !== 0 || !response || response.error) {
                root.setupError = response && response.error ? String(response.error).slice(0, 240) : "Setup could not finish. Check Python 3 is installed and retry.";
                root.setupLoaded = true;
                return;
            }
            root.setupState = response;
            root.setupStatus = String(response.message || "");
            root.setupLoaded = true;
            if (completedAction === "initialize") root.refresh();
            if (completedAction === "finish") root.setupFinished();
            if (root.setupInitializeQueued) {
                root.setupInitializeQueued = false;
                Qt.callLater(function() { root.setupAction("initialize"); });
            }
        }
    }

    property string rankingMode: ""
    property string cachedText: ""
    property bool savingPrice: priceWriter.running
    property string priceError: ""
    property string priceStatus: ""
    property var priceRequest: null
    property bool refreshQueued: false
    property var taskReviews: []
    property bool taskReviewTruncated: false
    property int taskReviewUnavailable: 0
    property bool taskReviewLoading: false
    property string taskReviewError: ""
    property bool taskReviewDeciding: false
    property string taskReviewStatus: ""
    onRankingModeChanged: if (cachedText) apply(cachedText)
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
        root.cachedText = text;
        var parsed = Model.parseDocument(text, root.rankingMode);
        root.providers = parsed.providers;
        root.totals = parsed.totals;
        root.observedAt = parsed.observedAt;
        root.loadError = parsed.ok ? "" : parsed.error;
        root.reloaded();
    }

    // Manual refresh. The collector writes the cache; the FileView below notices and re-parses, so
    // nothing here parses stdout — one parse path, one truth.
    function refresh() {
        if (root.refreshing || collector.running) { root.refreshQueued = true; return; }
        root.refreshing = true;
        root.refreshError = "";
        collector.command = ["python3", root.collectorPath, "--once", "--state-dir", root.stateDir];
        collector.running = true;
    }

    function savePrice(provider, amount, cycle) {
        var n = Number(amount);
        if (priceWriter.running) return;
        if (!String(amount).trim() || !isFinite(n) || n < 0) {
            root.priceStatus = "";
            root.priceError = "Enter a nonnegative USD subscription amount."; return;
        }
        root.priceError = "";
        root.priceStatus = "";
        root.priceRequest = {provider: provider, amount: n, cycle: cycle};
        priceWriter.command = ["python3", Qt.resolvedUrl("collector/subscription_value.py").toString().replace(/^file:\/\//, ""),
            "--state-dir", root.stateDir, "--provider", provider, "--amount", String(n), "--cycle", cycle];
        priceWriter.running = true;
    }
    function taskReviewPath() {
        return Qt.resolvedUrl("collector/task_review.py").toString().replace(/^file:\/\//, "")
    }
    function refreshTaskReviews() {
        if (taskReviewLoading || taskReviewDeciding) return;
        taskReviewError = "";
        taskReviewStatus = "";
        taskReviewLoading = true;
        taskReviewList.command = ["python3", taskReviewPath(), "list", "--state-dir", root.stateDir];
        taskReviewList.running = true;
    }
    function decideTask(taskRef, reviewer, decision, expectedRun, expectedVerification, semanticAccepted) {
        if (taskReviewDeciding || taskReviewLoading) return;
        if (!taskRef || !reviewer || !expectedRun) {
            taskReviewError = "A current task run and reviewer are required for this decision.";
            return;
        }
        var command = ["python3", taskReviewPath(), "decide", "--state-dir", root.stateDir,
            "--task-ref", taskRef, "--reviewer", reviewer, "--decision", decision,
            "--expected-run", expectedRun];
        if (decision === "accept") {
            if (!expectedVerification || !semanticAccepted) {
                taskReviewError = "Acceptance needs a successful current check and explicit semantic confirmation.";
                return;
            }
            command.push("--expected-verification", expectedVerification, "--semantic-accepted");
        }
        taskReviewError = "";
        taskReviewStatus = "";
        taskReviewDeciding = true;
        taskReviewDecision.command = command;
        taskReviewDecision.running = true;
    }
    QSIo.Process {
        id: priceWriter
        onExited: function(code) {
            if (code !== 0) root.priceError = "Could not save subscription price.";
            else {
                var request = root.priceRequest;
                var row = root.providers.filter(p => p.id === request.provider)[0];
                root.priceStatus = "Saved " + (row ? row.name : request.provider) + ": $"
                    + request.amount.toFixed(2) + " / " + request.cycle + ".";
                root.refresh();
            }
        }
    }
    QSIo.Process {
        id: taskReviewList
        command: []
        stdout: QSIo.StdioCollector { id: taskReviewListOut; waitForEnd: true }
        onExited: function(code) {
            root.taskReviewLoading = false;
            if (code !== 0) {
                root.taskReviewError = "Could not read the private task review list.";
                root.taskReviews = [];
                root.taskReviewTruncated = false;
                root.taskReviewUnavailable = 0;
                return;
            }
            try {
                var response = JSON.parse(String(taskReviewListOut.text || ""));
                if (response.schema_version !== 1 || !Array.isArray(response.tasks)) throw new Error("shape");
                root.taskReviews = response.tasks.slice(0, 100);
                root.taskReviewTruncated = response.truncated === true;
                root.taskReviewUnavailable = typeof response.unavailable === "number" ? Math.max(0, Math.floor(response.unavailable)) : 0;
            } catch (e) {
                root.taskReviewError = "Task review returned an unreadable response.";
                root.taskReviews = [];
                root.taskReviewTruncated = false;
                root.taskReviewUnavailable = 0;
            }
        }
    }
    QSIo.Process {
        id: taskReviewDecision
        command: []
        stdout: QSIo.StdioCollector { id: taskReviewDecisionOut; waitForEnd: true }
        onExited: function(code) {
            root.taskReviewDeciding = false;
            var response = null;
            try { response = JSON.parse(String(taskReviewDecisionOut.text || "")); } catch (e) {}
            if (code !== 0 || !response || response.ok !== true) {
                root.taskReviewError = response && (response.error || response.reason)
                    ? String(response.error || response.reason).slice(0, 180)
                    : "Task decision could not be recorded; refresh the review list.";
                return;
            }
            root.refreshTaskReviews();
            root.taskReviewStatus = "Recorded explicit decision: " + String(response.status || "updated") + ".";
            root.refresh();
        }
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
            if (root.refreshQueued) {
                root.refreshQueued = false;
                Qt.callLater(root.refresh);
            }
        }
    }
}
