# Experiment #2A0 exploratory event study

## Frozen protocol

The controlling protocol is the **Experiment #2A0: frozen exploratory event
study** section of `ML_V2_EXPERIMENT_2_OWNERSHIP_ROADMAP.md`, approved with
final corrections on 2026-09-02. The implementation must stop before return
analysis unless its mandatory event-date observability gate passes.

## Pre-run audit -- 2026-09-02

Source filter:

```sql
threshold = '5pct'
AND is_custodian_move = 1
AND lot_change IS NOT NULL
AND lot_change != 0
AND captured_at < '2026-09-02'   -- as-of the freeze; see below
```

Population verification passed:

| Check | Confirmed |
|---|---:|
| Source rows | 45 |
| Positive / negative rows | 18 / 27 |
| Unique `(ticker, change_date)` primary events | 36 |
| Unique `(ticker, entity_id-or-raw, change_date)` holder-events | 39 |
| Tickers | 11 |
| BREN concentration | 13/36 (36.1%) |
| Top-five concentration | 28/36 (77.8%) |

## Cohort reproducibility -- fixed 2026-09-03

The pre-run audit above was reproducible only for as long as no new ownership
capture ran. The frozen filter carried no upper bound in time, so the
2026-09-03 capture -- ordinary, correct data collection -- added one newly
disclosed PKDA 5% custodian move (`SINI` / `change_date` 2026-09-01, first
captured 2026-09-03T07:32:14Z) and took the standing query to 46 rows, 37
primary events, and 12 tickers. The drift guard then failed on every run,
reporting normal capture as a breach of the freeze.

The cohort is now bounded at first-capture time: the frozen population is the
matching rows with `captured_at < 2026-09-02`, the day the audit was confirmed.
That bound reproduces the frozen numbers exactly -- 45 rows, 18/27, 36 events,
39 holder-events, 11 tickers, BREN 13/36, top-five 28/36 -- and changes no
event, filter term, horizon, or outcome rule. Rows captured on or after the
boundary are printed as post-freeze accretion and never enter the study.

Two guards now stand behind the freeze:

| Guard | Catches |
|---|---|
| Count audit (unchanged) | A change in the shape of the cohort |
| SHA-256 cohort digest `aec1358174f98b1bc89530f60e1abff6542b6140e466ca62af89ed07f0cb44a6` | A re-parse that rewrites a value in place while the counts still match |

The 2026-09-03 run reports: population audit 45/36/11 with the pinned digest,
post-freeze accretion of 1 row / 1 event / 1 ticker (`SINI/2026-09-01`), and
the observability gate still failing closed. Nothing below changed.

## Event-date observability gate -- FAILED

Return analysis was not run.

**Experiment status: BLOCKED at observability.**

Evidence:

1. `ownership_change.change_date` is parsed verbatim from the `Tanggal` column
   in NeoBDM's PKDA 5% move-history pane. Neither the page fragment, parser,
   database, nor captured provenance supplies a publication timestamp for an
   individual historical move.
2. All 45 frozen rows have `published_at IS NULL`,
   `dq_unknown_publication_time = 1`, and `available_at = captured_at`.
3. All 45 rows were first observed during the 2026-08-31 historical backfill,
   although their `change_date` values span 2025-09-01 through 2026-07-20.
   Therefore `change_date` cannot be used as a point-in-time observable-date
   proxy without backdating information.
4. The earliest defensible stored observation is the 2026-08-31 capture time,
   not the historical `change_date`. That establishes only that the backlog was
   visible by capture time; it does not recover each move's original public
   availability date.
5. The source's current 5% snapshot was dated 2026-08-28 when captured on
   2026-08-31, independently demonstrating source lag. Applicable ownership
   disclosure rules also distinguish transaction/change dates from later
   reporting/publication, so they do not validate same-date observability.

Relevant official references:

- [OJK Regulation No. 4 of 2024](https://ojk.go.id/id/regulasi/Pages/POJK-4-Tahun-2024-Laporan-Kepemilikan-atau-Setiap-Perubahan-Kepemilikan-Saham-Perusahaan-Terbuka-dan-Aktivitas-Menjaminkan.aspx)
  separately regulates the deadline for submitting ownership-change reports.
- [OJK's AKSes KSEI publication notice](https://www.ojk.go.id/id/berita-dan-kegiatan/siaran-pers/Pages/AKSes-KSEI-dan-Fitur-Publikasi-Melalui-Website-BEI.aspx)
  states that information is sent to BEI for publication after a report is
  submitted. It does not establish publication on the underlying change date.

### Publication-timestamp recovery audit -- INCOMPLETE

An event-level recovery audit of the frozen 36-event manifest was started on
2026-09-02 using official BEI publication records, including BEI publications
generated from KSEI electronic ownership reports. The audit was stopped before
every candidate could be fully reconciled to the underlying PKDA custodian
movement. Same-holder or nearby-date filings found during the partial review
were not sufficient by themselves because transaction quantities and/or dates
could differ from the frozen PKDA record.

No event is recorded as `verified_observable`, no recovery counts are final,
and no observability manifest was created. The original 36-event manifest is
unchanged. Publication recovery must resume from that manifest and complete
all event-level adjudications before the gate can be reconsidered.

Under the frozen stop rule, no raw returns, IHSG-adjusted returns, placebo
tests, bootstraps, multiplicity adjustments, or sensitivity estimates may be
computed from historical `change_date` values. The gate can be revisited only
with independently timestamped historical publication records or prospectively
captured first-availability observations with sufficient forward horizons.

The frozen implementation is `experiment_2a0_event_study.py`. Its cohort and
observability checks ran on 2026-09-02: the cohort audit passed, the
observability gate returned failure, no price file was loaded, and no result
artifact was created. Eleven focused implementation tests pass, including a
synthetic end-to-end study, strict-next-session alignment, horizon-specific
placebo eligibility, and the three cohort-boundary tests added on 2026-09-03.

### Note on prospectively captured moves

`SINI/2026-09-01` is outside the frozen cohort and is not studied here, but it
differs in kind from the 45 backfilled rows: it was first observed by a capture
two days after its `change_date`, rather than in a single backfill spanning a
year of history. Bounding first availability that way is what the historical
rows cannot support. This is an observation about the accretion, not a
prospective cohort -- none is defined, and defining one would need its own
approved preregistration.

Experiment #2A0 remains **BLOCKED at observability**. Experiment #2A ML
remains **FAILED**. Experiment #2B has not been started.
