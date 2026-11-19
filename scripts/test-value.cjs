const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const value = {};
vm.runInNewContext(fs.readFileSync(path.join(root, 'Value.js'), 'utf8').replace(/^\.pragma.*$/m, ''), value);
const context = {period: '2026-09', cohort: 'bounded-fix'};
const row = (id, tasks, spend, extra = {}) => ({id, name: id, economics: {
  ...context, coverage_complete: true, evidence_verified: true, outcomes_sha256: "a".repeat(64), spend_sha256: "b".repeat(64), spend_basis: 'recognized_subscription_and_metered_usd',
  validated_tasks: tasks, spend_usd: spend, ...extra
}});
const input = [row('unknown', 99, 1, {coverage_complete: false}), row('low', 5, 10),
  row('best', 40, 10), row('tied', 20, 5), row('zero', 0, 10), row('free', 5, 0)];
const result = value.rankProviders(input, context);
assert.deepEqual(Array.from(result, r => r.id), ['best', 'tied', 'low', 'zero', 'free', 'unknown']);
assert.deepEqual(Array.from(result, r => r.valueLabel), ['#1', '#1', '#3', '#4', 'Free', 'Unranked']);
assert.equal(input[0].valueRank, undefined);
for (const extra of [{evidence_verified: false}, {outcomes_sha256: "invalid"}, {spend_usd: null}, {spend_usd: -1}, {spend_usd: Infinity},
  {spend_usd: '10'}, {validated_tasks: 1.5}, {validated_tasks: NaN},
  {period: 'other'}, {cohort: 'other'}, {spend_basis: 'promotional-credit'}]) {
  assert.equal(value.rankProviders([row('bad', 5, 10, extra)], context)[0].valueLabel, 'Unranked');
}
assert.equal(value.rankProviders([row('old', 5, 10)], undefined)[0].valueLabel, 'Unranked');
const model = {Value: value};
vm.runInNewContext(fs.readFileSync(path.join(root, 'Model.js'), 'utf8')
  .replace(/^\.(pragma|import).*$/gm, ''), model);
const parsed = model.parseDocument(JSON.stringify({economics_context: context,
  providers: input.map(r => ({provider: r.id, economics: r.economics}))}));
assert.deepEqual(Array.from(parsed.providers, r => r.id), Array.from(result, r => r.id));
assert.equal(parsed.providers[0].valueLabel, '#1');
const legacy = model.parseDocument(JSON.stringify({providers: [{provider: 'z'}, {provider: 'a'}]}));
assert.deepEqual(Array.from(legacy.providers, r => r.id), ['a', 'z']);
assert.equal(legacy.providers[0].valueLabel, 'Unranked');
const meter = (status) => model.parseDocument(JSON.stringify({providers: [{provider: 'claude-code', status,
  windows: [{name: '5h', used_pct: 20, remaining_pct: 80, resets_in_s: 3600}]}]})).providers[0];
