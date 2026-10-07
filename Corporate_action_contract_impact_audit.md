# Corporate-action contract impact audit — PR #83

Consolidated on **2026-10-07, Asia/Jakarta**, resuming the interrupted audit. Repository: `0xFridayy/DailyScraper`. Inspected head: **`5a2ec4181ad897e3d6455c5b7855e0d9830b1768`** throughout. This report assesses that fixed head; it does not assert that GitHub or production still has the same state today.

**Final design verdict: READY TO IMPLEMENT CORPORATE-ACTION CONTRACT.** Use raw actual OHLC, a separate explicit reference registry, and **Option A: withhold every economic return or target whose holding interval crosses a corporate-action boundary**. Price acceptance and return comparability must be independent decisions. PR #83 should remain unmerged until the writer, audited price layer, active consumers and regression tests deploy this contract together. This is design readiness, not a claim that the current PR passes those conditions.

**Historical ENRG price mutation required: NO.** Preserve October 2 close **1440** and October 5 **O1080/H1085/L1000/C1030/V109977800**. Record **1065** separately as the October 5 exchange reference. The reference-relative change, **−3.286385%**, is a limit-validation diagnostic, not a shareholder return.

## Evidence, reconciliation and method

This is a local read-only architecture/dependency audit. No repository implementation, database mutation, historical repair, PR merge, collector execution, external message or BandarmoloNY change occurred. No repository modules, tests, model fitting or backtests were executed. New work consists of static source inspection, read-only queries of the existing exact-head DB export, arithmetic, and this report. No new public-source or NeoBDM request was made.

The parent report and all saved subagent notes were reconciled first:

| Saved work | Treatment in this consolidated report |
| --- | --- |
| `work/pr_contract.md` | Retain the exact guard analysis, including unchanged-close grandfathering, whole-ticker rejection and the broker-write cutoff. |
| `work/raja_followup.md` | Retain the measured factor-five splice classification. Do not turn the inaccurate August-split code comment into event evidence. |
| `work/archived_evidence.md` | Its original 13-capture/no-Oct-6 finding describes the checked-out evidence available then. The later exact-head export supersedes that availability finding; its warning about UNKNOWN source sessions remains valid. |
| `work/corporate_action_sources.md` | Retain issuer/KSEI confirmation and the distinction between the IDX-authored transcription and an unavailable direct IDX original. Its earlier statement that 1030 was only user-supplied is superseded by the committed DB and raw screener export. |
| [ENRG root-cause report](<C:/Users/jason/Documents/Codex/2026-10-06/files-pasted-by-the-user-task-2/outputs/ENRG_root_cause_report.md>) | Controlling consolidated factual baseline; no re-investigation of the corporate-action facts. |
| [Raw screener observations](<C:/Users/jason/Documents/Codex/2026-10-06/files-pasted-by-the-user-task-2/outputs/enrg_raw_screener_observations.csv>) and [committed evidence](<C:/Users/jason/Documents/Codex/2026-10-06/files-pasted-by-the-user-task-2/outputs/pr83_committed_evidence.json>) | 14 preserved responses. October 6 H/L/C **1085/1000/1030** corroborates the dated DB bar, but the source-session association remains inference. |
| `work/pr83_committed_neobdm.db`, `work/pr83_enrg_committed_evidence.json`, `work/pr83_oct6_enrg_screener_page4.json` | Existing byte-for-byte exports, reused rather than reacquired. SQLite access used URI `mode=ro` and `PRAGMA query_only=ON`. |
| `work/ca_contract_head`, `work/ca_contract_source_manifest.json` | Existing exact-head snapshot of 142 Python/workflow/selected contract files. Sources below refer to that commit, not a moving checkout. A read-only Git tree filename check found no additional tracked JS/TS/SQL/R/notebook/shell source outside the excluded BandarmoloNY scope. |

The interrupted architecture subagents supplied useful interim findings about open-to-open exit boundaries, direct screener consumers and independent broker/experimental calculations. They did not leave completed `ca_contract_*.md` reports before interruption. Their claims were checked against the existing source snapshot here; no redundant subagents were spawned on resumption.

The committed DB export was rehashed and still matches **`6fc475e6db6be597a539a8cc30f6a0c44f05a5c14b263367c07a3aa417389be5`**, Git blob `532a3cfc58779101a63327e7da4ebdeeafd99f7c`. Its October 5 acquisition time remains unknown. The raw October 6 response hash is **`9f4d5d1c2f693ae08c1b3e2bfd992e44e5735b3023e2c7b5767386e14c118de4`**. All 14 CSV source-session fields remain UNKNOWN. Final verification also rehashed **137 non-BandarmoloNY snapshot files**, with **zero differences** from the existing exact-head source manifest, and checked that every source line link resolves within its snapshot file. The earlier 47-artifact read-only verification remains preserved in the prior outputs; it was not misrepresented as a fresh production-state check.

### Quantified current-head behavior

These are consequences of inspected code and stored inputs, not observations of a production run:

| Case | Behavior at this head |
| --- | --- |
| Identical refresh of stored ENRG 1440→1030 | Both endpoints are unchanged; `validate_inventory_prices` skips this transition. It does **not** automatically freeze ENRG. Later ordinary bars may advance. |
| October 5 bar first inserted after stored October 2 C1440 | New endpoint triggers the ordinary comparison. **−28.472222% < −15.5%**, so the entire ticker response is rejected before the price/broker inserts. |
| Revised endpoint or newly inserted predecessor | Changed-neighbor logic rechecks the transition; the same false-positive veto can occur. Changing only other OHLCV fields with identical closes does not activate this particular close-transition check. |
| Audit of the committed October 5 row | `limit_violation=True`; `cross_ticker_dup=False`; `series_break=False`. Its centered 21-row median is 1325 and close/median is 0.77735849. The suspect label therefore comes from the inappropriate limit reference. |
| `load_clean` on this exact DB | `price_quarantine` exists, but contains **no ENRG row**. The function trusts its stored keys instead of rerunning detection, so it retains October 5 despite the detector's false-positive label. |
| Common forward/lag labels | Separate raw normal-step/open guards still withhold the crossing labels. A detector-only fix does not yet clear those guards; a careless guard-wide fix would remove this incidental protection. |
| Screener signal paths on this DB | Latest canonical ENRG `market_summary_daily` row is capture **October 5, C1440, last_date=NULL**. October 6 C1030 exists in preserved raw evidence but was not persisted there. Do not claim that this snapshot already produced a 1440→1030 screener outcome. |
| Failure monitoring | Exact DB has 45 price tickers. An illustrative single rejected ticker out of 45 is **2.2222%**, below the **>30%** run-failure gate. The actual requested ticker count is runtime-dependent. Table-wide `MAX(date)` can stay fresh while that ticker stalls. |

