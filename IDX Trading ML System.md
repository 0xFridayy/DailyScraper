# IDX Trading ML System

   ## Overview
   Two-layer approach: Layer 1 (XGBoost) validates edge, Layer 2 (DDQN) optimizes execution.

   ## Layer 1: XGBoost
   - **Purpose:** Detect hidden broker accumulation patterns (BRPT scenario)
   - **Input:** NeoBDM broker flow features (broker_concentration, retail_pct, etc.)
   - **Output:** GO/CAUTION/WATCH signal + confidence score
   - **Timeline:** Roadmap #2-5 (4 weeks starting June 28)
   - **Success metric:** Sharpe > 1.5, broker_concentration in Top 3 feature importance

   ## Layer 2: DDQN
   - **Purpose:** Learn optimal position sizing & add/reduce logic
   - **Timeline:** Phase 5+ (after Layer 1 validated + 50+ live trades)
   - **Triggers:** Layer 1 Sharpe > 1.5 sustained for 8+ weeks

   ## Current Phase: Roadmap #2 - XGBoost Walk-Forward Backtest

   ### XGBoost Config (v1, 2026-06-28)
```python
   max_depth=4
   learning_rate=0.05
   n_estimators=100
   subsample=0.8
   colsample_bytree=0.8
   reg_lambda=1.0
   early_stopping_rounds=10
```

   ### Data Split
   - Train: 60% (18 days)
   - Val: 20% (6 days)
   - Test: 20% (6 days)

   ### Success Criteria
   - Train MAE ≈ Val MAE (gap < 0.5%)
   - Val Sharpe > 1.5

   ## Roadmap
   - [ ] #1: NeoBDM collector (done ✓)
   - [ ] #2: XGBoost walk-forward backtest (IN PROGRESS)
   - [ ] #3: SHAP feature importance
   - [ ] #4: Kelly criterion sizing
   - [ ] #5: Live trading (50+ trades)
   - [ ] #6: DDQN backtester
   - [ ] #7: Hybrid deployment

   ## Backtest Results Log
   (update after each run)
   - 2026-06-28 v1: TBD

   ## Key Decisions
   - Why Layer 1 before Layer 2? (bandarmology is pattern recognition, XGBoost handles it; DDQN overkill until edge validated)
   - Why conservative XGBoost tuning? (30 days data = small; risk of overfitting high)