assert.match(model.barTextFor(meter('stale')), / · stale$/);
assert.doesNotMatch(model.barTextFor(meter('ok')), /stale/);
assert.equal(model.barToneFor(meter('stale'), 30, 10), 'warn');
assert.equal(model.barToneFor(meter('ok'), 30, 10), 'ok');
assert.match(model.tooltip([meter('stale')], Date.now(), Date.now()), /80% left \(5h\) · stale/);
console.log('PASS stale meter: marked in bar text, tone and tooltip');
console.log('PASS shared ranking: ordering, ties, free/unknown, coverage, period/cohort, legacy cache, Model integration');
const activity = (id,n,price) => ({id,name:id,activity:{observed_turns:n,window_days:30,price:{monthly_usd:price}}});
const ar = value.rankActivity([activity('slow',5,10),activity('fast',30,10),activity('free',2,0),activity('missing',3,undefined)]);
assert.deepEqual(Array.from(ar,r=>r.valueLabel),['#1*','#2*','Free*','Set price']);
assert.equal(ar[0].tasksPerDollar,null);
assert.equal(ar[0].valueScore,3);
assert.equal(ar[0].rankingMetric,'activity');
const activityDoc = model.parseDocument(JSON.stringify({comparison_mode:'activity', providers:[{provider:'one',activity:{observed_turns:20,window_days:30,price:{monthly_usd:10}}}]}));
assert.equal(activityDoc.providers[0].valueLabel,'#1*');
assert.equal(model.parseDocument(JSON.stringify({comparison_mode:'activity',providers:[{provider:'one'}]}),'validated').providers[0].valueLabel,'Unranked');
console.log('PASS provisional activity rankings remain distinct from validated task ranks');
const billingDoc={providers:[{provider:'codex',billing:{cash_paid_usd:95,allocated_known_usd:null,coverage_complete:false,invoices:[{paid_at:'2026-09-01',base_usd:100,discount_usd:10,tax_usd:5,fees_usd:0,paid_usd:95,source_ref:'private-url'}]},outcomes:{available:false,counts:{tasks:0}},console_report:{note:'workspace only'}}]};
const billed=model.parseDocument(JSON.stringify(billingDoc)).providers[0];
assert.equal(billed.billing.allocatedKnownUsd,null);
assert.equal(billed.outcomes.available,false);
assert(!JSON.stringify(billed.billing).includes('private-url'));
assert.equal(billed.consoleReport.note,'workspace only');
console.log('PASS invoice privacy, unknown amounts and unavailable outcomes');
const importDoc=model.parseDocument(JSON.stringify({
  report_import:{imported:1,unchanged:2,rejected:3,deferred:4,note:'secret /tmp/private.json token=private'},
  outcome_import:{applied:5,duplicates:6,rejected_files:7,pending_files:8,filename:'private.json'},
  providers:[{provider:'opencode-go', billing:{invoices:[],cash_paid_usd:null,allocated_known_usd:null},outcomes:{available:false,counts:{tasks:0}}}]
})).providers[0];
assert.deepEqual([importDoc.reportImport.newCount,importDoc.reportImport.unchangedCount,importDoc.reportImport.rejectedCount,importDoc.reportImport.pendingCount],[1,2,3,4]);
assert.deepEqual([importDoc.outcomeImport.newCount,importDoc.outcomeImport.unchangedCount,importDoc.outcomeImport.rejectedCount,importDoc.outcomeImport.pendingCount],[5,6,7,8]);
assert.equal(importDoc.billing.available,false);
assert.equal(importDoc.billing.cashPaidUsd,null);
assert.equal(importDoc.outcomes.available,false);
assert(!JSON.stringify(importDoc).includes('private.json'));
assert(!JSON.stringify(importDoc).includes('/tmp/'));
assert(!JSON.stringify(importDoc).includes('secret'));
const failedImport=model.parseDocument(JSON.stringify({report_import:{note:'private path'},outcome_import:{rejected_files:2},providers:[{provider:'codex'}]})).providers[0];
assert.equal(failedImport.reportImport.available,false);
assert.equal(failedImport.reportImport.failed,true);
assert.equal(failedImport.reportImport.rejectedCount,null);
assert.equal(failedImport.outcomeImport.rejectedCount,2);
console.log('PASS sanitized report/outcome import states and missing amounts stay unknown');
const cohortDoc=model.parseDocument(JSON.stringify({providers:[{provider:'codex',outcomes:{available:true,coverage:{kind:'observed ledger events',complete:false},counts:{tasks:2,validated:1,pending:1,failed:0,abandoned:0,reworked:0,turns:3,errors:0},by_cohort:{'small-fixes':{tasks:2,validated:1,turns:3}}}}]})).providers[0];
assert.equal(cohortDoc.outcomes.available,true);
assert.equal(cohortDoc.outcomes.cohorts[0].name,'small-fixes');
assert.equal(cohortDoc.outcomes.cohorts[0].counts.validated,1);
assert.equal(cohortDoc.outcomes.coverageText,'observed ledger events · incomplete; observed counts only');
console.log('PASS outcome cohort summaries normalize to observed-only counts');
const agy=model.parseDocument(JSON.stringify({providers:[{provider:'antigravity',status:'ok',
  quota_groups:[{name:'Gemini Models',windows:[{name:'5h',remaining_pct:60,resets_in_s:3600}]},
                {name:'Claude and GPT models',windows:[{name:'week',remaining_pct:10}]}],
  stats:{available:true,note:'Cache overlap unspecified',daily:[],models:[],totals:{},scan:{}}}]})).providers[0];
