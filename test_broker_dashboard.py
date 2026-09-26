"""Plain-script tests for broker_dashboard.py (py -3 test_broker_dashboard.py).

Offline only. send_document() is driven through an injected post and sleep, so
no test can reach Telegram. The fixture ctx mirrors the ctx contract of
BROKER_LEARNING.md §6 and its 2026-09-25 amendment (R6, h=60, big-move rates,
alpha cases, broker lift) and Amendment A2 (suspension shares at h=60, visible
alpha cases, Rp 200 jt eligibility, a page under 1 MB), including the ugly parts: NaN prices, a one-point
series, a basis break, a rule with no stats and a broker code named <script>.
Writes broker_dashboard_out/fixture_preview.html (gitignored) for eyeballing.
"""

import ast
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import date, timedelta

import broker_dashboard as bd

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "123456:SECRET-token-for-tests"
NAN = float("nan")


# ── fixture ──────────────────────────────────────────────────────────────────

def _sessions(end, n):
    out, d = [], date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def _ramp(final, n, peak_at=None, peak=None):
    """Integer cumulative lots rising to `final`, optionally via a peak (a
    buyer that turned seller near the end)."""
    if peak_at is None:
        return [int(round(final * (i + 1) / n)) for i in range(n)]
    up = [int(round(peak * (i + 1) / (peak_at + 1))) for i in range(peak_at + 1)]
    rest = n - peak_at - 1
    down = [int(round(peak + (final - peak) * (i + 1) / rest)) for i in range(rest)]
    return up + down


def _stat(h, status, mean, lo, hi, n_dates, hit, base, big, big_base, susp=None):
    if susp is None and h == 60:
        susp = 0.12
    return {"as_of": "2026-09-19", "ruleset": "v1", "h": h, "n_events": n_dates * 2,
            "n_dates": n_dates, "mean_excess": mean, "ci_lo": lo, "ci_hi": hi,
            "hit_rate": hit, "base_rate": base,
            "hit_edge": None if hit is None or base is None else hit - base,
            "daily_hit_edge": None if hit is None or base is None else hit - base - 0.01,
            "big_rate": big, "big_base_rate": big_base, "susp_rate": susp,
            "low_n": int(n_dates < max(30, 3 * h)),
            "status": status, "window_start": "2025-09-22", "window_end": "2026-09-19"}


def _rule(rid, direction, name, label, status, stats, weight=1.0, prev=None, live=None):
    return {"id": rid, "dir": direction, "name": name, "label": label,
            "desc": f"Deskripsi {rid}.", "weight": weight, "prev_weight": prev,
            "status": status, "stats_as_of": "2026-09-19" if stats else None,
            "stats": stats,
            "live": live or {"n_signals": 0, "n_scored": 0, "mean_excess": None,
                             "hit_rate": None, "base_rate": None}}


