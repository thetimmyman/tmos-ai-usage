#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
testdir=$(mktemp -d)
trap 'rm -rf "$testdir"' EXIT
ln -s "$PWD" "$testdir/Usage"
for part in /usr/share/omarchy/shell/*/; do ln -s "$part" "$testdir/$(basename "$part")"; done
python3 - "$testdir/state" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0,'collector')
import task_runner
p=Path(sys.argv[1]); p.mkdir(mode=0o700)
task_runner.run_task(p,'codex','synthetic-ui-test','ui-test',[sys.executable,'-c','pass'],label='Synthetic UI test')
task_runner.verify_task(p,'ui-test','test-reviewer',[sys.executable,'-c','assert 2+2==4'],check_label='Synthetic assertion')
PY
printf 'import sys\nsys.exit(0)\n' > "$testdir/collector.py"
cat > "$testdir/shell.qml" <<'QML'
import QtQuick
import Quickshell
import "Usage"
ShellRoot {
    UsageSource { id: source }
    FloatingWindow {
        visible: true; implicitWidth: 820; implicitHeight: 640
        TaskReview {
            id: review
            width: parent.width
            provider: 'codex'
            tasks: source.taskReviews
            loading: source.taskReviewLoading
            deciding: source.taskReviewDeciding
            statusText: source.taskReviewStatus
            errorText: source.taskReviewError
            onDecisionRequested: function(ref, reviewer, decision, run, verification, accepted) {
                source.decideTask(ref, reviewer, decision, run, verification, accepted);
            }
        }
        Timer { interval: 30; running: true; onTriggered: source.refreshTaskReviews() }
        Timer { interval: 350; running: true; onTriggered: {
            var row = source.taskReviews[0];
            if (!row || !row.can_accept) { console.error('ERROR no reviewable fixture'); Qt.quit(); return; }
            review.reviewer = 'test-reviewer';
            if (review.actionEnabled(row, 'accept')) console.error('ERROR acceptance without attestation');
            review.semanticAcceptedKey = review.semanticKey(row);
            if (!review.actionEnabled(row, 'accept')) console.error('ERROR explicit review disabled');
            review.decisionRequested(row.task_ref, review.reviewer, 'accept', row.run.execution_id, row.verification.verification_sha256, true);
        } }
        Timer { interval: 1000; running: true; onTriggered: {
            var row = source.taskReviews[0];
            if (!row || row.status !== 'validated' || source.taskReviewError || !source.taskReviewStatus.includes('validated'))
                console.error('ERROR decision not reflected with confirmation: ' + source.taskReviewError + ' ' + source.taskReviewStatus);
            else console.log('TASK_REVIEW_RUNTIME_OK');
            Qt.quit();
        } }
    }
}
QML
env -u WAYLAND_DISPLAY QT_QPA_PLATFORM=offscreen TMOS_USAGE_STATE_DIR="$testdir/state" TMOS_USAGE_COLLECTOR="$testdir/collector.py" timeout 15 quickshell -p "$testdir" --no-color > "$testdir/log" 2>&1 || { cat "$testdir/log"; exit 1; }
if grep -E 'ERROR|ReferenceError|TypeError|Unable to assign|Binding loop|is not defined|Cannot assign|Failed to load' "$testdir/log"; then exit 1; fi
grep TASK_REVIEW_RUNTIME_OK "$testdir/log"
