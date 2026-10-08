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
     '                      for field in ("open", "high", "low", "close", "volume"))}',
     '                      for field in ("close",))}',
     "test_corporate_action_findings.py::test_f06_unchanged_close_does_not_grandfather_changed_fields"),
    ("F07-unresolved-event-becomes-trusted-predecessor", "price_contract.py",
     '            or not event and admission.status == "UNRESOLVED"\n'
     '            and admission.reason == "MISSING_PREDECESSOR"))',
     '            or admission.status == "UNRESOLVED"))',
     "test_corporate_action_findings.py::test_f07_pending_event_cannot_back_ordinary_session_or_labels"),
    ("F08-unknown-source-gets-event-exception", "price_contract_frame.py",
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
