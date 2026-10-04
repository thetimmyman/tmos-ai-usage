# Changelog

## Unreleased

- Hold the whole-body read deadline during a blocking receive: each read gets only what is left of it, so a server that sends a byte and stalls can no longer stretch an 8 s read toward 16 s.
- Keep a Claude quota reading **stale** after a failed refresh, including while the call interval throttles the next attempt; only a successful read marks it current again.
- Bind the Claude quota gate (cached reading, backoff, rejected token) to the signed-in account, so switching accounts fetches the new account instead of showing the old one's meter; a cached meter is only reused for the token that read it.
- Mark a stale Claude meter in the collapsed bar and tooltip (` · stale`, warning tone), and in setup as detected rather than "sign in first".
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