def fixture():
    dates = _sessions("2026-09-24", 40)
    n = len(dates)
    close = [9000.0 + 95.0 * i for i in range(n)]
    close[10], close[11] = None, NAN              # a gap the price line must lift over
    close[-1] = 12650.0
    series = [
        {"broker": "XL", "side": "buy", "cum_lots": _ramp(117140, n, n - 6, 150559)},
        {"broker": "LG", "side": "buy", "cum_lots": _ramp(116890, n)},
        {"broker": "AK", "side": "buy", "cum_lots": _ramp(40210, n)},
        {"broker": "<script>", "side": "buy", "cum_lots": _ramp(22000, n)},
        {"broker": "CC", "side": "buy", "cum_lots": _ramp(9000, n)},
        {"broker": "EL", "side": "sell", "cum_lots": _ramp(-151200, n)},
        {"broker": "HP", "side": "sell", "cum_lots": _ramp(-97310, n)},
        {"broker": "TP", "side": "sell", "cum_lots": _ramp(-70555, n)},
        {"broker": "YP", "side": "sell", "cum_lots": _ramp(-12000, n)},
        {"broker": "ZP", "side": "sell", "cum_lots": _ramp(-5000, n)},
    ]
    series[2]["cum_lots"][20] = None              # a gap in one broker line
    book = [
        {"broker": "XL", "position_lots": 117140, "avg_cost": 8526.0, "realized_rp": 2.1e10,
         "unrealized_rp": 4.8309e10, "total_rp": 6.9309e10, "pct_vs_cost": 12650 / 8526 - 1,
         "net1d": -7011, "net5d": -33419, "bavg20": 12210.0, "savg20": 12480.0},
        {"broker": "LG", "position_lots": 116890, "avg_cost": 12460.0, "realized_rp": 0.0,
         "unrealized_rp": 2.221e8, "total_rp": 2.221e8, "pct_vs_cost": 12650 / 12460 - 1,
         "net1d": 2100, "net5d": 11500, "bavg20": 12455.0, "savg20": NAN},
        {"broker": "<script>", "position_lots": 22000, "avg_cost": 10020.0,
         "realized_rp": -1.5e8, "unrealized_rp": 5.786e9, "total_rp": 5.636e9,
         "pct_vs_cost": 0.2625, "net1d": 0, "net5d": 400, "bavg20": None, "savg20": None},
        {"broker": "EL", "position_lots": -151200, "avg_cost": 10400.0, "realized_rp": 1.2e9,
         "unrealized_rp": -3.402e10, "total_rp": -3.282e10, "pct_vs_cost": 12650 / 10400 - 1,
         "net1d": -900, "net5d": -4100, "bavg20": 12000.0, "savg20": 12390.0},
        {"broker": "HP", "position_lots": -97310, "avg_cost": 9950.0, "realized_rp": 3.0e8,
         "unrealized_rp": -2.627e10, "total_rp": -2.597e10, "pct_vs_cost": 12650 / 9950 - 1,
         "net1d": -300, "net5d": -2500, "bavg20": None, "savg20": 12300.0},
        {"broker": "TP", "position_lots": -70555, "avg_cost": 11800.0, "realized_rp": -2.0e8,
         "unrealized_rp": -5.997e9, "total_rp": -6.197e9, "pct_vs_cost": 12650 / 11800 - 1,
         "net1d": 0, "net5d": -800, "bavg20": None, "savg20": 12500.0},
    ]
    sini = {"ticker": "SINI", "close": 12650.0, "change_1d": 0.052, "data_through": "2026-09-24",
            "anchor": dates[0], "basis_ok": True, "eligible": True, "fired": ["R2"],
            "score": -1.0,
            "explain": ["R2 · XL untung +48% dari modal Rp 8.526, jual 33.419 lot dalam 5 hari "
                        "(−22% posisinya)"],
            "book": book, "curves": {"dates": dates, "close": close, "series": series},
            "facts": {"top_holder": {"broker": "XL", "position_lots": 117140, "avg_cost": 8526.0},
                      "biggest_winner": {"broker": "XL", "total_rp": 6.9309e10},
                      "distributing": [{"broker": "XL", "net5d": -33419},
                                       {"broker": "EL", "net5d": -4100}]}}
    r6_dates = dates[-25:]
    bnbr = {"ticker": "BNBR", "close": 184.0, "change_1d": -0.011, "data_through": "2026-09-24",
            "anchor": r6_dates[0], "basis_ok": True, "eligible": True, "fired": ["R6"],
            "score": 1.07,
            "explain": ["R6 · BK kumpulkan 45.200 lot (3,1x ADV) selama 31 dari 60 hari, harga "
                        "sideways (range 18%), modal ~Rp 180 (+2%)"],
            "book": [{"broker": "BK", "position_lots": 45200, "avg_cost": 180.4,
                      "realized_rp": 1.0e6, "unrealized_rp": 1.63e7, "total_rp": 1.73e7,
                      "pct_vs_cost": 0.02, "net1d": 800, "net5d": 3900, "bavg20": 181.0,
                      "savg20": 183.0}],
            "curves": {"dates": r6_dates, "close": [180.0 + (i % 5) for i in range(25)],
                       "series": [{"broker": "BK", "side": "buy",
                                   "cum_lots": _ramp(45200, 25)}]},
            "facts": {"top_holder": {"broker": "BK", "position_lots": 45200, "avg_cost": 180.4},
                      "biggest_winner": {"broker": "BK", "total_rp": 1.73e7},
                      "distributing": []}}
    buva = {"ticker": "BUVA", "close": 410.0, "change_1d": 0.0, "data_through": "2026-09-24",
            "anchor": None, "basis_ok": False, "eligible": False, "fired": [], "score": 0.0,
            "explain": [], "book": None, "curves": None, "facts": {}}
    cass = {"ticker": "CASS", "close": 505.0, "change_1d": None, "data_through": "2026-09-23",
            "anchor": "2026-09-24", "basis_ok": True, "eligible": False, "fired": [],
            "score": None, "explain": [],
            "book": [{"broker": "AK", "position_lots": 1200, "avg_cost": 505.0,
                      "realized_rp": 0.0, "unrealized_rp": 0.0, "total_rp": 0.0,
                      "pct_vs_cost": 0.0, "net1d": 1200, "net5d": 1200, "bavg20": 505.0,
                      "savg20": None}],
            "curves": {"dates": ["2026-09-24"], "close": [505.0],
                       "series": [{"broker": "AK", "side": "buy", "cum_lots": [1200]}]},
            "facts": {"top_holder": {"broker": "AK", "position_lots": 1200, "avg_cost": 505.0}}}

    rules = [
        _rule("R1", 1, "acc_near_cost", "Harga dekat modal akumulator besar", "DIRECTIONAL",
              {"5": _stat(5, "DIRECTIONAL", 0.004, -0.003, 0.011, 44, 0.52, 0.47, 0.06, 0.04),
               "10": _stat(10, "DIRECTIONAL", 0.009, -0.004, 0.021, 41, 0.55, 0.48, 0.07, 0.05),
               "20": _stat(20, "CONSISTENT", 0.021, 0.004, 0.038, 38, 0.58, 0.49, 0.09, 0.06),
               "60": _stat(60, "NEUTRAL", -0.01, -0.09, 0.07, 31, 0.40, 0.36, 0.05, 0.042)},
              weight=1.07, prev=1.0,
              live={"h": 10, "n_signals": 12, "n_scored": 7, "mean_excess": 0.013, "hit_rate": 0.57,
                    "base_rate": 0.49}),
        _rule("R2", -1, "winner_distributing", "Akumulator paling untung sedang ambil untung",
              "CONSISTENT",
              {"5": _stat(5, "CONSISTENT", -0.012, -0.02, -0.004, 35, 0.41, 0.48, 0.02, 0.04),
               "10": _stat(10, "CONSISTENT", -0.018, -0.03, -0.006, 33, 0.40, 0.49, 0.03, 0.05),
               "20": _stat(20, "DIRECTIONAL", -0.02, -0.05, 0.01, 31, 0.43, 0.49, 0.05, 0.06),
               "60": _stat(60, "LOW_N", -0.04, -0.2, 0.1, 18, 0.30, 0.34, 0.03, 0.042)},
              weight=0.93, prev=0.95),
        _rule("R3", -1, "holders_underwater", "Akumulator besar nyangkut", "CONTRARY",
              {"10": _stat(10, "CONTRARY", 0.02, 0.006, 0.034, 40, 0.56, 0.48, 0.08, 0.05)},
              weight=0.8, prev=0.9),
        _rule("R4", 1, "fresh_accumulation", "Akumulasi baru di harga pasar", "LOW_N",
              {"10": _stat(10, "LOW_N", 0.03, -0.02, 0.08, 12, 0.6, 0.5, None, None)},
              weight=1.0, prev=None),
        _rule("R5", 1, "seller_exhaustion", "Penjual besar berhenti jual", None, {}),
        _rule("R6", 1, "stealth_accumulation", "Akumulasi diam-diam, harga sideways", "LOW_N",
              {"5": _stat(5, "LOW_N", 0.001, -0.02, 0.02, 14, 0.5, 0.47, 0.03, 0.04),
               "10": _stat(10, "LOW_N", 0.004, -0.03, 0.04, 14, 0.52, 0.48, 0.04, 0.05),
               "20": _stat(20, "LOW_N", 0.012, -0.05, 0.08, 13, 0.5, 0.49, 0.06, 0.06),
               "60": _stat(60, "LOW_N", 0.31, -0.12, 0.74, 11, 0.64, 0.34, 0.18, 0.042,
                           susp=0.27)},
              weight=1.0,
              live={"h": 60, "n_signals": 4, "n_scored": 2, "mean_excess": 0.08,
                    "hit_rate": 0.5, "base_rate": 0.4, "susp_rate": 0.5}),
    ]
    return {
        "generated_utc": "2026-09-24T10:41Z", "data_through": "2026-09-24", "ruleset": "v1",
        "run": {"kind": "daily", "tickers_ok": 45, "tickers_fail": 1,
                "failed": {"XXXX": "short window (12 sessions)"},
                "empty": {"DEAD": "empty date axis: 0 sessions"},
                "warnings": ["BUVA: basis break on the last session", "CASS: data s.d. 2026-09-23"]},
        "rules": rules,
        "tickers": [cass, buva, bnbr, sini],
        "brokers": {"as_of": "2026-09-19", "scores": [
            {"broker": "XL", "side": "buy", "h": 10, "n_events": 90, "n_dates": 61,
             "n_tickers": 40, "mean_excess": 0.006, "ci_lo": -0.002, "ci_hi": 0.014,
             "shrunk": 0.0046, "low_n": False},
            {"broker": "<script>", "side": "buy", "h": 10, "n_events": 20, "n_dates": 12,
             "n_tickers": 9, "mean_excess": 0.02, "ci_lo": -0.01, "ci_hi": 0.05,
             "shrunk": 0.0075, "low_n": True},
            {"broker": "EL", "side": "sell", "h": 10, "n_events": 70, "n_dates": 50,
             "n_tickers": 33, "mean_excess": -0.008, "ci_lo": -0.015, "ci_hi": -0.001,
             "shrunk": -0.0057, "low_n": False},
            {"broker": "XL", "side": "buy", "h": 60, "n_events": 60, "n_dates": 40,
             "n_tickers": 30, "mean_excess": 0.05, "ci_lo": -0.02, "ci_hi": 0.12,
             "shrunk": 0.0125, "low_n": False},
        ], "profitability": [
            {"broker": "XL", "n_tickers": 310, "total_pnl_rp": 1.618e11, "turnover_rp": 1.2e13,
             "pnl_per_turnover": 0.0135, "share_profitable": 0.61},
            {"broker": "EL", "n_tickers": 280, "total_pnl_rp": -4.2e10, "turnover_rp": 8.0e12,
             "pnl_per_turnover": -0.0053, "share_profitable": 0.44},
        ]},
        "alpha": {"as_of": "2026-09-19", "n_cases": 23, "n_visible": 19, "susp_rate": 0.3,
                  "cases": [
                      {"ticker": "PADI", "session_date": "2026-03-02", "hold_60": 0.62,
                       "susp_60": 0, "sessions_before": 131, "visible": 1,
                       "rules_at_t": "", "rules_prior20": "R1", "top_broker": "YP",
                       "top_nl60_adv": 1.4, "top_cost_gap": -0.03, "range60": 0.41,
                       "val20": 3.1e9},
                      {"ticker": "RATU", "session_date": "2026-01-12", "hold_60": 3.80,
                       "susp_60": 1, "sessions_before": 79, "visible": 0,
                       "rules_at_t": "R6", "rules_prior20": "R1,R6", "top_broker": "BK",
                       "top_nl60_adv": 3.1, "top_cost_gap": 0.02, "range60": 0.18,
                       "val20": 1.4e9},
                      {"ticker": "SAME", "session_date": "2026-05-20", "hold_60": None,
                       "susp_60": None, "sessions_before": None, "visible": None,
                       "rules_at_t": None, "rules_prior20": None, "top_broker": None,
                       "top_nl60_adv": NAN, "top_cost_gap": None, "range60": None,
                       "val20": None},
                  ],
                  "lift": [
                      {"broker": "BK", "n_cases_top": 5, "n_rows_top": 900, "case_share": 5 / 23,
                       "row_share": 900 / 60000, "lift": (5 / 23) / (900 / 60000)},
                      {"broker": "YP", "n_cases_top": 4, "n_rows_top": 5200,
                       "case_share": 4 / 23, "row_share": 5200 / 60000,
                       "lift": (4 / 23) / (5200 / 60000)},
                      {"broker": "ZZ", "n_cases_top": 2, "n_rows_top": 10, "case_share": 2 / 23,
                       "row_share": 10 / 60000, "lift": 521.7},
                  ],
                  "base": {"big_base_rate_60": 0.042, "susp_rate_60": 0.08}},
        "learned": ["R1 bobot 1,00 -> 1,07 (n=41 tanggal, DIRECTIONAL)",
                    "R3 bobot 0,90 -> 0,80 (n=40 tanggal, CONTRARY)"],
        "disclaimer": bd.DISCLAIMER,
    }


