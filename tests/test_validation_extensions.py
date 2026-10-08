"""Offline tests for the audit's validation, provenance and analytic additions.

Each test encodes an expectation that can be stated without the production
code: calendar completeness, unit sanity, look-ahead coverage, rounding rules,
snapshot protection and the analytic bound on the mean-reversion signal.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as timex
from test_methodology import empty_events, market_frame


def test_sample_bounds_reject_late_start_and_early_stop():
    sessions = timex.exchange_sessions("2025-01-02", "2025-03-31")
    timex.validate_sample_bounds(sessions, "2025-01-01", "2025-04-01")
    with pytest.raises(ValueError, match="Missing \\(2\\)"):
        timex.validate_sample_bounds(sessions[:-2], "2025-01-01", "2025-04-01")
    with pytest.raises(ValueError, match="2025-01-02"):
        timex.validate_sample_bounds(sessions[1:], "2025-01-01", "2025-04-01")


def test_rate_in_wrong_units_is_rejected(tmp_path):
    sessions = timex.exchange_sessions("2024-01-02", "2025-06-30")[:320]
    close = pd.Series(100 * 1.0002 ** np.arange(len(sessions)), index=sessions.rename("Date"))
    pd.DataFrame({"Close": close, "Adj Close": close, "Dividends": 0.0, "Stock Splits": 0.0}).to_csv(tmp_path / "spy.csv")
    dates = pd.date_range(sessions[0] - pd.Timedelta(days=3), sessions[-1], freq="D")
    pd.DataFrame({"date": dates, "DTB3": 5.0}).to_csv(tmp_path / "dtb3.csv", index=False)
    # A basis-point feed mistaken for percent: 388 instead of 3.88.
    pd.DataFrame({"date": dates, "DFF": 388.0}).to_csv(tmp_path / "dff.csv", index=False)
    with pytest.raises(ValueError, match="plausible annual bounds"):
        timex.load_inputs(tmp_path, str((sessions[-1] + pd.Timedelta(days=1)).date()), timex.Config())


def test_fomc_lookahead_requires_coverage_two_sessions_past_last_price():
    index = timex.exchange_sessions("2026-01-02", "2026-10-07")  # Wednesday 7 Oct
    base = {"start": "2014-01-01", "end": "2026-10-08"}
    with pytest.raises(ValueError, match="through 2026-10-09"):
        timex.validate_fomc_coverage(base, index)
    # A documented next scheduled date inside the look-ahead window is no cure.
    with pytest.raises(ValueError, match="extend the verified calendar"):
        timex.validate_fomc_coverage({**base, "next_scheduled_after_coverage": "2026-10-09"}, index)
    ok = timex.validate_fomc_coverage({**base, "next_scheduled_after_coverage": "2026-10-28"}, index)
    assert ok["lookahead_required_through"] == "2026-10-09"
    assert timex.validate_fomc_coverage({**base, "end": "2026-10-09"}, index)["certified_by"] == "verified coverage end"
    with pytest.raises(ValueError, match="does not cover"):
        timex.validate_fomc_coverage({**base, "start": "2026-02-01"}, index)


def test_download_refuses_to_overwrite_an_existing_snapshot(tmp_path):
    (tmp_path / "spy.csv").write_text("sentinel")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        timex.download_inputs("2024-01-01", "2024-01-04", tmp_path)
    assert (tmp_path / "spy.csv").read_text() == "sentinel"


def test_online_run_refuses_frozen_data_dir_before_any_network_call(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network must not be touched")
    monkeypatch.setattr(timex, "download_inputs", forbidden)
    with pytest.raises(ValueError, match="frozen reference snapshot"):
        timex.main(["--data-dir", str(timex.FROZEN_DATA_DIR), "--end", "2026-10-08",
                    "--output-dir", str(tmp_path)])


def test_snapshot_revisions_report_vendor_restatements(tmp_path):
    frozen, fresh = tmp_path / "frozen", tmp_path / "fresh"
    for directory in (frozen, fresh):
        directory.mkdir()
    dates = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"], name="Date")
    base = pd.DataFrame({"Close": [100., 101., 102.], "Adj Close": [90., 91., 92.],
                         "Dividends": [0., 0., 0.], "Stock Splits": [0., 0., 0.]}, index=dates)
    base.to_csv(frozen / "spy.csv")
    revised = base.copy()
    revised.loc["2024-01-03", "Adj Close"] = 91.05
    revised.to_csv(fresh / "spy.csv")
    for series in ("DFF", "DTB3"):
        frame = pd.DataFrame({"date": dates, series: [5.0, 5.0, 5.0]})
        frame.to_csv(frozen / f"{series.lower()}.csv", index=False)
        frame.assign(**{series: [5.0, 5.1, 5.0]}).to_csv(fresh / f"{series.lower()}.csv", index=False)
    report = timex.snapshot_revisions(fresh, frozen)
    assert report["SPY"]["Adj Close"]["dates_differing_over_1e-6"] == 1
    assert report["SPY"]["Adj Close"]["max_abs_difference"] == pytest.approx(0.05)
    assert report["SPY"]["Close"]["dates_differing_over_1e-6"] == 0
    assert report["DFF"]["dates_differing"] == 1


def test_short_mr_analytic_bound_is_tight_and_below_cap():
    bound = timex.short_mr_bound()
    assert bound == pytest.approx(5 * 0.15 * np.sqrt(55) / 15 * np.sqrt(10 / 252))
    assert 0.0738 < bound < 0.0739
    dates = timex.exchange_sessions("2025-01-02", "2025-03-31")
    # Cauchy-Schwarz equality case: recent returns proportional to the 5..1
    # weights, older five zero. Small, NEGATIVE moves give a positive signal.
    returns = np.zeros(len(dates))
    returns[6:11] = -1e-6 * np.array([1, 2, 3, 4, 5])
    s = timex.generate_signals(market_frame(dates, returns), empty_events(), timex.Config())
    assert s.short_mr.iloc[10] == pytest.approx(bound, rel=1e-4)
    # Random paths with daily moves within +/-20% never reach the 10% clip.
    rng = np.random.default_rng(3)
    for _ in range(25):
        path = rng.uniform(-0.2, 0.2, len(dates))
        signal = timex.generate_signals(market_frame(dates, path), empty_events(), timex.Config()).short_mr
        assert signal.abs().max() < 0.10


def test_simplified_model_needs_both_volatility_windows():
    dates = pd.date_range("2025-01-06", periods=4, freq="B")
    market = pd.DataFrame({"dcf": [0., 1/360, 1/360, 1/360], "price_return": [0., .01, .01, .01],
                           "tbill_cash_return": 0.0}, index=dates)
    feat = pd.DataFrame({"spy_vol20": [.10, .10, .10, .10], "spy_vol60": [np.nan, np.nan, .20, .20]}, index=dates)
    result = timex.simplified_model(market, feat, timex.Config(), costs=False)
    # max(20d, 60d) is undefined until the 60-day window exists: no exposure.
    assert result.weight_close.to_list() == pytest.approx([0., 0., .5, .5])


@pytest.mark.parametrize("value,expected", [(100.005, 100.01), (100.0049, 100.0), (2.675, 2.68),
                                            (99.995, 100.0), (0.125, 0.13)])
def test_published_level_rounding_is_half_up_to_two_decimals(value, expected):
    assert timex.round_half_up(pd.Series([value])).iloc[0] == expected


def test_official_reconciliation_level_mode_and_regime_metrics(tmp_path):
    dates = timex.exchange_sessions("2023-06-01", "2023-09-29")
    research = pd.Series(100 * np.cumprod(1 + np.tile([.003, -.001, .002], len(dates))[:len(dates)]), index=dates)
    official = timex.round_half_up(research)
    official.iloc[-1] += 0.01  # one deliberate one-cent publication difference
    path = tmp_path / "official.csv"
    pd.DataFrame({"date": list(dates) + [pd.Timestamp("2023-10-01")],  # a Sunday: not a session
                  "level": list(official) + [official.iloc[-1]]}).to_csv(path, index=False)
    result = timex.audit_official(research, path, "2023-07-28", tmp_path, compare_levels=True)
    assert result["official_dates_not_matched"] == 1
    assert result["unrebased_exact_2dp_match_fraction"] == pytest.approx(1 - 1 / len(dates))
    assert result["unrebased_max_abs_level_error"] == pytest.approx(0.01, abs=1e-9)
    for regime in ("pre_launch", "live"):
        assert {f"{regime}_tracking_error", f"{regime}_mean_daily_return_bias",
                f"{regime}_max_absolute_rebased_level_error"} <= set(result)
    stored = pd.read_csv(tmp_path / "official_reconciliation.csv")
    assert {"Research Unrebased Rounded", "Official Unrebased"} <= set(stored.columns)


def test_diagnostic_hooks_default_to_the_methodology():
    """The fingerprint's alternative conventions are opt-in keyword arguments;
    calling the functions without them must reproduce the methodology path."""
    dates = timex.exchange_sessions("2025-01-02", "2025-06-30")
    market = market_frame(dates, np.r_[0, np.tile([.01, -.008, .004], len(dates))][:len(dates)])
    cfg = timex.Config()
    s_default = timex.generate_signals(market, empty_events(), cfg)
    s_explicit = timex.generate_signals(market, empty_events(), cfg, tom_mr_reference_offset=5)
    pd.testing.assert_frame_equal(s_default, s_explicit)
    f_default = timex.controlled_weights(market, s_default, cfg)
    f_explicit = timex.controlled_weights(market, s_default, cfg, vol_lag=1, vol_on_allocated=True)
    pd.testing.assert_frame_equal(f_default, f_explicit)
    with pytest.raises(ValueError, match="vol_lag"):
        timex.controlled_weights(market, s_default, cfg, vol_lag=2)
    with pytest.raises(ValueError, match="servicing_base"):
        timex.accounting(market.asset_er_return, f_default.weight_close, market.dcf, cfg, servicing_base="gross")


def _single_moves(dates, moves):
    returns = np.zeros(len(dates))
    for day, value in moves.items():
        returns[dates.get_loc(pd.Timestamp(day))] = value
    return market_frame(dates, returns)


def test_tom_mean_reversion_reference_is_eom_minus_five_not_minus_four():
    """Constant-return fixtures cannot tell EOM-5 from EOM-4; this path can.

    March 2025: EOM = 31 Mar, EOM-5 = 24 Mar, EOM-4 = 25 Mar, prior EOM = 28 Feb.
    A fall on 24 Mar puts A[EOM-5] below A[prior EOM]; a larger rise on 25 Mar
    puts A[EOM-4] above it. Only the methodology's EOM-5 reference fires.
    """
    dates = timex.exchange_sessions("2025-02-03", "2025-04-30")
    market = _single_moves(dates, {"2025-03-24": -0.01, "2025-03-25": 0.02})
    s = timex.generate_signals(market, empty_events(), timex.Config())
    march = s.loc["2025-03"]
    assert list(march.index[march.tom_mr.eq(1)]) == list(pd.to_datetime(
        ["2025-03-25", "2025-03-26", "2025-03-27", "2025-03-28"]))
    brochure_reading = timex.generate_signals(market, empty_events(), timex.Config(), tom_mr_reference_offset=4)
    assert brochure_reading.loc["2025-03", "tom_mr"].eq(0).all()


def test_option_expiry_reference_is_oe_minus_four_against_prior_expiry():
    """April 2025 expiry is Thu 17 Apr (Good Friday). OE-4 = 11 Apr, OE-3 = 14 Apr,
    prior expiry = 21 Mar. A fall on 11 Apr and a larger rise on 14 Apr make the
    OE-4 and OE-3 readings disagree in sign; the methodology uses OE-4."""
    dates = timex.exchange_sessions("2025-02-03", "2025-04-30")
    market = _single_moves(dates, {"2025-04-11": -0.01, "2025-04-14": 0.02})
    s = timex.generate_signals(market, empty_events(), timex.Config())
    april = s.loc["2025-04"]
    assert list(april.index[april.opex.ne(0)]) == list(pd.to_datetime(["2025-04-14", "2025-04-15", "2025-04-16"]))
    assert april.opex.loc["2025-04-14":"2025-04-16"].eq(-1).all()
