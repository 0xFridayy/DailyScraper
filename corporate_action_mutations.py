"""Explicit source mutations for the independent F01-F18 regression witnesses.

Each replacement removes a production contract decision. The runner checks a
passing unchanged witness before requiring an assertion failure from its mutant.
"""

FINDING_MUTANTS = [
    ("F01-invalid-registry-becomes-empty", "price_contract.py",
     '        if (not isinstance(doc, dict)',
     '        if isinstance(doc, dict) and not isinstance(doc.get("events"), list):\n'
     '            doc = dict(doc, events=[])\n'
     '        if (not isinstance(doc, dict)',
     "test_corporate_action_findings.py::test_f01_invalid_collection_is_not_an_empty_registry"),
    ("F02-malformed-evidence-authorizes-reference", "price_contract.py",
     '            _event_evidence(r)', '            pass  # unchecked provenance',
     "test_corporate_action_findings.py::test_f02_incomplete_provenance_cannot_authorize_reference"),
    ("F02-placeholder-provenance-accepted", "price_contract.py",
     '    return not placeholder_text(value) and _PLACEHOLDER_WORD.search(value.upper()) is None',
     '    return _text(value)',
     "test_corporate_action_findings.py::test_f02_placeholder_provenance_cannot_confirm_reference"),
    ("F02-degenerate-evidence-hash-accepted", "price_contract.py",
     '            and value != EMPTY_CONTENT_SHA256 and len(set(value)) >= 8\n'
     '            and not any(value == value[:size] * (64 // size) for size in (1, 2, 4, 8, 16, 32)))',
     '            )',
     "test_corporate_action_findings.py::test_f02_placeholder_provenance_cannot_confirm_reference"),
    ("F02-contradictory-chronology-accepted", "price_contract.py",
     '        canonical_session(source["published_on"])\n    _coherent_chronology(record)',
     '        canonical_session(source["published_on"])',
     "test_corporate_action_findings.py::test_f02_incoherent_chronology_cannot_confirm_reference"),
    ("F08-placeholder-session-evidence-verifies", "neobdm_source_contract.py",
     'and meaningful_sha256(value.get("sha256")))',
     'and isinstance(value.get("sha256"), str))',
     "test_corporate_action_findings.py::test_f08_placeholder_session_evidence_does_not_verify"),
    ("F03-stale-labels-recertified-by-annotation", "price_contract_frame.py",
     '    if existing or labels:', '    if False:',
     "test_corporate_action_findings.py::test_f03_annotation_does_not_certify_legacy_crossing_labels"),
    ("F04-edited-values-retain-certificate", "price_contract_frame.py",
     '    if list(px[CERTIFICATE_COLUMN]) != _row_certificates(px, identity):',
     '    if False:',
     "test_corporate_action_findings.py::test_f04_certificate_is_bound_to_observations_and_outputs"),
    ("F05-incomplete-bars-get-manufactured-extrema-anchors", "price_contract_frame.py",
     '    missing = set(SOURCE_COLUMNS) - set(px.columns)',
     '    for missing_anchor in ("high", "low"):\n'
     '        if missing_anchor not in px:\n'
     '            px[missing_anchor] = (px[["open", "close"]].max(axis=1) if missing_anchor == "high"\n'
     '                                  else px[["open", "close"]].min(axis=1))\n'
     '    missing = set(SOURCE_COLUMNS) - set(px.columns)',
     "test_corporate_action_mutation_witnesses.py::test_f05_missing_actual_extrema_anchors_refuse"),
    ("F06-writer-preserves-close-only-revisions", "backfill_inventory.py",
     '                          for field in ("open", "high", "low", "close", "volume"))}',
     '                          for field in ("close",))}',
     "test_corporate_action_findings.py::test_f06_unchanged_close_does_not_grandfather_changed_fields"),
    ("F07-unresolved-event-becomes-trusted-predecessor", "price_contract.py",
     '            if ref.status != "RESOLVED":\n'
     '                reason = "UNRESOLVED_EVENT_REFERENCE"\n'
     '            elif not complete_in_band:',
     '            if ref.status == "RESOLVED" and not complete_in_band:',
     "test_corporate_action_findings.py::test_f07_pending_event_cannot_back_ordinary_session_or_labels"),
    ("F08-unknown-source-gets-event-exception", "price_contract.py",
     'RAW_ACTUAL if representation == "UNKNOWN" and not events else representation',
     'RAW_ACTUAL if representation == "UNKNOWN" else representation',
     "test_corporate_action_findings.py::test_f08_unknown_event_input_cannot_receive_official_admission"),
    ("F09-rejected-payoff-endpoint-used", "strategy_variants.py",
     '        if not day["price_step_admissible"]:', '        if False:',
     "test_corporate_action_findings.py::test_f09_rejected_same_session_close_cannot_be_payoff",
     (('    if not g.loc[i0, "next_entry_open_admissible"]:', '    if False:'),)),
    ("F10-truncated-horizon-is-shortened", "strategy_variants.py",
     '        if i0 + k >= len(g):\n'
     '            g.attrs["trade_withheld_reason"] = "INCOMPLETE_HORIZON"\n'
     '            return None',
     '        if i0 + k >= len(g):\n'
     '            return (g.iloc[-1]["close"] - entry_price) / entry_price',
     "test_corporate_action_findings.py::test_f10_incomplete_timed_hold_is_not_shortened"),
    ("F11-direct-ranking-bypasses-contract", "daily_picks.py",
     '    refuse_unmigrated("daily_picks.rank_picks")', '    pass',
     "test_corporate_action_mutation_witnesses.py::test_f11_direct_ranking_refuses_valid_unversioned_candidates"),
    ("F12-wrapper-writes-before-refusing", "horizon_scan.py",
     '    refuse_unmigrated("horizon_scan.__main__")',
     '    sqlite3.connect(DB_PATH).close()\n'
     '    refuse_unmigrated("horizon_scan.__main__")',
     "test_corporate_action_findings.py::test_f12_cli_refusal_creates_no_database[horizon_scan]"),
    ("F13-recovery-hides-invalid-successor", "price_audit.py",
     '    audited = detect(px, trusted=keep, registry=registry, representation=representation)',
     '    pass  # retain adjudication from before event recovery',
     "test_corporate_action_mutation_witnesses.py::test_f13_recovery_does_not_hide_bad_successor"),
    ("F14-duplicate-identity-becomes-baseline", "price_audit.py",
     '    # Independent defects invalidate baselines before chaining references.',
     '    px = px.drop_duplicates(["ticker", "date"], keep="last")\n'
     '    # Independent defects invalidate baselines before chaining references.',
     "test_corporate_action_findings.py::test_f14_duplicate_identity_is_not_a_predecessor"),
    ("F15-removed-event-row-merges-regimes", "price_contract_frame.py",
     '    px["price_segment_id"] = [sum(e.session <= d for e in registry.matching(t, market))\n'
     '                              for t, d in zip(px.ticker, px.date)]',
     '    px["price_segment_id"] = px.groupby("ticker")["corporate_action_boundary"].cumsum()',
     "test_corporate_action_findings.py::test_f15_missing_event_row_does_not_join_median_segments"),
    ("F16-integrity-trusts-producer-reasons", "check_signal_integrity.py",
     '        if ordered.loc[pd.Series(illegal, index=ordered.index), column].notna().any():',
     '        if (column + "_reason" in ordered and\n'
     '                ordered.loc[ordered[column + "_reason"].ne(""), column].notna().any()):',
     "test_corporate_action_findings.py::test_f16_integrity_checks_actual_spans_with_blank_producer_reasons"),
    ("F17-ancient-unresolved-row-blocks-local-trade", "ara_arb_simulation.py",
     '    i0 = idx_map[entry_date]',
     '    if g["at_ara"].isna().any() or g["at_arb"].isna().any():\n'
     '        return None\n'
     '    i0 = idx_map[entry_date]',
     "test_corporate_action_findings.py::test_f17_unrelated_history_does_not_disable_valid_local_trade"),
    ("F18-first-complete-extrema-window-shifted-away", "price_audit.py",
     's.rolling(h, min_periods=h).max().shift(-h)',
     's.rolling(h, min_periods=h).max().shift(-h).shift(1)',
     "test_corporate_action_findings.py::test_f18_first_complete_extrema_window_and_incomplete_tail"),
]

