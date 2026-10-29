# Changelog

## Unreleased

- Read Command Code's quota with every credential the machine may hold — `$COMMAND_CODE_API_KEY`, `~/secrets/command-code.env`, the Command Code CLI's own login and Pi's `/login` — and pass over a credential the endpoint rejects (HTTP 401/403) for the next source, so a key Command Code retired in one store no longer hides a fresh sign-in in another. When every credential is rejected the row asks for `cmd login` and names the sources it read, never a key.
- Read theme colours through the `Commons` alias (`Commons.Color.foreground`). Qt exposes its own `Color` value type, which shadows the bare name on current Omarchy, so every themed colour in the bar widget and panels was undefined and the text and borders fell back to Qt's defaults.
- Read Google AI / Antigravity subscription quota through the official CLI's read-only JSON `/usage` command; keep Gemini and Claude/GPT allowances separate and credentials inside `agy`.
- Count local Antigravity activity, including runs delegated from Pi, from one canonical transcript per conversation; exclude duplicate exports and avoid inferred model, fee, cache-overlap, or task-validation claims.
- Add Antigravity to Setup, subscription-price entry, and explicit task capture.
- Read OpenCode's v1 `message` table alongside v2 `session_message` during the cutover, counting each message once by id, so sessions not yet migrated keep their history.
- Count Pi turns once: Command Code and OpenCode Go history that already includes Pi sessions records how many prompts came from Pi, and the activity ranking no longer adds them again.
- Read Pi history where Pi writes it: an absolute `PI_CODING_AGENT_DIR` or `PI_CODING_AGENT_SESSION_DIR`, or the `sessionDir` setting. A forked or cloned Pi session's copied history counts once, also when the file cap leaves out the session it was copied from, and a Pi row dated after the collection time waits for its time instead of creating a future day.
- Read an Antigravity transcript over 32 MiB from its most recent 32 MiB instead of its first, and say in the activity note that the history is incomplete.
- Count only what OpenCode Go answered in OpenCode's history: prompts and replies served by other providers, local models included, are no longer charged to OpenCode Go. A v2 prompt, which names no provider, belongs to the provider of the reply that answers it.
- Count OpenCode's reasoning tokens, as output and in the total, where OpenCode stored no total of its own.
- Give each `agy` quota check one log file, `agy-cli.log` in the state directory, rewritten every run, instead of a new log in agy's own folder every five minutes. A release without `--log-file` is reported as needing an update and is not run.
- An OpenCode row dated after the collection time waits for its time, and a scan that left one out is not cached, so a later refresh counts it.
- Cache signatures compare modification times to the nanosecond, so an Antigravity export or OpenCode WAL rewritten at the same size within one second is read again.
- Say when a relative Pi session directory (`PI_CODING_AGENT_SESSION_DIR`, `PI_CODING_AGENT_DIR` or `sessionDir`) keeps Pi sessions out of the counts. Pi resolves it from each session's folder, which the collector cannot know; the default directory is still read.
- Model-group reset countdowns follow the panel's clock instead of freezing at the time the usage document was read.

- Hold the whole-body read deadline during a blocking receive: each read gets only what is left of it, so a server that sends a byte and stalls can no longer stretch an 8 s read toward 16 s.
- Keep a Claude quota reading **stale** after a failed refresh, including while the call interval throttles the next attempt; only a successful read marks it current again.
- Bind the Claude quota gate (cached reading, backoff, rejected token) to the signed-in account, so switching accounts fetches the new account instead of showing the old one's meter; a cached meter is only reused for the token that read it.
- Mark a stale Claude meter in the collapsed bar and tooltip (`· stale`, warning tone), and in setup as detected rather than "sign in first".
- `--probe` honours `--state-dir`, so a diagnostic probe no longer reads or writes the live gate state.

## 0.3.2 · 2026-10-03

- Gate Claude Code's quota endpoint: honour `Retry-After` on a 429, never send an expired or already-rejected token, and reuse a reading taken within the last 10 minutes, so five-minute polling no longer keeps the account rate-limited.
- Serve the last Claude quota reading as **stale** while the endpoint is gated, dropping any window that has reset since; say when the token needs `claude` to refresh it.
- Never follow HTTP redirects on credential-bearing provider and console-export requests, so `Authorization` and provider headers cannot be re-sent to another host; a redirect is reported as "HTTP 30x".
- Bound provider responses while reading (2 MiB per usage document, 20 MiB for the console CSV), independent of `Content-Length`, with a whole-body deadline alongside the socket timeout.

## 0.3.1 · 2026-09-24

- Add a native first-run setup guide with provider readiness, price confirmation, and report-inbox instructions.
- Initialize private state automatically when setup opens; preserve existing billing, credentials, and usage history.
- Add explicit enable/disable controls for the task command and Pi observer, with conflict-safe managed links.
- Keep optional tracking opt-in and explain the manual evidence required for validated-task value.
- Test first-run persistence, initialization races, managed-link installation/removal, and clean-home behavior.

## 0.3.0 · 2026-09-24

Source released; marketplace listing submitted for maintainer review.

- Add native task review with explicit reviewer acceptance, retained check receipts, stale-run guards, and a `tmos-ai-task` capture command.
- Document provider report capabilities and distinguish consumer subscriptions from organization API billing.
- Add an optional Pi extension for private, pending-only provider activity capture, with bounded writes, replay deduplication, and no automatic task validation.
- Resume Cline billing-history pagination across refreshes with private account-scoped checkpoints and deduplication; distinguish local activity from provider report coverage.
- Simplify the overview: put ranked values first, align numeric columns, and show compact request, validation, and receipt coverage without overflowing rows.
- Confirm successful price saves and queue a follow-up refresh when a save overlaps an active collection, so rankings receive the persisted fee.
- Fix subscription price editors carrying the previous provider’s amount or billing cycle across tab changes; bind saves to the editor’s provider and preserve unsaved edits during refresh.
- Add native Inference X-RAY overview and provider detail tabs with shared provisional and evidence-qualified rankings.
- Show report, observed outcome, and invoice coverage separately; keep unknown amounts unknown and invoice cash distinct from known service allocation.
- Distinguish user-entered, last-invoice, plan-quote and published-estimate subscription price bases.
- Import request exports and outcome events from private local state, with sanitized rejected/waiting counts.
- Add explicit task capture, verification, reviewer acceptance, invoice import, and comparison publication tools.
- Preserve the existing `tmos.usage` identity, settings, collector service, and Omarchy entry points.
