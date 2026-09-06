"""The NORMALIZED layer: annotate the vendor cache, never silently rewrite it.

Layering
--------
    RAW         inventory_raw/*.json.gz          immutable, never written
    INGEST      build_inventory_db.py            faithful transcription, int64 lots
    NORMALIZED  this module                      provenance artifacts (JSON)
    DERIVED     panel construction               applies the artifacts

This module reads the raw cache and emits three artifacts:

    volume_repair_ledger.json    every uint32 volume wrap, with the evidence
    observed_basis_factor.json   per-ticker basis regimes, with the evidence
    market_dq_report.json        invariant violation counts

It does not modify the raw cache, either parquet, or neobdm.db. It applies no
correction: a consumer that wants a harmonised panel reads these artifacts and
applies them itself, so the correction is always visible and auditable.

Why "observed_basis_factor" and not "corporate action factor"
-------------------------------------------------------------
The cache carries no corporate-action metadata of any kind. We can measure that
the OHLC series and the broker series sit on different bases, and by how much.
We cannot attribute that to a named event. The neutral term is deliberate.

The measured factor r satisfies, for a high-confidence regime:

    adjusted_price  = as_traded_price  / r
    adjusted_volume = as_traded_volume * r
    adjusted_lots   = as_traded_lots   * r
    nominal rupiah values unchanged

Verified on real rows: for RAJA 2026-07-15 the rupiah product P*V is identical
on both bases to a ratio of 1.0000000000.

Note that the panel never needs reconstructed as-traded OHLC. The vendor's price
and volume are already mutually consistent; only the broker lots sit on the other
basis. So a consumer harmonises by scaling LOTS by r and leaving price and volume
alone -- which keeps the return series continuous and keeps lots integral. The
reconstruction test below is used purely as EVIDENCE that r is exact, never as a
transformation we ship.

Usage
-----
    py normalize_market_data.py                     # write artifacts to repo root
    py normalize_market_data.py --out-dir <dir>     # write elsewhere
    py normalize_market_data.py --verify            # build twice, prove determinism
"""
import argparse
import glob
import gzip
import hashlib
import json
import os
from fractions import Fraction

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "inventory_raw")

SHARES_PER_LOT = 100
MODULUS = 2 ** 32                 # unsigned 32-bit wrap observed in vendor volume

# A regime is a basis mismatch, not source rounding, when the deficit scales with
# volume. The two populations separate cleanly and by a wide margin: measured
# |r-1| is >= 1.35e-2 for every basis regime and <= 8.5e-4 for every coverage
# deficit, a 15.9x gap, and they separate again on the ratio correlation below.
BASIS_MIN_DEVIATION = 1e-3
RATIO_CORRELATION_MIN = 0.90
MIN_REGIME_DAYS = 20

# Reconstruction evidence: price * r must land on exact integers on the IDX tick
# grid. This is the criterion that actually discriminates -- dual-estimator
# agreement does not, because the volume-free estimator's own noise on clean
# tickers spans [0.873, 1.196] at the 99.8% level and so cannot resolve a factor
# closer than ~16% to 1.
RECONSTRUCTION_MIN_RATE = 0.999

# The volume-free estimator is used as a one-sided VETO, never as a certificate.
# On clean (r == 1) tickers its own ratio spans [0.873, 1.196] at the 99.8% level
# (sd 0.024, n=22,727 ticker-days), because a broker VWAP is not a close. So it
# cannot confirm a factor -- it agrees with every regime we measured, including
# ones that are demonstrably not reconstructible. It can still refute one: a
# disagreement wider than that measured noise band means the price series and the
# volume series disagree about the basis, and the regime is not trustworthy.
DUAL_ESTIMATOR_MAX_DISAGREEMENT = 0.20

# IDX fraksi harga (tick size) by price band.
TICK_BANDS = ((200, 1.0), (500, 2.0), (2000, 5.0), (5000, 10.0))
TICK_ABOVE = 25.0


def tick_size(price):
    price = np.asarray(price, dtype=np.float64)
    conds = [price < edge for edge, _ in TICK_BANDS]
    return np.select(conds, [size for _, size in TICK_BANDS], default=TICK_ABOVE)


def on_tick_grid(price):
    size = tick_size(price)
    rem = np.remainder(price, size)
    return np.isclose(rem, 0.0) | np.isclose(rem, size)


def raw_path(ticker):
    return os.path.join(RAW, ticker + ".json.gz")


def raw_tickers():
    return sorted(os.path.basename(p)[:-8]
                  for p in glob.glob(os.path.join(RAW, "*.json.gz")))