def _card(html, ticker):
    """The card's HTML up to the next card (cards nest a chart-table <details>)."""
    m = re.search(rf'<details class="card" id="t-{ticker}"[^>]*>.*?'
                  rf'(?=<details class="card"|</section>)', html, re.S)
    assert m, ticker
    return m.group(0)


def _rule_html(html, rid):
    """One rule block of the scorecard (not the chip in the signal list)."""
    start = html.index(f'<div class="rule"><div class="rh"><b>{rid}</b>')
    ends = [i for i in (html.find('<div class="rule">', start + 1), html.find("<h3>", start))
            if i > 0]
    return html[start:min(ends)]


# ── formatting ───────────────────────────────────────────────────────────────

def test_rupiah_lot_and_percent_formatting():
    assert bd.fmt_rp(12077) == "Rp 12.077"
    assert bd.fmt_rp(8526.4) == "Rp 8.526"
    assert bd.fmt_rp_big(161.8e9) == "Rp 161,8 M"
    assert bd.fmt_rp_big(1.23e9) == "Rp 1,23 M"
    assert bd.fmt_rp_big(1.2e12) == "Rp 1,2 T"
    assert bd.fmt_rp_big(999.97e9) == "Rp 1 T"
    assert bd.fmt_rp_big(-3.282e10, True) == "−Rp 32,8 M"
    assert bd.fmt_rp_big(6.9309e10, True) == "+Rp 69,3 M"
    assert bd.fmt_rp_big(0) == "Rp 0"
    assert bd.fmt_lot(117140) == "117.140 lot"
    assert bd.fmt_lot(-33419) == "−33.419 lot"
    assert bd.fmt_lot(116890, True) == "+116.890 lot"
    assert bd.fmt_pct(0.052, 1, True) == "+5,2%"
    assert bd.fmt_pct(3.8, 0, True) == "+380%"
    assert bd.fmt_pp(0.07) == "+7,0 pp"
    assert bd.fmt_num(1.07, 2) == "1,07"
    assert bd.fmt_int(-0.4, True) == "0", "a rounded zero never prints as −0"


def test_missing_values_print_as_dash_never_zero():
    for fn in (bd.fmt_rp, bd.fmt_rp_big, bd.fmt_lot, bd.fmt_pct, bd.fmt_int, bd.fmt_num):
        for v in (None, NAN, float("inf"), "x", True):
            assert fn(v) == bd.MISSING, (fn.__name__, v)


# ── the page ─────────────────────────────────────────────────────────────────

