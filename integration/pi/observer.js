/** Private pending-only Pi activity export. No prompts, tools or output text. */
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const PROVIDERS = Object.freeze({commandcode: 'command-code', 'command-code': 'command-code',
  clinepass: 'clinepass', 'opencode-go': 'opencode-go', 'openai-codex': 'codex'});
const hash = value => createHash('sha256').update(value).digest('hex');

export function observation(sessionId, entry) {
  if (typeof sessionId !== 'string' || !sessionId || sessionId.length > 256 || entry?.type !== 'message'
      || typeof entry.id !== 'string' || !entry.id || entry.id.length > 256) return null;
  const message = entry.message;
  const provider = message?.role === 'assistant' && typeof message.provider === 'string'
    && Object.hasOwn(PROVIDERS, message.provider) && PROVIDERS[message.provider];
  if (!provider || !['stop', 'length', 'toolUse', 'error', 'aborted'].includes(message.stopReason)) return null;
  const at = typeof entry.timestamp === 'string' ? Date.parse(entry.timestamp) : NaN;
  if (!Number.isFinite(at)) return null;
  const task = hash(sessionId + '\0' + provider);
  return {task_id: 'pi:' + task, provider, event_id: 'pi-turn:' + hash(sessionId + '\0' + entry.id),
    occurred_at: new Date(at).toISOString(), errors: 0, assistant_error: message.stopReason === 'error'};
}

async function save(directory, item) {
  // Advisory lock lifetime follows this bounded child, including crashes.
  const helper = fileURLToPath(new URL('./write_event.py', import.meta.url));
  await new Promise((resolve, reject) => {
    const child = spawn('python3', [helper, directory], {stdio: ['pipe', 'ignore', 'ignore']});
    const timeout = setTimeout(() => { child.kill('SIGKILL'); }, 2000);
    child.once('error', error => { clearTimeout(timeout); reject(error); });
    child.once('exit', code => { clearTimeout(timeout); code === 0 ? resolve() : reject(new Error('export_failed')); });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(item));
  });
}

export function createObserver({directory, onError = () => {}, maxPending = 256, write = save}) {
  let queue = [], running = false, closed = false;
  const waiters = [];
  function warn(code) { try { onError(code); } catch {} }
  async function drain() {
    if (running) return;
    running = true;
    while (queue.length) {
      const item = queue.shift();
      try { await write(directory, item); }
      catch { warn('activity_export_failed'); } // Never expose paths, provider body or credentials.
    }
    running = false;
    for (const resolve of waiters.splice(0)) resolve();
  }
  return {
    enqueue(item) {
      if (!item || closed) return false;
      if (queue.length >= maxPending) { warn('activity_queue_full'); return false; }
      queue.push(item); void drain(); return true;
    },
    flush() { return running || queue.length ? new Promise(resolve => waiters.push(resolve)) : Promise.resolve(); },
    close() { closed = true; return this.flush(); },
  };
}
