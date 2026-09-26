# DailyScraper data products — contract

**Status:** authoritative data-product contract, adopted 2026-09-26.

This is documentation only. It states the target contract. Where today's code
differs, the relevant section says so under **Current state**. Nothing here
changes runtime behaviour, databases, workflows, collection scope or existing
data.

The decisions behind this contract are logged in
[`DECISION_LOG.md`](DECISION_LOG.md) (DS-D01–DS-D11). On the semantics of a data
product, this document is authoritative. Experiment and consumer specs, such as
[`BROKER_LEARNING.md`](../BROKER_LEARNING.md) ruleset v1, keep governing their
own frozen behaviour.

---

## 1. Scope

### 1.1 Four logical systems, one repository

The repository is a monolith holding code for four logical systems. This
contract defines only what DailyScraper publishes. It does not move code or
redesign the other three systems.

| System | Owns | Relationship to DailyScraper products |
|---|---|---|
| **DailyScraper** | collection, scraping, raw evidence, normalization, canonical datasets, collection scope | Publisher |
| **Market Intelligence** | broker/actor interpretation, issuer-specific actor hypotheses, accumulation/distribution and ownership interpretation, evidence reconciliation, confidence/hypothesis state | Consumer; may request additional scope |
| **SPECTRA** | candidate discovery, screening, ranking/filtering, anomaly detection, evidence payloads, watchlist/candidate monitoring | Consumer; may request additional scope |
| **Sentinel** | data/pipeline health, freshness, integrity/regression checks, audit/guardrails | Read-only consumer of products and run metadata; produces health verdicts |

### 1.2 Boundary rules

- **DailyScraper publishes data, not conclusions (DS-D01).** Labels such as
  "retail", "smart money", "bandar", owner group or operator are
  interpretations. They belong to Market Intelligence or SPECTRA. A label the
  vendor supplies may be published as a vendor fact (§8).
- **Consumers read published products and manifests, not DailyScraper's Python
  modules (DS-D08).** Each consumer derives its own `eligible_at` (§2.2).
  **Current state:** modules import each other across these logical boundaries.
  For example, `broker_book.py` imports `build_inventory_db` and
  `broker_learning.py` imports `price_audit`. Untangling that is out of scope
  here.
- **Sentinel is read-only (DS-D09).** It checks published products and run
  metadata, and produces health verdicts. It writes no product data and
  triggers no repair. **Current state:** the checkers `check_capture_health.py`,
  `check_signal_integrity.py` and `check_ml_health.py` are Sentinel's
  precursors. They alert via Telegram and a non-zero exit code. A review on
  2026-09-26 checked the scripts and their workflows:
  - no SQL write statement, file write, git commit or artifact upload was found;
  - they open `neobdm.db` in normal read-write mode, so read-only is not
    enforced;
  - the modules and test scripts they import or run were not audited.

### 1.3 Products covered

| Product | Status | Section |
|---|---|---|
| `broker_inventory_daily` v1 | Logical contract; not yet published as a table | §3–§5 |
| `broker_flow` | Designated legacy: frozen for new development; runtime writers still active (migration debt) | §6 |
| `price_history` | In use; future principles adopted, replacement not implemented | §7 |
| Market summary (`market_summary_daily` and its source contract) | In use; provenance classes adopted | §8 |

Ownership capture (`neobdm_ownership.db`) is out of scope for this revision.

---

## 2. Cross-product principles

### 2.1 Session time vs capture time (DS-D02)

| Field | Meaning |
|---|---|
| `session_date` | The IDX trading session the data describes (Asia/Jakarta calendar date) |
| `captured_at` | The UTC instant DailyScraper obtained the evidence |

- These are different fields. Neither is derived from the other.
- A capture at 07:00 WIB on day D typically describes session D−1 or earlier
  ([`HANDOFF.md`](../HANDOFF.md) Lampiran E).
- Filesystem modification time is never evidence of either field.
- A legacy record without a capture timestamp has an unknown `captured_at`.
  Do not invent one.

### 2.2 Evidence vs eligibility (DS-D03)

DailyScraper publishes evidence timestamps: `captured_at`, plus any timestamp
the source itself supplies (for example `source_timestamp` in `ms_raw_response`).
It does not publish `eligible_at` or `available_at`.