Sources: [writer changed-neighbor guard](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/backfill_inventory.py#L192), [run failure handling](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/backfill_inventory.py#L391), [detector](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/price_audit.py#L92), [quarantine-key reader](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/price_audit.py#L342), [table freshness](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/check_signal_integrity.py#L146).

## A. Canonical price-series contract

**Confirm the proposed contract, with three qualifications: it is a future invariant, trust requires a session and representation contract, and an event's limit reference does not establish economic comparability.**

1. Canonical `price_history` represents actual exchange-session open/high/low/close in IDR per as-traded share, and traded volume in declared units. Preserve actual prices, including legitimate entitlement resets.
2. Never silently back-adjust this canonical series. A future adjusted analytical series must have its own representation name, factors, rounding convention, source version and contract version.
3. Corporate-action facts and official limit references live separately from OHLC. Neither an ex-date reference nor TERP is an actual prior/ex-date closing price.
4. A large gap is an unresolved transition diagnostic. It cannot establish an event, event date, split ratio, entitlement, or correction factor.
5. Only an explicitly verified, correctly scoped official reference can replace the ordinary reference for that exact ticker/session/market. A pending event cannot fall through to an ordinary-session approval.
6. Retain the raw previous close and its session/source as provenance. On the confirmed event session, compare the actual price with the official reference; subsequent ordinary sessions resume comparison to the previous trusted actual close, including the actual event-day close.
7. Missing identity, ambiguous session, incompatible/unknown price basis, pending reference, conflicting event records, untrusted predecessor or unsupported reference scope produce an explicit UNRESOLVED result. Do not coerce them into a passed Boolean or diagnose corruption solely from a gap.

**The current database is not globally certified raw.** RAJA demonstrates mixed raw/adjusted historical bases. A new declaration must not relabel legacy rows, parquet or vendor caches as raw. Preserve unknown/mixed regimes as withheld analytical inputs until their separate evidence audit resolves them. “Raw source response” describes capture lineage; it does not prove that a vendor's historical prices are unadjusted.

Ordinary validation needs a trusted actual predecessor on the immediately preceding verified exchange session. Adjacent available rows, weekday arithmetic and a ≤5-calendar-day gap are insufficient. An official event reference can independently resolve the event-day limit comparison if the predecessor is missing; cross-boundary returns and predecessor provenance remain unavailable. An event reference cannot rehabilitate a corrupt predecessor.

Registry absence is not proof that every historical interval is free of corporate actions. Record the reviewed universe/date coverage of the registry and the input representation. For a fully certified economic-return result, UNKNOWN action coverage or representation remains withheld. Ordinary-band diagnostics may be shown under their narrower rule scope; they must not silently certify a complete action-free history. This is especially relevant to frozen and legacy research.

## B. Complete consumer dependency map

The map covers all statically discovered price/return/limit consumers and their propagation paths at the fixed head, excluding BandarmoloNY. It includes scheduled paths and runnable research/legacy paths; an imported helper is not evidence that a scheduled job successfully ran. “Can span” means the calculation or holding window can reach across a boundary, not that the located input already contains ENRG October 5.

Notation: **PH** = `price_history`; **CP** = `price_audit.clean_panel`; **FR/LR** = common forward/lag calculations; **MS** = `market_summary_daily`, whose date is capture date unless separately verified; **IP** = inventory OHLC/broker parquet; **TX** = `txchart_history.db.ohlcv`. CP returns raw prices after filtering, not adjusted economic prices. **Policy A** means phase-aware interval withholding described in F–G.

### Core, writers and scheduled signal/report paths

| Consumer / source location | Input representation | Uses raw OHLC? | Computes returns? | Can span CA? | Current ENRG behavior | Required future behavior |
| --- | --- | --- | --- | --- | --- | --- |
| [price_audit: load/detect](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/price_audit.py#L87) | PH; legacy basis may be unknown | Direct values | Raw diagnostic change | Yes, previous close and centered median | False limit flag; no duplicate/series flag in the exact DB | Official reference for limits; separate raw-step diagnostic; median diagnostics within compatible segments; no blanket action exemption |
| [price_audit: load_clean/clean_panel](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/price_audit.py#L342) | PH plus quarantine keys | Yes | Delegates FR/LR | Yes | Retains event bar because existing quarantine has no ENRG key; guards withhold crossings | Retain admissible bar plus derived event/reference metadata; version-aware quarantine overlay and Policy A |
| [price_audit: FR/LR/open anchors](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/price_audit.py#L368) | Sorted filtered raw frame plus unfiltered date axis | Yes | CC, OC, OO, gap, lags, extrema | Yes, 1d and all h/k | Normal-step/open checks incidentally mask resets; changing them can leak OO at exit | Separate price admissibility from comparability; check actual start/end phases, intermediate events and exit-session boundary |
| [backfill_inventory: validation/insertion](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/backfill_inventory.py#L192) | Dated inventory JSON plus stored PH neighbors | Stores source OHLC directly; raw basis not declared by source field | Limit diagnostic; lot-derived netval | Yes | Existing pair grandfathered; new/revised pair refused | Shared reference resolver before writes; retain REAL/identity/schema checks and exact rejection evidence; never use reference as stored close |
| [check_signal_integrity](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/check_signal_integrity.py#L163) | PH/quarantine; MS cross-source checks | Yes | Diagnostic comparison, not target builder | Yes | Detector can call event contamination; table freshness misses individual stalls | Report confirmed boundary separately, retain real defects, check return withholding and per-ticker freshness; do not infer MS sessions from offsets |
| [neobdm_scraper](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/neobdm_scraper.py#L598) | Undated screener JSON→MS; direct broker execution values; inventory bagholders | H/L/C directly, O/V absent in relevant MS source | No ordinary target here; bagholder implied-price calculations | Inventory aggregation can span | Preserves capture values; raw Oct6 not persisted in MS | Preserve values/flow units; publish verified session/representation status to adapters; withhold action-spanning implied-cost claims |
| [daily_picks / morning](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/daily_picks.py#L121) | MS snapshots with copy detection and previous-weekday mapping | H/L/C source values | Day moves, holding returns, excess, follow progress, learned weights | Yes | No C1030 canonical snapshot yet; a future pair triggers gap heuristics and may be skipped; no explicit event contract | Verified session mapping first; registry-based boundaries and Policy A in every outcome/progress path; gap heuristic remains unresolved-data diagnostic, not event inference |
| [evaluate_signals](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/evaluate_signals.py#L118) | MS capture-date panel | H/L/C | Exit/entry, MFE, MAE | Yes | No present pair in MS; future crossing has no action guard | Verify sessions, refuse action-spanning outcomes/extrema and report withholding reason |
| [walk_forward_backtest](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/walk_forward_backtest.py#L235) | CP plus pinned canonical broker-flow snapshot | Yes | `lag_1`, OO target, CC diagnostic, volume ratio | Yes | Main path uses masked labels; raw momentum/CC fallbacks remain callable | Require contract metadata/guarded lags; remove raw diagnostic fallback; carry registry/policy identity with snapshot |
| [strategy_variants](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/strategy_variants.py#L127) | CP plus walk-forward predictions | Yes | Intraday/holding exit÷entry, TP/SL | Yes, variable hold | `gap_1`/`fwd_1` currently stop event trades | Separate entry admissibility from gap economic return; refuse actual held paths crossing CA before applying TP/SL or timed exits |
| [ara_arb_simulation](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/ara_arb_simulation.py#L52) | CP or caller-supplied close frame | Yes | Annotations and rolled CC returns | Yes, delayed exits | CP lag at event is NaN, so limit flags are false/uninformative; raw frame can falsely mark ARB | Shared official-reference annotations independent of lag returns; Policy A through actual rolled exit; keep near-limit tolerance distinct |
| [run_ml_reports](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/run_ml_reports.py#L99) | Walk-forward, strategy, DDQN, CP watch tracking | Yes, indirectly/directly | Targets, simulation summaries, watch CC/extrema | Yes | Current common guards suppress crossings; watch loop breaks at invalid fwd1 | Propagate price/registry/policy identity consistently to every report; mark watch outcome unavailable across CA rather than resolved at a shortened horizon |
| [ddqn_episode_data / ddqn_entry_exit](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/ddqn_episode_data.py#L77) | Canonical CP/flow; lag1 as daily reward | Yes | Daily returns and cumulative rewards/trades | Yes, persistent positions | NaN lag drops event day; episode gap logic offers current incidental protection | Explicit event segment/episode boundary; no carried position/reward over reset and no deleting a day then bridging rewards |
| [check_ml_health](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/check_ml_health.py#L245) | Current panel/model results; target-feasibility bounds | Indirect | Validates targets | Indirectly | Common targets unavailable; no CA contract check | Assert boundary masking, independent limit resolution, provenance and absence of raw fallback; report coverage/withheld counts |
| [broker_collect / build_inventory_db](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/build_inventory_db.py#L186) | Inventory cache JSON→OHLC/broker parquet | Source values; can be back-adjusted | No economic return here | Supplies history | Located ENRG historical parquet ends Aug21; broker cache ends Sep25 | Preserve captures and representation identity; do not claim a domain-valid payload is comparable across events |
| [inventory_features](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/inventory_features.py#L78) | IP plus reported lot/value broker data | Yes, vendor basis | Raw r1/rk/gap, volatility, price/cost, next-day labels | Yes | Located frozen input has no Oct event; refreshing can admit it without CP | Shared limit labels; Policy A for returns/features; compatible volume/lot windows; persist derived boundary/provenance into new panel |
| [scan_ara_arb / arb_veto](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/arb_veto.py#L82) | Refreshed IP panel and ara_multiday labels | Indirect | Classification/holding-label inputs | Yes, multi-day | Old located parquet cannot show Oct5; scheduled refresh is an independent bypass | Refuse panel lacking new registry/policy metadata; no false ARB training/veto from raw reset; withhold crossing holding targets |
| [broker_book](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/broker_book.py#L407) | Inventory actual-lot/value series, OHLC, measured basis regimes | Yes | Raw volatility, drift, average-cost/P&L, close/cost | Yes, 20/60/80-session and book anchor | Existing basis flags do not establish ENRG entitlement accounting; new Oct payload could calculate raw reset loss | Separate CA boundary from measured-basis flags; withhold/reset analytical book and mixed windows under an explicit new segment; preserve transaction flows; do not invent entitlement accounting |
| [broker_rules](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/broker_rules.py#L207) | Broker-book rolling state | Yes | Drift59, close/cost/gain proxies and eligibility | Yes | No explicit ENRG event input; rules could interpret reset as drift/loss | Consume comparability/window status and withhold affected rules; no CA factor inference from cost containment |
| [broker_learning](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/broker_learning.py#L208) | Inventory OHLC→FR plus custom holder path, rules/book | Yes | OO5/10/20/60, delayed hold60, excess/profitability | Yes, exit may move after suspension | Normal guards reject reset; custom holder and entry-lock math duplicate reference assumptions | Shared reference/entry annotation; separate masks through actual delayed exit including exit event; withhold action-spanning profitability/cost claims |
| [broker_learning_run / broker_learning_db / broker_dashboard](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/broker_learning_run.py#L700) | Broker bundles, rules and saved derived outcomes | Yes/indirect | Records/summarizes returns, weights, P&L; charts raw close | Yes through upstream | Current outcome guards do not cover every book/rule field | Carry contract identity and explicit unavailable reasons; do not reuse old persisted outcomes under new semantics; annotate raw chart boundaries |

### Research, experimental and legacy paths

| Consumer / source location | Input representation | Uses raw OHLC? | Computes returns? | Can span CA? | Current ENRG behavior | Required future behavior |
| --- | --- | --- | --- | --- | --- | --- |
| [horizon_scan: build](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/horizon_scan.py#L60) | CP plus legacy broker_flow | Yes | Multi-h fwd, lag1/5/10, extrema | Yes | Common guards mask event; volume/ATR uses lag availability | Inherit Policy A; retain metadata and window masks, including rolling feature requirements |
| [regime_gated_momentum](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/regime_gated_momentum.py#L58) | CP/predictions plus aggregate foreign-flow regime | Yes | Rolled close holding return | Yes | fwd1 path stops crossing | Shared annotations and actual-exit span check; foreign cash-flow regime itself is not adjusted by reference price |
| [ml_v2_experiment_1](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/ml_v2_experiment_1.py#L143) | CP, broker averages/netval, reconstructed net lots | Yes | OO target, lags, price/inventory features | Yes | CP guards mask reset; uses inherited price helper | Policy A plus basis-compatible lot recovery/windows; new experiment contract/provenance rather than rewriting accepted digests |
| [ml_v2_experiment_1_robustness](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/ml_v2_experiment_1_robustness.py#L18) | Experiment1 panel/predictions with frozen accepted digest | Indirect | Paired scores/excess | Upstream | Located original experiment predates event; current rebuild uses shared guards | Preserve old accepted digest and archive; new CA-aware experiment identity and evidence, not in-place re-pin |
| [multiday_features](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/multiday_features.py#L58) | CP plus rolling broker-flow features | Yes | fwd1, lag1/5/10; rolling features | Yes | Common guards/lag availability currently mask | Preserve explicit price-window comparability; monetary-flow sums retain their own units |
| [feature_ablation](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/feature_ablation.py#L47) | CP multi-h plus broker features | Yes | Multi-h targets and volume ratio | Yes | Common fwd/lag guards mask | Inherit Policy A and report eligible denominator/contract identity |
| [smart_money_divergence](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/smart_money_divergence.py#L62) | CP plus broker-group features | Yes | fwd1, lag1/5, volume ratio | Yes | Common guards mask | Inherit Policy A; preserve boundary metadata after merges |
| [shap_analysis](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/shap_analysis.py#L25) | Walk-forward panel/models | Indirect | Uses labels, not new raw ratio | Upstream | Inherits panel exclusions | Explain only eligible CA-aware model inputs; record model/data/registry contract |
| [ara_arb_scan](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/ara_arb_scan.py#L90) | Direct PH; bespoke iterative deletion/limits | Yes | r1/rk/gap, rolling extrema, forward ARA/ARB | Yes | Treats raw pair as impossible and removes an endpoint according to centered-median distance; independent of CP fix | Replace independent cleaner/reference formulas with audited admission/annotations; preserve event bar; Policy A for price features |
| [ara_multiday](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/ara_multiday.py#L79) | IP panel | Yes | h-window limit labels, gap, hold and first-ARA exit | Yes | Frozen input lacks Oct5; refreshed data risks false ARB and raw hold reset | Session-specific shared limit labels; Policy A for hold/gap returns and full-window feature eligibility |
| [label_compare](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/label_compare.py#L69) | IP-derived panel | Yes | Entry open, horizon close/extrema, barrier returns | Yes | No Oct5 in located frozen data; no explicit event guard in bespoke labels | Guard all actual price/barrier horizons; distinguish event-aware limit occurrence labels from economic outcomes |
| [experiment_1f_universe_gate](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/experiment_1f_universe_gate.py#L2398) | Frozen/candidate full-market OHLC, separate basis and broker contracts | Source/derived basis explicitly reviewed | FR/LR and anchor diagnostics | Yes | Frozen ENRG OHLC ends Aug21; no Oct evidence | New contract version using explicit registry and interval masks; preserve basis quarantine; do not weaken/falsify old Gate-A authorization |
| [experiment_1f_features](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/experiment_1f_features.py#L205) | Gate-A panel/flow/session-axis parquet | Yes/indirect | Lags, volume/ADV features, inherited OO ranks | Yes | Inherits frozen/common guard behavior | New metadata and segment/window eligibility; require masked labels before rank/placebo/sample construction |
| [experiment_1f_evaluation](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/experiment_1f_evaluation.py#L140) | Gate-A raw anchors/labels plus bespoke exit search | Yes | Raw delayed OO returns; rankings/portfolio | Yes, actual exit differs from nominal | Its raw close-step and single-price direction guard happen to stop some resets; first usable event exit can bypass them after anchor fix | Shared reference semantics and independent actual entry→exit CA mask; CA unresolved outcome must not become an arbitrary cash-zero return |
| [experiment_1f_gate_b / gate_b_contract](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/experiment_1f_gate_b.py#L326) | Frozen prepared panel, feature/evaluation outputs | Indirect | IC, excess, hold-through/cash sensitivities | Upstream and variable exits | No October event in frozen inputs; old identity binds old contract | Separate new version/authorization with registry/policy in identity; preserve original results |
| [experiment_1f_candidate / manifest / validation / validity / normalization](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/experiment_1f_manifest.py#L383) | Preserved raw caches, candidate normalized artifacts, frozen digests | Source/derived; not universally raw | Mostly validation/provenance, not new economic targets | Indirect | Preserves older inputs; measured ratios do not prove action dates | Pin registry, representation, policy and code identity for new artifacts; never repurpose measured-basis factors as event records or silently update frozen manifests |
| [experiment_2a0_event_study](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/experiment_2a0_event_study.py#L267) | Historical close parquet plus IHSG CSV | Vendor/derived close | Horizon endpoint ratio, minus market return | Yes | Located historical input lacks Oct5; arithmetic has no CA mask | Policy A before market excess and placebo/baseline construction; IHSG subtraction does not repair a corporate-action reset |
| [inventory_evidence](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/inventory_evidence.py#L459) | Typed verified market observations and declared basis/session scope | Yes, when supplied | Endpoint market return; ADV/flow normalization | Yes | Existing capture/basis checks do not themselves supply CA metadata | Add action/interval contract and explicit withheld reason to market return; lot/volume comparability separate from unchanged cash-flow sums |
| [targeted_actor_panel / targeted_actor_observations](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/targeted_actor_observations.py#L1009) | Own immutable inventory snapshots, OHLC and reported lot/value windows | Carries OHLC; not PH | Implied side price and delegated inventory evidence, not shareholder return | Yes, quantity/price windows | Not an ENRG October source-date proof; membership sums are different from price return | Preserve selector membership/actual cash values; mark aggregate implied price unsupported if share-unit basis changes; route supplied market returns through new contract |
| [txchart_history_pull / txchart_backtest](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/txchart_backtest.py#L69) | Separate TX plotted OHLC and category flow | Source values; adjustment basis unproved | Raw pct_change1/5/10, horizon labels, daily path | Yes | October ENRG coverage not established; independent of PR83/CP | Declare source representation/session provenance, use action-aware adapter or refuse uncertified equity-return runs |
| [pattern_detector](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/pattern_detector.py#L31) | TX raw OHLC | Yes | Raw r1; price/volume/range/fractal windows | Yes | Separate input, no CA handling; no verified Oct result | Segment/reset price and volume pattern windows or withhold action-spanning signals |
| [pattern_backtest / pattern_type_backtest](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/pattern_type_backtest.py#L81) | TX close plus patterns and shared legacy limit annotations | Yes | Holding/rolled exits and always-long baseline | Yes | No action span check; not fixed by PH writer | Shared reference adapter and Policy A through actual exit; identical eligibility for signal and baseline |
| [foreign_flow_signal_backtest](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/foreign_flow_signal_backtest.py#L100) | TX close/category flow | Yes | Held close ratio after ARB-delayed exit | Yes | No independent CA span guard; coverage not established | Guard actual held interval; preserve monetary foreign-flow regime sums |
| [analyze_ticker_patterns](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/analyze_ticker_patterns.py#L101) | TX OHLC plus pattern CSV | Yes | Best/worst future excursion÷entry | Yes | No explicit event mask; not evidence of Oct5 output | Withhold crossing excursion windows and distinguish descriptive raw prices from economic outcomes |
| [regime_validation / pull_jci](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/regime_validation.py#L45) | IHSG/index prices, not ENRG equity OHLC | Index source series | Index pct_change/strategy return | Not ENRG event scope | ENRG record must not affect COMPOSITE/IHSG | Keep index contract separate; an equity-event key cannot match index history |
| [transaction_cost_model / signal_metrics / kelly_sizing](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/signal_metrics.py) | Already constructed return sequences | No | Costs, compounding/statistics/sizing | Upstream contamination can propagate | Cannot recognize ENRG from anonymous numbers | Caller must provide eligible returns/contract identity; no reference math or event inference inside generic statistics |

Additional propagation/support paths: `broker_flow_canonical.py`, `broker_flow_regime.py` and `broker_flow_manifest_refresh.py` certify flow provenance, not economic price comparability; `macro_analysis.py` aggregates monetary broker flow without creating an equity-price return; `neobdm_source_contract.py`, `inventory_capture.py`, `inventory_evidence.py` and source-capture health checks supply identity/capture/session evidence. `repair_scrape_date.py` intentionally does not relabel PH's session dates. `price_audit` repair/reconcile commands are mutators, not an authorized solution to this event. Their diagnostics must stop implying every suspect's raw price/netval is wrong. Ownership-only, message-delivery and schema-only modules do not introduce additional equity-return formulas. Relevant existing test files are listed in O. BandarmoloNY modules remain outside this audit/change scope.

### Deployment routes found in the workflows

| Workflow / route | Why the contract must reach it |
| --- | --- |
| `price-history-topup.yml` → backfill → audit cross-duplicate gate | Current post-run gate deliberately checks duplicates, not all limit suspects. The new writer guard is the active new/revised-transition veto. |
| `signal-integrity.yml` → check_signal_integrity | Otherwise it can continue calling a legitimate event new contamination after the writer accepts it. |
| `daily-scrape.yml` → morning → scraper/daily_picks; `signal-eval.yml` → evaluate_signals | MS-based outcomes bypass PH/CP entirely. |
| `ml-daily-report.yml` → run_ml_reports; `ml-health.yml` → health/canonical tests | Walk-forward, strategy, DDQN and watch outcomes need the same registry/policy identity. |
| `arb-veto.yml` → harvest → build_inventory_db → inventory_features → arb_veto | A separately refreshed parquet/model route can mislabel a reset or put it in veto training. |
| `broker-learning-daily.yml`, `broker-learning-weekly.yml` → broker_learning_run | Books/rules/learned outcomes need comparable windows and explicit withheld statuses, not only FR changes. |

No scheduled active route found is fixed merely by changing `price_audit.detect`. Legacy/manual research remains an explicit supported-or-refused contract decision, not an assumed safe consumer.

## C. Corporate-action reference contract

### Ownership and interface

Recommend one lightweight, pure **`price_contract.py`** module plus one versioned **`corporate_actions.json`** registry. This is a proposed architecture; neither file was implemented. Keep parsing/domain/reference/band/span logic free of SQLite, pandas, Playwright, credentials and runtime network requests. `price_audit` supplies the dataframe adapter; writers and other source adapters call the same domain contract.

Caller-shaped interfaces:

```text
resolve_limit_reference(ticker, session, market, previous_actual, registry, session_axis)
    -> RESOLVED(reference_price, reference_kind, reference_source,
                previous_actual_provenance, event_id, registry_version)
     | UNRESOLVED(reason, evidence/provenance)

validate_actual_price(actual_price, resolved_reference, rules_version)
    -> IN_BAND | OUT_OF_BAND | UNRESOLVED

return_span_status(ticker, start_anchor, end_anchor, market, registry,
                   session_axis, input_representation)
    -> COMPARABLE | WITHHELD(reason, event_ids)
```

An anchor includes a verified session and phase `OPEN` or `CLOSE`. These tagged outcomes are distinct types/concepts: **price admissibility is not return comparability**. Missing data cannot accidentally become `False` for violation and therefore approval. Consumers must explicitly handle UNRESOLVED.

| Session/reference state | LIMIT_REFERENCE_PRICE | REFERENCE_KIND | REFERENCE_SOURCE | Result |
| --- | --- | --- | --- | --- |
| Ordinary session with trusted actual immediate predecessor and compatible scope | Previous actual close | `PREVIOUS_TRUSTED_ACTUAL_CLOSE` | Prior bar's session, source/snapshot identity and trust basis | Apply centralized rules |
| Confirmed reference for exact market/ticker/effective session | Explicit official reference | `OFFICIAL_CORPORATE_ACTION_REFERENCE` plus event type | Event/document/evidence identity and registry version | Apply the same centralized rules to that reference |
| Confirmed boundary but missing official reference | None | `UNRESOLVED_REFERENCE` | Pending event evidence | Withhold validation; no ordinary fallback |
| Conflicting record, wrong scope, untrusted/ambiguous baseline or unsupported session | None | `UNRESOLVED_REFERENCE` | Reason and available provenance | Withhold validation; preserve rejection evidence |

No fuzzy ticker, nearest date, date range override, inferred split factor or fallback TERP formula. The event reference has one-session scope. For ENRG October 6, the ordinary previous actual close would be **1030**, if that session/bar context is verified; **1065 must not carry forward**.

### One implementation of limit rules

Move/re-export the existing rules into this module and migrate all callers, including independently copied scan/pick formulas. At this audited head, preserve the existing contract unless a separate rule change is reviewed:

- `ARA_BOUND(reference)` = +35% below Rp200, +25% at Rp200 through Rp5000, +20% above Rp5000.
- `ARB_BOUND` = −15%.
- Audit/writer `TOL` = 0.005 absolute return units, i.e. **0.5 percentage points**, inclusive endpoints.
- Choose the ARA tier using **the resolved limit reference**, not the raw cum close on a reset session.
- Keep near-limit/entry-lock execution proxies as separately named parameters. The simulation's current 1-percentage-point “near limit” slack is not the audit's 0.5-point admission tolerance, nor proof that an order can fill.

Existing tick/limit-price snapping in `ara_arb_scan` and `inventory_features` also needs one explicitly versioned common owner if those price-level annotations remain supported. Do not invent a universal rounding algorithm from ENRG's case. Its **1065 is an explicit source value**, not a helper-derived rounded TERP. Current model tick conventions must be named as model conventions unless independently established as the applicable exchange rule.

`daily_picks.limit_up` currently assigns exactly Rp200 to a different tier (`<=200`) than `price_audit` (`<200`). Centralization must remove that inconsistency, with a regression test rather than a silent convention change.

### Alternatives considered

| Design | Benefit | Problem / decision |
| --- | --- | --- |
| Keep everything inside `price_audit.py` | Smallest immediate import diff | Mixes pure rule ownership with pandas/DB audit/repair operations and leaves source adapters coupled to a large module. Useful compatibility re-exports only. |
| New pure module plus committed registry — recommended | Offline, deterministic, testable and shared by writer/audit/other adapters | Requires explicit caller migration and version propagation, addressed in L–O. |
| Corporate-action DB tables/runtime lookup service | Could support a larger administrative workflow later | Unnecessary initial schema/service/network dependency; would create mutable lookup state and snapshot consistency work. Not recommended for version one. |

Session-axis ownership stays with the existing offline calendar contract rather than duplicating a calendar. `idx_calendar.py` covers **2026–2027** only. Extend its verified coverage/version in a future evidence-backed change if 2025 history needs certification; do not silently fall back to weekdays or observed global price dates. Unsupported historical coverage may remain explicitly withheld in the first implementation.

## D. Event-record schema

Use a reviewed, immutable registry revision; no normal validation fetches a URL. A file-level header supplies `schema_version`, `registry_version`, reviewed market/universe/date coverage and content identity. Each source-backed event needs:

| Field | Minimum semantics |
| --- | --- |
| `event_id`, `revision` | Stable identity and positive revision; changes retain parent/history through Git or an append-only revision chain. |
| `venue`, `market_scope`, `ticker` | Exact canonical venue/market/security identity. Adapter scope must match; an equity event cannot match an index or another market by default. |
| `effective_session` | Exact canonical ISO exchange session; boundary occurs before that session's OPEN for this event class. A different timing class requires explicit support, not guesswork. |
| `event_type` | Supported enum, initially rights issue; splits/other actions may provide separate records only with explicit evidence. |
| `status` | `CONFIRMED_REFERENCE`, `PENDING_REFERENCE`, or `REVOKED`. Pending can have a confirmed boundary without usable reference. |
| `reference_price`, `reference_kind`, `currency_unit` | Positive finite decimal string and official-reference kind for confirmed records; null for pending. Units explicitly IDR per share here. |
| `source`, `source_document_id` | Author/publisher, document identity, publication date and retrieval locator/medium. A mirror transcription is identified as such. |
| `evidence_refs` | Preserved supporting evidence identities/hashes and retrieval limitations; document original hash if available, otherwise explicitly absent. A live URL alone is not an offline evidence snapshot. |
| `observed_at`, `verified_at` | Evidence observation and review availability, with timezone and precision. Unknown exact time must remain date-only/unknown rather than invented. |
| `notes` | Short provenance/scope limits and optional confirmed terms; not executable adjustment formulas. |

`effective_session` and `verified_at` are different clocks. A historical simulation must pin registry revision and information cutoff. A date-only knowledge timestamp conservatively becomes usable only after the entire stated local day; it cannot prove that the system knew the record before that day ended. A source's October 2 publication date does not prove this repository had verified it on October 2. Retrospective data-quality masking and point-in-time trading claims must be distinguished in experiment metadata.

Load-time invariants: exact key `(venue, market, ticker, effective_session)`; no overlapping active references or unresolved duplicate revisions; supported event/ref/unit enums; finite positive confirmed reference; confirmed source/document identity; canonical session; known scope; explicit precision for availability. Conflicts fail closed, not last-record-wins. Invalid records must not partially load and turn missing entries into ordinary approvals. Revocation/version changes invalidate cached adjudications.

There is no need to add columns to `price_history`. A reviewed registry plus derived dataframe columns and output-side provenance is sufficient initially. Additional subscription/entitlement/cashflow fields are **not** minimum inputs for this limit-and-withholding contract; they would be mandatory for a separately approved economic-return implementation.

## E. ENRG exact event semantics

The following is a **design record**, not an installed registry entry. All financial/event fields are taken from the already completed investigation. No precise original verification timestamp or original exchange-PDF hash is fabricated.

```json
{
  "event_id": "IDX:ENRG:RIGHTS:2026-10-05:REGULAR_NEGOTIATED",
  "revision": 1,
  "venue": "IDX",
  "market_scope": ["REGULAR", "NEGOTIATED"],
  "ticker": "ENRG",
  "effective_session": "2026-10-05",
  "event_type": "RIGHTS_ISSUE",
  "status": "CONFIRMED_REFERENCE",
  "reference_price": "1065",
  "reference_kind": "OFFICIAL_JATS_EX_RIGHTS_REFERENCE",
  "currency_unit": "IDR_PER_SHARE",
  "source": {
    "author": "PT Bursa Efek Indonesia",
    "published_on": "2026-10-02",
    "retrieval_medium": "FinancialFilings full document transcription",
    "url": "https://financialfilings.com/filings/energi-mega-persada-tbk/share-issuecapital-change/2026/64667882/"
  },
  "source_document_id": "Peng-00187/BEI.POP/10-2026",
  "observed_at": {"date": "2026-10-06", "precision": "DAY", "timezone": "Asia/Jakarta"},
  "verified_at": {"date": "2026-10-06", "precision": "DAY", "timezone": "Asia/Jakarta"},
  "evidence_refs": {
    "investigation_report": "ENRG_root_cause_report.md",
    "report_content_sha256": "420a16f75af1ce4b4fda64bb80b6694df229b4ae82b6650ede3d3b55053c1942",
    "direct_exchange_original_sha256": null,
    "issuer_document": "ENRG-PUT-IV-Prospektus-Ringkas-25-Sept-26.pdf",
    "issuer_url": "https://www.emp.id/wp-content/uploads/2026/09/ENRG-PUT-IV-Prospektus-Ringkas-25-Sept-26.pdf",
    "ksei_url": "https://web.ksei.co.id/services/registered-securities/shares/lc/ENRG"
  },
  "notes": "Issuer/KSEI confirm regular/negotiated cum Oct2 and ex Oct5, 2 old shares to 1 new at IDR310. Exchange transcription states actual cum close1440, TERP1063.333 and JATS reference1065. Direct IDX original was not retrieved. This record does not establish vendor rewriting or economic-return adjustment."
}
```

The evidence hash above was computed from the existing report bytes during this consolidation; no new acquisition was required. A fresh registry review should record its actual precise enrollment/reverification time and retain the original date-only evidence availability. Do not retrodate that new review to October 2. This schema also keeps a pending record usable as a boundary mask without fabricating its reference.

The source chain is already documented: [issuer prospectus](https://www.emp.id/wp-content/uploads/2026/09/ENRG-PUT-IV-Prospektus-Ringkas-25-Sept-26.pdf), [KSEI registry](https://web.ksei.co.id/services/registered-securities/shares/lc/ENRG), and [IDX-authored announcement transcription](https://financialfilings.com/filings/energi-mega-persada-tbk/share-issuecapital-change/2026/64667882/). These links identify prior evidence; they were not newly fetched for this audit.

### Limit proof without changing either close

```text
raw previous actual close = 1440   [2026-10-02]
raw event-day close       = 1030   [2026-10-05]
official limit reference  = 1065   [2026-10-05 only]

limit_change = 1030 / 1065 - 1 = -0.0328638497653 = -3.2863849765%
ARA_BOUND(1065) = +0.25
inclusive audit band = [-0.155, +0.255]
-0.155 <= -0.0328638497653 <= +0.255

limit_violation = FALSE
corporate_action_boundary = TRUE
close(Oct2)->close(Oct5) economic-return status = WITHHELD
```

The current audit tolerance gives numeric admission bounds **899.925..1336.575** around 1065. O1080/H1085/L1000/C1030 all lie inside those diagnostic bounds, and low≤open/close≤high. This is a proof under the inspected code's band semantics, not a certification of every intraday exchange rule/tick/fill condition.

Exact theoretical TERP `(2×1440+310)/3 = 1063.333333` corroborates the terms, but **1063.333333, rounded display1063 and JATS1065 are different quantities**. Do not store any of them as an actual close. Preserve the two actual bars and their evidence classes.

## F. Return/target contamination analysis

### The mathematical distinction

`1030/1440−1 = −28.472222%` is a raw price discontinuity between different entitlement states. `1030/1065−1 = −3.286385%` judges the event-day price against the exchange reference. Neither automatically measures the realized total return of an October 2 shareholder. The system lacks a complete rights entitlement/subscription/proceeds/execution convention for that claim.

### One-day paths

| Calculation | Current protection or bypass | Required result with the CA contract |
| --- | --- | --- |
| Oct2 close→Oct5 close; `fwd_1`/event `lag_1` | Normal bounds currently reject raw ratio | **NaN/WITHHELD**, even after both bars are admissible |
| Oct2 close→Oct5 open; `gap_1`, scanner gap/day move | Open/step guards currently reject; independent MS/raw paths may compute | **NaN/WITHHELD** as economic gap; separate `limit_open_change=1080/1065−1` diagnostic may exist |
| Decision Oct1, entry **Oct2 O1425**, exit **Oct5 O1080**; `fwd_oo_1` | Close-step window ends Oct2 and is ordinary-valid; exit-open guard alone currently catches reset | **NaN/WITHHELD**. A helper-only open fix would leak **−24.210526%** into an executable target |
| Decision Oct2, entry **Oct5 O1080**, exit **Oct5 C1030**; `fwd_oc_1` | Current close(T)→close(T+1) prerequisite masks it too | Holding interval itself **does not cross** the reset. **−4.629630%** is a same-session raw price return if anchors/data/feature timing are valid; signal eligibility is a separate decision |
| Entry Oct5 open→next session open | Later anchor absent from exact DB | Potentially comparable after reset if all actual anchors/coverage are verified; do not invent Oct6 price |
| At-ARB annotation on Oct5 | Raw close frame can mark false ARB; lag-based frame gets NaN | Use close/reference, independent of economic `lag_1`; **not near ARB under current proxy** |

The exit-boundary leak is demonstrable from **existing observed DB anchors**. `price_audit`'s OO step window ends at close(T+h), while its exit is open(T+1+h). After official-reference open validation is introduced, an event at **that exit session** must still independently invalidate the holding return. Do not inspect the post-exit close to discover it. [OO mask](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/price_audit.py#L498).

### Multi-day paths

| Window family | Exact interval to guard |
| --- | --- |
| `fwd_h`, future high/low extrema relative to close(T) | Close(T)→close(T+h), including every intermediate boundary and endpoint |
| `lag_k`, momentum k, close-based volatility | Close(T−k)→close(T); for volatility, every return in the required window |
| `fwd_oc_h` | Open(T+1)→close(T+h); entry-session reset precedes acquisition, later reset is inside holding |
| `fwd_oo_h` | Open(T+1)→open(T+1+h), including an event at the exit open |
| TP/SL/barrier/first-ARA simulation | Actual entry→each price used for payoff; do not interpret a reset gap as a stop execution |
| ARB/suspension delayed exit, holder60, Gate-B hold-through | Actual entry→actual resolved exit, not nominal h alone; check every extension and the final exit event |
| Always-long/universe/excess/rank/placebo baselines | Apply the same eligibility rules as signal returns before comparison/ranking |
| Rolling highs/lows, MA/fractal/range/volume/price-vs-cost | Complete declared information window must be representation-compatible and contain no unsupported reset/unit boundary |
| Broker cost/book/P&L | Anchor/transactions→mark or realization; no supported entitlement/accounting transition exists, so withhold spanning analytical claims |
| Persisted outcomes/weights/models | Registry/return contract that generated them must match; changing labels cannot retroactively relabel old results |

Multi-step masks must consult the unfiltered verified axis/registry. Removing the event bar or a NaN row and then shifting surviving observations is not a boundary policy. Endpoint ratios can look ordinary after offsetting moves and still cross a reset. Multiple events can cancel numerically and remain incomparable.

### Every discovered arithmetic escape route

The common FR/LR/gap/extrema builders are the main repair point, but the following require caller changes or refusal under the new contract:

- **Raw helper fallbacks:** `walk_forward_backtest._price_features_and_target` falls back to raw momentum when `lag_1` is absent and to raw CC when `fwd_1` is absent. The main supported path already supplies guarded lags/OO; do not incorrectly say it always recalculates raw momentum. Remove the remaining fallbacks as possible bypasses.
- **Independent current/next annotations:** `ara_arb_scan.load_prices/build`, `inventory_features.build`, `daily_picks.day_move/price_break/limit_up`, `ara_arb_simulation.annotate_limits` use previous-close arithmetic or infer references from lag returns. Shared limits must not replace invalid lags with official-reference returns.
- **Variable holdings:** `strategy_variants.simulate_trade`, `ara_arb_simulation.simulate_trade_with_limits`, `regime_gated_momentum.simulate_trade`, `broker_learning.holder_returns`, `experiment_1f_evaluation._search_exit/slot_outcomes` compute final price ratios after walking raw bars. Each needs a holding-span check independent of price-step admission.
- **MS outcomes/progress:** `evaluate_signals.outcome`, `daily_picks.session_returns/machine_returns/forward_excess/follow_lines` and holding/progress helpers compute ratios outside CP. Both explicit events and verified source sessions are needed; an inferred previous-weekday assignment is insufficient.
- **IP labels/features:** `inventory_features`, `ara_multiday.add_horizon_labels`, `label_compare.add_labels` compute raw rolling ratios, gaps, barrier/extrema payoffs and hold returns. Their copied limits and ≤5-day “contiguous” rule cannot establish comparability.
- **Book/rule fields:** `broker_book.rolling_state` directly computes close/previous and rv20, while average-cost P/L and `broker_rules` drift59/gain/close-cost fields can mix entitlement/share bases. Existing measured-basis containment flags are not a rights-accounting model.
- **Typed evidence and legacy research:** `inventory_evidence._market_measurement`, TX/pattern/foreign-flow studies and `experiment_2a0_event_study.event_return` perform endpoint arithmetic independently. Subtracting IHSG return, applying transaction costs, ranking, clipping or bootstrapping cannot remove this contamination.
- **Downstream anonymous returns:** metrics, sizing, learned weights and saved report/model artifacts receive numbers after ticker/date provenance may have been discarded. Mask/validate upstream and retain contract identity; do not try to infer events from return magnitudes there.

One nuance: an **ARA/ARB occurrence** label is not an economic return. A verified event-day limit occurrence can be meaningful when annotated against the correct reference. It must not be false ARB, and any associated holding payoff/features must satisfy their separate span rules. Do not conflate every binary market annotation with a shareholder-return target.

## G. Recommended initial return policy

**Choose Option A.** Preserve admissible raw bars, but output `NaN`/WITHHELD with reason `CORPORATE_ACTION_BOUNDARY` for every economic-return/target interval crossing a confirmed boundary. Pending-reference boundaries also block comparability. Unresolved source basis, action coverage, session coverage or data defects retain their own reasons. Do not change a withheld target to zero, a clipped loss, an ordinary limit-relative ratio or a delayed/postponed entry.

Define a boundary at the instant immediately before OPEN of effective session E. A span crosses if:

```text
start_anchor < boundary(E) <= end_anchor
```

Thus an entry at OPEN(E) is after the reset; an exit at OPEN(E) from an earlier holding includes it. A close-to-close interval ending E includes it. One event-day Boolean cannot alone encode every multi-day/phase combination; use the shared span function or equivalent verified interval masks.

Keep **three independent requirements** for an eligible trade/target: admissible actual anchors/path, a comparable holding interval, and valid information/features available at decision time. Same-session OC may be mathematically comparable while a model lacks eligible historical features. In version one, conservative strategy exclusion because required features or entry timing are unresolved is acceptable **only if reported with that reason**; do not falsely call every event-day intraday interval action-crossing.

Option B requires a separate design/evidence phase covering share/rights entitlement, subscription cash, rights sale/exercise assumptions, availability/timing, fees, dividends, split/share quantities, rounding and multiple actions, plus which return notion is being measured. An exchange reference alone is insufficient. No adjusted economic-return mathematics is proposed here.

## H. clean_panel design

Keep raw values and expose a small derived audited layer:

| Derived field / metadata | Purpose |
| --- | --- |
| `corporate_action_boundary`, `corporate_action_event_id`, `corporate_action_status` | Boundary on the exact effective session; retain known pending boundaries too |
| `limit_reference_price`, `limit_reference_kind`, `limit_reference_source` | Explain the limit comparison; null plus explicit unresolved status when unavailable |
| `previous_actual_close`, `previous_actual_session` | Raw provenance retained separately from reference |
| `price_admissibility_status` / unresolved reason | Distinguish valid raw bar from unknown or actual defect |
| `price_segment_id` or shared span masks | Prevent crossing shifts/windows; computed on the full verified axis, not after filtering |
| Per-target validity/reason, or a compact diagnostics ledger | Explain withheld fwd/lag/gap/extrema/variable-return values without one schema column per hypothetical horizon |
| Frame/output contract metadata | Registry content/version, rules version, return-policy version, calendar version, source/basis snapshot and knowledge mode |

`detect` uses official references for limit checks and performs discontinuity/median diagnostics within compatible segments. It must retain independent identity, numeric/OHLC-domain, duplicate and true series defects. A confirmed action cannot override nonpositive close, impossible open/high/low shape, cross-ticker response identity or an unsupported source representation.

`load_clean` must not blindly trust stale quarantine keys. Read the stored reason/provenance and overlay current adjudication: a **sole old ordinary-limit false positive** can be superseded in the derived view by a confirmed-reference result; independent or unresolved quarantine reasons remain withheld. If a legacy quarantine row has insufficient reason/version evidence, keep it unresolved. No quarantine or PH DB rewrite is necessary for this derived overlay. In the current exact DB there is no ENRG quarantine row to remove.

FR/LR should consume independent `price_step_admissible` and `return_step_comparable` concepts. Open-anchor validation uses the resolved limit reference, but OO/OC/gap label comparability follows G. `gap_1` no longer doubles as an entry-admission certificate: expose entry-open admissibility separately for simulators while keeping the economic gap unavailable across resets. This also prevents a valid OC1 from being rejected solely because an unrelated pre-entry economic gap is undefined.

Preserve existing quarantine/suspension/contiguity protection and no raw fallbacks. For OO labels, validate only the price/reference context needed by the actual exit open; the boundary mask comes from explicit event metadata and must not depend on that exit day's later close. Merges and selected-column projections must carry enough metadata or an explicit validated contract token; otherwise consumers refuse rather than quietly reconstructing targets.

No new PH schema is required. Derived output schemas/manifests may need new contract identity or withheld-reason fields. Changes to persisted outcome formats should be versioned, with old results retained under their old meaning.

## I. Broker-flow decision

**Continue using raw actual close for existing lot-derived netval, and leave reported execution-value flows unchanged.**

`backfill_inventory.insert_inventory` uses `nlot ×100×actual_close /1e9` as a billions-of-IDR price proxy. It is not actual trade-by-trade execution accounting. A reference price is a market validation anchor, not a traded price; substituting it changes the proxy without justification. For example, 100 lots at1030 produce **Rp10.3m =0.0103bn**; at1065 they produce **Rp10.65m**, an unjustified **3.3981%** increase in magnitude. Negative net lots would have their negative magnitude changed too.

This writer only writes broker rows on/before **2026-07-04**. Its October price top-up does not rewrite October broker rows. Direct `bval/sval/netval/bavg/savg` values from broker captures preserve their own actual-execution/unit/source contract. Do not run `price_audit.cmd_repair`'s close-ratio netval rescaling for a legitimate CA event.

This preservation decision does **not** approve cross-boundary broker **analytics**. Reported cash-flow sums can remain actual activity in IDR, but quantity/ADV comparisons, ratio-of-sums implied prices, rolling cost, reconstructed holdings and P/L may combine incompatible share/entitlement states. Withhold those affected derived fields/windows, or start an explicitly labeled new analytical anchor. Do not adjust historical transaction quantities/flows or represent that new anchor as the holder's entitlement-complete original position without evidence.

Sources: [backfill insertion](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/backfill_inventory.py#L237), [repair scaling](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/price_audit.py#L718), [book cost/P&L](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/broker_book.py#L407), [raw volatility](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/broker_book.py#L559).

## J. Signal-integrity design

Expected ENRG outcome after implementation:

```text
2026-10-05 ENRG: CORPORATE_ACTION_BOUNDARY / confirmed reference
actual close1030; raw previous1440; official reference1065
limit_change=-3.286385%; limit_violation=False
crossing CC/gap/OO targets: withheld under corporate-action policy
raw OHLC retained; no historical price repair
```

`check_new_contamination` must exclude **this reference-resolved false positive**, while retaining independent defects and unresolved transitions. Record boundary/reference/pending/withheld counts in stats/notes even if `fresh_suspects==0`; the current early return would otherwise omit the boundary. Do not use an unconditional ticker/date “ignore” or suppress all abnormal moves.

Check that derived labels cannot publish the raw reset as an economic return, including nominally one-day OO exit events and multi-day/delayed spans. Check source-session status before cross-source joins: MS capture date minus0/1/2 days is an exploratory alignment diagnostic, not evidence of canonical session identity. If a source session is UNKNOWN, report unknown comparison status rather than use its capture date to activate an event exception.

Add per-ticker/session coverage/rejection visibility using the verified exchange calendar and capture status. A current table maximum and a green ≤30%-failure run do not establish every ticker's freshness. Preserve original refusal diagnostics and distinguish known-reference acceptance, pending event, sparse history, mixed basis and genuinely out-of-band actual prices.

## K. SINI / RAJA treatment

| Case | Confirmed facts | Registry/validation disposition | Additional evidence |
| --- | --- | --- | --- |
| **SINI 2026-07-09** | Issuer/KSEI confirm rights boundary, regular/negotiated cumJul8/exJul9; 2 old→3 rights, exercise5000. Located C10950→8100. | Add **PENDING_REFERENCE** boundary record with `reference_price=null`. Block crossing returns. Do not override limit validation with theoretical7380 or an inferred rounded price. | Exact official exchange reference and its identifiable supporting document. Original dated raw-bar evidence/source basis remains useful for independent bar provenance, not for inventing the missing reference. |
| **RAJA 2025-08-22→25** | DB C2710/V24461500 then C534/V168074500; cache C542/V122307500 then C534/V168074500. First DB bar is cache close×5/volume÷5, next matches adjusted cache. Ledger/cache corroborate factor-five historical regime through Jul15 2026. | **Mixed-basis historical splice, no August25 event exception.** Keep basis/return quarantine; no price repair in this audit. | Source-dated as-traded historical bars, acquisition/request/body versions before/after the relevant vendor adjustment, ingest/DB lineage, consistent OHLC/volume/lot/value units; separate evidence for any proposed true event date/reference. |
| **RAJA July2026 conversion** | Prior investigation found KSEI 1:5 conversion recordJul17/distributionJul20; cache basis factor becomes one Jul16. | Do not equate administrative dates/cache regime transition with a verified exchange ex-session/reference automatically. | Exchange effective trading session/reference and source representation chronology before creating a July event record. |

SINI `(2×10950+3×5000)/5=7380` is a theoretical check only. Its exact official reference is missing, so evidence is sufficient for a **boundary record**, not a confirmed-reference override. [Prior issuer evidence](https://singarajaputra.com/wp-content/uploads/2026/07/SINI_Informasi-Tambahan-PMHMETD-fn.pdf), [KSEI SINI](https://web.ksei.co.id/services/registered-securities/shares/lc/SINI).

The RAJA cache's August close pair moves only **−1.4760%** on its own consistent adjusted basis. The code comment “2710→546 on an Aug25 split” is not event proof: **546 is the cache's August25 open, not close**. Keep the separate measured-basis ledger and corporate-action registry separate. A reconstructible factor does not establish an event session. [Relevant inaccurate comment](https://github.com/0xFridayy/DailyScraper/blob/5a2ec4181ad897e3d6455c5b7855e0d9830b1768/price_audit.py#L386).

Neither missing SINI reference nor unresolved RAJA history prevents implementing a fail-closed contract: pending/unknown states are explicit supported outcomes. They do prevent approving those respective overrides or historical returns.

## L. PR #83 integration decision

**Prefer further commits to PR #83**, with atomic deployment of writer/reference semantics and downstream comparability. The current PR cannot be treated as a completed generalized price-integrity fix.

The rationale is **not an assertion that today's ENRG refresh is frozen**. Both closes are already stored and the head deliberately grandfathers unchanged historical violations. The known risk is that a **new/revised legitimate action transition** is rejected, while a future blanket exception could admit bad analytical returns. Both sides belong in the same deployable contract.

Minimum atomic release contents:

1. Pure shared resolver/bounds/span contract, offline reviewed registry, confirmed ENRG reference, pending SINI boundary and explicit unknown/mixed-basis handling.
2. Writer guard and audit adapter use the same reference decision; unchanged legacy preservation is not mislabeled “validated.” Existing bad history does not acquire new analytical trust through grandfathering.
3. CP/FR/LR/open/gap/extrema separate price acceptance from comparability, with exit-event tests.
4. Every active scheduled adapter/report/model route in B either consumes the new contract and preserves withheld states, **or explicitly refuses CA-aware execution until migrated**. Raw direct/persisted inputs cannot silently bypass it.
5. Offline integration/regression tests and versioned output identities. Registry/rules/return-policy identity must be consistent across one run; no mixed snapshots.

For callable legacy research, preserve frozen code/artifacts and make new-contract runs use a new version or fail clearly. Do not silently rerun/re-pin previously reviewed experiments. A migration commit can establish an explicit unsupported-mode refusal while a later research redesign is reviewed; it cannot leave a route running unguarded under the new semantics.

A separate dependent PR is acceptable only if branch/release controls guarantee the **combined** code is what reaches production and prevent PR83's guard-only state from running. Sequential ordinary merges followed by “we will fix CA later” do not provide that guarantee. Multiple reviewable commits in one PR are simpler here.

Monitor each ticker's disposition. Do not loosen `MAX_FAILURE_RATE`, insert a raw ratio allowlist, substitute a reference close or weaken unrelated identity/schema/domain protections to get a green top-up.

## M. Historical DB mutation required?

**NO, for the supported ENRG pair.** October2 actual close1440 and October5 actual close1030 are compatible with the confirmed rights event and correct market reference. The exact-head DB already contains both dated bars, including the full October5 OHLCV. No missing October5 row needs importing into that snapshot. Do not manufacture an October6 price because this head has none.

Required future changes are the derived reference/adjudication/return policies and event registry, not replacement of historical OHLC. There is also no current ENRG quarantine key to delete. Even a stale sole-limit quarantine classification can be superseded in a derived audited view without changing PH.

The source-dated original October inventory response is still absent, so this audit does not certify vendor rewrite behavior or all source lineage. That limitation supplies no justification to replace a supported actual close. RAJA's separate historical correction is outside scope and remains evidence-dependent; SINI's missing reference is not a repair instruction.

## N. Exact files/functions the implementation phase would change

The following is the proposed change surface, not edits performed. All new-contract reachable paths must be migrated or explicitly refused before release. Frozen research is versioned separately; it is not permission to overwrite accepted artifacts.

| File | Exact function/contract surface | Required change |
| --- | --- | --- |
| **New `price_contract.py`** | Registry parser/domain types; `resolve_limit_reference`, centralized bounds/annotations, `return_span_status` | Pure shared ownership; typed unresolved outcomes; no network or DB side effects |
| **New `corporate_actions.json`** | File-level version/scope plus ENRG/SINI records | Offline confirmed/pending facts and pinned evidence, exact-match lookup |
| `price_audit.py` | `ara_bound`, constants; `load`, `detect`, `_reasons`, `load_clean`, `_open_anchor_valid`, `add_forward_returns`, `add_lagged_returns`, `clean_panel`; report/export/count/quarantine diagnostics | Re-export common rules, derive reference/boundary metadata, independent span masks, version-aware quarantine overlay; correct false-corruption language and RAJA comment |
| `backfill_inventory.py` | `validate_inventory_prices`, `insert_inventory`, `run_backfill` | Resolve changed transition references before writes; preserved actual OHLC/netval; explicit unresolved/pending dispositions and per-ticker outcomes |
| `idx_calendar.py` | `is_idx_session`, `latest_idx_session_before`, version/coverage | Reuse current2026–27 contract; extend verified historical coverage only if needed/supported, otherwise explicit unsupported status |
| `check_signal_integrity.py` | `check_new_contamination`, `check_freshness`, `check_cross_source`, orchestration/format stats | Separate boundary notes from contamination; per-ticker freshness and label/contract checks |
| `walk_forward_backtest.py` | `_price_features_and_target`, `load_canonical_inputs`, price snapshot identity/provenance helpers, `build_panel` | Require masked columns/metadata; remove raw momentum/CC fallback; registry/policy snapshot identity |
| `strategy_variants.py` | `_index_price_history`, `simulate_trade`, `run_strategy_search` | Carry event/path metadata; separate entry admission from economic gap and guard actual payoff span |
| `ara_arb_simulation.py` | `ara_bound`/constants, `annotate_limits`, `simulate_trade_with_limits`, `run_ara_arb_check` | Remove duplicated reference formulas; annotate via official reference without using lag as limit return; actual-exit mask |
| `regime_gated_momentum.py` | `load_neobdm`, `simulate_trade`, `simulate_predictions` | Preserve contract metadata and action-span validity through rolled exits |
| `run_ml_reports.py` | `run_xgboost_report`, `run_strategy_variants_report`, `run_ddqn_report`, `run_konglo_watch_report`, snapshot/report provenance | Same registry/policy for all components; unavailable watch status and exclusion counts |
| `ddqn_episode_data.py`, `ddqn_entry_exit.py` | `build_episode_frame`, `session_episode_ids`; environment/episode construction and reward/trade aggregation | Explicit CA episode cuts; preserve gaps and no position/reward carry across resets |
| `check_ml_health.py` | `check_panel`, `_executable_target_bounds`, `check_known_defects`, checks/formatting | Boundary/reference/label assertions; retain distinct feasibility versus comparability diagnostics |
| `daily_picks.py` | `_session_of`, `load_snapshots`, `limit_up`, `corporate_action_hint`, `day_move`, `price_break`, `recent_corporate_action`, `session_returns`, `machine_returns`, `forward_excess`, follow/holding/progress/result helpers | Verified sessions; explicit registry boundaries; remove inferred-event claims; guarded outcomes throughout |
| `evaluate_signals.py` | `load_panel`, `outcome`, `evaluate`, report status | Guard verified actual holding/extrema windows; retain unavailable reasons |
| `neobdm_scraper.py`, `neobdm_source_contract.py` | Capture/session/representation adapters, MS consumer metadata; `get_inventory_bagholders`/bagholder output contract where retained | Preserve actual values and raw capture/session distinction; prevent unsupported cross-basis implied-cost claims; no reference substitution in flow persistence |
| `inventory_features.py` | `ara_limit`, `_tick`, `_snap`, `ara_price`, `arb_price`, `build` | Shared annotations; guarded price/gap/volatility/cost windows and new panel metadata |
| `ara_arb_scan.py` | `ara_limit`, `tick`, `_snap`, `ara_price`, `arb_price`, `load_prices`, `build` | Eliminate independent “impossible move” deletion and formulas; adopt audited admission plus return masks |
| `ara_multiday.py`, `label_compare.py` | `add_horizon_labels`, `load`; `add_labels`, `load` | Separate event-aware occurrence labels from guarded economic payoffs; mask full relevant horizon |
| `scan_ara_arb.py`, `arb_veto.py` | `refresh`, `main`; `score` and output eligibility | Require versioned CA-aware panel or refuse; no malformed reset targets for training/scoring |
| `broker_book.py`, `broker_rules.py` | `basis_flags`, `anchor_after_flags`, `average_cost_book`, `rolling_state`, `ticker_bundle`; `eligibility`, `evaluate` and price/cost/drift helpers | Preserve observed transactions; separate basis/CA status, withhold crossing analytics, explicitly label new analytical anchors |
| `broker_learning.py` | `outcomes`, `holder_returns`, `broker_profitability`, `live_outcome_rows` and dependent rule/weight/alpha scoring | Shared entry/limit reference; independent actual delayed-exit masks; propagate unavailable outcomes and identity |
| `broker_learning_run.py`, `broker_learning_db.py`, `broker_dashboard.py` | `load_items`, `_book_rows`, `ticker_ctx`, `learn`, daily/weekly orchestration; derived-outcome persistence/`live_summary`; cost/P&L/chart rendering | New result contract/withheld reasons; no relabeling of saved old outcomes; boundary-aware presentation |
| `broker_collect.py`, `build_inventory_db.py`, `harvest_inventory.py` | Source envelope/cache validation and output manifest/`main` | Capture/representation provenance for refreshed inventory-derived consumers; keep financial values intact |
| `horizon_scan.py`, `multiday_features.py`, `feature_ablation.py`, `smart_money_divergence.py`, `shap_analysis.py` | `build`; `build_panel_with_multiday`; `build_multi_horizon_panel`; `build_panel_with_smart_money`; model-panel loading | Require/preserve masked columns, rolling-window validity and result identity |
| `ml_v2_experiment_1.py`, `ml_v2_experiment_1_robustness.py` | `_historical_net_lots`, `build_broker_identity_features`, `build_experiment_panel`, `run_experiment`, prediction/split identity; robustness runner/pins | Basis-compatible recovery/window masks; new experiment version, preserve accepted legacy digests |
| `experiment_1f_universe_gate.py` | `build_validated_panel`, `open_anchor_diagnostics`, `integrity_checks`, `run_gate`, identity/rule-version verification | New registry/policy inputs and diagnostics; unchanged measured-basis quarantine |
| `experiment_1f_evaluation.py` | `open_usable`, `close_step_in_band`, `_search_exit`, `slot_outcomes`, `execution_diagnostics`, return-view aggregators | Shared admission/annotations and independent actual span masks; withheld CA cannot become a fabricated cash sensitivity |
| `experiment_1f_features.py` | `price_features`, `build_features`, `history_eligible`, `preparation_ledgers`, rank input binding | Comparable windows and new contract identity before rank/placebo construction |
| `experiment_1f_manifest.py`, `experiment_1f_gate_b.py`, `experiment_1f_gate_b_contract.py`, candidate/validation/validity/normalization modules | Manifest `build`/code identity, Gate-B `load_context`/execution identity, rule contract; candidate snapshots and validity reporting | New version/refusal path; pin registry/calendar/policy with new inputs and preserve frozen authorizations/artifacts |
| `experiment_2a0_event_study.py` | `load_prices`, `event_return`, `construct_returns`, `run_study`, placebo eligibility | Representation/action-aware horizon mask before stock/market excess/baselines |
| `inventory_evidence.py`, `targeted_actor_observations.py` | `_market_measurement`, `_build_inventory_evidence`, `validate_document`; `_window`/implied-price views and evidence handoff | Explicit action-null reasons/version; compatible quantity/price windows; keep selection/actual flow semantics |
| `txchart_backtest.py`, `pattern_detector.py`, `pattern_backtest.py`, `pattern_type_backtest.py`, `foreign_flow_signal_backtest.py`, `analyze_ticker_patterns.py` | `engineer`/`evaluate`; `compute_patterns`; `simulate_trades`/baseline; `prep_price_panels`/`build_trades`; `generate_trades`; `analyze_ticker` | Action-aware source adapter and masks, or explicit refusal of uncertified new-contract runs |
| Relevant workflow/test configuration and documentation | Top-up, integrity, MS evaluation, ML, arb-veto and broker-learning routes listed in B | Atomic enabling/gates, offline contract tests, status/provenance consistency |

For the initial price/reference/return contract, generic `transaction_cost_model`, `signal_metrics`, `kelly_sizing`, ownership modules, index-only calculation code and broker-flow cash-value ingestion need no new corporate-action mathematics. Their callers must supply eligible/versioned inputs. BandarmoloNY is excluded from this change surface.

## O. Required regression/mutation tests

These are **implementation-phase requirements**, not tests executed in this read-only audit. Use disposable in-memory/temp fixtures; never production databases. Real observed ENRG anchors may be used as fixtures but must remain labeled reconstructed test input, not original inventory responses.

| Test group | Required assertion |
| --- | --- |
| Confirmed ENRG writer/audit agreement | With official1065, new event-day close1030 is in-band in both paths; stored Oct2/Oct5 raw OHLCV remain unchanged; raw previous provenance stays1440. |
| Current grandfathering behavior | Identical existing1440→1030 refresh passes preservation; new/revised endpoint exercises shared resolver. An unchanged violation is never promoted to “validated/comparable” merely because skipped. |
| No event / wrong ticker/date/market | Same raw reset without an applicable confirmed reference fails closed; ENRG record cannot change another ticker, prior/next session, cash market or index. |
| Pending SINI / revoked/conflicting event | Pending refnull blocks override and crossing return; theoretical7380 is never used automatically; conflicting active records fail closed. |
| Ordinary-session equivalence | No behavioral drift in current ordinary bounds/REAL normalization; exact200/5000 tiers, inclusive endpoints and ±TOL tests. Day after event uses actual event close, not carried official1065. |
| Identity/domain preservation | Existing malformed symbol/date/duplicate-date/nonfinite/nonpositive/bool/overflow cases still refuse before price/broker writes; confirmed event cannot bypass them. Open/low/high checks remain effective. |
| Session/trust tests | Missing predecessor, quarantined predecessor, suspended/missing sessions and unsupported calendar/basis do not become an ordinary daily comparison or bridged return. Confirmed official reference resolves only its scoped limit check. |
| **Observed OO1 exit reset** | DecisionOct1 →entryOct2 O1425 →exitOct5 O1080 stays unavailable even when exit-open reference validation passes. Do not emit **−24.210526%**. Remove the exit-boundary mask as a mutant: the test must fail. |
| CC/gap/lag reset | Oct2 C1440→Oct5 C1030 fwd1/lag1 and gap close→open remain unavailable; diagnostic −3.286385% is never substituted into any economic label. |
| OC entry reset versus holding reset | OpenOct5→closeOct5 can yield −4.629630% under admissible anchors and eligible context; old-close→event-close cannot. Test signal/feature eligibility separately. |
| Multi-h/intermediate/offsetting actions | Every h/k and extrema/barrier window crossing one/multiple events is withheld even if endpoints yield an in-band ratio; deleting the event row still leaves the registry boundary active. |
| Variable exits and TP/SL | Nominal and delayed/suspension/ARB exits check actual final interval; event at first usable exit open is caught. A reset must not produce a fictitious stop fill or a cash-zero fallback. |
| DDQN episode/reward | No position/reward crosses CA, no NaN-drop bridge; new episode only with eligible post-event observations. |
| MS/session adapters | Raw Oct6 response with UNKNOWN session must not receive an Oct5 exception solely from timestamp/HLC match; canonical verified session fixture can resolve. MS outcomes/progress cannot bypass masks. |
| Broker flow and analytics | Same actual lots/close produce identical netval; official1065 is not persisted as close/proxy; cutoff/direct values unchanged. Book/rule/rv/drift/cost spanning event withheld without altering source transactions. |
| RAJA negative control | August mixed-basis splice remains unresolved/quarantined; factor-five measured ledger or inaccurate code comment cannot create an Aug25 event. No automatic OHLC/volume repair. |
| Cleaner/cache lifecycle | Confirmed sole old-limit quarantine reason can be superseded in derived view; independent duplicate/domain reasons stay blocked; registry version/hash changes invalidate cached adjudication. |
| Downstream/rank/model/persistence | Every active path requires new metadata or refuses; column-dropping/raw fallback mutants fail; signal and benchmark masks agree; frozen digests/manifests/results remain unchanged. |
| Offline/deterministic/as-of | Network forbidden during resolver tests; same input+registry/rules/policy yields identical result; date-only known-at respects precision; future/revoked revisions do not silently alter pinned historical runs. |

Other useful mutants: replace reference with raw previous; derive reference from TERP; use raw cum close for ARA tier; convert UNRESOLVED to pass; make pending fall through; carry reference to next day; check only nominal exit; replace unavailable return by0; remove an intermediate event; choose last duplicate record; infer event from ratio; substitute reference for netval actual close. Each must be killed by a semantic assertion, not merely by a snapshot that mirrors implementation.

Extend existing relevant suites: `test_inventory_capture.py`, `test_walk_forward_canonical.py`, `test_ddqn_canonical.py`, `test_daily_picks.py`, `test_arb_veto.py`, `test_broker_book.py`, `test_broker_rules.py`, `test_broker_learning.py`, `test_broker_learning_run.py`, `test_inventory_evidence.py`, `test_inventory_signal.py`, `test_targeted_actor_observations.py`, `test_experiment_1f_gate_b.py`, `test_experiment_1f_phase2.py`, `test_experiment_2a0_event_study.py`, `test_idx_calendar.py`, and source-contract tests where session metadata is affected. Add a pure price-contract suite and writer/audit parity tests. No BandarmoloNY suite or production DB is a mutation target.

## P. Findings by severity

| Severity | Finding | Evidence / implication |
| --- | --- | --- |
| **BLOCKER — merge condition** | Current PR's mandatory new/revised-transition veto lacks explicit legitimate-event references. | ENRG new1030 after1440 is rejected; unchanged pair is grandfathered. Atomic contract/consumer deployment required before general-integrity merge. No actual production rejection was demonstrated. |
| **BLOCKER — incomplete-fix condition** | Fixing limit/open admission without separate span masks can publish false economic targets. | Observed OO1 anchors1425→1080 would emit−24.21% after exit-open admission clears. Writer/detector-only change is insufficient. |
| **HIGH** | Multiple direct MS/IP/TX/experimental/book paths bypass common FR/CP semantics. | Independent ratio/annotation, rolling cost/volatility and delayed-exit formulas listed in B/F; all active paths must migrate or refuse. |
| **HIGH** | Existing data is not globally raw; RAJA is a measured mixed-basis splice. | Canonical-contract declaration cannot relabel old DB/parquet or turn Aug25 into an event exception. |
| **HIGH** | Reference-relative change can be confused with economic return or traded price. |1030/1065 is limit diagnostic only;1065 cannot replace an actual close, netval proxy or rights-accounting model. |
| **MEDIUM** | Existing quarantine cache and selected-column consumers lack new registry identity. | Sole false labels may persist, or metadata may be lost; versioned derived overlay and provenance needed. Current exact DB has no ENRG quarantine key. |
| **MEDIUM** | MS sessions are UNKNOWN and legacy consumers use weekday/date-offset heuristics. | Raw Oct6 HLC corroboration is not a source-dated bar; no current C1030 canonical MS outcome is proven. |
| **MEDIUM** | Table freshness and ≤30%-failure success can conceal ticker-level refusal; sparse rows are treated as daily neighbors. | Illustrative1/45=2.22%; exact calendar/trust and per-ticker dispositions needed. |
| **MEDIUM** | SINI reference remains missing; historical calendar/source/action coverage may be unsupported. | Pending boundary and withheld results are valid first-version outcomes; do not infer reference or certify uncovered history. |
| **MEDIUM** | Frozen/persisted experiments and learned outcomes have prior contracts. | New registry/policy changes data eligibility; preserve original artifacts/digests and establish a new contract identity. |
| **LOW** | Duplicated tier/tick/near-limit conventions can drift. | ExactRp200 discrepancy and distinct tolerances need a shared owner and targeted tests; no universal JATS rounding rule proven here. |
| **LOW** | Original dated October inventory response/direct IDX original were not preserved. | Retain evidence-chain limitations; stronger provenance can be added later, with no invented rewriting claim or unnecessary price repair. |
| **INFO** | ENRG's official-reference false positive is established and raw bars are supported. | Reference1065 admits1030; both existing closes stay intact. |
| **INFO** | PR83's identity/REAL/date/rejection-evidence protections remain valuable. | Preserve them while changing reference semantics; grandfathering is historical preservation, not adjudication. |
| **INFO** | Option A supports unresolved cases without speculative adjustment mathematics. | Missing SINI ref and RAJA history do not block implementing explicit pending/withheld states. |

## Q. Final verdict

**READY TO IMPLEMENT CORPORATE-ACTION CONTRACT.**

The bounded first implementation is sufficiently specified: raw actual canonical prices; one offline, versioned reference/rule/span contract; explicit ENRG1065 and pending SINI; preserved RAJA basis quarantine; and Option A withholding across actual holding boundaries. No economic adjustment formula or historical ENRG mutation is needed.

PR #83 should remain unmerged until that implementation and the required tests are complete, with every active downstream path covered or explicitly refused. Broader historical raw-series certification, SINI's reference override, RAJA repair and entitlement-complete economic returns each retain their separate evidence requirements.

This report completes the requested read-only audit. No implementation, database mutation, historical repair, PR merge or BandarmoloNY change was performed.