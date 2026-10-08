"""Offline source-derived tests of the July 2023 TimeX methodology.

Expected values below are derived from the PDF formulas and calendar anchors,
not from a second implementation of run.py. No network data are required.
"""
from dataclasses import replace
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as timex


@pytest.fixture
def cfg():
    return timex.Config()


def empty_events():
    return pd.DataFrame({"date": pd.Series([], dtype="datetime64[ns]"),
                         "event_type": pd.Series([], dtype=str),
                         "source_url": pd.Series([], dtype=str)})


def market_frame(dates, returns=0.001):
    dates = pd.DatetimeIndex(dates)
    values = np.full(len(dates), returns, dtype=float) if np.isscalar(returns) else np.array(returns, dtype=float)
    values[0] = 0.0
    levels = 100.0 * np.cumprod(1.0 + values)
    return pd.DataFrame({"Close": levels, "asset_er": levels, "asset_er_return": values,
                         "dcf": dates.to_series().diff().dt.days.fillna(0).to_numpy()/360}, index=dates)


def raw_signals(dates, targets):
    return pd.DataFrame({"raw_allocation_target": targets}, index=dates)


def test_turn_of_month_decision_and_held_windows(cfg):
    dates = timex.exchange_sessions("2025-02-03", "2025-04-30")
    # A negative month-to-date move activates the ToM mean reversion rule.
    market = market_frame(dates, -0.001)
    s = timex.generate_signals(market, empty_events(), cfg)
    march = dates[dates.to_period("M") == pd.Period("2025-03")]
    expected_mr_decisions = pd.to_datetime(["2025-03-25", "2025-03-26", "2025-03-27", "2025-03-28"])
    assert list(s.loc[march].index[s.loc[march, "tom_mr"].eq(1)]) == list(expected_mr_decisions)
    expected_long_decisions = pd.to_datetime(["2025-03-03", "2025-03-04", "2025-03-05", "2025-03-31"])
    assert list(s.loc[march].index[s.loc[march, "tom_long"].eq(1)]) == list(expected_long_decisions)
    # Decisions at close t earn the next session's return: last FOUR, first FOUR.
    assert list(s.loc[march].index[s.tom_mr.shift(1).reindex(march).eq(1)]) == list(pd.to_datetime([
        "2025-03-26", "2025-03-27", "2025-03-28", "2025-03-31"]))
    assert list(s.loc[march].index[s.tom_long.shift(1).reindex(march).eq(1)]) == list(pd.to_datetime([
        "2025-03-03", "2025-03-04", "2025-03-05", "2025-03-06"]))
    # The user's final-two/first-three flag is kept as a separate diagnostic.
    assert list(s.loc[march].index[s.loc[march, "tom_requested"]]) == list(march[:3].union(march[-2:]))


def test_turn_of_month_mr_uses_previous_month_end_and_known_cutoff(cfg):
    dates = timex.exchange_sessions("2025-02-03", "2025-03-31")
    market = market_frame(dates, 0.002)
    s = timex.generate_signals(market, empty_events(), cfg)
    assert s.tom_mr.sum() == 0
    # Change a future close after March's EOM-5 cutoff. It cannot alter the flag.
    later = market.copy()
    later.loc["2025-03-28":, "asset_er"] *= 0.5
    altered = timex.generate_signals(later, empty_events(), cfg)
    pd.testing.assert_series_equal(s.tom_mr, altered.tom_mr)


def test_partial_month_does_not_become_false_month_end(cfg):
    dates = timex.exchange_sessions("2025-01-02", "2025-03-12")
    s = timex.generate_signals(market_frame(dates), empty_events(), cfg)
    assert not s.loc["2025-03-12", "tom_requested"]
    assert s.loc["2025-03-12", "tom_long"] == 0