def test_fixture_renders_every_section_in_indonesian():
    html = bd.render(fixture())
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    assert '<html lang="id">' in html
    for sid in ("sinyal", "ticker", "belajar", "alpha", "broker", "kualitas"):
        assert f'<section id="{sid}">' in html, sid
    for text in ("Sinyal hari ini", "Per ticker", "Apa yang sudah dipelajari",
                 "Kasus alpha (retrospektif)", "Papan peringkat broker", "Kualitas data",
                 "Ledger live", "Perubahan bobot vs minggu lalu", "Profitabilitas trading",
                 "R1 bobot 1,00 -&gt; 1,07", "short window (12 sessions)",
                 "BUVA: basis break on the last session", "6 aturan × 4 horizon = 24 uji"):
        assert text in html, text


def test_disclaimer_in_header_and_footer():
    for ctx in (fixture(), {}):
        html = bd.render(ctx)
        header = html[html.index("<header>"):html.index("</header>")]
        footer = html[html.index("<footer>"):html.index("</footer>")]
        for part in (header, footer):
            assert "bukan kepemilikan sebenarnya" in part
            assert "Research-grade" in part


def test_page_makes_no_external_request():
    html = bd.render(fixture())
    for bad in ("http://", "https://", "src=", "@import", "url(", "<link", "@font-face"):
        assert bad not in html, bad


def test_dynamic_text_is_escaped():
    html = bd.render(fixture())
    assert html.count("<script>") == 1, "only the page's own hover script"
    assert "&lt;script&gt;" in html
    ctx = fixture()
    ctx["tickers"][3]["explain"] = ['<img onerror="x">']
    ctx["run"]["warnings"] = ["<b>bold</b>"]
    ctx["alpha"]["cases"][0]["top_broker"] = "<i>"
    html = bd.render(ctx)
    for raw in ('<img onerror', "<b>bold</b>", "<i></dd>"):
        assert raw not in html, raw
    assert "&lt;img onerror=&quot;x&quot;&gt;" in html


def test_signed_values_are_coloured_and_signed():
    html = bd.render(fixture())
    pos = re.findall(r'<span class="pos">([^<]*)</span>', html)
    neg = re.findall(r'<span class="neg">([^<]*)</span>', html)
    assert pos and neg
    assert all(t.startswith("+") for t in pos), [t for t in pos if not t.startswith("+")]
    assert all(t.startswith(bd.MINUS) for t in neg), [t for t in neg if not t.startswith(bd.MINUS)]
    sini = _card(html, "SINI")
    assert '<span class="neg">−33.419</span>' in sini           # XL net 5 sesi
    assert '<span class="pos">+117.140 lot</span>' in sini
    assert '<span class="pos">+48,4%</span>' in sini             # XL price vs cost
    assert "Rp 8.526" in sini and "Rp 12.460" in sini
    assert '<span class="neg">−151.200 lot</span>' in sini
    assert "bukan short" in sini, "a negative position is labelled as distribution"


def test_statuses_carry_indonesian_label_and_explanation():
    html = bd.render(fixture())
    for code, label in bd.STATUS_LABEL.items():
        m = re.search(rf'<span class="st" data-status="{code}" title="([^"]+)">'
                      rf'<span aria-hidden="true">[^<]+</span> {re.escape(label)}</span>', html)
        assert m, code
        assert m.group(1) == bd.escape(bd.STATUS_NOTE[code], quote=True)
    assert "belum dinilai" in html, "R5 has no status and no stats"
    assert "terbukti" not in html.replace("belum terbukti", "").replace("&#x27;terbukti&#x27;", ""), \
        "nothing is ever called proven"


def test_every_hit_and_big_rate_sits_beside_its_base():
    html = bd.render(fixture())
    paired = re.findall(r'<span class="hb">[^<]* <span class="vs">vs</span> (?:base|rata-rata) ',
                        html)
    assert html.count('<span class="hb">') == len(paired) >= 20
    assert "18,0% <span class=\"vs\">vs</span> rata-rata 4,2% (≥ +50%)" in html   # R6 h=60
    assert "hit 57% <span class=\"vs\">vs</span> base 49%" in html               # live R1


def test_scorecard_has_h60_primary_and_big_move_columns():
    html = bd.render(fixture())
    assert "Naik besar vs rata-rata" in html
    r6 = _rule_html(html, "R6")
    assert "h=60 (utama)" in r6 and "h=10 (utama)" not in r6
    assert "horizon utama h=60" in r6
    r1 = _rule_html(html, "R1")
    assert "h=10 (utama)" in r1 and "h=60<br>" in r1
    assert '<circle class="clip"' in r6, "the +74% CI end is past the domain and marked"
    r5 = _rule_html(html, "R5")
    assert "Belum ada statistik retrospektif" in r5


def test_live_ledger_shows_each_rules_own_horizon():
    # The orchestrator passes each rule's live row at its primary h (R6 at 60);
    # the heading must not claim one h for all of them.
    def live(h):
        return {"h": h, "n_signals": 3, "n_scored": 1, "mean_excess": 0.01,
                "hit_rate": 1.0, "base_rate": 0.5}
    mixed = bd.render({"rules": [_rule("R1", 1, "a", "A", "LOW_N", {}, live=live(10)),
                                 _rule("R6", 1, "f", "F", "LOW_N", {}, live=live(60))]})
    sec = mixed[mixed.index("Ledger live"):mixed.index("Perubahan bobot")]
    assert "h utama tiap aturan" in sec and '<th scope="col">h</th>' in sec
    assert "<td>R1</td><td>10</td>" in sec and "<td>R6</td><td>60</td>" in sec
    same = bd.render({"rules": [_rule("R1", 1, "a", "A", "LOW_N", {}, live=live(10))]})
    sec = same[same.index("Ledger live"):same.index("Perubahan bobot")]
    assert "(prospektif, h=10)" in sec and '<th scope="col">h</th>' not in sec


def test_alpha_section_cases_explanation_and_lift():
    html = bd.render(fixture())
    sec = html[html.index('<section id="alpha">'):]
    sec = sec[:sec.index("</section>")]
    assert sec.index("RATU") < sec.index("PADI") < sec.index("SAME"), "largest move first"
    assert '<span class="pos">+380%</span>' in sec
    assert "R1, R6" in sec and "tidak ada" in sec
    assert '<span class="pos">+3,1× ADV20</span>' in sec
    assert "Rp 1,4 M" in sec
    assert "jauh lebih sering" in sec and "Ujian yang sebenarnya" in sec
    assert "naik besar vs" in sec and "h=60" in sec
    assert "rata-rata 4,2% saham eligible naik" in sec
    assert ("kode broker = perusahaan sekuritas berisi banyak nasabah, bukan identitas bandar"
            in sec.lower())
    assert "14,49×" in sec and "BK" in sec
    assert "ZZ" not in sec, "lift from fewer than 3 cases is not shown"
    assert "23 kasus" in sec and "19 terlihat" in sec
    # Amendment A2: invisible cases are marked, lift is on visible cases only,
    # and the per-case look-alike count gave way to the h = 60 big-move table
    ratu = sec[sec.index("RATU"):sec.index("PADI")]
    assert "fase akumulasi sebelum data dimulai — tidak terlihat" in ratu.lower()
    assert "kena suspensi" in ratu and "Sesi sebelum T dalam data" in ratu
    padi = sec[sec.index("PADI"):sec.index("SAME")]
    assert "tidak terlihat" not in padi and "kena suspensi</span>" not in padi
    assert "yang terlihat" in sec and "look-alike" not in sec.lower().replace("pengganti hitungan look-alike", "")
    assert "Look-alike (" not in sec