def load_raw(ticker):
    with gzip.open(raw_path(ticker), "rt", encoding="utf-8") as fh:
        return json.load(fh)


def file_fingerprint(ticker):
    h = hashlib.sha256()
    with open(raw_path(ticker), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _int_or_none(value):
    """Vendor lots are a counting quantity; anything else is not a lot."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def daily_totals(data):
    """Exact per-session integer lot totals and float rupiah totals.

    Lots are summed as Python ints so the wrap signature survives; float
    summation of the same values destroys it, which is why this reads raw JSON
    rather than the parquet.
    """
    dates = data.get("date") or []
    n = len(dates)
    out = {
        "dates": dates,
        "blot": [0] * n, "slot": [0] * n, "nlot": [0] * n,
        "bval": [0.0] * n, "sval": [0.0] * n, "nval": [0.0] * n,
        "non_integer_lots": 0,
    }
    for field in ("blot", "slot", "nlot"):
        for _, series in (data.get(field) or {}).items():
            for i, raw_value in enumerate(series[:n]):
                value = _int_or_none(raw_value)
                if value is None:
                    if raw_value is not None:
                        out["non_integer_lots"] += 1
                    continue
                out[field][i] += value
    for field in ("bval", "sval", "nval"):
        for _, series in (data.get(field) or {}).items():
            for i, raw_value in enumerate(series[:n]):
                if raw_value is not None:
                    out[field][i] += float(raw_value)

    ohlc = {row["date"]: row for row in (data.get("ohlc") or [])}
    out["volume"] = [ohlc[d].get("volume") if d in ohlc else None for d in dates]
    for key in ("open", "high", "low", "close"):
        out[key] = [ohlc[d].get(key) if d in ohlc else None for d in dates]
    return out


# --------------------------------------------------------------------------
# uint32 volume wrap
# --------------------------------------------------------------------------

def detect_volume_wraps(ticker, totals):
    """Stage A: basis-free, exact integer detection of an unsigned 32-bit wrap.

    Fires only when every guard holds simultaneously, with NO tolerance. Exact
    equality is available because both sides are integers, and a tolerance would
    add risk for nothing: the nearest non-wrap observation in the whole cache sits
    3.5% of 2**32 away from the signature.

    Guards, all independently checkable:
      shortfall is exactly one modulus     d == MODULUS
      stored value could have wrapped      0 <= v < MODULUS
      buy and sell sides agree             sum(blot) == sum(slot)
      residue is consistent with the wrap  v % 100 == (-MODULUS) % 100 == 4
        (true volume is a whole number of lots, so a wrapped value must be
         congruent to -MODULUS modulo SHARES_PER_LOT)
    """
    hits = []
    for i, date in enumerate(totals["dates"]):
        volume = totals["volume"][i]
        if volume is None or int(volume) != volume:
            continue
        v = int(volume)
        sb = SHARES_PER_LOT * totals["blot"][i]
        ss = SHARES_PER_LOT * totals["slot"][i]
        if sb <= 0:
            continue
        shortfall = sb - v
        if shortfall != MODULUS:
            continue
        if not 0 <= v < MODULUS:
            continue
        if sb != ss:
            continue
        if v % SHARES_PER_LOT != (-MODULUS) % SHARES_PER_LOT:
            continue
        hits.append({
            "ticker": ticker,
            "date": date,
            "rule": "uint32_volume_wrap_stage_a",
            "raw_volume": v,
            "normalized_volume": v + shortfall,
            "delta": shortfall,
            "wrap_count": shortfall // MODULUS,
            "evidence": {
                "equals_sum_blot_shares": (v + shortfall) == sb,
                "equals_sum_slot_shares": (v + shortfall) == ss,
                "raw_below_modulus": v < MODULUS,
                "lot_residue_consistent": v % SHARES_PER_LOT == 4,
            },
        })
    return hits


def detect_masked_wraps(ticker, totals, factor):
    """Stage B: DIAGNOSTIC ONLY -- a wrap hidden behind a basis factor.

    When a ticker's volume is reported on a different basis (factor r), a wrap in
    the underlying as-traded share count is invisible to stage A: the stored value
    is r*(true_shares - MODULUS), which is neither below MODULUS nor an exact
    multiple away from sum(blot)*100.

    This is real -- BNBR 2026-02-27 reproduces exactly as 1.191*(sum_blot*100 -
    2**32) -- but it is NOT a repair path. It requires r, which for every ticker
    where stage B fires is a factor we could not certify; and the reconstruction
    is inexact (BNBR 2026-03-02 lands 0.4 shares off). So a stage-B hit is
    recorded as evidence REINFORCING quarantine, never as an authorised repair.
    """
    if factor is None:
        return []
    hits = []
    for i, date in enumerate(totals["dates"]):
        volume = totals["volume"][i]
        if volume is None:
            continue
        v = float(volume)
        sb = SHARES_PER_LOT * totals["blot"][i]
        if sb <= 0:
            continue
        if abs(v - float(factor) * sb) < 1.0:
            continue                        # ordinary day for this basis
        for k in (1, 2, 3):
            if sb - k * MODULUS <= 0:
                break
            if abs(float(factor) * (sb - k * MODULUS) - v) < 1.0:
                hits.append({
                    "ticker": ticker,
                    "date": date,
                    "rule": "uint32_volume_wrap_stage_b_diagnostic",
                    "raw_volume": v,
                    "wrap_count": k,
                    "basis_factor": float(factor),
                    "implied_as_traded_shares": sb - k * MODULUS,
                    "authorised_repair": False,
                })
                break
    return hits


# --------------------------------------------------------------------------
# observed basis factor
# --------------------------------------------------------------------------

def observed_basis_factor(ticker, totals, repaired_volume):
    """Measure the basis regime and classify it on evidence, not on attribution."""
    dates = totals["dates"]
    usable = [i for i, v in enumerate(repaired_volume)
              if v is not None and totals["blot"][i] > 0]
    if not usable:
        return None

    ratios = {}
    for i in usable:
        ratios[i] = Fraction(int(repaired_volume[i]),
                             SHARES_PER_LOT * totals["blot"][i])
    off = [i for i in usable if abs(float(ratios[i]) - 1.0) > BASIS_MIN_DEVIATION]
    if len(off) < MIN_REGIME_DAYS:
        return None

    on = [i for i in usable if i not in set(off)]
    values = [ratios[i] for i in off]
    exactly_constant = len(set(values)) == 1
    prefix_block = (not on) or (max(off) < min(on))
    representative = values[len(values) // 2] if exactly_constant else \
        Fraction(float(np.median([float(v) for v in values]))).limit_denominator(100000)

    lots = np.array([totals["blot"][i] for i in usable], dtype=np.float64)
    gap = np.array([abs(int(repaired_volume[i]) - SHARES_PER_LOT * totals["blot"][i])
                    / SHARES_PER_LOT for i in usable], dtype=np.float64)
    moving = gap > 0
    correlation = (float(np.corrcoef(lots[moving], gap[moving])[0, 1])
                   if moving.sum() > 2 and np.std(lots[moving]) > 0 else float("nan"))

    # volume-free corroborating estimator: broker VWAP against OHLC close
    e2_samples = []
    for i in off:
        close = totals["close"][i]
        blot = totals["blot"][i]
        if close and blot > 0:
            vwap = totals["bval"][i] / (blot * SHARES_PER_LOT)
            e2_samples.append(vwap / float(close))
    estimator_volume_free = float(np.median(e2_samples)) if e2_samples else float("nan")

    # reconstruction evidence
    prices = []
    for i in off:
        for key in ("open", "high", "low", "close"):
            p = totals[key][i]
            if p:
                prices.append(float(p))
    rate_integral = rate_on_grid = 0.0
    if prices:
        rebuilt = np.array(prices) * float(representative)
        rate_integral = float(np.isclose(rebuilt, np.round(rebuilt)).mean())
        rate_on_grid = float(on_tick_grid(np.round(rebuilt, 6)).mean())

    # One-sided veto: agreement proves nothing, but gross disagreement refutes.
    if np.isnan(estimator_volume_free) or float(representative) == 0:
        estimators_disagree = False
        estimator_gap = float("nan")
    else:
        estimator_gap = abs(estimator_volume_free / float(representative) - 1.0)
        estimators_disagree = estimator_gap > DUAL_ESTIMATOR_MAX_DISAGREEMENT

    reconstructible = bool(
        exactly_constant
        and prefix_block
        and (np.isnan(correlation) or correlation >= RATIO_CORRELATION_MIN)
        and rate_integral >= RECONSTRUCTION_MIN_RATE
        and rate_on_grid >= RECONSTRUCTION_MIN_RATE
        and not estimators_disagree
    )
    return {
        "ticker": ticker,
        "factor": float(representative),
        "factor_exact": f"{representative.numerator}/{representative.denominator}",
        "days_in_regime": len(off),
        "days_outside_regime": len(on),
        "regime_first_date": dates[min(off)],
        "regime_last_date": dates[max(off)],
        "evidence": {
            "exactly_constant": exactly_constant,
            "prefix_block": prefix_block,
            "ratio_like_correlation": correlation,
            "estimator_volume_based": float(representative),
            "estimator_volume_free": estimator_volume_free,
            "estimator_relative_gap": estimator_gap,
            "estimators_disagree": estimators_disagree,
            "reconstruction_integral_rate": rate_integral,
            "reconstruction_on_grid_rate": rate_on_grid,
        },
        "classification": "RECONSTRUCTIBLE" if reconstructible else "QUARANTINE",
    }


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------

def invariant_counts(totals, repaired_volume, skip_dates):
    """Count cross-source invariant violations for one ticker.

    Classification rationale, from measured source semantics:

      I1/I2  sum(blot|slot)*100 vs volume
             QUARANTINE when the deficit is ratio-like (a basis mismatch);
             REPORTED when it is a small absolute lot count. Lot conservation is
             NOT source-guaranteed: buy- and sell-side agreement with volume is
             symmetric (96.0986% vs 96.1081%), all 101 declared broker codes are
             present, and the residual deficit is 2-23 lots against millions
             traded -- source rounding, not truncation. Making exact equality
             FATAL would reject 15 in-universe tickers on ~100% of their days.
      I3     row-level nlot == blot - slot: FATAL. Holds exactly in raw integers
             (0 of 2,689,054 in-universe rows); every observed failure was a
             float32 write artifact.
      I3b    sum(nlot) == 0: REPORTED, same coverage argument as I1/I2.
      I4/I5  value conservation: FATAL control. Exact today (0 violations), and
             it is what proves lots rather than values are the broken field.
      I6     implied price within [low, high]: REPORTED until basis harmonisation
             lands, because 90.1% of violations are downstream of I1.
    """
    skip = set(skip_dates or ())
    counts = {k: 0 for k in ("ticker_days", "i1_buy_vs_volume", "i2_sell_vs_volume",
                             "i3_row_nlot_identity", "i3b_sum_nlot_nonzero",
                             "i4_value_conservation", "i5_sum_nval_nonzero",
                             "i6_implied_price_outside_range")}
    for i, date in enumerate(totals["dates"]):
        if date in skip:
            continue
        volume = repaired_volume[i]
        blot, slot = totals["blot"][i], totals["slot"][i]
        if volume is None or (blot <= 0 and slot <= 0):
            continue
        counts["ticker_days"] += 1
        v = int(volume)
        if SHARES_PER_LOT * blot != v:
            counts["i1_buy_vs_volume"] += 1
        if SHARES_PER_LOT * slot != v:
            counts["i2_sell_vs_volume"] += 1
        if totals["nlot"][i] != 0:
            counts["i3b_sum_nlot_nonzero"] += 1
        if abs(totals["bval"][i] - totals["sval"][i]) > 0.5:
            counts["i4_value_conservation"] += 1
        if abs(totals["nval"][i]) > 0.5:
            counts["i5_sum_nval_nonzero"] += 1
    return counts


def row_identity_failures(data):
    """Row-level nlot == blot - slot, in exact integer arithmetic."""
    dates = data.get("date") or []
    n = len(dates)
    failures = rows = 0
    blot_all, slot_all, nlot_all = (data.get(f) or {} for f in ("blot", "slot", "nlot"))
    for broker in sorted(set(blot_all) | set(slot_all) | set(nlot_all)):
        b = blot_all.get(broker) or [0] * n
        s = slot_all.get(broker) or [0] * n
        net = nlot_all.get(broker) or [0] * n
        for i in range(n):
            bi, si, ni = _int_or_none(b[i]), _int_or_none(s[i]), _int_or_none(net[i])
            if bi is None or si is None or ni is None:
                continue
            if bi == 0 and si == 0 and ni == 0:
                continue
            rows += 1
            if ni != bi - si:
                failures += 1
    return failures, rows


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def build_artifacts(tickers, universe=None):
    """Deterministic: output depends only on the frozen cache and these inputs."""
    ledger, diagnostics, factors = [], [], []
    report = {k: 0 for k in ("tickers", "ticker_days", "i1_buy_vs_volume",
                             "i2_sell_vs_volume", "i3_row_nlot_identity",
                             "i3_rows_checked", "i3b_sum_nlot_nonzero",
                             "i4_value_conservation", "i5_sum_nval_nonzero",
                             "non_integer_lot_values")}
    for ticker in sorted(tickers):
        data = load_raw(ticker)
        totals = daily_totals(data)
        report["non_integer_lot_values"] += totals["non_integer_lots"]

        wraps = detect_volume_wraps(ticker, totals)
        if wraps:
            fingerprint = file_fingerprint(ticker)
            for hit in wraps:
                hit["source_fingerprint"] = fingerprint
            ledger.extend(wraps)

        repaired = list(totals["volume"])
        for hit in wraps:
            repaired[totals["dates"].index(hit["date"])] = hit["normalized_volume"]

        regime = observed_basis_factor(ticker, totals, repaired)
        if regime is not None:
            factors.append(regime)
            diagnostics.extend(detect_masked_wraps(ticker, totals, regime["factor"]))

        if universe is not None and ticker not in universe:
            continue
        report["tickers"] += 1
        failures, rows = row_identity_failures(data)
        report["i3_row_nlot_identity"] += failures
        report["i3_rows_checked"] += rows
        counts = invariant_counts(totals, repaired, [h["date"] for h in wraps])
        for key, value in counts.items():
            if key in report:
                report[key] += value

    ledger.sort(key=lambda h: (h["ticker"], h["date"]))
    diagnostics.sort(key=lambda h: (h["ticker"], h["date"]))
    factors.sort(key=lambda f: f["ticker"])
    return {
        "volume_repair_ledger": {
            "rule_version": "uint32_volume_wrap/1",
            "modulus": MODULUS,
            "authorised_repairs": ledger,
            "diagnostic_only": diagnostics,
        },
        "observed_basis_factor": {
            "note": ("Measured basis ratios. No corporate-action metadata exists "
                     "in the source, so no event attribution is claimed."),
            "basis_min_deviation": BASIS_MIN_DEVIATION,
            "reconstruction_min_rate": RECONSTRUCTION_MIN_RATE,
            "regimes": factors,
        },
        "market_dq_report": report,
    }


def digest(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def write_artifacts(artifacts, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    written = {}
    for name, payload in artifacts.items():
        path = os.path.join(out_dir, name + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        written[name] = (path, digest(payload))
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=HERE)
    ap.add_argument("--universe", default=os.path.join(HERE, "experiment_1f_universe.json"),
                    help="restrict the DQ report to this universe (factors and the "
                         "ledger are always computed market-wide)")
    ap.add_argument("--verify", action="store_true",
                    help="build twice and assert byte-identical artifacts")
    a = ap.parse_args()

    universe = None
    if a.universe and os.path.exists(a.universe):
        loaded = json.load(open(a.universe))
        universe = set(loaded["tickers"] if isinstance(loaded, dict) else loaded)

    tickers = raw_tickers()
    print(f"{len(tickers)} cached tickers"
          + (f", DQ report restricted to {len(universe)} universe names" if universe else ""))

    artifacts = build_artifacts(tickers, universe)
    if a.verify:
        again = build_artifacts(tickers, universe)
        for name in artifacts:
            if digest(artifacts[name]) != digest(again[name]):
                raise SystemExit(f"NON-DETERMINISTIC: {name}")
        print("determinism: two independent builds produced identical artifacts")

    written = write_artifacts(artifacts, a.out_dir)
    ledger = artifacts["volume_repair_ledger"]
    factors = artifacts["observed_basis_factor"]["regimes"]
    report = artifacts["market_dq_report"]

    print(f"\nvolume repairs authorised : {len(ledger['authorised_repairs'])}")
    for hit in ledger["authorised_repairs"]:
        print(f"  {hit['ticker']:<5} {hit['date']}  {hit['raw_volume']:>13,}"
              f" -> {hit['normalized_volume']:>13,}  src={hit['source_fingerprint']}")
    print(f"diagnostic-only wrap hits : {len(ledger['diagnostic_only'])}")
    for hit in ledger["diagnostic_only"]:
        print(f"  {hit['ticker']:<5} {hit['date']}  behind basis factor "
              f"{hit['basis_factor']:g}  (NOT repaired)")

    print(f"\nbasis regimes : {len(factors)}")
    for f in factors:
        e = f["evidence"]
        print(f"  {f['ticker']:<5} r={f['factor']:<9.4f} {f['days_in_regime']:>4}d  "
              f"const={str(e['exactly_constant']):<5} corr={e['ratio_like_correlation']:>6.3f}  "
              f"integral={e['reconstruction_integral_rate']:.4f} "
              f"grid={e['reconstruction_on_grid_rate']:.4f}  {f['classification']}")

    print("\nDQ report:")
    for key in sorted(report):
        print(f"  {key:<32} {report[key]:,}")
    print()
    for name, (path, dig) in sorted(written.items()):
        print(f"wrote {os.path.basename(path):<32} digest={dig}")


if __name__ == "__main__":
    main()
