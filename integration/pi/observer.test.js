import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { spawnSync } from 'node:child_process';
import { createObserver, observation } from './observer.js';
import extension from './index.js';

const entry = (id, provider = 'commandcode', stopReason = 'stop') => ({type: 'message', id,
  timestamp: '2026-09-24T21:00:00.000Z', message: {role: 'assistant', provider, stopReason,
    get content() { throw new Error('must not access transcript content'); },
    get errorMessage() { throw new Error('must not access provider/error text'); }}});
async function sandbox(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'tmos-pi-test-'));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  return root;
}
async function events(directory) {
  const files = (await fs.readdir(directory)).filter(name => name.endsWith('.jsonl'));
  return (await Promise.all(files.map(name => fs.readFile(path.join(directory, name), 'utf8'))))
    .flatMap(raw => raw.trim().split('\n').map(line => JSON.parse(line)));
}

test('explicit providers only; private fields never accessed; generic errors are not upstream', () => {
  const item = observation('session', entry('a', 'opencode-go', 'error'));
  assert.equal(item.provider, 'opencode-go'); assert.equal(item.errors, 0); assert.equal(item.assistant_error, true);
  assert.equal(observation('session', entry('a', 'constructor')), null);
  assert.equal(observation('session', entry('a', '__proto__')), null);
  assert.equal(observation('session', entry('a', 'anthropic')), null);
  assert.equal(observation('session', entry('a', 'local-qwen')), null);
  assert.equal(observation('session', entry('a', 'commandcode', 'pending')), null);
});

