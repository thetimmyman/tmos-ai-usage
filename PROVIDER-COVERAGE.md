# Provider coverage and source boundaries

This matrix separates live quota readings, request/activity observations, and
billing reports. A quota meter describes remaining allowance; it does not show
which requests consumed it. Request rows and local transcripts do not establish
validated task outcomes. Only the explicit outcome ledger and its scoped
validation evidence can support the task-value comparison.

| Subscription | Live quota source | Request/activity source | What the report means and its limits |
| --- | --- | --- | --- |
| Claude Code on Claude Pro/Max | Claude Code's first-party OAuth usage reading; local transcript activity is a fallback | `~/.claude/projects` transcripts, local and incomplete | Transcript tokens/turns are local harness activity. Anthropic's Admin Usage/Cost reports require organization Admin API access and describe API usage; they do not report a consumer Claude Pro/Max subscription. |
| Codex on ChatGPT | The first-party ChatGPT usage reading used by the Codex CLI; CLI-recorded quota state is a labeled fallback | `~/.codex/sessions` rollouts, local and incomplete | Rollout token/turn records describe activity on this machine. OpenAI Platform Usage/Costs APIs and CSV exports report API-organization usage, not ChatGPT/Codex personal-plan usage. ChatGPT's account data export is a manual privacy export, not a live usage API. |
| ClinePass | The authenticated Cline account's usage-limit reading; some limit/detail routes are app-facing and their response contracts are not fully documented publicly | Cursor-paginated `/users/{id}/usages`, at most 10 pages × 100 rows per refresh, privately resumed and deduplicated across refreshes | Checkpoints retain allowlisted token counts and model labels only. Coverage remains partial even when the walk reaches the endpoint's end: retention and completeness are not certified. The field named `costUsd` is not presented as USD until its units are independently verified. |
| Command Code | The authenticated account's 5-hour/weekly limits and monthly credit pool | Current billing-period usage-summary endpoint: aggregate requests, success/failure counts, tokens and credits; not a per-request export | Public Studio documentation describes per-request detail in the UI but does not document a personal usage-history export API. Credits are not cash subscription spend. |
| OpenCode Go | OpenCode Go's first-party `/zen/go/v1/usage` meter | Local OpenCode session database/transcripts; optional imported request-log sample | The user's Go credential supports quota reads, not the documented Console export. The Console Usage API requires a separate service-account key and organization/member/service-account/model scope; organization exports may include other members, web search, BYOK and free rows. Workspace charges are not Go subscription expense. |

## API versus subscription reports

The similarly named administrative APIs do not fill personal subscription-history
gaps. [OpenAI's Usage API](https://platform.openai.com/docs/api-reference/usage/completions)
and [monthly usage export](https://help.openai.com/en/articles/20001072)
cover the OpenAI API platform and its organization/project billing. Eligible
Enterprise/Edu workspaces also have ChatGPT Desktop Work/Codex usage views and
Personal Analytics, but these are app/plugin surfaces rather than a public
personal-plan usage API. [ChatGPT account export](https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data)
is requested through Settings or the Privacy Portal and delivered as a data
export.

[Anthropic's Usage and Cost APIs](https://docs.anthropic.com/en/api/admin-api/usage-cost/get-messages-usage-report)
require organization Admin API credentials and report Console API activity.
They do not describe a Claude.ai Pro/Max plan's included subscription allowance
or its consumer-billing history. Claude Code's subscription OAuth meter is used
for quota windows; local Claude Code transcripts supply the request activity
available to this collector.

[OpenCode's Console Usage API](https://opencode.ai/v2/docs/console/usage/)
requires a Console service-account key. Its CSV export supports organization,
member, service-account and model scopes for fixed UTC ranges; it is distinct
from the personal OpenCode Go key and usage meter. The adapter is explicitly
opt-in and labels its output as workspace data.

[Command Code Studio](https://commandcode.ai/docs/studio) documents request-level
usage details in its UI. Its current API summary integration is aggregate and
billing-period scoped; no public per-request personal-history export contract
is documented. Cline's [API overview](https://docs.cline.bot/api/overview)
documents the inference API and enterprise administration surface, while the
usage-limit and history response contracts used by the collector are not fully
documented as a stable personal reporting API.

## Current completeness controls

- Cline history fetches a bounded page batch on each refresh, stores records in
  a private account-and-endpoint-scoped checkpoint, resumes its cursor, and
  restarts from the newest page after endpoint exhaustion to pick up updates.
  Pagination, retention, and the comparison period are never marked complete.
- Imported OpenCode request logs carry their own truncation and time-coverage
  evidence. A sample export is not a complete billing period.
- Local transcript activity is machine- and harness-specific. Missing sessions,
  remote workers, API calls through other clients, and unrecorded tasks remain
  unobserved.
- Request success, assistant completion, token totals and quota usage never
  substitute for semantic task validation or recognized subscription spend.
