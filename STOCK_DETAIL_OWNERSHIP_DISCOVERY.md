# NeoBDM Stock Detail — Ownership/Custody Data Discovery

Data-discovery pass only, per [[ML_NEXT_STEP_BROKER_IDENTITY_ROADMAP.md]]'s
instruction to investigate the Stock Detail page before starting Experiment #2.
**No model features implemented. No ownership/broker mapping hard-coded.**
Nothing here has been ingested into `neobdm.db`.

Verified live against `neobdm.tech` on 2026-08-30 in two passes. **Pass 1**
(BREN, PANI) established the endpoint coverage below. **Pass 2** (§5) added
TPIA, CDIA, GOTO, UNVR, BBCA, WIFI, ENRG specifically to stress-test the
pass-1 hypotheses that rested on n=1/n=2 — and **overturned one of them**
(§5.1). Discovery scripts left at repo root (untracked, following this
repo's existing `discover_*.py` convention): `discover_stock_detail.py`,
`discover_stock_detail_page.py`, `discover_stock_detail_ownership.py`,
`scratch_check_ticker.py`, `discover_stock_detail_batch2.py`. Raw captures:
`discovery_stock_detail*.json`, `scratch_ownership_panes*.json`,
`discovery_batch2.json`, `scratch_stock_detail_0_load.png` (full-page
screenshot).

**Read §5 before trusting §1.3's "foreign-only" framing or §1.2's XLSX-as-
source claim — both were revised after pass 2.**

---

## 1. Endpoint coverage report

### 1.1 The page itself

| | |
|---|---|
| URL | `GET https://neobdm.tech/stock_detail/<TICKER>/` (trailing slash optional, both 200) |
| Auth | Playwright session cookies from the existing `login()` in `neobdm_scraper.py` — same session already used for everything else |
| Response | Server-rendered Django template, **~1.1 MB of HTML per ticker**. All ownership content below is baked into this single response — confirmed by network capture across two full ticker loads: no XHR/fetch fires when switching KDA/PKDA tabs |
| Not found | No sidebar/nav link points here (the sidebar's identically-labelled "KDA 1%/PKDA 1%/KDA 5%/PKDA 5%" entries are a **different**, separate UI element — global anchors, not this page's tabs). Only found by directly probing `/stock_detail/<TICKER>/` after ~40 other path guesses 404'd |

This means: **no JSON API exists for any of KDA/PKDA/free-float/holder-count**.
Confirmed by ~40 direct `GET /api/...` probes (all 404 — see
`discovery_stock_detail.json.rest_probes`) plus request/response capture on
two full page loads. The only way to obtain this data programmatically is to
parse the rendered HTML of `stock_detail/<TICKER>/`, or to go around NeoBDM
entirely and fetch IDX's own filing (§1.5). This directly contradicts the
"prefer structured payloads over DOM scraping" default — noted, not overridden,
because the structured option doesn't exist here.

### 1.2 KDA 1% / PKDA 1% — official IDX ≥1% ownership-change disclosure

Two Bootstrap pill-tabs (`data-bs-toggle="pill"`) inside one card, DOM ids
`#insider-current` (KDA 1%, current snapshot) and `#insider-moves` (PKDA 1%,
change log). Both already present in the initial HTML.

**KDA 1% (`#insider-current`) — current-state table**

Columns: `Investor` (name + category label — pass 2 widened the observed set
well beyond pass 1's four: `Corporate`, `Individual`, `Bank`, `Securities
Company`, `Investment Advisors`, `Investment Manager`, `Private Bank`,
`Venture Capital`, `Sovereign Wealth Fund`, `State Owned Enterprises`,
`Private Equity`, `Mutual Funds`, `Trustee Bank`, `-`; still not confirmed
exhaustive — plus an "F" badge on some rows, see §5.1 for what that badge
does and does **not** predict) | `Kepemilikan` (%) | `Scrip` (lot + %) |
`Scripless` (lot + %). Header shows `Total Kepemilikan` (sum of listed
holders) and `Data per <DD mon YYYY>` plus a link labelled "XLSX".

**§5.2 correction: that XLSX link is not a reliable per-ticker source.** Pass
1 read it as "the source IDX filing" for BREN. Pass 2 fetched the *same*
link — `https://www.idx.co.id/Media/fjelt0en/peng-2026-07-00016-satu-persen.xlsx`
— on all 7 new tickers (spanning energy, tech, banking, telecom, and
petroleum, no shared issuer group) and it was byte-identical every time,
**9/9 tickers tested including BREN and PANI**. Nine unrelated issuers
sharing one IDX announcement number for the same period is not plausible as
a real per-issuer filing link; treat it as a broken/placeholder link on
NeoBDM's side unless proven otherwise. Direct fetch also returned **HTTP 403**
from `idx.co.id` on all 7 attempts (bot/hotlink protection, not tested via a
real browser navigation) — so even if the link were real, this session
couldn't inspect its contents or timestamp. **`source_xlsx_url` should not be
relied on for `published_at`.** BREN example, `Data per 31 jul 2026`, Total
Kepemilikan 93.4%:

```
BARITO PACIFIC       Corporate           64.6%   846M lot 63.3%   18.4M lot 1.4%
GREEN ERA ENERGY      Corporate F         22.7%   -                303M lot 22.7%
JUPITER TIGER HOLDINGS Mutual Funds F     3.1%    -                42.1M lot 3.1%
PRIME HILL FUND        Trustee Bank F     2.9%    -                38.9M lot 2.9%
```

Note Barito Pacific's stake is mostly still **scrip** (846M lot, 63.3%) with
only 18.4M lot (1.4%) scripless — a live example of the scrip/scripless split
the task flags: if that scrip block later converts, scripless % jumps with
zero real accumulation.

**PKDA 1% (`#insider-moves`) — change log**

Columns: `Tanggal` (date) | `Investor` | resulting `Kepemilikan` % | `Scrip`
delta | `Scripless` delta | `Catatan` (`Masuk PKDA 1%` / `Keluar PKDA 1%` when
an investor crosses the 1% line, else blank). Dates are **month-end**, one row
per investor per month that had any recorded lot movement — this is a
periodic (monthly) log, not a continuous transaction feed. BREN's pull (5
months, 2026-03-31 → 2026-07-31) shows the exact false-signal case the task
warned about:

```
2026-05-29  PRIME HILL FUND          3.0%   -   +40.6M lot   Masuk PKDA 1%
2026-05-29  ZHAOCAI PRIME HILL FUND  <1%    -   -41.4M lot   Keluar PKDA 1%
```

Same date, near-equal and opposite lot deltas, "Trustee Bank"/"F" on both
sides. This is almost certainly **the same beneficial owner re-registered
under a renamed custodian entity**, not one investor buying out another. A
naive parser reading `Catatan` literally ("investor X exited, investor Y
entered") would record fabricated turnover. **`investor_name` needs an
alias/entity-resolution layer before any change is trusted as real turnover**
— confirmed a second time for PANI: `ALPHA INVESTMENT` exits PKDA 1% the same
month `TREASURE VENTURE INVESTMENT` and others move, with no re-entry
visible, i.e. name churn shows up in this feed by itself, independent of any
real transaction.

### 1.3 KDA 5% / PKDA 5% — near-daily custodian/KSEI-participant breakdown

Two more pill-tabs, DOM ids `#insider5p-current` and `#insider5p-moves`.
**Structurally different from the 1% tabs, and on a different, much shorter
lag**: BREN's `#insider5p-current` says `Data per 27 aug 2026` (3 days behind
the scrape date) vs `#insider-current`'s `Data per 31 jul 2026` (one month
behind). **These two "as of" dates must never be treated as the same
snapshot.**

