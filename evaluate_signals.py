"""
Does the telebot entry signal actually work?

Joins konglo_signal_watch (what was flagged, by which source) against
market_summary_daily (the daily panel of ~200 liquid non-gorengan names) and
measures forward performance of signalled names against the unsignalled rest of
the same universe on the same days.

Timing is deliberately pessimistic: a signal is computed from end-of-day data on
day D, so the earliest honest entry is the CLOSE of D+1. Entering at D's close
would be look-ahead and would flatter every number in here.

Per horizon h (trading days after entry) it reports:
  ret    close-to-close return from the entry close to the close h days later
  MFE    max favourable excursion  (best high between entry+1 and exit)
  MAE    max adverse excursion     (worst low  between entry+1 and exit)
The control group is every panel name NOT signalled that day, run through the
identical math -- so "signal returned +2%" only means something next to what the
market did over the same window.

Run:  py evaluate_signals.py            -> print report
      py evaluate_signals.py --telegram -> also send it to the bot
"""

import os
import sqlite3
import statistics
import sys
from collections import defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "neobdm.db")
HORIZONS = (1, 3, 5, 10)
MIN_SIGNALS = 30          # below this, report "insufficient data" rather than noise


def _load_dotenv():
    """Same minimal .env loader as neobdm_scraper -- the Telegram secrets live in
    a git-ignored .env, not in the user/machine environment, so a scheduled run
    would otherwise have no credentials. Importing the scraper for this would
    drag in Playwright, so it is duplicated deliberately."""
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# ── data ──────────────────────────────────────

def load_panel(conn):
    """(date, ticker) -> (close, high, low), plus the sorted list of panel dates."""
    panel, dates = {}, set()
    for date, ticker, close, high, low in conn.execute(
            "select date, ticker, close, high, low from market_summary_daily "
            "where close is not null"):
        panel[(date, ticker)] = (close, high, low)
        dates.add(date)
    return panel, sorted(dates)


def load_signals(conn):
    """[(flag_date, ticker, [sources])] -- one row per flagged ticker per day."""
    try:
        rows = conn.execute(
            "select flag_date, ticker, sources from konglo_signal_watch "
            "order by flag_date, ticker").fetchall()
    except sqlite3.OperationalError:
        return []
    return [(d, t, (s or "").split(",")) for d, t, s in rows]


# ── measurement ───────────────────────────────

def outcome(panel, dates, ticker, signal_date, horizon):
    """Return (ret, mfe, mae) in %, entering at the close of the day AFTER the
    signal and exiting `horizon` trading days later. None if the window is not
    fully covered by the panel."""
    try:
        i = dates.index(signal_date)
    except ValueError:
        return None
    entry_i, exit_i = i + 1, i + 1 + horizon
    if exit_i >= len(dates):
        return None

    entry = panel.get((dates[entry_i], ticker))
    exit_ = panel.get((dates[exit_i], ticker))
    if not entry or not exit_:
        return None
    entry_close = entry[0]
    if not entry_close:
        return None

    highs, lows = [], []
    for j in range(entry_i + 1, exit_i + 1):
        bar = panel.get((dates[j], ticker))
        if not bar:
            return None          # gap in the window -> don't guess
        _c, h, l = bar
        if h is not None:
            highs.append(h)
        if l is not None:
            lows.append(l)

    ret = (exit_[0] - entry_close) / entry_close * 100
    mfe = (max(highs) - entry_close) / entry_close * 100 if highs else None
    mae = (min(lows) - entry_close) / entry_close * 100 if lows else None
    return ret, mfe, mae


def summarise(samples):
    """samples: list of (ret, mfe, mae)."""
    if not samples:
        return None
    rets = [s[0] for s in samples]
    mfes = [s[1] for s in samples if s[1] is not None]
    maes = [s[2] for s in samples if s[2] is not None]
    return {
        "n": len(rets),
        "mean": statistics.fmean(rets),
        "median": statistics.median(rets),
        "hit": sum(1 for r in rets if r > 0) / len(rets) * 100,
        "mfe": statistics.fmean(mfes) if mfes else float("nan"),
        "mae": statistics.fmean(maes) if maes else float("nan"),
    }


