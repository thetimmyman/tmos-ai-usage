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