**Coverage is not "every ≥5% holder", and pass 1's "foreign-flagged (`F`)
holders only" guess was WRONG — corrected in pass 2 (§5.1).** The real
gating variable, confirmed against 8 holder-level examples across 5 tickers
with zero contradictions, is: **a holder's SCRIPLESS lot count is ≥5% of
shares outstanding** (not total ownership, not the foreign flag). This makes
structural sense once stated: KSEI's custody network only tracks
dematerialized (scripless) shares — a scrip-form block has no "custodian" to
report, so of course only the scripless tranche can ever get a per-custodian
breakdown, regardless of who owns it or where they're domiciled. Domestic
controlling families in this data happened to hold large legacy blocks in
scrip form (keeping their scripless tranche under 5% even at 60%+ total
ownership), which is why pass 1's n=2 sample looked like a foreign/domestic
split — see §5.1 for the full evidence.

**KDA 5% (`#insider5p-current`) — per-custodian breakdown of one holder**,
BREN/Green Era Energy (22.7% total):

```
Citibank 33.6%   Bank HSBC 33.0%   Bank DBS 21.6%   Deutsche Bank 5.6%
Bank HSBC 2.8%   Citibank 2.3%     AI 1.1%          Citibank 0.0%
```

These percentages sum to ~100% and are read as **share of the holder's own
(scripless) position** — **confirmed with high confidence in pass 2 (§5.3)**,
not just inferred from one example anymore: 7 independent holder-level
breakdowns across 5 tickers all summed to almost exactly 100.0%, which is
only arithmetically consistent with "% of the holder's own stake," not "% of
total shares outstanding" or "% of free float" (either of those would need to
sum to the holder's much-smaller company-wide %, not 100%). Note the two
separate "Bank HSBC" lines and three separate "Citibank" lines for Green Era
Energy: KSEI evidently tracks **multiple sub-accounts per custodian bank** as
distinct entries, not one row per bank.

**The `AI` coincidence generalizes far wider than one code.** Pass 2 found
the *same* breakdown table also populated with plain 2-letter codes for
**domestic** holders with no custodian-bank name at all: TPIA's Barito
Pacific breaks into `AF`/`DX`/`NI`; WIFI's Investasi Sukses Bersama into
`YB`/`DR`/`TP`; ENRG's Shima Global Kapital into `CP`/`DR`/`BR`/`XA`/`BQ`.
Every one of these — `AF, DX, NI, YB, DR, TP, CP, BR, XA, BQ`, plus the
original `AI` — is a 2-letter code of the exact shape used throughout
`neobdm_scraper.py`'s existing broker taxonomy (`DX`/`NI` are literally
`BANDAR_GROUPS["Prajogo"]`; `YB` is in `BANDAR_GROUPS["Hengky"]`). This
table mixes two different kinds of KSEI Participant — custodian banks
(Citibank, HSBC, DBS, Deutsche Bank) and securities companies/brokers (who
are themselves registered KSEI Participants and legitimately hold client
shares in sub-accounts) — in the same column, with no type indicator. **These
codes are almost certainly not the same fact as broker-flow trading
activity** — this is exactly the task's "KSEI participant/custodian is not
automatically the execution broker" warning, now seen on ~10 codes, not one.
`custody_participants.participant_type` (schema below) exists specifically so
this ambiguity is recorded per-label, not resolved by assumption — and this
needs to stay unresolved: seeing `DX` here says only that KSEI Participant
`DX` custodies these shares, nothing about who executed any trade in them.

