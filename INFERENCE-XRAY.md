# Inference X-RAY — TMOS AI Usage evolution (PS-679)

Accepted direction, 2026-09-24. This expands TMOS AI Usage, with its existing
`tmos.usage` identity and `thetimmyman/tmos-ai-usage` repository. The native QML
dashboard and local evidence projections are implemented in this source tree;
this document records the design constraints and separates current behavior from
future scope. Keep this plugin identity and repository; the marketplace registry
does not yet list it, so publication requires an initial listing submission after
the implementation is merged. Do not create a duplicate plugin or source repository.
Implementation, verification, merge and publication remain separate states.

## Surfaces and ordering

Keep the compact budget dropdown. Add a larger native QML Inference X-RAY view.
The quick budget dropdown, overview rows and subscription tabs must consume the
same ordered provider list from `Model.parseDocument` / `Value.rankProviders`;
none may independently sort by quota, name or tokens after ranking. The dropdown
shows the same rank labels beside subscription names. The bar's tightest-budget
warning remains a separate alert, not the economic score.
Overview is the first/default tab and compares every configured subscription.
The selected metric controls the shared provider order: provisional observed local
turns per monthly fee is the default; **Validated tasks / $** is available only
when scoped, complete outcome and recognized-spend evidence qualifies. Tabs and
the overview use that same ranked list. Incomplete records stay unranked. Keep
the selected subscription by ID when refreshing, never by its previous index.

Rank account/subscription pools, not just provider brands: two genuinely separate
subscriptions can have separate rows, while one pool shared across clients must
not be duplicated. The initial preview uses the five configured provider rows;
account-level identity still needs collector integration.

The overview shows outcome, billing and report coverage before the ranking table.
Capacity has its own overlay. When both observed turns and invoice cash exist, a
separate chart uses independent scales and labels their observation windows; it
does not calculate cost per turn or influence rankings. Missing samples remain
gaps, never fabricated zeros. Historical value series and account-level pool
identity are future scope; provider rows currently follow the configured sources.

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
implemented and verified. For the first listing, merge to the public repository,
then use the [Omarchy submission form](https://github.com/omacom/omarchy-plugin-marketplace/issues/new?template=submit-plugin.yml)
and its [publishing guide](https://plugins.omarchy.org/publish.html). Later listing
updates use the [verification/update form](https://github.com/omacom/omarchy-plugin-marketplace/issues/new?template=verify-plugin.yml)
with an exact target commit. Source implementation, merge, deployment, theme
verification and marketplace submission are separate completion states; none are
implied by this design.
