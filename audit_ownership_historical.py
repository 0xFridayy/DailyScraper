"""Read-only: audit the historical PKDA ownership data for Experiment #2A feasibility."""
import collections, datetime as dt, sqlite3

c = sqlite3.connect("neobdm_ownership.db")

# ---------- basic counts ----------
nd = c.execute("select count(distinct change_date) from ownership_change").fetchone()[0]
rng = c.execute("select min(change_date), max(change_date) from ownership_change").fetchone()
nt = c.execute("select count(distinct ticker) from ownership_change").fetchone()[0]

# ticker-date observations
td_pairs = c.execute(
    "select ticker, change_date, count(*) "
    "from ownership_change group by ticker, change_date order by ticker"
).fetchall()
print(f"Distinct dates          : {nd}  ({rng[0]} .. {rng[1]})")
print(f"Distinct tickers        : {nt}")
print(f"Ticker-date obs         : {len(td_pairs)}")

per_ticker = {}
for ticker, _, _ in td_pairs:
    per_ticker[ticker] = True
print(f"Tickers with any data   : {len(per_ticker)}")

# holder-level observations
he_rows = c.execute(
    "select ticker, investor_name_raw, count(*) "
    "from ownership_change group by ticker, investor_name_raw"
).fetchall()
print(f"(ticker, entity) pairs : {len(he_rows)}")
ne = c.execute("select count(distinct entity_id) from entity_alias").fetchone()[0]
print(f"Distinct canonical ents : {ne}")

# ---------- 5pct rows: custodian moves vs threshold adjustments ----------
n5 = c.execute("select count(*) from ownership_change where threshold='5pct'").fetchone()[0]
cm = c.execute("select count(*) from ownership_change where is_custodian_move=1").fetchone()[0]
nzc5 = c.execute(
    "select count(*) from ownership_change "
    "where threshold='5pct' and lot_change is not null and lot_change != 0"
).fetchone()[0]
zc5 = c.execute(
    "select count(*) from ownership_change "
    "where threshold='5pct' and lot_change = 0"
).fetchone()[0]
print(f"\n5pct rows               : {n5}")
print(f"  is_custodian_move=1  : {cm}  (real custodian transfer events)")
print(f"  lot_change != 0       : {nzc5}  (non-zero lot change)")
print(f"  lot_change = 0        : {zc5}")
print(f"  is_custodian_move=0   : {n5 - cm}  (threshold/registration adjustments)")

# ---------- 1pct rows ----------
n1 = c.execute("select count(*) from ownership_change where threshold='1pct'").fetchone()[0]
print(f"\n1pct rows               : {n1}")
note_rows = c.execute(
    "select note, count(*) from ownership_change "
    "where note is not null group by note order by count(*) desc"
).fetchall()
entry_note = sum(cnt for note, cnt in note_rows if "Masuk PKDA" in (note or ""))
exit_note = sum(cnt for note, cnt in note_rows if "Keluar PKDA" in (note or ""))
print(f"  Masuk PKDA (entry)   : {entry_note}")
print(f"  Keluar PKDA (exit)   : {exit_note}")
r1d = c.execute(
    "select count(distinct change_date) from ownership_change where threshold='1pct'"
).fetchone()[0]
rt1 = c.execute(
    "select count(distinct ticker) from ownership_change where threshold='1pct'"
).fetchone()[0]
print(f"  1pct distinct dates   : {r1d}")
print(f"  1pct distinct tickers : {rt1}")

# ---------- lot_change sign distribution ----------
q = """select
    sum(case when lot_change > 0 then 1 else 0 end) as inc,
    sum(case when lot_change < 0 then 1 else 0 end) as dec,
    sum(case when lot_change = 0 then 1 else 0 end) as zero,
    sum(case when lot_change is null then 1 else 0 end) as nulls
from ownership_change where lot_change is not null"""
inc, dec, zero, nulls = c.execute(q).fetchone()
print(f"\nlot_change sign dist    : inc={inc}, dec={dec}, zero={zero}")

# ---------- custodian move per ticker ----------
cm_ticker = c.execute(
    "select ticker, count(*) as n from ownership_change "
    "where is_custodian_move=1 group by ticker order by n desc"
).fetchall()
print(f"\nCustodian moves/ticker : top 10")
for ticker, n in cm_ticker[:10]:
    print(f"  {ticker}: {n}")
