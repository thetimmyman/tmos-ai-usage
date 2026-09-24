/** Pi 0.87+ user extension. Pending activity only; no inference control or validation. */
import path from 'node:path';
import os from 'node:os';
import { createObserver, observation } from './observer.js';

export default function tmosActivity(pi) {
  let writer, sessionId, cursor = 0, warned = false;
  const warn = (ctx, code) => {
    if (warned) return;
    warned = true;
    try { if (ctx.hasUI) ctx.ui.notify('TMOS AI Usage: activity export incomplete (' + code + ').', 'warning'); } catch {}
  };
  function baseline(ctx) {
    sessionId = ctx.sessionManager.getSessionId();
    cursor = ctx.sessionManager.getEntries().length;
  }
  function observe(_event, ctx) {
    try {
      if (!writer) return;
      if (ctx.sessionManager.getSessionId() !== sessionId) { baseline(ctx); return; }
      const entries = ctx.sessionManager.getEntries();
      if (entries.length < cursor) { baseline(ctx); return; }
      if (entries.length - cursor > 256) { cursor = entries.length; warn(ctx, 'batch_limit'); return; }
      for (; cursor < entries.length; cursor++) writer.enqueue(observation(sessionId, entries[cursor]));
    } catch { warn(ctx, 'session_metadata_unavailable'); }
  }
  pi.on('session_start', (_event, ctx) => {
    try {
      if (!writer) {
        const state = process.env.TMOS_USAGE_STATE_DIR || path.join(os.homedir(), '.local/state/tmos-ai-usage');
        if (!path.isAbsolute(state)) throw new Error('absolute state path required');
        writer = createObserver({directory: path.join(state, 'outcome-events'), onError: code => warn(ctx, code)});
      }
      baseline(ctx); // Do not reclassify historical transcript content on install/resume.
    } catch { warn(ctx, 'observer_unavailable'); }
  });
  pi.on('session_tree', (_event, ctx) => {
    try { baseline(ctx); } catch { warn(ctx, 'session_metadata_unavailable'); }
  });
  pi.on('turn_end', observe);
  pi.on('agent_settled', observe);
  pi.on('session_shutdown', async (_event, ctx) => {
    observe(_event, ctx);
    const ending = writer; writer = undefined;
    if (ending) await Promise.race([ending.close(), new Promise(resolve => { const timer = setTimeout(resolve, 250); timer.unref?.(); })]);
  });
}
