"""Regression and external-anchor tests on the frozen, hash-verified snapshot.

Reference values were produced by the original executed project and
independently re-derived from the PDF formulas by a separate implementation
during the audit. Published anchors come from Goldman Sachs documents, not
from this code. No network access is used.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as timex

DATA = timex.FROZEN_DATA_DIR
END = "2026-10-08"


@pytest.fixture(scope="module")
def frozen():
    cfg = timex.Config()
    market, metadata = timex.load_inputs(DATA, END, cfg)
    events = timex.load_fomc(timex.FOMC_CALENDAR_PATH, END)
    signals = timex.generate_signals(market, events, cfg)
    feat = timex.controlled_weights(market, signals, cfg)
    return cfg, market, metadata, events, signals, feat


def test_frozen_inputs_match_manifest_hashes():
    manifest = json.loads((DATA / "download_manifest.json").read_text())
    for series, source in manifest["sources"].items():
        assert timex.sha256(DATA / source["file"]) == source["sha256"], series


def test_frozen_snapshot_shape_and_bounds(frozen):
    cfg, market, metadata, *_ = frozen
    assert len(market) == 3210
    assert (metadata["first_price"], metadata["last_price"]) == ("2014-01-02", "2026-10-07")
    assert metadata["dividend_events"] == 51 and metadata["split_events"] == 0
    assert metadata["DFF_last"] == metadata["DTB3_last"] == "2026-10-06"
    timex.validate_sample_bounds(market.index, "2014-01-01", END, cfg.calendar)


def test_reference_results_reproduce(frozen):
    cfg, market, _, events, _, feat = frozen
    anchor = market.index[-1] - pd.DateOffset(years=10)
    pos = int(market.index.searchsorted(anchor))
    anchor = market.index[pos]
    assert str(anchor.date()) == "2016-10-07" and pos == 697 and len(market) - pos - 1 == 2512
    ledger = timex.accounting(market.asset_er_return.loc[anchor:], feat.weight_close.loc[anchor:],
                              market.dcf.loc[anchor:], cfg)
    stats = timex.performance(ledger.net_level, market.cash_return, True)
    assert stats["Annualized Return"] == pytest.approx(0.06745943, abs=1e-8)
    assert stats["Annualized Volatility"] == pytest.approx(0.10041956, abs=1e-8)
    assert stats["Sharpe Ratio"] == pytest.approx(0.70254, abs=1e-6)
    assert stats["Maximum Drawdown"] == pytest.approx(-0.19323170, abs=1e-8)
    assert stats["Terminal Wealth per 100"] == pytest.approx(192.076831, abs=1e-6)
    held = feat.weight_held.loc[anchor:].iloc[1:]
    assert held.mean() == pytest.approx(0.677877, abs=1e-6)
    assert (held >= 1.5 - 1e-10).mean() == pytest.approx(116 / 2512)
    tr = timex.performance(timex.normalize_path(timex.wealth(market.adj_return), anchor), market.cash_return, False)
    assert tr["Terminal Wealth per 100"] == pytest.approx(423.222728, abs=1e-6)


def test_published_initialization_volatility_fingerprint(frozen):
    """The methodology prints the EWMA vols set on the 3 May 2021 Asset Base Date.

    They are a state of GS's own backtest, so they test the full ER -> five
    signals -> allocation -> algorithmic portfolio -> EWMA chain.
    """
    _, _, _, _, _, feat = frozen
    seed = pd.Timestamp("2021-05-03")
    assert round(100 * feat.loc[seed, "ewma_short"], 4) == 12.0433
    assert 100 * feat.loc[seed, "ewma_long"] == pytest.approx(14.6234, abs=0.005)


def test_dividend_forfeiture_contradicts_published_seed(frozen):
    cfg, market, _, events, _, _ = frozen
    close_only = timex.with_asset_return(market, market.price_return - market.cash_return)
    feat = timex.controlled_weights(close_only, timex.generate_signals(close_only, events, cfg), cfg)
    # Far outside the baseline's own residual (0.0027 vol points on the 0.97 EWMA).
    assert abs(100 * feat.loc["2021-05-03", "ewma_short"] - 12.0433) > 0.05


def test_anchor_fingerprint_flags_known_misreadings(frozen):
    cfg, market, _, events, _, _ = frozen
    anchors, fingerprint = timex.published_anchor_checks(market, events, cfg)
    assert len(anchors) == len(timex.PUBLISHED_ANCHORS)
    assert fingerprint.loc["Methodology baseline", "Verdict"] == "methodology"
    for convention in ("Dividends forfeited (Close-only ER)", "EWMA on unallocated SPY ER",
                       "Servicing on NAV, not notional"):
        assert fingerprint.loc[convention, "Verdict"] == "contradicted by published anchors", convention
    # Funding-proxy and dividend-convention variants are too close to call; the
    # check must not overclaim discrimination it does not have.
    for convention in ("DTB3 money-market funding", "Yahoo Adj Close total return in ER"):
        assert fingerprint.loc[convention, "Verdict"] == "not distinguishable at anchor precision", convention
    five_year = anchors.loc["5Y annualized return"]
    assert abs(five_year["Difference (pp)"]) < 0.10


def test_vendor_adjusted_close_is_prior_close_reinvestment(frozen):
    _, market, *_ = frozen
    anchor = pd.Timestamp("2016-10-07")
    table, summary = timex.dividend_convention_audit(market, anchor)
    assert len(table) == 51
    assert table["Vendor minus Identity (bp)"].abs().max() < 0.02
    assert summary["share_of_gap_explained_by_reinvestment_convention"] > 0.99
    assert summary["cash_dividend_tr_wealth"] == pytest.approx(423.5071, abs=1e-4)
    assert summary["max_abs_daily_difference_date"] == "2020-03-20"
    assert summary["max_abs_daily_cash_minus_vendor_bp"] == pytest.approx(2.5221, abs=1e-4)


def test_short_mr_cap_never_binds_in_sample(frozen):
    _, _, _, _, signals, _ = frozen
    assert signals.short_mr.abs().max() < timex.short_mr_bound() < 0.10


def test_fomc_lookahead_is_certified_for_the_snapshot(frozen):
    cfg, market, *_ = frozen
    coverage = json.loads(timex.FOMC_CALENDAR_PATH.with_name("fomc_coverage.json").read_text())
    result = timex.validate_fomc_coverage(coverage, market.index, cfg.calendar)
    assert result["lookahead_required_through"] == "2026-10-09"
    assert "2026-10-28" in result["certified_by"]


def test_fee_measures_reconcile_and_differ_by_denominator(frozen):
    cfg, market, _, _, _, feat = frozen
    anchor = pd.Timestamp("2016-10-07")
    m, f = market.loc[anchor:], feat.loc[anchor:]
    ledger = timex.accounting(m.asset_er_return, f.weight_close, m.dcf, cfg)
    nofee = timex.accounting(m.asset_er_return, f.weight_close, m.dcf,
                             timex.replace(cfg, deduction=0, servicing=0, transaction=0)).net_level
    shapley = timex.shapley_costs(market, feat, cfg, anchor)
    table = timex.fee_drag_measures(ledger, nofee, shapley, market.cash_return)
    nominal = table.loc["Sum of nominal daily charges"]
    compounded = table.loc["Compounded terminal wealth lost (Shapley)"]
    assert nominal.Total == pytest.approx(17.972116, abs=1e-6)
    assert compounded.Total == pytest.approx(23.246090, abs=1e-6)
    assert compounded.Total == pytest.approx(nofee.iloc[-1] - ledger.net_level.iloc[-1], abs=1e-9)
    assert compounded.Total > nominal.Total  # removed capital cannot compound
    assert table.loc["Relative terminal-wealth loss", "Total"] == pytest.approx(10.795920, abs=1e-6)
    assert table.loc["CAGR reduction", "Total"] == pytest.approx(1.226663, abs=1e-6)


def test_synthetic_gap_exposure_rises_before_stepwise_derisking(frozen):
    cfg, market, _, events, _, _ = frozen
    _, synthetic, paths = timex.stress_analysis(market, events, cfg, pd.Timestamp("2016-10-07"))
    gap = synthetic.loc["Overnight -20% gap"]
    assert gap["Exposure Before Shock"] == pytest.approx(1.313170, abs=1e-6)
    assert gap["First Shock Index Return"] == pytest.approx(-0.262824, abs=1e-6)
    # The shock-day close still uses pre-shock vol (VC at t-1): exposure rises.
    assert gap["Exposure After Shock Close"] > gap["Exposure Before Shock"]
    path = paths.loc[paths.Scenario == "Overnight -20% gap"].set_index("Session Relative To Shock")
    steps = path["Close Exposure"].diff().loc[1:]
    assert steps.min() >= -cfg.lookthrough_step - 1e-12
    # Held basis: 131% (shock day), 138% (+1), 88% (+2), 38% (+3).
    assert path.loc[2, "Held Exposure"] == pytest.approx(gap["Exposure After Shock Close"] - 0.5, abs=1e-12)
    assert gap["First Session With Held Exposure Below 50%"] == 3


def test_april_2025_is_shown_both_ways(frozen):
    cfg, market, _, events, _, _ = frozen
    historical, _, _ = timex.stress_analysis(market, events, cfg, pd.Timestamp("2016-10-07"))
    rebound = historical.loc["April 2025 rebound"]
    assert (rebound.Start, rebound.End) == ("2025-04-08", "2025-06-30")
    assert rebound["Index Return"] == pytest.approx(0.0533, abs=5e-5)
    assert rebound["SPY TR Return"] == pytest.approx(0.2481, abs=5e-5)


def test_regime_breakdown_and_excess_return_benchmark(frozen):
    """The blended CAGR hides regimes; the ER benchmark removes the cash rate."""
    cfg, market, _, _, _, feat = frozen
    anchor = pd.Timestamp("2016-10-07")
    m, f = market.loc[anchor:], feat.loc[anchor:]
    level = timex.accounting(m.asset_er_return, f.weight_close, m.dcf, cfg).net_level
    benchmark = timex.normalize_path(timex.wealth(market.adj_return - market.cash_return), anchor)
    stats = timex.performance(benchmark, market.cash_return, True)
    assert stats["Annualized Return"] == pytest.approx(0.126967, abs=1e-6)
    assert stats["Terminal Wealth per 100"] == pytest.approx(330.4009, abs=1e-4)
    levels = pd.DataFrame({timex.INDEX_NAME: level,
                           timex.TOTAL_NAME: timex.normalize_path(timex.wealth(market.adj_return), anchor),
                           timex.ER_BENCHMARK_NAME: benchmark})
    periods = timex.period_breakdown(levels, feat.weight_held.loc[anchor:], cfg)
    assert list(periods.index) == ["Before index base date", "Index base date to launch", "Since launch"]
    recon = periods[f"{timex.INDEX_NAME} CAGR"]
    assert recon.round(4).tolist() == [0.1289, -0.0011, 0.0290]
    assert periods[f"{timex.TOTAL_NAME} CAGR"].round(4).tolist() == [0.1759, 0.0566, 0.1957]
    # Regime boundaries are shared closes, so the three legs compound to the whole.
    total = np.prod([(1 + c) ** ((pd.Timestamp(r.End) - pd.Timestamp(r.Start)).days / 365.25)
                     for c, (_, r) in zip(recon, periods.iterrows())])
    assert total == pytest.approx(level.iloc[-1] / level.iloc[0], rel=1e-12)
