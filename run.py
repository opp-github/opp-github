#!/usr/bin/env python3
"""Goldman Sachs Equity TimeX: methodology research reconstruction and audit.

Source: supplied Goldman Sachs methodology, July 28, 2023, sections 2-11.
This is an independent research implementation, not a certified official series.
Run `python run.py --help`. All offline inputs, outputs and manifests are explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import itertools
import json
import logging
import os
import platform
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from io import StringIO
from pathlib import Path
import time
from typing import Callable

# Avoid GUI dependencies; both charts and the entire pipeline run headlessly.
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).parent / ".mplcache"))
os.environ.setdefault("XDG_CACHE_HOME", str(Path(__file__).parent / ".cache"))
Path(os.environ["XDG_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
import exchange_calendars as xcals
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter
import numpy as np
import pandas as pd
import requests
import seaborn as sns

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("timex")
METHODOLOGY_URL = "https://www.goldmansachs.com/what-we-do/ficc-and-equities/products-and-business-groups/products/index-methodologies"
INDEX_NAME = "TimeX research reconstruction"
PRICE_NAME = "SPY Price Return"
TOTAL_NAME = "SPY Total Return"
SIMPLE_NAME = "Requested simplified approximation"

# The frozen snapshot reproduces the reference results. Fresh downloads go to a
# separate directory so they can never silently overwrite those inputs.
FROZEN_DATA_DIR = ROOT / "data"
FRESH_DATA_DIR = ROOT / "data_fresh"
FOMC_CALENDAR_PATH = FROZEN_DATA_DIR / "fomc_calendar.csv"
# Annual cash rates outside this band almost certainly indicate a unit or vendor
# error (e.g. 388 instead of 3.88); fail rather than accrue them.
RATE_BOUNDS = (-0.05, 0.25)
# Pre-anchor sessions needed before an accounting ledger is used for published
# anchor checks; EWMA seed influence is then below 0.97**252 = 5e-4 in variance.
ANCHOR_LEDGER_WARMUP = 252


@dataclass(frozen=True)
class Config:
    """Immutable, serialized model assumptions. Annual rates are decimals."""
    vol_target: float = 0.10
    equity_cap: float = 1.50
    base_weight: float = 0.75
    allocation_step: float = 0.75
    lookthrough_step: float = 0.50
    deduction: float = 0.005
    servicing: float = 0.005
    transaction: float = 0.0001
    lambda_short: float = 0.94
    lambda_long: float = 0.97
    seed_short: float = 0.120433
    seed_long: float = 0.146234
    day_basis: float = 360.0
    official_seed_date: str = "2021-05-03"
    index_base_date: str = "2021-06-01"
    launch_date: str = "2023-07-28"
    calendar: str = "XNYS"  # holiday proxy for NYSE Arca; see methodology audit
    short_window: int = 20
    long_window: int = 60
    signal_leverages: tuple = (0.75, 0.75, 0.75, 0.75, 1.50)

    def __post_init__(self):
        if not 0 < self.vol_target or not 0 < self.equity_cap:
            raise ValueError("Target and cap must be positive")
        if not all(0 < x < 1 for x in (self.lambda_short, self.lambda_long)):
            raise ValueError("EWMA decay factors must be strictly between zero and one")
        if min(self.deduction, self.servicing, self.transaction) < 0:
            raise ValueError("Costs must be nonnegative")


@dataclass(frozen=True)
class PublishedAnchor:
    """A number Goldman Sachs printed that a correct reconstruction should approach.

    These are GS backtest/initialization figures, built on GS's own data. They are
    consistency anchors, not official daily levels: agreement corroborates the
    implemented conventions but does not certify replication.
    """
    key: str
    label: str
    published: float          # percent, exactly as printed
    decimals: int             # printed precision
    family: str
    source: str
    start: str | None = None  # trailing-return window start (inclusive close)
    years: int | None = None  # None: not annualized / not a return


ANCHOR_SEED_DATE = "2021-05-03"     # Asset Base Date: EWMA set to initialization vols
ANCHOR_RETURN_END = "2023-07-28"    # Brochure performance end date (launch date)
PUBLISHED_ANCHORS = (
    PublishedAnchor("seed_vol_094", "0.94 EWMA vol, 3 May 2021", 12.0433, 4, "Seed volatility",
                    "Methodology PDF p.9 (printed p.7): Asset Initialization Volatility"),
    PublishedAnchor("seed_vol_097", "0.97 EWMA vol, 3 May 2021", 14.6234, 4, "Seed volatility",
                    "Methodology PDF p.9 (printed p.7): Asset Initialization Volatility"),
    PublishedAnchor("return_1y", "1Y return to 28 Jul 2023", 2.15, 2, "Trailing return",
                    "Brochure PDF p.6: 1 Year Return, 27 Jul 2022 - 28 Jul 2023", "2022-07-27", 1),
    PublishedAnchor("return_3y", "3Y annualized return", 5.32, 2, "Trailing return",
                    "Brochure PDF p.6: 3 Year Annualized Return, 27 Jul 2020 - 28 Jul 2023", "2020-07-27", 3),
    PublishedAnchor("return_5y", "5Y annualized return", 6.02, 2, "Trailing return",
                    "Brochure PDF p.6: 5 Year Annualized Return, 27 Jul 2018 - 28 Jul 2023", "2018-07-27", 5),
    PublishedAnchor("cost_rate_avg", "Servicing + rebalancing, rolling 1Y average", 0.61, 2, "Cost rate",
                    "Methodology PDF p.5 (printed p.3) Summary s.5; GS sample 3 Jan 2000 - 28 Jul 2023"),
    PublishedAnchor("cost_rate_max", "Servicing + rebalancing, rolling 1Y high", 0.92, 2, "Cost rate",
                    "Methodology PDF p.5 (printed p.3) Summary s.5; GS sample 3 Jan 2000 - 28 Jul 2023"),
)
# Alternative conventions evaluated against the anchors. Each is a plausible
# misreading or shortcut; none is the methodology except the first row.
FINGERPRINT_CONVENTIONS = (
    ("Methodology baseline", {}),
    ("Dividends forfeited (Close-only ER)", {"asset": "close_only"}),
    ("Yahoo Adj Close total return in ER", {"asset": "adj_close"}),
    ("DTB3 money-market funding", {"asset": "dtb3"}),
    ("Same-day Fed Funds (look-ahead)", {"asset": "rate_same_day"}),
    ("ACT/365 financing accrual", {"asset": "act365"}),
    ("ToM-MR reference EOM-4 (brochure wording)", {"tom_mr_reference_offset": 4}),
    ("Same-day volatility in target (VC_t)", {"vol_lag": 0}),
    ("EWMA on unallocated SPY ER", {"vol_on_allocated": False}),
    ("Servicing on NAV, not notional", {"servicing_base": "nav"}),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_dates(index) -> pd.DatetimeIndex:
    """Preserve exchange-local dates when stripping time zones, never UTC-shift."""
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def exchange_sessions(start, end, name="XNYS") -> pd.DatetimeIndex:
    # Calendar construction bounds are session bounds; pad so weekend/holiday
    # start/end arguments remain inside the calendar's available session range.
    cal = xcals.get_calendar(name, start=str((pd.Timestamp(start)-pd.Timedelta(days=7)).date()),
                              end=str((pd.Timestamp(end)+pd.Timedelta(days=7)).date()))
    return normalize_dates(cal.sessions_in_range(start, end))


def retry(fn: Callable, description: str):
    """Bounded network retry. Fail loudly; never substitute synthetic prices."""
    error = None
    for attempt in range(3):
        try:
            return fn()
        except (requests.RequestException, OSError, ValueError) as exc:
            error = exc
            LOG.warning("%s attempt %d: %s", description, attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{description} failed after three attempts") from error


def download_inputs(start: str, end: str, directory: Path, overwrite: bool = False) -> dict:
    """yfinance Close + Adj Close + cash actions; FRED DFF and DTB3, no key.

    Yahoo end date is EXCLUSIVE. auto_adjust=False is essential: otherwise Close
    would silently become dividend-adjusted. Corporate-action repair is disabled
    so a vendor repair algorithm does not become an undocumented model input.
    An existing snapshot is never overwritten unless explicitly requested.
    """
    import yfinance as yf
    directory.mkdir(parents=True, exist_ok=True)
    targets = [directory / name for name in ("spy.csv", "dff.csv", "dtb3.csv")]
    if not overwrite and any(path.exists() for path in targets):
        raise FileExistsError(f"{directory} already holds an input snapshot; refusing to overwrite it. "
                              "Use another --data-dir or pass --overwrite-inputs deliberately.")
    yf.set_tz_cache_location(str(directory / ".yfinance_cache"))
    parameters = {"start": start, "end_exclusive": end, "auto_adjust": False, "actions": True, "repair": False}

    def get_spy():
        raw = yf.download("SPY", start=start, end=end, auto_adjust=False,
                          actions=True, progress=False, threads=False,
                          repair=False, timeout=30)
        if raw.empty:
            raise ValueError("Yahoo returned no SPY observations")
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        required = {"Close", "Adj Close", "Dividends", "Stock Splits"}
        if not required.issubset(raw.columns):
            raise ValueError(f"Missing Yahoo fields: {required - set(raw.columns)}")
        raw.index = normalize_dates(raw.index)
        raw.index.name = "date"
        return raw

    spy = retry(get_spy, "SPY download")
    spy.to_csv(targets[0], float_format="%.12g")
    sources = {"SPY": {"provider": "Yahoo via yfinance", "ticker": "SPY", "file": targets[0].name,
                       "url": "https://finance.yahoo.com/quote/SPY/history/",
                       "download_parameters": parameters,
                       "first_actual_observation": str(spy.index.min().date()),
                       "last_actual_observation": str(spy.index.max().date()), "rows": len(spy),
                       "dividend_events": int((spy["Dividends"] > 0).sum()),
                       "split_events": int((spy["Stock Splits"] != 0).sum()),
                       "sha256": sha256(targets[0])}}
    for series, path in (("DFF", targets[1]), ("DTB3", targets[2])):
        rate_end = str((pd.Timestamp(end)-pd.Timedelta(days=1)).date())
        csv_url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}&coed={rate_end}"

        def get_rate(csv_url=csv_url, series=series):
            response = requests.get(csv_url, timeout=40)
            response.raise_for_status()
            raw = pd.read_csv(StringIO(response.text), na_values=".")
            if series not in raw.columns or raw.empty:
                raise ValueError(f"Invalid FRED CSV for {series}")
            raw.columns = ["date", series]
            return raw

        rates = retry(get_rate, f"FRED {series}")
        rates.to_csv(path, index=False)
        valid = rates.dropna()
        sources[series] = {"provider": "FRED / Federal Reserve H.15", "file": path.name,
                           "source_page": f"https://fred.stlouisfed.org/series/{series}",
                           "url": csv_url, "units": "annual percentage points",
                           "first_actual_observation": str(valid.iloc[0, 0]),
                           "last_actual_observation": str(valid.iloc[-1, 0]),
                           "rows": len(rates), "missing_observations": int(rates[series].isna().sum()),
                           "sha256": sha256(path)}
    manifest = {"requested_start": start, "requested_end_exclusive": end,
                "downloaded_utc": datetime.now(timezone.utc).isoformat(),
                "yfinance_version": importlib.metadata.version("yfinance"), "sources": sources}
    (directory / "download_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def load_inputs(directory: Path, end: str, cfg: Config) -> tuple[pd.DataFrame, dict]:
    """Validate prices against a complete exchange calendar, align cash causally.

    Rates are forward-filled FROM PAST observations only. No backfill is allowed.
    Missing rate intervals longer than seven calendar days fail the run, as do
    rate observations outside RATE_BOUNDS (a unit or vendor error).
    """
    market = pd.read_csv(directory / "spy.csv")
    market = market.rename(columns={"Date": "date"})
    market["date"] = pd.to_datetime(market["date"])
    market = market.set_index("date")
    market.index = normalize_dates(market.index)
    market = market.loc[market.index < pd.Timestamp(end)].sort_index()
    if market.index.has_duplicates or len(market) < 300:
        raise ValueError("SPY dates must be unique with at least 300 observations")
    required = ["Close", "Adj Close", "Dividends", "Stock Splits"]
    if not set(required).issubset(market.columns):
        raise ValueError(f"SPY input requires {required}")
    if not np.isfinite(market[required].to_numpy()).all() or (market[["Close", "Adj Close"]] <= 0).any().any():
        raise ValueError("Missing/nonpositive prices or missing corporate actions")
    if "Capital Gains" in market and market["Capital Gains"].fillna(0).ne(0).any():
        raise ValueError("Unexpected SPY capital gains distribution; audit return accounting")
    if (market["Dividends"] < 0).any():
        raise ValueError("Negative cash dividend")
    if market["Stock Splits"].ne(0).any():
        raise ValueError("SPY split detected: audit per-share normalization before proceeding")
    expected = exchange_sessions(market.index[0], market.index[-1], cfg.calendar)
    if not expected.equals(market.index):
        raise ValueError(f"Calendar mismatch. Missing: {expected.difference(market.index).tolist()}; "
                         f"unexpected: {market.index.difference(expected).tolist()}")
    metadata = {"first_price": str(market.index[0].date()),
                "last_price": str(market.index[-1].date()), "rows": len(market),
                "dividend_events": int((market["Dividends"] > 0).sum()),
                "split_events": int(market["Stock Splits"].ne(0).sum())}
    for series in ("DFF", "DTB3"):
        raw = pd.read_csv(directory / f"{series.lower()}.csv")
        raw.columns = ["date", series]
        raw["date"] = pd.to_datetime(raw["date"])
        raw[series] = pd.to_numeric(raw[series], errors="coerce")
        raw = raw.dropna().sort_values("date")
        if raw["date"].duplicated().any():
            raise ValueError(f"Duplicate {series} dates")
        if not raw[series].div(100.0).between(*RATE_BOUNDS).all():
            raise ValueError(f"{series} observations outside plausible annual bounds {RATE_BOUNDS}; check units")
        # The return from previous close to this close uses the previous session's
        # financing rate; release/vintage limitations are recorded in the report.
        prior_dates = pd.Series(market.index, index=market.index).shift(1)
        left = pd.DataFrame({"pricing_date": market.index, "asof": prior_dates.values})
        valid = left["asof"].notna()
        aligned = pd.merge_asof(left.loc[valid].sort_values("asof"), raw,
                                left_on="asof", right_on="date", direction="backward")
        age = (aligned["asof"] - aligned["date"]).dt.days
        if aligned[series].isna().any() or age.max() > 7:
            raise ValueError(f"Missing or stale {series} cash rates (>7 days)")
        y = aligned.set_index("pricing_date")[series] / 100.0
        if series == "DTB3":
            # DTB3 is BANK DISCOUNT yield, not an investment return. Convert with
            # an explicit 91-day tenor proxy: y_MM = d / (1 - d * tenor / 360).
            # This remains a short-rate proxy, not a traded T-bill total return.
            y = y / (1.0 - y * 91.0 / 360.0)
        market[series.lower() + "_prior"] = y.reindex(market.index)
        metadata[series + "_last"] = str(raw["date"].max().date())
        metadata[series + "_max_staleness_days"] = int(age.max())
    market["dcf"] = market.index.to_series().diff().dt.days / cfg.day_basis
    market["price_return"] = market["Close"].pct_change(fill_method=None)
    market["adj_return"] = market["Adj Close"].pct_change(fill_method=None)
    market["cash_dividend_return"] = market["Dividends"] / market["Close"].shift(1)
    market["cash_total_return"] = market["price_return"] + market["cash_dividend_return"]
    market["cash_return"] = market["dff_prior"] * market["dcf"]
    market["tbill_cash_return"] = market["dtb3_prior"] * market["dcf"]
    market["asset_er_return"] = market["cash_total_return"] - market["cash_return"]
    market["asset_er"] = 100 * (1 + market["asset_er_return"].fillna(0)).cumprod()
    if (market["asset_er"] <= 0).any():
        raise ValueError("Excess-return asset reached zero; cannot estimate log vol")
    return market, metadata


def validate_sample_bounds(index: pd.DatetimeIndex, start, end, calendar: str = "XNYS") -> dict:
    """Every exchange session in [start, end) must be present.

    load_inputs rejects interior gaps; this additionally rejects a vendor response
    that silently starts late or stops early, which would otherwise shorten the
    requested sample without any error.
    """
    last_allowed = pd.Timestamp(end) - pd.Timedelta(days=1)
    expected = exchange_sessions(pd.Timestamp(start), last_allowed, calendar)
    if not expected.equals(index):
        missing = [str(d.date()) for d in expected.difference(index)]
        unexpected = [str(d.date()) for d in index.difference(expected)]
        raise ValueError(f"Price sample does not cover every {calendar} session in [{start}, {end}). "
                         f"Missing ({len(missing)}): {missing[:10]}; unexpected ({len(unexpected)}): {unexpected[:10]}. "
                         "If the vendor has not yet published the latest close, use an earlier --end.")
    return {"requested_start": str(pd.Timestamp(start).date()), "requested_end_exclusive": str(pd.Timestamp(end).date()),
            "sessions_expected": len(expected), "sessions_present": len(index)}


def snapshot_revisions(fresh_dir: Path, frozen_dir: Path) -> dict:
    """Disclose vendor revisions between a fresh download and the frozen snapshot.

    Yahoo can restate adjusted closes and FRED can revise observations. These
    differences are reported, never silently absorbed into the reference results.
    """
    def read(path, index_col=0):
        frame = pd.read_csv(path, index_col=index_col)
        frame.index = normalize_dates(pd.to_datetime(frame.index))
        return frame

    report = {}
    old, new = read(frozen_dir / "spy.csv"), read(fresh_dir / "spy.csv")
    common = old.index.intersection(new.index)
    report["SPY"] = {"common_dates": len(common), "only_in_frozen": len(old.index.difference(new.index)),
                     "only_in_fresh": len(new.index.difference(old.index))}
    for field in ("Close", "Adj Close", "Dividends"):
        diff = (new.loc[common, field] - old.loc[common, field]).abs()
        report["SPY"][field] = {"max_abs_difference": float(diff.max()),
                                "dates_differing_over_1e-6": int((diff > 1e-6).sum())}
    for series in ("DFF", "DTB3"):
        old_r = read(frozen_dir / f"{series.lower()}.csv").iloc[:, 0].dropna()
        new_r = read(fresh_dir / f"{series.lower()}.csv").iloc[:, 0].dropna()
        common = old_r.index.intersection(new_r.index)
        diff = (new_r.loc[common] - old_r.loc[common]).abs()
        report[series] = {"common_dates": len(common), "max_abs_difference_pct_points": float(diff.max()),
                          "dates_differing": int((diff > 0).sum())}
    return report


def third_friday(month: pd.Period) -> pd.Timestamp:
    first = month.start_time.normalize()
    return first + pd.Timedelta(days=(4 - first.weekday()) % 7 + 14)


def load_fomc(path: Path, end: str) -> pd.DataFrame:
    events = pd.read_csv(path, parse_dates=["date"])
    if not {"date", "event_type", "source_url"}.issubset(events):
        raise ValueError("FOMC CSV requires date,event_type,source_url")
    # Retain known scheduled dates beyond a historical price cutoff: decisions
    # one/two sessions before a meeting still belong inside the requested sample.
    # The source calendar's own verified-as-of coverage excludes unseen events.
    events = events.sort_values("date")
    if events["date"].duplicated().any() or events.source_url.isna().any():
        raise ValueError("Duplicate FOMC dates or missing source URLs")
    return events


def validate_fomc_coverage(coverage: dict, index: pd.DatetimeIndex, calendar: str = "XNYS") -> dict:
    """The FOMC decision at close T looks ahead to sessions T+1 and T+2.

    Coverage must therefore certify the scheduled calendar through the second
    session after the last price: either the verified coverage extends that far,
    or the coverage file documents the next scheduled announcement after its end
    and that announcement lies beyond the look-ahead window.
    """
    start, end = pd.Timestamp(coverage["start"]), pd.Timestamp(coverage["end"])
    if start > index[0] or end < index[-1]:
        raise ValueError("FOMC calendar coverage does not cover the downloaded SPY sample; update it explicitly")
    required = exchange_sessions(index[-1], index[-1] + pd.Timedelta(days=14), calendar)[2]
    result = {"last_price": str(index[-1].date()), "lookahead_required_through": str(required.date()),
              "coverage_end": str(end.date())}
    if end >= required:
        return {**result, "certified_by": "verified coverage end"}
    upcoming = coverage.get("next_scheduled_after_coverage")
    if upcoming is None or pd.Timestamp(upcoming) <= required:
        raise ValueError(f"FOMC coverage ends {end.date()} but the decision at {index[-1].date()} needs the "
                         f"scheduled calendar through {required.date()}; extend the verified calendar")
    return {**result, "certified_by": f"next scheduled announcement {upcoming} lies beyond the look-ahead"}


def generate_signals(market: pd.DataFrame, events: pd.DataFrame, cfg: Config,
                     tom_mr_reference_offset: int = 5) -> pd.DataFrame:
    """Compute close-t decision signals; they affect P&L starting session t+1.

    A padded exchange calendar prevents partial sample months from falsely
    looking like month-end, and resolves Good Friday option expiry correctly.
    Calendar dates are deterministic; price comparisons use only already-known
    closes. FOMC is scheduled-only, with archival (not point-in-time) dates.
    `tom_mr_reference_offset` is the methodology's EOM-5; other values exist only
    for the published-anchor diagnostics and are never used for the index.
    """
    idx = market.index
    first = (idx[0].to_period("M") - 1).start_time
    last = (idx[-1].to_period("M") + 1).end_time.normalize()
    sessions = exchange_sessions(first, last, cfg.calendar)
    groups = {m: g for m, g in pd.Series(sessions, index=sessions).groupby(sessions.to_period("M"))}
    er = market["asset_er"]
    s = pd.DataFrame(0.0, index=idx, columns=["tom_mr", "tom_long", "opex", "fomc", "short_mr"])
    flags = pd.DataFrame(False, index=idx, columns=["tom_requested", "opex_date", "fomc_announcement"])
    for month in idx.to_period("M").unique():
        days = pd.DatetimeIndex(groups[month].values)
        # User diagnostic convention: final 2 and first 3 TRADING/RETURN days.
        flags.loc[idx.intersection(days[:3].union(days[-2:])), "tom_requested"] = True
        decision_days = days[:3].union(days[-1:])
        s.loc[idx.intersection(decision_days), "tom_long"] = 1.0
        previous_days = groups.get(month - 1)
        if previous_days is not None and len(days) > tom_mr_reference_offset:
            cutoff, prior_eom = days[-1 - tom_mr_reference_offset], previous_days.iloc[-1]
            if cutoff in er.index and prior_eom in er.index and er[cutoff] < er[prior_eom]:
                s.loc[idx.intersection(days[-5:-1]), "tom_mr"] = 1.0
        expiry_pos = sessions.searchsorted(third_friday(month), side="right") - 1
        prior_pos = sessions.searchsorted(third_friday(month - 1), side="right") - 1
        if expiry_pos < 4 or prior_pos < 0:
            # The padded calendar makes this unreachable; never wrap to sessions[-1].
            continue
        expiry, prior_expiry = sessions[expiry_pos], sessions[prior_pos]
        if expiry in idx:
            flags.loc[expiry, "opex_date"] = True
        cutoff = sessions[expiry_pos - 4]
        if cutoff in er.index and prior_expiry in er.index:
            direction = float(np.sign(er[cutoff] - er[prior_expiry]))
            s.loc[idx.intersection(sessions[expiry_pos - 3:expiry_pos]), "opex"] = direction
    scheduled = events.loc[events.event_type == "scheduled", "date"]
    for day in scheduled:
        if sessions[0] <= day <= sessions[-1]:
            pos = sessions.searchsorted(day)
            if pos >= len(sessions) or sessions[pos] != day:
                raise ValueError(f"Scheduled FOMC date is not a session: {day}")
            if day in idx:
                flags.loc[day, "fomc_announcement"] = True
            s.loc[idx.intersection(sessions[max(0, pos - 2):pos]), "fomc"] = 1.0
    r = market["asset_er_return"]
    # Formula §10.3.4: 5,4,3,2,1 weights; scalar is 15% / 10-day
    # annualized RMS LOG return (not de-meaned sample standard deviation).
    # Zero-RMS convention: a ten-session history of exactly zero returns also has
    # a zero weighted return, so the signal is set to its limit of zero.
    rms10 = np.sqrt(np.log1p(r).pow(2).rolling(10, min_periods=10).mean() * 252)
    recent = sum((5 - lag) * r.shift(lag) for lag in range(5)) / 15.0
    scalar = 0.15 / rms10.replace(0, np.nan)
    s["short_mr"] = (-5.0 * scalar * recent).clip(-0.10, 0.10).fillna(0)
    s["mr_vol_scalar"] = scalar
    core = ["tom_mr", "tom_long", "opex", "fomc", "short_mr"]
    s["raw_allocation_target"] = cfg.base_weight + s[core].mul(cfg.signal_leverages).sum(axis=1)
    # The first month / ten observations are warmup only. Run audit identifies
    # them explicitly; no backfill leaks later prices into these initial signals.
    return s.join(flags)


def short_mr_bound() -> float:
    """Upper bound on |short_mr| when log(1+r) ~ r (Cauchy-Schwarz).

    |sum a_i r_i| <= ||a|| * ||r||, a = (5,4,3,2,1)/15 on five of ten returns and
    ||r|| = sqrt(10) * RMS. Hence |signal| <= 5 * 0.15 * sqrt(55)/15 * sqrt(10/252)
    = 7.39%: the ±10% clip binds only for daily returns of roughly +80% or more.
    """
    return 5 * 0.15 * np.sqrt(55) / 15 * np.sqrt(10 / 252)


def controlled_weights(market: pd.DataFrame, signals: pd.DataFrame, cfg: Config,
                       official_seeds: bool = False, vol_lag: int = 1,
                       vol_on_allocated: bool = True) -> pd.DataFrame:
    """Sequential algorithmic portfolio and two-stage volatility control.

    Volatility is on the ALGORITHMIC (signal allocated) ER portfolio. For t-close
    target lookthrough, multiply allocation[t] by target/vol[t-1], then slew-limit
    the change to 50 percentage points and cap at 150%. The current day's shock
    cannot retroactively reduce the weight that earned that day's return.
    `vol_lag=0` and `vol_on_allocated=False` are diagnostic misreadings used only
    by the published-anchor fingerprint; the defaults are the methodology.
    """
    if vol_lag not in (0, 1):
        raise ValueError("vol_lag must be 1 (methodology) or 0 (diagnostic)")
    n = len(market)
    alloc = np.zeros(n)
    alg = np.full(n, 100.0)
    vs, vl = np.full(n, np.nan), np.full(n, np.nan)
    target, final = np.zeros(n), np.zeros(n)
    er_r = market.asset_er_return.fillna(0).to_numpy()
    raw = signals.raw_allocation_target.to_numpy()
    for i, date in enumerate(market.index):
        seed = i == 0 or (official_seeds and date == pd.Timestamp(cfg.official_seed_date))
        if seed:
            # Historical warmup uses the published seed as an explicit proxy;
            # the official-inception run resets it on the actual Asset Base Date.
            alloc[i] = raw[i] if i > 0 else np.clip(raw[i], 0, cfg.equity_cap)
            alg[i] = 100.0
            vs[i], vl[i] = cfg.seed_short, cfg.seed_long
        else:
            alloc[i] = np.clip(alloc[i-1] + np.clip(raw[i] - alloc[i-1],
                                -cfg.allocation_step, cfg.allocation_step), 0, cfg.equity_cap)
            alg_r = alloc[i-1] * er_r[i]
            if alg_r <= -1:
                raise ValueError("Algorithmic portfolio reached zero; log vol undefined")
            alg[i] = alg[i-1] * (1 + alg_r)
            log_r2 = np.log1p(alg_r if vol_on_allocated else er_r[i]) ** 2
            vs[i] = np.sqrt(cfg.lambda_short * vs[i-1]**2 + (1-cfg.lambda_short)*252*log_r2)
            vl[i] = np.sqrt(cfg.lambda_long * vl[i-1]**2 + (1-cfg.lambda_long)*252*log_r2)
        lag_vol = max(vs[i-1], vl[i-1]) if (i and vol_lag == 1) else max(vs[i], vl[i])
        if lag_vol <= 0:
            target[i] = cfg.equity_cap if alloc[i] > 0 else 0
        else:
            target[i] = cfg.vol_target / lag_vol * alloc[i]
        initial = i == 0 or (official_seeds and date == pd.Timestamp(cfg.index_base_date))
        final[i] = min(cfg.equity_cap, target[i]) if initial else np.clip(
            final[i-1] + np.clip(target[i]-final[i-1], -cfg.lookthrough_step,
                                cfg.lookthrough_step), 0, cfg.equity_cap)
    output = signals.copy()
    output["allocation"] = alloc
    output["algorithmic_value"] = alg
    output["ewma_short"] = vs
    output["ewma_long"] = vl
    output["algorithmic_vol"] = np.maximum(vs, vl)
    output["lookthrough_target"] = target
    output["weight_close"] = final
    output["weight_held"] = output.weight_close.shift(1)
    output["spy_vol20"] = np.log(market.Close).diff().rolling(cfg.short_window).std(ddof=1)*np.sqrt(252)
    output["spy_vol60"] = np.log(market.Close).diff().rolling(cfg.long_window).std(ddof=1)*np.sqrt(252)
    return output


def accounting(asset_returns: pd.Series, weights: pd.Series, dcf: pd.Series,
               cfg: Config, initial_level: float = 100.0,
               servicing_base: str = "notional") -> pd.DataFrame:
    """Exact §2-6 quantity accounting. Costs reduce NAV after quantity selection.

    Q_t = gross_t * W_t / A_t; this is intentionally GROSS, not net, NAV.
    RC_t = |Q_t-Q_(t-1)| * A_t * rate. This includes drifting quantities and
    previous cost deductions, which a simple abs(delta weight) model misses.
    Index at/below zero is absorbing, as required by §2.
    `servicing_base="nav"` is a diagnostic misreading for the anchor fingerprint.
    """
    if servicing_base not in ("notional", "nav"):
        raise ValueError("servicing_base must be 'notional' (methodology) or 'nav' (diagnostic)")
    if not asset_returns.index.equals(weights.index) or not weights.index.equals(dcf.index):
        raise ValueError("Accounting indices are not aligned")
    if not weights.between(0, cfg.equity_cap + 1e-12).all():
        raise ValueError("Weights outside authorized range")
    r = asset_returns.fillna(0).to_numpy()
    if np.any(1 + r <= 0):
        raise ValueError("Underlying asset ER at/below zero")
    asset = 100.0 * np.cumprod(1 + np.r_[0.0, r[1:]])
    n = len(r)
    net, gross, qty = np.zeros(n), np.zeros(n), np.zeros(n)
    rc, sc, dc = np.zeros(n), np.zeros(n), np.zeros(n)
    actual_exposure = np.zeros(n)
    turn_notional = np.zeros(n)
    net[0] = gross[0] = initial_level
    qty[0] = initial_level * weights.iloc[0] / asset[0]
    for i in range(1, n):
        if net[i-1] <= 0:
            continue
        actual_exposure[i] = qty[i-1] * asset[i-1] / net[i-1]
        gross[i] = net[i-1] + qty[i-1] * (asset[i] - asset[i-1])
        if gross[i] <= 0:
            net[i] = 0.0
            continue
        qty[i] = gross[i] * weights.iloc[i] / asset[i]
        turn_notional[i] = abs(qty[i]-qty[i-1]) * asset[i]
        rc[i] = turn_notional[i] * cfg.transaction
        servicing_notional = qty[i-1] * asset[i-1] if servicing_base == "notional" else net[i-1]
        sc[i] = servicing_notional * dcf.iloc[i] * cfg.servicing
        dc[i] = net[i-1] * dcf.iloc[i] * cfg.deduction
        net[i] = max(0.0, gross[i]-rc[i]-sc[i]-dc[i])
        if net[i] == 0:
            qty[i] = 0
    frame = pd.DataFrame({"asset_level": asset, "gross_level": gross, "net_level": net,
                          "quantity": qty, "transaction_cost": rc, "servicing_cost": sc,
                          "index_deduction": dc, "turnover_notional": turn_notional,
                          "weight_close": weights, "actual_nav_exposure": actual_exposure}, index=weights.index)
    frame["return"] = frame.net_level.pct_change(fill_method=None).fillna(0)
    frame["explicit_cost"] = frame.transaction_cost + frame.servicing_cost + frame.index_deduction
    return frame


def with_asset_return(market: pd.DataFrame, asset_return: pd.Series,
                      cash_return: pd.Series | None = None) -> pd.DataFrame:
    """Copy of `market` with an alternative ER asset, for sensitivities only.

    Signals and volatility read `asset_er`, so it is rebuilt from the new returns;
    the baseline frame is never mutated.
    """
    alt = market.copy()
    if cash_return is not None:
        alt["cash_return"] = cash_return
    alt["asset_er_return"] = asset_return
    alt["asset_er"] = wealth(alt.asset_er_return)
    if (alt["asset_er"] <= 0).any():
        raise ValueError("Alternative excess-return asset reached zero")
    return alt


def simplified_model(market: pd.DataFrame, feat: pd.DataFrame, cfg: Config,
                     rate="tbill_cash_return", costs=True) -> pd.DataFrame:
    """Requested approximation, deliberately separated from official rules.

    Close-only asset return, max 20/60-day SPY vol and 10% target / lagged vol.
    Calendar flags are diagnostic; no unrequested event multiplier is invented.
    Servicing is flat NAV and transaction cost is proportional weight turnover.
    Exposure is zero until BOTH volatility windows exist: the requested rule is the
    maximum of the two, which is undefined while the 60-day window is incomplete.
    """
    vol = feat[["spy_vol20", "spy_vol60"]].max(axis=1, skipna=False)
    # w is the CLOSE decision. held[t] = w[t-1] therefore implements precisely
    # return-day Weight_t = min(cap, target / Realized_Vol_(t-1)).
    w = (cfg.vol_target / vol.replace(0, np.nan)).clip(0, cfg.equity_cap).fillna(0)
    w.loc[vol == 0] = cfg.equity_cap
    held = w.shift(1).fillna(0)
    turnover = w.diff().abs().fillna(0)
    dcf = market.dcf.fillna(0)
    deduction = cfg.deduction * dcf if costs else dcf * 0
    service = cfg.servicing * dcf if costs else dcf * 0
    transaction = cfg.transaction * turnover if costs else turnover * 0
    ret = held * (market.price_return.fillna(0)-market[rate].fillna(0)) - deduction-service-transaction
    level = wealth(ret)
    return pd.DataFrame({"weight_close": w, "weight_held": held, "return": ret,
                         "net_level": level, "deduction_return": deduction,
                         "servicing_return": service, "transaction_return": transaction}, index=market.index)


def wealth(returns: pd.Series) -> pd.Series:
    """Daily compounded wealth with an absorbing zero after a >=100% loss."""
    factors = (1 + returns.fillna(0)).clip(lower=0)
    return 100.0 * factors.cumprod()


def normalize_path(level: pd.Series, anchor: pd.Timestamp) -> pd.Series:
    if level.loc[anchor] <= 0:
        raise ValueError("Cannot rebase an exhausted path")
    return 100 * level.loc[anchor:] / level.loc[anchor]


def performance(level: pd.Series, cash: pd.Series, already_excess: bool) -> dict:
    returns = level.pct_change(fill_method=None).iloc[1:]
    days = (level.index[-1] - level.index[0]).days
    years = days / 365.25
    if years <= 0 or len(returns) < 2:
        raise ValueError("Performance period too short")
    vol = returns.std(ddof=1) * np.sqrt(252)
    excess = returns if already_excess else returns - cash.reindex(returns.index)
    denominator = excess.std(ddof=1) * np.sqrt(252)
    sharpe = excess.mean() * 252 / denominator if denominator > 0 else np.nan
    return {"Annualized Return": (level.iloc[-1]/level.iloc[0])**(1/years)-1,
            "Annualized Volatility": vol,
            "Sharpe Ratio": sharpe,
            "Maximum Drawdown": (level/level.cummax()-1).min(),
            "Terminal Wealth per 100": level.iloc[-1]}


def summary_metrics(paths: pd.DataFrame, cash: pd.Series, nofee: pd.Series) -> pd.DataFrame:
    rows = {}
    for name in paths:
        rows[name] = performance(paths[name], cash, already_excess=name == INDEX_NAME)
        # Relative terminal-wealth loss vs SAME policy with all explicit fees off.
        # A separate ledger reports nominal costs; these are different quantities.
        rows[name]["Cumulative Fee Drag"] = (1-paths[name].iloc[-1]/nofee.iloc[-1]) if name == INDEX_NAME else 0.0
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("Strategy")


def run_counterfactuals(market: pd.DataFrame, feat: pd.DataFrame, cfg: Config,
                        anchor: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Telescoping terminal wealth attribution, holding baseline weights fixed.

    Changing funding/dividends here never feeds back into signal/vol weights.
    That is intentional: the estimand is direct economic impact. A separate
    sensitivity analysis recomputes weights to capture policy feedback.
    """
    zero = replace(cfg, deduction=0, servicing=0, transaction=0)
    # Reset every portfolio to the SAME NAV and holdings on the comparison
    # anchor. Normalizing separately simulated pre-anchor fee histories would
    # retain tiny differences in gross/net exposure from costs before the period.
    market, feat = market.loc[anchor:], feat.loc[anchor:]
    w, dcf = feat.weight_close, market.dcf
    specs = [
        ("SPY adjusted total return", market.adj_return, pd.Series(1.0, index=market.index), zero),
        ("Explicit cash-dividend total return", market.cash_total_return, pd.Series(1.0, index=market.index), zero),
        ("Signal allocation", market.cash_total_return, feat.allocation.clip(0, cfg.equity_cap), zero),
        ("Volatility control + caps", market.cash_total_return, w, zero),
        ("Fed Funds excess return", market.asset_er_return, w, zero),
        ("Servicing cost", market.asset_er_return, w, replace(zero, servicing=cfg.servicing)),
        ("Rebalancing cost", market.asset_er_return, w, replace(zero, servicing=cfg.servicing, transaction=cfg.transaction)),
        ("Index deduction / net reconstruction", market.asset_er_return, w, cfg),
    ]
    curves = {}
    for label, r, weight, charges in specs:
        curves[label] = normalize_path(accounting(r, weight, dcf, charges).net_level, anchor)
    levels = pd.DataFrame(curves)
    terminal = levels.iloc[-1]
    attribution = pd.DataFrame({"Terminal Wealth per 100": terminal,
                                "Incremental Impact per 100": terminal.diff().fillna(0),
                                "Incremental Relative Impact": terminal.pct_change(fill_method=None).fillna(0)})
    attribution.index.name = "Step (order dependent)"
    dividend_off = normalize_path(accounting(market.price_return-market.cash_return, w, dcf, cfg).net_level, anchor)
    financing_off = normalize_path(accounting(market.cash_total_return, w, dcf, cfg).net_level, anchor)
    nofee = levels["Fed Funds excess return"]
    net = levels.iloc[:, -1]
    direct = {
        "explicit_fee_terminal_loss_per_100": float(nofee.iloc[-1]-net.iloc[-1]),
        "explicit_fee_relative_wealth_loss": float(1-net.iloc[-1]/nofee.iloc[-1]),
        "dividend_forfeit_terminal_loss_per_100": float(net.iloc[-1]-dividend_off.iloc[-1]),
        "financing_terminal_loss_per_100": float(financing_off.iloc[-1]-net.iloc[-1]),
        "attribution_reconciliation_error": float(attribution["Incremental Impact per 100"].sum()-(net.iloc[-1]-terminal.iloc[0])),
    }
    levels["Hypothetical dividend forfeiture"] = dividend_off
    return attribution, levels, direct


