# Current state handoff

**HEAD:** `9f094d6` — local == `origin/master`.

---

## Completed

| Step | Commit | What |
|---|---|---|
| — | `927ccc6` | Preserve pattern detection + ownership audit tooling (3 files) |
| 1 | `e290676` | `transaction_cost_model.py`: `sharpe_from_net_returns` → `net_return_stats` via `signal_metrics.trade_stats()`. AST guard widened: `sqrt(252/N)`, `sqrt(N*252)` now caught. |
| 2 | `7585a39` | `txchart_backtest.py` + `pattern_backtest.py`: all invalid Sharpe annualisation removed, replaced with `trade_stats()` / `ret_per_risk` / `hit_edge`. |
| 3a | `eda3724` | `pattern_type_backtest.py`: Sharpe (including the `sharpe_from_net_returns` import and the `holdout_sharpe_portfolio` ranking key) replaced with date-balanced hit edge. |
| 3b | `fcc311d` | `foreign_flow_signal_backtest.py`: same Sharpe → date-balanced hit edge replacement; hardcoded absolute path at the old L47 replaced with `DB = os.path.join(HERE, "txchart_history.db")`. |
| — | `0545cd9` / PR #34 (`a67e216`) | Authoritative price reconciliation code restored. |
| — | `c9d5632` / PR #35 (`11d2dcb`) | `neobdm.db` repaired: cloned cross-ticker price history fixed. `cross_ticker_dup` is now **0** (confirmed via `price_audit.py count cross_ticker_dup`, the authoritative CI-gate command). |
| 4 | `9f094d6` | `regime_gated_momentum.py`: now tracked and remediated — no Sharpe/annualized Sharpe anywhere in the file; threshold selection (`select_threshold`) uses date-balanced `daily_hit_edge` as the headline metric, with `min_signal_days` reliability gating (pooled `hit_edge` is only a deterministic tie-breaker). `check_ml_health.py` (added to `CORE_MODULES`) and `test_pipeline.py` (new `test_regime_threshold_selection_uses_daily_edge_and_minimum_dates`) changes are committed alongside it. |

---

## SQRT252 policy

**`SQRT252_BUDGET = 0`.** Unchanged. The guard in both `check_ml_health.py` and `test_pipeline.py` matches `sqrt(...)` calls whose argument subtree contains the literal constant `252` *anywhere* — so `sqrt(252)`, `sqrt(252/N)`, `sqrt(N*252)` all count. `sqrt(5)` / `sqrt(10)` z-score normalisations are not flagged. `regime_gated_momentum.py` adds no new sqrt-252 site; the budget is confirmed still 0 by `check_ml_health.py`.

**Why `sqrt(252/N)` is also forbidden.** HANDOFF.md TEMUAN 2 names two independent defects: (1) dimensional — per-trade returns scaled as daily, (2) independence — concurrent cross-ticker trades counted as independent draws (signal_metrics.py measures +0.275 pairwise correlation → ~39 tickers ≈ 3.4 independent series). Rescaling the scalar addresses only (1) and makes the site invisible to the guard, which is worse than leaving it alone. Prescription: "ganti metrik, jangan tambal formula."

---

## Tier B file status

All four originally-flagged Tier B files are now disposed of:

| File | Status |
|---|---|
| `pattern_type_backtest.py` | **Done** (`eda3724`). No remaining Sharpe. |
| `foreign_flow_signal_backtest.py` | **Done** (`fcc311d`). No remaining Sharpe; absolute path fixed. |
| `regime_gated_momentum.py` | **Done** (`9f094d6`). Tracked, no Sharpe, date-balanced `daily_hit_edge` selection with `min_signal_days` gating. |
| `regime_validation.py` | **Still parked separately, untracked.** Single-asset non-overlapping daily series — the one file where `sqrt(252)` is dimensionally defensible. Residual issue: autocorrelation in regime state makes t-stat and Sharpe SE understated. Requires an explicit policy decision (narrow `SQRT252_BUDGET` raise to 2, file-specific exemption) before it can be committed. Not part of this remediation pass. |

---

## Out of scope

- **Kelly / position sizing:** `kelly_sizing.py` is dormant, never imported from any pipeline. Leave untouched. Confidence/quality metrics (`daily_ic`, `hit_edge`, `ret_per_risk`, etc.) must stay separate from sizing — none of the Tier B remediation work introduced any sizing logic.
- **Experiment #2:** Not started. `ML_V2_EXPERIMENT_2_OWNERSHIP_ROADMAP.md` L1–30: "Specification stage — not yet implemented."
- **Experiment #2A0:** Remains **BLOCKED at observability** under the frozen stop rule (`EXPERIMENT_2A0_EVENT_STUDY_RESULTS.md`). Experiment #2A ML remains **FAILED**. Experiment #2B has not been started. This status is unchanged by the Tier B work above and must not be altered without resolving the publication-timestamp recovery audit.
- **Entity canonicalization / owner-broker mapping:** Untouched.
- **Ownership DB:** `neobdm_ownership.db` read-only audits reproduced and consistent.

---

## Future architecture note (not implemented)

Preserved for reference only — do not build this yet:

> Mispricing -> Catalyst -> Information Diffusion -> Flow -> Price/Volume Trigger -> ML Markup Probability -> Confidence/Uncertainty

---

## Next task

Tier B remediation (Sharpe removal + date-balanced metrics) is complete for all four originally-listed files as of `9f094d6`, except `regime_validation.py`, which stays parked pending the `SQRT252_BUDGET` exemption policy decision noted above. No other next task is set here.
