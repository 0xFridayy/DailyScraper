"""Read-only PKDA-5% custodian-move audit for Experiment #2A0.
Frozen filter: threshold='5pct' AND is_custodian_move=1
              AND lot_change IS NOT NULL AND lot_change != 0
Joins entity_alias on investor_name_raw -> investor_name_canonical (entity_id)
for holder-level aggregation. Writes nothing."""
import collections, sqlite3, statistics

c = sqlite3.connect("neobdm_ownership.db")

W = ("threshold='5pct' AND is_custodian_move=1 "
     "AND lot_change IS NOT NULL AND lot_change != 0")

# Sanity: base row count and signed breakdown
n_total = c.execute(f"SELECT COUNT(*) FROM ownership_change WHERE {W}").fetchone()[0]
n_pos = c.execute(f"SELECT COUNT(*) FROM ownership_change WHERE {W} AND lot_change>0").fetchone()[0]
n_neg = c.execute(f"SELECT COUNT(*) FROM ownership_change WHERE {W} AND lot_change<0").fetchone()[0]
print(f"frozen-filter rows: {n_total}  (pos={n_pos}, neg={n_neg})")
print()

# 1. unique (ticker, change_date) primary events
n_prim = c.execute(
    f"SELECT COUNT(*) FROM (SELECT DISTINCT ticker, change_date "
    f"FROM ownership_change WHERE {W})"
).fetchone()[0]
print(f"1. unique (ticker, change_date) primary events: {n_prim}")

# 2. unique (ticker, investor_name_canonical, change_date) holder events
n_hold = c.execute(
    f"SELECT COUNT(*) FROM ("
    f"  SELECT DISTINCT oc.ticker, COALESCE(ea.entity_id, oc.investor_name_raw), oc.change_date "
    f"  FROM ownership_change oc "
    f"  LEFT JOIN entity_alias ea ON ea.name_raw = oc.investor_name_raw "
    f"  WHERE {W.replace('lot_change','oc.lot_change').replace('threshold','oc.threshold').replace('is_custodian_move','oc.is_custodian_move')}"
    f")"
).fetchone()[0]
print(f"2. unique (ticker, entity_id-or-raw, change_date) holder events: {n_hold}")

# 3. primary-event count per ticker
per_ticker = c.execute(
    f"SELECT ticker, COUNT(DISTINCT change_date) "
    f"FROM ownership_change WHERE {W} GROUP BY ticker ORDER BY 2 DESC"
).fetchall()
print(f"3. primary-event count per ticker ({len(per_ticker)} tickers):")
for t, n in per_ticker:
    print(f"   {t:>6s}: {n}")

# 4. top-1 / top-5 ticker concentration
sorted_t = sorted(per_ticker, key=lambda x: x[1], reverse=True)
top1 = sorted_t[0][1]
top5 = sum(n for _, n in sorted_t[:5])
print()
print(f"4. top-1 ticker: {sorted_t[0][0]} with {top1} events "
      f"({top1/n_prim*100:.1f}% of {n_prim})")
print(f"   top-5 tickers: {top5} events ({top5/n_prim*100:.1f}%)")
print(f"   top-5: {', '.join(t for t,_ in sorted_t[:5])}")

# 5. primary-event count per date
per_date = c.execute(
    f"SELECT change_date, COUNT(DISTINCT ticker) "
    f"FROM ownership_change WHERE {W} GROUP BY change_date ORDER BY 1"
).fetchall()
print()
print(f"5. primary-event count per date ({len(per_date)} dates):")
print(f"   min per date: {min(n for _,n in per_date)}")
print(f"   max per date: {max(n for _,n in per_date)}")
print(f"   mean: {sum(n for _,n in per_date)/len(per_date):.2f}")
print(f"   first 5 dates: {[(d,n) for d,n in per_date[:5]]}")
print(f"   last 5 dates:  {[(d,n) for d,n in per_date[-5:]]}")

# 6. dates with >1 ticker-event
multi = [(d, n) for d, n in per_date if n > 1]
print()
print(f"6. dates with >1 ticker-event: {len(multi)} of {len(per_date)}")
print(f"   {multi[:15]}{' ...' if len(multi)>15 else ''}")

# 7. holder-events collapsed per ticker-event: mean, median, max
#    For each (ticker, change_date) compute COUNT(DISTINCT entity)
holder_per_event = c.execute(
    f"SELECT ticker, change_date, "
    f"       COUNT(DISTINCT COALESCE(ea.entity_id, oc.investor_name_raw)) AS h "
    f"FROM ownership_change oc "
    f"LEFT JOIN entity_alias ea ON ea.name_raw = oc.investor_name_raw "
    f"WHERE {W.replace('lot_change','oc.lot_change').replace('threshold','oc.threshold').replace('is_custodian_move','oc.is_custodian_move')} "
    f"GROUP BY ticker, change_date"
).fetchall()
counts = [h for _, _, h in holder_per_event]
print()
print(f"7. holder-events collapsed per ticker-event ({len(holder_per_event)} ticker-events):")
print(f"   mean:   {statistics.mean(counts):.4f}")
print(f"   median: {statistics.median(counts):.4f}")
print(f"   max:    {max(counts)}")
print(f"   distribution of holder count:")
for h, freq in sorted(collections.Counter(counts).items()):
    print(f"     {h} holder(s): {freq} ticker-events")

# 8. ticker-events containing both positive and negative holder signs
mixed_sql = (
    f"SELECT ticker, change_date, "
    f"  SUM(CASE WHEN lot_change>0 THEN 1 ELSE 0 END) AS npos, "
    f"  SUM(CASE WHEN lot_change<0 THEN 1 ELSE 0 END) AS nneg "
    f"FROM ownership_change WHERE {W} "
    f"GROUP BY ticker, change_date"
)
mixed_rows = c.execute(mixed_sql).fetchall()
mixed = [(t, d, p, n) for t, d, p, n in mixed_rows if p > 0 and n > 0]
print()
print(f"8. ticker-events with BOTH positive and negative holder signs: {len(mixed)}")
for t, d, p, n in mixed[:20]:
    print(f"   {t} {d}: +{p} pos, -{n} neg")
if len(mixed) > 20:
    print(f"   ... and {len(mixed)-20} more")