def test_cards_open_only_when_fired_and_flag_problems():
    html = bd.render(fixture())
    assert re.search(r'<details class="card" id="t-SINI" open>', html)
    assert re.search(r'<details class="card" id="t-BNBR" open>', html)
    assert re.search(r'<details class="card" id="t-CASS">', html)
    assert re.search(r'<details class="card" id="t-BUVA">', html)
    buva = _card(html, "BUVA")
    assert "basis putus" in buva and "Basis lot dan harga" in buva and "<svg" not in buva
    cass = _card(html, "CASS")
    assert "tidak eligible" in cass and "Rp 200 jt" in cass and "data s.d. 2026-09-23" in cass
    assert html.index('id="t-BNBR"') < html.index('id="t-SINI"') < html.index('id="t-BUVA"'), \
        "fired first, strongest score first"
    bnbr = _card(html, "BNBR")
    assert "R6 · BK kumpulkan 45.200 lot" in bnbr


def test_mobile_first_layout():
    html = bd.render(fixture())
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert html.count("<table") == html.count('<div class="tw"><table') > 0
    assert re.search(r"\.tw\{overflow-x:auto", html)
    css = html[html.index("<style>"):html.index("</style>")]
    for prop, px in re.findall(r"(?<![-\w(])(width|min-width):\s*(\d+)px", css):  # not @media
        assert int(px) <= 360, (prop, px)
    for w in re.findall(r'viewBox="0 0 (\d+) \d+"', html):
        assert int(w) <= 360, w
    assert re.search(r'<svg[^>]*\swidth="', html) is None


def test_dark_mode_is_selected_not_flipped():
    html = bd.render({})
    assert "@media (prefers-color-scheme: dark){:root:not([data-theme=\"light\"])" in html
    assert ':root[data-theme="dark"]' in html
    assert re.search(r"body\{[^}]*background:var\(--page\)", html)
    assert bd._DARK["surface"] != bd._LIGHT["surface"]
    assert set(bd._DARK) == set(bd._LIGHT)


def test_chart_handles_gaps_nan_and_single_point():
    ctx = fixture()
    sini = ctx["tickers"][3]
    svg = bd._chart(sini["curves"], "SINI", sini["anchor"])
    price = re.search(r'<path class="ln px" d="([^"]+)"', svg).group(1)
    assert price.count("M") == 2, "the price line lifts the pen over None and NaN"
    assert "nan" not in svg.lower().replace("tanggal", "")
    assert svg.count('<path class="ln s') == 6, "3 buyers + 3 sellers"
    ak = re.search(r'<path class="ln s3" d="([^"]+)"', svg).group(1)   # AK, third buyer
    assert ak.count("M") == 2
    for label in ("XL +117,1rb", "LG +116,9rb", "EL −151,2rb"):
        assert label in svg, label
    assert "Harga penutupan (Rp)" in svg and "Kumulatif net lot sejak anchor" in svg
    assert 'class="lg"' in svg and "Beli bersih" in svg and "Jual bersih" in svg
    assert "Tabel data grafik" in svg

    one = bd._chart(ctx["tickers"][0]["curves"], "CASS")
    assert "<svg" in one and "<path" not in one, "one point: dots, no paths"
    assert 'class="dot px-d"' in one and "AK +1,2rb" in one

    assert bd._chart(None, "X") == ""
    assert bd._chart({"dates": ["2026-09-24"], "close": [None], "series": []}, "X") == ""
    only_nan = bd._chart({"dates": ["a", "b"], "close": [NAN, 5.0],
                          "series": [{"broker": "Q", "cum_lots": [None, None]}]}, "X")
    assert "<svg" in only_nan and "belum ada posisi" in only_nan


def test_empty_and_partial_ctx_still_render():
    for ctx in ({}, None, {"data_through": None, "tickers": [], "rules": [], "run": {}},
                {"tickers": [{"ticker": "AAAA"}], "rules": [{"id": "R9"}], "alpha": {"cases": [{}]},
                 "brokers": {"scores": [{}], "profitability": [{}]}}):
        html = bd.render(ctx)
        assert "Kasus alpha (retrospektif)" in html and "</html>" in html
        assert "https://" not in html
        cap = bd.caption(ctx)
        assert 0 < len(cap) <= bd.CAPTION_LIMIT and "research-grade" in cap
    html = bd.render({})
    for text in ("Tidak ada aturan yang aktif", "Belum ada kasus alpha", "Belum ada skor broker",
                 "Belum ada broker dengan ≥ 3 kasus"):
        assert text in html, text


def test_caption_is_plain_bounded_and_complete():
    cap = bd.caption(fixture())
    assert "<" not in cap and ">" not in cap
    assert "2026-09-24" in cap and "research-grade" in cap
    assert "SINI R2 · skor −1,00" in cap and "BNBR R6 · skor +1,07" in cap
    assert cap.index("BNBR") < cap.index("SINI"), "strongest score first"
    assert "bukan kepemilikan sebenarnya" in cap

    ctx = fixture()
    ctx["tickers"] = [{"ticker": f"T{i:03d}", "fired": ["R1", "R4", "R6"], "score": 3.0 - i / 100}
                      for i in range(50)]
    cap = bd.caption(ctx)
    assert len(cap) <= bd.CAPTION_LIMIT
    m = re.search(r"…dan (\d+) lainnya", cap)
    assert m, cap
    shown = len(re.findall(r"^• T\d{3} R1\+R4\+R6 · skor", cap, re.M))
    assert shown + int(m.group(1)) == 50 and shown > 5
    assert "T000" in cap and "research-grade" in cap and "2026-09-24" in cap


def test_write_names_file_after_data_through():
    with tempfile.TemporaryDirectory() as tmp:
        path = bd.write(bd.render(fixture()), out_dir=tmp)
        assert os.path.basename(path) == "broker_dashboard_2026-09-24.html"
        assert os.path.getsize(path) > 10_000 and not os.path.exists(path + ".tmp")
        path = bd.write(bd.render({}), out_dir=tmp)
        assert os.path.basename(path) == "broker_dashboard_undated.html"
        path = bd.write("<p>x</p>", out_dir=tmp, name="../escape.html")
        assert os.path.dirname(path) == tmp


