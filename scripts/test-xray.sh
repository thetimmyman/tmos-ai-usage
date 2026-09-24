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
    UsageSource {
        id: reviewSource
        visible: false
        Component.onCompleted: refreshTaskReviews()
    }
    FloatingWindow {
        id: window
        property var savedPrices: []
        property var decisionCall: null
        visible: true
        implicitWidth: 940
        implicitHeight: 720
        XrayView {
            id: view
            onPriceSaved: function(provider, amount, cycle) { window.savedPrices.push([provider, amount, cycle]); }
            onTaskDecisionRequested: function(taskRef, reviewer, decision, expectedRun, expectedVerification, semanticAccepted) {
                window.decisionCall = [taskRef, reviewer, decision, expectedRun, expectedVerification, semanticAccepted];
            }
            anchors.fill: parent
            providers: Model.parseDocument(JSON.stringify({report_import:{imported:1,unchanged:0,rejected:1,deferred:1},outcome_import:{applied:2,duplicates:0,rejected_files:0,pending_files:1},providers:[{provider:'codex',activity:{price:{amount_usd:200,cycle:'year'}},stats:{available:false},outcomes:{available:true,coverage:{kind:'observed ledger events',complete:false},counts:{tasks:2,pending:1,validated:1,failed:0,abandoned:0,reworked:0,turns:3,errors:0},by_cohort:{smoke:{tasks:2,pending:1,validated:1,failed:0,abandoned:0,reworked:0,turns:3,errors:0}}}},{provider:'opencode-go',activity:{price:{amount_usd:10,cycle:'month'}},windows:[{name:'week',remaining_pct:30}],report:{metrics:{Requests:1000},note:'Sample'},billing:{cash_paid_usd:null,allocated_known_usd:null,coverage_complete:false,invoices:[]}}]})).providers
        }
        function findNamed(item, name) {
            if (item.objectName === name) return item;
            var kids = item.children || [];
            for (var i = 0; i < kids.length; i++) {
                var found = findNamed(kids[i], name);
                if (found) return found;
            }
            return null;
        }
        function checkPrice(provider, amount) {
            view.selectedId = provider;
            var input = findNamed(view, 'subscriptionPriceInput');
            if (!input || input.text !== amount) {
                console.error('ERROR price for ' + provider + ': ' + (input ? input.text : 'missing') + ' expected ' + amount);
                Qt.quit();
            }
        }
        Timer { interval: 500; running: true; onTriggered: {
            function expect(actual, expected, label) {
                if (actual !== expected) console.error('ERROR coverage ' + label + ': ' + actual + ' expected ' + expected);
            }
            expect(view.localCoverageText({activity:{observed_turns:12},stats:{available:false}}), 'Observed · 12 turns', 'local observed')
            expect(view.localCoverageText({activity:{observed_turns:0},stats:{available:false}}), 'Unavailable', 'local unavailable')
            expect(view.reportCoverageText({report:{source:'provider export',coverage:{contains_truncated_export:true}}}), 'Partial · truncated export', 'truncated report')
            expect(view.reportCoverageText({report:{source:'history',coverage:{kind:'accumulated_provider_history',stored_records:27,backfill_pending:true,complete:false}}}), '27 records · backfilling', 'history backfill')
            expect(view.reportCoverageText({report:{source:'snapshot'}}), 'Snapshot · coverage unknown', 'snapshot')
            expect(view.taskCoverageText({outcomes:{available:true,counts:{validated:3}}}), '3 validated · observed', 'task outcomes')
            expect(view.taskCoverageText({outcomes:{available:false}}), 'Unavailable', 'task unavailable')
            expect(view.billingCoverageText({billing:{available:true,complete:false,invoices:[{},{}]}}), 'Partial · 2 receipts', 'partial billing')
            expect(view.billingCoverageText({billing:{available:false,invoices:[]}}), 'Unavailable', 'billing unavailable')
            view.taskReviews = [{task_ref:'abcdef0123456789',provider:'codex',cohort:'smoke',status:'pending',label:'Task abcdef0123',turns:1,errors:0,
                run:{execution_id:'00000000-0000-4000-8000-000000000001',started_at:'2026-01-01T00:00:00Z',ended_at:'2026-01-01T00:01:00Z',exit_code:0,artifact_bound:false},
                verification:null,can_accept:false,reason:'Successful verification required'}];
            window.checkPrice('codex', '200');
            var review = window.findNamed(view, 'taskReviewPanel');
            if (!review || !review.hasCompletedRun(view.taskReviews[0])) console.error('ERROR task review component or run receipt');
            review.reviewer = 'Reviewer';
            if (!review.actionEnabled(view.taskReviews[0], 'fail') || review.actionEnabled(view.taskReviews[0], 'accept')) console.error('ERROR task decision gating before check');
            review.loading = true;
            if (review.actionEnabled(view.taskReviews[0], 'fail')) console.error('ERROR task decision allowed during list refresh');
            review.loading = false;
            var readyTask = JSON.parse(JSON.stringify(view.taskReviews[0]));
            readyTask.verification = {reviewer:'Reviewer',ended_at:'2026-01-01T00:02:00Z',exit_code:0,verification_sha256:'a'.repeat(64),artifact_unchanged:null};
            readyTask.can_accept = true;
            view.taskReviews = [readyTask];
            if (!window.findNamed(view, 'semanticConfirmation').enabled) console.error('ERROR semantic confirmation cannot be selected');
            if (review.actionEnabled(view.taskReviews[0], 'accept')) console.error('ERROR semantic confirmation was not required');
            review.semanticAcceptedKey = review.semanticKey(view.taskReviews[0]);
            if (!review.actionEnabled(view.taskReviews[0], 'accept')) console.error('ERROR explicit acceptance remained disabled');
            review.decisionRequested(view.taskReviews[0].task_ref, 'Reviewer', 'accept', '00000000-0000-4000-8000-000000000001', view.taskReviews[0].verification.verification_sha256, true);
            if (JSON.stringify(window.decisionCall) !== JSON.stringify(['abcdef0123456789','Reviewer','accept','00000000-0000-4000-8000-000000000001','a'.repeat(64),true])) console.error('ERROR decision payload binding');
            var changedRun = JSON.parse(JSON.stringify(view.taskReviews[0])); changedRun.run.execution_id = '00000000-0000-4000-8000-000000000002';
            if (review.actionEnabled(changedRun, 'accept')) console.error('ERROR semantic confirmation survived a new run');
            var otherTask = JSON.parse(JSON.stringify(view.taskReviews[0])); otherTask.task_ref = 'bbbbbbbbbbbbbbbb';
            if (review.actionEnabled(otherTask, 'accept')) console.error('ERROR semantic confirmation leaked to another task');
            var sameTaskRows = view.taskReviews;
            var refreshedTask = JSON.parse(JSON.stringify(view.taskReviews[0])); refreshedTask.run.execution_id = '00000000-0000-4000-8000-000000000002';
            view.taskReviews = [refreshedTask];
            if (review.semanticAcceptedKey !== '') console.error('ERROR refresh identity change retained confirmation');
            view.taskReviews = sameTaskRows;
            view.taskReviews[0].run = {};
            if (review.actionEnabled(view.taskReviews[0], 'abandon')) console.error('ERROR decision allowed without completed run');
            window.checkPrice('opencode-go', '10');
            window.checkPrice('codex', '200');
            window.checkPrice('opencode-go', '10');
            view.savePriceEditor();
            window.checkPrice('codex', '200');
            view.savePriceEditor();
            if (JSON.stringify(window.savedPrices) !== JSON.stringify([['opencode-go','10','month'],['codex','200','year']])) console.error('ERROR cross-provider save');
            var rows = JSON.parse(JSON.stringify(view.providers));
            rows.filter(p => p.id === 'codex')[0].activity.price = {amount_usd:240,cycle:'year'};
            view.providers = rows.reverse();
            window.checkPrice('codex', '240');
            var cycle = window.findNamed(view, 'subscriptionCycleInput');
            if (cycle.currentIndex !== 1) console.error('ERROR refresh cycle');
            var input = window.findNamed(view, 'subscriptionPriceInput');
            input.text = '250';
            input.textEdited();
            view.providers = JSON.parse(JSON.stringify(rows)).reverse();
            if (input.text !== '250') console.error('ERROR refresh lost draft');
            view.savePriceEditor();
            if (JSON.stringify(window.savedPrices[2]) !== JSON.stringify(['codex','250','year'])) console.error('ERROR draft save');
            window.checkPrice('opencode-go', '10');
            if (cycle.currentIndex !== 0) console.error('ERROR previous cycle leaked');
            view.selectedId = '';
            view.savePriceEditor();
            if (window.savedPrices.length !== 3 || input.text !== '') console.error('ERROR overview retained editor');
            window.checkPrice('codex', '240');
            view.providers = rows.filter(p => p.id !== 'codex');
            view.savePriceEditor();
            if (window.savedPrices.length !== 3 || view.selectedId !== '') console.error('ERROR missing provider save');
            view.providers = rows;
            window.checkPrice('codex', '240');
            input.text = '999'; input.textEdited();
            cycle.activated();
            view.providers = JSON.parse(JSON.stringify(rows));
            if (input.text !== '999' || cycle.currentIndex !== 0) console.error('ERROR refresh lost cycle draft');
            window.checkPrice('opencode-go', '10');
            view.savePriceEditor();
            if (JSON.stringify(window.savedPrices[3]) !== JSON.stringify(['opencode-go','10','month'])) console.error('ERROR other provider inherited dirty draft');
        } }
        Timer { interval: 1000; running: true; onTriggered: {
            if (reviewSource.taskReviewLoading || reviewSource.taskReviewError !== '' || reviewSource.taskReviews.length !== 0) console.error('ERROR UsageSource review-list integration');
            console.log('XRAY_RUNTIME_OK'); Qt.quit()
        } }
    }
}
QML
if ! env -u WAYLAND_DISPLAY TMOS_USAGE_STATE_DIR="$testdir/state" QT_QPA_PLATFORM=offscreen timeout 15 quickshell -p "$testdir" --no-color > "$testdir/log" 2>&1; then
    cat "$testdir/log"; exit 1
fi
if grep -E 'ERROR|ReferenceError|TypeError|Unable to assign|Binding loop|is not defined|Cannot assign|Failed to load' "$testdir/log"; then exit 1; fi
grep XRAY_RUNTIME_OK "$testdir/log"
