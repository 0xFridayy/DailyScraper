# Machine pick execution timing

## Contract

`next_session_close_v1` uses signal snapshot `k`, entry `k+1`, and exit
`k+1+HORIZON`. `HORIZON` remains 5. `machine_window()` supplies the anchors
for tracking, holding prevention and learning. Prices and the market benchmark
use the same entry and exit. Explanations cover the five sessions after entry.

Snapshot dates are capture dates, not trading dates. A Saturday or Monday
capture can hold Friday's close. `load_snapshots()` removes weekend and holiday
copies before counting sessions. Tags and ranking still use only data through
the signal snapshot.

Example with no holidays:

| Trading session | Snapshot | Close | New machine state |
| --- | --- | ---: | --- |
| Friday 11 Sep 2026 | k | 1,000 | Signal data, visible Monday morning; entry pending, no return |
| Monday 14 Sep | k+1 | 1,200 | Entry, day 0/5, 0.0% |
| Tuesday 15 Sep | k+2 | 1,224 | Day 1/5, +2.0% |
| Friday 18 Sep | k+5 | 1,296 | Day 4/5, +8.0%; still held |
| Monday 21 Sep | k+6 | 1,320 | Day 5/5, finished, +10.0% |

Previously, live reporting finished at `k+5`, returning +29.6% from Friday's
1,000. Learning waited until `k+6` and returned +10.0% from Monday's 1,200.
New live results and learning both use the latter definition. A new signal for
the same ticker is allowed once the old exit snapshot exists; its own entry is
the following session. Reports become available on the next morning's capture.

`picks.snapshot_date` and `picks.close` remain the signal snapshot and signal
close. They are not an execution price. `pick_results.started` retains the signal
key for deduplication; `start_snapshot` and `start_price` describe entry, and
`end_snapshot` and `end_price` describe exit. Both tables carry `timing`.

## Missing data

- Before the entry snapshot exists, display pending entry and no percentage.
- Missing/suspended ticker data at entry stays unavailable even if the ticker
  returns later. Never replace the scheduled entry with a later price.
- Missing/suspended ticker data at exit produces an unscored result at the
  scheduled exit, with null return and benchmark. Never wait for a recovery
  price. Such rows do not count as wins or losses. A missing entry is likewise
  recorded unscored when the scheduled exit snapshot exists.
- A ticker's suspension between valid entry and exit does not stop the market
  session clock. The suspended running observation displays n/a. A later valid
  observation can resume the return from the original entry.
- A missing whole-market capture between signal and exit makes the exact
  execution window unknown. Display execution timing unavailable, record no
  result, exclude that window from learning, and reserve the ticker against
  duplicate picks. This includes a missing signal snapshot. Repair/backfill
  the source data before the pick can be resolved; there is no timeout that
  silently releases it. A gap after the scheduled exit has no effect.
- Corporate-action breaks inside the executed window are unscored in live
  reporting and learning. A break from signal to entry is outside the return.
- An unsent snapshot is insufficient evidence of freshness. The fallback for
  missing today's capture only accepts the expected prior weekday session,
  preserving Monday's use of a Saturday capture of Friday's close.

Session inference still depends on the existing copy heuristic and capture
dates. It cannot distinguish a missing holiday capture from a missed trading
session, or identify an incorrect source session timestamp with certainty.
Ambiguous detected gaps are held unscored. An exchange calendar and authoritative
source session timestamps remain outside this P0 change.

## Historical compatibility

The old tables contain no timing version or executable-entry provenance.
`recorded_utc` cannot establish the price at which an old pick was intended to
execute. Do not infer a new convention from that timestamp or rewrite history.

`ensure_schema()` adds `timing` to `picks` and `pick_results` with default
`legacy_signal_close`. It is idempotent and does not update any existing value
in the original columns. Existing finished results are never recalculated.
Existing unfinished picks continue their old `k -> k+5` clock and are explicitly
labelled legacy signal-close in running and finished reports. This is a
compatibility interpretation of unversioned history, not proof of execution.

Every new machine pick explicitly stores `next_session_close_v1`. Its result
inherits that version. New user results store `user_reference`; historical user
rows keep their values and all user reference-price and duration rules remain
unchanged. Result writes remain insert-only and failed sends roll back result
and new-pick writes.

The weekly scoreboard displays legacy/unversioned machine history separately.
It excludes that history from current machine averages and pooled explanation
statistics. Learning continues to use snapshot outcomes, not legacy result rows.
Unknown pick timing versions raise an error instead of silently selecting a clock.

The repository's committed database is not migrated by this change. Migration
runs when the updated application opens it; preview migrates only an in-memory
copy. Take the normal database backup before deployment. Older application
versions use positional inserts and cannot write the expanded schema; a rollback
must restore the matching pre-migration database backup, retaining the newer
database separately to avoid losing new records.

## Verification and scope

Run `python test_daily_picks.py`. Regression fixtures pin the signal/entry/exit
indices, running and finished returns, learning parity, holidays, missing and
suspended observations, unknown session gaps, duplicate prevention, legacy
migration, scoreboard separation and failed-send retries.

The four production checks, `MIN_TAGS`, `HORIZON`, ARB veto, Telegram commands,
broker-learning production integration, strategy types and SPECTRA/ML experiments
are unchanged. Only execution-clock correctness and its compatibility boundary
are covered here. P1 is not started.