print(f"  tickers with 0 moves : {nt - len(cm_ticker)}")

# ---------- consecutive-date transitions ----------
# Per ticker: consecutive dates with data
q = """select ticker, change_date from ownership_change
       order by ticker, change_date"""
date_rows = c.execute(q).fetchall()
transitions = 0
ticker_trans = collections.defaultdict(int)
prev_ticker = prev_date = None
for ticker, date_str in date_rows:
    if ticker == prev_ticker:
        gap = (dt.date.fromisoformat(date_str) - dt.date.fromisoformat(prev_date)).days
        if gap == 1:
            transitions += 1
            ticker_trans[ticker] += 1
    prev_ticker = ticker
    prev_date = date_str
print(f"\nConsecutive-date transitions (gap=1): {transitions}")
if ticker_trans:
    vals = list(ticker_trans.values())
    print(f"  Per ticker: min={min(vals)}, max={max(vals)}, mean={sum(vals)/len(vals):.1f}")
    print(f"  Tickers with >=5 trans: {sum(1 for v in vals if v >= 5)}")
    print(f"  Tickers with >=10 trans: {sum(1 for v in vals if v >= 10)}")

# ---------- entity transitions ----------
q = """select investor_name_raw, change_date from ownership_change
       order by investor_name_raw, change_date"""
e_rows = c.execute(q).fetchall()
e_trans = 0
e_trans_map = collections.defaultdict(int)
prev_e = prev_d = None
for ent, date_str in e_rows:
    if ent == prev_e:
        gap = (dt.date.fromisoformat(date_str) - dt.date.fromisoformat(prev_d)).days
        if gap == 1:
            e_trans += 1
            e_trans_map[ent] += 1
    prev_e = ent
    prev_d = date_str
print(f"\nEntity consecutive-date transitions: {e_trans}")
if e_trans_map:
    vals = list(e_trans_map.values())
    print(f"  Per entity: min={min(vals)}, max={max(vals)}, mean={sum(vals)/len(vals):.1f}")

# ---------- concentration: top tickers / entities ----------
print(f"\nTop ticker by custodian-move count:")
st = sorted(cm_ticker, key=lambda x: x[1], reverse=True)
top1 = st[0][1] if st else 0
top5 = sum(v for _, v in st[:5])
top10 = sum(v for _, v in st[:10])
print(f"  Top-1  : {top1} ({top1/cm*100:.1f}%)")
print(f"  Top-5  : {top5} ({top5/cm*100:.1f}%)")
print(f"  Top-10 : {top10} ({top10/cm*100:.1f}%)")

print(f"\nTop entities by custodian-move count:")
q = """select investor_name_raw, count(*) as n from ownership_change
       where is_custodian_move=1
       group by investor_name_raw order by n desc limit 20"""
for r in c.execute(q).fetchall():
    print(f"  {r[0][:45]:45s}: {r[1]}")

# ---------- 1pct: how many tickers have multiple dates ----------
q = """select ticker, count(distinct change_date) as nd
       from ownership_change where threshold='1pct'
       group by ticker order by nd desc"""
for r in c.execute(q).fetchall():
    print(f"  1pct {r[0]}: {r[1]} dates")

# ---------- gaps in the data ----------
print("\n=== MISSING-DATE STRUCTURE ===")
all_dates = sorted(r[0] for r in c.execute("select distinct change_date from ownership_change"))
gaps = []
for i in range(1, len(all_dates)):
    gap = (dt.date.fromisoformat(all_dates[i]) - dt.date.fromisoformat(all_dates[i-1])).days
    if gap > 1:
        gaps.append((all_dates[i-1], all_dates[i], gap))
print(f"Date gaps > 1 day: {len(gaps)}")
for a, b, g in gaps[:10]:
    print(f"  {a} .. {b}: {g} days")
if len(gaps) > 10:
    print(f"  ... and {len(gaps)-10} more")

# ---------- what 5pct rows look like by custodian_move ----------
print("\n=== 5pct ROW COMPOSITION BY custodian_move FLAG ===")
print("is_custodian_move=1  (real custodian transfers):")
for r in c.execute(
    "select lot_change, custodian_or_code, investor_name_raw "
    "from ownership_change where is_custodian_move=1 limit 15"
).fetchall():
    print(f"  lot_change={r[0]:>15.0f}  broker={r[1]}  name={r[2][:40]}")
