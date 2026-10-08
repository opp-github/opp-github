"""Independent input and financing QA, using controlled economic fixtures.

These tests never contact a provider and never compare a function to a copy of
its implementation. Fixtures encode exchange sessions, a known cash dividend,
and a dated cash-rate jump so that cashflow and information-time expectations
can be stated independently of the production pipeline.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as timex


@pytest.fixture
def controlled_inputs(tmp_path):
    """360 authentic NYSE calendar sessions, one explicit $2 distribution.

    The price process loses $2 on the distribution date. The cash-dividend total
    return benchmark explicitly reinvests that payment at the day's close; the
    adjusted-close fixture represents that same economic total return process.
    """
    calendar = xcals.get_calendar("XNYS", start="2023-01-01", end="2025-01-01")
    sessions = pd.DatetimeIndex(calendar.sessions_in_range("2023-01-03", "2024-12-31"))[:360]
    if sessions.tz is not None:
        sessions = sessions.tz_localize(None)
    sessions.name = "Date"
    close = pd.Series(100 * 1.0002 ** np.arange(len(sessions)), index=sessions)
    close.iloc[100:] -= 2.0
    dividends = pd.Series(0.0, index=sessions)
    dividends.iloc[100] = 2.0
    total_return = close.pct_change(fill_method=None) + dividends / close.shift(1)
    adjusted = 100 * (1 + total_return.fillna(0)).cumprod()
    prices = pd.DataFrame({"Close": close, "Adj Close": adjusted,
                           "Dividends": dividends, "Stock Splits": 0.0})
    prices.to_csv(tmp_path / "spy.csv")
    cash_dates = pd.date_range(sessions[0] - pd.Timedelta(days=3), sessions[-1], freq="D")
    for series, level in (("DFF", 4.0), ("DTB3", 5.0)):
        pd.DataFrame({"date": cash_dates, series: level}).to_csv(
            tmp_path / f"{series.lower()}.csv", index=False)
    return tmp_path, sessions, prices


def test_end_date_is_exclusive_and_uppercase_date_is_supported(controlled_inputs):
    directory, sessions, _ = controlled_inputs
    end = sessions[-10]
    market, metadata = timex.load_inputs(directory, str(end.date()), timex.Config())
    assert market.index[-1] == sessions[-11]
    assert end not in market.index
    assert len(market) == len(sessions) - 10
    assert metadata["last_price"] == str(sessions[-11].date())


def test_cash_rate_change_cannot_affect_the_same_days_return(controlled_inputs):
    directory, sessions, _ = controlled_inputs
    jump = sessions[150]
    rates = pd.read_csv(directory / "dff.csv", parse_dates=["date"])
    rates["DFF"] = np.where(rates.date < jump, 1.0, 9.0)
    rates.to_csv(directory / "dff.csv", index=False)
    market, _ = timex.load_inputs(directory, "2025-01-01", timex.Config())
    assert market.loc[jump, "dff_prior"] == pytest.approx(0.01)
    assert market.loc[sessions[151], "dff_prior"] == pytest.approx(0.09)


def test_future_rate_observation_is_never_backfilled(controlled_inputs):
    directory, sessions, _ = controlled_inputs
    rates = pd.read_csv(directory / "dff.csv", parse_dates=["date"])
    rates = rates.loc[rates.date >= sessions[1]]
    rates.to_csv(directory / "dff.csv", index=False)
    with pytest.raises(ValueError, match="Missing or stale DFF"):
        timex.load_inputs(directory, "2025-01-01", timex.Config())


def test_missing_exchange_session_is_rejected(controlled_inputs):
    directory, sessions, prices = controlled_inputs
    prices.drop(index=sessions[200]).to_csv(directory / "spy.csv")
    with pytest.raises(ValueError, match="Calendar mismatch"):
        timex.load_inputs(directory, "2025-01-01", timex.Config())


def test_stale_cash_observation_is_rejected(controlled_inputs):
    directory, sessions, _ = controlled_inputs
    rates = pd.read_csv(directory / "dff.csv", parse_dates=["date"])
    begin = sessions[160]
    rates = rates.loc[~rates.date.between(begin, begin + pd.Timedelta(days=20))]
    rates.to_csv(directory / "dff.csv", index=False)
    with pytest.raises(ValueError, match="Missing or stale DFF"):
        timex.load_inputs(directory, "2025-01-01", timex.Config())


def test_tbill_discount_yield_is_converted_before_accrual(controlled_inputs):
    directory, sessions, _ = controlled_inputs
    market, _ = timex.load_inputs(directory, "2025-01-01", timex.Config())
    # A $100 face-value 91-day bill at 5% bank discount costs this amount.
    discount = 0.05
    price = 100 * (1 - discount * 91 / 360)
    investment_yield = ((100 - price) / price) * 360 / 91
    assert market.loc[sessions[1], "dtb3_prior"] == pytest.approx(investment_yield)
    assert investment_yield > discount


def test_weekend_cash_cost_uses_elapsed_calendar_days(controlled_inputs):
    directory, _, _ = controlled_inputs
    market, _ = timex.load_inputs(directory, "2025-01-01", timex.Config())
    three_day = market.loc[market.dcf.eq(3 / 360)].iloc[0]
    assert three_day["cash_return"] == pytest.approx(0.04 * 3 / 360)
    assert three_day["cash_return"] != pytest.approx(0.04 / 252)


def test_dividend_is_added_once_and_excess_return_keeps_it(controlled_inputs):
    directory, sessions, prices = controlled_inputs
    market, _ = timex.load_inputs(directory, "2025-01-01", timex.Config())
    day, previous = sessions[100], sessions[99]
    previous_price, closing_price = prices.loc[previous, "Close"], prices.loc[day, "Close"]
    # A holder's terminal wealth is one closing share plus the $2 cash payment.
    economic_return = (closing_price + 2.0) / previous_price - 1
    row = market.loc[day]
    assert row.price_return < 0
    assert row.cash_total_return == pytest.approx(economic_return)
    assert row.adj_return == pytest.approx(economic_return)
    assert row.asset_er_return == pytest.approx(economic_return - row.cash_return)
    assert row.cash_total_return - row.price_return == pytest.approx(2 / previous_price)


@pytest.mark.parametrize("bad_field,bad_value,match", [
    ("Close", 0.0, "Missing/nonpositive"),
    ("Dividends", -1.0, "Negative cash dividend"),
    ("Stock Splits", 2.0, "split detected"),
])
def test_unsupported_or_invalid_cashflows_fail_loudly(controlled_inputs, bad_field, bad_value, match):
    directory, sessions, prices = controlled_inputs
    prices.loc[sessions[150], bad_field] = bad_value
    prices.to_csv(directory / "spy.csv")
    with pytest.raises(ValueError, match=match):
        timex.load_inputs(directory, "2025-01-01", timex.Config())


def test_download_preserves_yahoo_multiindex_price_and_total_return_fields(tmp_path, monkeypatch):
    import yfinance as yf

    index = pd.DatetimeIndex(["2024-01-02", "2024-01-03"], name="Date")
    fields = ["Close", "Adj Close", "Dividends", "Stock Splits"]
    raw = pd.DataFrame([[100, 90, 0, 0], [99, 90, 1, 0]], index=index,
                       columns=pd.MultiIndex.from_product([fields, ["SPY"]]))
    yahoo_calls, rate_urls = [], []

    def fake_download(ticker, **kwargs):
        yahoo_calls.append((ticker, kwargs))
        return raw.copy()

    def fake_get(url, **kwargs):
        rate_urls.append(url)
        series = "DFF" if "id=DFF" in url else "DTB3"
        return SimpleNamespace(text=f"observation_date,{series}\n2024-01-02,5.0\n2024-01-03,5.0\n",
                               raise_for_status=lambda: None)

    monkeypatch.setattr(yf, "download", fake_download)
    monkeypatch.setattr(yf, "set_tz_cache_location", lambda *_: None)
    monkeypatch.setattr(timex.requests, "get", fake_get)
    metadata = timex.download_inputs("2024-01-01", "2024-01-04", tmp_path)
    saved = pd.read_csv(tmp_path / "spy.csv")
    assert saved.Close.tolist() == [100, 99]
    assert saved["Adj Close"].tolist() == [90, 90]
    assert saved.Dividends.tolist() == [0, 1]
    assert saved.columns.is_unique
    ticker, kwargs = yahoo_calls[0]
    assert ticker == "SPY" and kwargs["auto_adjust"] is False
    assert kwargs["actions"] is True and kwargs["repair"] is False
    assert kwargs["end"] == "2024-01-04"
    assert metadata["requested_end_exclusive"] == "2024-01-04"
    assert all("/graph/fredgraph.csv?" in url for url in rate_urls)
    assert pd.read_csv(tmp_path / "dtb3.csv").DTB3.iloc[0] == 5.0
