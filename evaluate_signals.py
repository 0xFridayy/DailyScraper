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

"Not signalled" is only evidence when the source could have signalled. A source
that was SOURCE_UNAVAILABLE or RETIRED_SOURCE on a day (signal_source_status, or
the field lifecycle registry for days before that table existed) produced no
negatives that day:
  BY SOURCE   a source is compared only on days it was available: its hits and
              its control both drop its unavailable days
  ALL/MARKET  the combined signal is the union of the sources IN the strategy that
              day. A retired source has left the strategy, so its days stay; a live
              source that was SOURCE_UNAVAILABLE leaves the union incomplete, so
              that day drops from both the combined hits and the control.
A retirement changes which sources make up ALL SIGNALS, so ALL SIGNALS and MARKET
are reported per strategy version (neobdm_source_contract.signal_strategy_regime),
never pooled across versions.

Run:  py evaluate_signals.py            -> print report
      py evaluate_signals.py --telegram -> also send it to the bot
"""

import os
import sqlite3
import statistics
import sys
from collections import defaultdict

import requests

import neobdm_source_contract as nsc

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


def load_source_status(conn):
    """{source: {flag_date: status}} from signal_source_status (empty if absent)."""
    if not nsc.table_exists(conn, "signal_source_status"):
        return {}
    out = defaultdict(dict)
    for d, source, status in conn.execute("select flag_date, source, status from signal_source_status"):
        out[source][d] = status
    return dict(out)


def source_state(status_by_source, source, day):
    """RETIRED_SOURCE / SOURCE_UNAVAILABLE / None (available) for one source on one
    day. The lifecycle registry covers retired days that have no status row."""
    status = status_by_source.get(source, {}).get(day)
    if status in (nsc.RETIRED_SOURCE, nsc.SOURCE_UNAVAILABLE):
        return status
    for field, entry in nsc.FIELD_LIFECYCLE.items():
        if source in (entry.get("signals") or []) and nsc.lifecycle_state(field, day) == nsc.RETIRED:
            return nsc.RETIRED_SOURCE
    return None


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
    status = load_source_status(conn)

    flagged = defaultdict(set)                 # date -> {ticker}
    for d, t, _s in signals:
        flagged[d].add(t)

    sources = sorted({s for _d, _t, ss in signals for s in ss if s} | set(status))
    # Days a live (non-retired) source failed: the combined union is incomplete.
    incomplete = {d for d in dates if any(source_state(status, s, d) == nsc.SOURCE_UNAVAILABLE
                                          for s in sources)}
    unavailable = {s: {d for d in dates if source_state(status, s, d)} for s in sources}

    regime = {d: nsc.signal_strategy_regime(d) for d in dates}
    by_source = defaultdict(lambda: defaultdict(list))   # source -> h -> samples
    source_control = defaultdict(lambda: defaultdict(list))
    overall = defaultdict(lambda: defaultdict(list))     # strategy -> h -> samples
    control = defaultdict(lambda: defaultdict(list))     # strategy -> h -> samples
    uncovered = [t for d, t, _s in signals if (d, t) not in panel]

    for d, t, srcs in signals:
        for h in HORIZONS:
            o = outcome(panel, dates, t, d, h)
            if o is None:
                continue
            if d not in incomplete:
                overall[regime[d]["strategy"]][h].append(o)
            for s in srcs:
                if s and d not in unavailable[s]:
                    by_source[s][h].append(o)

    panel_dates = set(dates)
    for (d, t) in panel:
        if d not in panel_dates or t in flagged.get(d, ()):
            continue
        for h in HORIZONS:
            o = outcome(panel, dates, t, d, h)
            if o is None:
                continue
            if d not in incomplete:
                control[regime[d]["strategy"]][h].append(o)
            for s in sources:
                if d not in unavailable[s]:
                    source_control[s][h].append(o)

    return {
        "dates": dates,
        "n_signal_rows": len(signals),
        "uncovered": uncovered,
        "overall": {st: {h: summarise(v) for h, v in hs.items()} for st, hs in overall.items()},
        "control": {st: {h: summarise(v) for h, v in hs.items()} for st, hs in control.items()},
        "strategies": {r["strategy"]: {"since": r["since"], "retired_sources": r["retired_sources"],
                                       "first": min(d for d in dates if regime[d]["strategy"] == r["strategy"]),
                                       "last": max(d for d in dates if regime[d]["strategy"] == r["strategy"])}
                       for r in regime.values()},
        "by_source": {s: {h: summarise(v) for h, v in hs.items()}
                      for s, hs in by_source.items()},
        "source_control": {s: {h: summarise(v) for h, v in hs.items()}
                           for s, hs in source_control.items()},
        "unavailable_days": {s: len(v) for s, v in unavailable.items() if v},
        "incomplete_days": len(incomplete),
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
    for src, n in sorted(res.get("unavailable_days", {}).items()):
        out.append(f"source {src} unavailable/retired on {n} panel day(s) — excluded from its own "
                   f"comparison, not counted as no-signal days")
    if res.get("incomplete_days"):
        out.append(f"{res['incomplete_days']} day(s) with a live source unavailable — excluded from "
                   f"ALL SIGNALS and MARKET")

    measured = max((s["n"] for hs in res["overall"].values() for s in hs.values() if s), default=0)
    if measured < MIN_SIGNALS:
        out += ["", f"⏳ Only {measured} measurable signal-outcomes so far "
                    f"(need ≥{MIN_SIGNALS}).", "Too early to call — still accumulating."]
        return "\n".join(out)

    for strategy, info in sorted(res["strategies"].items(), key=lambda kv: kv[1]["first"]):
        retired = (f"; without retired {', '.join(info['retired_sources'])} since {info['since']}"
                   if info["retired_sources"] else "")
        out += ["", f"ALL SIGNALS [{strategy}: {info['first']} → {info['last']}{retired}] "
                    f"vs market (same days, same universe)"]
        for h in HORIZONS:
            out.append(_line(f"{h}d", res["overall"].get(strategy, {}).get(h),
                             res["control"].get(strategy, {}).get(h)))
        out += ["", f"MARKET (unsignalled control) [{strategy}]"]
        for h in HORIZONS:
            out.append(_line(f"{h}d", res["control"].get(strategy, {}).get(h), None))

    for src in sorted(res["by_source"]):
        stats = res["by_source"][src]
        if not any(stats.values()):
            continue
        out += ["", f"BY SOURCE — {src} (vs control on days {src} was available)"]
        for h in HORIZONS:
            out.append(_line(f"{h}d", stats.get(h), res["source_control"].get(src, {}).get(h)))

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
