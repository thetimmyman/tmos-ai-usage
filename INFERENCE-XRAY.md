# Inference X-RAY — TMOS AI Usage evolution (PS-679)

Accepted direction, 2026-09-24. This expands TMOS AI Usage, with its existing
`tmos.usage` identity and `thetimmyman/tmos-ai-usage` repository. Do not create a
second plugin, duplicate collector service, or second marketplace listing.
The native implementation is pending; the conversation preview demonstrates the
layout with observed data and explicit missing evidence.

## Surfaces and ordering

Keep the compact budget dropdown. Add a larger native QML Inference X-RAY view.
The quick budget dropdown, overview rows and subscription tabs must consume the
same ordered provider list from `Model.parseDocument` / `Value.rankProviders`;
none may independently sort by quota, name or tokens after ranking. The dropdown
shows the same rank labels beside subscription names. The bar's tightest-budget
warning remains a separate alert, not the economic score.
Overview is the first/default tab and compares every configured subscription.
Subscription tabs follow in descending **validated tasks per actual dollar**,
with prominent numeric ranks (#1, #2, #3...) mirrored in the overview table and
selected subscription heading. Exact ties share a rank (1, 1, 3) and use stable
subscription identity for secondary ordering. Keep the selected subscription by
ID when refreshing, never by its previous index.

Rank account/subscription pools, not just provider brands: two genuinely separate
subscriptions can have separate rows, while one pool shared across clients must
not be duplicated. The initial preview uses the five configured provider rows;
account-level identity still needs collector integration.

Overview contains a common-period value comparison and overlays all subscriptions
on shared axes. Default economic series: validated tasks per dollar over time once
history exists. Additional overlays: spend, validated output, retries/provider
errors and capacity. The current preview shows available capacity across quota
windows; it does not substitute this for economic ranking. Missing samples remain
gaps, never fabricated zeros. Series have stable theme-derived colors plus labels
and distinct markers/line styles. Unknown cost/outcome cohorts remain visible.

## Score and attribution

Score = uniquely attributed, validated completed tasks / recognized subscription
expense plus metered extras for the exact same selected UTC interval. Annual fees
are allocated over their service period, and partial periods use an explicit
documented allocation. Preserve cash payments separately from recognized expense.
Credits, catalog-equivalent usage and promotional allowances are not cash spend.
Include retry/rework consumption in the denominator; count a final validated task
once. Mixed-provider tasks need a declared attribution policy before ranking; never
award the same full completion to each assisting provider.

Compare the same period and task cohort/difficulty; show the selected cohort,
sample size, coverage, and uncertainty with each empirical result. This describes
observed value, not proof that provider choice caused the difference. Request
success, assistant turns and sessions cannot stand in for validated tasks.

Incomplete billing/outcome evidence => **Unranked**, after scored subscriptions.
Fully evidenced positive spend and zero completed tasks => score zero, ranked last
among scored subscriptions. Zero-dollar/free pools => **Free**, shown separately
without dividing by zero or displaying infinity. Unknown spend is not free. Tiny
samples retain visible N and low-confidence status. Stabilize updates so tabs do
not move while the user is interacting; apply a new order on refresh completion.

## Omarchy integration

Build in native QML using the same `qs.Commons` and `qs.Ui` surfaces as `Panel.qml`.
Bind background, foreground, accent, urgent, muted, borders and chart series to
Omarchy's published theme tokens; bind type, padding, spacing, radii and scaling to
the shell's style tokens. Use bar overrides where the bar surface supplies them.
Do not port the HTML preview's illustrative palette or font constants into QML.
Theme changes must update an already-open panel without requiring a shell restart.

Keep network and credentials in the existing collector process. QML consumes
sanitized, versioned local snapshots only. Outcome/routing integrations are optional:
the plugin must work independently on a clean Omarchy install, showing Unranked when
no task evidence source is configured. No required Odysseus/TMOS estate installation.

## Marketplace release work

Preserve manifest ID, settings and entry-point compatibility. Additive cache fields
must tolerate older snapshots; surface auth, missing data, stale sources and partial
exports explicitly. Keep provider configuration generic; no personal paths,
credentials, account identifiers or user exports in fixtures or marketplace images.

Before submission/update: run offline collector and importer tests, validate the
manifest against the installed shell, and validate native QML with resolved shell
imports. Then test hot theme changes in both light and dark themes, multiple font
scales, keyboard navigation, narrow screens, missing providers, no outcomes, zero
spend, tied scores, stale/partial exports, and reinstall/upgrade behavior. Refresh
the README, preview image, changelog and version only when the native feature is
implemented and verified. Submit an update through the existing marketplace route.
Source implementation, merge, deployment, theme verification and marketplace
submission are separate completion states; none are implied by this design.