test('private atomic exports deduplicate replay/resume and separate mixed providers, no finalization', async t => {
  const root = await sandbox(t), directory = path.join(root, 'outcome-events');
  let writer = createObserver({directory});
  const a = observation('s', entry('a')), b = observation('s', entry('b', 'opencode-go', 'error'));
  writer.enqueue(a); writer.enqueue(a); writer.enqueue(b); await writer.close();
  writer = createObserver({directory}); writer.enqueue(a); writer.enqueue(b); await writer.close();
  const rows = await events(directory);
  assert.equal(rows.length, 4); assert.equal(new Set(rows.map(e => e.task_id)).size, 2);
  assert.equal(rows.filter(e => e.type === 'task_finalized').length, 0);
  assert.equal(rows.filter(e => e.type === 'turns_recorded').reduce((n,e) => n+e.errors, 0), 0);
  for (const filename of await fs.readdir(directory)) {
    assert.equal((await fs.stat(path.join(directory, filename))).mode & 0o077, 0);
  }
  const sidecar = (await fs.readdir(directory)).find(name => name.endsWith('.assistant-errors.json'));
  assert.equal(JSON.parse(await fs.readFile(path.join(directory, sidecar))).event_ids.length, 1);
  // Verify compatibility with the real Python collector/ledger, entirely in temp state.
  const result = spawnSync('python3', ['-c', `import sys,json;sys.path.insert(0,'collector');import outcome_ingest,outcome_ledger;from pathlib import Path;p=Path(sys.argv[1]);r=outcome_ingest.ingest(p);assert r['rejected_files']==0,r;d=json.loads((p/'outcome-ledger.json').read_text());assert all(t['status']=='pending' for t in d['tasks'].values());print(r['applied'])`, root], {encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr); assert.equal(result.stdout.trim(), '4');
});

test('bounded queue and filesystem failure never reject caller or touch symlink target', async t => {
  const root = await sandbox(t), directory = path.join(root, 'link'), target = path.join(root, 'target');
  await fs.mkdir(target, {mode: 0o700}); await fs.symlink(target, directory);
  const warnings = []; const writer = createObserver({directory, onError: c => warnings.push(c)});
  writer.enqueue(observation('s', entry('a'))); await writer.close();
  assert.deepEqual(await fs.readdir(target), []); assert.deepEqual(warnings, ['activity_export_failed']);
  let release; const wait = new Promise(resolve => {release = resolve;});
  const bounded = createObserver({directory: target, maxPending: 1, write: () => wait, onError: c => warnings.push(c)});
  assert(bounded.enqueue(observation('s', entry('a')))); assert(bounded.enqueue(observation('s', entry('b'))));
  assert.equal(bounded.enqueue(observation('s', entry('c'))), false);
  release(); await bounded.close(); assert(warnings.includes('activity_queue_full'));
});

test('real lifecycle API registers pending new entries only and ignores pre-install history', async t => {
  const root = await sandbox(t), prior = process.env.TMOS_USAGE_STATE_DIR;
  process.env.TMOS_USAGE_STATE_DIR = root;
  t.after(() => { if (prior === undefined) delete process.env.TMOS_USAGE_STATE_DIR; else process.env.TMOS_USAGE_STATE_DIR = prior; });
  const handlers = new Map(); extension({on: (name, handler) => handlers.set(name, handler)});
  const history = [entry('old')];
  const ctx = {hasUI: false, sessionManager: {getSessionId: () => 'live-session', getEntries: () => history}};
  handlers.get('session_start')({}, ctx);
  history.push(entry('new', 'openai-codex'));
  assert.equal(handlers.get('turn_end')({}, ctx), undefined); // No promise delays inference.
  handlers.get('agent_settled')({}, ctx);
  await handlers.get('session_shutdown')({}, ctx);
  const rows = await events(path.join(root, 'outcome-events'));
  assert.equal(rows.length, 2); assert.equal(rows[0].provider, 'codex');
  assert.equal(rows[1].event_id, observation('live-session', entry('new', 'openai-codex')).event_id);
});

test('concurrent observers preserve both turns and an existing unlocked lock permits resume', async t => {
  const root = await sandbox(t), directory = path.join(root, 'outcome-events');
  const a = createObserver({directory}), b = createObserver({directory});
  a.enqueue(observation('shared', entry('a'))); b.enqueue(observation('shared', entry('b')));
  await Promise.all([a.close(), b.close()]);
  const c = createObserver({directory}); c.enqueue(observation('shared', entry('c'))); await c.close();
  assert.equal((await events(directory)).length, 4);
});

test('branch navigation rebaselines equal/larger histories instead of importing old branch turns', async t => {
  const root = await sandbox(t), prior = process.env.TMOS_USAGE_STATE_DIR;
  process.env.TMOS_USAGE_STATE_DIR = root;
  t.after(() => { if (prior === undefined) delete process.env.TMOS_USAGE_STATE_DIR; else process.env.TMOS_USAGE_STATE_DIR = prior; });
  const handlers = new Map(); extension({on: (name, handler) => handlers.set(name, handler)});
  let history = [entry('original')];
  const ctx = {hasUI: false, sessionManager: {getSessionId: () => 'branch-session', getEntries: () => history}};
  handlers.get('session_start')({}, ctx);
  history = [entry('old-branch-a')]; handlers.get('session_tree')({}, ctx);
  history = [entry('old-branch-b'), entry('old-branch-c')]; handlers.get('session_tree')({}, ctx);
  history.push(entry('actual-new')); handlers.get('agent_settled')({}, ctx);
  await handlers.get('session_shutdown')({}, ctx);
  const rows = await events(path.join(root, 'outcome-events'));
  assert.equal(rows.length, 2);
  assert.equal(rows[1].event_id, observation('branch-session', entry('actual-new')).event_id);
});

test('session replacement start after shutdown observes first new turn with fresh writer', async t => {
  const root = await sandbox(t), prior = process.env.TMOS_USAGE_STATE_DIR;
  process.env.TMOS_USAGE_STATE_DIR = root;
  t.after(() => { if (prior === undefined) delete process.env.TMOS_USAGE_STATE_DIR; else process.env.TMOS_USAGE_STATE_DIR = prior; });
  const handlers = new Map(); extension({on: (name, handler) => handlers.set(name, handler)});
  let id = 'before', history = [];
  const ctx = {hasUI: false, sessionManager: {getSessionId: () => id, getEntries: () => history}};
  handlers.get('session_start')({reason: 'startup'}, ctx);
  await handlers.get('session_shutdown')({reason: 'resume'}, ctx);
  id = 'after'; history = [entry('historical')];
  handlers.get('session_start')({reason: 'resume'}, ctx);
  history.push(entry('first-new'));
  handlers.get('turn_end')({}, ctx);
  await handlers.get('session_shutdown')({reason: 'quit'}, ctx);
  const rows = await events(path.join(root, 'outcome-events'));
  assert.equal(rows.length, 2);
  assert.equal(rows[1].event_id, observation('after', entry('first-new')).event_id);
});
