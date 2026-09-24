# Changelog

## 0.3.0 source update · 2026-09-24 (not yet published)

- Fix subscription price editors carrying the previous provider’s amount or billing cycle across tab changes; bind saves to the editor’s provider and preserve unsaved edits during refresh.
- Add native Inference X-RAY overview and provider detail tabs with shared provisional and evidence-qualified rankings.
- Show report, observed outcome, and invoice coverage separately; keep unknown amounts unknown and invoice cash distinct from known service allocation.
- Distinguish user-entered, last-invoice, plan-quote and published-estimate subscription price bases.
- Import request exports and outcome events from private local state, with sanitized rejected/waiting counts.
- Add explicit task capture, verification, reviewer acceptance, invoice import, and comparison publication tools.
- Preserve the existing `tmos.usage` identity, settings, collector service, and Omarchy entry points.
