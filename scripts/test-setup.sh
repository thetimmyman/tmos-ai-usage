#!/usr/bin/env bash
# First-run UI to real setup backend; isolated HOME, no provider calls.
set -euo pipefail
cd "$(dirname "$0")/.."
testdir=$(mktemp -d)
trap 'rm -rf "$testdir"' EXIT
mkdir -p "$testdir/home"
ln -s "$PWD" "$testdir/Usage"
for part in /usr/share/omarchy/shell/*/; do ln -s "$part" "$testdir/$(basename "$part")"; done
printf 'import sys\nsys.exit(0)\n' > "$testdir/collector.py"
cat > "$testdir/shell.qml" <<'QML'
import QtQuick
import Quickshell
import "Usage"
ShellRoot {
    UsageSource { id: source; onSetupFinished: window.finishCount++ }
    FloatingWindow {
        id: window
        visible: true; implicitWidth: 600; implicitHeight: 900
        property int phase: 0
        property int ticks: 0
        property int finishCount: 0
        Component.onCompleted: { source.setupAction('status'); source.setupAction('initialize'); }
        SetupView {
            id: setup
            anchors.fill: parent
            setupState: source.setupState
            busy: source.setupBusy
            errorText: source.setupError
            statusText: source.setupStatus
            onActionRequested: function(action) { source.setupAction(action); }
        }
        Timer {
            interval: 50; repeat: true; running: true
            onTriggered: {
                if (++window.ticks > 160 || source.setupError) {
                    console.error('ERROR setup: ' + source.setupError + ' phase=' + window.phase + ' state=' + JSON.stringify(source.setupState) + ' queued=' + source.setupInitializeQueued); Qt.quit(); return;
                }
                if (!source.setupLoaded || source.setupBusy) return;
                if (window.phase === 0) {
                    if (source.setupState.completed) console.error('ERROR not a clean first run');
                    if (setup.providerRows().length !== 5 || setup.canReviewPrice({id:'codex'})) console.error('ERROR new provider readiness');
                    // Opening setup while initial status is in flight must not lose initialization.
                    if (!source.setupState.initialized) return;
                    window.phase = 1; setup.actionRequested('initialize');
                } else if (window.phase === 1) {
                    if (!source.setupState.initialized || source.setupState.completed) console.error('ERROR initialize state');
                    if (source.setupState.pi_observer.installed) console.error('ERROR observer installed without opt-in');
                    window.phase = 2; setup.actionRequested('install-task-command');
                } else if (window.phase === 2) {
                    if (!source.setupState.task_command.installed) console.error('ERROR task command not installed');
                    window.phase = 3; setup.actionRequested('finish');
                } else if (window.phase === 3) {
                    if (!source.setupState.completed || window.finishCount !== 1) console.error('ERROR finish not persisted');
                    window.phase = 4; source.setupAction('status');
                } else {
                    if (!source.setupState.completed || !source.setupState.task_command.installed) console.error('ERROR setup state not durable');
                    console.log('SETUP_RUNTIME_OK'); Qt.quit();
                }
            }
        }
    }
}
QML
env -u WAYLAND_DISPLAY -u PI_CODING_AGENT_DIR HOME="$testdir/home" QT_QPA_PLATFORM=offscreen \
    TMOS_USAGE_STATE_DIR="$testdir/home/.local/state/tmos-ai-usage" TMOS_USAGE_COLLECTOR="$testdir/collector.py" \
    timeout 15 quickshell -p "$testdir" --no-color > "$testdir/log" 2>&1 || { cat "$testdir/log"; exit 1; }
if grep -E 'ERROR|ReferenceError|TypeError|Unable to assign|Binding loop|is not defined|Cannot assign|Failed to load' "$testdir/log"; then exit 1; fi
grep SETUP_RUNTIME_OK "$testdir/log"
HOME="$testdir/home" "$testdir/home/.local/bin/tmos-ai-task" review > "$testdir/tasks.json"
python3 - "$testdir/home" "$testdir/tasks.json" <<'PY'
import json,sys
from pathlib import Path
home=Path(sys.argv[1]); state=home/'.local/state/tmos-ai-usage'
assert json.loads((state/'setup.json').read_text())['completed'] is True
assert not (state/'subscriptions.json').exists(), 'setup must not invent subscription prices'
assert not (home/'.pi/agent/extensions/tmos-ai-usage').exists(), 'Pi is opt-in'
assert json.loads(Path(sys.argv[2]).read_text())['tasks']==[]
PY
