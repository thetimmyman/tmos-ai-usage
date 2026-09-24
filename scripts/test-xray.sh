#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
testdir=$(mktemp -d)
trap 'rm -rf "$testdir"' EXIT
ln -s "$PWD" "$testdir/Usage"
for part in /usr/share/omarchy/shell/*/; do ln -s "$part" "$testdir/$(basename "$part")"; done
cat > "$testdir/shell.qml" <<'QML'
import QtQuick
import Quickshell
import "Usage"
import "Usage/Model.js" as Model
ShellRoot {
    FloatingWindow {
        visible: true
        implicitWidth: 940
        implicitHeight: 720
        XrayView {
            id: view
            anchors.fill: parent
            providers: Model.parseDocument(JSON.stringify({report_import:{imported:1,unchanged:0,rejected:1,deferred:1},outcome_import:{applied:2,duplicates:0,rejected_files:0,pending_files:1},providers:[{provider:'codex',stats:{available:false},outcomes:{available:true,coverage:{kind:'observed ledger events',complete:false},counts:{tasks:2,pending:1,validated:1,failed:0,abandoned:0,reworked:0,turns:3,errors:0},by_cohort:{smoke:{tasks:2,pending:1,validated:1,failed:0,abandoned:0,reworked:0,turns:3,errors:0}}}},{provider:'opencode-go',windows:[{name:'week',remaining_pct:30}],report:{metrics:{Requests:1000},note:'Sample'},billing:{cash_paid_usd:null,allocated_known_usd:null,coverage_complete:false,invoices:[]}}]})).providers
        }
        Timer { interval: 500; running: true; onTriggered: view.selectedId = 'opencode-go' }
        Timer { interval: 1000; running: true; onTriggered: { console.log('XRAY_RUNTIME_OK'); Qt.quit() } }
    }
}
QML
if ! env -u WAYLAND_DISPLAY QT_QPA_PLATFORM=offscreen timeout 15 quickshell -p "$testdir" --no-color > "$testdir/log" 2>&1; then
    cat "$testdir/log"; exit 1
fi
if grep -E 'ERROR|ReferenceError|TypeError|Unable to assign|Binding loop|is not defined|Cannot assign|Failed to load' "$testdir/log"; then exit 1; fi
grep XRAY_RUNTIME_OK "$testdir/log"
