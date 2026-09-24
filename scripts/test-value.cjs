const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const value = {};
vm.runInNewContext(fs.readFileSync(path.join(root, 'Value.js'), 'utf8').replace(/^\.pragma.*$/m, ''), value);
const context = {period: '2026-09', cohort: 'bounded-fix'};
const row = (id, tasks, spend, extra = {}) => ({id, name: id, economics: {
  ...context, coverage_complete: true, spend_basis: 'recognized_subscription_and_metered_usd',
  validated_tasks: tasks, spend_usd: spend, ...extra
}});
const input = [row('unknown', 99, 1, {coverage_complete: false}), row('low', 5, 10),
  row('best', 40, 10), row('tied', 20, 5), row('zero', 0, 10), row('free', 5, 0)];
const result = value.rankProviders(input, context);
assert.deepEqual(Array.from(result, r => r.id), ['best', 'tied', 'low', 'zero', 'free', 'unknown']);
assert.deepEqual(Array.from(result, r => r.valueLabel), ['#1', '#1', '#3', '#4', 'Free', 'Unranked']);
assert.equal(input[0].valueRank, undefined);
for (const extra of [{spend_usd: null}, {spend_usd: -1}, {spend_usd: Infinity},
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
