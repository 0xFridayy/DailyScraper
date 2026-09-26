# Broker Learning Dashboard — spec and preregistration (ruleset v1)

> **Ringkas (ID):** dashboard HTML harian untuk watchlist, dibangun dari
> `/api/inventory` NeoBDM (semua 101 broker). Untuk tiap broker dihitung posisi,
> modal rata-rata, dan untung/rugi (realized + unrealized). Enam aturan sinyal
> yang dikunci di sini (termasuk R6 "akumulasi diam-diam") dicatat setiap hari,
> lalu dinilai 5/10/20/60 sesi kemudian. Saham yang naik ≥50% dalam 3 bulan
> dikumpulkan sebagai "kasus alpha", lengkap dengan snapshot inventory sebelum
> naik.
> Bobot aturan dan skor track record broker diperbarui mingguan dari data seluruh
> pasar. Semua output **research-grade**. Ini *observable broker inventory*,
> bukan kepemilikan sebenarnya, dan tidak ada yang otomatis dianggap "terbukti".

Status: **research-grade, not production-approved.** Nothing here feeds
`daily_picks.py`, the ARB veto, or any ML experiment.

---

## 0. Authorization record (guardrail exception)

`.clinerules/01-project-guardrails.md` says `observable_inventory` (per-broker
cumulative net lots) "was rejected by Experiment #1 and must not be reintroduced
unless explicitly requested".

On **2026-09-24** the repo owner **explicitly requested and authorized**, in the
Claude Code session that built this module, the following:
- per-broker cumulative net lot;
- the effective / average cost derived from it;
- the mark-to-market trading P/L derived from it.

These may be used in **(a)** a descriptive dashboard and **(b)** a prospective /
retrospective *rule-evaluation ledger*, labelled research-grade.

The authorization is scoped as follows:

- It does **not** reinstate `observable_inventory` as a feature in
  Experiment #1/#1E/#1F or any successor ML experiment. Those results are frozen
  and unchanged.
- It does not relax any other guardrail:
  - point-in-time semantics;
  - no Sharpe / `sqrt(252)`;
  - base rate always shown;
  - broker identity stays research-grade;
  - no owner/controller inference;
  - no automatic promotion.
- Every surface that shows these numbers carries the wording **"observable broker
  inventory, not beneficial ownership"** (dashboard: *"inventory broker yang
  terlihat, bukan kepemilikan sebenarnya"*).