def test_good_friday_expiry_adjustment_and_momentum_windows(cfg):
    dates = timex.exchange_sessions("2025-02-03", "2025-04-30")
    positive = timex.generate_signals(market_frame(dates, 0.001), empty_events(), cfg)
    # Third Friday April 18 is Good Friday; monthly expiry is April 17.
    assert timex.third_friday(pd.Period("2025-04")) == pd.Timestamp("2025-04-18")
    assert positive.loc["2025-04-17", "opex_date"]
    expected = pd.to_datetime(["2025-04-14", "2025-04-15", "2025-04-16"])
    april = positive.loc["2025-04"]
    assert list(april.index[april.opex.eq(1)]) == list(expected)
    assert list(april.index[positive.opex.shift(1).reindex(april.index).eq(1)]) == list(pd.to_datetime([
        "2025-04-15", "2025-04-16", "2025-04-17"]))
    negative = timex.generate_signals(market_frame(dates, -0.001), empty_events(), cfg)
    assert list(negative.loc["2025-04"].index[negative.loc["2025-04", "opex"].eq(-1)]) == list(expected)


def test_fomc_preannouncement_decisions_and_emergency_exclusion(cfg):
    dates = timex.exchange_sessions("2025-01-02", "2025-03-31")
    events = pd.DataFrame({"date": pd.to_datetime(["2025-03-19", "2025-03-12"]),
                           "event_type": ["scheduled", "emergency"], "source_url": ["https://federalreserve.gov"]*2})
    s = timex.generate_signals(market_frame(dates), events, cfg)
    assert list(s.index[s.fomc.eq(1)]) == list(pd.to_datetime(["2025-03-17", "2025-03-18"]))
    assert list(s.index[s.fomc.shift(1).eq(1)]) == list(pd.to_datetime(["2025-03-18", "2025-03-19"]))
    assert s.loc["2025-03-19", "fomc_announcement"]
    assert not s.loc["2025-03-12", "fomc_announcement"]


def test_scheduled_fomc_beyond_price_cutoff_still_has_prior_decisions(tmp_path, cfg):
    path = tmp_path / "fomc.csv"
    pd.DataFrame({"date": ["2025-03-19"], "event_type": ["scheduled"],
                  "source_url": ["https://federalreserve.gov"]}).to_csv(path, index=False)
    # With prices through March 18 (exclusive end March 19), the known March 19
    # meeting still needs decision flags on March 17 and March 18.
    events = timex.load_fomc(path, "2025-03-19")
    dates = timex.exchange_sessions("2025-01-02", "2025-03-18")
    s = timex.generate_signals(market_frame(dates), events, cfg)
    assert list(s.index[s.fomc.eq(1)]) == list(pd.to_datetime(["2025-03-17", "2025-03-18"]))


def test_short_term_mean_reversion_exact_weighting_rms_and_clip(cfg):
    dates = timex.exchange_sessions("2025-01-02", "2025-03-31")
    returns = np.zeros(len(dates))
    sample = np.array([.001, -.002, .003, -.004, .002, -.001, .001, -.002, .003, -.001])
    returns[1:11] = sample
    market = market_frame(dates, returns)
    s = timex.generate_signals(market, empty_events(), cfg)
    sigma = np.sqrt(252 * np.mean(np.log1p(sample)**2))
    recent = np.dot(sample[-5:][::-1], [5, 4, 3, 2, 1]) / 15
    expected = np.clip(-5 * (0.15/sigma) * recent, -.10, .10)
    assert s.short_mr.iloc[10] == pytest.approx(expected, abs=1e-14)
    assert s.mr_vol_scalar.iloc[10] == pytest.approx(.15/sigma)
    # A concentrated, exceptionally large positive-return block exercises the
    # negative 10% cap; ordinary constant returns scale down via RMS and do not.
    extreme_returns = np.zeros(len(dates))
    extreme_returns[6:11] = 1.2
    extreme = timex.generate_signals(market_frame(dates, extreme_returns), empty_events(), cfg)
    assert extreme.short_mr.iloc[10] == pytest.approx(-.10)
    downward = timex.generate_signals(market_frame(dates, -.10), empty_events(), cfg)
    assert 0 < downward.short_mr.iloc[20] <= .10
    quiet = timex.generate_signals(market_frame(dates, 0.0), empty_events(), cfg)
    assert quiet.short_mr.eq(0).all()  # Explicit zero-RMS engineering convention.


