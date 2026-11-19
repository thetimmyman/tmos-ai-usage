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
import qs.Commons as Commons
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
            providers: Model.parseDocument(JSON.stringify({outcome_models:{status:'observed',start:'2026-04-02T00:00:00Z',end:'2026-04-03T00:00:00Z',models:[{model:'Example A',providers:2,sample:4,validated:1,turns_per_validated:6,usd_per_validated:3,provisional:true}]},report_import:{imported:1,unchanged:0,rejected:1,deferred:1},outcome_import:{applied:2,duplicates:0,rejected_files:0,pending_files:1},providers:[{provider:'codex',outcome_value:{status:'observed',start:'2026-04-02T00:00:00Z',end:'2026-04-03T00:00:00Z',total:{sample:3,validated:1,turns_per_validated:4,usd_per_validated:2,provisional:true},models:[{model:'Example A',sample:3,validated:1,turns_per_validated:4,usd_per_validated:2,provisional:true}]},activity:{price:{amount_usd:200,cycle:'year'}},stats:{available:false},outcomes:{available:true,coverage:{kind:'observed ledger events',complete:false},counts:{tasks:2,pending:1,validated:1,failed:0,abandoned:0,reworked:0,turns:3,errors:0},by_cohort:{smoke:{tasks:2,pending:1,validated:1,failed:0,abandoned:0,reworked:0,turns:3,errors:0}}}},{provider:'antigravity',observed_at:'2026-10-19T14:00:00Z',quota_groups:[{name:'Gemini Models',windows:[{name:'5h',remaining_pct:60,resets_in_s:3600}]},{name:'Claude and GPT models',windows:[{name:'week',remaining_pct:10}]}]},{provider:'opencode-go',activity:{price:{amount_usd:10,cycle:'month'}},windows:[{name:'week',remaining_pct:30}],report:{metrics:{Requests:1000},note:'Sample'},billing:{cash_paid_usd:null,allocated_known_usd:null,coverage_complete:false,invoices:[]}}]})).providers
        }
        // Capacity overview: one labelled row per subscription, checked at the narrowest and the
        // widest panel width. A separate view, so the checks above keep their own state.
        XrayView {
            id: capacityView
            z: -1
            width: 520
            height: 720
            providers: Model.parseDocument(JSON.stringify({comparison_mode:'activity',providers:[
                {provider:'relay',label:'Relay Pass',status:'unauthenticated'},
                {provider:'nimbus',label:'Nimbus Assist',status:'stale',activity:{observed_turns:30,window_days:30,price:{monthly_usd:10}},
                    windows:[{name:'5h',remaining_pct:43},{name:'week',remaining_pct:78}]},
                {provider:'acme',label:'Acme Code',status:'ok',activity:{observed_turns:90,window_days:30,price:{monthly_usd:10}},
                    windows:[{name:'5h',remaining_pct:84},{name:'week',remaining_pct:12},{name:'month',remaining_pct:30}]},
                {provider:'antigravity',status:'estimate',activity:{observed_turns:20,window_days:30,price:{monthly_usd:10}},
                    windows:[{name:'week',remaining_pct:120}]},
                {provider:'quarry',label:'Quarry Agent',status:'error',windows:[{name:'5h',remaining_pct:90},{name:'week',remaining_pct:9},{name:'month',remaining_pct:30}]},
                {provider:'orbital',label:'Orbital CLI',status:'unknown'},
                {provider:'ledger',label:'Ledger Only',status:'ok',balance:{remaining_usd:4,funded_usd:10}}
            ]})).providers
        }
        function countCanvases(item) {
            var n = typeof item.getContext === 'function' && typeof item.requestPaint === 'function' ? 1 : 0;
            var kids = item.children || [];
            for (var i = 0; i < kids.length; i++) n += countCanvases(kids[i]);
            return n;
        }
        function toneToken(tone) {
            if (tone === 'urgent') return Commons.Color.urgent;
            if (tone === 'warn') return Commons.Color.accent;
            if (tone === 'muted') return capacityView.secondary;
            return Commons.Color.foreground;
        }
        // Text keeps 8-bit channels and Rectangle does not, so colours match to one step.
        function sameColor(a, b) {
            return ['r', 'g', 'b', 'a'].every(c => Math.abs(a[c] - b[c]) <= 1 / 255 + 1e-6);
        }
        function checkCapacity(at, warn, low) {
            var v = capacityView, lastY = -1;
            if (v.warnHeadroomPct !== warn || v.lowHeadroomPct !== low)
                console.error('ERROR capacity thresholds are ' + v.warnHeadroomPct + '/' + v.lowHeadroomPct + ' at ' + at + ', expected ' + warn + '/' + low);
            if (countCanvases(v) !== 1) console.error('ERROR capacity line chart still drawn: ' + countCanvases(v) + ' canvases at ' + at);
            var headers = ['5h', 'week', 'month'].map(k => window.findNamed(v, 'capacityHeader-' + k));
            if (headers.some(h => !h || !h.visible) || headers.map(h => h.text).join('|') !== '5h|Week|Month')
                console.error('ERROR capacity window labels missing at ' + at);
            var foot = window.findNamed(v, 'capacityFootnote');
            if (!foot || !foot.visible || foot.text.indexOf('not reported') < 0 || foot.text.indexOf('no reading') < 0) console.error('ERROR capacity footnote does not explain not reported and no reading');
            v.providers.forEach(function(p) {
                var row = window.findNamed(v, 'capacityRow-' + p.id);
                if (!row || !row.visible) { console.error('ERROR capacity row missing for ' + p.id + ' at ' + at); return; }
                var y = row.mapToItem(v, 0, 0).y;
                if (y <= lastY) console.error('ERROR capacity rows not in ranking order at ' + p.id);
                lastY = y;
                var name = window.findNamed(v, 'capacityName-' + p.id);
                if (!name || name.text !== p.valueLabel + ' · ' + p.name || name.truncated || name.elide !== Text.ElideNone
                        || name.contentWidth > name.width + 1 || name.height + 1 < name.contentHeight)
                    console.error('ERROR capacity name not shown in full for ' + p.id + ' at ' + at + ': ' + (name ? name.text : 'missing'));
                var status = window.findNamed(v, 'capacityStatus-' + p.id);
                var shown = !!status && status.visible && status.text !== '';
                if (p.status === 'ok' ? shown : !(shown && status.text === p.statusLabel))
                    console.error('ERROR capacity status word for ' + p.id + ' (' + p.status + '): ' + (status ? '"' + status.text + '" visible=' + status.visible : 'missing'));
                var nameRight = name ? name.mapToItem(v, name.width, 0).x : 0;
                ['5h', 'week', 'month'].forEach(function(k) {
                    var id = p.id + '-' + k;
                    var cell = window.findNamed(v, 'capacityCell-' + id), text = window.findNamed(v, 'capacityText-' + id);
                    var track = window.findNamed(v, 'capacityTrack-' + id), fill = window.findNamed(v, 'capacityFill-' + id);
                    if (!cell || !text || !track || !fill) { console.error('ERROR capacity cell missing for ' + id + ' at ' + at); return; }
                    var w = p.windows[k], known = !!w && w.remainingPct >= 0;
                    var anyKnown = ['5h', 'week', 'month'].some(function(j) { return !!p.windows[j] && p.windows[j].remainingPct >= 0; });
                    var rawTone = known ? Model.tone(w.remainingPct, warn, low) : 'muted';
                    var tone = p.status === 'ok' || rawTone !== 'ok' ? rawTone : 'muted';
                    var label = { '5h': '5h', week: 'Week', month: 'Month' }[k];
                    var wording = known ? Math.round(w.remainingPct) + '%' : (p.status === 'ok' || anyKnown ? 'not reported' : 'no reading');
                    if (cell.mapToItem(v, 0, 0).x < nameRight) console.error('ERROR capacity cell overlaps the name for ' + id + ' at ' + at);
                    if (text.text !== wording) console.error('ERROR capacity text for ' + id + ': ' + text.text + ' expected ' + wording);
                    var speech = p.valueLabel + ' · ' + p.name + (p.status === 'ok' ? '' : ' (' + p.statusLabel + ')') + ' ' + label + ': ' + wording;
                    if (text.Accessible.name !== speech) console.error('ERROR capacity accessible name for ' + id + ': ' + text.Accessible.name);
                    if (track.visible !== known) console.error('ERROR capacity meter track visible=' + track.visible + ' for ' + id + ' (reading known: ' + known + ')');
                    if (!text.visible || text.contentWidth > cell.width + 1) console.error('ERROR capacity text clipped for ' + id + ' at ' + at);
                    if (cell.tone !== tone) console.error('ERROR capacity tone for ' + id + ': ' + cell.tone + ' expected ' + tone);
                    if (!window.sameColor(text.color, window.toneToken(tone))) console.error('ERROR capacity text colour for ' + id + ' is not the ' + tone + ' token');
                    if (known) {
                        var expected = track.width * Math.max(0, Math.min(1, w.remainingPct / 100));
                        if (!track.visible || track.width < 8 || Math.abs(fill.width - expected) > 1)
                            console.error('ERROR capacity meter for ' + id + ': fill ' + fill.width + ' of ' + track.width + ' expected ' + expected);
                        if (!window.sameColor(fill.color, window.toneToken(tone))) console.error('ERROR capacity fill colour for ' + id + ' is not the ' + tone + ' token');
                        if (track.mapToItem(v, track.width, 0).x > text.mapToItem(v, 0, 0).x + 0.5) console.error('ERROR capacity meter runs under its number for ' + id + ' at ' + at);
                    } else if (fill.visible && fill.width > 0) console.error('ERROR capacity not-reported cell has a fill for ' + id);
                });
            });
            var stale = window.findNamed(v, 'capacityFill-nimbus-5h');
            if (!stale || window.sameColor(stale.color, Commons.Color.foreground)) console.error('ERROR stale capacity fill uses the live colour');
        }
        Timer { interval: 600; running: true; onTriggered: { window.checkCapacity('520px', 35, 15); capacityView.width = 1280; } }
        Timer { interval: 740; running: true; onTriggered: window.checkCapacity('1280px', 35, 15) }
        // Thresholds the Budget panel hands over change the tones (the panel's own are 35 and 15).
        Timer { interval: 780; running: true; onTriggered: { capacityView.warnHeadroomPct = 90; capacityView.lowHeadroomPct = 40; } }
        Timer { interval: 920; running: true; onTriggered: window.checkCapacity('1280px thresholds 90/40', 90, 40) }
        Timer { interval: 220; running: true; onTriggered: {
            var rows = view.modelRows
            if (rows.length !== 1 || rows[0].value.model !== 'Example A' || rows[0].value.providers !== 2) console.error('ERROR model comparison missing')
            else {
                var cost = window.findNamed(view, 'outcomeCost-' + rows[0].label)
                if (!cost || !cost.visible || cost.text !== '$3.00 / validated') console.error('ERROR rendered outcome cost')
            }
            view.selectedId = 'codex'
            if (view.modelRows.length !== 2 || view.modelRows[0].value.model) console.error('ERROR provider outcome total')
            view.selectedId = ''
        } }
        Timer { interval: 300; running: true; onTriggered: { view.nowMs = Date.parse('2026-10-19T14:30:00Z'); view.selectedId = 'antigravity' } }
        Timer { interval: 450; running: true; onTriggered: {
            var gemini = window.findNamed(view, 'quotaGroup-Gemini Models');
            var thirdParty = window.findNamed(view, 'quotaGroup-Claude and GPT models');
            if (!gemini || gemini.text.indexOf('60.0% left') < 0 || !thirdParty || thirdParty.text.indexOf('10.0% left') < 0)
                console.error('ERROR Antigravity quota groups are not visible');
            // Observed at 14:00 with an hour left, viewed at 14:30: the countdown follows the view's clock.
            if (!gemini || gemini.text.indexOf('(in 30m)') < 0)
                console.error('ERROR quota-group countdown does not follow the view clock: ' + (gemini ? gemini.text : 'missing'));
            view.nowMs = Date.now();
        } }
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
