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
