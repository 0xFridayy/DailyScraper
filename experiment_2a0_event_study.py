"""Experiment #2A0: frozen exploratory PKDA 5% event study.

The controlling specification is the "Experiment #2A0" section of
ML_V2_EXPERIMENT_2_OWNERSHIP_ROADMAP.md.  This command is deliberately
fail-closed: cohort drift or incomplete event-date observability stops the
program before price data are loaded or any return is calculated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


FILTER_SQL = (
    "threshold='5pct' AND is_custodian_move=1 "
    "AND lot_change IS NOT NULL AND lot_change != 0"
)
EXPECTED = {"rows": 45, "positive": 18, "negative": 27,
            "events": 36, "holders": 39, "tickers": 11}
HORIZONS = (1, 5, 10, 20)
TOP_FIVE = ("BREN", "BRMS", "EMTK", "FAST", "SCMA")
N_RESAMPLES = 10_000
PLACEBO_SEED = 20260200
TICKER_BOOTSTRAP_SEED = 20260201
DATE_BOOTSTRAP_SEED = 20260202


class FrozenSpecError(RuntimeError):
    """The frozen cohort or protocol cannot be honored."""


class ObservabilityGateError(FrozenSpecError):
    """Event-level public observability was not established."""


@dataclass(frozen=True)
class PriceSeries:
    dates: np.ndarray
    closes: np.ndarray
    index_by_date: dict[pd.Timestamp, int]


def readonly_sqlite(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def load_source_rows(path: Path) -> pd.DataFrame:
    sql = f"""
        SELECT oc.ticker, oc.change_date, oc.investor_name_raw,
               COALESCE(ea.entity_id, oc.investor_name_raw) AS holder_id,
               oc.lot_change, oc.published_at, oc.captured_at, oc.available_at,
               oc.dq_unknown_publication_time, oc.source_url
        FROM ownership_change oc
        LEFT JOIN entity_alias ea ON ea.name_raw = oc.investor_name_raw
        WHERE oc.threshold='5pct' AND oc.is_custodian_move=1
          AND oc.lot_change IS NOT NULL AND oc.lot_change != 0
        ORDER BY oc.ticker, oc.change_date, oc.id
    """
    with readonly_sqlite(path) as conn:
        return pd.read_sql_query(sql, conn)


def audit_population(rows: pd.DataFrame) -> dict:
    events = rows[["ticker", "change_date"]].drop_duplicates()
    holders = rows[["ticker", "holder_id", "change_date"]].drop_duplicates()
    audit = {
        "rows": int(len(rows)),
        "positive": int((rows["lot_change"] > 0).sum()),
        "negative": int((rows["lot_change"] < 0).sum()),
        "events": int(len(events)),
        "holders": int(len(holders)),
        "tickers": int(events["ticker"].nunique()),
    }
    if audit != EXPECTED:
        raise FrozenSpecError(f"frozen population drift: expected {EXPECTED}, got {audit}")
    per_ticker = events.groupby("ticker").size().sort_values(ascending=False)
    if int(per_ticker.get("BREN", 0)) != 13:
        raise FrozenSpecError("frozen BREN concentration drift")
    if int(per_ticker.reindex(TOP_FIVE).sum()) != 28:
        raise FrozenSpecError("frozen top-five concentration drift")
    return audit


def _read_observability_manifest(path: Path, events: pd.DataFrame) -> pd.DataFrame:
    manifest = pd.read_csv(path, dtype=str)
    required = {"ticker", "change_date", "observable_date", "evidence"}
    if not required.issubset(manifest.columns):
        raise ObservabilityGateError(
            f"observability manifest requires columns {sorted(required)}"
        )
    manifest = manifest[list(required)].copy()
    if manifest[["ticker", "change_date"]].duplicated().any():
        raise ObservabilityGateError("observability manifest has duplicate event keys")
    expected_keys = set(map(tuple, events[["ticker", "change_date"]].to_numpy()))
    actual_keys = set(map(tuple, manifest[["ticker", "change_date"]].to_numpy()))
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        raise ObservabilityGateError(
            f"observability manifest key mismatch; missing={missing}, extra={extra}"
        )
    if manifest["evidence"].fillna("").str.strip().eq("").any():
        raise ObservabilityGateError("every observable date requires evidence")
    manifest["observable_date"] = pd.to_datetime(
        manifest["observable_date"], errors="raise", utc=True
    ).dt.tz_convert(None).dt.normalize()
    effective = pd.to_datetime(manifest["change_date"], errors="raise").dt.normalize()
    if manifest["observable_date"].lt(effective).any():
        raise ObservabilityGateError("observable_date cannot precede change_date")
    return manifest


def establish_observability(
    rows: pd.DataFrame, manifest_path: Path | None = None
) -> pd.DataFrame:
    """Return one defensible observable date per primary event or fail closed.

    A row-level published_at is independently verified provenance.  The current
    historical backfill has no such timestamps: available_at merely equals the
    collector's first capture and cannot recover each old move's original
    event-level availability.  A complete evidence-bearing manifest is the only
    supported way to supply those independently verified dates later.
    """
    keys = rows[["ticker", "change_date"]].drop_duplicates().reset_index(drop=True)
    if manifest_path is not None:
        verified = _read_observability_manifest(manifest_path, keys)
        return keys.merge(verified, on=["ticker", "change_date"], validate="one_to_one")

    publication = rows.copy()
    publication["published_ts"] = pd.to_datetime(publication["published_at"], errors="coerce")
    if publication["published_ts"].notna().all():
        observed = (
            publication.groupby(["ticker", "change_date"], as_index=False)["published_ts"]
            .max()
            .rename(columns={"published_ts": "observable_date"})
        )
        observed["observable_date"] = (
            pd.to_datetime(observed["observable_date"], utc=True)
            .dt.tz_convert(None).dt.normalize()
        )
        observed["evidence"] = "ownership_change.published_at"
        return observed

    unknown = int(publication["published_ts"].isna().sum())
    fallback = int(
        (publication["available_at"].astype(str) == publication["captured_at"].astype(str)).sum()
    )
    date_min = publication["change_date"].min()
    date_max = publication["change_date"].max()
    capture_min = publication["captured_at"].min()
    capture_max = publication["captured_at"].max()
    raise ObservabilityGateError(
        "event-date observability FAILED: change_date is the PKDA Tanggal field, "
        f"not a verified publication date; {unknown}/{len(publication)} rows have "
        "published_at=NULL and "
        f"{fallback}/{len(publication)} have available_at=captured_at. Historical "
        f"change dates span {date_min}..{date_max}, but first captures span "
        f"{capture_min}..{capture_max}. Supply a complete independently evidenced "
        "observability manifest; returns were not loaded or calculated."
    )


def load_prices(path: Path) -> tuple[pd.DataFrame, dict[str, PriceSeries]]:
    frame = pd.read_parquet(path, columns=["date", "ticker", "close"])
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame[frame["close"].gt(0)].drop_duplicates(["ticker", "date"], keep="last")
    frame = frame.sort_values(["ticker", "date"]).reset_index(drop=True)
    series = {}
    for ticker, group in frame.groupby("ticker", sort=True):
        dates = group["date"].to_numpy(dtype="datetime64[ns]")
        closes = group["close"].to_numpy(dtype=float)
        series[ticker] = PriceSeries(
            dates=dates,
            closes=closes,
            index_by_date={pd.Timestamp(d): i for i, d in enumerate(dates)},
        )
    return frame, series


def load_ihsg(path: Path) -> pd.Series:
    frame = pd.read_csv(path, usecols=["date", "close"])
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame[frame["close"].gt(0)].drop_duplicates("date", keep="last")
    return frame.set_index("date")["close"].sort_index()


def event_return(
    ticker: str,
    event_date: pd.Timestamp,
    horizon: int,
    prices: dict[str, PriceSeries],
    ihsg: pd.Series,
) -> dict | None:
    px = prices.get(ticker)
    if px is None:
        return None
    normalized = np.datetime64(pd.Timestamp(event_date).normalize(), "ns")
    base_i = int(np.searchsorted(px.dates, normalized, side="right"))
    end_i = base_i + horizon
    if base_i >= len(px.dates) or end_i >= len(px.dates):
        return None
    base_date, end_date = pd.Timestamp(px.dates[base_i]), pd.Timestamp(px.dates[end_i])
    raw = float(px.closes[end_i] / px.closes[base_i] - 1.0)
    adjusted = None
    if base_date in ihsg.index and end_date in ihsg.index:
        market = float(ihsg.loc[end_date] / ihsg.loc[base_date] - 1.0)
        adjusted = raw - market
    return {"t0": base_date, "end_date": end_date, "raw": raw,
            "ihsg_adjusted": adjusted}


def construct_returns(
    events: pd.DataFrame,
    prices: dict[str, PriceSeries],
    ihsg: pd.Series,
) -> pd.DataFrame:
    records = []
    for event in events.itertuples(index=False):
        for horizon in HORIZONS:
            result = event_return(event.ticker, event.observable_date, horizon, prices, ihsg)
            if result is not None:
                records.append({"ticker": event.ticker, "change_date": event.change_date,
                                "observable_date": event.observable_date,
                                "horizon": horizon, **result})
    return pd.DataFrame.from_records(records)


def summarize(values: pd.Series, tickers: pd.Series) -> dict:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    ticker_values = tickers.loc[clean.index]
    if clean.empty:
        return {"mean": None, "median": None, "positive_pct": None,
                "n_events": 0, "n_tickers": 0, "min": None, "max": None}
    return {
        "mean": float(clean.mean()),
        "median": float(clean.median()),
        "positive_pct": float((clean > 0).mean() * 100),
        "n_events": int(len(clean)),
        "n_tickers": int(ticker_values.nunique()),
        "min": float(clean.min()),
        "max": float(clean.max()),
    }


def analysis_samples(events: pd.DataFrame) -> dict[str, pd.DataFrame]:
    samples = {"full": events, "ex_BREN": events[events["ticker"] != "BREN"],
               "ex_top5": events[~events["ticker"].isin(TOP_FIVE)]}
    for ticker in sorted(events["ticker"].unique()):
        samples[f"leave_out_{ticker}"] = events[events["ticker"] != ticker]
    return samples


def _derived_seed(base: int, *parts: object) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return base + int.from_bytes(digest[:4], "big")


def ticker_cluster_interval(frame: pd.DataFrame, column: str, seed: int) -> list[float]:
    clusters = {t: g[column].dropna().to_numpy(float) for t, g in frame.groupby("ticker")}
    names = sorted(k for k, v in clusters.items() if len(v))
    if not names:
        return [float("nan"), float("nan")]
    rng = np.random.default_rng(seed)
    out = np.empty(N_RESAMPLES)
    for i in range(N_RESAMPLES):
        draw = rng.choice(names, size=len(names), replace=True)
        out[i] = np.concatenate([clusters[t] for t in draw]).mean()
    return np.quantile(out, [0.025, 0.975]).astype(float).tolist()


def date_block_interval(
    frame: pd.DataFrame, column: str, ihsg_calendar: pd.DatetimeIndex, seed: int
) -> list[float]:
    if frame.empty:
        return [float("nan"), float("nan")]
    first = frame["t0"].min()
    positions = pd.Series(np.arange(len(ihsg_calendar)), index=ihsg_calendar)
    origin = int(positions.loc[positions.index >= first].iloc[0])
    work = frame[frame["t0"].isin(positions.index)].copy()
    work["date_block"] = work["t0"].map(positions).sub(origin).floordiv(20).astype(int)
    blocks = {b: g[column].dropna().to_numpy(float) for b, g in work.groupby("date_block")}
    names = sorted(k for k, v in blocks.items() if len(v))
    if not names:
        return [float("nan"), float("nan")]
    rng = np.random.default_rng(seed)
    out = np.empty(N_RESAMPLES)
    for i in range(N_RESAMPLES):
        draw = rng.choice(names, size=len(names), replace=True)
        out[i] = np.concatenate([blocks[b] for b in draw]).mean()
    return np.quantile(out, [0.025, 0.975]).astype(float).tolist()


def _eligible_pseudo_dates(
    ticker: str,
    real_date: pd.Timestamp,
    horizon: int,
    outcome: str,
    prices: dict[str, PriceSeries],
    ihsg: pd.Series,
    idx_calendar: pd.DatetimeIndex,
    excluded: set[pd.Timestamp],
) -> tuple[list[pd.Timestamp], list[pd.Timestamp]]:
    calendar = idx_calendar
    candidates = []
    for date in calendar:
        date = pd.Timestamp(date).normalize()
        if date in excluded:
            continue
        result = event_return(ticker, date, horizon, prices, ihsg)
        if result is not None and result[outcome] is not None:
            candidates.append(date)
    same_month = [d for d in candidates if d.year == real_date.year and d.month == real_date.month]
    center = int(np.searchsorted(calendar.to_numpy(), np.datetime64(real_date), side="left"))
    lo, hi = max(0, center - 40), min(len(calendar), center + 41)
    window = set(pd.Timestamp(d).normalize() for d in calendar[lo:hi])
    fallback = [d for d in candidates if d in window]
    return same_month, fallback


def placebo_distribution(
    events: pd.DataFrame,
    horizon: int,
    outcome: str,
    prices: dict[str, PriceSeries],
    ihsg: pd.Series,
    idx_calendar: pd.DatetimeIndex,
    seed: int,
) -> np.ndarray:
    excluded_by_ticker = {
        ticker: set(pd.to_datetime(group["observable_date"]).dt.normalize()) |
                set(pd.to_datetime(group["change_date"]).dt.normalize())
        for ticker, group in events.groupby("ticker")
    }
    pools = {}
    for idx, event in events.reset_index(drop=True).iterrows():
        real_date = pd.Timestamp(event["observable_date"]).normalize()
        pools[idx] = _eligible_pseudo_dates(
            event["ticker"], real_date, horizon, outcome, prices, ihsg, idx_calendar,
            excluded_by_ticker[event["ticker"]],
        )
    rng = np.random.default_rng(seed)
    distribution = np.empty(N_RESAMPLES)
    ordered = events.reset_index(drop=True)
    for rep in range(N_RESAMPLES):
        used: dict[str, set[pd.Timestamp]] = {}
        values = []
        for idx, event in ordered.iterrows():
            ticker = event["ticker"]
            used.setdefault(ticker, set())
            same_month, fallback = pools[idx]
            candidates = [d for d in same_month if d not in used[ticker]]
            if not candidates:
                candidates = [d for d in fallback if d not in used[ticker]]
            if not candidates:
                raise FrozenSpecError(
                    f"no horizon-{horizon} pseudo-date for {ticker} {event['change_date']}"
                )
            pseudo_date = candidates[int(rng.integers(len(candidates)))]
            used[ticker].add(pseudo_date)
            result = event_return(ticker, pseudo_date, horizon, prices, ihsg)
            values.append(result[outcome])
        distribution[rep] = float(np.mean(values))
    return distribution


def holm_adjust(pvalues: list[float]) -> list[float]:
    order = np.argsort(pvalues)
    adjusted = np.empty(len(pvalues), dtype=float)
    running = 0.0
    for rank, idx in enumerate(order):
        value = min(1.0, (len(pvalues) - rank) * pvalues[idx])
        running = max(running, value)
        adjusted[idx] = running
    return adjusted.tolist()


def run_study(events: pd.DataFrame, prices_path: Path, ihsg_path: Path) -> dict:
    price_frame, prices = load_prices(prices_path)
    idx_calendar = pd.DatetimeIndex(sorted(price_frame["date"].unique()))
    ihsg = load_ihsg(ihsg_path)
    returns = construct_returns(events, prices, ihsg)
    if returns.empty:
        raise FrozenSpecError("no event has valid return coverage")

    samples = analysis_samples(events)
    output: dict = {"samples": {}, "leave_one_ticker_out": {}}
    full_tests = []
    for sample_name, sample_events in samples.items():
        keys = sample_events[["ticker", "change_date"]]
        sample_returns = returns.merge(keys, on=["ticker", "change_date"], how="inner")
        sample_output = {}
        for horizon in HORIZONS:
            horizon_frame = sample_returns[sample_returns["horizon"] == horizon]
            for outcome in ("raw", "ihsg_adjusted"):
                label = f"{outcome}_{horizon}d"
                outcome_frame = horizon_frame[horizon_frame[outcome].notna()].copy()
                covered_keys = outcome_frame[["ticker", "change_date"]].drop_duplicates()
                placebo_events = sample_events.merge(
                    covered_keys, on=["ticker", "change_date"], how="inner",
                    validate="one_to_one",
                )
                stats = summarize(outcome_frame[outcome], outcome_frame["ticker"])
                if outcome_frame.empty:
                    stats.update({
                        "ticker_cluster_ci95": [None, None],
                        "date_block_ci95": [None, None],
                        "placebo_percentile": None,
                        "placebo_p_two_sided": None,
                    })
                    sample_output[label] = stats
                    if sample_name == "full":
                        full_tests.append((label, None))
                    continue
                stats["ticker_cluster_ci95"] = ticker_cluster_interval(
                    outcome_frame, outcome,
                    _derived_seed(TICKER_BOOTSTRAP_SEED, sample_name, label),
                )
                stats["date_block_ci95"] = date_block_interval(
                    outcome_frame, outcome, idx_calendar,
                    _derived_seed(DATE_BOOTSTRAP_SEED, sample_name, label),
                )
                placebo = placebo_distribution(
                    placebo_events, horizon, outcome, prices, ihsg, idx_calendar,
                    _derived_seed(PLACEBO_SEED, sample_name, label),
                )
                observed = stats["mean"]
                stats["placebo_percentile"] = float((placebo <= observed).mean() * 100)
                stats["placebo_p_two_sided"] = float(
                    (1 + np.count_nonzero(np.abs(placebo) >= abs(observed))) /
                    (N_RESAMPLES + 1)
                )
                sample_output[label] = stats
                if sample_name == "full":
                    full_tests.append((label, stats["placebo_p_two_sided"]))
        output["samples"][sample_name] = sample_output

    valid_full_tests = [(label, p) for label, p in full_tests if p is not None]
    adjusted = holm_adjust([p for _, p in valid_full_tests])
    for (label, _), p_adj in zip(valid_full_tests, adjusted):
        output["samples"]["full"][label]["holm_p_eight_tests"] = p_adj
    for label, p in full_tests:
        if p is None:
            output["samples"]["full"][label]["holm_p_eight_tests"] = None

    output["interpretation_flags"] = {}
    for label, full_stats in output["samples"]["full"].items():
        ex_bren = output["samples"]["ex_BREN"][label]
        full_p = full_stats["placebo_p_two_sided"]
        ex_bren_p = ex_bren["placebo_p_two_sided"]
        full_nominal = full_p is not None and full_p < 0.05
        sign_reversal = bool(
            full_stats["mean"] is not None and ex_bren["mean"] is not None and
            full_stats["mean"] != 0 and ex_bren["mean"] != 0 and
            np.sign(full_stats["mean"]) != np.sign(ex_bren["mean"])
        )
        output["interpretation_flags"][label] = {
            "concentration_driven": bool(
                full_nominal and
                (ex_bren_p is None or ex_bren_p >= 0.05 or sign_reversal)
            ),
            "ex_BREN_sign_reversal": sign_reversal,
            "single_horizon_significance_is_exploratory": True,
            "cannot_authorize_ML_or_production": True,
        }

    full = output["samples"]["full"]
    for horizon in HORIZONS:
        for outcome in ("raw", "ihsg_adjusted"):
            label = f"{outcome}_{horizon}d"
            estimates = {
                ticker.removeprefix("leave_out_"): sample[label]["mean"]
                for ticker, sample in output["samples"].items()
                if ticker.startswith("leave_out_")
            }
            full_mean = full[label]["mean"]
            valid = {ticker: value for ticker, value in estimates.items() if value is not None}
            changes = ({ticker: abs(value - full_mean) for ticker, value in valid.items()}
                       if full_mean is not None else {})
            signs = {int(np.sign(value)) for value in valid.values() if value != 0}
            output["leave_one_ticker_out"][label] = {
                "estimates": estimates,
                "min": min(valid.values()) if valid else None,
                "max": max(valid.values()) if valid else None,
                "sign_stable": len(signs) <= 1,
                "largest_absolute_change_omission": (
                    max(changes, key=changes.get) if changes else None
                ),
            }
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ownership-db", type=Path, default=Path("neobdm_ownership.db"))
    parser.add_argument("--prices", type=Path, default=Path("ohlc.parquet"))
    parser.add_argument("--ihsg", type=Path, default=Path("jci_daily.csv"))
    parser.add_argument("--observability-manifest", type=Path)
    parser.add_argument("--output", type=Path, default=Path("experiment_2a0_results.json"))
    args = parser.parse_args()

    rows = load_source_rows(args.ownership_db)
    audit = audit_population(rows)
    print("population audit:", json.dumps(audit, sort_keys=True))
    try:
        events = establish_observability(rows, args.observability_manifest)
    except ObservabilityGateError as exc:
        print(str(exc))
        return 2

    print("event-date observability: PASS")
    results = run_study(events, args.prices, args.ihsg)
    payload = {"population_audit": audit, "results": results}
    args.output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