def shapley_costs(market, feat, cfg, anchor) -> pd.DataFrame:
    """Order-independent attribution of the three explicit cost components.

    Eight coalition valuations capture compounding and gross/net interactions.
    Shapley losses sum EXACTLY to the no-fee versus full-fee terminal difference.
    Dividends/financing are measured separately and are not mislabeled fees.
    """
    market, feat = market.loc[anchor:], feat.loc[anchor:]
    names = ("Index deduction", "Servicing", "Rebalancing")
    values = {}
    for bits in itertools.product((0, 1), repeat=3):
        c = replace(cfg, deduction=cfg.deduction*bits[0], servicing=cfg.servicing*bits[1],
                    transaction=cfg.transaction*bits[2])
        level = accounting(market.asset_er_return, feat.weight_close, market.dcf, c).net_level
        values[bits] = normalize_path(level, anchor).iloc[-1]
    losses = np.zeros(3)
    for order in itertools.permutations(range(3)):
        state = [0, 0, 0]
        for k in order:
            before = values[tuple(state)]
            state[k] = 1
            losses[k] += (before-values[tuple(state)]) / 6
    result = pd.DataFrame({"Terminal Wealth Loss per 100": losses}, index=names)
    result.index.name = "Cost Component"
    if not np.isclose(losses.sum(), values[(0, 0, 0)]-values[(1, 1, 1)], atol=1e-9):
        raise AssertionError("Shapley reconciliation failed")
    return result


