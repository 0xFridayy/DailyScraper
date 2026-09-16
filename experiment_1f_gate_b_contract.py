"""Experiment #1F Gate-B contract and pre-registration.

Gate A is a DATA-ADMISSIBILITY gate and stays frozen. This contract REFERENCES
Gate A by hash; it does not redefine it. It freezes the immutable Gate-A ->
Gate-B export, the Stage-1 design and the analysis plan BEFORE any Gate-B fit.

Two separate questions, never conflated:
  EXECUTION INTEGRITY  did the pre-registered experiment run without any
                       invariant violation? (technical PASS/FAIL)
  SCIENTIFIC OUTCOME   what evidence exists for A, B, C and D?
                       (GRADUATE_TOWARD_SPECTRA / SENSITIVITY_ONLY / REJECT_FOR_NOW)

Status READY_FOR_FIRST_FIT means only that methodology, inputs, features,
samples, statistics, timing, placebos and execution policies were frozen before
any Gate-B model result existed. It does NOT mean Gate B passed. After the first
Gate-B model output nothing here may change; a change is a new contract version.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATE_DIR = os.path.join(HERE, "backtest_out", "experiment_1f_candidate")
EXPORT_DIR = os.path.join(CANDIDATE_DIR, "gate_b_inputs")
CONTRACT_DIR = os.path.join(CANDIDATE_DIR, "gate_b")
PINS_FILE = os.path.join(HERE, "experiment_1f_gate_b_preparation_pins.json")
CONTRACT_VERSION = "gate_b_contract_v2"
READY = "READY_FOR_FIRST_FIT"
DRAFT = "DRAFT_PREFIT_EXECUTION_REVIEW"

PROVENANCE = {
    "supersedes": {
        "version": "gate_b_contract_v1", "status": READY,
        "canonical_sha256": "dffad173a812bb96eeba46e30fb6d7cf0d40aa118da76a68304217568457ed5d",
        "document": "backtest_out/experiment_1f_candidate/gate_b/gate_b_contract_v1_ready_for_first_fit.json",
        "document_sha256": "21fb930ae7b7a1ccb9cea207df0cf8b7b1d9913789f2521b1ca4021196146a90"},
    "reason": ("pre-fit execution review: entry rule overclaimed board-specific caps; exit side was not "
               "symmetric; HOLD_THROUGH searched only the next row; execution code identity was not frozen"),
    "model_results_observed_before_revision": False,
}

RESEARCH_QUESTION = (
    "Can EOD(T) information rank stocks cross-sectionally in a way that produces genuine out-of-sample "
    "predictive separation and a defensible Top-3 outcome after the next execution opportunity? "
    "A stock-selection / ranking experiment, not an expected-return magnitude model.")

# ── Gate A (referenced, frozen) ────────────────────────────────────────────
GATE_A = {
    "role": "DATA_ADMISSIBILITY",
    "frozen_commit": "b31c2b6eb80ea3ea0c469de0460086a66f974856",
    "mode": "PRIMARY_PIT_CONSERVATIVE",
    "manifest": {"path": "experiment_1f_manifest_v3.json",
                 "sha256": "5103971214bb8df4efaa49e2abb24f9cb9c2cf2a8560c745ab352eb8cba3a0f9"},
    "report": {"path": "gate_a_primary.json",
               "sha256": "d6c5d2e190f34d8cfbd1212699415dcad296f4360ce048a4019d3563f5a62921"},
    "panel_digest": "c7b117efa4a27567",
    "broker_digest": "a7bc0059d3028a36",
}

UNIVERSE = {
    "universe_digest": "7a4ce50ffb5a7ebe",
    "universe_json_git_content_sha256": "67c54a2b434da5ec76c2ea18007b4c65f1cdbc210e9404958cc3be5abdc40be4",
    "approved": 298, "working": 297, "excluded": {"WIKA": "empty raw capture; excluded symmetrically"},
    "frozen_at_utc": "2026-09-05T18:50:36Z", "sample_last_session": "2026-08-21",
}

# ── Step 0: immutable export; every digest has exactly one meaning ─────────
EXPORT = {
    "method": ("in-process Gate A at the verified-clean frozen commit, driven by a scratch script outside the "
               "repository; written once into the gitignored candidate area; refuses to overwrite"),
    "directory": "backtest_out/experiment_1f_candidate/gate_b_inputs",
    "export_manifest_sha256": "a94020c24b3fc5773e003650cb142f768e95154ff2f52b47b92dbf80a7ea439c",
    "files": {
        "gate_a_panel.parquet": {"sha256": "343cc6370431a5d78be93bd060fecd5a29e95afb5eb0d2b0f8cbb02de5b22d29",
                                 "rows": 67952, "tickers": 297, "key": ["ticker", "date"], "duplicate_keys": 0},
        "gate_a_broker.parquet": {"sha256": "e50dc4a1f0a4acbf0edbf942188faf20b249e617a502e62290bd22596d052394",
                                  "rows": 2634054, "tickers": 297, "key": ["date", "ticker", "broker_code"],
                                  "duplicate_keys": 0},
        "gate_a_session_axis.parquet": {"sha256": "d7c55bae48a11a5c9ab1ce3cd6afa5e888abe437f18362f70191386c6324759c",
                                        "rows": 67959, "tickers": 297, "key": ["ticker", "date"], "duplicate_keys": 0},
        "gate_a_calendar.json": {"sha256": "cb6c100edf0a3edfa5b32d03ed059b4ceb85848af1befc7af6163b352d76b2c8",
                                 "sessions": 239, "first": "2025-08-22", "last": "2026-08-21"},
    },
    "digests": {
        "panel_semantic": {"value": "c7b117efa4a27567", "file": "gate_a_panel.parquet", "kind": "frame_digest",
                           "meaning": "Gate-A panel identity (reproduces gate_a_primary.json panel_digest)",
                           "columns": ["date", "ticker", "open", "high", "low", "close", "volume",
                                       "fwd_oo_1", "fwd_oo_2", "fwd_oo_3", "fwd_oo_4", "fwd_oo_5", "gap_1"],
                           "order": "as emitted by Gate A (ticker, date)"},
        "broker_semantic": {"value": "a7bc0059d3028a36", "file": "gate_a_broker.parquet", "kind": "frame_digest",
                            "meaning": "Gate-A broker identity (reproduces gate_a_primary.json broker_digest)",
                            "columns": ["date", "ticker", "broker_code", "netval", "bval", "sval"],
                            "order": "as emitted by Gate A"},
        "lot_columns_emitted_order": {"value": "033a31b30194883a", "file": "gate_a_broker.parquet",
                                      "kind": "frame_digest",
                                      "meaning": "exported lot columns in Gate-A emitted row order",
                                      "columns": ["date", "ticker", "broker_code", "nlot", "blot", "slot"],
                                      "order": "as emitted by Gate A"},
        "lot_content_canonical": {"value": "de23c817da1e080c", "file": "gate_a_broker.parquet", "kind": "frame_digest",
                                  "meaning": "exported lot CONTENT, independent of row order; unrelated to quarantine",
                                  "columns": ["date", "ticker", "broker_code", "nlot", "blot", "slot"],
                                  "order": "stable sort by date, ticker, broker_code",
                                  "dtypes": {"date": "str", "ticker": "str", "broker_code": "str",
                                             "nlot": "int64", "blot": "int64", "slot": "int64"}},
        "session_axis_semantic": {"value": "43f34d5d1f959ffd", "file": "gate_a_session_axis.parquet",
                                  "kind": "frame_digest", "meaning": "derived session axis with both validity flags",
                                  "columns": ["date", "ticker", "open", "high", "low", "close", "volume",
                                              "price_valid", "broker_basis_valid"],
                                  "order": "ticker, date"},
        "basis_invalid_sessions": {"value": "29f729a4473aa32c75bbd5d7878de7b85f00503e5b2400f546023ddcc9ef030b",
                                   "kind": "sha256", "count": 1425,
                                   "meaning": "Gate-A broker-basis-invalid SESSION keys",
                                   "schema": "'date|ticker' strings, sorted ascending, joined by '\\n', UTF-8"},
        "basis_invalid_sessions_as_published": {
            "value": "e0a8729a69e316133aab0c933f8c63f05b63470351ed0de806b4b2c8cc6634f3", "kind": "sha256",
            "count": 1425, "meaning": "the same session set in gate_a_primary.json invalid_keys format",
            "schema": "'TICKER DATE' strings exactly as published (sorted), joined by '\\n', UTF-8"},
        "basis_quarantined_broker_rows": {
            "value": "2308ca11972103d5a64c3c6f3ae399913522c5491a4b6e8785f29909c637fa69", "kind": "sha256",
            "count": 55404,
            "meaning": ("SOURCE broker rows (broker_daily_v2.parquet) excluded from the export; every one lies on a "
                        "basis-invalid session and together they cover all 1,425"),
            "schema": "'date|ticker|broker_code' strings, sorted ascending, joined by '\\n', UTF-8"},
        "broker_code_vocabulary": {"value": "d3461ea7ed7376c12dd7ba4742a8085f994e3c7eee117bf971fba95f7f7b239c",
                                   "kind": "sha256", "count": 93, "meaning": "sorted broker codes",
                                   "schema": "codes sorted ascending, joined by '\\n', UTF-8"},
        "value_with_zero_lot_keys": {"value": "2997b7f6e6481f94a4c0c7898b29847aef32896a64b997010905f1f1206352b0",
                                     "kind": "sha256", "count": 733,
                                     "meaning": "rows where a side reports value with zero lots (CTRA 359, BRPT 209, TINS 165)",
                                     "schema": "'date ticker broker_code' strings, sorted ascending, joined by '\\n'"},
    },
    "source_identity": {"source": "broker_daily_v2.parquet",
                        "source_sha256": "ddd03020dedd2d67659f064b71abef0de5eb408e57a99f7cd49f3f5ce32751a5",
                        "nlot_blot_slot_exact_mismatches": 0, "export_rows_unmatched_in_source": 0,
                        "nlot_equals_blot_minus_slot_violations": 0,
                        "statement": "exported lots are the admitted source lots; nothing is reconstructed"},
    "domain_separation": {"broker_rows_on_price_quarantined_sessions": 254,
                          "statement": "the broker export is not joined to the price panel"},
    "export_verification_record_sha256": "1293ca00d1283cbd99d27a684b3fdc57a0db0b73952463486e3363ec204cf999",
}

# ── Timing, target, label ──────────────────────────────────────────────────
TIMING = {
    "information_cutoff": "EOD(T)", "decision": "after EOD(T)", "entry": "open(T+1)",
    "confirmatory_outcome": "fwd_oo_1 = open(T+1) -> open(T+2)",
    "exploratory_outcomes": ["fwd_oo_2", "fwd_oo_3", "fwd_oo_4", "fwd_oo_5"],
    "horizon_binding": "one parameter h selects the outcome, the rank label, both purges and evaluation; asserted",
    "forbidden_feature_prefixes": ["fwd_", "gap_", "target", "label", "y_"],
    "no_oos_control": ("best_iteration uses EVAL (pre-OOS) data only; OOS outcomes never select hyperparameters, "
                       "features, seeds, objective, thresholds or iterations"),
}

LABEL = {
    "name": "rank_label_h",
    "definition": "within-date percentile rank of fwd_oo_h: (average_rank - 0.5) / n",
    "reference_set": ("every Gate-A panel row with a valid fwd_oo_h on that date; independent of sample, arm and "
                      "variant, so labels are identical wherever a key appears"),
    "ties": "average rank", "range": "(0, 1); each date's mean is exactly 0.5",
    "min_names_per_date": 10, "evaluable_dates_below_min_names_h1": 0,
    "missing_or_censored": "no label; excluded from fit and early-stopping rows; never imputed; key stays eligible",
    "training_rows": "sample keys on FIT dates (fit) and EVAL dates (early stopping) with a non-null label",
}

MODEL = {
    "estimator": "xgboost.XGBRegressor 3.3.0",
    "objective": "reg:squarederror on rank_label_h",
    "eval_metric": "rmse on rank_label_h over EVAL dates (pre-OOS)",
    "fixed_params": {"max_depth": 4, "learning_rate": 0.05, "n_estimators": 100, "subsample": 0.8,
                     "colsample_bytree": 0.8, "reg_lambda": 1.0, "early_stopping_rounds": 10,
                     "n_jobs": 1, "tree_method": "hist"},
    "comparability_note": "#1E trained on raw returns with an MAE stopping metric; #1F deliberately does not",
    "not_run": "raw-return or clipped-excess modelling is not a second headline experiment",
    "xgboost_objective": "reg:squarederror",
    "xgboost_eval_metric": "rmse",
    "logged_per_fit": ["best_iteration", "fit_rows", "eval_rows", "test_rows", "feature_count"],
}

EVALUATION = {
    "daily_ic": ("Spearman (average ranks) of prediction vs RAW fwd_oo_h over sample keys with both present; "
                 "defined with >= 10 names; constant predictions on a date score IC 0.0"),
    "equivalence": ("the rank label is a tie-preserving monotone transform of fwd_oo_h over a superset of every "
                    "sample, so Spearman vs raw return equals Spearman vs the label; no non-equivalence arises"),
}

BROKER_TIMING_ARMS = {
    "lag0": {"latest_broker_session": "T", "interpretation": "TIMING-UNVERIFIED RESEARCH ARM; not executable"},
    "lag1": {"latest_broker_session": "T-1 (market calendar)",
             "interpretation": "CONSERVATIVE TIMING / GRADUATION ARM; not historically PIT-proven"},
    "mechanism": ("anchor = calendar session T-lag, the ticker must have an axis row there; every B/C/D window "
                  "is formed from observations at or before the anchor; no row shifting"),
    "graduation_arm": "lag1",
    "basis_invalid_decision_keys": ("validity is observation-based: under lag1, 7 decision rows whose own session is "
                                    "basis-invalid keep features that consume only valid sessions at or before T-1"),
}

VALIDITY_RULES = [
    "Price domain (Gate-A panel) governs price features, decision rows and outcomes only.",
    "A broker session is valid iff the ticker has an axis row, broker rows exist and broker_basis_valid is true.",
    "On a valid session an absent broker code contributes 0 lots.",
    "An invalid or unobserved session makes every window that consumes it unavailable; nothing is imputed.",
    "Windows count GLOBAL market-calendar sessions; any calendar gap breaks a window.",
    "C windows 1/3/5/10/20 and D's 60 require a complete valid lookback; recovery follows automatically.",
    "No unbounded cumulative feature; no reset; no re-anchor; no inferred suspension zeros.",
    "Price quarantine does not invalidate broker observations; later windows consume them.",
    "A window whose sum_b blot is 0 is unavailable.",
    "Value-with-zero-lot rows are kept as reported and never affect eligibility.",
]

FAMILIES = {
    "A": {"columns": ["lag_1", "lag_3", "lag_5", "lag_10", "lag_20", "volume_ratio", "log_adv20"],
          "definitions": {"lag_k": "Gate-A panel close(T)/close(T-k)-1, contiguity- and ARA/ARB-guarded",
                          "volume_ratio": "volume(T)/mean(volume T-5..T-1); only where lag_5 is non-null",
                          "log_adv20": "ln(mean(volume T-19..T)); only where lag_20 is non-null"}},
    "B": {"adds": ["broker_concentration", "n_brokers", "net_buy_ratio", "retail_presence_pct", "broker_correlation_1d"],
          "definitions": {
              "broker_concentration": "top-3 |netval| / sum |netval| on the anchor session",
              "n_brokers": "broker rows on the anchor session",
              "net_buy_ratio": "share of broker rows with netval > 0 on the anchor session",
              "retail_presence_pct": "sum |netval| over {XL, XC, YP, PD} / sum |netval| on the anchor session",
              "broker_correlation_1d": "Pearson of per-code netval, anchor vs preceding market session, >= 3 common codes"},
          "excluded": {"net_flow_total": "identically zero by market clearing; not replaced"}},
    "C": {"template": "broker_{code}_flow_{w}d", "windows": [1, 3, 5, 10, 20], "codes": 93,
          "definition": "sum_window nlot_code / sum_window sum_b blot"},
    "D": {"template": "broker_{code}_accum_flow_60d", "window": 60, "codes": 93,
          "definition": "sum_60 nlot_code / (sum_60 sum_b blot / 60)  [ADV60 units]",
          "semantics": "bounded accumulated observable broker net flow; NOT true ownership inventory"},
    "counts": {"A": 7, "B": 12, "C": 477, "D": 570},
    "nesting": "set(A) < set(B) < set(C) < set(D), asserted",
    "attribution": {
        "B-A": "B-A plus the B aggregate-alignment placebo support ticker-linked aggregate broker information",
        "C-B": ("C-B plus the identity-shuffle placebo jointly support a broker-identity claim; C also changes "
                "representation and dimensionality, so C-B alone is not an identity effect"),
        "D-C": "D-C plus the D state placebo jointly support ticker-linked bounded broker-state information"},
}

SAMPLES = {
    "pairwise_primary": {"pairs": [["A", "B"], ["B", "C"], ["C", "D"]],
                         "rule": "decision rows whose LARGER variant's columns are all non-null under the arm",
                         "shared_within_pair": ["keys", "labels", "split ledger", "OOS dates", "n_daily_ic"]},
    "all_variant_intersection_sensitivity": {"rule": "rows with every A/B/C/D column non-null",
                                             "assertion": "equals the C-vs-D key set in both arms; divergence fails",
                                             "role": "SECONDARY; absolute A/B/C/D levels compared only here"},
    "timing_arm_intersection_sensitivity": {"rule": "per sample, keys eligible under BOTH lag0 and lag1",
                                            "role": "ROBUSTNESS DIAGNOSTIC separating timing from sample composition; "
                                                    "never replaces the lag1 primary sample"},
    "A_full_panel": {"rule": "rows with every A column non-null", "role": "family A"},
    "eligibility": "keys never require a label, so post-decision censoring cannot remove a name from selection",
    "counts": "see preparation_pins",
}

SPLITS = {
    "function": "walk_forward_backtest.make_walk_forward_splits (frozen, imported)",
    "params": {"train_min": 30, "test_window": 6, "eval_fraction": 0.20, "embargo": 0,
               "min_fit_days": 24, "min_eval_days": 3},
    "dates": "contiguous calendar slice spanning the sample, keeping sessions whose open(T+1+h) is in the calendar",
    "purges": "fit->eval and train->test purges both equal h market sessions",
}

SEEDS = {"model": [17, 19, 23, 29, 31], "digest_of_record": 17, "rule": "17 and the next four primes",
         "statistical_role": "model seeds are not independent observations; bootstrap over dates, never seeds",
         "placebo": "all placebo nulls use model seed 17 only",
         "placebo_rng": "numpy default_rng([17, stream, draw]); stream B=2, C=3, D=4; draws 0..49",
         "bootstrap_rng": "numpy default_rng([17, 7, pair_index, block_length]); pair_index B-A=0, C-B=1, D-C=2, A=3"}

PLACEBOS = {
    "B_alignment": {"sample": "A-vs-B pair keys (lag1)", "unit": "one date",
                    "transform": "the 5-column B-only vector permuted JOINTLY across tickers within the date",
                    "preserves": ["A columns", "keys", "dates", "labels", "each date's multiset of B vectors"],
                    "destroys": "ticker <-> aggregate-broker linkage"},
    "C_identity": {"sample": "B-vs-C pair keys (lag1)", "unit": "one valid (ticker, session), before timing/windows",
                   "transform": "the 93-value nlot vector permuted across broker codes",
                   "preserves": ["per-session multiset and sum of nlot", "sum_b blot", "validity", "A and B columns"],
                   "destroys": "which broker a column denotes, across tickers and days"},
    "D_state": {"sample": "C-vs-D pair keys (lag1)", "unit": "one date",
                "transform": "the 93-column D vector permuted JOINTLY across tickers within the date",
                "preserves": ["all non-D columns", "keys", "each date's multiset of D vectors"],
                "destroys": "the ticker a D vector belongs to"},
    "draws_per_family": 50, "model_seed": 17, "arm": "lag1", "horizon": 1,
    "fits": "the smaller variant is fitted once per seed; each draw refits only the larger variant",
}

STATISTICS = {
    "confirmatory_hypotheses": ["B-A", "C-B", "D-C"],
    "arm": "lag1", "horizon": 1, "sample": "the pair's complete-case keys",
    "primary_series": "per date: mean over the 5 model seeds of [daily IC(larger) - daily IC(smaller)]",
    "primary_effect": "theta_hat = mean of the primary series over the pair's IC dates",
    "placebo_effect": "theta_17 = mean over the same dates of the seed-17 daily IC delta",
    "bootstrap": {"method": "circular block bootstrap over dates of the primary series", "replicates": 10000,
                  "block_length": 10, "sensitivity_block_lengths": [5, 20]},
    "ci": "two-sided 95% basic (reverse-percentile) interval: [2*theta_hat - q_0.975, 2*theta_hat - q_0.025]",
    "p_value": ("equal-tailed two-sided: min(1, 2*min(1+#{theta*-theta_hat >= theta_hat}, "
                "1+#{theta*-theta_hat <= -theta_hat}) / (1+B)); consistent with the basic interval"),
    "multiplicity": "Holm step-down over the three p-values at family alpha 0.05; intervals are unadjusted",
    "direction": "a Holm rejection counts as evidence only with theta_hat > 0",
    "power": "MDE_80 = (z_{1-alpha/2} + z_0.80) * SE_bootstrap at alpha 0.05/3 and 0.05, reported with every result",
    "halves": "first ceil(n/2) IC dates vs the rest",
    "placebo": {"threshold": "theta_17 > numpy.quantile(null_17, 0.95, method='linear')",
                "p_empirical": "(1 + #{null_17 >= theta_17}) / 51", "holm": "not applied"},
    "family_A": "same bootstrap on A's 5-seed mean daily IC and Top-3 excess; basic 95% intervals; no Holm",
    "sensitivity": ("experiment_1f_gate_b.sensitivity_report: robust_lag0, robust_all_variant, "
                    "robust_timing_intersection and exploratory h2..h5 summaries (5-seed mean daily IC delta, or A's "
                    "mean IC; block 10; basic interval and p; bootstrap stream 100 + position in sensitivity_specs; "
                    "no Holm; H1 pairs add Top-3 deltas under every view) plus confirmatory Top-3 deltas under every "
                    "view; never decides graduation"),
}

EXECUTABLE = {
    "name": "TOP-3 ATTEMPTED-H1 EXECUTION",
    "timeline": "select at EOD(T); attempt entry at open(T+1); attempt exit at open(T+2); long only",
    "board_regime": ("the admitted data (OHLCV, broker flows, universe) carries no point-in-time board or price-limit "
                     "regime; no rule claims that a bar was legally limit-locked"),
    "entry": {"valid": "gap_1 non-null (Gate A: contiguous T+1 session, close step in band, usable open(T+1))",
              "SINGLE_PRICE_UP_ENTRY": ("T+1 open == high == low and open > close(T) -> unfilled, cash; a "
                                        "conservative OHLC assumption"),
              "no_backfill": "an unfilled pick is never replaced by a lower-ranked name"},
    "exit": {"usable_open": ("price_audit._open_anchor_valid on the Gate-A panel, reused unchanged; at T+2 it "
                             "reproduces Gate-A fwd_oo_1 validity exactly (asserted at runtime)"),
             "SINGLE_PRICE_DOWN_EXIT": ("open == high == low and open < close of the ticker's previous session -> "
                                        "not executable on that session; a conservative OHLC assumption"),
             "path_integrity": ("every close-to-close step between the entry session and the exit's previous "
                                "session lies inside the Gate-A ARA/ARB band (the _step_valid band without its "
                                "contiguity requirement)"),
             "HOLD_THROUGH_search": ("ticker sessions after T+1 in calendar order (missing sessions are skipped); "
                                     "at each session: path break -> UNRESOLVED; unusable open -> next session; "
                                     "SINGLE_PRICE_DOWN_EXIT -> next session; otherwise exit at that open; running "
                                     "out of sessions -> UNRESOLVED"),
             "holding_sessions": "global market-calendar sessions from the entry session T+1 to the exit session",
             "EXIT_H1_identity": "an exit at calendar session T+2 returns exactly fwd_oo_1 (asserted)"},
    "statuses": {"UNFILLED_NO_ENTRY": "cash 0", "UNFILLED_SINGLE_PRICE_UP": "cash 0",
                 "EXIT_H1": "fwd_oo_1", "EXIT_HOLD_THROUGH": "open(exit)/open(T+1) - 1, variable holding period",
                 "UNRESOLVED": ("cash 0, counted; no_later_usable_open includes right-censoring at the end of the "
                                "export")},
    "views": {"HOLD_THROUGH": "PRIMARY EXECUTION PROXY; not a pure H1 return for delayed exits",
              "CASH": "CONSERVATIVE SENSITIVITY; EXIT_HOLD_THROUGH and UNRESOLVED return 0",
              "EXCLUDED": "DIAGNOSTIC ONLY; EXIT_HOLD_THROUGH and UNRESOLVED dropped; never decides graduation",
              "OHLC_OPTIMISTIC": "SENSITIVITY; single-price bars block neither entry nor exit"},
    "eligible_set": "the analysis sample's keys on date T, fixed at EOD(T), no label requirement",
    "selection": "3 highest scores; ties by ticker ascending",
    "portfolio": "equal ex-ante weight 1/3 per slot; unfilled or missing slots stay in cash (return 0)",
    "benchmark": ("all eligible keys on T, equal ex-ante weight; unfilled entries keep their weight in cash; the same "
                  "view as the portfolio; no renormalisation (EXCLUDED diagnostic excepted)"),
    "daily_excess": "Top-3 portfolio return - benchmark return, same view",
    "pairwise_top3_delta": ("mean over the pair's executable test dates of the 5-seed mean of "
                            "[daily excess(larger) - daily excess(smaller)], lag1, pair sample; the benchmark cancels"),
    "diagnostic_function": "experiment_1f_evaluation.execution_diagnostics (model-free; rows whose calendar T+2 exists)",
    "counts_h1_evaluable": {
        "rows": 67366, "EXIT_H1": 66041, "UNFILLED_SINGLE_PRICE_UP": 732, "EXIT_HOLD_THROUGH": 400,
        "UNFILLED_NO_ENTRY": 183, "UNRESOLVED": 10,
        "hold_through_delay_reason": {"t2_single_price_down": 280, "t2_missing": 120, "t2_open_unusable": 0},
        "unresolved_reason": {"path_break": 6, "no_later_usable_open": 4},
        "blocked_single_price_down_exit_attempts": 402, "positions_with_a_blocked_exit_attempt": 296,
        "unusable_open_exit_attempts": 6,
        "ohlc_optimistic": {"EXIT_H1": 67034, "EXIT_HOLD_THROUGH": 138, "UNFILLED_NO_ENTRY": 183,
                            "UNFILLED_SINGLE_PRICE_UP": 0, "UNRESOLVED": 11}},
    "holding_sessions_hold_through": {
        "n": 400, "p50": 2.0, "p90": 6.1, "p99": 14.01, "max": 36.0,
        "distribution": {"2": 284, "3": 50, "4": 12, "5": 7, "6": 7, "7": 6, "8": 9, "9": 5, "10": 5, "11": 5,
                         "12": 3, "13": 2, "14": 1, "15": 1, "17": 1, "23": 1, "36": 1}},
    "diagnostics_price_level_proximity_only": {
        "statement": "proximity to a price level is descriptive and is not evidence of board membership",
        "entry_single_price_up": {"near_10pct_level": 607, "near_ara_level": 44, "other": 81},
        "t2_blocked_single_price_down_exit": {"near_minus_10pct_level": 167, "near_arb_level": 36, "other": 77}},
    "superseded_v1_rule": "651 cap-classified locked entries, no exit blocking, next-row-only hold",
    "reported": ["status counts inside Top-3 and top decile per variant", "holding-period distribution per variant",
                 "gross only"],
}

STAGE1_PLAN = {
    "function": "experiment_1f_gate_b.stage1_plan", "digest": "9d33f74388cc7e22c2bc53caa9044f8ebf74805ebcf19bbfccfbea258756bbfd",
    "categories": {
        "confirmatory": "lag1 h1: A on A_full; A,B on AB; B,C on BC; C,D on CD; 5 seeds",
        "placebo": "lag1 h1 seed 17: B_alignment on AB (B), C_identity on BC (C), D_state on CD (D); 50 draws each",
        "robust_lag0": "lag0 h1: the three pairs on their lag0 samples; 5 seeds",
        "robust_all_variant": "lag1 h1: A,B,C,D on common_ABCD; 5 seeds",
        "robust_timing_intersection": "lag0 and lag1 h1: the three pairs on the timing-arm intersection keys; 5 seeds",
        "exploratory": "lag1 h2..h5: A_full and the three pairs; 5 seeds"},
    "totals": {"models": 435, "fits": 11810},
    "rows": "FIT and EVAL rows need a rank label; TEST rows are every sample key on the test dates",
    "prediction": "xgboost sklearn predict (early-stopped best iteration)",
    "prediction_ledger_columns": ["job_id", "category", "arm", "sample", "variant", "placebo_family", "placebo_draw",
                                  "seed", "horizon", "fold", "best_iteration", "fit_rows", "eval_rows", "test_rows",
                                  "feature_count", "date", "ticker", "prediction"],
    "reports": {"graduation": "experiment_1f_gate_b.graduation_report",
                "sensitivity": "experiment_1f_gate_b.sensitivity_report (never decides graduation)",
                "execution": "status counts and holding sessions inside Top-3 and top decile per H1 job"},
    "outputs": "backtest_out/experiment_1f_candidate/gate_b/stage1_<head12>/ (new directory, never overwritten)",
}

EXECUTION_IDENTITY = {
    "manifest": "backtest_out/experiment_1f_candidate/gate_b/gate_b_execution_manifest_v1.json (gitignored, never overwritten)",
    "established": "after the Gate-B code checkpoint commit, before any fit",
    "execution_files": ["experiment_1f_features.py", "experiment_1f_evaluation.py", "experiment_1f_gate_b_contract.py",
                        "experiment_1f_gate_b.py", "experiment_1f_gate_b_preparation_pins.json"],
    "helper_files": ["walk_forward_backtest.py", "price_audit.py", "normalize_market_data.py",
                     "experiment_1f_universe_gate.py", "signal_metrics.py"],
    "identity": "Git-content SHA-256 at the committed HEAD plus a worktree-matches-HEAD check",
    "local_module_closure": "every repo-local module loaded by the runner must be an execution or helper file",
    "fail_closed_checks": ["manifest exists", "HEAD equals the committed HEAD", "tracked tree clean",
                           "execution and helper code identities", "local module closure", "contract canonical hash",
                           "READY contract document", "preparation pins", "export raw hashes",
                           "semantic digests and ledgers recomputed", "no pending decisions",
                           "python and package versions", "Stage-1 plan digest"],
    "re_checked_before_every_fit": ["HEAD", "tracked tree clean", "manifest bytes", "local module closure"],
    "on_mismatch": "HARD STOP; no repair, no re-establishment, no fallback dataset",
}

PIT_ELIGIBILITY_SENSITIVITY = {
    "rule": ">= 20 market sessions since the ticker's first axis session, measured at T",
    "liquidity_floor": "none (dropped); trailing ADV reported descriptively only",
    "does_not_repair": "survivorship from the post-sample frozen universe",
}

GRADUATION = {
    "execution_integrity": "PASS iff every contract invariant held; says nothing about alpha",
    "increment_conditions_lag1_h1": {
        "G1": "theta_hat > 0 and Holm rejects the family hypothesis",
        "G2": "theta_17 > 95th percentile of the family's 50-draw seed-17 placebo null",
        "G3": "all five per-seed mean deltas > 0",
        "G4": "primary series mean > 0 in both halves",
        "G5": "pairwise Top-3 delta >= 0 under HOLD_THROUGH and >= 0 under CASH"},
    "increment_outcomes": {
        "REJECT_FOR_NOW": "theta_hat <= 0 OR theta_17 <= placebo-null median",
        "GRADUATE_TOWARD_SPECTRA": "not REJECT and G1..G5 all hold",
        "SENSITIVITY_ONLY": "theta_hat > 0 AND theta_17 > placebo-null median AND a G condition fails"},
    "median_comparison_basis": ("theta_17 vs the seed-17 placebo median, never the 5-seed theta_hat against a "
                                "seed-17 null"),
    "A_outcomes": {
        "GRADUATE_TOWARD_SPECTRA": ("basic 95% IC interval > 0 AND basic 95% HOLD_THROUGH Top-3 excess interval > 0 "
                                    "AND IC and Top-3 positive in all five seeds and both halves"),
        "REJECT_FOR_NOW": "5-seed mean daily IC <= 0", "SENSITIVITY_ONLY": "otherwise"},
    "exclusivity": "outcomes are mutually exclusive and exhaustive (tested)",
    "wording": "failure to graduate is never reported as proof of zero effect; MDE is always reported",
}

LIMITATIONS = [
    "historical broker publication/capture timing is not established (neither arm is PIT-proven)",
    "survivorship: the universe list post-dates the sample",
    "roughly one market regime; no generalisation claim",
    "no transaction costs", "no market impact", "no capacity analysis", "no causal attribution",
    "no true ownership inventory",
    "vendor price history is a retrospective restatement",
    "broker basis regimes are inferred retrospectively",
    "single-price entry/exit blocking is a conservative OHLC assumption, not a legal price-limit determination",
    "HOLD_THROUGH censored exits have a variable holding period",
]

PENDING_DECISIONS = {}


# ── contract document ──────────────────────────────────────────────────────
def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_pins(path=None):
    with open(path or PINS_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def contract_body():
    return {
        "contract_version": CONTRACT_VERSION,
        "status": DRAFT if PENDING_DECISIONS else READY,
        "provenance": PROVENANCE,
        "status_meaning": ("frozen before any Gate-B model result; not a scientific pass"),
        "pending_decisions": PENDING_DECISIONS,
        "research_question": RESEARCH_QUESTION,
        "gate_a": GATE_A, "universe": UNIVERSE, "export": EXPORT, "timing": TIMING, "label": LABEL,
        "model": MODEL, "evaluation": EVALUATION, "broker_timing_arms": BROKER_TIMING_ARMS,
        "validity_rules": VALIDITY_RULES, "families": FAMILIES, "samples": SAMPLES, "splits": SPLITS,
        "seeds": SEEDS, "placebos": PLACEBOS, "statistics": STATISTICS, "executable": EXECUTABLE,
        "pit_eligibility_sensitivity": PIT_ELIGIBILITY_SENSITIVITY, "graduation": GRADUATION,
        "limitations": LIMITATIONS,
        "stage1_plan": STAGE1_PLAN, "execution_identity": EXECUTION_IDENTITY,
        "preparation_pins": {"file": os.path.basename(PINS_FILE), "sha256": file_sha256(PINS_FILE),
                             "content": load_pins()},
    }


def contract_sha256(body=None):
    return hashlib.sha256(canonical_json(body or contract_body()).encode("ascii")).hexdigest()


class ContractNotFrozen(RuntimeError):
    pass


def assert_frozen(body=None):
    """The Stage-1 runner calls this before its first fit."""
    body = body or contract_body()
    if body["pending_decisions"] or body["status"] != READY:
        raise ContractNotFrozen(f"Gate-B contract is {body['status']}; pending: {sorted(body['pending_decisions'])}")
    return contract_sha256(body)


# ── verification ───────────────────────────────────────────────────────────
def verify_file_pins(pins, directory):
    failures = []
    for name, expected in sorted(pins.items()):
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            failures.append(f"missing {name}")
        elif file_sha256(path) != expected:
            failures.append(f"sha256 mismatch {name}")
    return failures


def verify_logical_digests(digests, directory, digest_fn):
    import pandas as pd
    failures, frames = [], {}
    for label, spec in sorted(digests.items()):
        if spec.get("kind") not in (None, "frame_digest"):
            continue
        if spec["file"] not in frames:
            frames[spec["file"]] = pd.read_parquet(os.path.join(directory, spec["file"]))
        frame = frames[spec["file"]]
        if spec.get("order", "").startswith("stable sort"):
            frame = (frame.sort_values(["date", "ticker", "broker_code"], kind="mergesort")[spec["columns"]]
                     .astype(spec["dtypes"]).reset_index(drop=True))
        actual = digest_fn(frame, spec["columns"])
        if actual != spec["value"]:
            failures.append(f"{label} mismatch: {actual} != {spec['value']}")
    return failures, frames


def _sha_lines(lines):
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def verify_inputs(recompute_logical=True, recompute_ledgers=False, root=HERE):
    """Re-derive boundary pins from bytes. Returns failure strings (empty = pass)."""
    failures = []
    cand = os.path.join(root, "backtest_out", "experiment_1f_candidate")
    failures += verify_file_pins({GATE_A["manifest"]["path"]: GATE_A["manifest"]["sha256"],
                                  GATE_A["report"]["path"]: GATE_A["report"]["sha256"]}, cand)
    export_dir = os.path.join(cand, "gate_b_inputs")
    failures += verify_file_pins({n: s["sha256"] for n, s in EXPORT["files"].items()}, export_dir)
    failures += verify_file_pins({"export_manifest.json": EXPORT["export_manifest_sha256"]}, export_dir)
    if failures:
        return failures
    report = json.load(open(os.path.join(cand, GATE_A["report"]["path"]), encoding="utf-8"))
    if report["panel_digest"] != GATE_A["panel_digest"] or report["broker_digest"] != GATE_A["broker_digest"]:
        failures.append("Gate-A report digests differ from the contract")
    keys = report["broker_basis_validity"]["invalid_keys"]
    d = EXPORT["digests"]
    if _sha_lines(keys) != d["basis_invalid_sessions_as_published"]["value"]:
        failures.append("published invalid_keys differ from the contract")
    sessions = sorted(f"{k.split(' ', 1)[1]}|{k.split(' ', 1)[0]}" for k in keys)
    if _sha_lines(sessions) != d["basis_invalid_sessions"]["value"]:
        failures.append("basis-invalid session digest mismatch")
    if subprocess.run(["git", "merge-base", "--is-ancestor", GATE_A["frozen_commit"], "HEAD"],
                      cwd=root, capture_output=True).returncode != 0:
        failures.append("HEAD does not descend from the frozen Gate-A commit")
    if recompute_logical:
        import pandas as pd
        sys.path.insert(0, root)
        from experiment_1f_universe_gate import frame_digest
        logical_failures, frames = verify_logical_digests(d, export_dir, frame_digest)
        failures += logical_failures
        broker = frames["gate_a_broker.parquet"]
        if _sha_lines(sorted(broker["broker_code"].unique())) != d["broker_code_vocabulary"]["value"]:
            failures.append("broker code vocabulary differs")
        src = pd.read_parquet(os.path.join(cand, "broker_daily_v2.parquet"), columns=["date", "ticker", "broker"])
        src = src.rename(columns={"broker": "broker_code"})
        merged = src.merge(broker[["date", "ticker", "broker_code"]], how="left", indicator=True)
        excluded = merged[merged["_merge"] == "left_only"]
        if _sha_lines(sorted(excluded["date"] + "|" + excluded["ticker"] + "|" + excluded["broker_code"])) \
                != d["basis_quarantined_broker_rows"]["value"]:
            failures.append("basis-quarantined broker-row digest mismatch")
    if recompute_ledgers:
        import experiment_1f_features as features
        if features.preparation_ledgers(features.load_inputs(export_dir)) != load_pins():
            failures.append("recomputed feature/sample/split/label ledgers differ from the pins")
    return failures


def write_contract(directory=CONTRACT_DIR):
    """Write the contract document once; an existing document is never changed (identical rewrite is a no-op)."""
    body = contract_body()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{CONTRACT_VERSION}_{body['status'].lower()}.json")
    text = json.dumps(dict(body, contract_sha256=contract_sha256(body)), indent=1, sort_keys=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            if fh.read() != text:
                raise ContractNotFrozen(f"{path} exists with different content; contract documents are immutable")
        return path
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--verify-ledgers", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    body = contract_body()
    print(f"{CONTRACT_VERSION} status={body['status']} sha256={contract_sha256(body)}")
    if args.verify or args.verify_ledgers:
        failures = verify_inputs(recompute_ledgers=args.verify_ledgers)
        print("boundary verification:", "PASS" if not failures else "FAIL")
        for failure in failures:
            print("  -", failure)
        if failures:
            return 1
    if args.write:
        path = write_contract()
        print(f"written {path} file_sha256={file_sha256(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