Each consumer derives eligibility from that evidence under its own declared
policy. A record without evidence carries none. How a consumer treats it (as
ineligible, or as explicitly retrospective) is that consumer's policy.

**Current state (needs semantic review):** the ownership store, which is
otherwise outside this revision (§1.3), stores an `available_at` column.
`ownership_ingest.resolve_available_at` sets it to `published_at` when that is
verifiable, and otherwise to `captured_at`.
- `captured_at`, `published_at` and an evidence-derived availability timestamp
  may legitimately be publisher evidence.
- `eligible_at` is a consumer policy decision.

It is not yet established whether this column records only evidence
availability or embeds downstream eligibility policy. Until that review, it is
not classified as a DS-D03 conflict. This document does not change it.

### 2.3 Append and revision (DS-D11)

Historical observations are revisioned, not silently rewritten. Point-in-time
replay and reproducibility require knowing what was observed, and when each
correction became known. In future canonical products, observations and
corrections are appended as new records or revisions:
- a source correction is its own revision;
- a DailyScraper repair is its own revision;
- a quarantine is its own annotation.

Silent historical overwrite is not the target architecture. A "current best"
table may exist as a derived view over that history.

**Current state:** `price_history` and `broker_flow` are written with
`INSERT OR REPLACE`. `price_quarantine` is a snapshot rebuilt on every run.
That behaviour is migration debt, not the target contract. This document does
not change it.

### 2.4 Absence is not zero (DS-D04)

A missing row, key, broker or session is never read as zero. A zero is
published only where the source explicitly returned zero. Coverage states for
broker data are defined in §4.

The market summary already follows this rule: `market_summary_daily` NULL means
unavailable, never 0 (`neobdm_source_contract.py`).

### 2.5 Collection scope (DS-D05)