def evaluate(conn):
    panel, dates = load_panel(conn)
    signals = load_signals(conn)

    flagged = defaultdict(set)                 # date -> {ticker}
    for d, t, _s in signals:
        flagged[d].add(t)

    by_source = defaultdict(lambda: defaultdict(list))   # source -> h -> samples
    overall = defaultdict(list)                          # h -> samples
    control = defaultdict(list)                          # h -> samples
    uncovered = [t for d, t, _s in signals if (d, t) not in panel]

    for d, t, sources in signals:
        for h in HORIZONS:
            o = outcome(panel, dates, t, d, h)
            if o is None:
                continue
            overall[h].append(o)
            for s in sources:
                if s:
                    by_source[s][h].append(o)

    panel_dates = set(dates)
    for (d, t) in panel:
        if d not in panel_dates or t in flagged.get(d, ()):
            continue
        for h in HORIZONS:
            o = outcome(panel, dates, t, d, h)
            if o is not None:
                control[h].append(o)

    return {
        "dates": dates,
        "n_signal_rows": len(signals),
        "uncovered": uncovered,
        "overall": {h: summarise(v) for h, v in overall.items()},
        "control": {h: summarise(v) for h, v in control.items()},
        "by_source": {s: {h: summarise(v) for h, v in hs.items()}
                      for s, hs in by_source.items()},
    }


# ── report ────────────────────────────────────

def _line(tag, s, ctl):
    if not s:
        return f"  {tag:<5} no data"
    edge = f"{s['mean'] - ctl['mean']:+.2f}" if ctl else "  n/a"
    return (f"  {tag:<5} n={s['n']:<4} ret {s['mean']:+.2f}% "
            f"(med {s['median']:+.2f}%)  win {s['hit']:.0f}%  "
            f"MFE {s['mfe']:+.1f}% MAE {s['mae']:+.1f}%  vs mkt {edge}%")


def format_report(res):
    dates = res["dates"]
    out = ["📈 Signal Evaluation — telebot entry strategy"]
    if not dates:
        return "\n".join(out + ["", "No panel data yet."])

    out.append(f"panel {dates[0]} → {dates[-1]}  ({len(dates)} trading days)")
    out.append(f"signal rows: {res['n_signal_rows']}")
    if res["uncovered"]:
        out.append(f"⚠️ {len(res['uncovered'])} signalled name(s) missing from the "
                   f"panel — not measurable")

    measured = max((s["n"] for s in res["overall"].values() if s), default=0)
    if measured < MIN_SIGNALS:
        out += ["", f"⏳ Only {measured} measurable signal-outcomes so far "
                    f"(need ≥{MIN_SIGNALS}).", "Too early to call — still accumulating."]
        return "\n".join(out)

    out += ["", "ALL SIGNALS vs market (same days, same universe)"]
    for h in HORIZONS:
        out.append(_line(f"{h}d", res["overall"].get(h), res["control"].get(h)))

    out += ["", "MARKET (unsignalled control)"]
    for h in HORIZONS:
        out.append(_line(f"{h}d", res["control"].get(h), None))

    for src in sorted(res["by_source"]):
        stats = res["by_source"][src]
        if not any(stats.values()):
            continue
        out += ["", f"BY SOURCE — {src}"]
        for h in HORIZONS:
            out.append(_line(f"{h}d", stats.get(h), res["control"].get(h)))

    out += ["", "entry = close of D+1 (signal is EOD on D); no costs/slippage applied"]
    return "\n".join(out)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — not sending")
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": message,
                            "disable_web_page_preview": True}, timeout=15)
    print("sent to Telegram" if r.ok else f"telegram error {r.status_code}: {r.text}")


def main():
    _load_dotenv()
    conn = sqlite3.connect(DB_PATH)
    try:
        report = format_report(evaluate(conn))
    finally:
        conn.close()
    print(report)
    if "--telegram" in sys.argv:
        send_telegram(report)


if __name__ == "__main__":
    main()