def fee_drag_measures(ledger: pd.DataFrame, nofee: pd.Series, cost_shapley: pd.DataFrame,
                      cash: pd.Series) -> pd.DataFrame:
    """Four fee measures with different denominators, kept deliberately apart.

    1. Sum of nominal daily charges: cash points deducted, ignoring compounding.
    2. Compounded terminal wealth lost: no-fee minus net terminal wealth (Shapley split).
    3. Relative terminal-wealth loss: (2) as a share of no-fee terminal wealth.
    4. CAGR reduction: no-fee CAGR minus net CAGR, in percentage points.
    """
    scale = 100 / ledger.net_level.iloc[0]
    sample = ledger.iloc[1:]
    nominal = {"Index deduction": sample.index_deduction.sum() * scale,
               "Servicing": sample.servicing_cost.sum() * scale,
               "Rebalancing": sample.transaction_cost.sum() * scale}
    shapley = cost_shapley.iloc[:, 0].to_dict()
    net_cagr = performance(ledger.net_level * scale, cash, True)["Annualized Return"]
    nofee_cagr = performance(nofee, cash, True)["Annualized Return"]
    years = (ledger.index[-1] - ledger.index[0]).days / 365.25
    rows = [
        ("Sum of nominal daily charges", "per initial 100", sum(nominal.values()), nominal),
        ("Compounded terminal wealth lost (Shapley)", "per initial 100", sum(shapley.values()), shapley),
        ("Relative terminal-wealth loss", "% of no-fee terminal wealth",
         100 * (1 - ledger.net_level.iloc[-1] * scale / nofee.iloc[-1]), {}),
        ("CAGR reduction", "percentage points", 100 * (nofee_cagr - net_cagr), {}),
        ("Mean annual nominal charge rate", "% of prior net NAV per year",
         100 * (sample.explicit_cost / ledger.net_level.shift(1).iloc[1:]).sum() / years,
         {k: 100 * (sample[c] / ledger.net_level.shift(1).iloc[1:]).sum() / years for k, c in
          (("Index deduction", "index_deduction"), ("Servicing", "servicing_cost"), ("Rebalancing", "transaction_cost"))}),
    ]
    table = pd.DataFrame([{"Measure": m, "Unit": u, "Total": t,
                           "Index deduction": parts.get("Index deduction", np.nan),
                           "Servicing": parts.get("Servicing", np.nan),
                           "Rebalancing": parts.get("Rebalancing", np.nan)} for m, u, t, parts in rows])
    return table.set_index("Measure")


