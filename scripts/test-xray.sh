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
            providers: Model.parseDocument(JSON.stringify({providers:[{provider:'codex',stats:{available:false}},{provider:'opencode-go',windows:[{name:'week',remaining_pct:30}],report:{metrics:{Requests:1000},note:'Sample'}}]})).providers
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
