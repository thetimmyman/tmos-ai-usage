# Changelog

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
