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
artifact was created. Eight focused implementation tests passed, including a
synthetic end-to-end study, strict-next-session alignment, and
horizon-specific placebo eligibility.

Experiment #2A0 remains **BLOCKED at observability**. Experiment #2A ML
remains **FAILED**. Experiment #2B has not been started.
