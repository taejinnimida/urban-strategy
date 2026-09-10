# R17 verification - longterm/public-complex judgment linkage

Baseline: R16 `app.html` + R15 backend `app.py` unchanged.

## Scope
- Long-term station-area rental housing
  - planned household count is no longer an initial site hard gate; shown as planning-stage requirement.
  - consent is no longer an initial site hard gate; shown as project-promotion-stage requirement.
  - aging fact is re-linked from the same Building HUB records if a stale fact-store would otherwise show `자료 없음`.
  - zoning/landscape/heritage exclusion facts reuse existing Site Analysis GIS; hill condition is separated as follow-up.
- Urban public-housing complex project
  - common exclusion is split into renewal area / urban-development area / urban-regeneration project-permit status.
  - renewal/development exclusions reuse existing Site Analysis facts.
  - urban-regeneration activation-area designation is NOT treated as equivalent to an actual project permit.
  - aging fact is re-linked from Building HUB records.
  - Seoul's three subtypes are evaluated independently: 주거상업고밀 / 주거산업융합 / 주택공급활성화.
  - popup has subtype tabs and per-subtype result.
  - resident consent is displayed as a later project feasibility consideration, not an automatic initial location hard gate.
- Site Analysis
  - `도시계획(개발)구역` card shows a separate `도시재생사업 인허가` status; remains REVIEW until an authoritative permit-boundary dataset is connected.

## Verification
- Inline JavaScript: `node --check` PASS.
- R17 targeted static regression: 15/15 PASS.
- R15 land-ledger backend targeted regression: 15/15 PASS; backend is unchanged.
- R16 activation route availability / REVIEW-vs-conditional / small-scale street-block key logic signatures preserved.

## Deployment
Only `app.html` changes in R17. Keep the current R15 `app.py` and all existing data files unchanged.
