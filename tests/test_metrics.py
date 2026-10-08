"""Metric and optional published-level reconciliation tests with known values."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as timex


def test_sharpe_subtracts_cash_once_for_spy_and_uses_zero_for_er():
    dates = pd.date_range("2025-01-02", periods=5, freq="B")
    returns = np.array([.03, .01, -.02, .015])
    cash_values = np.array([.001, .003, .002, .001])
    level = pd.Series(np.r_[100., 100*np.cumprod(1+returns)], index=dates)
    cash = pd.Series(np.r_[0., cash_values], index=dates)
    raw = timex.performance(level, cash, already_excess=False)
    er = timex.performance(level, cash, already_excess=True)
    expected_raw = np.mean(returns-cash_values)/np.std(returns-cash_values, ddof=1)*np.sqrt(252)
    expected_er = np.mean(returns)/np.std(returns, ddof=1)*np.sqrt(252)
    assert raw["Sharpe Ratio"] == pytest.approx(expected_raw)
    assert er["Sharpe Ratio"] == pytest.approx(expected_er)
    # An ER comparator is zero regardless of the supplied financing cash path.
    assert timex.performance(level, cash*50, True)["Sharpe Ratio"] == pytest.approx(expected_er)
    # Reported realized volatility is always the raw path's return volatility.
    assert raw["Annualized Volatility"] == pytest.approx(er["Annualized Volatility"])


def test_cagr_uses_elapsed_calendar_years_while_volatility_uses_252():
    dates = pd.to_datetime(["2022-01-03", "2022-07-05", "2023-01-03", "2023-07-03"])
    level = pd.Series([100., 125., 90., 110.], index=dates)
    result = timex.performance(level, pd.Series(0., index=dates), True)
    years = (dates[-1]-dates[0]).days/365.25
    expected_returns = np.array([.25, 90/125-1, 110/90-1])
    assert result["Annualized Return"] == pytest.approx(1.10**(1/years)-1)
    assert result["Annualized Volatility"] == pytest.approx(np.std(expected_returns, ddof=1)*np.sqrt(252))
    assert result["Maximum Drawdown"] == pytest.approx(-.28)
    assert result["Terminal Wealth per 100"] == 110
    # Counting the three observations as 3/252 years would be a different CAGR.
    assert not np.isclose(result["Annualized Return"], 1.1**(252/3)-1)


def test_drawdown_is_measured_from_comparison_anchor_not_old_high():
    dates = pd.date_range("2025-01-02", periods=4, freq="B")
    full = pd.Series([150., 100., 80., 120.], index=dates)
    anchored = timex.normalize_path(full, dates[1])
    result = timex.performance(anchored, pd.Series(0., index=dates), True)
    assert anchored.iloc[0] == 100
    assert anchored.iloc[-1] == 120
    assert result["Maximum Drawdown"] == pytest.approx(-.20)
    assert (full/full.cummax()-1).min() < result["Maximum Drawdown"]


def test_zero_volatility_sharpe_is_undefined_and_exhausted_path_cannot_rebase():
    dates = pd.date_range("2025-01-02", periods=4, freq="B")
    level = pd.Series(100., index=dates)
    result = timex.performance(level, pd.Series(0., index=dates), True)
    assert np.isnan(result["Sharpe Ratio"])
    assert result["Annualized Volatility"] == 0
    assert result["Maximum Drawdown"] == 0
    with pytest.raises(ValueError, match="exhausted"):
        timex.normalize_path(pd.Series([100., 0., 0., 0.], index=dates), dates[1])


def test_simplified_model_has_exactly_one_volatility_to_held_return_lag():
    dates = pd.date_range("2025-01-06", periods=5, freq="B")
    market = pd.DataFrame({"dcf": [0., 1/360, 1/360, 1/360, 1/360],
                           "price_return": [0., .01, -.02, .03, .01],
                           "tbill_cash_return": 0.0}, index=dates)
    feat = pd.DataFrame({"spy_vol20": [.10, .20, .40, .10, .05],
                         "spy_vol60": [.08, .15, .30, .08, .04]}, index=dates)
    result = timex.simplified_model(market, feat, timex.Config(), costs=False)
    assert result.weight_close.to_list() == pytest.approx([1., .5, .25, 1., 1.5])
    assert result.weight_held.to_list() == pytest.approx([0., 1., .5, .25, 1.])
    assert result["return"].to_list() == pytest.approx([0., .01, -.01, .0075, .01])
    # A volatility observation at t cannot affect the already-held exposure at t.
    changed = feat.copy()
    changed.loc[dates[2], "spy_vol20"] = .80
    changed_result = timex.simplified_model(market, changed, timex.Config(), costs=False)
    assert changed_result["return"].iloc[2] == result["return"].iloc[2]
    assert changed_result["return"].iloc[3] == pytest.approx(.03*.125)


def test_official_reconciliation_rebases_and_reports_known_daily_residuals(tmp_path):
    dates = pd.date_range("2023-06-26", periods=50, freq="B")
    official_returns = np.tile([.004, -.002, .001, .003, -.001], 10)[1:]
    residual = np.tile([.0002, -.0001, .0000, -.0002, .0001], 10)[1:]
    research_returns = official_returns+residual
    research = pd.Series(np.r_[200., 200*np.cumprod(1+research_returns)], index=dates)
    official = pd.Series(np.r_[500., 500*np.cumprod(1+official_returns)], index=dates)
    path = tmp_path/"official.csv"
    pd.DataFrame({"date": dates, "level": official.values}).to_csv(path, index=False)
    result = timex.audit_official(research, path, "2023-07-28", tmp_path)
    assert result["status"] == "compared, not certified"
    assert result["common_dates"] == 50
    assert result["mean_daily_return_bias"] == pytest.approx(np.mean(residual), abs=1e-14)
    assert result["annualized_tracking_error"] == pytest.approx(np.std(residual, ddof=1)*np.sqrt(252))
    expected_difference = 100*research/research.iloc[0]-100*official/official.iloc[0]
    assert result["max_absolute_rebased_level_error"] == pytest.approx(expected_difference.abs().max())
    assert "pre_launch_tracking_error" in result and "live_tracking_error" in result
    stored = pd.read_csv(tmp_path/"official_reconciliation.csv", index_col="date")
    assert stored.iloc[0].Research == 100
    assert stored.iloc[0].Official == 100
    assert stored.iloc[0]["Level Difference"] == 0
    assert result["official_file_sha256"] == timex.sha256(path)


@pytest.mark.parametrize("bad_level", [0., -1., np.nan, np.inf, -np.inf, "invalid"])
def test_official_reconciliation_rejects_nonpositive_missing_or_nonfinite_levels(tmp_path, bad_level):
    dates = pd.date_range("2023-01-02", periods=25, freq="B")
    research = pd.Series(np.linspace(100, 120, len(dates)), index=dates)
    levels = research.astype(object).to_numpy().copy()
    levels[5] = bad_level
    path = tmp_path/"bad_official.csv"
    pd.DataFrame({"date": dates, "level": levels}).to_csv(path, index=False)
    with pytest.raises(ValueError, match="finite|positive|missing"):
        timex.audit_official(research, path, "2023-07-28", tmp_path)


def test_official_reconciliation_rejects_duplicate_dates(tmp_path):
    dates = pd.date_range("2023-01-02", periods=25, freq="B")
    research = pd.Series(np.linspace(100, 120, len(dates)), index=dates)
    frame = pd.DataFrame({"date": dates, "level": research.values})
    frame.loc[5, "date"] = dates[4]
    path = tmp_path/"duplicate.csv"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="unique"):
        timex.audit_official(research, path, "2023-07-28", tmp_path)


def test_official_reconciliation_requires_sufficient_common_observations(tmp_path):
    dates = pd.date_range("2023-01-02", periods=25, freq="B")
    research = pd.Series(np.linspace(100, 120, len(dates)), index=dates)
    path = tmp_path/"short_official.csv"
    pd.DataFrame({"date": dates[:19], "level": research.iloc[:19].values}).to_csv(path, index=False)
    with pytest.raises(ValueError, match="20 common dates"):
        timex.audit_official(research, path, "2023-07-28", tmp_path)


def test_official_reconciliation_rejects_missing_observation_date(tmp_path):
    dates = pd.date_range("2023-01-02", periods=25, freq="B")
    research = pd.Series(np.linspace(100, 120, len(dates)), index=dates)
    frame = pd.DataFrame({"date": dates, "level": research.values})
    frame.loc[5, "date"] = pd.NaT
    path = tmp_path/"missing_date.csv"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="date|unique|missing"):
        timex.audit_official(research, path, "2023-07-28", tmp_path)