assert.equal(agy.name,'Google AI / Antigravity');
assert.equal(agy.quotaGroups.length,2);
assert.match(agy.quotaGroups[0].detail,/60.0% left/);
assert.match(agy.quotaGroups[1].detail,/10.0% left/);
assert.match(agy.stats.sourceText,/Cache overlap unspecified/);
assert.equal(agy.valueLabel,'Unranked');
console.log('PASS Antigravity model groups and incomplete activity provenance');
const resetDoc = model.parseDocument(JSON.stringify({
  observed_at: '2026-10-19T14:00:00Z',
  providers: [{
    provider: 'opencode-go', status: 'ok',
    windows: [
      {name: '5h', used_pct: 100, remaining_pct: 0, resets_at: '2026-10-19T18:00:00Z', resets_in_s: 14400},
      {name: 'week', used_pct: 40, remaining_pct: 60, resets_in_s: 7200}
    ]
  }]
})).providers[0];
assert.equal(resetDoc.windows['5h'].resetsAt, '2026-10-19T18:00:00Z');
assert.equal(resetDoc.windows['5h'].resetsAtMs, Date.parse('2026-10-19T18:00:00Z'));
const nowMs = Date.parse('2026-10-19T14:00:00Z');
const text5h = model.resetText(resetDoc.windows['5h'], nowMs);
assert.match(text5h, /^Resets Today .*\(in 4h 00m\)$/);
const textNoObs = model.resetText({resetsInS: 7200}, nowMs);
assert.equal(textNoObs, 'resets in 2h 00m');
const tooltip5h = model.windowTooltip(resetDoc.windows['5h'], nowMs);
assert.match(tooltip5h, /5h: 0.0% remaining/);
assert.match(tooltip5h, /Resets Today/);
console.log('PASS reset occurrence and time remaining formatting for quota windows');
const groupDoc = model.parseDocument(JSON.stringify({
  observed_at: '2026-10-19T14:00:00Z',
  providers: [{provider: 'antigravity', status: 'ok',
    quota_groups: [{name: 'Gemini Models', windows: [{name: '5h', remaining_pct: 60, resets_in_s: 7200}]}]}]
})).providers[0];
const quotaGroup = groupDoc.quotaGroups[0];
assert.equal(quotaGroup.windows[0].resetsAtMs, Date.parse('2026-10-19T16:00:00Z'));
assert.match(model.quotaGroupDetail(quotaGroup, Date.parse('2026-10-19T14:00:00Z')), /^5h: 60\.0% left · .*\(in 2h 00m\)$/);
assert.match(model.quotaGroupDetail(quotaGroup, Date.parse('2026-10-19T15:30:00Z')), /\(in 30m\)$/);
console.log('PASS quota-group reset countdowns follow the view clock');
const capacityDoc = model.parseDocument(JSON.stringify({comparison_mode: 'activity', providers: [
  {provider: 'relay', label: 'Relay Pass', status: 'unauthenticated'},
  {provider: 'nimbus', label: 'Nimbus Assist', status: 'stale', activity: {observed_turns: 30, window_days: 30, price: {monthly_usd: 10}},
    windows: [{name: '5h', remaining_pct: 43}, {name: 'week', remaining_pct: 78}]},
  {provider: 'acme', label: 'Acme Code', status: 'ok', activity: {observed_turns: 90, window_days: 30, price: {monthly_usd: 10}},
    windows: [{name: '5h', remaining_pct: 84}, {name: 'week', remaining_pct: 12}, {name: 'month', remaining_pct: 30}]},
  {provider: 'antigravity', status: 'estimate', activity: {observed_turns: 20, window_days: 30, price: {monthly_usd: 10}},
    windows: [{name: 'week', remaining_pct: 120}, {name: 'month', remaining_pct: -1}]},
  {provider: 'quarry', label: 'Quarry Agent', status: 'error', windows: [{name: '5h', remaining_pct: 90}, {name: 'week', remaining_pct: 9}, {name: 'month', remaining_pct: 30}]},
  {provider: 'orbital', label: 'Orbital CLI', status: 'unknown'},
  {provider: 'ledger', label: 'Ledger Only', status: 'ok', balance: {remaining_usd: 4, funded_usd: 10}}
]})).providers;
const capacity = model.capacityRows(capacityDoc, 35, 15);
const plain = (v) => JSON.parse(JSON.stringify(v));
assert.deepEqual(Array.from(capacity, r => r.id), Array.from(capacityDoc, p => p.id));
assert.deepEqual(Array.from(capacity, r => r.title), Array.from(capacityDoc, p => `${p.valueLabel} · ${p.name}`));
assert.equal(capacity.find(r => r.id === 'antigravity').title, `${capacityDoc.find(p => p.id === 'antigravity').valueLabel} · Google AI / Antigravity`);
const capRow = (id) => plain(capacity.find(r => r.id === id));
assert.deepEqual(Array.from(capacity, r => r.statusWord), Array.from(capacityDoc, p => p.status === 'ok' ? '' : p.statusLabel));
assert.equal(capRow('nimbus').statusWord, 'stale');
assert.equal(capRow('antigravity').statusWord, 'estimate');
assert.equal(capRow('relay').statusWord, 'sign in');
assert.equal(capRow('quarry').statusWord, 'error');
assert.equal(capRow('acme').statusWord, '');
for (const r of capacity) assert.deepEqual(plain(r.cells).map(c => [c.key, c.label]), [['5h', '5h'], ['week', 'Week'], ['month', 'Month']]);
const cellsOf = (id, field) => capRow(id).cells.map(c => c[field]);
assert.deepEqual(cellsOf('acme', 'text'), ['84%', '12%', '30%']);
assert.deepEqual(cellsOf('acme', 'fill'), [0.84, 0.12, 0.3]);
assert.deepEqual(cellsOf('acme', 'tone'), ['ok', 'urgent', 'warn']);
assert.deepEqual(plain(model.capacityRows(capacityDoc, 90, 15).find(r => r.id === 'acme').cells).map(c => c.tone), ['warn', 'urgent', 'warn']);
// A reading that is not live never takes the "ok" colour, but a low one keeps its urgent or warn tone:
// the Budget panel and the bar do not hide a stale or failed reading that is nearly used up either.
assert.deepEqual(cellsOf('nimbus', 'text'), ['43%', '78%', 'not reported']);
assert.deepEqual(cellsOf('nimbus', 'tone'), ['muted', 'muted', 'muted']);
assert.deepEqual(cellsOf('nimbus', 'fill'), [0.43, 0.78, 0]);
assert.deepEqual(cellsOf('nimbus', 'known'), [true, true, false]);
assert.deepEqual(cellsOf('antigravity', 'text'), ['not reported', '120%', 'not reported']);
assert.deepEqual(cellsOf('antigravity', 'fill'), [0, 1, 0]);
assert.deepEqual(cellsOf('antigravity', 'tone'), ['muted', 'muted', 'muted']);
assert.deepEqual(cellsOf('quarry', 'text'), ['90%', '9%', '30%']);
assert.deepEqual(cellsOf('quarry', 'tone'), ['muted', 'urgent', 'warn']);
// No current reading at all is "no reading", not a claim about the provider's limits; a live reading
// that lacks a window says "not reported".
assert.deepEqual(cellsOf('relay', 'text'), ['no reading', 'no reading', 'no reading']);
assert.deepEqual(cellsOf('relay', 'fill'), [0, 0, 0]);
assert.deepEqual(cellsOf('relay', 'tone'), ['muted', 'muted', 'muted']);
assert.deepEqual(cellsOf('orbital', 'text'), ['no reading', 'no reading', 'no reading']);
assert.deepEqual(cellsOf('ledger', 'text'), ['not reported', 'not reported', 'not reported']);
// The row's cells carry the status word and window for a screen reader, which reads each number alone.
assert.deepEqual(Array.from(capRow('nimbus').cells, c => c.speech), ['Nimbus Assist (stale) 5h: 43%', 'Nimbus Assist (stale) Week: 78%', 'Nimbus Assist (stale) Month: not reported'].map(t => t.replace('Nimbus Assist', capRow('nimbus').title)));
assert.equal(capRow('acme').cells[1].speech, `${capRow('acme').title} Week: 12%`);
assert.deepEqual(plain(model.capacityRows([], 35, 15)), []);
// Panel is a PanelWindow and cannot render without a layer-shell backend, so its hand-over of the
// headroom thresholds to the view is guarded at the source.
const panelSource = fs.readFileSync(path.join(__dirname, '..', 'Panel.qml'), 'utf8');
assert.match(panelSource, /XrayView\s*\{[^}]*warnHeadroomPct:\s*root\.warnHeadroomPct[^}]*lowHeadroomPct:\s*root\.lowHeadroomPct/);
console.log('PASS capacity rows: ranking order, full names, status words, not-reported and no-reading cells, clamped fills, tones');