def test_volatility_is_max_ewma_of_algorithmic_portfolio_with_lag(cfg):
    dates = pd.date_range("2025-01-06", periods=5, freq="B")
    market = market_frame(dates, [0., .02, -.01, .03, -.02])
    s = raw_signals(dates, [.75]*5)
    f = timex.controlled_weights(market, s, cfg)
    # First observed 2% ER gain at 75% allocation produces a 1.5% AV gain.
    av_return = .75*.02
    log_square = np.log1p(av_return)**2
    v_short = np.sqrt(.94*cfg.seed_short**2 + .06*252*log_square)
    v_long = np.sqrt(.97*cfg.seed_long**2 + .03*252*log_square)
    assert f.algorithmic_value.iloc[1] == pytest.approx(101.5)
    assert f.ewma_short.iloc[1] == pytest.approx(v_short)
    assert f.ewma_long.iloc[1] == pytest.approx(v_long)
    assert f.algorithmic_vol.iloc[1] == pytest.approx(max(v_short, v_long))
    # t=1 decision uses the INITIAL vol, not the return just observed at t=1.
    assert f.lookthrough_target.iloc[1] == pytest.approx(.75*.10/cfg.seed_long)
    assert f.lookthrough_target.iloc[2] == pytest.approx(.75*.10/max(v_short, v_long))
    pd.testing.assert_series_equal(f.weight_held, f.weight_close.shift(1).rename("weight_held"))
    shocked = market.copy()
    shocked.loc[dates[1], "asset_er_return"] = -.30
    shifted = timex.controlled_weights(shocked, s, cfg)
    assert shifted.weight_close.iloc[1] == pytest.approx(f.weight_close.iloc[1])
    assert shifted.weight_held.iloc[1] == pytest.approx(f.weight_held.iloc[1])
    assert shifted.weight_close.iloc[2] < f.weight_close.iloc[2]


def test_two_stage_caps_and_zero_floor(cfg):
    dates = pd.date_range("2025-01-06", periods=6, freq="B")
    market = market_frame(dates, 0.0)
    low_vol = replace(cfg, seed_short=.01, seed_long=.01)
    s = raw_signals(dates, [.75, 10., -10., -10., 10., 10.])
    f = timex.controlled_weights(market, s, low_vol)
    assert f.allocation.to_list() == pytest.approx([.75, 1.5, .75, 0., .75, 1.5])
    assert f.weight_close.to_list() == pytest.approx([1.5, 1.5, 1.5, 1., 1.5, 1.5])
    assert f.allocation.diff().abs().dropna().max() <= .75+1e-12
    assert f.weight_close.diff().abs().dropna().max() <= .50+1e-12
    assert f.weight_close.between(0, 1.50).all()


def test_official_seed_dates_reset_vol_and_initial_index_cap(cfg):
    dates = timex.exchange_sessions("2021-04-01", "2021-06-02")
    market = market_frame(dates, .01)
    # Raw allocation exceeds 1.5 only on the asset base date; §10.1's literal
    # base-date rule uses target directly, and later dates apply allocation caps.
    targets = np.full(len(dates), .75)
    targets[dates.get_loc("2021-05-03")] = 1.6
    s = raw_signals(dates, targets)
    f = timex.controlled_weights(market, s, cfg, official_seeds=True)
    assert f.loc["2021-05-03", "algorithmic_value"] == 100
    assert f.loc["2021-05-03", "ewma_short"] == cfg.seed_short
    assert f.loc["2021-05-03", "ewma_long"] == cfg.seed_long
    assert f.loc["2021-05-03", "allocation"] == 1.6
    assert f.loc["2021-05-04", "allocation"] <= 1.50
    assert f.loc["2021-06-01", "weight_close"] == pytest.approx(
        min(1.50, f.loc["2021-06-01", "lookthrough_target"]))


