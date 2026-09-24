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
An optional [Pi observer](integration/pi/README.md) also captures new persisted
assistant-response metadata into pending provider-session segments. These are
observations, not validated tasks, and do not qualify for task-value rankings.
The observer must be installed separately in each selected Pi agent directory.
CLI and Pi logs have no shared request identity: imported/mirrored work across
those two stores can count twice, so keep each store as its native log source. Provider
request counts remain separate and are never treated as user turns or validated
tasks. Codex token reporting deduplicates per-response usage records, with a
snapshot-deduplicating fallback for older logs.

Each provider tab has **Subscription price**: enter a USD amount, choose Monthly
or Annual, and save. Annual fees normalize to monthly by dividing by twelve.
These provisional comparison inputs are stored privately in `subscriptions.json`;
they do not manufacture recognized-spend receipts for the validated-task comparison.
For a receipt-derived provisional amount, provide its digest explicitly:

```sh
python3 collector/subscription_value.py --state-dir ~/.local/state/tmos-ai-usage \
  --provider clinepass --amount "$INVOICE_AMOUNT" --cycle year \
  --source-kind receipt --source-sha256 "$SOURCE_SHA256"
```

Set those variables from the private receipt record and its SHA-256 digest before
running the command. The UI labels this basis **last invoice amount — not guaranteed future charge**.
Only its source kind is shown; the digest and source receipt remain private.

Without a user-entered fee, dated published price estimates are available for
exactly detected Command Code GOAT, OpenCode Go and Claude Max tiers. Claude's
local tier distinguishes 5x from 20x; a generic Max label cannot choose a price.
Cline's plan API supplies its per-seat annual/monthly quote. Provider quotes and
published amounts are estimates, not invoices, taxes, top-ups or proof of actual spend. Generic
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

- Command Code: the current billing-period usage summary endpoint reports
  aggregate request counts, token totals and credits; it is not a per-request
  history export. The public Studio docs describe request-level details in the
  UI, but do not document a personal usage export API. Credits are not
  subscription cash spend. A report failure retains the quota reading.
- Cline: the authenticated account's cursor-paginated `/users/{id}/usages`
  route is read in batches of at most 100 records, ten pages per refresh. A
  private checkpoint resumes the walk on later refreshes and merges by hashed
  record ID. Repeated cursors, storage limits and later-page failures remain
  visibly partial. Reaching the endpoint's end is not proof of complete
  provider retention or a complete comparison period. Only token categories
  and model names are retained. Despite the field name `costUsd`, observed
  values require unit verification; the UI does not present them as USD. The
  usage-limit and detail routes are not documented as a stable public personal
  reporting API, so future API changes can make this report unavailable.
- OpenCode: import the Console JSON export with the command below. Requests are
  deduplicated by workspace and row identity, credentials and raw metadata are
  excluded, and truncation/time coverage remain visible. The Console organization
  CSV API uses a separate service-account credential; the optional adapter is
  described below. It reports organization-wide workspace activity, which may
  include other members, products and web-search charges; it does not represent
  personal OpenCode Go subscription usage or cash spend.
- Claude Code/Codex personal subscriptions: local transcript collectors are
  the request-activity source, alongside first-party OAuth quota readings.
  Anthropic's Admin Usage/Cost Reports and OpenAI's Platform Usage API/CSV
  describe API organizations, not Claude Pro/Max or ChatGPT/Codex personal-plan
  usage. They are not substituted for these personal reports. ChatGPT account
  data export is a manual privacy export, not a documented live usage API.

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
The optional native task wrapper and Odysseus bridge produce future outcome events. Existing conversations are not retroactively declared validated.

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
24-hour freshness limit. The collector refreshes recognized official deal banners every six hours into private state. Failed refreshes preserve the original timestamp. Advertisements are never routing authority. Providers may end a preview or hit capacity early. A fresh
observation still needs callable-route, allowance and eligibility checks before
dispatch. No paid fallback is enabled by this plugin.

The companion Odysseus PS-679 change binds verified offers to dispatch and records explicit task outcomes. Its configuration and acceptance bridge are documented in `docs/PS679-USAGE-BRIDGE.md` in that repository. Native CLIs require the explicit wrapper below; installing this plugin does not silently replace their commands.

## Release checks