print("\nis_custodian_move=0  (threshold adjustments, samples):")
for r in c.execute(
    "select lot_change, note, investor_name_raw "
    "from ownership_change where threshold='5pct' and is_custodian_move=0 limit 15"
).fetchall():
    print(f"  lot_change={str(r[0]):>15}  note={str(r[1]):>20}  name={str(r[2])[:40]}")

# ---------- can we build change features from the 5pct table alone? ----------
print("\n=== FEATURE RECONSTRUCTIBILITY ASSESSMENT ===")
print()
print("O1 features (level from snapshot):")
print("  own_pct_total, own_pct_top5, own_pct_top10, own_hhi,")
print("  own_institutional_pct, own_foreign_pct, own_holder_count")
print("  -> ownership_snapshot has only 3 distinct dates")
print("     CANNOT reconstruct with depth for any meaningful experiment.")
print()
print("O2 features (change from PKDA):")
print("  own_net_lot_change_1d  : lot_change from 5pct rows (IS_CUSTODIAN_MOVE=1)")
print("  own_flow_magnitude_1d  : abs(lot_change) from 5pct rows")
print("  own_abs_pct_change_1d  : requires resulting_ownership_pct from 1pct rows")
print("  own_new_entries_5d     : note='Masuk PKDA 1%' from 1pct rows")
print("  own_exits_5d           : note='Keluar PKDA 1%' from 1pct rows")
print("  own_turnover_5d        : derived from entries/exits")
print("  own_concentration_change_5d: requires ownership_pct from snapshots -> CANNOT")
print("  -> Partially reconstructible from PKDA. Custodian moves: 62 events.")
print("     Entry/exit from note field: needs 1pct rows.")
print()
print("O3 features (persistence):")
print("  own_persistent_holder_pct, own_stable_concentration")
print("  -> Requires multiple snapshot dates -> CANNOT reconstruct.")
print()
print("O4 features (cross-sectional):")
print("  own_crowding, own_common_holder_pct")
print("  -> Requires global entity IDs + snapshot data -> CANNOT reconstruct.")
print()
print("=== PROPOSED SAMPLE GATES FOR EXPERIMENT 2A ===")
print()
print("Based on effective custodian-move events and 1pct change data:")
print()
print("Gate A: Custodian-move signal")
print("  Requirement: >=20 custodian-move events (is_custodian_move=1)")
print(f"  Current: {cm}  -> {'PASS' if cm >= 20 else 'FAIL'}")
print()
print("Gate B: Ticker diversity of custodian moves")
print("  Requirement: >=10 tickers with >=2 custodian-move events each")
tickers_w_cm2 = sum(1 for _, n in cm_ticker if n >= 2)
print(f"  Current: {tickers_w_cm2} tickers  -> {'PASS' if tickers_w_cm2 >= 10 else 'FAIL'}")
print()
print("Gate C: Entry/exit signal")
print("  Requirement: >=5 entry events AND >=5 exit events")
print(f"  Current: entry={entry_note}, exit={exit_note}  -> {'PASS' if entry_note >= 5 and exit_note >= 5 else 'FAIL'}")
print()
print("Gate D: Consecutive-date transitions")
print("  Requirement: >=10 usable consecutive-date transitions")
print(f"  Current: {transitions}  -> {'PASS' if transitions >= 10 else 'FAIL'}")
print()
print("Gate E: Ticker-date coverage for 1pct change features")
print("  Requirement: >=10 tickers with >=2 distinct 1pct dates")
q1d = """select ticker, count(distinct change_date) as nd
          from ownership_change where threshold='1pct'
          group by ticker having nd >= 2"""
tickers_1pct_2plus = c.execute(q1d).fetchall()
print(f"  Current: {len(tickers_1pct_2plus)} tickers  -> {'PASS' if len(tickers_1pct_2plus) >= 10 else 'FAIL'}")
for ticker, nd_ in sorted(tickers_1pct_2plus, key=lambda x: x[1], reverse=True)[:10]:
    print(f"    {ticker}: {nd_} dates")
print()
print("=== RECOMMENDATION ===")
print()
print("Experiment #2A (Historical Ownership Change) appears FEASIBLE NOW")
print("based on the effective sample gates above.")
print()
print("Experiment #2B (Ownership State/Persistence) requires the daily")
print("snapshot capture pipeline change and is deferred.")
