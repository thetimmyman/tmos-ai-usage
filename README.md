# TMOS AI Usage

AI subscription budgets, usage and comparisons in one Omarchy bar panel.

Rate-limit windows with reset times, prepaid balances, and local token history by day and by
model — for Claude Code, Codex, ClinePass, Command Code, OpenCode and anything else you have
configured through a supported reader. The budget dropdown and native Inference X-RAY overview
share the same ranking; clicking a budget row expands its details.

**New installation? Start with [Getting started](GETTING-STARTED.md).** The native **Setup**
guide explains what is automatic and offers optional task capture and Pi observation.

![TMOS AI Usage](preview.png)

The X-RAY preview uses synthetic provider, activity, billing and task-outcome values;
it contains no account exports or personal amounts.

## What it shows

For every provider, on the collapsed row:

- its name and plan ("Claude Code · max", "Command Code · GOAT")
- today's activity (token and prompt totals, when available)
- all three windows — **5h / week / month** — as a remaining-percent meter each, on one line
- a status badge when the number is not the provider's own (`estimate`, `sign in`, `unknown`, `error`)

Expanding a row adds, for that provider only:

- each window full width, with "resets in 1d 2h"
- the prepaid ledger, for providers that have one: remaining, spent of funded, and a draining meter
- **tokens by day** for the last seven days, scaled to the busiest day
- **tokens by model**, ranked, with the input / output / cache split on hover
- where the numbers came from — the exact transcript directory, file count and window covered

## Supported providers

