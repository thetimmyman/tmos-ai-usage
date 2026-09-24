# Working comparisons before validated task evidence exists

The dashboard now defaults to **Observed turns / $ · provisional** while complete
validated-task receipts are unavailable. Overview, provider tabs and the quick
budget dropdown share this ordering. `#1*` means the highest **observed local turns
in the collector's 30-day window / monthly subscription fee**. It does not mean
best task quality or lowest cost per accepted task. Sparse logs and activity in
other harnesses can materially change this order; this is not a buying recommendation.

Native CLI transcripts and `~/.pi/agent/sessions` contribute turns. Pi turns are
attributed to the first assistant provider following a user message, deduplicated
by session/entry identity; generic API providers are not assigned to subscriptions.
Ephemeral `--no-session` workers and other machines are not covered.
CLI and Pi logs have no shared request identity: imported/mirrored work across
those two stores can count twice, so keep each store as its native log source. Provider
request counts remain separate and are never treated as user turns or validated
tasks. Codex token reporting deduplicates per-response usage records, with a
snapshot-deduplicating fallback for older logs.

Each provider tab has **Subscription price**: enter a USD amount, choose Monthly
or Annual, and save. Annual fees normalize to monthly by dividing by twelve.
These user-entered prices are stored privately in `subscriptions.json`; they do
not manufacture recognized-spend receipts for the validated-task comparison.

Without a user-entered fee, dated published price estimates are available for
exactly detected Command Code GOAT, OpenCode Go and Claude Max tiers. Claude's
local tier distinguishes 5x from 20x; a generic Max label cannot choose a price.
Cline's plan API supplies its per-seat annual/monthly quote. Quotes are explicitly
estimated fees, not invoices, taxes, top-ups or proof of actual spend. Generic
Codex Pro metadata does not establish a fee; the UI says **Set price**.

The **Validated tasks / $** toggle retains the strict comparison below. Switching
metrics changes the overview, provider tabs and quick dropdown together. Every
missing evidence row explains what is absent. The default moves to validated
only once all displayed providers have matching projected evidence; selecting a
mode manually holds it for that widget's lifetime.

# Inference X-RAY

Open the AI budget popup, then **Open Inference X-RAY**. Overview compares every
subscription. Provider tabs, overview rows and the budget dropdown share `Value.js`.
Rank #1 has the most validated tasks per recognized USD; ties share a rank. Free
and unavailable evidence are separate. The comparison period and workload cohort
must match. A successful inference request is not a completed task.

This is an upgrade to `tmos.usage`, using native Omarchy Color/Style tokens. It has
no browser, network calls or credentials in QML. Existing settings and widget ID
are retained. No Odysseus installation is required.

## Reports

- Command Code: billing-period usage-summary API, refreshed with quota collection.
  Counts completed/failed requests, input/output tokens and credits. Credits are
  not subscription cash spend. A report failure retains the quota reading.
- Cline: latest 100 usage API billing records, explicitly a bounded sample.
  Token categories remain separate. Despite the field name `costUsd`, observed
  values require unit verification; the UI does not present them as USD.
- OpenCode: import the Console JSON export with the command below. Requests are
  deduplicated by workspace and row identity, credentials and raw metadata are
  excluded, and truncation/time coverage remain visible. The Console organization
  CSV API uses a separate service-account credential and is not configured here.
- Claude/Codex: existing local token collectors and quota adapters. Organization
  billing APIs are not substituted for personal subscription reports.

```bash
python3 collector/request_log_import.py ~/Downloads/request-logs-2026-09-24.json \
  --ledger ~/.local/state/tmos-ai-usage/request-ledger.json \
  --summary ~/.local/state/tmos-ai-usage/request-summary.json
```

Refresh the popup afterward. Ledger, summary and usage cache writes are atomic and
private (0600). Imported observations remain a partial sample, not full billing
coverage. The importer supports the supplied OpenCode JSON schema; other exports
need dedicated adapters.

## Outcome and recognized-spend receipts

A harness can produce private receipts under the plugin state directory. It must
attest truthful validation, account for the whole comparison population, and use
consistent task attribution and spend allocation across providers. Hash checks
establish snapshot integrity, not whether a test genuinely proves task quality.
There is no automatic outcome producer connected to this installation yet.

`economics.json` contains:

```json
{
  "context": {"period": "2026-09", "cohort": "bounded-fixes"},
  "providers": {
    "codex": {"outcomes_sha256": "<64 lowercase hex>", "spend_sha256": "<64 lowercase hex>"}
  }
}
```

Each reference resolves to `evidence/<sha256>.json`; its raw bytes must match.
Both receipts require matching `period`, `cohort`, `provider` and
`coverage_complete: true`. These fields are declarations from the trusted producer.
An outcomes receipt additionally contains `population_task_ids` and `tasks`:

```json
{"id":"task-123", "status":"validated", "turns":3,
 "reworked":false, "evidence_sha256":"<validation evidence digest>"}
```

Statuses are `validated`, `failed`, or `abandoned`; unfinished populations cannot
rank. Every declared task must appear exactly once. Count a task once across a
comparison; do not credit every fallback provider with the same completion.

A spend receipt has `basis: "recognized_subscription_and_metered_usd"` and
`charges` containing unique `id`, nonnegative `recognized_usd` and
`evidence_sha256` references to billing/allocation evidence. Recognize annual
subscriptions over their service periods and include idle subscription expense.
Do not substitute API list-price equivalents, promotional credit face value, or
prepaid top-ups for recognized expense. A truly free comparison still needs an
explicit zero-cost charge attestation. Missing receipts do not mean zero spend.

Only allowlisted derived metrics and hashes reach the cache. Turns, failures and
rework are shown when valid receipts exist. Complete outcome capture and mixed
provider attribution must be connected in the routing harness before a purchase
recommendation is trustworthy.

## Promotions and routing

`collector/offer-observations.json` holds dated, sourced observations, shown with a
24-hour freshness limit. They are not automatically refreshed or treated as
routing authority. Providers may end a preview or hit capacity early. A fresh
observation still needs callable-route, allowance and eligibility checks before
dispatch. No paid fallback is enabled by this plugin.

The separate PS-679 harness work contains the immutable offer-receipt contract;
binding those receipts to real dispatch/outcome records is still pending.

## Release checks

Run `bash scripts/check.sh`: offline collectors, privacy/accounting tests, shared
ranking tests, QML syntax, native Quickshell smoke test (on Omarchy), manifest
validation. The runtime test exercises overview and a provider tab offscreen.
The marketplace update uses the existing repository and plugin identity. Local
installation and testing do not imply marketplace publication.