# Evidence-restart contract (price_contract.adjudicate_series). Each mutant
# restores one rejected trust rule; its witness must fail on an assertion.
RESTART_MUTANTS = [
    ("R1-once-invalid-always-invalid", "price_contract.py",
     '        else:\n            streak = 1\n',
     '        else:\n            streak = 0 if last is not None else 1\n',
     "test_corporate_action_restart.py::test_w1_unsupported_history_restarts_only_on_a_complete_2026_window"),
    ("R2-trust-first-row-of-supplied-slice", "price_contract.py",
     'or (linked and prior.trusted))',
     'or (linked and prior.trusted) or last is None)',
     "test_corporate_action_restart.py::test_first_row_of_any_slice_is_never_positionally_trusted"),
    ("R3-two-good-looking-bars-restart", "price_contract.py",
     'RESTART_SESSIONS = 10', 'RESTART_SESSIONS = 2',
     "test_corporate_action_restart.py::test_w3_evidence_free_short_run_cannot_bootstrap_trust"),
    ("R4-unbounded-recursive-admissibility", "price_contract.py",
     'prior is not None and prior.local_ok and domain:',
     'prior is not None and prior.admissible and domain:',
     "test_corporate_action_restart.py::test_w7_alternating_discontinuities_keep_a_bounded_dependency",
     (('consistency == "OUT_OF_BAND" and not prior.discontinuous', 'consistency == "OUT_OF_BAND"'),)),
    ("R5-truncated-series-context-enters-restart", "price_contract_frame.py",
     'context = groups.cumcount().ge(SERIES_CONTEXT_ROWS)', 'context = groups.cumcount().ge(0)',
     "test_corporate_action_restart.py::test_w7_truncated_series_break_context_cannot_widen_trust"),
]