- The rules in §4 are **frozen as v1**. Any change to a rule, threshold, horizon
  or metric is a **new ruleset version**. v1 rows are kept, never rewritten.
  "No feature or hyperparameter rescue" (Exp #1E) applies.

---

## 1. Architecture

```
            weekday 10:30 UTC (17:30 WIB)                 Saturday 11:00 UTC
            broker-learning-daily.yml                     broker-learning-weekly.yml
                     │                                             │
 broker_collect.py   ├─ watchlist (~50 tickers, all 101 brokers)   ├─ whole market (all_tickers.json)
                     ▼                                             ▼
 broker_book.py      per-ticker frames → basis flags → avg-cost book + rolling features
 broker_rules.py     ruleset v1 → rule rows (eligible, fired) + broker track-record events
 broker_learning.py  outcomes fwd_oo_{5,10,20} → vol-neutral excess → stats / weights / scores
 broker_learning_db.py                 broker_learning.db  (small, committed by CI)
                     │                                             │
 broker_dashboard.py ├─ render HTML → Telegram sendDocument        └─ retro stats, weights,
                     │  (private chat), never committed               broker scores, profitability
 broker_learning_run.py = CLI orchestrator:  daily | weekly | render
```

Everything except `broker_collect.collect()` is pure pandas/numpy. It must import
without Playwright, NeoBDM or Telegram secrets, so `check_ml_health.py` can
import and test it.

### Files

| File | Role |
|---|---|
| `broker_collect.py` | Fetch `/api/inventory` (login via `neobdm_scraper.login`, lazily imported), validate, cache to `broker_learning_raw/<mode>/<T>.json.gz` (gitignored) |
| `broker_book.py` | Payload → frames; basis-break flags; anchored average-cost book; rolling per-ticker-day features |
| `broker_rules.py` | Frozen ruleset v1 constants and rule evaluation; broker track-record events |
| `broker_learning.py` | Outcomes, vol-neutral excess, block bootstrap, rule stats, weights, broker scores, broker profitability |
| `broker_learning_db.py` | SQLite schema and I/O for `broker_learning.db` |
| `broker_dashboard.py` | Static self-contained HTML render; Telegram `sendDocument` |
| `broker_learning_run.py` | `daily` / `weekly` / `render` orchestration |
| `broker_watchlist.json` | Daily watchlist (edit freely; not part of the frozen ruleset) |
| `test_broker_*.py` | Assert-script tests (repo convention: no pytest; print `  ok <name>`) |
| `.github/workflows/broker-learning-{daily,weekly}.yml` | Schedules |

---

## 2. Data semantics

### 2.1 Source

The source is `GET https://neobdm.tech/api/inventory` with these parameters:
- `symbol=T`;
- all 101 codes from `broker_codes.json`, sent as **repeated** `brokers=` params;
- `start_date = today(UTC) − 360 days` (`harvest_inventory.start_date()`);
- `end_date = today`;
- `investor_type=A`.

**Response:** `data.date[]`, plus a per-broker dict for each of
`blot`/`slot`/`nlot` (lots) and `bval`/`sval`/`nval` (full Rupiah). Every value
is **daily, not cumulative**. `ohlc[]` holds `{date, open, high, low, close,
volume}`.

**Normalization:** `build_inventory_db.strict_ticker_frame(data, ticker)`
produces the broker frame (`date,ticker,broker,nlot,nval,blot,bval,slot,sval`).
It fails closed and drops all-zero rows, so a missing row means **zero trades
that day as asserted by the source**. That holds because every code was
requested.

1 lot = 100 shares.
- BAVG = `bval/(blot·100)`
- SAVG = `sval/(slot·100)`

### 2.2 Validation per fetched ticker (collector)

A fetch is rejected (the ticker fails for this run) if any of these holds:
- `success` is false;
- `meta.symbol` is present and ≠ the requested ticker;
- `strict_ticker_frame` raises;
- its OHLC signature (`len, first 3 dates, last 3 dates, first 3 closes, last 3
  closes`) equals that of another ticker already fetched in the same run
  (cross-ticker clone guard).

**Short windows:**
- **Market mode:** if the *first* successfully fetched ticker has fewer than 100
  sessions, the run aborts (`SystemExit`); the rolling window has moved (see
  harvest commit 4b99b18).
- **Watchlist mode:** abort if **≥3 of the first 5** fetched tickers have fewer
  than 100 sessions.

**Failure rate:** the run exits non-zero if the ticker failure rate exceeds 30%
(`price_audit.should_fail_run`). Zero-session tickers (§10 A2, `empty`) are in
the denominator only, but more than 25% of the attempted tickers `empty` also
fails the run (`broker_collect.too_many_empty`), and a failed weekly run writes
no weekly table. An unreadable cache file counts as a failed ticker.

**Payload validation (`broker_book.frames_from_payload`)** refuses, on top of
`strict_ticker_frame`, a negative gross value (`blot`, `slot`, `bval`, `sval`):
a buy or a sell cannot be below zero, and the book would turn it into a silent
NaN. `nlot`/`nval` are signed.

### 2.3 Basis breaks (lots vs price on different share bases)

For each ticker, a date is **basis-flagged** if either of these holds:
- **(a)** it falls inside any regime for that ticker in `observed_basis_factor.json`
  (inclusive `regime_first_date..regime_last_date`, any classification);
- **(b)** the runtime containment check fails. It is evaluated only on days with
  Σblot > 0 and a valid `low`/`high`:
  - the day's market VWAP is `Σ_b bval / (Σ_b blot · 100)`, summing `bval` only over broker-days with `blot > 0` (A3: odd-lot rupiah has no lot to divide by);
  - the check fails when that VWAP is outside `[low·0.99, high·1.01]`.

Consequences:
- The **anchored book** (§3) starts the session after the **last** flagged date.
  If the last session itself is flagged, the ticker is `basis_ok = False`: the
  book is not computed and the ticker is shown with a warning.
- **Rolling features** at T require **no flagged date in the last 80 sessions**
  ending at T (`basis_ok80`).

### 2.4 Point-in-time rules

- Every feature and rule at session T uses only rows with `date ≤ T`, on the
  ticker's own session axis. Rolling windows include T.
- **Decision at EOD(T); entry at open(T+1)** (executable contract, #1E).
- An outcome for (T, h) is **realized at open(T+1+h)**. It may enter any learned
  quantity only when that date is ≤ the last session of the data being used.
  This is automatic here, because outcomes come from the same fetch.
- **Retrospective (weekly, market-wide)** evaluation recomputes v1 over the
  fetched rolling year. Its limits:
  - the vendor may restate history, so it is **not PIT-proven**;
  - it is labelled *retrospective* everywhere.
- **Prospective (daily, watchlist)** evaluation behaves as follows:
  - the ledger records v1 for the latest session only, with `captured_utc`;
  - it is insert-only (`INSERT OR IGNORE`) and never recomputed or backfilled;
  - missed days stay missing.
- Rolling-window features (§4) are **anchor-free**: they depend only on the last
  80 sessions. This makes them identical whether computed live on day T or
  retrospectively from a later fetch, restatements aside.

---

## 3. Anchored average-cost book (dashboard, descriptive)

The book is kept per ticker and per broker, from the anchor (§2.3) to the last
session, and processes days in order.

**Each day `(B, pb, S, ps)`:**
- `B = blot`, `S = slot`
- `pb = bval/(B·100)` if B > 0
- `ps = sval/(S·100)` if S > 0

**1. Intraday round trip.**
- `m = min(B, S)`
- `realized += m·100·(ps − pb)` (only if m > 0)

**2. Residual.** `n = B − S`.
- `n > 0`: buy `n` at `pb`.
- `n < 0`: sell `|n|` at `ps`.

**3. Position update.** State is position `P` (lots, signed), average cost `C`
(Rp/share) and `realized` (Rp).
- **Adding in the same direction** as `P`, or opening from `P = 0`: `C` becomes
  the lot-weighted average of the old `C` and the trade price.
- **Reducing:** close `q = min(|trade|, |P|)` at the trade price.
  - `realized += q·100·(price − C)·sign(P)`
  - Any remainder opens the opposite side at the trade price, so `C` resets on a
    sign flip.
- **`P == 0`:** `C = NaN`.

**Outputs:**
- **Unrealized:** `unrealized = P·100·(close_last − C)`, or 0 when P = 0.
- **Total:** `total = realized + unrealized`.
- **Invariant, tested:** `total == Σnlot·100·close_last − Σnval` over the same
  days, to within 1 Rp per 1e9 of turnover. Mark-to-market P/L is
  path-independent; average cost only splits it into realized and unrealized.
- **Negative `P`:** the broker is a *net seller since the anchor*. That means
  distributing inventory held before the anchor, **not a short**. The dashboard
  labels it that way.

**Per-broker output row:**
- position: `position_lots`, `avg_cost`, `pct_vs_cost = close/avg_cost − 1`;
- P/L: `realized_rp`, `unrealized_rp`, `total_rp`;
- activity: `gross_buy_lots`, `gross_sell_lots`, `turnover_rp = Σ(bval+sval)`,
  `net1d`, `net5d` (lots);
- averages: `bavg20`, `savg20` (VWAPs over the last 20 sessions, NaN if no
  trades), `first_date`.

**Chart series:** cumulative net lots since the anchor, for the top 5 net buyers
and top 5 net sellers by final position, plus close.

---

## 4. Ruleset v1 (FROZEN 2026-09-24)

### 4.1 Rolling features

Features are computed per ticker and session T, on the ticker's own session
axis, over windows ending at T.

**Per broker b:**
- `NL_w`, `NV_w`, `BL_w`, `SL_w` are the rolling sums of `nlot`, `nval`, `blot`,
  `slot` over the last `w` sessions, for w ∈ {5, 60}.

**Ticker level:**
- `ADV20` = mean over the last 20 sessions of `Σ_b blot` (traded lots; immune to
  vendor volume wraps).
- `VAL20` = mean over the last 20 sessions of `Σ_b bval` (Rp).
- `low_w` / `high_w` = min low / max high over the last w sessions.
- `n_sessions` = the ticker's session count up to and including T.

**Window cost of broker b:** `cost_w(b) = NV_w/(NL_w·100)`. It is **valid** only
if all of these hold:
- `NL_w > 0`;
- `SL_w ≤ 0.5·BL_w` (mostly one-directional);
- `low_w ≤ cost_w ≤ high_w`.

**Holder group H:** the top 3 brokers by `NL_60` among those with `NL_60 > 0`.
- `NLH = Σ_H NL_60`, `NVH = Σ_H NV_60`
- `costH = NVH/(NLH·100)`, valid if `NLH > 0` and `low_60 ≤ costH ≤ high_60`.

### 4.2 Eligibility

These rows form the universe for base rates.

`eligible(T)` requires all of:
- `n_sessions ≥ 80`
- `VAL20 ≥ Rp 2e8` (Rp 200 juta/day; amended 2026-09-25, see §10). Most
  multi-baggers start illiquid: of the 17 ten-baggers in the 2025-26 window,
  only 3 traded ≥ Rp 1 bn/day when their run began.
- `ADV20 > 0`
- `close > 0`
- `basis_ok80`

Rules are evaluated only on eligible rows. The universe for a date is the set of
eligible rows that date.

### 4.3 Rules

`dir` is the preregistered expected direction of forward excess return.

| id | dir | name (ID label) | fires when |
|---|---|---|---|
| `R1` | +1 | `acc_near_cost` — *harga dekat modal akumulator besar* | `NLH ≥ 3·ADV20` and costH valid and `−0.02 ≤ close/costH − 1 ≤ 0.05` |
| `R2` | −1 | `winner_distributing` — *akumulator paling untung sedang ambil untung* | Let W = brokers with valid `cost_60` and `NL_60 ≥ 1·ADV20`. Let w = argmax over W of `NL_60·100·(close − cost_60)`. Fires if W is non-empty and `close/cost_60(w) − 1 ≥ 0.20` and `NL_5(w) ≤ −0.20·NL_60(w)` |
| `R3` | −1 | `holders_underwater` — *akumulator besar nyangkut* | `NLH ≥ 3·ADV20` and costH valid and `close/costH − 1 ≤ −0.10` |
| `R4` | +1 | `fresh_accumulation` — *akumulasi baru di harga pasar* | Let f = argmax over b of `NL_5` (ties broken by code). Fires if `NL_5(f) ≥ 1·ADV20` and `SL_5(f) ≤ 0.25·BL_5(f)` and `cost_5(f)` is within `[low_5, high_5]` and `|close/cost_5(f) − 1| ≤ 0.03` |
| `R5` | +1 | `seller_exhaustion` — *penjual besar berhenti jual* | Let S = the bottom 3 brokers by `NL_60` among those with `NL_60 < 0`. Fires if `Σ_S NL_60 ≤ −3·ADV20` and `Σ_S NL_5 ≥ 0` |

| `R6` | +1 | `stealth_accumulation` — *akumulasi diam-diam, harga dibuat sideways* | See §4.3.1. **Primary horizon h = 60** (≈3 months) |

**Composite score** (dashboard ordering only): `score = Σ_r fired_r · dir_r · weight_r`.
Weights come from the latest weekly run, default 1.0.

#### 4.3.1 R6 `stealth_accumulation`

R6 was added 2026-09-25, before any v1 result was observed. It encodes the
owner's thesis: a large buyer collects steadily while price is held sideways,
ahead of a multi-month move.

Let `a` = argmax over b of `NL_60` (ties broken by code). Let `BUYDAYS_60(b)` be
the number of the last 60 sessions with `nlot_b > 0`. R6 fires only if all of
these hold:
- `NL_60(a) ≥ 2·ADV20`;
- `SL_60(a) ≤ 0.4·BL_60(a)`: mostly buying;
- `BUYDAYS_60(a) ≥ 24`: net-bought on at least 40% of sessions, so the position
  was collected steadily rather than in one block;
- `cost_60(a)` is valid (§4.1) and `|close/cost_60(a) − 1| ≤ 0.10`: price is
  still near the collector's cost;
- sideways price: `high_60/low_60 − 1 ≤ 0.30` and `|close/close(T−59) − 1| ≤ 0.10`.

*Planned for a later ruleset, not v1:* a "new holder" condition, where `a` held
~nothing in the 60 sessions before the window. It needs 140+ sessions of history
per ticker, and the rolling-year API cannot provide that yet (see §4.7).

### 4.4 Broker track-record events

These are evaluated on eligible rows. Brokers are ranked by `NL_5`, with ties
broken by code.
- **Buy side:** the top 3 with `NL_5 ≥ 0.5·ADV20` give an event `(T, ticker, broker, side='buy')`.
- **Sell side:** the bottom 3 with `NL_5 ≤ −0.5·ADV20` give an event with `side='sell'`.

### 4.5 Outcomes and metrics

**Forward returns.** `fwd_oo_h` for h ∈ {5, 10, 20, 60} comes from
`price_audit.add_forward_returns(ohlc, all_dates, horizons=(5,10,20,60), open_anchored=True)`.
- `all_dates` is the sorted union of dates across every ticker in the fetch.
- **Primary horizon:** h = 10 for R1–R5; h = 60 for R6. `PRIMARY_H` is per rule.
- **Survivorship caveat:** a window containing a suspension or a corporate action
  is dropped (NaN), not bridged. Some real multi-baggers that were suspended
  mid-run are therefore invisible to the stats. This is logged, not fixed.

**Big-move rate.** For every rule and h:
- `big_rate` = share of events with `fwd_oo_h ≥ BIG[h]`;
- `big_base_rate` = the same share over eligible rows on the same event dates;
- `BIG = {5: 0.15, 10: 0.20, 20: 0.30, 60: 0.50}`.

This answers "of the stocks this rule flagged, how many really took off,
compared with any stock that day?".

*Measured on the 2025-08-22..2026-08-21 cache (for context, not a result):* at
h = 60, 4.2% of valid ticker-days go ≥ +50%, 0.94% go ≥ +100%, and 0.01% go
≥ +300%. The median h = 60 return is −7.0%, and 65.8% of ticker-days are
negative.

**Entry blocked.** If open(T+1) is locked at the limit (`open(T+1) == high(T+1)`
and `open(T+1)/close(T) − 1 ≥ ara_bound(close(T)) − 0.01`), then `fwd_oo_h` is
NaN for all h.

**Volatility bucket.** `rv20(T)` is the std of daily close-to-close returns over
the last 20 sessions on the ticker's own axis (at least 15 valid). The
`rv20_q` quintile (1..5) is computed per date across eligible rows. If a date has
fewer than 25 eligible rows with rv20, a single bucket is used.

**Primary metric, vol-neutral excess.**
`x_h = fwd_oo_h − mean(fwd_oo_h over eligible rows with the same date and the same rv20_q)`.
- Where the watchlist cross-section is too small for quintiles (live ledger), a
  single bucket is used, i.e. excess vs the equal-weight watchlist mean.
  The live ledger stores each row's return only; the excess is computed when
  the ledger is read (`broker_learning_db.live_summary`), over every outcome
  recorded for that session. At h = 60 a ticker whose exit a suspension delays
  is recorded later than the others, and a stored excess would have frozen a
  benchmark without it.

**Per rule and h** (bearish rules are reported raw, with `dir` alongside):
- **Counts:** `n_events` and `n_dates` (distinct T with ≥1 event with a non-NaN
  `x_h`).
- **`mean_excess`:** date-balanced. For each date take the mean `x_h` over its
  events, then average across dates.
- **CI:** moving-block bootstrap over the sorted event dates of the per-date
  means. Block length = h dates, B = 1000, `numpy.random.default_rng(17)`, 2.5 / 97.5
  percentiles.
- **Hit rates:**
  - `hit_rate` = share of events with `fwd_oo_h > 0`;
  - `base_rate` = share of *eligible* rows on the same event dates with
    `fwd_oo_h > 0`;
  - `hit_edge = hit_rate − base_rate` (pooled);
  - `daily_hit_edge` = mean over event dates of `(hit_d − base_d)`, from
    `signal_metrics.date_balanced_hit_edge`.
- **`low_n`:** `n_dates < 30`.
- **Net trade stats** (dir = +1 rules only; informational):
  `signal_metrics.trade_stats(fwd_oo_h − 0.0073)`, where 0.0073 is the moderate
  round trip from `transaction_cost_model`. No table column holds them: the
  weekly run stores them in `runs.note` (`net_trade_stats`, at each rule's
  primary h) and the scorecard prints one line under each buy rule.

**Status** (at each rule's primary h, per rule, per run):

| status | condition | ID label |
|---|---|---|
| `LOW_N` | `n_dates < 30` | *data belum cukup* |
| `CONSISTENT` | `dir·ci_lo > 0` | *konsisten (retro), belum terbukti* |
| `CONTRARY` | `dir·ci_hi < 0` | *berlawanan dengan dugaan* |
| `DIRECTIONAL` | `dir·mean_excess > 0`, CI crosses 0 | *searah, belum robust* |
| `NEUTRAL` | otherwise | *tidak ada edge terlihat* |

None of these is "proven". There is no automatic promotion. Six rules × four
horizons is multiple testing, and the dashboard says so.

**Weights (the "learning").** Weights mirror `daily_picks.learn_weights`. They
are computed per rule from the weekly retrospective market-wide stats at the
rule's primary horizon `H` (10, or 60 for R6):
- `n = n_dates / H`
- `avg_pct = 100 · dir · (date-balanced mean of per-event x_H capped at ±0.15)`
- `weight = clip(1 + 0.5 · n/(n+20) · avg_pct, 0.25, 2.0)`

Weights are versioned by `as_of` (the last session in the weekly fetch) and never
overwritten.

**Broker scores.** For each `(broker, side, h)` the same metric machinery as the
rules is applied to the track-record events. The event direction is +1 for buy
and −1 for sell.
- **Recorded:** `n_events`, `n_dates`, `n_tickers`, `mean_excess`, `ci_lo`, `ci_hi`.
- **Shrinkage:** `shrunk = n/(n+20)·mean_excess`, with `n = n_dates/h`.
- **`low_n`:** `n_dates < 30`.

Broker codes are **not** entities: one code aggregates many clients. Research-grade.

**Broker profitability** (weekly, market-wide, descriptive). For each broker,
over tickers eligible at the run's last session (`as_of`) with `basis_ok`, the
anchored book (§3) gives:
- `n_tickers`
- `total_pnl_rp = Σ total_rp`
- `turnover_rp = Σ turnover_rp`
- `pnl_per_turnover`
- `share_profitable` = share of tickers with `total_rp > 0`

This is a trading P/L over the rolling window, **not** a claim about who owns
what. A ticker whose data ends before `as_of` (suspended, delisted) is left
out, because its book would be marked at an old close; the weekly `runs.note`
counts them (`n_books_stale`).

### 4.6 Alpha case library (weekly, retrospective, descriptive)

This section serves the owner's "learn from snapshots of the stocks that flew"
idea without falling for hindsight.

**What counts as a case.** A case is an eligible `(ticker, T)` with
`fwd_oo_60 ≥ +0.50`. Only the earliest T per episode is kept: once a case is
taken, the same ticker's next 60 sessions are skipped.

**What each case records.** All values are as of T, so each case is the snapshot
a trader could have seen:
- `fwd_oo_60`;
- the rules firing at T, and those firing in `[T−20, T]`;
- the top broker by `NL_60`, with `NL_60/ADV20` and `close/cost_60 − 1`;
- `high_60/low_60 − 1` and `VAL20`.

**How it is shown.** The dashboard shows the latest cases **next to the
look-alike count**: how many eligible rows on the same dates had the same rule
fire and did *not* reach +50%. A pattern is only interesting if it appears in
cases much more often than in the look-alikes. §4.5's `big_rate` vs
`big_base_rate` at h = 60 is the formal version of that comparison.

**Broker lift.** For each broker code, count how often it was the case's top
broker, divided by how often it was the top broker on eligible rows with a known `hold_60` (A3). Lift is
descriptive only. Broker codes are firms with many clients, not people.

### 4.7 History beyond the rolling year (optional)

The API forgets everything older than one year, and long-horizon, rare-event
learning needs more history.
- **Writing:** each weekly run writes the broker and OHLC rows of the **last
  20 sessions** of its fetch to `broker_history/broker_<as_of>.parquet` and
  `ohlc_<as_of>.parquet` (zstd, with a `captured_utc` column). Consecutive
  weeks overlap by about four weeks, so one missed Saturday leaves no hole.
  The legacy-cache export measured 8.8 MB + 0.2 MB, i.e. about 9 MB/week
  (~470 MB/year of draft assets when upload is on).
- **Reading:** resolve the overlap per `(date, ticker)` partition, never per
  broker row: for each `(date, ticker)`, take every broker row from the newest
  file whose OHLC file covers that `(date, ticker)`, and nothing from older
  files. All-zero broker rows are not written, so a newer file cannot state
  that a broker's row became zero; a `(date, ticker, broker)` dedupe would
  keep a row the vendor later restated away.
- **Upload:** only if the repo variable `BROKER_HISTORY_UPLOAD == 'true'`. The
  workflow then uploads them as assets of the GitHub release tag `broker-history`.
  They are **not** committed, so git does not bloat.
- **Default is off.** This is paid NeoBDM data and the repo is public, so turning
  it on is the owner's decision.

---

## 5. `broker_learning.db` schema

The database is small and committed by CI. Primary keys are shown. The code is
authoritative: `broker_learning_db.SCHEMA`.

```sql
runs(run_id TEXT PK, kind TEXT, started_utc TEXT, finished_utc TEXT, status TEXT,
     tickers_ok INT, tickers_fail INT, data_through TEXT, note TEXT)
live_signals(session_date TEXT, ticker TEXT, ruleset TEXT, rule_id TEXT,
     fired INT, score REAL, captured_utc TEXT, features TEXT,
     PRIMARY KEY(session_date, ticker, ruleset, rule_id))          -- eligible rows only
live_outcomes(session_date TEXT, ticker TEXT, h INT, fwd_oo REAL, susp INT,
     exit_date TEXT, recorded_utc TEXT, PRIMARY KEY(session_date, ticker, h))
     -- fwd_oo is hold_60 at h = 60; susp is susp_60 there, NULL at other h;
     -- the excess vs the watchlist is computed at read time (§4.5)
rule_stats(as_of TEXT, ruleset TEXT, rule_id TEXT, h INT, n_events INT, n_dates INT,
     mean_excess REAL, ci_lo REAL, ci_hi REAL, hit_rate REAL, base_rate REAL,
     hit_edge REAL, daily_hit_edge REAL, big_rate REAL, big_base_rate REAL,
     susp_rate REAL, low_n INT, status TEXT,
     window_start TEXT, window_end TEXT, PRIMARY KEY(as_of, ruleset, rule_id, h))
alpha_cases(as_of TEXT, ticker TEXT, session_date TEXT, hold_60 REAL, susp_60 INT,
     sessions_before INT, visible INT, rules_at_t TEXT, rules_prior20 TEXT,
     top_broker TEXT, top_nl60_adv REAL, top_cost_gap REAL, range60 REAL, val20 REAL,
     PRIMARY KEY(as_of, ticker, session_date))
rule_weights(as_of TEXT, ruleset TEXT, rule_id TEXT, weight REAL, n_dates INT,
     avg_excess_pct REAL, PRIMARY KEY(as_of, ruleset, rule_id))
broker_scores(as_of TEXT, broker TEXT, side TEXT, h INT, n_events INT, n_dates INT,
     n_tickers INT, mean_excess REAL, ci_lo REAL, ci_hi REAL, shrunk REAL, low_n INT,
     PRIMARY KEY(as_of, broker, side, h))
broker_profitability(as_of TEXT, broker TEXT, n_tickers INT, total_pnl_rp REAL,
     turnover_rp REAL, pnl_per_turnover REAL, share_profitable REAL,
     PRIMARY KEY(as_of, broker))
broker_lift(as_of TEXT, broker TEXT, n_cases_top INT, n_rows_top INT, case_share REAL,
     row_share REAL, lift REAL, PRIMARY KEY(as_of, broker))
```

Amendment A2 renamed `alpha_cases.fwd_oo_60` to `hold_60` and added
`susp_60`, `sessions_before`, `visible`, `rule_stats.susp_rate` and
`live_outcomes.susp`.

**Write rules:**
- Every write is `INSERT OR IGNORE`, except `runs`.
- Weekly tables are keyed by `as_of`, so re-running the same week is a no-op
  rather than a rewrite.

---

## 6. Dashboard and delivery

**Page.** It is a single self-contained HTML file:
- inline CSS, inline SVG charts, no external requests;
- light and dark themes, readable on a phone.

**Language.** The UI text is Indonesian.

**Sections:**
1. Header: data-through date, run status, disclaimer.
2. Today's signals: watchlist tickers with fired rules, composite score, and each
   rule's status and weight.
3. Per-ticker cards: price and cumulative net-lot chart; book table with
   position, average cost, realized, unrealized, total and % vs cost; key facts
   (largest holder and their cost; biggest winner; who is distributing).
4. "Apa yang sudah dipelajari": the rule scorecard (retro h = 5/10/20/60 with CIs and big-move rate vs base,
   base rates, status), the live ledger counts, and weight changes vs the
   previous week.
5. "Kasus alpha": the latest cases (§4.6), each with its pre-move snapshot, the
   look-alike count and broker lift.
6. Broker leaderboards: track-record scores (with CI and `LOW_N`) and trading
   profitability.
7. Data quality: failed tickers, basis warnings, anchors.

**Delivery.** Telegram `sendDocument` to `TELEGRAM_CHAT_ID`, with a caption of at
most 1,024 characters. The HTML is written to `broker_dashboard_out/` (ignored via
`*.html`) and is **never committed**. The repo is public.

**Secrets and logs:**
- Errors print exception types only, never URLs; the Telegram URL contains the
  token.
- `getUpdates` is never called; it would swallow `telegram_inbox.py` replies.

---

## 7. Operations

| Workflow | Cron (UTC) | Does |
|---|---|---|
| `broker-learning-daily.yml` | `30 10 * * 1-5` | `python broker_learning_run.py daily` → commits `broker_learning.db` |
| `broker-learning-weekly.yml` | `0 11 * * 6` | `python broker_learning_run.py weekly` (~1,040 fetches, ~45 min) → commits `broker_learning.db`. Uploads `broker_history/` to release `broker-history` only when repo variable `BROKER_HISTORY_UPLOAD` is `true` (§4.7) |

- **Concurrency:** both workflows share the group `broker-learning` with
  `cancel-in-progress: false`.
- **Schedule placement:** both sit outside the scheduled NeoBDM windows of the
  other jobs (daily-scrape ≈23:00–01:45, top-up ≈04:30–05:20, ownership
  ≈07:00–08:25 every day including Saturday, arb-veto Sun 21:00). GitHub can
  delay a scheduled run by hours (top-up has committed as late as 11:39), and a
  manual run can start at any time, so an overlap is unlikely, not impossible.
- **Weekly NeoBDM load:** two full-universe `/api/inventory` harvests of
  ~1,000 calls each (this weekly job on Saturday, `arb-veto.yml` on Sunday),
  plus ~5 × 46 daily watchlist calls. Feeding both jobs from one harvest is
  possible follow-up work.
- **Secrets:** all four (`NEOBDM_USERNAME`, `NEOBDM_PASSWORD`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), because importing `neobdm_scraper`
  requires all four.

**Local runs:**
```
py broker_learning_run.py daily --dry-run                  # fetch watchlist, render, no Telegram, no DB commit
py broker_learning_run.py daily --dry-run --no-fetch       # --no-fetch needs --dry-run or --db (cached rows are not prospective)
py broker_learning_run.py weekly --no-fetch --raw-dir inventory_raw --legacy-cache
py broker_learning_run.py render --dry-run                 # re-render from cache + DB
```

---

## 8. What this must not claim

- That a broker's position, cost or P/L is a real owner's holding or cost basis.
- That any rule or broker score is proven, production-grade or tradable.
  Statuses are research labels.
- Sharpe or any annualized statistic.
- Any edge from one pooled metric. Base rate, `n_dates` and the CI are always
  shown.
- That retrospective results are point-in-time. Only the live ledger is
  prospective.
- Anything about beneficial ownership. See `ML_V2_EXPERIMENT_2_OWNERSHIP_ROADMAP.md`.

## 9. Known limitations

- **Rolling year:** the API serves only one rolling year, so retrospective stats
  cover at most about 240 sessions (about 150 evaluable dates after warm-up and
  h = 20).
- **Single regime:** it is one market regime. The universe drift was +124%/yr
  (HANDOFF), which is why excess, not raw return, is the metric.
- **Broker codes are not entities:** leave-one-broker-out fragility (Exp #1)
  applies to broker scores.
- **Vendor restatement:** restatements can change retrospective numbers between
  weeks. The live ledger is immune by construction.
- **Basis regimes:** tickers with basis regimes are excluded or re-anchored, not
  repaired.

---

## 10. Amendment log

### Amendment A2 (2026-09-25, before any production run). Normative; overrides earlier sections where they conflict.

**Why.** The owner sent nine real multi-bagger charts (IMPC, ENRG, VKTR, MGLV,
RLCO, PTRO, BRPT, PSKT, SINI). Checking them in the cache showed three measurement
problems. None of them came from looking at how any rule performed.

1. **The contiguity guard in `add_forward_returns` hides most big winners.** It
   drops any window that contains a missing session. IDX suspends stocks that run
   hard, and suspensions show up as missing sessions.
   - At h = 60 this happened in 78% of ≥ +100% windows and 99% of ≥ +300% windows.
   - VKTR's best 60-session move is +601% as a holder saw it, but only +44%
     after the guard.
2. **Most multi-baggers start illiquid.** Only 3 of the 17 ten-baggers traded
   ≥ Rp 1 bn/day at the start of their run.
3. **One year of data cannot give a meaningful CI at h = 60.** There are about 99
   evaluable dates, which is fewer than 2 blocks of 60 dates.

**What changed:**

- **Holder return at h = 60: `hold_60`.**
  - Entry is open(T+1); entry is impossible if the ticker did not trade on the
    next calendar session, and the result is NaN.
  - Exit is the open of the first traded session on or after calendar position
    `pos(T)+1+60`.
  - Missing sessions (suspensions) are **bridged**: the position is frozen. Every
    **observed** close-to-close step in the window must still sit inside the
    ARA/ARB band (`price_audit.ara_bound`, `ARB_BOUND`, `TOL`), so splits and
    reverse splits stay excluded.
  - Entry-blocked (limit-locked open) → NaN.
  - `susp_60` = 1 if the window contains a missing session.
  - **All h = 60 statistics (rule_stats, weights, broker_scores), `x_60`, alpha
    cases and big-move rates use `hold_60`.** h ∈ {5, 10, 20} keep `fwd_oo_h`.
  - The dashboard shows the suspension share next to every h = 60 number.
    Suspension means money is frozen and the reopening can go either way.
- **Eligibility:** `VAL20 ≥ Rp 2e8`, for every rule.
- **LOW_N:** `n_dates < max(30, 3·h)`, so h = 60 needs ≥ 180 event dates.
  - `block_bootstrap_ci` becomes a **circular** moving-block bootstrap. It returns
    NaN when `n_dates < 3·block`, and a NaN CI is `LOW_N`, never `NEUTRAL` or
    `CONSISTENT`.
  - With one rolling year, h = 60 verdicts will say *data belum cukup* until
    history accumulates (§4.7). That is the honest answer.
- **Alpha cases:**
  - use `hold_60 ≥ +0.50`;
  - add `sessions_before` (the ticker's sessions before T in the data) and
    `visible = sessions_before ≥ 80`;
  - add `susp_60`.
  - The dashboard marks invisible cases *"fase akumulasi sebelum data dimulai —
    tidak terlihat"* and computes broker lift on visible cases only.
  - The per-case look-alike count is replaced by the h = 60 big-move-rate vs
    base-rate table.
- **History release:** created as a **draft** release (`gh release create --draft`).
  Draft assets are visible only to repo collaborators, so the paid data is not
  published even though the repo is public.
- **Zero-session responses** (delisted or long-suspended tickers) are counted as
  `empty`, not as fetch failures. `short_window_abort` still guards against the
  rolling window moving.

**Pre-amendment integration run (recorded so it cannot be cherry-picked later).**
Legacy cache 2025-08-22..2026-08-21, guarded `fwd_oo_60`, `VAL20 ≥ 1e9`, old
bootstrap. Mean excess %:
- h = 10: R1 −0.12, R3 −0.00, R4 −0.01, R5 −0.05, R6 +0.21. R2 was LOW_N.
- h = 60: R1 +1.29, R3 −0.82, R4 +1.82, R5 −0.25, R6 +2.23.

Big-move rate at h = 60 vs base 1.42%: R1 1.01, R4 2.09, R6 0.88. The h = 60
"CONSISTENT" labels in that run were bootstrap artifacts (~1.65 blocks), and the
integration run found means lying outside their own CI.

### Amendment A3 (2026-09-26, before any production run)

These are two owner-level decisions raised by the adversarial review. Neither
touches a rule threshold.

- **Basis check (§2.3b): odd-lot rupiah is left out of the VWAP numerator.**
  Rupiah on a broker-day with `blot = 0` comes from fills under 100 shares and
  has no lot to divide by. Counting it pushed thin days over the `[low·0.99,
  high·1.01]` band and created basis breaks that did not exist. For example,
  INCI on 2025-09-19 read 617.28 against a ceiling of 616.10, and 615.35 without
  the odd-lot rupiah. UFOE lost 46 eligible rows and its anchor moved. This
  corrects a measurement error and does not widen the band: a real basis break
  still flags (test `test_odd_lot_rupiah_does_not_fake_a_basis_break`).
- **Broker lift (§4.6): the base is eligible rows with a known `hold_60`.** A
  case can only come from those rows, so comparing against all eligible rows
  would mix in rows whose outcome is unknowable (the last 60 sessions and
  suspended entries). The code already did this, and the spec now says so.