def dividend_convention_audit(market: pd.DataFrame, anchor: pd.Timestamp) -> tuple[pd.DataFrame, dict]:
    """Isolate why explicit cash-dividend TR differs from Yahoo Adj Close TR.

    Methodology §11 credits the cash dividend at the ex-date close:
        (Close_t + D_t) / Close_(t-1) - 1.
    A multiplicative back-adjustment by (1 - D_t / Close_(t-1)) instead implies
        Close_t / (Close_(t-1) - D_t) - 1,
    i.e. the dividend is reinvested at the prior close. The gap equals
        D (Close_t - Close_(t-1) + D) / (Close_(t-1) (Close_(t-1) - D)),
    positive on up ex-dates and negative on down ex-dates. It is a reinvestment
    convention difference, not a fee, and is reported rather than absorbed.
    """
    prior = market.Close.shift(1)
    identity = market.Close / (prior - market.Dividends) - 1
    ex = market.Dividends > 0
    table = pd.DataFrame({"Dividend": market.Dividends, "Prior Close": prior, "Close": market.Close,
                          "Cash Dividend TR": market.cash_total_return,
                          "Vendor Adj Close TR": market.adj_return,
                          "Back-adjustment Identity TR": identity}).loc[ex]
    table["Cash minus Vendor (bp)"] = 1e4 * (table["Cash Dividend TR"] - table["Vendor Adj Close TR"])
    table["Vendor minus Identity (bp)"] = 1e4 * (table["Vendor Adj Close TR"] - table["Back-adjustment Identity TR"])
    table.index.name = "Ex-date"
    window = market.loc[anchor:].iloc[1:]
    cash_w = 100 * (1 + window.cash_total_return).prod()
    vendor_w = 100 * (1 + window.adj_return).prod()
    identity_w = 100 * (1 + identity.loc[window.index].where(ex.loc[window.index], window.price_return)).prod()
    gap = cash_w - vendor_w
    summary = {"dividend_events_total": int(ex.sum()),
               "dividend_events_in_comparison": int(ex.loc[window.index].sum()),
               "cash_dividend_tr_wealth": float(cash_w), "vendor_adj_close_tr_wealth": float(vendor_w),
               "identity_tr_wealth": float(identity_w),
               "relative_difference": float(cash_w / vendor_w - 1),
               "share_of_gap_explained_by_reinvestment_convention": float((cash_w - identity_w) / gap) if gap else np.nan,
               "max_abs_vendor_minus_identity_bp": float(table["Vendor minus Identity (bp)"].abs().max()),
               "max_abs_daily_cash_minus_vendor_bp": float(table["Cash minus Vendor (bp)"].abs().max()),
               "max_abs_daily_difference_date": str(table["Cash minus Vendor (bp)"].abs().idxmax().date())}
    return table, summary