Nothing is hardcoded to a subscription you do not have: a provider appears when its CLI or key is
configured, and disappears when it is not. The five below are built in; anything else that publishes
a simple usage or balance JSON at one endpoint can be added as data, without a release — see
[Adding a provider](#adding-a-provider).

| Provider | Limits | Balance | Token history |
| --- | --- | --- | --- |
| Claude Code | Anthropic's own OAuth usage endpoint (5h + weekly) | — | `~/.claude/projects` transcripts |
| Codex | the ChatGPT usage endpoint, falling back to the limit reading the Codex CLI itself recorded in `~/.codex/sessions` | — | `~/.codex/sessions` rollouts |
| ClinePass | Cline's usage-limits meter, falling back to caps + charge ledger | — | — |
| Command Code | 5h + weekly meters, monthly credit pool | prepaid credit pool | — |
| OpenCode Go | OpenCode Zen usage endpoint | — | — |

## How it works

Two halves, deliberately:

| Half | What it is | What it may do |
| --- | --- | --- |
| `collector/usage_collector.py` | python3, standard library only | reads each provider's own usage endpoint with the credential that provider's CLI already stores, read-only; writes one file |
| the QML | Quickshell plugin | reads that one file. **No network, no credentials, no endpoints in the shell process.** |

Every number on screen can be traced to a line in the collector. A number the collector could not
read is absent, not guessed; a number it derived is labelled `estimate` and says how.

## Install

```sh
omarchy plugin add https://github.com/thetimmyman/tmos-ai-usage --enable
```

On install the collector starts within a few seconds and runs every five minutes after that. Its
cache is `~/.local/state/tmos-ai-usage/usage.json`; the panel reads that file and never makes a
network request of its own. `TMOS_USAGE_STATE_DIR` moves both halves if you want the state
elsewhere.

Open the budget dropdown: the first visit opens the setup guide and initializes
private local folders. You can reopen it with **Set up AI Usage · start here**,
or **Setup · providers and optional tracking** after finishing. The X-RAY header
also has **Setup**. Sign into any desired provider with its own CLI, then choose
**Refresh now**. Setup reuses supported existing credentials read-only; it does not
install provider CLIs or sign into accounts.

In each X-RAY provider tab, confirm **Subscription price** with your actual amount
and billing cycle. Optional Setup actions install the `tmos-ai-task` command and
Pi observer by linking their bundled files; they refuse unrelated existing files.
Neither is required for automatic quota and supported local-history collection.
See [Getting started](GETTING-STARTED.md) for imports, ranking labels and next steps.

## Configure

Settings live in the widget's entry in `~/.config/omarchy/shell.json`:

```sh
omarchy bar set tmos.usage warnHeadroomPct 35    # accent below this much headroom
omarchy bar set tmos.usage lowHeadroomPct 15     # urgent below this much
omarchy bar set tmos.usage staleAfterMin 20      # call the cache stale after this long
omarchy bar set tmos.usage rotateSecs 5          # seconds per provider on the bar (0 = tightest only)
```

Move it to another bar section:

```sh
omarchy bar move tmos.usage --section right
```

## Adding a provider

The five providers above are built in. A sixth does **not** need a release, as long as its whole
reading is *one endpoint, one JSON document and a path to a number*: write a definition instead.
The format is deliberately narrower than the built-ins — it is an extension path, not a
replacement for them, and [the table below](#what-still-needs-code) is the honest boundary.

A definition is a small JSON file. This one reads two percentages, a plan name, a reset time and a
prepaid balance out of a single response:

```json
{
  "definition_version": 1,
  "id": "acme",
  "label": "Acme AI",
  "endpoint": {
    "url": "https://api.acme.example/v1/usage",
    "base_url_env": "ACME_API_BASE",
    "credential": {"kind": "env", "name": "ACME_API_KEY", "files": ["~/secrets/acme.env"]},
    "timeout_s": 8
  },
  "plan_path": "plan.name",
  "windows": [
    {"kind": "percent", "window": "5h", "path": "usage.five_hour.utilization",
     "reset": {"path": "usage.five_hour.resets_at", "format": "iso"}},
    {"kind": "percent", "window": "week", "path": "usage.seven_day.utilization",
     "reset": {"path": "usage.seven_day.resets_at", "format": "iso"}}
  ],
  "balance": {"remaining": "credits.remaining", "funded": "credits.funded", "currency": "USD"}
}
```

A provider that publishes a *ratio* rather than a percent, keys its windows by its own names, or
returns an array of limits, says so with a different `kind`:

```json
"windows": [
  {"kind": "ratio", "window": "week", "used": "windowLimits.weekly.used",
   "cap": "windowLimits.weekly.cap", "reset": {"path": "windowLimits.weekly.resetAt", "format": "epoch_ms"}},
  {"kind": "map", "from": "usage", "keys": {"rolling": "5h", "weekly": "week", "monthly": "month"},
   "percent": "percent", "reset": {"path": "resetsAt", "format": "iso"}, "status": "status"},
  {"kind": "list", "from": "data.limits", "by": "type",
   "keys": {"five_hour": "5h", "weekly": "week", "monthly": "month"}, "percent": "percentUsed"}
]
```

A provider that names each window by its **length** instead of a name, and expresses its reset only
as "in N seconds", uses `window_from_duration` and `"format": "duration_s"` — that is exactly what
the built-in Codex adapter reads.

### Where definitions live

| Directory | Purpose |
| --- | --- |
| `<plugin>/providers.d/*.json` | ships with the plugin. Empty by default: the five built-ins are code. |
| `~/.config/tmos-ai-usage/providers.d/*.json` | where your definitions go. |

`TMOS_USAGE_PROVIDERS_DIR` (a colon-separated list) replaces both, which is how the test suite and
the release gate keep a run offline and deterministic.

- A built-in adapter **always wins its id**. A definition that collides with one is reported in the
  panel, never silently dropped.
- Between two definition files, the last one read wins — so your directory overrides the plugin's —
  and the file that lost is reported too.

### Writing and testing one

```sh
python3 collector/usage_collector.py --list-providers   # every provider, and where each comes from
python3 collector/usage_collector.py --probe acme       # read that one now, and say what it saw
```

`--probe` prints the credential source (never its secret), whether that source is readable, the endpoint it
will `GET`, and then the actual reading — or the reason there is none. It exits `0` when the
provider resolved (a provider in an error state *is* the answer) and `2` for an id that does not
exist. Neither command needs a release, and `--list-providers` never touches the network.

Every example above has a runnable fixture under `collector/fixtures/providers.d/`, with its
response alongside in `collector/fixtures/providers.responses/`, and every way a definition can be
rejected has one under `collector/fixtures/providers.reject.d/`.

### The rules a definition follows

- **A definition names a credential; it never contains one.** There is no key in the schema that
  accepts a secret, and `Authorization` cannot be set from a definition — the collector writes it
  from the credential you named. Unknown keys are an error rather than ignored, so
  `"token": "sk-..."` fails validation by name. Credentials are read from an env var (optionally
  falling back to a shell-style env file), or from a JSON file plus a dotted field.
- **Strict validation, loud failure.** Malformed JSON, a missing key, an unsupported mapping, an
  inlined secret, a credential that is not there, or an endpoint that refuses: each one appears in
  the panel in a non-ok state with a one-line reason naming the file. A broken definition never
  produces silence, which is the failure mode this layer exists to prevent.
- **`http`/`https` only.** The same check the built-ins go through refuses to open anything else.
- **Nothing invented.** A window the response does not carry is absent, not zero; the panel says
  "no reading". A ratio TMOS divides itself is the provider's own two numbers.
- **No local history.** A definition reports limits and balances only. Tokens by day and by model
  come from a CLI's own transcripts, and reading those is code.

### What a definition can and cannot express

| The provider… | As data? |
| --- | --- |
| exposes one endpoint returning one JSON document | yes |
| publishes a percent at a nested path (`a.b.c`) | yes |
| publishes a used/cap *ratio* instead of a percent | yes (`kind: ratio`) |
| keys its windows by its own names in one object | yes (`kind: map`) |
| returns an array of limits selected by one of its fields | yes (`kind: list`) |
| resets with ISO-8601, epoch seconds, epoch milliseconds, or "in N seconds" | yes |
| names a window only by its length in seconds | yes (`window_from_duration`) |
| publishes a prepaid balance (and optionally what funded it, and a currency) | yes |
| reads its credential from an env var or a JSON file field | yes |
| needs a **second** endpoint (to derive a window from two documents) | no — code |
| needs **pagination** (a charge ledger) | no — code |
| needs an **OAuth refresh** | no — code |
| needs a **plan-id → allowance lookup table** | no — code |
| needs a **fallback** to the CLI's own local transcripts | no — code |
| needs **unit reconciliation** the provider is vague about | no — code |
| reports **local token history** (tokens by day and by model) | no — code |

### What still needs code

Every one of the five built-in providers needs at least one row from that second half, which is
exactly why they stay code — and why "any provider" would be an over-claim:

- **Claude Code** — its healthy read is expressible, but the local transcript fallback that keeps a
  number on screen when the endpoint refuses is not, and neither is the plan name (it lives in the
  credential file, not the response).
- **Codex** — same shape: the window-from-length and duration-reset mapping is expressible, but the
  fallback to the limit reading the Codex CLI itself recorded in `~/.codex/sessions` is not.
- **ClinePass** — reads three endpoints, pages a charge ledger, and derives windows from a cap whose
  unit it cannot verify. Only the first endpoint is expressible.
- **Command Code** — reads two endpoints and maps a `planId` through a hardcoded plan/allowance
  table to build the monthly pool and the prepaid ledger.
- **OpenCode Go** — the one whose read is genuinely expressible as data end to end. Its shape is
  the `kind: map` example above.

So: a provider that publishes a simple usage or balance JSON at one endpoint can be added as data.
A provider that needs any of the second half of that table needs a reader in
`collector/usage_collector.py` — and that is a deliberate boundary, not a gap. X-RAY reporting,
private invoice history and explicit task-outcome capture are documented below.

## Usage

- **Bar**: one provider at a time (`Claude Code D100% W0% M—`), rotating every `rotateSecs`; the
  colour follows the least headroom.
- **Click** the bar icon to open the panel; **click a row** to expand it.
- **`j` / `k`** move through the rows, **Enter** opens the selected one, **`r`** refreshes,
  **Escape** closes, **Tab** moves to the neighbouring panel.
- **Right-click or middle-click** the bar icon to refresh immediately.
- **Keybinding**: `omarchy-shell shell summon tmos.usage '{}'` opens the panel and
  `omarchy-shell shell hide tmos.usage` closes it.

## Privacy

- Credentials are read the way the provider's own CLI stores them, read-only, and are never
  printed, logged, or written to the cache. Long token-shaped strings are scrubbed from any error
  text before it can reach the state file.
- A provider definition (`providers.d/*.json`) may only *name* a credential — an environment
  variable, or a JSON file plus a dotted field — never contain one. No key in that schema accepts a
  secret, `Authorization` cannot be set from a definition, and unknown keys are rejected rather
  than ignored.
- The panel displays; it never talks to the network. Only the collector does, and only to the
  provider's own endpoint: `api.anthropic.com`, `chatgpt.com/backend-api`, `api.cline.bot`,
  `api.commandcode.ai`, `opencode.ai`.
- No telemetry, no analytics, no third-party aggregator service. Nothing leaves your machine except
  the requests to the providers you already pay.
- Reads are read-only: `~/.claude/.credentials.json`, `~/.codex/auth.json`,
  `~/.cline/data/settings/providers.json`, `~/.commandcode/auth.json`,
  `~/.local/share/opencode/auth.json`, plus the transcript directories in the table above.

## External dependencies

- `python3` (standard library only — no pip packages) for the collector.
- `quickshell` / Omarchy Quattro, which provides the plugin runtime.
- Optional task capture uses Bash and GNU `readlink` (coreutils), normally included with Omarchy.
- Optional Pi observation requires an already installed Pi 0.87+; Setup installs only the observer.
- Node.js is used by the developer test suite; the core collector has no Node dependency.

## Remove

Before removing the plugin, open **Setup** and choose **Disable** for any optional
task command or Pi observer installed through Setup. This removes only symlinks
managed by this installation; unrelated files and manually copied extensions stay
unchanged. Reload Pi or start a new session after disabling its observer.

```sh
omarchy plugin remove tmos.usage
```

Keep `~/.local/state/tmos-ai-usage/` to retain invoices, imported history and task
evidence. Delete only that plugin-specific directory if you explicitly want to
erase its data, or the custom directory you selected with `TMOS_USAGE_STATE_DIR`.
Do not delete the shared `~/.local/state/` directory.

## Roadmap

- Providers added as data, not code: **shipped**. A validated `providers.d/*.json` definition format
  covers a provider that publishes a simple usage or balance JSON at one endpoint. The built-ins
  stay code on purpose — see [Adding a provider](#adding-a-provider) for where the boundary falls.
- Provider request-health reports, export imports, invoice history and observed outcome capture:
  **available with explicit local sources**. These sources remain distinct from subscription
  quota and no imported sample implies complete billing or task coverage.
- Token history for the providers that keep their sessions in SQLite today.

## License

MIT — see `LICENSE`.

## Inference X-RAY (0.3)

TMOS AI Usage includes a native, Omarchy-themed comparison dashboard. Open the
budget dropdown and choose **Open Inference X-RAY**. Overview, subscription tabs,
and the dropdown use one ranking. The default `#1*` comparison measures observed
local turns per current monthly fee; **Validated tasks / $** uses complete scoped
outcome and recognized-spend evidence. Neither successful HTTP requests nor agent
completion messages count as validated work.

The collector refreshes quotas, local usage, provider reports, an explicit export
inbox, outcome events, billing projections, and sourced deal observations. Invoice
payments, service-period cost allocation, API usage charges, promotional credits,
and subscription prices remain distinct. Missing data stays unknown.

See [ECONOMICS.md](ECONOMICS.md) for setup, supported sources, task capture,
invoice imports, comparison publication, and trust boundaries. This remains the
same `tmos.usage` plugin and public repository; this will be its first Omarchy
marketplace listing. Do not create a duplicate plugin identity or source repository.
The overview shows the shared ranking first, followed by per-subscription report,
outcome and billing coverage. Import diagnostics show counts only; source filenames, receipt
references and credentials stay out of the view.

See [PROVIDER-COVERAGE.md](PROVIDER-COVERAGE.md) for documented API versus
subscription-report boundaries and current history coverage limits.

Run `bash scripts/check.sh` before release. This covers offline/degraded collection,
accounting and evidence regressions, shared ordering, native QML runtime and the
Omarchy manifest. Version 0.3.1 adds native first-run setup. The initial marketplace listing is
[submitted for review](https://github.com/omacom/omarchy-plugin-marketplace/issues/8574).
Marketplace approval is separate from the tested source release. For later listing
updates, use the [plugin verification/update form](https://github.com/omacom/omarchy-plugin-marketplace/issues/new?template=verify-plugin.yml)
and provide the exact target commit. For a git-managed local installation, use
`omarchy plugin update tmos.usage`; `omarchy plugin validate <plugin-folder>` checks
the manifest. See the [publishing guide](https://plugins.omarchy.org/publish.html)
for the marketplace review process.

For captured runs, labelled checks, and explicit accept/fail decisions in the native dashboard, see [Task evidence review](TASK-REVIEW.md).