def test_write_fixture_preview():
    path = bd.write(bd.render(fixture()), name="fixture_preview.html")
    assert path == os.path.join(HERE, "broker_dashboard_out", "fixture_preview.html")
    print(f"    preview: {path} ({os.path.getsize(path):,} bytes)")


# ── delivery ─────────────────────────────────────────────────────────────────

class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self.ok = status < 400
        self._body = body if body is not None else {"ok": status < 400}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _doc():
    fd, path = tempfile.mkstemp(suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("<p>dash</p>")
    return path


def test_send_document_multipart_and_429_retry():
    path, calls, slept = _doc(), [], []
    replies = [Resp(429, {"ok": False, "parameters": {"retry_after": 99}}), Resp(200)]

    def post(url, data=None, files=None, timeout=None):
        calls.append((url, data, files, timeout))
        return replies.pop(0)

    try:
        out = io.StringIO()
        with redirect_stdout(out):
            assert bd.send_document(TOKEN, 42, path, "cap " * 400, post=post, sleep=slept.append)
        assert out.getvalue() == ""
        assert slept == [30], "retry_after is honoured but capped at 30 s"
        url, data, files, timeout = calls[0]
        assert url == f"https://api.telegram.org/bot{TOKEN}/sendDocument"
        assert data["chat_id"] == "42" and len(data["caption"]) == bd.CAPTION_LIMIT
        name, content, ctype = files["document"]
        assert name == os.path.basename(path) and content == b"<p>dash</p>" and ctype == "text/html"
        assert timeout and len(calls) == 2

        calls.clear(), slept.clear()
        replies[:] = [Resp(429, {"parameters": {"retry_after": 2}}), Resp(429, ValueError()),
                      Resp(429, {})]
        with redirect_stdout(io.StringIO()) as out:
            assert bd.send_document(TOKEN, 42, path, "c", post=post, sleep=slept.append) is False
        assert len(calls) == bd.SEND_TRIES and slept == [2, bd.RETRY_AFTER_DEFAULT]
        assert "rate-limited" in out.getvalue()
    finally:
        os.remove(path)


def test_send_document_never_prints_token_or_url():
    path = _doc()

    def boom(url, **kw):
        raise ConnectionError(f"Max retries exceeded with url: {url} (token {TOKEN})")

    def bad(url, **kw):
        return Resp(400, {"ok": False, "description": f"bad request {url}"})

    try:
        for post, expect in ((boom, "ConnectionError"), (bad, "400")):
            out = io.StringIO()
            with redirect_stdout(out):
                assert bd.send_document(TOKEN, 42, path, "c", post=post, sleep=lambda s: None) is False
            text = out.getvalue()
            assert expect in text, text
            for secret in (TOKEN, "SECRET", "api.telegram.org", "https://"):
                assert secret not in text, secret
        out = io.StringIO()
        with redirect_stdout(out):
            assert bd.send_document(TOKEN, 42, path + ".missing", "c", post=boom) is False
        assert "FileNotFoundError" in out.getvalue() and TOKEN not in out.getvalue()
    finally:
        os.remove(path)


def test_send_from_env_without_secrets_reports_names_only():
    saved = {k: os.environ.pop(k, None) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            assert bd.send_from_env("nope.html", "c") is False
        assert "TELEGRAM_BOT_TOKEN" in out.getvalue()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ── module hygiene ───────────────────────────────────────────────────────────

def test_module_is_pure_and_clean():
    src_path = os.path.join(HERE, "broker_dashboard.py")
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    assert "sharpe" not in src.lower()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "sqrt":
            assert "252" not in ast.dump(node), "no annualisation"
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.col_offset == 0:
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            for bad in ("requests", "neobdm_scraper", "playwright", "pandas", "numpy"):
                assert bad not in names, bad
    code = "import sys, broker_dashboard; print('requests' in sys.modules)"
    res = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout.strip() == "False", res.stderr


def test_every_h60_number_carries_its_suspension_share():
    html = bd.render(fixture())
    r6 = _rule_html(html, "R6")
    row60 = r6[r6.index("h=60 (utama)"):]
    row60 = row60[:row60.index("</tr>")]
    assert '<span class="sp">kena suspensi 27%</span>' in row60
    r1 = _rule_html(html, "R1")
    assert "kena suspensi 12%" in r1
    assert r1.count("kena suspensi") == 2, "the h=60 row and its CI tooltip, nothing else"
    assert "kena suspensi 27%" in re.search(r"<title>h=60:[^<]*</title>", r6).group(0)
    live = html[html.index("Ledger live"):html.index("Perubahan bobot")]
    r6_live = live[live.index("<td>R6</td>"):]
    r6_live = r6_live[:r6_live.index("</tr>")]
    assert "kena suspensi 50%" in r6_live
    r1_live = live[live.index("<td>R1</td>"):]
    assert "suspensi" not in r1_live[:r1_live.index("</tr>")], "h=10 never bridges one"
    alpha = html[html.index('<section id="alpha">'):]
    alpha = alpha[:alpha.index("</section>")]
    assert "kena suspensi 30%" in alpha                      # of the cases
    assert "kena suspensi 8%" in alpha                       # of the base rate
    table = alpha[alpha.index("Naik besar per aturan"):]
    assert "kena suspensi 27%" in table and "kena suspensi 12%" in table


def test_h60_verdicts_explained_as_not_enough_data_yet():
    html = bd.render(fixture())
    sec = html[html.index('<section id="belajar">'):]
    sec = sec[:sec.index("</section>")]
    assert "return pemegang" in sec and "dijembatani" in sec
    assert 'status h=60 masih "data belum cukup"' in sec.lower().replace("&quot;", '"')
    assert "180 tanggal event" in sec and "1 tahun" in sec
    assert "bootstrap blok melingkar" in sec
    assert "max(30, 3×h)" in bd.STATUS_NOTE["LOW_N"] and "180" in bd.STATUS_NOTE["LOW_N"]
    q = html[html.index('<section id="kualitas">'):]
    assert "1 kosong" in q and "DEAD" in q and "delisting" in q


def _walk_ctx(n_tickers=46, n=240, seed=7):
    """n_tickers cards shaped like a real watchlist run: a year of sessions,
    ten broker lines in the millions of lots, twelve book rows. Lines are a
    drift with bursts and day-to-day noise, like the cumulative net lots of
    the 2026-08-21 integration page (whose 46 cards this test stands in for),
    and prices move up to 6% a day."""
    import random
    rng = random.Random(seed)
    dates = _sessions("2026-09-24", n)
    tickers = []
    for k in range(n_tickers):
        px, close = rng.uniform(80, 9000), []
        for _ in range(n):
            px = max(50.0, px * (1 + rng.uniform(-0.06, 0.06)))
            close.append(float(round(px)))
        series = []
        for b in range(10):
            lots, cum = 0.0, []
            drift = rng.uniform(2000, 20000) * (1 if b < 5 else -1)
            for _ in range(n):
                burst = rng.gauss(0, 60000) if rng.random() < 0.08 else 0.0
                lots += drift + burst + rng.gauss(0, abs(drift) * 0.3)
                cum.append(float(round(lots)))
            series.append({"broker": f"B{b}", "side": "buy" if b < 5 else "sell", "cum_lots": cum})
        book = [{"broker": f"B{b}", "position_lots": series[b]["cum_lots"][-1] * 1.0,
                 "avg_cost": close[-1] * rng.uniform(0.7, 1.3), "realized_rp": rng.gauss(0, 5e9),
                 "unrealized_rp": rng.gauss(0, 5e10), "total_rp": rng.gauss(0, 5e10),
                 "pct_vs_cost": rng.uniform(-0.4, 0.4), "net1d": rng.gauss(0, 1e4),
                 "net5d": rng.gauss(0, 5e4), "bavg20": close[-1] * 0.98, "savg20": close[-1] * 1.01}
                for b in range(12) if b < 10] + [
                {"broker": f"X{b}", "position_lots": 10.0 * b, "avg_cost": 100.0, "realized_rp": 0.0,
                 "unrealized_rp": 0.0, "total_rp": 0.0, "pct_vs_cost": 0.0, "net1d": 0.0,
                 "net5d": 0.0, "bavg20": None, "savg20": None} for b in range(2)]
        tickers.append({"ticker": f"T{k:03d}", "close": close[-1], "change_1d": 0.01,
                        "data_through": dates[-1], "anchor": dates[0], "basis_ok": True,
                        "eligible": True, "fired": ["R1"] if k % 5 == 0 else [], "score": 1.0,
                        "explain": ["R1 · penjelasan singkat aturan"] if k % 5 == 0 else [],
                        "book": book, "curves": {"dates": dates, "close": close, "series": series},
                        "facts": {"top_holder": {"broker": "B0", "position_lots": 1e6,
                                                 "avg_cost": 1000.0},
                                  "biggest_winner": {"broker": "B1", "total_rp": 1e10},
                                  "distributing": [{"broker": "B5", "net5d": -1e4}]}})
    ctx = fixture()
    ctx["tickers"] = tickers
    return ctx, dates


def test_page_stays_under_1mb_for_46_tickers():
    ctx, dates = _walk_ctx()
    html = bd.render(ctx)
    size = len(html.encode("utf-8"))
    print(f"    46 tickers x 240 sessions: {size:,} bytes")
    assert size < 1_000_000, size
    # nothing was dropped to get there: every card keeps its chart and book
    assert html.count('<figure class="ch"') == 46 and html.count('<table class="book">') == 46
    assert html.count('<path class="ln s') == 46 * 6 and html.count('<path class="ln px"') == 46
    assert html.count("<tr>") > 46 * 12

    # the thinned hover data decodes back to the exact curve values
    t = ctx["tickers"][0]
    fig = re.search(r'<figure class="ch"[^>]*data-json="([^"]*)"', html).group(1)
    data = json.loads(fig.replace("&quot;", '"').replace("&amp;", "&"))
    # the session list, rebuilt as the hover script does: every st-th + the last
    idx = list(range(0, data["n"], data["st"]))
    idx += [] if idx[-1] == data["n"] - 1 else [data["n"] - 1]
    assert data["n"] == 240 and len(idx) <= bd.HOVER_POINTS + 1 and idx[-1] == 239
    assert "d.i.push(k)" in html and "k+=d.st" in html
    d0 = date.fromisoformat(data["d0"])
    assert [(d0 + timedelta(days=g)).isoformat() for g in data["g"]] == [dates[i] for i in idx]
    assert data["c"] == [int(t["curves"]["close"][i]) for i in idx]
    by_broker = {s["broker"]: s["cum_lots"] for s in t["curves"]["series"]}
    for s in data["s"]:
        assert s["v"] == [int(by_broker[s["b"]][i]) for i in idx], s["b"]


def _decode(d):
    """Absolute polylines from a path written as 'M x,y l dx dy ...' runs."""
    runs = []
    for seg in re.findall(r"M[^M]*", d):
        head, _, rest = seg[1:].partition("l")
        x, y = (float(v) for v in head.split(","))
        nums = [float(v) for v in re.findall(r"-?(?:\d+\.?\d*|\.\d+)", rest)]
        pts = [(x, y)]
        for dx, dy in zip(nums[::2], nums[1::2]):
            x, y = x + dx, y + dy
            pts.append((x, y))
        runs.append(pts)
    return runs


def _dist(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def test_compact_path_draws_the_same_line():
    import random
    rng = random.Random(3)
    xs = [46 + 220 * i / 239 for i in range(240)]
    vals = []
    v = 50.0
    for i in range(240):
        v += rng.uniform(-3, 3) if i % 40 else 25
        vals.append(None if i in (100, 101) else v)
    d, lone = bd._path(xs, vals, lambda u: 200 - u)
    runs = _decode(d)
    assert len(runs) == 2 and not lone, "the pen lifts over the gap"
    pts = [(x, 200 - u) for x, u in zip(xs, vals) if u is not None]
    for p in pts:
        best = min(_dist(p, a, b) for run in runs for a, b in zip(run, run[1:]))
        assert best <= bd.PATH_EPS + 0.1, (p, best)          # + rounding to 0.1
    assert runs[0][0][0] == 46.0 and abs(runs[0][0][1] - (200 - vals[0])) <= 0.051
    assert abs(runs[1][-1][0] - 266.0) < 1e-6
    old = sum(len(f"L{x:.1f},{y:.1f} ") for x, y in pts)
    assert len(d) < old * 0.6, (len(d), old)
    # a straight line collapses to its ends
    d, _ = bd._path(xs, [2.0 * i for i in range(240)], lambda u: 200 - u / 10)
    assert len(_decode(d)[0]) == 2


def test_mirrored_constants_match_the_ruleset():
    """The dashboard mirrors these instead of importing them (render needs
    only the ctx); this catches drift when the ruleset or metrics change."""
    import broker_learning as bl
    import broker_rules as br
    assert bd.HORIZONS == tuple(bl.HORIZONS)
    assert bd.BIG == dict(bl.BIG)
    assert (bd.ALPHA_H, bd.ALPHA_MIN) == (bl.ALPHA_H, bl.ALPHA_MIN)
    assert bd.PRIMARY_H == dict(br.PRIMARY_H)
    assert [r["id"] for r in br.RULES] == list(bd.PRIMARY_H)
    assert bd.MIN_VAL20 == br.MIN_VAL20 == 2e8
    assert "Rp 200 jt" in bd.render({"tickers": [{"ticker": "AAAA"}]})
    assert (bd.HOLD_H, bd.VISIBLE_MIN) == (bl.HOLD_H, bl.VISIBLE_MIN)
    assert all(bd._low_n_min(h) == bl.low_n_min(h) for h in bd.HORIZONS)
    assert bd.ROUND_TRIP_COST == bl.ROUND_TRIP_COST


def test_unit_boundaries_round_before_choosing_the_unit():
    """Review finding: a value just under the next unit printed as
    '1.000rb' / 'Rp 1.000.000' instead of '1jt' / 'Rp 1 jt'."""
    assert bd._lots_compact(999_960) == "+1jt" and bd._lots_compact(-999_960) == "−1jt"
    assert bd._lots_compact(999.6) == "+1rb" and bd._lots_compact(999_949) == "+999,9rb"
    assert bd._lots_compact(999.4) == "+999" and bd._lots_compact(1.25e6) == "+1,25jt"
    assert bd._lots_compact(0.3) == "0" and bd._lots_compact(-0.3) == "0"
    assert bd.fmt_rp_big(999_999.7) == "Rp 1 jt" and bd.fmt_rp_big(999_999.4) == "Rp 999.999"
    assert bd.fmt_rp_big(-999_999.7, True) == "−Rp 1 jt"
    assert bd.fmt_rp_big(999.96e6) == "Rp 1 M"        # the scaled promotion still holds


def test_send_document_retries_network_errors_and_5xx():
    """Review finding: one ConnectionError or 502 lost the day's page. They
    are retried after TRANSIENT_WAITS; a plain 4xx is still final."""
    path = _doc()

    class Timeout(OSError):          # the name requests uses; matched by class name
        pass

    try:
        for first in (ConnectionError("down"), Timeout("slow"), Resp(502), Resp(503)):
            calls, slept, replies = [], [], [first, Resp(200)]

            def post(url, data=None, files=None, timeout=None):
                calls.append(url)
                r = replies.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r

            with redirect_stdout(io.StringIO()) as out:
                assert bd.send_document(TOKEN, 42, path, "c", post=post, sleep=slept.append)
            assert len(calls) == 2 and slept == [bd.TRANSIENT_WAITS[0]], (first, slept)
            assert TOKEN not in out.getvalue() and "https://" not in out.getvalue()

        calls, slept = [], []

        def down(url, **kw):
            calls.append(url)
            raise ConnectionError(f"Max retries exceeded with url: {url}")

        with redirect_stdout(io.StringIO()) as out:
            assert bd.send_document(TOKEN, 42, path, "c", post=down, sleep=slept.append) is False
        assert len(calls) == bd.SEND_TRIES and slept == list(bd.TRANSIENT_WAITS)
        assert "still ConnectionError" in out.getvalue() and TOKEN not in out.getvalue()

        calls.clear()

        def bad(url, **kw):
            calls.append(url)
            return Resp(403, {"ok": False})

        with redirect_stdout(io.StringIO()):
            assert bd.send_document(TOKEN, 42, path, "c", post=bad, sleep=slept.append) is False
        assert len(calls) == 1, "a 4xx other than 429 is not retried"

        def typo(url, **kw):
            raise ValueError("not a network problem")

        with redirect_stdout(io.StringIO()):
            assert bd.send_document(TOKEN, 42, path, "c", post=typo, sleep=None) is False
    finally:
        os.remove(path)


def test_failure_reasons_are_shown_in_indonesian():
    """Review finding: the Kualitas data table printed the collector's English
    reasons. The page shows an Indonesian label; the raw text stays in runs.note."""
    cases = {"HTTP 503": "server menolak (HTTP 503)", "non-JSON body": "jawaban bukan JSON",
             "no cached payload": "tidak ada cache",
             "unreadable cache (EOFError)": "cache tidak terbaca · EOFError",
             "OHLC identical to BBRI (cross-ticker clone)": "OHLC sama persis dengan BBRI (klon)",
             "strict frame: StrictSourceError: x": "gagal validasi ketat · StrictSourceError: x",
             "no usable response": "tidak ada jawaban yang bisa dipakai",
             "PayloadError: T: bad": "payload tidak valid · T: bad",
             "something new": "something new"}
    for raw, shown in cases.items():
        assert bd.fmt_reason(raw) == shown, (raw, bd.fmt_reason(raw))
    ctx = fixture()
    ctx["run"]["failed"] = {"AAAA": "HTTP 502", "BBBB": "no cached payload"}
    html = bd.render(ctx)
    assert "server menolak (HTTP 502)" in html and "tidak ada cache" in html
    assert "no cached payload" not in html


def test_chart_axis_text_meets_contrast_on_a_phone():
    """Review finding: --muted text was 3.5:1 in light mode at ~10 px on a
    375 px phone. Axis text uses --ink2; --muted stays for strokes."""
    html = bd.render(fixture())
    assert ".ch text,.ci text{font-size:11px;fill:var(--ink2)}" in html
    assert "fill:var(--muted)}" not in html.split(".ch text,.ci text")[1].split("}")[0] + "}"


def test_buy_rules_show_net_of_cost_trade_stats():
    """§4.5's informational net trade stats (dir = +1 rules) reach the page,
    with the hit rate beside its base, and a rule without them shows nothing."""
    ctx = fixture()
    net = {"n_trades": 120, "mean_ret": 0.0123, "median_ret": -0.004, "hit_rate": 0.48,
           "ret_per_risk": 0.1, "base_rate": 0.45, "hit_edge": 0.03}
    for r in ctx["rules"]:
        if r["id"] == "R1":
            r["net"] = net
    html = bd.render(ctx)
    r1 = _rule_html(html, "R1")
    assert "Bersih biaya 0,73% pulang-pergi" in r1 and "n=120" in r1 and "+1,23%" in r1
    assert "48%" in r1 and "45%" in r1
    assert "Bersih biaya" not in _rule_html(html, "R4")


ALL = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    print(f"broker dashboard: {len(ALL)} tests\n")
    for fn in ALL:
        fn()
        print(f"  ok {fn.__name__}")
    print(f"\nAll {len(ALL)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