def stress_analysis(market, events, cfg, anchor) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Historical stress windows and causal, seeded synthetic shock scenarios."""
    feat = controlled_weights(market, generate_signals(market, events, cfg), cfg)
    ledger = accounting(market.asset_er_return, feat.weight_close, market.dcf, cfg)
    windows = {"COVID selloff": ("2020-02-19", "2020-03-23"),
               "COVID rebound": ("2020-03-23", "2020-08-31"),
               "2022 tightening": ("2021-12-31", "2022-12-30"),
               "April 2025 selloff": ("2025-04-02", "2025-04-08")}
    historical = []
    for label, (start, end) in windows.items():
        sample = market.loc[start:end]
        if len(sample) < 2 or sample.index[-1] < pd.Timestamp(end) - pd.Timedelta(days=4):
            continue
        dates = sample.index
        level = ledger.net_level.loc[dates]
        tr = wealth(market.adj_return).loc[dates]
        historical.append({"Scenario": label, "Start": str(dates[0].date()), "End": str(dates[-1].date()),
                           "Index Return": level.iloc[-1]/level.iloc[0]-1,
                           "SPY TR Return": tr.iloc[-1]/tr.iloc[0]-1,
                           "Index Maximum Drawdown": (level/level.cummax()-1).min(),
                           "Mean Held Exposure": feat.weight_held.loc[dates[1:]].mean(),
                           "Minimum Held Exposure": feat.weight_held.loc[dates[1:]].min()})
    # Synthetic returns are labeled separately and never substitute real data.
    # Seeded quiet history permits >1x exposure before an unanticipated gap. The
    # scenarios run on real 2019 session dates, so calendar signals (ToM, OpEx)
    # still apply; FOMC events are removed. The shock date is reported.
    rng = np.random.default_rng(17)
    quiet = rng.normal(0.00015, 0.003, 120)
    shocks = {"Overnight -20% gap": np.r_[-0.20, np.zeros(39)],
              "Crash then rapid rebound": np.r_[np.full(10, -0.025), np.full(10, 0.028), np.zeros(20)],
              "Low-volatility chop": np.tile([0.004, -0.004], 20)}
    synthetic, paths = [], []
    for label, future in shocks.items():
        dates = exchange_sessions("2019-01-02", "2020-01-31")[:len(quiet)+len(future)+1]
        r = np.r_[0.0, quiet, future]
        px = 100*np.cumprod(1+r)
        m = pd.DataFrame({"Close": px, "Adj Close": px, "Dividends": 0.0, "Stock Splits": 0.0}, index=dates)
        m["dcf"] = m.index.to_series().diff().dt.days.fillna(0)/360
        m["price_return"] = m["cash_total_return"] = m["adj_return"] = r
        m["cash_return"] = 0.04*m.dcf
        m["asset_er_return"] = r-m.cash_return
        m["asset_er"] = wealth(m.asset_er_return)
        f = controlled_weights(m, generate_signals(m, events.iloc[:0], cfg), cfg)
        l = accounting(m.asset_er_return, f.weight_close, m.dcf, cfg)
        begin = len(quiet)
        shock = begin + 1
        segment = l.net_level.iloc[begin:]
        benchmark = pd.Series(px, index=dates).iloc[begin:]
        below = np.flatnonzero(f.weight_close.iloc[shock:].to_numpy() < 0.5)
        synthetic.append({"Scenario": label, "Shock Date (synthetic, 2019 calendar)": str(dates[shock].date()),
                          "Index Return": segment.iloc[-1]/segment.iloc[0]-1,
                          "SPY Price Return": benchmark.iloc[-1]/benchmark.iloc[0]-1,
                          "Maximum Drawdown": (segment/segment.cummax()-1).min(),
                          "Exposure Before Shock": f.weight_close.iloc[begin],
                          "Exposure After Shock Close": f.weight_close.iloc[shock],
                          "First Shock Index Return": l["return"].iloc[shock],
                          "Minimum Subsequent Exposure": f.weight_close.iloc[shock:].min(),
                          "Sessions After Shock Until Exposure Below 50%": int(below[0]) if len(below) else np.nan})
        steps = np.arange(len(dates)) - shock
        paths.append(pd.DataFrame({"Scenario": label, "Session Relative To Shock": steps, "Date": dates,
                                   "Underlying Return": r, "Underlying Level": px / px[begin] * 100,
                                   "Index Level": l.net_level.to_numpy() / l.net_level.iloc[begin] * 100,
                                   "Index Return": l["return"].to_numpy(),
                                   "Held Exposure": f.weight_held.to_numpy(), "Close Exposure": f.weight_close.to_numpy(),
                                   "Algorithmic Vol": f.algorithmic_vol.to_numpy(),
                                   "Allocation": f.allocation.to_numpy()}).iloc[begin - 5:])
    return (pd.DataFrame(historical).set_index("Scenario"), pd.DataFrame(synthetic).set_index("Scenario"),
            pd.concat(paths, ignore_index=True))


def sensitivity_analysis(market, events, cfg, anchor) -> pd.DataFrame:
    """Policy feedback tests recompute signals, vol and exposure from scratch."""
    cases = {"Baseline": (cfg, market, events),
             "Vol target 8%": (replace(cfg, vol_target=0.08), market, events),
             "Vol target 12%": (replace(cfg, vol_target=0.12), market, events),
             "No leverage above 100%": (replace(cfg, equity_cap=1.0), market, events),
             "Fast EWMA only": (replace(cfg, lambda_long=cfg.lambda_short, seed_long=cfg.seed_short), market, events),
             "Slow EWMA only": (replace(cfg, lambda_short=cfg.lambda_long, seed_short=cfg.seed_long), market, events),
             "No calendar / MR signals": (replace(cfg, signal_leverages=(0.,)*5), market, events)}
    bumped_cash = market.cash_return + 0.02*market.dcf
    cases["Funding +200 bp (policy recomputed)"] = (
        cfg, with_asset_return(market, market.cash_total_return - bumped_cash, bumped_cash), events)
    # A final-calendar archive cannot establish precisely when March 18's
    # cancellation became known. Recompute the policy with that planned meeting
    # included, without asserting this is the official historical treatment.
    alternate = events.copy()
    alternate.loc[(alternate.event_type == "cancelled") &
                  (alternate.date == pd.Timestamp("2020-03-18")), "event_type"] = "scheduled"
    cases["Include planned 18 Mar 2020 FOMC"] = (cfg, market, alternate)
    # Rate proxy sensitivity is distinct from a funding spread stress.
    cases["DTB3 funding proxy (policy recomputed)"] = (
        cfg, with_asset_return(market, market.cash_total_return - market.tbill_cash_return,
                               market.tbill_cash_return), events)
    rows = []
    for name, (c, m, e) in cases.items():
        f = controlled_weights(m, generate_signals(m, e, c), c)
        m, f = m.loc[anchor:], f.loc[anchor:]
        l = accounting(m.asset_er_return, f.weight_close, m.dcf, c)
        row = performance(normalize_path(l.net_level, anchor), market.cash_return, True)
        row.update({"Scenario": name, "Mean Held Exposure": f.weight_held.iloc[1:].mean()})
        rows.append(row)
    table = pd.DataFrame(rows).set_index("Scenario")
    table["Terminal Wealth Change vs Baseline"] = (table["Terminal Wealth per 100"]
                                                   - table.loc["Baseline", "Terminal Wealth per 100"])
    return table


def anchor_metrics(feat: pd.DataFrame, ledger: pd.DataFrame) -> dict:
    """Reconstruction values comparable with PUBLISHED_ANCHORS (percent units)."""
    out = {"seed_vol_094": np.nan, "seed_vol_097": np.nan}
    seed = pd.Timestamp(ANCHOR_SEED_DATE)
    if seed in feat.index:
        out["seed_vol_094"] = 100 * feat.loc[seed, "ewma_short"]
        out["seed_vol_097"] = 100 * feat.loc[seed, "ewma_long"]
    level, end = ledger.net_level, pd.Timestamp(ANCHOR_RETURN_END)
    for anchor in PUBLISHED_ANCHORS:
        if anchor.years is None:
            continue
        start = pd.Timestamp(anchor.start)
        if start in level.index and end in level.index:
            total = level[end] / level[start] - 1
            out[anchor.key] = 100 * ((1 + total) ** (1 / anchor.years) - 1)
        else:
            out[anchor.key] = np.nan
    rate = (ledger.transaction_cost + ledger.servicing_cost) / level.shift(1)
    rolling = rate.rolling(252).sum().loc[:end].dropna()
    out["cost_rate_avg"] = 100 * rolling.mean() if len(rolling) else np.nan
    out["cost_rate_max"] = 100 * rolling.max() if len(rolling) else np.nan
    out["cost_window"] = (f"{rolling.index[0].date()} to {rolling.index[-1].date()} (window ends)"
                          if len(rolling) else "not computable")
    return out


def _comparison_value(anchor: PublishedAnchor, value: float) -> float:
    """Annualized returns are compared as cumulative returns so that 1Y, 3Y and
    5Y errors share one scale; other anchors are compared as printed."""
    if anchor.years:
        return 100 * ((1 + value / 100) ** anchor.years - 1)
    return value


def published_anchor_checks(market: pd.DataFrame, events: pd.DataFrame,
                            cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compare the reconstruction, and plausible misreadings, with GS figures.

    The anchors are GS-printed backtest and initialization numbers built on GS
    data. Every convention is run through the full policy from the same
    extended history. A convention is flagged as contradicted by an anchor when
    its error exceeds three times the larger of (a) the baseline's own largest
    unexplained error in that anchor family and (b) the printed rounding
    half-unit. The rule is a declared heuristic, not a statistical test, and is
    deliberately loose relative to the baseline's residual data noise.
    """
    start = market.index[min(ANCHOR_LEDGER_WARMUP, len(market) - 1)]
    variants = {}
    for name, opts in FINGERPRINT_CONVENTIONS:
        kind = opts.get("asset")
        if kind is None:
            m = market
        elif kind == "close_only":
            m = with_asset_return(market, market.price_return - market.cash_return)
        elif kind == "adj_close":
            m = with_asset_return(market, market.adj_return - market.cash_return)
        elif kind == "dtb3":
            m = with_asset_return(market, market.cash_total_return - market.tbill_cash_return, market.tbill_cash_return)
        elif kind == "rate_same_day":
            same_day = market.dff_prior.shift(-1).ffill() * market.dcf
            m = with_asset_return(market, market.cash_total_return - same_day, same_day)
        elif kind == "act365":
            act365 = market.cash_return * cfg.day_basis / 365.0
            m = with_asset_return(market, market.cash_total_return - act365, act365)
        else:
            raise ValueError(kind)
        signals = generate_signals(m, events, cfg, tom_mr_reference_offset=opts.get("tom_mr_reference_offset", 5))
        feat = controlled_weights(m, signals, cfg, vol_lag=opts.get("vol_lag", 1),
                                  vol_on_allocated=opts.get("vol_on_allocated", True))
        ledger = accounting(m.asset_er_return.loc[start:], feat.weight_close.loc[start:], m.dcf.loc[start:],
                            cfg, servicing_base=opts.get("servicing_base", "notional"))
        variants[name] = anchor_metrics(feat, ledger)
    baseline = variants["Methodology baseline"]
    errors = {name: {a.key: _comparison_value(a, v[a.key]) - _comparison_value(a, a.published)
                     for a in PUBLISHED_ANCHORS} for name, v in variants.items()}
    tolerance = {}
    for family in dict.fromkeys(a.family for a in PUBLISHED_ANCHORS):
        members = [a for a in PUBLISHED_ANCHORS if a.family == family]
        base_errs = [abs(errors["Methodology baseline"][a.key]) for a in members]
        half_unit = max(0.5 * 10 ** -a.decimals * (a.years or 1) for a in members)
        # A sample that does not cover an anchor family cannot judge it.
        tolerance[family] = 3 * max(np.nanmax(base_errs), half_unit) if np.isfinite(base_errs).any() else np.nan
    anchors = pd.DataFrame([{
        "Anchor": a.label, "Family": a.family, "Published (%)": a.published,
        "Reconstruction (%)": baseline[a.key], "Difference (pp)": baseline[a.key] - a.published,
        "Comparison Basis": "cumulative return" if a.years else "as printed",
        "Difference On Comparison Basis (pp)": errors["Methodology baseline"][a.key],
        "Fingerprint Tolerance (pp)": tolerance[a.family],
        "Reconstruction Sample": (f"policy from {market.index[0].date()}, ledger from {start.date()}; "
                                  + (f"windows {baseline['cost_window']}" if a.family == "Cost rate" else
                                     f"{a.start} to {ANCHOR_RETURN_END}" if a.years else f"EWMA on {ANCHOR_SEED_DATE}")),
        "Source": a.source} for a in PUBLISHED_ANCHORS]).set_index("Anchor")
    rows = []
    for name, values in variants.items():
        flagged = [a.label for a in PUBLISHED_ANCHORS
                   if np.isfinite(errors[name][a.key]) and np.isfinite(tolerance[a.family])
                   and abs(errors[name][a.key]) > tolerance[a.family]]
        computable = any(np.isfinite(errors[name][a.key]) for a in PUBLISHED_ANCHORS)
        row = {"Convention": name, **{a.label: values[a.key] for a in PUBLISHED_ANCHORS}}
        row["Anchors Contradicting"] = "; ".join(flagged) if flagged else "none"
        row["Verdict"] = ("methodology" if name == "Methodology baseline" else
                          "contradicted by published anchors" if flagged else
                          "not distinguishable at anchor precision" if computable else
                          "anchors not covered by this sample")
        rows.append(row)
    return anchors, pd.DataFrame(rows).set_index("Convention")


def round_half_up(values: pd.Series, decimals: int = 2) -> pd.Series:
    """Published GSEQTMX levels use 2 decimals with 0.005 rounded upwards."""
    quantum = Decimal(1).scaleb(-decimals)
    return values.map(lambda x: float(Decimal(repr(float(x))).quantize(quantum, rounding=ROUND_HALF_UP)))


def audit_official(reconstructed: pd.Series, path: Path, launch: str, output: Path,
                   compare_levels: bool = False, filename: str = "official_reconciliation.csv") -> dict:
    """Optional external reconciliation; no official levels are fabricated.

    Supply CSV date,level. Both paths are rebased on the first overlapping date.
    Residuals measure discrepancies, not evidence of certified replication.
    With `compare_levels`, the unrebased reconstruction is also rounded like the
    published index (2 dp, half up) and compared level-for-level; this is only
    meaningful when both series share the June 1, 2021 base of 100.
    """
    raw = pd.read_csv(path)
    if not {"date", "level"}.issubset(raw.columns):
        raise ValueError("Official CSV requires date,level")
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw["level"] = pd.to_numeric(raw["level"], errors="coerce")
    official = raw.set_index("date")["level"].sort_index()
    if (official.index.has_duplicates or official.index.isna().any()
            or not np.isfinite(official.to_numpy()).all() or (official <= 0).any()):
        raise ValueError("Official levels must be unique, finite and positive")
    joined = pd.concat([reconstructed.rename("Research"), official.rename("Official")], axis=1).dropna()
    if len(joined) < 20:
        raise ValueError("Official audit needs at least 20 common dates")
    normalized = joined / joined.iloc[0] * 100
    residual = normalized.pct_change(fill_method=None).dropna().diff(axis=1).iloc[:, -1] * -1
    normalized["Level Difference"] = normalized.Research-normalized.Official
    result = {"status": "compared, not certified", "common_dates": len(joined),
              "overlap_start": str(joined.index[0].date()), "overlap_end": str(joined.index[-1].date()),
              "official_dates_not_matched": int(len(official.index.difference(joined.index))),
              "annualized_tracking_error": float(residual.std()*np.sqrt(252)),
              "mean_daily_return_bias": float(residual.mean()),
              "max_absolute_rebased_level_error": float(normalized["Level Difference"].abs().max()),
              "official_file_sha256": sha256(path)}
    if compare_levels:
        normalized["Research Unrebased Rounded"] = round_half_up(joined.Research)
        normalized["Official Unrebased"] = joined.Official
        level_gap = normalized["Research Unrebased Rounded"] - joined.Official
        result["unrebased_max_abs_level_error"] = float(level_gap.abs().max())
        result["unrebased_exact_2dp_match_fraction"] = float((level_gap.abs() < 5e-9).mean())
    normalized.to_csv(output / filename, index_label="date")
    # Preserve the distinction between official backtest and live calculation.
    for regime, mask in [("pre_launch", joined.index < pd.Timestamp(launch)),
                         ("live", joined.index >= pd.Timestamp(launch))]:
        period = joined.loc[mask]
        if len(period) >= 20:
            returns = period.pct_change(fill_method=None).dropna()
            diff = returns.Research - returns.Official
            result[regime+"_tracking_error"] = float(diff.std()*np.sqrt(252))
            result[regime+"_mean_daily_return_bias"] = float(diff.mean())
            result[regime+"_max_absolute_rebased_level_error"] = float(normalized.loc[mask, "Level Difference"].abs().max())
    return result