**PKDA 5% (`#insider5p-moves`) — daily custodian move log**

Columns: `Tanggal` | `Investor` | `Perubahan` (one combined lot delta,
sometimes prefixed `Cust.`). Dates are **daily**, not monthly:

```
2026-04-08  GREEN ERA ENERGY  F   Cust. -3.5M lot
2025-10-13  GREEN ERA ENERGY  F   Cust. -20M lot
2025-10-13  GREEN ERA ENERGY  F   Cust. +20M lot     <- same-day, opposite sign
2025-10-03  GREEN ERA ENERGY  F   AI 3.19M lot        <- no "Cust." prefix
2025-10-03  GREEN ERA ENERGY  F   Cust. -91.9K lot
```

The `Cust.` prefix is the page's own explicit signal that a row is a
custodian-to-custodian transfer of the **same beneficial owner**, not a market
transaction — this is the cleanest anti-mislabeling signal found in the whole
discovery pass and should be preserved verbatim (`is_custodian_move`), not
inferred. Rows without the prefix (e.g. `AI 3.19M lot`) are ambiguous: could
be a genuine custody assignment naming the receiving participant, or something
else — flagged as unresolved.

### 1.4 Free float / scripless % / holder count

Plain text badges next to the *embedded* Balance Position Chart title inside
`stock_detail/<TICKER>/`:

```
BREN: [Scripless: 36.7%] [Free Float: 12.6%] [Holder: 43.6K]
PANI: [Scripless: 16.6%] [Free Float: 9.3%]  [Holder: 52.3K]
```

No dedicated element carries their own "as of" date. The Balance Position
chart's own accessibility text says its data runs to `2026-07-01` (monthly,
one month behind scrape date) — these three numbers are almost certainly the
latest point of that same monthly series, but this is **inferred, not
confirmed** — no explicit timestamp sits next to the badges themselves.
Holder count is pre-abbreviated by the page ("43.6K") — the true integer is
not recoverable from the UI; only ~3 significant figures are available.

### 1.5 Balance Position Chart — now fully captured (both surfaces)

Two ways to reach the same figure data:

1. Standalone page `https://neobdm.tech/balance_position_chart/`
   → `POST https://neobdm.tech/django_plotly_dash/app/bp_app/_dash-update-component`
   with body `{"output":"balance-position-chart.figure", "inputs":[{"id":"tick","property":"value","value":"<TICKER>"},{"id":"darkmode","property":"value","value":true}], ...}`
   → returns a full Plotly figure JSON. **This is the one clean structured
   payload found in this whole pass** — worth preferring over HTML parsing for
   this specific sub-feature.
2. Embedded inside `stock_detail/<TICKER>/` as a Highcharts instance (same
   numbers, different rendering library — not independently re-verified
   which JS calls feed it, but visually identical).

Only two Dash inputs exist (`tick`, `darkmode`) — **no date-range control**.
Every request returns the **full available history** (no way to page/filter
by date), currently **34 monthly points, 2023-10-31 → 2026-07-31** for BREN.
22 traces per response:

- 18 bar series = `{Lokal, Foreign} × {asuransi, korporat, dapen, bank,
  individual, reksadana, sekuritas, yayasan, lainnya}` (insurance, corporate,
  pension fund, bank, individual, mutual fund, brokerage, foundation, other),
  values in **absolute lots**.
- 4 line series = `%Retail`, `%Institusi`, `%Foreign`, `scripless` (all
  percentages).

Dates are **month-end**, one point per month, and — like §1.2/§1.4 — running
about one month behind the scrape date. `min_date_allowed`/`max_date_allowed`
were `null` for the `tick` dropdown, i.e. no declared floor.

**History floor confirmed per-ticker in pass 2 (§5.4), with a clean
explanation.** BREN's 2023-10-31 floor (34 points) is not a fixed dataset
limit — it's BREN's own IPO month (Barito Renewables listed on IDX in
October 2023). CDIA, a 2025 IPO, returned only **13 points, floor
2025-07-31** — again matching its own listing, not a shared constant. The
other 6 pass-2 tickers (TPIA, GOTO, UNVR, BBCA, WIFI, ENRG — all listed well
before 2023) uniformly floored at **2023-08-31, 36 points**, two months
earlier than BREN's own floor. Read together: NeoBDM/KSEI's underlying
dataset for this chart appears to start **2023-08-31** for any
already-listed company, and a ticker listed after that date floors at its
own listing month instead. High confidence now (7/7 tickers checked are
consistent with this rule, zero exceptions), though not tested against a
delisted-then-relisted ticker, which could behave differently.

### 1.6 Not found / ruled out

- No `/api/*` endpoint answered for stock-detail, company profile, KSEI,
  shareholder, holder, free-float, ownership, balance-position, KDA, PKDA, or
  scripless under any spelling tried (≈40 probes, `discovery_stock_detail.json`).
- Clicking a row in the `new-market-summary/` Tabulator grid does not open any
  detail view or fire new requests (§2 of `discovery_stock_detail.json`).