DailyScraper owns collection scope: which instruments, brokers, investor types,
windows and fields are requested.
- **Neutral:** scope is defined by coverage (for example, "every broker code the
  source lists"), never by an interpretation category.
- **Versioned:** every capture records a `scope_id` and version. Scope changes
  only by an explicit new version.
- **Requests:** Market Intelligence and SPECTRA may request additional scope. A
  granted request adds coverage. It does not make the category behind the
  request ("retail", "smart money", an owner group, a bandar group) part of the
  canonical data.

**Current state:**
- `broker_flow` scope is `BROKER_FLOW_CODES`. `neobdm_scraper.py` builds it from
  `RETAIL_BROKERS`, `SMART_MONEY`, the algo tiers and `BANDAR_GROUPS`, so the
  scope is defined by interpretation. That is one reason `broker_flow` is
  designated legacy (§6).
- `backfill_inventory.py` requests the vendor selectors `TOP_5_NB_LOT_C20` and
  `TOP_5_NS_LOT_C20` (recency-weighted top movers). It then keeps only codes in
  `BROKER_FLOW_CODES`.
- `broker_collect.py` (broker learning) requests every code in
  `broker_codes.json`. That scope is neutral, but it is not versioned. Nothing
  beyond the payload's own `meta` records it per capture.

### 2.6 Provenance classes

Every published field has exactly one class.

| Class | Meaning | Who may publish it |
|---|---|---|
| `RAW_VENDOR_OBSERVATION` | A value as the vendor returned it (lossless parse only) | DailyScraper |
| `VENDOR_COMPUTED` | A value, score, ranking or category the vendor computed or assigned | DailyScraper, labelled as the vendor's |
| `OUR_NORMALIZATION` | A deterministic, versioned DailyScraper transformation that changes representation, not meaning: typing, reshaping, identity mapping, declared units, coverage state | DailyScraper |
| `DERIVED` | Deterministic math over published fields (averages, cumulative curves, average cost, rolling state). It carries a derivation id, a version and its inputs | Whoever computes it; never stored in a primary field |
| `OUR_INTERPRETATION` | A claim about actors, intent, ownership, a signal's meaning or a candidate's merit | Market Intelligence or SPECTRA only |

---

## 3. `broker_inventory_daily` v1

`broker_inventory_daily` is the successor to `broker_flow` for all new broker
work (DS-D06). It is a logical contract; no table publishes it yet.

**Nearest existing code (not the product itself):**
- `broker_collect.py` captures payloads. It caches each one as an envelope
  `{fetched_utc, meta, data}` under `broker_learning_raw/` (gitignored).
- `build_inventory_db.strict_ticker_frame` normalizes a payload into broker rows.
- `harvest_inventory.py` caches bare `data` dicts under `inventory_raw/`
  (gitignored, legacy).

### 3.1 Source

The source is `GET https://neobdm.tech/api/inventory`:
- **Parameters:** `symbol`; `brokers` (repeated; broker codes or vendor
  selectors); `start_date`; `end_date`; `investor_type` (`A` all, `F` foreign,
  `D` domestic).
- **Window:** it serves a rolling window of about one year. Older sessions
  cannot be re-fetched.
- **Response:**
  - `date[]`;
  - one per-broker dict for each of `blot`, `slot`, `nlot`, `bval`, `sval` and
    `nval`;
  - `ohlc[]`;
  - `meta` (`symbol`, `brokers`, `start_date`, `end_date`, `investor_type`).

### 3.2 Identity

One observation is one broker, in one session, from one capture.

| Field | Meaning |
|---|---|
| `source` | Source id (for example `neobdm.api.inventory`), plus the endpoint version where the vendor exposes one |
| `instrument` | The requested ticker. The payload's `meta.symbol` must equal it, or the capture fails |
| `session_date` | An element of the payload's `date[]` |
| `broker_code` | The vendor broker code, as returned |
| `investor_type` | As requested. `A`, `F` and `D` are different observations of the same broker-session |
| `capture_id` | The capture (§5) that produced the observation. `captured_at` is a property of the capture |

- `(source, instrument, session_date, broker_code, investor_type)` identifies the
  logical observation. Adding `capture_id` identifies one occurrence of it.
- Two captures of the same session are two observations, even when their values
  match.
- Choosing a current-best value across captures is a derived view, not an
  overwrite.
- Instrument identity across renames, relistings and corporate actions is not
  defined yet (§9).

### 3.3 Fields and units

| Field | Class | Unit and type | Rule |
|---|---|---|---|
| `blot` | `RAW_VENDOR_OBSERVATION` | lots, integer ≥ 0 | |
| `slot` | `RAW_VENDOR_OBSERVATION` | lots, integer ≥ 0 | |
| `nlot` | `RAW_VENDOR_OBSERVATION` | lots, signed integer | `nlot = blot − slot` |
| `bval` | `RAW_VENDOR_OBSERVATION` | full Rupiah, ≥ 0 | |
| `sval` | `RAW_VENDOR_OBSERVATION` | full Rupiah, ≥ 0 | |
| `nval` | `RAW_VENDOR_OBSERVATION` | full Rupiah, signed | `nval = bval − sval` |
| `open`, `high`, `low`, `close` | `RAW_VENDOR_OBSERVATION` | source price units, as quoted | not rescaled |
| `volume` | `RAW_VENDOR_OBSERVATION` | shares | |
| `volume_sma20` (if present) | `VENDOR_COMPUTED` | shares | |
| `coverage_state` | `OUR_NORMALIZATION` | §4 | |

- All six broker fields are daily values, not cumulative
  ([`HANDOFF.md`](../HANDOFF.md) Lampiran N).
- Both invariants held exactly in every inspected cell (§10).
  `strict_ticker_frame` enforces `nlot` exactly and `nval` within 0.5 Rp.
- A capture that violates an invariant, a type or a series length fails closed.
  Nothing is padded, truncated or zero-filled. `strict_ticker_frame` already
  behaves this way. `broker_book.frames_from_payload` also refuses negative gross
  fields.

### 3.4 Lot size

1 lot = 100 shares.

The lot size is reference data. It should be published as a versioned,
provenanced reference (value, effective range, evidence) that derived values
cite, not as an unexplained constant.

**Current state:** the literal `100` appears in the code, for example in
`broker_book.py`, `backfill_inventory.py` and `price_audit.py`.

### 3.5 Session semantics

- `date[]` is parallel to every broker series and to `ohlc[]`.
- Each element of the payload's `date[]` is a `session_date`.
- In the 500 inspected captures:
  - no date was duplicated or out of order;
  - no session date fell on a weekend;
  - OHLC dates were aligned in 500 of 500.
- `captured_at` comes from the capture envelope: `fetched_utc` in
  `broker_learning_raw/`.
- Legacy `inventory_raw/` payloads are bare `data` dicts with no `fetched_utc`.
  Their `captured_at` is unknown. It is never taken from the file's modification
  time.

### 3.6 Evidence

The unit and session claims in §3.3–§3.5 rest on the read-only cache
inspection of 2026-09-26, recorded with its limitations in §10.

### 3.7 Derived values

These values are `DERIVED`:
- buy average `bval / (blot × 100)` and sell average `sval / (slot × 100)`;
- inventory curves (cumulative `nlot` from an anchor);
- average cost, and realized and unrealized P/L;
- rolling inventory state.

Rules for every derived value:
- It carries a derivation id and version, the lot-size reference version, and
  the capture ids of its inputs.
- It is undefined, not zero, where its denominator is zero or its inputs are
  not `OBSERVED_*` (§4).
- It never replaces a primary field and is never stored in one.
- A cumulative quantity starts from an unknown opening balance. An inventory
  curve is observed net flow since an anchor, not a holding.

**Current state:** `broker_book.py` computes the anchored average-cost book for
the broker-learning dashboard ([`BROKER_LEARNING.md`](../BROKER_LEARNING.md) §3).
Under this contract, that output is `DERIVED`.

### 3.8 Data-quality condition: value without lots

**Verified fact:** in 556 inspected broker-session cells, `bval > 0` while
`blot = 0` (§10). That field inconsistency is all the inspection established.
Their cause is not verified.
- These records are not fixed, dropped or reinterpreted.
- They are a data-quality condition that needs future validation and
  annotation. The target is a timestamped annotation per cell (§2.3).
- They do not weaken the full-Rupiah unit, which rests on 4,217,822
  implied-price tests (§10).

[`BROKER_LEARNING.md`](../BROKER_LEARNING.md) Amendment A3 explains rupiah on
`blot = 0` broker-days as odd-lot fills (under 100 shares), and leaves it out of
its VWAP numerator. "Odd-lot" is a historical hypothesis; the cache inspection
did not establish it. A3's handling is a consumer choice and is unaffected.

---

## 4. Coverage semantics

**Missing row ≠ zero.** Every `(capture, session_date, broker_code)` has exactly
one coverage state.

| State | Meaning | Values |
|---|---|---|
| `OBSERVED_NONZERO` | The broker is returned for the session, and at least one of `blot`, `slot`, `nlot` is non-zero | as returned |
| `OBSERVED_ZERO` | The broker is returned for the session with explicit `blot = slot = nlot = 0` | as returned; a non-zero value field with zero lots is flagged per §3.8 |
| `NOT_REQUESTED` | The broker is not in the capture's requested set | none |
| `REQUESTED_NOT_RETURNED` (`OMITTED_BY_SOURCE`) | The broker or session was requested and the capture succeeded, but the source did not return it | none: unknown, not zero |
| `SOURCE_FAILURE` | The capture failed: HTTP error, `success = false`, symbol mismatch, validation rejection, or cross-ticker clone | none, for every broker in the capture |
| `OUTSIDE_SOURCE_WINDOW` | The session lies outside the window the source serves (the rolling year) or outside the requested window | none |
| `NOT_TRADED` | The instrument did not trade in an exchange session (for example, a suspension), shown by calendar or instrument-status evidence | none |
| `UNKNOWN_LEGACY_COVERAGE` | The capture's exact requested set was not preserved, so an absent broker cannot be classified | none |

**Rules:**
1. `OBSERVED_ZERO` requires the broker to be in the returned payload with
   explicit zero quantities for that session. Nothing else produces a zero.
2. Future captures must preserve the exact requested broker set and the
   returned broker set (§5).
3. A normalizer may drop all-zero rows only if it keeps the returned broker set
   alongside. Otherwise `OBSERVED_ZERO` and `REQUESTED_NOT_RETURNED` become
   indistinguishable. **Current state:** `strict_ticker_frame` drops all-zero
   rows and does not record the returned set. The `broker_learning_raw/`
   envelopes keep the payload `meta`.
4. A vendor selector (for example `TOP_5_NB_LOT_C20`) resolves at the source.
   Every broker outside its result is `NOT_REQUESTED` for that capture.
5. A session missing from `date[]` cannot be `NOT_TRADED` until DailyScraper
   publishes exchange-calendar and instrument-status evidence. Until then it is:
   - `OUTSIDE_SOURCE_WINDOW`, if it lies before the served window;
   - otherwise `REQUESTED_NOT_RETURNED`, at session level.
6. Coverage is per capture. Combining captures (for example into a current-best
   view) is a derived view and must state the rule it used.

**Evidence** (§10): the inspection observed both behaviours.
- 7,019,501 returned cells were explicit zeros, so `OBSERVED_ZERO` is common
  and real.
- All 21 captures with explicit request metadata returned every requested
  broker.
- 56 of 479 legacy `inventory_raw/` captures omitted at least one broker from a
  request set inferred from today's `broker_codes.json`.

The behaviour is mixed, so omission by the source cannot be ruled out. The
legacy request sets are inferred, so some inferred omissions may come from a
historical request list that differed from today's `broker_codes.json`. Legacy
`inventory_raw/` coverage therefore stays `UNKNOWN_LEGACY_COVERAGE`.

---

## 5. Capture / coverage manifest (future requirement)

This is not implemented. Every `broker_inventory_daily` capture will need one
manifest row with these fields:

| Field | Content |
|---|---|
| `capture_id` | Unique id of one fetch |
| `captured_at` | UTC start (and finish) of the fetch |
| `source`, endpoint and version | Where available |
| `instrument` | The requested ticker, and the echoed `meta.symbol` |
| `investor_type` | As requested |
| requested broker set | Exactly as sent (codes or selectors) |
| returned broker set | The brokers present in the payload |
| requested start / end | The requested session window |
| returned session range | First and last session actually returned, and their count |
| `scope_id`, scope version | §2.5 |
| status | Success or failure, with a failure class |
| raw evidence reference | Content hash and location of the raw response |
| schema version | The payload schema the parser accepted |
| normalization version | The version of the normalizer that produced the rows |

- **Precedent:** the market-summary source contract already keeps a manifest of
  this shape (`ms_capture_manifest` and `ms_raw_response` in
  `neobdm_source_contract.py`).
- **Partial precursor:** `broker_learning_raw/` envelopes keep `fetched_utc` and
  `meta`.
- **Storage constraint:** raw payloads are paid vendor data, and the repository
  is public. A raw evidence reference must point to non-public storage, or to a
  schema-allowlisted projection as `market_summary_raw_fragments/` does.

---

## 6. Legacy `broker_flow`

`broker_flow` lives in `neobdm.db`, with primary key `(date, ticker,
broker_code)` and columns `bval`, `sval`, `netval`, `bavg`, `savg`.

| | |
|---|---|
| **Design status** | **Designated legacy** (DS-D06). Frozen for new development: it must not be the basis of new broker-intelligence work. It stays available to reproduce past work (Experiments #1, #1E and #1F, and the walk-forward panel). |
| **Runtime state** | **Not frozen.** Its existing writers still run, temporarily, for compatibility. They are migration debt (see "Current runtime" below). |
| **Successor** | `broker_inventory_daily` (§3), for all new broker work. |

Its history mixes two incompatible writers:

| Aspect | `backfill_inventory.py` rows (`/api/inventory`; dates ≤ `BACKFILL_END` = 2026-07-04) | `neobdm_scraper.py` broker-stalker rows (live, from 2026-07-05) |
|---|---|---|
| Source semantics | Per-session series from the inventory API. Earlier rows came from chart extraction on the retired `/inventory/` page. Some were rescaled by `price_audit.py repair` ([`HANDOFF.md`](../HANDOFF.md) TEMUAN 1) | The stalker page's "Today" aggregate per broker and side (`get_netflow`) |
| Date | Session date from the payload | WIB calendar date of the scrape (`datetime.now`), not a verified session date |
| Units | `netval = nlot × 100 × close / 1e9` (billion Rupiah, lot-derived); `bval`, `sval`, `bavg`, `savg` NULL | `bval`, `sval`, `netval`, `bavg`, `savg` parsed from page display strings (`parse_num`) |
| Coverage | Depends on the selector or chart (currently `TOP_5_NB/NS_LOT_C20`), filtered to `BROKER_FLOW_CODES`; request set not recorded | `BROKER_FLOW_CODES` × {akum, dist}; a failed code or side is logged and skipped |

**Rules:**
- Do not rewrite or migrate its historical rows merely to make them conform to
  this contract.
- Do not base new analyses, features or broker-intelligence work on it.
- Do not union it with `broker_inventory_daily`.
- Legacy experiments treated a missing broker as zero captured flow
  ([`ML_NEXT_STEP_BROKER_IDENTITY_ROADMAP.md`](../ML_NEXT_STEP_BROKER_IDENTITY_ROADMAP.md)).
  That treatment belongs to those historical experiments and does not carry forward
  (§4).
- The broker-stalker page is a separate vendor view. It feeds the live
  `broker_flow` rows and the daily "retail jual" Telegram block. It stays a
  separate legacy product, conceptually distinct from `broker_inventory_daily`,
  even though its rows share the `broker_flow` table.

**Current runtime (migration debt; not changed here):**
- `price-history-topup.yml` runs `backfill_inventory.py` every night. That run
  re-fetches the rolling 360-day window and `INSERT OR REPLACE`s `broker_flow`
  rows dated ≤ 2026-07-04, for whichever brokers the selector returns that
  night. Until the window start passes 2026-07-04 (around the end of June 2027),
  historical `broker_flow` rows keep being replaced or added.
- The daily scrape keeps appending stalker-page rows
  (`save_daily_broker_flow` in `neobdm_scraper.py`).

The nightly rewrite also conflicts with DS-D11 (revision, not overwrite).
Retiring or aligning these writers is a separate runtime change that needs its
own review (§9).

---

## 7. `price_history` principles (future)

**Current state:**
- `price_history` lives in `neobdm.db`, with primary key `(date, ticker)` and
  OHLCV columns.
- `backfill_inventory.py` rewrites it every night from the `/api/inventory`
  `ohlc[]` with `INSERT OR REPLACE`.
- `price_audit.py repair` repairs it in place.
- `price_quarantine` flags suspect rows as a snapshot rebuilt on every run; the
  originals stay in `price_history`.
- There is no `captured_at`.
- `volume` is shares (§10).

These are the accepted principles for its future contract, applying DS-D11 to
prices. They are not implemented, and no replacement is built here.

1. `session_date` is separate from `captured_at`.
2. A source correction (the vendor restates a session) is a new revision, not an
   overwrite.
3. A DailyScraper repair is an explicit repair revision. It records what
   changed, why, the evidence and the repair version. Examples are
   corrections applied through `price_audit.py repair` or
   `volume_repair_ledger.json`.
4. A quarantine is a timestamped annotation on a revision. It is not a deletion,
   and not a snapshot that forgets its own history.
5. Legacy rows get no invented availability or capture timestamps. Unknown stays
   unknown.
6. "Current best" prices may become a derived view over revisions, not a
   destructive history.

---

## 8. Market summary principles

The market-summary source contract (`neobdm_source_contract.py`, contract v2)
already separates RAW, NORMALIZED, CANONICAL and DERIVED storage layers, and
keeps NULL distinct from 0. This section adds the provenance classes from §2.6.

| Class | Market-summary examples |
|---|---|
| `RAW_VENDOR_OBSERVATION` | Response bytes in `market_summary_raw_fragments/` (indexed in `ms_raw_response`); `close`, `high`, `low`, `tval` as returned |
| `VENDOR_COMPUTED` | NeoBDM screener metrics and labels: `m_dn_0` ("akum bandar"), `nr_dn_0` ("retail jualan"), `f_dn_0` (foreign), `m_cn_5`, `m_dn_3`, `top_5_buyer`, `clean_score`, `pct_5`, `market_cap_t` |
| `OUR_NORMALIZATION` | `market_summary_daily` rows, `ms_capture_manifest` presence facts, `source_field_lifecycle` |
| `OUR_INTERPRETATION` | `konglo_signal_watch` hits; the Top-2 screen; the `RETAIL_BROKERS`, `SMART_MONEY` and `BANDAR_GROUPS` classifications and the owner-proxy logic in `neobdm_scraper.py` |

**Rules:**
- Vendor fields and categories may be preserved as vendor facts, labelled as
  the vendor's.
- A vendor category ("bandar", "retail", "asing", a top-buyer list) never
  becomes a verified actor, owner or operator fact by being stored.
- Interpretation belongs downstream, in Market Intelligence or SPECTRA.
  **Current state:** the interpretation code in the last table row lives in the
  monolith, next to collection. This contract does not move it.
- `market_summary_daily.date` is the capture (scrape) date, not the session date
  ([`HANDOFF.md`](../HANDOFF.md) Lampiran E). `ms_capture_manifest` records
  `session_date` and `session_date_status` separately, consistent with §2.1.

---

## 9. Open questions

1. **Remaining `broker_flow` writers (migration debt).** When are the nightly
   top-up rewrite of rows ≤ 2026-07-04 and the live stalker appends retired or
   aligned (§6)? Either is a runtime change and needs review.
2. **Derived values.** Does DailyScraper publish `DERIVED` broker views, or do
   consumers compute them (§3.7)?
3. **Instrument identity.** How is identity kept across ticker renames,
   relistings and corporate actions? Basis breaks are currently handled in
   `observed_basis_factor.json` and [`BROKER_LEARNING.md`](../BROKER_LEARNING.md)
   §2.3.
4. **Calendar evidence.** Which exchange-calendar and instrument-status source
   will establish `NOT_TRADED` (§4)?
5. **Lot-size reference.** Where does it live, how is it versioned, and has the
   IDX board lot ever changed within the data's range (§3.4)?
6. **Value without lots.** What causes the 556 cells in §3.8? The odd-lot
   hypothesis is unverified.
7. **Inspection reproducibility.** The inspector `inspect_inventory_cache.py` is
   not tracked in this repository, and the caches it read are not tracked
   either (§10). Should the inspector be committed? Only the inspector would go
   in; the raw payloads stay out.
8. **Raw evidence storage.** Where can raw evidence for paid data live, given the
   public repository (§5)?

---

## 10. Contract evidence (2026-09-26 cache inspection)

This section is the provenance for the unit, session and coverage claims in §3
and §4. It is not a claim that every future vendor payload will behave the same
way. The fail-closed validation (§3.3) and the coverage states (§4) exist
because future payloads may not.

| | |
|---|---|
| Run date | 2026-09-26 |
| Inspector | `inspect_inventory_cache.py`: a read-only local inspector, not part of the production runtime, and not tracked in this repository |
| Inputs | Cached `/api/inventory` payloads: `broker_learning_raw/daily` (envelopes) and `inventory_raw/` (bare `data` dicts); both gitignored |
| Sample | 500 payloads, out of 1,086 candidate files |
| Sampling rule | The inspector sorted the candidate cache files by file modification time. Because the candidates exceeded `--max-files`, it inspected evenly spaced indices across that ordered list. Modification time was used only to spread the sample across the cached files. It is not market or point-in-time evidence (§2.1) |
| Cells | 9,573,982 broker-session cells |

**Verified:**

| Check | Result |
|---|---|
| `nlot = blot − slot` | 100% (9,573,982 / 9,573,982) |
| Lot cells are integers | 100%; 0 negative `blot`/`slot`; 0 explicit nulls |
| `nval = bval − sval` | 100% (9,573,982 / 9,573,982) |
| Full-Rupiah value: implied price `value / (lots × 100)` within session low/high ±2% | 98.43% of 4,217,822 tests; median implied price / close = 1 |
| Thousand / million / billion Rupiah candidates | 0% fit |
| Lot-size-1 control | 0% fit |
| Full-coverage sessions: OHLC `volume / Σ blot` | median 100 |
| Full-coverage sessions: `Σ bval / (volume × close)` | median 1 |
| Session dates | Ordered, no duplicates, weekdays only; aligned to OHLC in 500 / 500 |
| Explicit zeros | 7,019,501 returned cells with `blot = slot = nlot = 0`, most with zero values |
| Omissions | Observed; see the coverage table below |
| Value without lots | 556 cells with `bval > 0` and `blot = 0`; cause not verified (§3.8) |

**Coverage** (captures with a recoverable request set):

| Captures | Request set from | Result |
|---|---|---|
| 21 in `broker_learning_raw/daily` | Explicit, in payload metadata | 21 / 21 returned every requested broker |
| 479 in legacy `inventory_raw/` | Inferred from today's `broker_codes.json` | 423 returned all; 56 omitted at least one broker; 5,578 omitted broker slots in total |

**Limitations:**
- The 479 legacy `inventory_raw/` request sets were inferred from today's
  `broker_codes.json`, not from preserved historical request metadata.
- Only 21 payloads contained explicit request metadata.
- Bare `inventory_raw/` payloads have no capture timestamp.
- The raw paid vendor caches are not tracked with this documentation and must
  not be added to Git.
- The inspector (`inspect_inventory_cache.py`) is not currently tracked either.
- Another machine therefore cannot reproduce the exact run from the repository
  alone.
