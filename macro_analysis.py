"""Quick macro analysis: is the market in accumulation or distribution mode?"""
import os
import sqlite3
import pandas as pd

# The DB lives in this repo; an absolute path pinned it to one machine.
HERE = os.path.dirname(os.path.abspath(__file__))
con = sqlite3.connect(os.environ.get("NEOBDM_DB", os.path.join(HERE, "neobdm.db")))
bf = pd.read_sql('SELECT date, ticker, broker_code, netval FROM broker_flow ORDER BY date, ticker', con)
con.close()

# aggregate by date
by_date = bf.groupby('date')['netval'].sum().reset_index().rename(columns={'netval': 'total_net'})
print('=== BROKER FLOW BY DATE (all 31 tickers aggregated) ===')
print(by_date.to_string(index=False))

# by broker code (who's buying/selling)
smart = ['IF', 'TP', 'AZ', 'BB']
retail = ['XL', 'XC', 'YP', 'PD']
bf['category'] = bf['broker_code'].apply(lambda x: 'smart' if x in smart else ('retail' if x in retail else 'other'))
by_cat = bf.groupby(['date', 'category'])['netval'].sum().unstack(fill_value=0)
print('\n=== NET FLOW BY BROKER CATEGORY ===')
print(by_cat)

# summary stats
print(f'\n=== MACRO SUMMARY ===')
first_date_net = by_date[by_date['date'] == by_date['date'].min()]['total_net'].values[0]
last_date_net = by_date[by_date['date'] == by_date['date'].max()]['total_net'].values[0]
cum_net = by_date['total_net'].sum()

print(f'First date ({by_date["date"].min()}): {first_date_net:+.0f}B')
print(f'Last date ({by_date["date"].max()}): {last_date_net:+.0f}B')
print(f'Cumulative net (Jun 8 - Jul 6): {cum_net:+.0f}B')
print(f'Trend: {"UP (bullish, inflow)" if last_date_net > first_date_net else "DOWN (bearish, outflow)"}')
print()

# check smart vs retail
smart_total = bf[bf['category'] == 'smart']['netval'].sum()
retail_total = bf[bf['category'] == 'retail']['netval'].sum()
print(f'Smart brokers (IF/TP/AZ/BB) cumulative: {smart_total:+.0f}B')
print(f'Retail brokers (XL/XC/YP/PD) cumulative: {retail_total:+.0f}B')
print()

if cum_net > 0 and smart_total > 0:
    print('SIGNAL: Accumulation phase + smart money buying = BULLISH, patterns should work')
elif cum_net < 0 or smart_total < 0:
    print('SIGNAL: Distribution phase OR smart money exiting = BEARISH, skip patterns')
else:
    print('SIGNAL: Mixed = unclear, wait for clarity')
