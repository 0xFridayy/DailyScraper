# DailyScraper decision log

This log records durable DailyScraper architecture decisions. Details live in
[`DATA_PRODUCTS.md`](DATA_PRODUCTS.md); each entry states only the decision, its
consequence and where it is specified.

- **Numbering.** IDs are `DS-Dnn`. This series belongs to this repository and is
  separate from SPECTRA's own `D01`–`D35` log, which lives in the SPECTRA repo.
- **Changes.** Never edit a decision's meaning in place. A new entry supersedes
  an old one and names it.

## 2026-09-26 — data-product contract

All entries below are **accepted**. They are documentation decisions. None of
them changes runtime behaviour, databases, workflows, collection scope or
existing data by itself. Where the runtime does not yet match a decision, the
gap is migration debt, recorded in DATA_PRODUCTS.md.

| ID | Decision | Consequence | Specified in |
|---|---|---|---|
| DS-D01 | **DailyScraper publishes data, not conclusions.** | Interpretation (actor, intent, ownership, candidate merit) belongs to Market Intelligence or SPECTRA. Vendor labels may be kept as vendor facts only. | DATA_PRODUCTS §1.2, §2.6, §8 |
| DS-D02 | **`session_date` and `captured_at` are different concepts.** | Every product carries both where known. Neither is derived from the other, and filesystem modification time is evidence of neither. | §2.1, §3.5 |
| DS-D03 | **DailyScraper publishes evidence timestamps; consumers derive `eligible_at`.** | Each consumer applies its own declared policy. Legacy records get no invented availability timestamps. | §2.2, §7 |
| DS-D04 | **Absence is never silently interpreted as zero.** | `OBSERVED_ZERO` requires the source to return explicit zeros. A missing broker or session gets an explicit coverage state, never a zero, and legacy request sets that were not preserved are `UNKNOWN_LEGACY_COVERAGE`. | §2.4, §4 |
| DS-D05 | **Collection scope is DailyScraper-owned, neutral and versioned.** | Market Intelligence and SPECTRA may request additional scope. Interpretation categories ("retail", "smart money", owner groups) never define canonical truth. | §2.5 |
| DS-D06 | **`broker_flow` is designated legacy; `broker_inventory_daily` (based on `/api/inventory`) is the successor for new work.** | Design status: frozen for new development, and not a basis for new broker-intelligence work. Runtime state: its existing writers remain temporarily for compatibility, as migration debt. It is **not** runtime-frozen. Historical rows are not rewritten or migrated merely to conform to the new contract. The stalker-page product stays conceptually separate. | §3, §6, §9 |
| DS-D07 | **`broker_inventory_daily` unit contract.** | `blot`/`slot`/`nlot` are integer lots with `nlot = blot − slot`. `bval`/`sval`/`nval` are full Rupiah with `nval = bval − sval`. 1 lot = 100 shares, as a versioned reference. OHLC `volume` is shares; prices stay in source units. Derived values are marked `DERIVED`, with a derivation version. | §3.3–§3.7; evidence §10 |
| DS-D08 | **The target architecture has no cross-system Python imports.** | Consumers read published products and manifests. The monolith's current imports are not restructured by this decision. | §1.2 |
| DS-D09 | **Sentinel is read-only.** | Sentinel checks published products and run metadata, and produces health verdicts. It writes no product data and triggers no repair. | §1.2 |
| DS-D10 | **The `observable_inventory` rejection is narrowed.** | It applies only to V1 cumulative net lots as an ML feature, tested at a one-session horizon on legacy `broker_flow` (Experiment #1, `fwd_1`; #1E, `fwd_oo_1`). It does not reject inventory reconstruction, `broker_book`, or deterministic inventory math. Any new ML use still needs its own approved experiment. | `.clinerules` guardrails; [`ML_V2_EXPERIMENT_1_RESULTS.md`](../ML_V2_EXPERIMENT_1_RESULTS.md), [`ML_V2_EXPERIMENT_1E_RESULTS.md`](../ML_V2_EXPERIMENT_1E_RESULTS.md) |
| DS-D11 | **Historical observations are revisioned, not silently rewritten.** | Future canonical products preserve source revisions and DailyScraper repairs as explicit revisions or annotations. A current-best representation may be a derived view. Why: point-in-time replay and reproducibility require knowing what was observed, and when each correction became known. Current `INSERT OR REPLACE` behaviour is migration debt, not the target contract. | §2.3, §6, §7 |