def test_exact_quantity_cost_accounting_and_act360_weekend(cfg):
    dates = pd.to_datetime(["2025-01-03", "2025-01-06", "2025-01-07"])
    returns = pd.Series([0., .10, 0.], index=dates)
    weights = pd.Series([.5, .5, .5], index=dates)
    dcf = pd.Series([0., 3/360, 1/360], index=dates)
    ledger = timex.accounting(returns, weights, dcf, cfg)
    assert ledger.quantity.iloc[0] == .5
    # A half-invested portfolio gains five on a 10% underlying gain.
    assert ledger.asset_level.iloc[1] == pytest.approx(110)
    assert ledger.gross_level.iloc[1] == pytest.approx(105)
    assert ledger.quantity.iloc[1] == pytest.approx(105*.5/110)
    # Fixed weight does NOT imply zero turnover: drift reduces quantity.
    assert ledger.turnover_notional.iloc[1] == pytest.approx(2.5)
    assert ledger.transaction_cost.iloc[1] == pytest.approx(.00025)
    assert ledger.servicing_cost.iloc[1] == pytest.approx(50*(3/360)*.005)
    assert ledger.index_deduction.iloc[1] == pytest.approx(100*(3/360)*.005)
    expected_net = 105-.00025-50*(3/360)*.005-100*(3/360)*.005
    assert ledger.net_level.iloc[1] == pytest.approx(expected_net)
    # The next day has no asset or target-weight change. Prior charges still
    # reduce gross NAV and Q, and hence create a small positive transaction cost.
    assert ledger.transaction_cost.iloc[2] > 0
    assert ledger.quantity.iloc[2] == pytest.approx(expected_net*.5/110)
    assert ledger.actual_nav_exposure.iloc[2] == pytest.approx(52.5/expected_net)
    assert ledger.actual_nav_exposure.iloc[2] > .5
    assert ledger.explicit_cost.iloc[0] == 0


def test_zero_nav_is_absorbing_after_leveraged_gap(cfg):
    dates = pd.date_range("2025-01-06", periods=4, freq="B")
    returns = pd.Series([0., -.80, 5., .10], index=dates)
    weights = pd.Series(1.5, index=dates)
    dcf = pd.Series([0., 1/360, 1/360, 1/360], index=dates)
    ledger = timex.accounting(returns, weights, dcf, cfg)
    assert ledger.net_level.to_list() == [100., 0., 0., 0.]
    assert ledger.quantity.iloc[1:].eq(0).all()
    assert ledger["return"].iloc[1] == -1.0
    assert ledger["return"].iloc[2:].eq(0).all()


def test_shapley_and_direct_cost_estimands_reconcile_at_common_anchor(cfg):
    dates = pd.date_range("2025-01-06", periods=10, freq="B")
    market = market_frame(dates, [0., .01, -.005, .02, -.01, .01, -.02, .015, .003, -.001])
    market["price_return"] = market.asset_er_return
    market["adj_return"] = market.asset_er_return
    market["cash_total_return"] = market.asset_er_return
    market["cash_return"] = 0.0
    f = pd.DataFrame({"weight_close": [.5,.8,1.1,.7,.2,.4,.9,1.2,.8,.5],
                      "allocation": [.75]*10}, index=dates)
    anchor = dates[3]
    attribution, curves, direct = timex.run_counterfactuals(market, f, cfg, anchor)
    assert curves.iloc[0].eq(100).all()
    assert direct["attribution_reconciliation_error"] == pytest.approx(0, abs=1e-12)
    shapley = timex.shapley_costs(market, f, cfg, anchor)
    assert shapley.iloc[:, 0].sum() == pytest.approx(direct["explicit_fee_terminal_loss_per_100"], abs=1e-10)
    assert direct["explicit_fee_relative_wealth_loss"] == pytest.approx(
        1-curves["Index deduction / net reconstruction"].iloc[-1]/curves["Fed Funds excess return"].iloc[-1])