- The identically-labelled sidebar "KDA 1%/PKDA 1%/KDA 5%/PKDA 5%" links are
  **not** the in-page tabs; clicking them produced no XHR in either capture
  and their actual behavior (anchor scroll? filter? something else?) was not
  resolved — low priority since the in-page tabs already expose the same data
  more directly.
- `/stock_detail/<ticker>/`'s "Broker Summary" mini-panel (`POST
  /api/broker-summary`) *is* a real JSON endpoint, but it's the existing
  broker-flow feature already covered by `neobdm.db`, not ownership data —
  captured only incidentally.

---

## 2. Proposed SQLite schema (design only — nothing populated)

Seven tables: `ownership_snapshot`, `ownership_change`,
`custody_breakdown_snapshot`, `custody_participants`,
`balance_position_monthly`, `balance_position_summary_monthly`,
`float_holder_snapshot`.

Every date-bearing table carries **four** distinct point-in-time fields, not
two — collapsing them to "snapshot vs captured" understated the actual
leakage surface:

| Field | Meaning |
|---|---|
| `snapshot_date` / `change_date` | The economic/reporting date printed on the page (e.g. "Data per 31 jul 2026") — **when the fact was true**, not when it became knowable |
| `published_at` | Earliest **independently verifiable** public availability date — e.g. an IDX filing's own timestamp — filled **only** when such evidence exists; otherwise left NULL |
| `captured_at` | When this repo's scraper first observed the record — always known, always filled |
| `available_at` | The timestamp a model is actually allowed to condition on: `published_at` when it is independently verifiable, **else `captured_at`** |

A row discovered today about a fact dated a month or a year ago is not
retroactively knowable at that earlier date — `available_at` exists precisely
to block that mistake. `published_at` must never be filled by guessing or by
copying `snapshot_date` forward; it is populated only from an external,
checkable source (e.g. an XLSX's own creation/modification timestamp or an
HTTP `Last-Modified` header — see §6 in the second discovery pass below for
what's actually verifiable). See §3 for the full consequences.

```sql
-- KDA 1% / KDA 5% "current" tables — point-in-time composition
CREATE TABLE ownership_snapshot (
    id                      INTEGER PRIMARY KEY,
    ticker                  TEXT NOT NULL,
    threshold               TEXT NOT NULL CHECK (threshold IN ('1pct', '5pct')),
    snapshot_date           TEXT NOT NULL,   -- KDA "Data per ..." date, ISO
    investor_name_raw       TEXT NOT NULL,   -- exactly as rendered
    investor_name_canonical TEXT,            -- NULL until entity resolution exists (Experiment #2, not this pass)
    investor_category       TEXT,            -- 'Corporate' | 'Mutual Funds' | 'Trustee Bank' | 'Private Equity' | '-' ...
    is_foreign              INTEGER,         -- 0/1, from the 'F' badge
    ownership_pct_raw       TEXT,            -- keep raw ('< 1%' is not a number)
    ownership_pct           REAL,            -- NULL when raw is '< 1%' or similar
    scrip_lot               REAL,
    scrip_pct               REAL,
    scripless_lot           REAL,
    scripless_pct           REAL,
    source_xlsx_url         TEXT,            -- official IDX filing, when shown
    published_at            TEXT,            -- NULL unless independently verified (e.g. XLSX metadata / Last-Modified)
    captured_at             TEXT NOT NULL,   -- scrape wall-clock time
    available_at            TEXT NOT NULL,   -- = published_at if known, else captured_at -- the ONLY field a model may condition on
    UNIQUE (ticker, threshold, snapshot_date, investor_name_raw)
);

-- PKDA 1% / PKDA 5% "moves" tables — change log
CREATE TABLE ownership_change (
    id                      INTEGER PRIMARY KEY,
    ticker                  TEXT NOT NULL,
    threshold               TEXT NOT NULL CHECK (threshold IN ('1pct', '5pct')),
    change_date             TEXT NOT NULL,   -- month-end for 1pct, DAILY for 5pct -- do not assume cadence
    investor_name_raw       TEXT NOT NULL,
    investor_name_canonical TEXT,
    investor_category       TEXT,            -- present for 1pct rows; usually NULL for 5pct rows
    is_foreign              INTEGER,
    resulting_ownership_pct REAL,            -- 1pct only
    scrip_lot_change        REAL,            -- 1pct only
    scripless_lot_change    REAL,            -- 1pct only
    lot_change              REAL,            -- 5pct "Perubahan" (single combined delta)
    is_custodian_move       INTEGER,         -- 1 iff the page's own "Cust." prefix was present (5pct only) -- NEVER inferred, only copied
    custodian_or_code       TEXT,            -- e.g. 'Citibank', 'AI' -- raw label attached to a non-"Cust." 5pct move; identity unconfirmed
    note                    TEXT,            -- 'Masuk PKDA 1%' / 'Keluar PKDA 1%' / NULL
    source_xlsx_url         TEXT,
    published_at            TEXT,            -- NULL unless independently verified
    captured_at             TEXT NOT NULL,
    available_at            TEXT NOT NULL    -- = published_at if known, else captured_at
);

-- KDA 5% "current" per-custodian split of one holder's stake
CREATE TABLE custody_breakdown_snapshot (
    id                        INTEGER PRIMARY KEY,
    ticker                    TEXT NOT NULL,
    snapshot_date             TEXT NOT NULL,     -- KDA5% has its OWN "Data per ..." date, distinct from KDA1%'s
    investor_name_raw         TEXT NOT NULL,
    investor_total_pct        REAL,
    is_foreign                INTEGER,
    custodian_label           TEXT NOT NULL,     -- 'Citibank' / 'Bank HSBC' / 'AI' / ... raw, unresolved
    custodian_pct_of_holder   REAL,              -- % of the HOLDER's stake, NOT of shares outstanding (inferred, unconfirmed -- see §6 second-pass test)
    published_at              TEXT,
    captured_at               TEXT NOT NULL,
    available_at              TEXT NOT NULL     -- = published_at if known, else captured_at
);

-- Dimension table: every distinct label seen in custodian_label / custodian_or_code
CREATE TABLE custody_participants (
    label             TEXT PRIMARY KEY,   -- raw string exactly as it appears on the page
    participant_type  TEXT,              -- 'custodian_bank' | 'broker_code' | 'unknown' -- deliberately NOT auto-derived
    overlaps_broker_code TEXT,           -- e.g. 'AI' also exists in BROKER_FLOW_CODES -- record the coincidence, do not assert identity
    notes             TEXT
);

-- Balance Position Chart, monthly, per investor-category lots
CREATE TABLE balance_position_monthly (
    ticker       TEXT NOT NULL,
    period_date  TEXT NOT NULL,   -- month-end, from the chart's own x-axis value
    category     TEXT NOT NULL,   -- e.g. 'local_individual','foreign_korporat', ... (18 canonical values, see §1.5)
    lots         REAL,
    published_at TEXT,
    captured_at  TEXT NOT NULL,
    available_at TEXT NOT NULL,   -- = published_at if known, else captured_at
    PRIMARY KEY (ticker, period_date, category)
);

-- Balance Position Chart, monthly, the 4 percentage line-series
CREATE TABLE balance_position_summary_monthly (
    ticker         TEXT NOT NULL,
    period_date    TEXT NOT NULL,
    pct_retail     REAL,
    pct_institusi  REAL,
    pct_foreign    REAL,
    pct_scripless  REAL,
    published_at   TEXT,
    captured_at    TEXT NOT NULL,
    available_at   TEXT NOT NULL,   -- = published_at if known, else captured_at
    PRIMARY KEY (ticker, period_date)
);

-- Free float / holder count badges
CREATE TABLE float_holder_snapshot (
    ticker           TEXT NOT NULL,
    snapshot_date    TEXT NOT NULL,   -- INFERRED = latest balance_position_monthly.period_date; not independently timestamped on the page
    free_float_pct   REAL,
    scripless_pct    REAL,
    holder_count     REAL,            -- pre-abbreviated by the page (e.g. 43600 from "43.6K"); ~3 significant figures only
    published_at     TEXT,
    captured_at      TEXT NOT NULL,
    available_at     TEXT NOT NULL,   -- = published_at if known, else captured_at
    PRIMARY KEY (ticker, snapshot_date)
);
```

Design choices worth flagging explicitly:

- Every table keeps `*_raw` investor-name/category strings untouched and adds
  a separate nullable `*_canonical` column for later entity resolution — the
  raw string must survive because normalization logic will itself need to be
  reviewed (Green Era Energy / Green Era Energy PTE LTD / Green Era Energy
  Pte.Ltd. and Prime Hill Fund / Zhaocai Prime Hill Fund were both seen as
  probable same-entity renames within a single ticker's data in this pass).
- `is_custodian_move` is copied verbatim from the page's own `Cust.` prefix,
  never inferred — this is the single most reliable anti-mislabeling signal
  found and shouldn't be reconstructed from netting logic that could get it
  wrong.
- `custody_participants.overlaps_broker_code` exists purely to record the
  `AI` coincidence (and any future ones) without asserting it means anything.

---

## 3. Date / leakage risks

Four different "as of" cadences coexist on **one page**, and none of them
match the scrape date:

| Data | Cadence | Observed lag vs scrape date (2026-08-30) |
|---|---|---|
| KDA 1% / PKDA 1% | monthly | ~1 month (`Data per 31 jul 2026`) |
| KDA 5% / PKDA 5% current | near-daily, foreign holders only | ~3 days (`Data per 27 aug 2026`) |
| PKDA 5% moves log | daily | rows dated day-by-day, e.g. `2026-04-08` |
| Balance Position Chart / float / holder badges | monthly | ~1 month (chart's last x value `2026-07-01`) |

Consequences for any future modeling work:

1. **`snapshot_date`/`change_date` is not usable as the "available-as-of"
   date.** A row dated `2026-07-31` was not actually visible until sometime
   in August (the IDX filing itself is dated/published after month-end, and
   NeoBDM then has to scrape and render it). Any walk-forward split must use
   `available_at` (§2's four-field model) — which is `published_at` only when
   independently verified, and `captured_at` otherwise. A historical value
   discovered today (e.g. a five-month PKDA backlog pulled in one scrape) is
   **not** retroactively knowable at its own `snapshot_date` — every row in a
   first backfill pull effectively shares one `captured_at`, regardless of how
   far back its `snapshot_date` reaches, and `available_at` must reflect that
   rather than silently inheriting the older date. This is the same discipline
   already enforced for `price_history`/`broker_flow` via the pre-open
   scrape-date offset (HANDOFF Appendix E), extended to a source with a much
   longer, more variable, and only sometimes independently verifiable lag.
2. **The 1% and 5% families cannot be joined on date.** A pipeline that
   naively left-joins `ownership_snapshot` rows for the same ticker across
   `threshold` values by nearest date would silently mix a July 31 monthly
   figure with an August 27 near-daily figure and call it "same day."
3. **No table here declares a `min_date_allowed`.** Historical depth is
   whatever NeoBDM happens to render for a given ticker (2023-10-31 was
   BREN's earliest Balance Position point; not yet checked for other
   tickers, especially recently-listed ones — could be much shorter, and IPO
   tickers with <34 months of listing will not have monthly data before
   listing regardless of NeoBDM's own retention).
4. **The XLSX source is stronger ground truth than NeoBDM's rendering** for
   KDA 1%/PKDA 1% (it's the literal regulatory filing), but it's also
   per-filing, per-ticker, at an unpredictable IDX media URL that can only be
   discovered by first loading NeoBDM's page — there is no way to construct
   or enumerate these URLs independently, so IDX itself cannot fully replace
   scraping NeoBDM's rendering.
5. **Entity renames masquerading as turnover (§1.2, §1.4)** are a leakage
   risk in a different sense: a naive feature ("investor X newly entered
   PKDA 1% this month") could fire purely because of a custodian relabeling,
   producing a feature that looks predictive in-sample (correlated with
   nothing real) and fails out-of-sample, or worse, looks like a real
   accumulation signal that happens to correlate with price by coincidence in
   the fitting window.

---

## 4. Missing data / historical-depth limitations

Resolved in pass 2 (§5) — kept here only as a pointer, details there:

- ~~PKDA 1% log depth unconfirmed~~ → **resolved, high confidence**: fixed
  ~5-month window (floor 2026-03-31), identical across all 9 tickers tested
  regardless of how many holders/events each had. See §5.5.
- ~~KDA 5%/PKDA 5% coverage rule inferred from n=2~~ → **rule itself was
  wrong**; corrected to the scripless-≥5% rule, confirmed on 8 examples
  across 5 tickers, zero contradictions. See §5.1.
- ~~Custodian-percentage semantics inferred from magnitude~~ → **confirmed,
  high confidence**, 7 examples across 5 tickers all summing to ~100%. See
  §5.3.
- ~~Balance Position earliest date unverified across tickers~~ → **resolved,
  high confidence**: per-ticker floor = own IPO month if listed after
  2023-08-31, else a shared 2023-08-31 dataset floor. See §5.4.
- ~~Investor-category taxonomy incomplete (n=2)~~ → widened from 4 to 14
  distinct categories across the 9 tickers tested (§1.2); still not asserted
  exhaustive, but no longer resting on 2 tickers.

Still open after pass 2:

- **True holder count is unrecoverable.** The page only ever shows an
  abbreviated value ("43.6K"/"783K"); no endpoint returns the unrounded
  integer. Not addressed by pass 2 — no path found to recover it.
- **Free-float/scripless/holder badges' own "as of" date is still not
  independently verified.** Pass 2 did not find a new way to confirm they
  share the Balance Position chart's month-end date rather than some other
  cadence — still an inference, not a citation.
- **The IDX XLSX link cannot be used for `published_at`.** Confirmed broken
  as a per-ticker source (§1.2) and blocked (HTTP 403) as a direct fetch
  target from this session. No alternative independently-verifiable
  publication-date source was found for KDA 1%/PKDA 1% or Balance Position —
  in practice `available_at` will fall back to `captured_at` for nearly all
  rows unless a different verification path turns up later.
- **Rate-limit budget**: capturing this page for all 45 `TRACKED_TICKERS`
  costs one ~1.1 MB page load per ticker. Pass 2 ran 7 tickers sequentially
  with an explicit 9–13s jittered pause between each (`discover_stock_
  detail_batch2.py`) and hit no rate-limit errors; `harvest_inventory.py`'s
  existing PACE/JITTER/REST_EVERY pattern should still be reused, with
  similar or wider margins, for a full 45-ticker capture.
- **PKDA 5%'s `Cust.` prefix is not universal.** WIFI's entire move log
  (6 rows) carried plain broker/participant codes with zero `Cust.`-flagged
  rows, while ENRG and TPIA had a mix. This isn't a contradiction of
  anything claimed, just a reminder that `is_custodian_move = 0` will be the
  common case, not the exception, and shouldn't be read as "therefore this
  was a real market transaction" — it only means the page didn't apply its
  own custodian label to that particular row.

---

## 5. Second discovery pass — TPIA, CDIA, GOTO, UNVR, BBCA, WIFI, ENRG

Selected for structural diversity, not randomly: TPIA/CDIA (Prajogo group,
one of them a 2025 IPO), GOTO (widely-held tech co, many foreign VC/PE
holders), UNVR (single dominant foreign-parent holder, ~85%), BBCA (single
dominant domestic holder, ~55%), WIFI (Hashim group, smaller/less liquid),
ENRG (Bakrie group, history of debt-to-equity conversions). Captured with
`discover_stock_detail_batch2.py`, one page load per ticker, 9–13s jittered
pause between tickers, no retries. Raw output: `discovery_batch2.json`.

### 5.1 KDA 5%/PKDA 5% gating — foreign flag rejected, scripless-≥5% confirmed

Cross-tabulating every ≥5%-ish holder seen across all 9 tickers (pass 1 + 2)
against whether it got a KDA5% entry:

| Ticker | Holder | Total % | Scrip % | Scripless % | `F` flag | KDA5% entry? |
|---|---|---|---|---|---|---|
| BREN | Barito Pacific | 64.6% | 63.3% | 1.4% | no | **no** |
| BREN | Green Era Energy | 22.7% | – | 22.7% | yes | **yes** |
| PANI | Multi Artha Pratama | 83.8% | 83.4% | 0.3% | no | **no** |
| TPIA | Barito Pacific | 34.6% | 21.0% | 13.7% | no | **yes** (at 13.7%, matching scripless, not 34.6%) |
| TPIA | Bangkok Bank Public | 5.0% | – | 5.0% | yes | (present, not separately re-checked) |
| UNVR | Unilever Indonesia Holding | 85.0% | 85.0% | – (0%) | no | **no** |
| BBCA | Dwimuria Investama Andalan | 54.9% | 54.9% | – (0%) | no | **no** |
| WIFI | Investasi Sukses Bersama | 54.4% | – | 54.4% | no | **yes** |
| WIFI | Djoni (individual) | 5.3% | – | 5.3% | no | **yes** |
| ENRG | Shima Global Kapital | 17.5% | – | 17.5% | no | **yes** |
| GOTO | SVF Subco Singapore | 7.6% | – | 7.6% | yes | **yes** |
| GOTO | Taobao China Holding | 7.4% | – | 7.4% | yes | **yes** |

The `F` flag column and the KDA5%-entry column **disagree** on TPIA, WIFI,
and ENRG's biggest holders — all domestic (no `F`), all still got a KDA5%
breakdown. What predicts it perfectly across all 12 rows is **scripless % ≥
~5%**, independent of the `F` flag. Mechanism: KSEI's custody network only
holds dematerialized (scripless) shares, so a scrip-form block is definitionally
outside any custodian's book — there is nothing to break down. Domestic
founder/legacy blocks in this sample happened to sit mostly in scrip form
(BREN, PANI, and by a different mechanism UNVR/BBCA's parents at 0%
scripless), which is why pass 1's n=2 sample looked like a foreign/domestic
split. **Revise the schema/report's mental model accordingly: gate on
scripless lots, not on the `F` badge.**

### 5.2 IDX XLSX link — not a working per-ticker source

All 7 pass-2 tickers exposed the identical URL
`https://www.idx.co.id/Media/fjelt0en/peng-2026-07-00016-satu-persen.xlsx` —
the same one recorded for BREN and PANI in pass 1. 9/9 tickers, one URL, one
announcement number, spanning unrelated sectors and listing dates. Direct
`GET` on that URL returned **HTTP 403** from `idx.co.id` on all 7 attempts
(`content-type: text/html`, i.e. a block page, not the file) — plausibly bot/
hotlink protection rejecting a non-browser request context; not re-tested via
a full browser navigation. Net effect: **this link cannot currently supply
`published_at`**, either because it isn't really per-ticker or because it
can't be fetched from this session even if it were.

### 5.3 Custodian-percentage denominator — confirmed "% of holder's own position"

| Ticker | Holder | Custodian breakdown | Sum |
|---|---|---|---|
| BREN | Green Era Energy | Citibank 33.6, HSBC 33.0, DBS 21.6, Deutsche 5.6, HSBC 2.8, Citibank 2.3, AI 1.1, Citibank 0.0 | 100.0 |
| TPIA | Barito Pacific | AF 60.8, DX 22.3, NI 16.9 | 100.0 |
| WIFI | Investasi Sukses Bersama | YB 61.0, DR 35.0, TP 4.0 | 100.0 |
| ENRG | Shima Global Kapital | CP 77.8, DR 5.7, CP 5.6, BR 4.3, XA 3.5, BQ 2.5, BQ 0.6 | 100.0 |
| GOTO | SVF Subco Singapore | Deutsche Bank 100.0 | 100.0 |
| GOTO | Taobao China Holding | Citibank 100.0 | 100.0 |

7 independent breakdowns (6 shown fully; WIFI's Djoni also seen, partial),
5 tickers, **every one sums to 100.0%**. This is only arithmetically
consistent with "% of the holder's own (scripless) position" — the
"% of total shares outstanding" or "% of free float" readings would each need
to sum to the holder's much smaller company-wide %, not to 100. **Confidence
raised from "inferred, n=1" to high, n=7.**

### 5.4 Balance Position history floor — confirmed per-ticker, tied to listing

| Ticker | Points | Floor date | Notes |
|---|---|---|---|
| BREN | 34 | 2023-10-31 | IPO'd IDX Oct 2023 |
| CDIA | 13 | 2025-07-31 | 2025 IPO |
| TPIA, GOTO, UNVR, BBCA, WIFI, ENRG | 36 each | 2023-08-31 | all listed well before 2023 |

7/7 consistent with: floor = own listing month if listed after 2023-08-31,
else a shared 2023-08-31 dataset floor. High confidence; not tested against a
delisted-then-relisted ticker.

### 5.5 PKDA 1% historical depth — confirmed fixed ~5-month window

Earliest date seen in `#insider-moves` (PKDA 1%) across every ticker with any
rows at all: TPIA 2026-03-31, GOTO 2026-03-31, ENRG 2026-03-31, BREN
2026-03-31 (pass 1), PANI 2026-03-31 (pass 1), WIFI 2026-04-30, BBCA
2026-04-30, UNVR 2026-06-30 (only one event in the window). **Never earlier
than 2026-03-31, across 9/9 tickers**, regardless of how many rows a ticker
had (ENRG alone had 48 date mentions vs. UNVR's 2). This is a fixed trailing
window (~5 months as of the 2026-08-30 scrape), not a per-ticker artifact and
not a rendering/pagination limit that varies by data volume. High confidence.

### 5.6 Entity naming variation — cleaner, repeating example found

GOTO's PKDA 1% log shows the exact same custodian oscillating between two
spellings for its **static, unchanged 2.7% position**, three months running:

```
2026-04-30  UBS HONGKONG   2.7%  +320M lot  Masuk PKDA 1%
2026-04-30  UBS HONG KONG  <1%   -320M lot  Keluar PKDA 1%
2026-05-29  UBS HONG KONG  2.7%  +320M lot  Masuk PKDA 1%
2026-05-29  UBS HONGKONG   <1%   -320M lot  Keluar PKDA 1%
2026-06-30  UBS HONGKONG   2.7%  +320M lot  Masuk PKDA 1%
2026-06-30  UBS HONG KONG  <1%   -320M lot  Keluar PKDA 1%
```

Pure whitespace formatting difference ("UBS HONGKONG" vs "UBS HONG KONG"),
zero real position change, and the naive `Catatan` field says "Masuk"/"Keluar"
every single month. This is a stronger example than pass 1's Prime Hill
Fund / Zhaocai Prime Hill Fund case (which at least involved a genuine
custodian-registration change) — here it's simply inconsistent string
formatting for the identical label, recurring monthly. **This alone should be
treated as proof, not hypothesis, that raw investor/custodian names cannot be
used to detect entry/exit without normalization first.**

### 5.7 Genuine ownership-change event — partial

Within the fixed ~5-month PKDA 1% window (§5.5), GOTO shows several
foreign institutional/VC holders (CGS International Securities, Morgan
Stanley, GOTO Peopleverse Fund) each trimming small negative lot deltas
month over month — a real, gradual, multi-holder distribution pattern, not a
single dramatic event. GOTO's well-known larger 2023–2024 lockup-expiry
sell-downs predate this window and were not observed. **Objective only
partially met**: found a genuine (non-artifact) ownership-decreasing pattern
across independent unrelated holders, but not a single sharp corporate-action
event to use as a clean positive control. A true positive control would need
either a longer history window than the page exposes, or a ticker with a
known recent (within-5-month) tender offer/block trade — not identified in
this pass.

### 5.8 Free-float/holder badge date — still unverified

No new evidence either way. Not resolved by pass 2.

### Evidence table

| Hypothesis | n tickers checked | Supporting cases | Contradicting cases | Confidence |
|---|---|---|---|---|
| KDA5%/PKDA5% gates on foreign (`F`) flag | 9 (BREN, PANI, TPIA, CDIA, GOTO, UNVR, BBCA, WIFI, ENRG) | 4 (BREN, PANI both directions; GOTO's two `F` holders) | 3 (TPIA, WIFI, ENRG domestic holders got entries) | **Rejected** |
| KDA5%/PKDA5% gates on scripless lots ≥~5% | 9 | 12/12 holder-rows checked (§5.1 table) | 0 | **High** |
| Custodian % = share of holder's own position | 5 (BREN, TPIA, WIFI, ENRG, GOTO) | 7/7 breakdowns sum to 100.0% | 0 | **High** |
| Custodian % = share of company shares outstanding | 5 | 0 | 7/7 (would need to sum to holder's own %, not 100%) | **Rejected** |
| Balance Position floor = per-ticker listing / 2023-08-31 shared floor | 9 | 7/7 (BREN, CDIA distinct; other 6 uniform) | 0 | **High** |
| PKDA 1% window is a fixed ~5-month trailing log | 9 | 9/9 (never earlier than 2026-03-31) | 0 | **High** |
| IDX XLSX link is a real, fetchable per-ticker source | 9 | 0 | 9/9 (identical URL; 403 on fetch) | **Rejected** |
| Entity-name variation causes false Masuk/Keluar signals | 3 (BREN, PANI, GOTO) | 3/3 (Prime Hill Fund rename; Alpha Investment churn; UBS Hongkong/Hong Kong spelling oscillation) | 0 | **High** |
| Free-float/holder badges share Balance Position's snapshot date | 0 independently tested | 0 | 0 | **Untested** (still pure inference from co-location on the page) |
| A genuine (non-artifact) corporate ownership-change event is observable in PKDA 1% | 1 (GOTO) | 1 partial (gradual multi-holder distribution, no sharp single event) | 0 | **Low / inconclusive** |

---

## Stop condition

This is a data-discovery report only, covering two passes (9 tickers total).
No Experiment #2 features, no entity-resolution/alias logic, and no
ownership/owner-broker mapping have been written — §5.6 collects naming-
variation examples as evidence only, per instruction, with alias rules
deliberately left unproposed for a future, separately-reviewed step. Codes
seen in custodian breakdowns (`AF`, `DX`, `NI`, `YB`, `DR`, `TP`, `CP`, `BR`,
`XA`, `BQ`, `AI`, `CC`) are recorded as raw labels only; none has been
resolved into an execution-broker identity. Waiting for review before
proceeding.