# ---------------------------------------------------------------- charts ----
# Validated categorical slots (dataviz reference palette, light surface):
# blue = TimeX, orange = SPY price series, aqua = SPY total return. Aqua is
# below 3:1 contrast, so every line is also direct-labeled in ink.
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, AQUA, RED, BLUE_LIGHT = "#2a78d6", "#eb6834", "#1baf7a", "#e34948", "#86b6ef"
SERIES_COLORS = {INDEX_NAME: BLUE, PRICE_NAME: ORANGE, TOTAL_NAME: AQUA}
SVG_METADATA = {"Date": None}


def _chart_style():
    sns.set_theme(style="white", context="notebook", font="DejaVu Sans")
    plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
                         "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.labelcolor": INK2,
                         "axes.titlecolor": INK, "axes.titlesize": 12.5, "axes.titleweight": "bold",
                         "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.linestyle": "-",
                         "axes.spines.top": False, "axes.spines.right": False,
                         "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK2,
                         "ytick.labelcolor": INK2, "legend.frameon": False, "legend.labelcolor": INK,
                         "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
                         "svg.hashsalt": "gseqtmx-research"})


def _save(fig, output: Path, stem: str, dpi: int = 180):
    fig.savefig(output / f"{stem}.png", dpi=dpi)
    fig.savefig(output / f"{stem}.svg", metadata=SVG_METADATA)
    plt.close(fig)


def _event_markers(ax, start, end, label: bool):
    for date, text in ((pd.Timestamp("2021-06-01"), "Index base date"), (pd.Timestamp("2023-07-28"), "Launch")):
        if start <= date <= end:
            ax.axvline(date, color=AXIS, linewidth=0.9, zorder=1)
            if label:
                ax.text(date, 1.0, f" {text}", transform=ax.get_xaxis_transform(), va="top", ha="left",
                        fontsize=8.5, color=MUTED)


def make_charts(paths, feat, attribution, cost_shapley, fee_measures, anchors, fingerprint,
                synthetic_paths, output):
    """Required two-panel dashboard plus attribution, anchor and gap-risk figures."""
    _chart_style()
    start, end = paths.index[0], paths.index[-1]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 10.5), sharex=True,
                                   gridspec_kw={"height_ratios": [1.3, 1]}, layout="constrained")
    fig.suptitle("Goldman Sachs Equity TimeX methodology: independent research reconstruction",
                 x=0.01, ha="left", fontsize=17, fontweight="bold", color=INK)
    for name in paths:
        ax1.plot(paths.index, paths[name], color=SERIES_COLORS[name], label=name, linewidth=1.7)
        ax1.plot(end, paths[name].iloc[-1], "o", color=SERIES_COLORS[name], markersize=6,
                 markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=4)
        ax1.annotate(f"{paths[name].iloc[-1]:,.1f}", (end, paths[name].iloc[-1]), xytext=(8, 0),
                     textcoords="offset points", va="center", fontsize=10, color=INK, fontweight="bold")
    ax1.set_yscale("log")
    ax1.yaxis.set_major_locator(LogLocator(base=10, subs=[1, 1.5, 2, 3, 4, 5, 7]))
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value:,.0f}"))
    ax1.yaxis.set_minor_formatter(NullFormatter())
    ax1.set_xlim(start, end + (end - start) * 0.045)
    ax1.set_ylabel("Growth of 100, log scale")
    ax1.set_title(f"A  Growth of 100 from {start:%d %b %Y} to {end:%d %b %Y}, identical dates and anchor",
                  loc="left", pad=10)
    ax1.legend(loc="upper left", fontsize=10)
    _event_markers(ax1, start, end, label=True)
    f = feat.reindex(paths.index)
    held = f.weight_held * 100
    ax2.plot(f.index, held, color=BLUE_LIGHT, linewidth=0.6, label="Held equity exposure, daily")
    ax2.plot(f.index, held.rolling(21, min_periods=5).mean(), color=BLUE, linewidth=1.8,
             label="Held equity exposure, 21-session mean")
    ax2.axhline(150, color=MUTED, linewidth=0.8)
    ax2.text(start, 151.5, "150% cap", fontsize=8.5, color=MUTED, va="bottom")
    ax2.set_ylim(0, 165)
    ax2.set_yticks([0, 25, 50, 75, 100, 125, 150])
    ax2.set_ylabel("Held equity exposure, % of gross NAV")
    ax2.set_title("B  Exposure versus market volatility (two scales: exposure left, SPY volatility right)",
                  loc="left", pad=30)
    right = ax2.twinx()
    right.plot(f.index, f.spy_vol20 * 100, color=ORANGE, linewidth=1.1, label="SPY 20-day realized volatility (right)")
    # Headroom keeps the volatility peak visibly below the 150% exposure cap line,
    # so the two scales do not suggest an alignment that is not in the data.
    right.set_ylim(0, max(40, np.nanmax(f.spy_vol20 * 100) * 1.35))
    right.set_ylabel("SPY 20-day realized volatility, % annualized")
    right.grid(False)
    right.spines["right"].set_visible(True)
    handles = ax2.get_legend_handles_labels()[0] + right.get_legend_handles_labels()[0]
    ax2.legend(handles, [h.get_label() for h in handles], loc="lower left", bbox_to_anchor=(0, 1.0),
               ncol=3, fontsize=9.5, borderaxespad=0.3)
    _event_markers(ax2, start, end, label=False)
    ax2.set_xlim(start, end + (end - start) * 0.045)
    fig.supxlabel("Data: Yahoo SPY (Close, Adj Close, dividends) and FRED DFF. Methodology dated 28 Jul 2023. "
                  "Research reconstruction, not official GSEQTMX levels.", fontsize=9.5, color=INK2)
    _save(fig, output, "timex_dashboard", dpi=190)

    # Attribution: order-dependent bridge as a waterfall, plus nominal vs compounded costs.
    fig, axes = plt.subplots(1, 2, figsize=(16, 6.8), layout="constrained", gridspec_kw={"width_ratios": [1.45, 1]})
    terminal = attribution["Terminal Wealth per 100"]
    steps = list(terminal.index)
    labels = [steps[0]] + [f"{s}" for s in steps[1:]] + ["TimeX reconstruction (net)"]
    previous = np.r_[0.0, terminal.to_numpy()[:-1]]
    ax = axes[0]
    for k, label in enumerate(labels):
        if k == 0 or k == len(labels) - 1:
            value = terminal.iloc[0] if k == 0 else terminal.iloc[-1]
            ax.barh(k, value, color=INK2 if k == 0 else BLUE, height=0.42)
            ax.text(value + 6, k, f"{value:,.1f}", va="center", fontsize=9.5, color=INK, fontweight="bold")
        else:
            lo, hi = sorted((previous[k], terminal.iloc[k]))
            delta = terminal.iloc[k] - previous[k]
            ax.barh(k, max(hi - lo, 0.4), left=lo, color=BLUE if delta >= 0 else RED, height=0.42)
            ax.text(hi + 6, k, f"{delta:+,.1f}", va="center", fontsize=9.5, color=INK)
    ax.set_yticks(range(len(labels)), labels, fontsize=9.5)
    ax.invert_yaxis()
    ax.set_xlim(0, max(terminal.max(), previous.max()) * 1.13)
    ax.set_xlabel("Terminal wealth per initial 100")
    ax.set_title("Sequential terminal-wealth bridge (order dependent)", loc="left", pad=26)
    ax.grid(axis="y", visible=False)
    ax.legend(handles=[Patch(color=INK2, label="Benchmark total"), Patch(color=BLUE, label="Increase / net total"),
                       Patch(color=RED, label="Decrease")], loc="lower left", bbox_to_anchor=(0, 1.0),
              ncol=3, fontsize=9, borderaxespad=0.3)
    ax = axes[1]
    names = list(cost_shapley.index)
    nominal = fee_measures.loc["Sum of nominal daily charges", names].astype(float).to_numpy()
    compounded = cost_shapley.iloc[:, 0].to_numpy()
    y = np.arange(len(names))
    ax.barh(y - 0.15, nominal, height=0.26, color=MUTED, label="Sum of nominal daily charges")
    ax.barh(y + 0.15, compounded, height=0.26, color=BLUE, label="Compounded wealth lost (Shapley)")
    for k in range(len(names)):
        ax.text(nominal[k] + 0.15, y[k] - 0.15, f"{nominal[k]:.2f}", va="center", fontsize=9.5, color=INK2)
        ax.text(compounded[k] + 0.15, y[k] + 0.15, f"{compounded[k]:.2f}", va="center", fontsize=9.5,
                color=INK, fontweight="bold")
    ax.set_yticks(y, names)
    ax.invert_yaxis()
    ax.set_xlim(0, max(nominal.max(), compounded.max()) * 1.2)
    ax.set_xlabel("Per initial 100")
    ax.set_title("Explicit costs: charged vs compounded", loc="left", pad=26)
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, fontsize=9, borderaxespad=0.3)
    fig.suptitle("Structural economics at fixed baseline exposures", x=0.01, ha="left",
                 fontsize=16, fontweight="bold", color=INK)
    _save(fig, output, "drag_attribution")

    # Published-anchor consistency: GS-printed figures against the reconstruction.
    families = list(dict.fromkeys(anchors.Family))
    fig, axes = plt.subplots(1, len(families), figsize=(16, 4.8), layout="constrained")
    contradicted = fingerprint.loc[fingerprint.Verdict == "contradicted by published anchors"]
    decimals = {a.label: a.decimals for a in PUBLISHED_ANCHORS}
    units = {"Seed volatility": "% annualized", "Trailing return": "%", "Cost rate": "% per year"}
    for ax, family in zip(axes, families):
        block = anchors.loc[anchors.Family == family]
        y = np.arange(len(block))
        ticks = []
        for row, (label, item) in enumerate(block.iterrows()):
            d = decimals[label]
            ticks.append(f"{label}\nGS {item['Published (%)']:.{d}f} | recon {item['Reconstruction (%)']:.{d}f}")
            alt = contradicted[label].dropna().to_numpy()
            ax.plot(alt, np.full(len(alt), row), "x", color=MUTED, markersize=7, markeredgewidth=1.4, zorder=2)
            ax.plot(item["Published (%)"], row, "o", markersize=11, markerfacecolor=SURFACE,
                    markeredgecolor=INK, markeredgewidth=1.6, zorder=3)
            ax.plot(item["Reconstruction (%)"], row, "o", markersize=7, color=BLUE,
                    markeredgecolor=SURFACE, markeredgewidth=1.0, zorder=4)
        ax.set_yticks(y, ticks, fontsize=9.5)
        ax.set_ylim(len(block) - 0.4, -0.6)
        ax.set_title(f"{family} ({units.get(family, '%')})", loc="left")
        ax.grid(axis="y", visible=False)
    fig.legend(handles=[Line2D([], [], marker="o", linestyle="", markerfacecolor=SURFACE, markeredgecolor=INK,
                               markersize=10, label="Published by Goldman Sachs"),
                        Line2D([], [], marker="o", linestyle="", color=BLUE, markersize=7,
                               label="Methodology reconstruction"),
                        Line2D([], [], marker="x", linestyle="", color=MUTED, markersize=7,
                               label="Contradicted alternative conventions")],
               loc="lower left", ncol=3, bbox_to_anchor=(0.01, -0.02), fontsize=9.5)
    fig.suptitle("Consistency with figures published in the methodology and brochure (not official daily levels)",
                 x=0.01, ha="left", fontsize=14.5, fontweight="bold", color=INK)
    fig.get_layout_engine().set(h_pad=0.15)
    _save(fig, output, "published_anchor_checks")

    # Gap risk: synthetic -20% overnight gap, exposure path through the double lag.
    gap = synthetic_paths.loc[synthetic_paths.Scenario == "Overnight -20% gap"]
    gap = gap.loc[gap["Session Relative To Shock"].between(-3, 8)]
    fig, (left, right) = plt.subplots(1, 2, figsize=(15, 5.4), layout="constrained")
    for column, color, name in (("Underlying Level", ORANGE, "Synthetic underlying"),
                                ("Index Level", BLUE, "TimeX policy on synthetic path")):
        left.plot(gap["Session Relative To Shock"], gap[column], color=color, linewidth=1.8, marker="o",
                  markersize=5, markeredgecolor=SURFACE, label=name)
        left.annotate(f"{gap[column].iloc[-1]:.1f}", (gap["Session Relative To Shock"].iloc[-1], gap[column].iloc[-1]),
                      xytext=(6, 0), textcoords="offset points", va="center", fontsize=9.5, color=INK)
    left.axvline(0, color=AXIS, linewidth=0.9)
    left.set_xlabel("Sessions relative to the shock session")
    left.set_ylabel("Level, 100 = close before shock")
    left.set_title("Level path", loc="left")
    left.legend(loc="center right", fontsize=9.5)
    steps = gap["Session Relative To Shock"].to_numpy()
    exposure = gap["Held Exposure"].to_numpy() * 100
    right.bar(steps, exposure, width=0.42, color=BLUE)
    right.axvline(0, color=AXIS, linewidth=0.9, zorder=0)
    for x, value in zip(steps, exposure):
        if -1 <= x <= 3:
            right.text(x, value + 2, f"{value:.0f}%", ha="center", fontsize=9.5, color=INK)
    right.axhline(150, color=MUTED, linewidth=0.8)
    right.text(steps[0] - 0.3, 151.5, "150% cap", fontsize=8.5, color=MUTED, va="bottom")
    right.set_ylim(0, 165)
    right.set_xlabel("Sessions relative to the shock session")
    right.set_ylabel("Exposure held over the session, %")
    right.set_title("Exposure held over each session", loc="left")
    right.text(0.98, 0.62, "The shock-day close uses volatility through t-1,\nso exposure rises for session +1;\n"
               "afterwards each close can cut at most 50pp.", transform=right.transAxes, ha="right",
               va="top", fontsize=9.5, color=INK2)
    right.grid(axis="x", visible=False)
    fig.suptitle("Synthetic stress (seeded, not historical): -20% overnight gap after a quiet history",
                 x=0.01, ha="left", fontsize=14.5, fontweight="bold", color=INK)
    _save(fig, output, "synthetic_gap")