Run `bash scripts/check.sh`: offline collectors, privacy/accounting tests, shared
ranking tests, QML syntax, native Quickshell smoke test (on Omarchy), manifest
validation. The runtime test exercises overview and a provider tab offscreen.
This keeps the existing repository and plugin identity, but the marketplace does
not yet have a `tmos.usage` entry. After merge to the public repository, submit its
first listing with the [marketplace submission form](https://github.com/omacom/omarchy-plugin-marketplace/issues/new?template=submit-plugin.yml)
and [publishing guide](https://plugins.omarchy.org/publish.html). Future updates
to that listing use the [verification/update form](https://github.com/omacom/omarchy-plugin-marketplace/issues/new?template=verify-plugin.yml)
and a full target commit. Local installation and testing do not imply publication.

## Automatic export inbox and optional Console API

Place OpenCode request-log JSON or documented Console usage CSV in
`~/.local/state/tmos-ai-usage/imports/` and refresh. JSON imports merge by row identity;
Console CSV remains a workspace snapshot and is not merged with Go subscriptions.
Originals remain untouched. Bounded batches rotate so older files cannot block
later ones; malformed exports do not prevent other files from importing. State is
private, atomic, and idempotent. Custom state directories use their own `imports/`.

For automatic Console CSV retrieval, explicitly provide
`OPENCODE_CONSOLE_SERVICE_KEY` to the collector's environment. This is a Console
service-account key with usage-read access, **not** the Go/Zen inference key. The
adapter requests the documented organization-scoped 30-day export, whose range
starts at midnight UTC. It labels workspace scope and converts microcents using
100,000,000 per USD. BYOK, free and unclassified records can report zero; zero is
not evidence of a free subscription. API/report failures do not erase quota data.

Reference: https://opencode.ai/v2/docs/console/usage/

Provider distinctions: [Command Code Studio](https://commandcode.ai/docs/studio),
[Cline API overview](https://docs.cline.bot/api/overview),
[Anthropic Admin Usage API](https://docs.anthropic.com/en/api/admin-api/usage-cost/get-messages-usage-report),
[OpenAI Platform Usage API](https://platform.openai.com/docs/api-reference/usage/completions),
[ChatGPT data export](https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data),
and [Codex/Work usage and Personal Analytics](https://help.openai.com/en/articles/20001478-reviewing-work-and-codex-usage-and-using-personal-analytics-in-chatgpt-desktop).

## Invoice history and service-period expense

Import a minimal invoice record and a private source receipt:

```sh
python3 collector/billing_ledger.py --state-dir ~/.local/state/tmos-ai-usage \
  --invoice /private/invoice.json --evidence /private/original-receipt
```

Record fields: `id`, `provider`, `currency: "USD"`, timezone-aware `paid_at`,
`base_usd`, `discount_usd`, `tax_usd`, optional `fees_usd`, `paid_usd`, and
`source_ref`. Amounts must reconcile exactly in cents. Optional `service_start`
and `service_end` form a half-open service interval. If the receipt gives dates
only, set `service_precision: "calendar_date_utc"`; allocation uses midnight UTC
as an explicit accounting convention. Otherwise timestamps require timezones.
Do not infer service dates from a payment date or label a discount as proration
unless the source establishes that.

The private ledger preserves source bytes and validates hashes, conflicts and
duplicate evidence. The dashboard exposes only approved amounts/dates. It shows
cash paid separately from the known portion of elapsed service expense, including
taxes, discounts and fees. Unknown service periods prevent complete cost claims;
annual plans are allocated across their actual service interval. An incomplete
invoice collection never changes the default provisional ranking. Refunds and
foreign-currency conversion require separate supported allocation evidence and
are rejected by the invoice importer rather than silently treated as purchases.

## Capture future native CLI tasks

Use an explicit wrapper around an existing agent command. It does not replace
saved commands, install hidden hooks, or make an inference call on its own:

```sh
python3 collector/task_runner.py run --state-dir ~/.local/state/tmos-ai-usage \
  --provider command-code --cohort repository-fixes --task-id fix-123 \
  --artifact /absolute/path/to/result.patch -- \
  command-code --model deepseek-v4.1-flash --print 'Implement the assigned fix'
python3 collector/task_runner.py verify --state-dir ~/.local/state/tmos-ai-usage \
  --task-id fix-123 --reviewer reviewer-id -- python3 -m unittest discover
# After an independent review confirms the requested result:
python3 collector/task_runner.py accept --state-dir ~/.local/state/tmos-ai-usage \
  --task-id fix-123 --reviewer reviewer-id
```

A successful process or check remains pending until explicit semantic acceptance.
Use `fail` or `abandon` to record that outcome; rerunning a terminal task records
rework. Process failure is not automatically an upstream-provider error. The wrapper
stores private command hashes and execution metadata, not command arguments,
prompts or captured output. Commands execute as argv arrays with no shell.
When a task produces a file, pass its output path with `run --artifact`; the wrapper
hash-binds that file through verification and acceptance. Without `--artifact`, the
record is metadata-only and does not bind repository changes or other outputs. Use
nonempty checks that exercise the requested result, then have an independent reviewer
inspect the artifact and explicitly accept the task. A passing check command alone
does not validate semantic quality.

Odysseus can instead export `events.jsonl` into the state's `outcome-events/`
directory. Refresh ingests events idempotently. Events distinguish registration,
turn/error increments, finalization and reopening. Validation requires a retained,
task-bound semantic attestation and successful check references; existing task
history with no attestation remains unknown. The observation window groups tasks
by start time and reports their latest state, not a historical state reconstruction.

## Publish a validated comparison

A comparison producer must explicitly attest complete task and subscription-spend
coverage. Use `comparison_publish.py --help` for its period/cohort/provider flags.
The publisher derives counts from terminal task records, verifies retained proofs,
requires invoice service coverage across the comparison interval, and allocates
all overlapping subscription cost including idle expense. It refuses pending
tasks, missing service dates, gaps, and changed evidence, then publishes the
content-addressed receipts and `economics.json` atomically. It does not accept
editable task-count totals. The completeness attestation is a trust boundary:
hashes prove integrity and cannot establish missing history or reviewer honesty.

Personal subscription billing APIs may not expose full history, and inference
logs cannot prove semantic correctness. These are visible data-availability limits,
not values to fill with guessed zeroes. Rankings are meaningful only for comparable
workload cohorts and coverage; logged turns are utilization, not model quality.
