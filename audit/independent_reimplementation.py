#!/usr/bin/env python3
"""Second, independent implementation of the GSEQTMX rules for cross-checking.

Written directly from the July 28, 2023 methodology PDF during the audit,
deliberately WITHOUT importing run.py: its own calendar logic (exchange
sessions grouped by hand), its own literal translation of each section, plain
Python floats and explicit loops. Agreement with run.py is therefore evidence
that both implement the document, not that one copies the other.

Usage:  python audit/independent_reimplementation.py [data_dir] [ledger_csv]
Compares against results/daily_audit_ledger.csv and prints max abs differences.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
END = "2026-10-08"
ANCHOR = pd.Timestamp("2016-10-07")


def reimplement(data_dir: Path = ROOT / "data", end: str = END, anchor: pd.Timestamp = ANCHOR):
    spy = pd.read_csv(data_dir / "spy.csv", parse_dates=["Date"]).set_index("Date")
    spy = spy[spy.index < end]
    dff = pd.read_csv(data_dir / "dff.csv", parse_dates=["Date"]).set_index("Date")["DFF"].dropna()
    fomc = pd.read_csv(data_dir / "fomc_calendar.csv", parse_dates=["date"])
    scheduled = set(fomc.loc[fomc.event_type == "scheduled", "date"])

    cal = xcals.get_calendar("XNYS", start="2013-11-01", end="2026-12-31")
    sessions = [pd.Timestamp(s).tz_localize(None) if pd.Timestamp(s).tz else pd.Timestamp(s)
                for s in cal.sessions_in_range("2013-11-01", "2026-12-31")]
    pos = {d: i for i, d in enumerate(sessions)}
    dates = list(spy.index)
    if dates != [d for d in sessions if dates[0] <= d <= dates[-1]]:
        raise ValueError("price dates are not the complete XNYS session list")
    index_of = {d: i for i, d in enumerate(dates)}

    # Section 11: A_t = A_{t-1} * ((I_t + D_t) / I_{t-1} - Interest_Rate_{t-1} * DCF_{t-1,t}).
    def rate_asof(day):
        known = dff.loc[:day]
        if (day - known.index[-1]).days > 7:
            raise ValueError("stale rate")
        return known.iloc[-1] / 100.0

    A = {dates[0]: 100.0}
    for k in range(1, len(dates)):
        t, p = dates[k], dates[k - 1]
        A[t] = A[p] * ((spy.Close[t] + spy.Dividends[t]) / spy.Close[p] - rate_asof(p) * (t - p).days / 360.0)

    def month_sessions(y, m):
        return [d for d in sessions if d.year == y and d.month == m]

    def previous_month(y, m):
        return (y - 1, 12) if m == 1 else (y, m - 1)

    def option_expiry(y, m):
        first = date(y, m, 1)
        friday = pd.Timestamp(first + timedelta(days=(4 - first.weekday()) % 7 + 14))
        return max(d for d in sessions if d <= friday)  # third Friday or preceding session

    def signals(t):
        y, m = t.year, t.month
        this_month = month_sessions(y, m)
        eom, bom = this_month[-1], this_month[0]
        py, pm = previous_month(y, m)
        previous_eom = month_sessions(py, pm)[-1]
        # 10.3.1.1: EOM-4 <= t <= EOM-1 and A[EOM-5] < A[previous EOM].
        w1 = 0.0
        if pos[eom] - 4 <= pos[t] <= pos[eom] - 1:
            reference = sessions[pos[eom] - 5]
            if reference in A and previous_eom in A and A[reference] < A[previous_eom]:
                w1 = 1.0
        # 10.3.1.2: t == EOM, or BOM <= t <= BOM+2.
        w2 = 1.0 if (t == eom or pos[bom] <= pos[t] <= pos[bom] + 2) else 0.0
        # 10.3.2: OE-3 <= t <= OE-1, sign of A[OE-4] - A[previous OE].
        oe, previous_oe = option_expiry(y, m), option_expiry(py, pm)
        w3 = 0.0
        if pos[oe] - 3 <= pos[t] <= pos[oe] - 1:
            reference = sessions[pos[oe] - 4]
            if reference in A and previous_oe in A:
                w3 = 1.0 if A[reference] > A[previous_oe] else (-1.0 if A[reference] < A[previous_oe] else 0.0)
        # 10.3.3: second or first session before a scheduled announcement.
        w4 = 1.0 if (sessions[pos[t] + 1] in scheduled or sessions[pos[t] + 2] in scheduled) else 0.0
        # 10.3.4: weighted 5-day return, 10-day RMS log-return scalar, +/-10% clip.
        k = index_of[t]
        w5 = 0.0
        if k >= 10:
            weighted = sum((6 - s) * (A[dates[k - s + 1]] / A[dates[k - s]] - 1) for s in range(1, 6)) / 15.0
            mean_square = sum(math.log(A[dates[k - i + 1]] / A[dates[k - i]]) ** 2 for i in range(1, 11)) / 10
            if mean_square > 0:
                w5 = min(0.10, max(-0.10, -5 * 0.15 / math.sqrt(252 * mean_square) * weighted))
        return w1, w2, w3, w4, w5

    leverages = (0.75, 0.75, 0.75, 0.75, 1.50)
    rows, alloc_prev, weight_prev = [], None, None
    av, v94, v97 = 100.0, 0.120433, 0.146234
    for k, t in enumerate(dates):
        w = signals(t)
        target_alloc = 0.75 + sum(l * x for l, x in zip(leverages, w))  # 10.2
        vol_prev = max(v94, v97)                                         # AlgoVol^final_{t-1}
        if k == 0:
            alloc = min(1.5, max(0.0, target_alloc))
        else:
            av_return = alloc_prev * (A[t] / A[dates[k - 1]] - 1)           # section 9 uses w_{t-1}
            av *= 1 + av_return
            v94 = math.sqrt(0.94 * v94 ** 2 + 0.06 * 252 * math.log(1 + av_return) ** 2)   # section 8
            v97 = math.sqrt(0.97 * v97 ** 2 + 0.03 * 252 * math.log(1 + av_return) ** 2)
            alloc = min(1.5, max(0.0, alloc_prev + min(0.75, max(-0.75, target_alloc - alloc_prev))))  # 10.1
        lookthrough_target = 0.10 / vol_prev * alloc                     # 7.1: VC_{t-1} * w_t
        weight = (min(1.5, lookthrough_target) if k == 0 else            # 6.1-6.2
                  min(1.5, weight_prev + min(0.5, max(-0.5, lookthrough_target - weight_prev))))
        rows.append(dict(date=t, tom_mr=w[0], tom_long=w[1], opex=w[2], fomc=w[3], short_mr=w[4],
                         raw_allocation_target=target_alloc, allocation=alloc, ewma_short=v94, ewma_long=v97,
                         lookthrough_target=lookthrough_target, weight_close=weight, asset_er=A[t]))
        alloc_prev, weight_prev = alloc, weight
    policy = pd.DataFrame(rows).set_index("date")

    # Sections 2-6 from the comparison anchor (index base convention: N=G=100, zero costs).
    sub = policy.loc[anchor:]
    net, qty = 100.0, 100.0 * sub.weight_close.iloc[0] / sub.asset_er.iloc[0]
    ledger, previous = [dict(date=sub.index[0], net=net, notional=qty * sub.asset_er.iloc[0],
                             rc=0.0, sc=0.0, dc=0.0)], sub.index[0]
    for t in sub.index[1:]:
        a_t, a_p = sub.asset_er[t], sub.asset_er[previous]
        dcf = (t - previous).days / 360.0
        gross = net + qty * (a_t - a_p)
        new_qty = gross * sub.weight_close[t] / a_t
        rc = abs(new_qty - qty) * a_t * 0.0001
        sc = qty * a_p * dcf * 0.005
        dc = net * dcf * 0.005
        net = max(0.0, gross - rc - sc - dc)
        qty = new_qty
        ledger.append(dict(date=t, net=net, notional=qty * a_t, rc=rc, sc=sc, dc=dc))
        previous = t
    return policy, pd.DataFrame(ledger).set_index("date")


def compare(policy: pd.DataFrame, ledger: pd.DataFrame, audit_ledger: Path) -> dict:
    """Max absolute differences against run.py's exported daily audit ledger."""
    shipped = pd.read_csv(audit_ledger, parse_dates=["date"]).set_index("date")
    out = {column: float((policy[column] - shipped[column]).abs().max()) for column in policy.columns}
    window = shipped.loc[ledger.index]
    pairs = {"net": window.ledger_net_level, "notional": window.ledger_quantity * window.ledger_asset_level,
             "rc": window.ledger_transaction_cost, "sc": window.ledger_servicing_cost, "dc": window.ledger_index_deduction}
    out.update({f"ledger_{k}": float((ledger[k] - v).abs().max()) for k, v in pairs.items()})
    return out


if __name__ == "__main__":
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"
    audit_ledger = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "results" / "daily_audit_ledger.csv"
    policy, ledger = reimplement(data_dir)
    differences = compare(policy, ledger, audit_ledger)
    print(json.dumps(differences, indent=1))
    print("terminal net level per 100:", round(ledger.net.iloc[-1], 6))
    print("EWMA on 2021-05-03 (published 12.0433 / 14.6234):",
          round(100 * policy.loc["2021-05-03", "ewma_short"], 4), round(100 * policy.loc["2021-05-03", "ewma_long"], 4))
    # The ledger CSV is written with %.12g, so ~1e-9 absolute differences at
    # level ~100-400 are export precision; anything larger is a real mismatch.
    worst = max(differences.values())
    if worst > 1e-8:
        sys.exit(f"MISMATCH: max abs difference {worst:.3e} exceeds 1e-8")
    print(f"OK: independent implementation agrees (max abs difference {worst:.1e})")
