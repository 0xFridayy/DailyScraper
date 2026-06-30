# IDX Trading ML System

> Single source of truth for the trading system. Version-controlled — update this
> file whenever architecture, parameters, or decisions change.

## Architecture

- **Layer 1: XGBoost** — pattern detection (which setups are worth taking)
- **Layer 2: DDQN** — position management (entry/exit/sizing once in a trade)

### Data sources
- **NeoBDM scraper** (`neobdm_scraper.py`) — Market Summary + Broker Stalker.
  Playwright + Chromium. Runs daily at 07:00 Asia/Kuala_Lumpur via GitHub Actions
  (`.github/workflows/daily-scrape.yml`) and pushes results to Telegram.
  Supports on-demand runs via the Telegram `/scrape` command and the Actions
  "Run workflow" button.

## Roadmap

- [x] **#1: NeoBDM reverse-engineer** — scraper live, scheduled, sending to Telegram
- [ ] **#2: XGBoost walk-forward backtest**
- [ ] **#3: SHAP feature importance**
- [ ] **#4: Kelly criterion sizing**
- [ ] **#5: DDQN position management (Layer 2)**
- [ ] **#6: Paper trading / live validation**
- [ ] **#7: Live deployment**

## Key decisions

- Two-layer design: separate "what to trade" (XGBoost) from "how to manage it" (DDQN).
- Scraper runs in the cloud (GitHub Actions), not on a personal laptop — no machine
  needs to be on for the daily data pull.
- Secrets (NeoBDM login, Telegram token/chat) live as GitHub Actions secrets and a
  local git-ignored `.env`; never committed.

## XGBoost configuration (Last updated: 2026-06-28)

```
max_depth=4
learning_rate=0.05
# ... add the rest as you tune
```

## Parameter tuning journal

| Date | Change | Reason / result |
|------|--------|-----------------|
| 2026-06-28 | Initial XGBoost params (depth=4, lr=0.05) | Baseline |

## Backtest results

```
Date: 2026-06-28
Train Sharpe: 1.62
Val Sharpe:   1.41
→ Not overfitting ✓
```
