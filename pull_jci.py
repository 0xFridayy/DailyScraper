"""Pull JCI (IDX:COMPOSITE) daily bars via tvDatafeed (anonymous) -> jci_daily.csv"""
from tvDatafeed import TvDatafeed, Interval

tv = TvDatafeed()  # anonymous: fewer bars than logged-in, usually ~5000 daily
df = tv.get_hist(symbol="COMPOSITE", exchange="IDX",
                 interval=Interval.in_daily, n_bars=5000)
if df is None or df.empty:
    raise SystemExit("tvDatafeed returned nothing")
df = df.reset_index()
df["date"] = df["datetime"].dt.strftime("%Y-%m-%d")
out = df[["date", "open", "high", "low", "close", "volume"]]
out.to_csv("jci_daily.csv", index=False)
print(f"{len(out)} bars {out['date'].iloc[0]}..{out['date'].iloc[-1]} -> jci_daily.csv")
print(out.tail(3).to_string(index=False))