def _md_table(frame: pd.DataFrame, formats: dict, index_label: str) -> list[str]:
    columns = list(formats)
    align = ["---|" if formats[c] == "{}" else "---:|" for c in columns]
    lines = ["| " + " | ".join([index_label] + columns) + " |", "|---|" + "".join(align)]
    for label, row in frame.iterrows():
        cells = []
        for column in columns:
            value = row[column]
            if isinstance(value, (int, float, np.floating)):
                cells.append(formats[column].format(value) if np.isfinite(value) else "n/a")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join([str(label)] + cells) + " |")
    return lines


def write_report(summary, simple_summary, direct, fee_measures, anchors, fingerprint, historical, synthetic,
                 sensitivity, dividend_summary, diagnostics, output):
    """Generate the research report from executed results, with defined estimands."""
    pct, pp = "{:.2%}", "{:+.4f}"
    gap = synthetic.loc["Overnight -20% gap"]
    lines = ["# Equity TimeX research results", "",
             f"Executed comparison: **{diagnostics['comparison_start']} to {diagnostics['comparison_end']}** "
             f"({diagnostics['comparison_observations']:,} daily returns after {diagnostics['warmup_observations']} "
             "warm-up sessions).", "",
             "> **Status.** Independent research reconstruction of the Goldman Sachs Equity TimeX Index "
             "(GSEQTMX) from the supplied methodology dated 28 July 2023. Official daily GSEQTMX levels were "
             "not available, so these results are **not** verified official index performance. Before the "
             "methodology's 2021 base dates the series is a research extension.", "",
             "## Performance", ""]
    lines += _md_table(summary, {"Annualized Return": pct, "Annualized Volatility": pct, "Sharpe Ratio": "{:.3f}",
                                 "Maximum Drawdown": pct, "Terminal Wealth per 100": "{:.2f}",
                                 "Cumulative Fee Drag": pct}, "Strategy")
    lines += ["", "Conventions: CAGR uses elapsed calendar years (365.25 days); volatility is the daily sample "
              "standard deviation times sqrt(252); maximum drawdown includes the starting value. The ER index "
              "return is already an excess return, so its Sharpe ratio uses a zero comparator; SPY price and total "
              "returns subtract the same DFF cash accrual. Cumulative fee drag is 1 - net terminal wealth / "
              "no-explicit-fee terminal wealth under the same exposures. SPY's embedded fund expense is already in "
              "its prices and is not charged again.", "",
              f"The requested simplified approximation is a different model, not the index:", ""]
    lines += _md_table(simple_summary, {"Annualized Return": pct, "Annualized Volatility": pct,
                                        "Sharpe Ratio": "{:.3f}", "Maximum Drawdown": pct,
                                        "Terminal Wealth per 100": "{:.2f}", "Cumulative Fee Drag": pct}, "Model")
    lines += ["", "## Consistency with figures Goldman Sachs published", "",
              "The methodology prints the EWMA volatilities used to initialize the index on 3 May 2021, and the "
              "methodology and brochure print backtested returns and cost rates. These were produced by GS with "
              "its own data, so they test the implemented conventions without certifying the series.", ""]
    lines += _md_table(anchors, {"Published (%)": "{:g}", "Reconstruction (%)": "{:.4f}",
                                 "Difference (pp)": pp}, "Anchor")
    contradicted = fingerprint.loc[fingerprint.Verdict == "contradicted by published anchors"]
    indistinct = fingerprint.loc[fingerprint.Verdict == "not distinguishable at anchor precision"]
    lines += ["", "Convention fingerprint (each row recomputes the full policy):", ""]
    lines += _md_table(fingerprint[["Anchors Contradicting", "Verdict"]],
                       {"Anchors Contradicting": "{}", "Verdict": "{}"}, "Convention")
    lines += ["", f"Contradicted: {', '.join(contradicted.index) or 'none'}. Not distinguishable at the printed "
              f"precision: {', '.join(indistinct.index) or 'none'}. The flag rule (error above three times the "
              "larger of the baseline's own family residual and the rounding half-unit) is a declared heuristic, "
              "not a statistical test.", "",
              "## Fee and financing economics", ""]
    lines += _md_table(fee_measures, {"Unit": "{}", "Total": "{:.4f}", "Index deduction": "{:.4f}",
                                      "Servicing": "{:.4f}", "Rebalancing": "{:.4f}"}, "Measure")
    lines += ["", "These measures have different denominators and are not interchangeable. Removed capital "
              "cannot compound, so compounded terminal wealth lost exceeds the sum of nominal charges. The Shapley "
              "split averages all six orderings of the three costs and sums exactly to the total.", "",
              f"Direct fixed-exposure counterfactuals: Fed Funds financing costs "
              f"**{direct['financing_terminal_loss_per_100']:.2f}** per initial 100; a hypothetical forfeiture of "
              f"dividends would cost a further **{direct['dividend_forfeit_terminal_loss_per_100']:.2f}**. The "
              f"sequential bridge reconciles to **{direct['attribution_reconciliation_error']:.1e}** (floating-point "
              "noise).", "",
              "## Three interview talking points", ""]
    close_only = fingerprint.loc["Dividends forfeited (Close-only ER)"]
    base = fingerprint.loc["Methodology baseline"]
    lines += ["1. **Excess return keeps dividends; financing and dividend forfeiture are separate mechanisms.** "
              "Methodology section 11 adds the cash dividend to the price return and subtracts Fed Funds on the "
              f"equity notional. At fixed exposures financing costs **{direct['financing_terminal_loss_per_100']:.2f}** "
              f"per initial 100, while forfeiting dividends would cost **{direct['dividend_forfeit_terminal_loss_per_100']:.2f}**. "
              "GS's own published numbers support the dividend-inclusive reading. The reconstructed 0.94 EWMA on "
              f"3 May 2021 is **{base['0.94 EWMA vol, 3 May 2021']:.4f}%** against the printed 12.0433%. A Close-only ER "
              f"gives {close_only['0.94 EWMA vol, 3 May 2021']:.4f}%, and its 5Y annualized return "
              f"({close_only['5Y annualized return']:.2f}%) misses the brochure's 6.02% by about a point a year; "
              f"the methodology path gives {base['5Y annualized return']:.2f}%.", ""]
    if "COVID rebound" in historical.index:
        row = historical.loc["COVID rebound"]
        lines.append("2. **Volatility control lags the recovery.** From "
                     f"{row['Start']} to {row['End']} the reconstruction returned **{row['Index Return']:.2%}** "
                     f"against **{row['SPY TR Return']:.2%}** for SPY TR, with mean held exposure of "
                     f"**{row['Mean Held Exposure']:.1%}**. Taking the maximum of the 0.94 and 0.97 EWMAs de-risks "
                     "quickly and re-risks only as the slower estimate decays. This is evidence about this interval, "
                     "not proof of future underperformance.")
    lines += ["", "3. **A 10% volatility target is not a loss limit, and the timing makes gaps worse.** In the "
              f"seeded synthetic -20% overnight gap the index held **{gap['Exposure Before Shock']:.1%}** exposure "
              f"and lost **{abs(gap['First Shock Index Return']):.2%}** on the shock day. The weight chosen at that "
              "close uses volatility through the previous day (VC at t-1), so the shock has not yet entered the "
              f"estimate. Exposure for the next session *rose* to **{gap['Exposure After Shock Close']:.1%}**, helped "
              "by the mean-reversion signal. After that the 50pp daily limit allowed only stepwise de-risking: "
              + (f"exposure first fell below 50% {int(gap['Sessions After Shock Until Exposure Below 50%'])} sessions "
                 "after the shock." if np.isfinite(gap["Sessions After Shock Until Exposure Below 50%"])
                 else "exposure never fell below 50% within the scenario.")
              + (" A continuation shock on the next day would have hit at above 100% exposure."
                 if gap["Exposure After Shock Close"] > 1 else ""), "",
              "## Stress and sensitivity", "", "Historical windows (close to close):", ""]
    lines += _md_table(historical, {"Index Return": pct, "SPY TR Return": pct, "Index Maximum Drawdown": pct,
                                    "Mean Held Exposure": "{:.1%}", "Minimum Held Exposure": "{:.1%}"}, "Window")
    lines += ["", "Synthetic scenarios (seed 17, 120-session quiet history, real 2019 session dates; synthetic "
              "results never replace historical data):", ""]
    lines += _md_table(synthetic, {"Index Return": pct, "SPY Price Return": pct, "Maximum Drawdown": pct,
                                   "Exposure Before Shock": "{:.1%}", "Exposure After Shock Close": "{:.1%}",
                                   "First Shock Index Return": pct}, "Scenario")
    lines += ["", "Parameter and policy sensitivities (full policy recomputed for each row):", ""]
    lines += _md_table(sensitivity, {"Annualized Return": pct, "Annualized Volatility": pct,
                                     "Sharpe Ratio": "{:.3f}", "Maximum Drawdown": pct,
                                     "Terminal Wealth per 100": "{:.2f}", "Mean Held Exposure": "{:.1%}"}, "Scenario")
    lines += ["", "## Data and convention checks", "",
              f"- **Dividend convention.** Explicit cash-dividend TR wealth is "
              f"{dividend_summary['cash_dividend_tr_wealth']:.4f} against Yahoo Adj Close "
              f"{dividend_summary['vendor_adj_close_tr_wealth']:.4f} ({dividend_summary['relative_difference']:.4%}). "
              f"Yahoo's ex-date return equals Close_t / (Close_t-1 - D) - 1 to within "
              f"{dividend_summary['max_abs_vendor_minus_identity_bp']:.3f} bp, which is reinvestment at the prior close. "
              f"That convention explains {dividend_summary['share_of_gap_explained_by_reinvestment_convention']:.2%} of the "
              f"gap. The largest daily difference is {dividend_summary['max_abs_daily_cash_minus_vendor_bp']:.4f} bp on "
              f"{dividend_summary['max_abs_daily_difference_date']}.",
              f"- **Short-term mean-reversion cap.** The ±10% clip never binds in this sample (maximum |signal| "
              f"{diagnostics['short_mr_max_abs']:.2%}). By Cauchy-Schwarz the signal is bounded near "
              f"{diagnostics['short_mr_theoretical_bound']:.2%} whenever log(1+r) is close to r.",
              f"- **FOMC look-ahead coverage.** The decision on {diagnostics['fomc_lookahead']['last_price']} needs the "
              f"scheduled calendar through {diagnostics['fomc_lookahead']['lookahead_required_through']}; certified by "
              f"{diagnostics['fomc_lookahead']['certified_by']}.",
              f"- **Inception reconstruction.** Resetting the published seeds on 3 May 2021 and NAV on 1 June 2021 "
              f"leaves the end level within {diagnostics['inception_vs_extended_terminal_difference']:+.4f} (per 100 at the "
              "index base date) of the extended-history path, so the initialization is forgotten.", "",
              "## Limitations", "",
              "- The ten-year study precedes the 1 June 2021 index base date and the 28 July 2023 launch date.",
              "- Yahoo prices/actions and FRED final-vintage DFF and DTB3 are proxies, not the calculation agent's "
              "inputs; DTB3 is converted from bank-discount to money-market yield with a 91-day tenor.",
              "- XNYS is a holiday-calendar proxy for NYSE Arca. Calendar mismatches fail the run.",
              "- The FOMC archive gives final calendars, not a vendor's point-in-time schedule. Unscheduled "
              "statements and notation votes are excluded, and the four-business-day scheduling test cannot be "
              "certified historically. 18 March 2020 is a sensitivity case.",
              "- Discretionary market disruption, adjustment and restatement decisions are not reproduced.",
              "- The methodology is the July 2023 version; later amendments are not presumed absent.",
              "- An index reconstruction does not reproduce a structured note's participation rate, caps, fees, "
              "or issuer credit exposure.", "",
              "## Resume wording", "",
              '"Built a source-audited Python reconstruction of a calendar-driven, volatility-controlled equity '
              'excess-return index (Goldman Sachs Equity TimeX methodology). Implemented quantity-based cost '
              'accounting, causal signal timing, Shapley fee attribution, and historical and synthetic stress tests. '
              'Corroborated the implementation against the index\'s published initialization volatilities '
              '(0.94 EWMA matched to 4 d.p.) and brochure backtest statistics."', "",
              "Do not describe this as an exact or official replication without an official-level reconciliation.", ""]
    (output / "research_report.md").write_text("\n".join(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--start", help="Comparison anchor; default last price minus ten years")
    parser.add_argument("--end", help="Exclusive data end date; offline default is the frozen snapshot's end, "
                                      "online default is today")
    parser.add_argument("--download-start", default="2014-01-01", help="Include warmup before comparison")
    parser.add_argument("--data-dir", type=Path, help="Input directory; default data/ offline, data_fresh/ online")
    parser.add_argument("--output-dir", type=Path, default=ROOT/"results")
    parser.add_argument("--offline", action="store_true", help="Reuse validated frozen local input CSVs")
    parser.add_argument("--overwrite-inputs", action="store_true",
                        help="Allow a download to replace an existing snapshot in --data-dir")
    parser.add_argument("--fomc-calendar", type=Path, default=FOMC_CALENDAR_PATH,
                        help="Verified FOMC event CSV; fomc_coverage.json must sit beside it")
    parser.add_argument("--official-levels", type=Path, help="Optional official CSV: date,level")
    parser.add_argument("--vol-target", type=float, default=0.10)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    cfg = Config(vol_target=args.vol_target)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    data_dir = (args.data_dir or (FROZEN_DATA_DIR if args.offline else FRESH_DATA_DIR)).resolve()
    revisions = None
    if args.offline:
        snapshot_path = data_dir / "download_manifest.json"
        snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else {}
        end = args.end or snapshot.get("requested_end_exclusive")
        if end is None:
            raise ValueError("Offline run needs --end or a download_manifest.json with requested_end_exclusive")
        sample_start = snapshot.get("requested_start")
        download_status = "not executed (offline run on the frozen snapshot)"
    else:
        end = args.end or datetime.now().date().isoformat()
        if data_dir == FROZEN_DATA_DIR.resolve() and not args.overwrite_inputs:
            raise ValueError("Refusing to download over the frozen reference snapshot in data/; "
                             "use the default data_fresh/ or pass --overwrite-inputs deliberately")
        download_inputs(args.download_start, end, data_dir,
                        overwrite=args.overwrite_inputs or data_dir == FRESH_DATA_DIR.resolve())
        sample_start = args.download_start
        download_status = f"executed into {data_dir}"
        if data_dir != FROZEN_DATA_DIR.resolve() and (FROZEN_DATA_DIR / "spy.csv").exists():
            revisions = snapshot_revisions(data_dir, FROZEN_DATA_DIR)
            (output / "data_revisions.json").write_text(json.dumps(revisions, indent=2))
    market, metadata = load_inputs(data_dir, end, cfg)
    metadata["sample_bounds"] = validate_sample_bounds(market.index, sample_start or market.index[0], end, cfg.calendar)
    events = load_fomc(args.fomc_calendar, end)
    # Deliberately demand sufficient calendar coverage, even if few meetings,
    # including the two-session look-ahead of the final FOMC decision.
    coverage_path = args.fomc_calendar.with_name("fomc_coverage.json")
    coverage = json.loads(coverage_path.read_text())
    fomc_lookahead = validate_fomc_coverage(coverage, market.index, cfg.calendar)
    desired = pd.Timestamp(args.start) if args.start else market.index[-1]-pd.DateOffset(years=10)
    pos = int(market.index.searchsorted(desired))
    if pos >= len(market) or pos < 252:
        raise ValueError("Comparison requires at least 252 prior warmup observations and a nonempty window")
    anchor = market.index[pos]
    signals = generate_signals(market, events, cfg)
    feat = controlled_weights(market, signals, cfg)
    comparison_market, comparison_feat = market.loc[anchor:], feat.loc[anchor:]
    ledger = accounting(comparison_market.asset_er_return, comparison_feat.weight_close, comparison_market.dcf, cfg)
    zero_cfg = replace(cfg, deduction=0, servicing=0, transaction=0)
    nofee = accounting(comparison_market.asset_er_return, comparison_feat.weight_close, comparison_market.dcf, zero_cfg).net_level
    paths = pd.DataFrame({INDEX_NAME: normalize_path(ledger.net_level, anchor),
                          PRICE_NAME: normalize_path(wealth(market.price_return), anchor),
                          TOTAL_NAME: normalize_path(wealth(market.adj_return), anchor)})
    summary = summary_metrics(paths, market.cash_return, nofee)
    attribution, counterfactuals, direct = run_counterfactuals(market, feat, cfg, anchor)
    cost_shapley = shapley_costs(market, feat, cfg, anchor)
    fee_measures = fee_drag_measures(ledger, nofee, cost_shapley, market.cash_return)
    dividend_table, dividend_summary = dividend_convention_audit(market, anchor)
    historical, synthetic, synthetic_paths = stress_analysis(market, events, cfg, anchor)
    sensitivity = sensitivity_analysis(market, events, cfg, anchor)
    anchors, fingerprint = published_anchor_checks(market, events, cfg)
    simplified = simplified_model(market, feat, cfg)
    simplified_nofee = simplified_model(market, feat, cfg, costs=False)
    simple_path = normalize_path(simplified.net_level, anchor)
    simple_summary = performance(simple_path, market.tbill_cash_return, True)
    simple_summary["Cumulative Fee Drag"] = 1-simple_path.iloc[-1]/normalize_path(simplified_nofee.net_level, anchor).iloc[-1]
    simple_summary = pd.DataFrame([simple_summary], index=pd.Index(["Requested approximation"], name="Model"))
    simplified_daily = simplified.join(signals[["tom_requested", "opex_date", "fomc_announcement"]])
    # A separate model actually honors official initialization dates. Its first
    # accounting row is June 1, 2021 NAV=100, with Q and zero costs set at base.
    feat_official = controlled_weights(market, signals, cfg, official_seeds=True)
    base_date = pd.Timestamp(cfg.index_base_date)
    mask = market.index >= base_date
    inception = None
    if mask.sum() > 20 and base_date in market.index:
        inception = accounting(market.asset_er_return.loc[mask], feat_official.weight_close.loc[mask],
                               market.dcf.loc[mask], cfg)
        inception.join(feat_official[["allocation", "algorithmic_vol"]]).to_csv(output/"official_inception_daily.csv", index_label="date")
    sample = ledger.loc[anchor:].iloc[1:]
    costs_scale = 100/ledger.net_level.loc[anchor]
    diagnostics = {"comparison_start": str(anchor.date()), "comparison_end": str(market.index[-1].date()),
                   "comparison_observations": len(paths)-1, "warmup_observations": pos,
                   "mean_held_exposure": float(feat.weight_held.loc[anchor:].iloc[1:].mean()),
                   "fraction_held_at_150pct": float((feat.weight_held.loc[anchor:].iloc[1:] >= 1.5-1e-10).mean()),
                   "maximum_actual_nav_exposure": float(sample.actual_nav_exposure.max()),
                   "explicit_nominal_costs_per_initial_100": float(sample.explicit_cost.sum()*costs_scale),
                   "nofee_annualized_return": float(performance(nofee, market.cash_return, True)["Annualized Return"]),
                   "fee_cagr_reduction_pp": float(100*(performance(nofee, market.cash_return, True)["Annualized Return"]-summary.loc[INDEX_NAME, "Annualized Return"])),
                   "mean_annual_nominal_cost_rate": float((sample.explicit_cost/ledger.net_level.shift(1).reindex(sample.index)).sum()/((paths.index[-1]-anchor).days/365.25)),
                   "cash_vs_adjusted_total_return_max_daily_difference": float((market.cash_total_return-market.adj_return).abs().max()),
                   "max_final_weight_step": float(feat.weight_close.diff().abs().max()),
                   "max_allocation_step": float(feat.allocation.diff().abs().max()),
                   "short_mr_max_abs": float(signals.short_mr.abs().max()),
                   "short_mr_theoretical_bound": float(short_mr_bound()),
                   "fomc_lookahead": fomc_lookahead,
                   "dividend_convention": dividend_summary,
                   **direct}
    if inception is not None:
        extended = accounting(market.asset_er_return.loc[mask], feat.weight_close.loc[mask], market.dcf.loc[mask], cfg)
        diagnostics["inception_vs_extended_terminal_difference"] = float(inception.net_level.iloc[-1] - extended.net_level.iloc[-1])
        diagnostics["inception_vs_extended_max_weight_difference_after_2021_09"] = float(
            (feat_official.weight_close - feat.weight_close).loc["2021-09-01":].abs().max())
    official_audit = {"status": "not performed: official GSEQTMX levels not supplied"}
    if args.official_levels:
        if inception is None:
            raise ValueError("Official reconciliation needs prices covering the 1 June 2021 index base date")
        official_audit = {"inception_seeded": audit_official(inception.net_level, args.official_levels, cfg.launch_date,
                                                             output, compare_levels=True)}
        start_ext = market.index[ANCHOR_LEDGER_WARMUP]
        extended_full = accounting(market.asset_er_return.loc[start_ext:], feat.weight_close.loc[start_ext:],
                                   market.dcf.loc[start_ext:], cfg)
        official_audit["extended_history"] = audit_official(extended_full.net_level, args.official_levels, cfg.launch_date,
                                                            output, filename="official_reconciliation_extended.csv")
    exports = {"performance_summary.csv": summary, "equity_curves.csv": paths,
               "daily_audit_ledger.csv": market.join(feat).join(ledger.add_prefix("ledger_")),
               "drag_attribution.csv": attribution, "cost_shapley.csv": cost_shapley,
               "fee_drag_measures.csv": fee_measures, "dividend_convention_audit.csv": dividend_table,
               "counterfactual_curves.csv": counterfactuals, "historical_stress.csv": historical,
               "synthetic_stress.csv": synthetic, "synthetic_paths.csv": synthetic_paths.set_index("Scenario"),
               "parameter_sensitivity.csv": sensitivity,
               "published_anchor_checks.csv": anchors, "convention_fingerprint.csv": fingerprint,
               "requested_approximation_daily.csv": simplified_daily,
               "requested_approximation_summary.csv": simple_summary}
    for filename, data in exports.items():
        data.to_csv(output/filename, index_label=data.index.name or "date", float_format="%.12g")
    make_charts(paths, feat, attribution, cost_shapley, fee_measures, anchors, fingerprint, synthetic_paths, output)
    write_report(summary, simple_summary, direct, fee_measures, anchors, fingerprint, historical, synthetic,
                 sensitivity, dividend_summary, diagnostics, output)
    output_hashes = {p.name: sha256(p) for p in sorted(output.iterdir())
                     if p.is_file() and p.name not in ("run_manifest.json", "validation.json")}
    manifest = {"executed_utc": datetime.now(timezone.utc).isoformat(), "config": asdict(cfg),
                "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                "resolved": {"data_dir": str(data_dir), "end_exclusive": end, "sample_start": sample_start},
                "fresh_download": download_status, "data_revisions_vs_frozen": revisions,
                "data": metadata, "diagnostics": diagnostics, "official_audit": official_audit,
                "fomc_coverage": coverage, "methodology": METHODOLOGY_URL,
                "published_anchors": [asdict(a) for a in PUBLISHED_ANCHORS],
                "script_sha256": sha256(Path(__file__)),
                "input_sha256": {p.name: sha256(p) for p in sorted(data_dir.glob("*.csv"))},
                "fomc_calendar_sha256": sha256(args.fomc_calendar),
                "output_sha256": output_hashes,
                "python": sys.version.split()[0], "platform": platform.platform(),
                "versions": {pkg: importlib.metadata.version(pkg) for pkg in
                             ("pandas", "numpy", "yfinance", "matplotlib", "seaborn", "exchange_calendars",
                              "requests", "pytest")},
                "limitations": ["Research extension before index base date", "NYSE calendar proxy for Arca",
                                "Final-vintage Yahoo/FRED data", "FOMC archive not point-in-time vendor history",
                                "Official disruptions, amendments and restatements not modeled",
                                "Published anchors are GS backtest/initialization figures, not official daily levels"]}
    (output/"run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    (output/"diagnostics.json").write_text(json.dumps(diagnostics, indent=2, default=str))
    LOG.info("Comparison %s through %s", anchor.date(), market.index[-1].date())
    print(summary.to_string(float_format=lambda x: f"{x:.6f}"))
    print(anchors[["Published (%)", "Reconstruction (%)", "Difference (pp)"]].to_string(float_format=lambda x: f"{x:.4f}"))
    LOG.info("Results written to %s", output)
    return summary


if __name__ == "__main__":
    main()
