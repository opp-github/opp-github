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
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
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


def download_inputs(start: str, end: str, directory: Path) -> dict:
    """yfinance Close + Adj Close + cash actions; FRED DFF and DTB3, no key.

    Yahoo end date is EXCLUSIVE. auto_adjust=False is essential: otherwise Close
    would silently become dividend-adjusted. Corporate-action repair is disabled
    so a vendor repair algorithm does not become an undocumented model input.
    """
    import yfinance as yf
    directory.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(directory / ".yfinance_cache"))

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
    spy.to_csv(directory / "spy.csv", float_format="%.12g")
    sources = {"spy": {"provider": "Yahoo via yfinance", "ticker": "SPY",
                        "first": str(spy.index.min().date()),
                        "last": str(spy.index.max().date()), "rows": len(spy)}}
    for series in ("DFF", "DTB3"):
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
        rates.to_csv(directory / f"{series.lower()}.csv", index=False)
        sources[series] = {"provider": "FRED / Federal Reserve H.15",
                           "source_url": f"https://fred.stlouisfed.org/series/{series}",
                           "download_url": csv_url,
                           "units": "annual percent", "last": str(rates.dropna().iloc[-1, 0])}
    sources["downloaded_utc"] = datetime.now(timezone.utc).isoformat()
    sources["requested_start"] = start
    sources["requested_end_exclusive"] = end
    (directory / "download_manifest.json").write_text(json.dumps(sources, indent=2))
    return sources


def load_inputs(directory: Path, end: str, cfg: Config) -> tuple[pd.DataFrame, dict]:
    """Validate prices against a complete exchange calendar, align cash causally.

    Rates are forward-filled FROM PAST observations only. No backfill is allowed.
    Missing rate intervals longer than seven calendar days fail the run.
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
                "last_price": str(market.index[-1].date()), "rows": len(market)}
    for series in ("DFF", "DTB3"):
        raw = pd.read_csv(directory / f"{series.lower()}.csv")
        raw.columns = ["date", series]
        raw["date"] = pd.to_datetime(raw["date"])
        raw[series] = pd.to_numeric(raw[series], errors="coerce")
        raw = raw.dropna().sort_values("date")
        if raw["date"].duplicated().any():
            raise ValueError(f"Duplicate {series} dates")
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


def generate_signals(market: pd.DataFrame, events: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Compute close-t decision signals; they affect P&L starting session t+1.

    A padded exchange calendar prevents partial sample months from falsely
    looking like month-end, and resolves Good Friday option expiry correctly.
    Calendar dates are deterministic; price comparisons use only already-known
    closes. FOMC is scheduled-only, with archival (not point-in-time) dates.
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
        if previous_days is not None and len(days) >= 6:
            cutoff, prior_eom = days[-6], previous_days.iloc[-1]
            if cutoff in er.index and prior_eom in er.index and er[cutoff] < er[prior_eom]:
                s.loc[idx.intersection(days[-5:-1]), "tom_mr"] = 1.0
        friday = third_friday(month)
        expiry_pos = sessions.searchsorted(friday, side="right") - 1
        expiry = sessions[expiry_pos]
        if expiry in idx:
            flags.loc[expiry, "opex_date"] = True
        prior_friday = third_friday(month - 1)
        prior_expiry = sessions[sessions.searchsorted(prior_friday, side="right") - 1]
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


def controlled_weights(market: pd.DataFrame, signals: pd.DataFrame, cfg: Config,
                       official_seeds: bool = False) -> pd.DataFrame:
    """Sequential algorithmic portfolio and two-stage volatility control.

    Volatility is on the ALGORITHMIC (signal allocated) ER portfolio. For t-close
    target lookthrough, multiply allocation[t] by target/vol[t-1], then slew-limit
    the change to 50 percentage points and cap at 150%. The current day's shock
    cannot retroactively reduce the weight that earned that day's return.
    """
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
            log_r2 = np.log1p(alg_r) ** 2
            vs[i] = np.sqrt(cfg.lambda_short * vs[i-1]**2 + (1-cfg.lambda_short)*252*log_r2)
            vl[i] = np.sqrt(cfg.lambda_long * vl[i-1]**2 + (1-cfg.lambda_long)*252*log_r2)
        lag_vol = max(vs[i-1], vl[i-1]) if i else max(vs[i], vl[i])
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
               cfg: Config, initial_level: float = 100.0) -> pd.DataFrame:
    """Exact §2-6 quantity accounting. Costs reduce NAV after quantity selection.

    Q_t = gross_t * W_t / A_t; this is intentionally GROSS, not net, NAV.
    RC_t = |Q_t-Q_(t-1)| * A_t * rate. This includes drifting quantities and
    previous cost deductions, which a simple abs(delta weight) model misses.
    Index at/below zero is absorbing, as required by §2.
    """
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
        sc[i] = qty[i-1] * asset[i-1] * dcf.iloc[i] * cfg.servicing
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


def simplified_model(market: pd.DataFrame, feat: pd.DataFrame, cfg: Config,
                     rate="tbill_cash_return", costs=True) -> pd.DataFrame:
    """Requested approximation, deliberately separated from official rules.

    Close-only asset return, max 20/60-day SPY vol and 10% target / lagged vol.
    Calendar flags are diagnostic; no unrequested event multiplier is invented.
    Servicing is flat NAV and transaction cost is proportional weight turnover.
    Missing historical vol at the very start means zero exposure until available.
    """
    vol = feat[["spy_vol20", "spy_vol60"]].max(axis=1)
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


def stress_analysis(market, events, cfg, anchor) -> tuple[pd.DataFrame, pd.DataFrame]:
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
    # Seeded quiet history permits >1x exposure before an unanticipated gap.
    rng = np.random.default_rng(17)
    quiet = rng.normal(0.00015, 0.003, 120)
    shocks = {"Overnight -20% gap": np.r_[-0.20, np.zeros(39)],
              "Crash then rapid rebound": np.r_[np.full(10, -0.025), np.full(10, 0.028), np.zeros(20)],
              "Low-volatility chop": np.tile([0.004, -0.004], 20)}
    synthetic = []
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
        segment = l.net_level.iloc[begin:]
        benchmark = pd.Series(px, index=dates).iloc[begin:]
        synthetic.append({"Scenario": label, "Index Return": segment.iloc[-1]/segment.iloc[0]-1,
                          "SPY Price Return": benchmark.iloc[-1]/benchmark.iloc[0]-1,
                          "Maximum Drawdown": (segment/segment.cummax()-1).min(),
                          "Exposure Before Shock": f.weight_close.iloc[begin],
                          "Exposure After Shock Close": f.weight_close.iloc[begin+1],
                          "First Shock Index Return": l["return"].iloc[begin+1],
                          "Minimum Subsequent Exposure": f.weight_close.iloc[begin+1:].min()})
    return pd.DataFrame(historical).set_index("Scenario"), pd.DataFrame(synthetic).set_index("Scenario")


def sensitivity_analysis(market, events, cfg, anchor) -> pd.DataFrame:
    """Policy feedback tests recompute signals, vol and exposure from scratch."""
    cases = {"Baseline": (cfg, market),
             "Vol target 8%": (replace(cfg, vol_target=0.08), market),
             "Vol target 12%": (replace(cfg, vol_target=0.12), market),
             "No leverage above 100%": (replace(cfg, equity_cap=1.0), market),
             "Fast EWMA only": (replace(cfg, lambda_long=cfg.lambda_short, seed_long=cfg.seed_short), market),
             "Slow EWMA only": (replace(cfg, lambda_short=cfg.lambda_long, seed_short=cfg.seed_long), market),
             "No calendar / MR signals": (replace(cfg, signal_leverages=(0.,)*5), market)}
    bumped = market.copy()
    bumped["cash_return"] = market.cash_return + 0.02*market.dcf
    bumped["asset_er_return"] = bumped.cash_total_return - bumped.cash_return
    bumped["asset_er"] = wealth(bumped.asset_er_return)
    cases["Funding +200 bp (policy recomputed)"] = (cfg, bumped)
    rows = []
    for name, (c, m) in cases.items():
        f = controlled_weights(m, generate_signals(m, events, c), c)
        m, f = m.loc[anchor:], f.loc[anchor:]
        l = accounting(m.asset_er_return, f.weight_close, m.dcf, c)
        row = performance(normalize_path(l.net_level, anchor), market.cash_return, True)
        row.update({"Scenario": name, "Mean Held Exposure": f.weight_held.loc[anchor:].iloc[1:].mean()})
        rows.append(row)
    # A final-calendar archive cannot establish precisely when March 18's
    # cancellation became known. Recompute the policy with that planned meeting
    # included, without asserting this is the official historical treatment.
    alternate = events.copy()
    alternate.loc[(alternate.event_type == "cancelled") &
                  (alternate.date == pd.Timestamp("2020-03-18")), "event_type"] = "scheduled"
    f = controlled_weights(market, generate_signals(market, alternate, cfg), cfg)
    m, f = market.loc[anchor:], f.loc[anchor:]
    l = accounting(m.asset_er_return, f.weight_close, m.dcf, cfg)
    row = performance(l.net_level, market.cash_return, True)
    row.update({"Scenario": "Include planned 18 Mar 2020 FOMC", "Mean Held Exposure": f.weight_held.iloc[1:].mean()})
    rows.append(row)
    # Rate proxy sensitivity is distinct from a funding spread stress.
    alt = market.copy()
    alt["cash_return"] = alt.tbill_cash_return
    alt["asset_er_return"] = alt.cash_total_return-alt.cash_return
    alt["asset_er"] = wealth(alt.asset_er_return)
    f = controlled_weights(alt, generate_signals(alt, events, cfg), cfg)
    m, f = alt.loc[anchor:], f.loc[anchor:]
    l = accounting(m.asset_er_return, f.weight_close, m.dcf, cfg)
    row = performance(l.net_level, market.cash_return, True)
    row.update({"Scenario": "DTB3 funding proxy (policy recomputed)", "Mean Held Exposure": f.weight_held.iloc[1:].mean()})
    rows.append(row)
    return pd.DataFrame(rows).set_index("Scenario")


def audit_official(reconstructed: pd.Series, path: Path, launch: str, output: Path) -> dict:
    """Optional external reconciliation; no official levels are fabricated.

    Supply CSV date,level. Both paths are rebased on the first overlapping date.
    Residuals measure discrepancies, not evidence of certified replication.
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
    normalized.to_csv(output / "official_reconciliation.csv", index_label="date")
    result = {"status": "compared, not certified", "common_dates": len(joined),
              "overlap_start": str(joined.index[0].date()), "overlap_end": str(joined.index[-1].date()),
              "annualized_tracking_error": float(residual.std()*np.sqrt(252)),
              "mean_daily_return_bias": float(residual.mean()),
              "max_absolute_rebased_level_error": float(normalized["Level Difference"].abs().max()),
              "official_file_sha256": sha256(path)}
    # Preserve the distinction between official backtest and live calculation.
    for regime, mask in [("pre_launch", joined.index < pd.Timestamp(launch)),
                         ("live", joined.index >= pd.Timestamp(launch))]:
        period = joined.loc[mask]
        if len(period) >= 20:
            diff = period.pct_change(fill_method=None).dropna().Research-period.pct_change(fill_method=None).dropna().Official
            result[regime+"_tracking_error"] = float(diff.std()*np.sqrt(252))
    return result


def make_charts(paths, feat, ledger, attribution, cost_shapley, historical, output):
    """Publication-quality required two-panel figure and diagnostic attribution."""
    sns.set_theme(style="whitegrid", context="notebook", font="DejaVu Sans")
    plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False,
                         "grid.alpha": 0.22, "figure.facecolor": "white"})
    colors = {INDEX_NAME: "#153E64", PRICE_NAME: "#DF8C32", TOTAL_NAME: "#238B7D"}
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(15, 10), sharex=True,
                                  gridspec_kw={"height_ratios": [1.25, 1]}, layout="constrained")
    fig.suptitle("EQUITY TIMEX | RESEARCH RECONSTRUCTION", x=0.07, ha="left", fontsize=21, fontweight="bold", color="#153E64")
    for name in paths:
        ax1.plot(paths.index, paths[name], color=colors[name], label=name, linewidth=2.0)
    ax1.set_yscale("log")
    ax1.yaxis.set_major_locator(LogLocator(base=10, subs=[1, 2, 3, 4, 5, 7]))
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value:,.0f}"))
    ax1.yaxis.set_minor_formatter(NullFormatter())
    ax1.set_ylabel("Growth of 100 | log scale")
    ax1.set_title(f"A   {paths.index[0]:%d %b %Y} - {paths.index[-1]:%d %b %Y} | identical comparison dates", loc="left", pad=12)
    ax1.legend(loc="upper left", frameon=False)
    f = feat.reindex(paths.index)
    ax2.fill_between(f.index, 0, f.weight_held*100, color="#153E64", alpha=0.13)
    ax2.plot(f.index, f.weight_held*100, color="#153E64", linewidth=0.8, label="Held exposure (gross-normalized)")
    ax2.axhline(100, color="#708090", linestyle="--", linewidth=0.7)
    ax2.set_ylim(0, 155)
    ax2.set_yticks([0, 50, 100, 150])
    ax2.set_ylabel("Equity exposure (%)", color="#153E64")
    ax2.set_title("B   Exposure responds to lagged realized volatility", loc="left", pad=12)
    right = ax2.twinx()
    right.plot(f.index, f.spy_vol20*100, color="#C45755", linewidth=1.0, alpha=0.85, label="SPY 20-day realized vol")
    right.set_ylim(0, max(40, np.nanmax(f.spy_vol20*100)*1.08))
    right.set_ylabel("Annualized market volatility (%)", color="#C45755")
    right.grid(False)
    handles1, labels1 = ax2.get_legend_handles_labels()
    handles2, labels2 = right.get_legend_handles_labels()
    ax2.legend(handles1+handles2, labels1+labels2, loc="upper left", frameon=True,
               facecolor="white", framealpha=0.97, edgecolor="none", fontsize=9)
    for ax in (ax1, ax2):
        if paths.index[0] <= pd.Timestamp("2023-07-28") <= paths.index[-1]:
            ax.axvline(pd.Timestamp("2023-07-28"), color="#788797", linestyle=":", linewidth=1)
    ax1.text(pd.Timestamp("2023-07-28"), ax1.get_ylim()[0]*1.10, " Official launch", fontsize=9, color="#647484")
    fig.supxlabel("Yahoo SPY + FRED DFF | 2023 methodology | independent proxy, not official published levels", fontsize=10)
    fig.savefig(output / "timex_dashboard.png", dpi=190)
    fig.savefig(output / "timex_dashboard.svg")
    plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(15, 6), layout="constrained")
    terminal = attribution["Terminal Wealth per 100"]
    axes[0].barh(np.arange(len(terminal)), terminal, color=["#238B7D"]*4+["#153E64"]*4)
    axes[0].set_yticks(np.arange(len(terminal)), terminal.index, fontsize=9)
    axes[0].invert_yaxis()
    axes[0].set_xlabel("Terminal wealth from identical 100 starting value")
    axes[0].set_title("Sequential attribution | order dependent", loc="left")
    axes[1].bar(cost_shapley.index, cost_shapley.iloc[:, 0], color=["#153E64", "#238B7D", "#DF8C32"])
    axes[1].set_ylabel("Terminal wealth lost per initial 100")
    axes[1].set_title("Explicit costs | order-independent Shapley attribution", loc="left")
    fig.suptitle("STRUCTURAL ECONOMICS | FIXED BASELINE EXPOSURES", fontsize=17, fontweight="bold", color="#153E64")
    fig.savefig(output / "drag_attribution.png", dpi=180)
    plt.close(fig)


def write_report(summary, direct, historical, synthetic, diagnostics, metadata, output):
    """Generate talking points from executed results, with defined estimands."""
    lines = ["# Equity TimeX research results", "",
             f"Executed comparison: **{diagnostics['comparison_start']} to {diagnostics['comparison_end']}**.", "",
             "Independent research reconstruction using the supplied July 28, 2023 methodology. "
             "Official published levels were not supplied unless indicated in run_manifest.json; "
             "results must not be presented as verified GSEQTMX performance.", "",
             "## Performance", "", "| Strategy | CAGR | Volatility | Sharpe | Max drawdown | Fee wealth loss |",
             "|---|---:|---:|---:|---:|---:|"]
    for label, row in summary.iterrows():
        lines.append(f"| {label} | {row['Annualized Return']:.2%} | {row['Annualized Volatility']:.2%} | "
                     f"{row['Sharpe Ratio']:.2f} | {row['Maximum Drawdown']:.2%} | {row['Cumulative Fee Drag']:.2%} |")
    lines += ["", "Sharpe convention: an ER-index return is already excess and is compared with zero; "
              "SPY price and total returns subtract the same DFF accrual. CAGR uses elapsed calendar "
              "years; daily volatility uses 252 observations/year. Fee wealth loss is a percentage of "
              "the no-explicit-fee terminal wealth under fixed baseline exposure. SPY's embedded fund "
              "expense is already in prices, not an additional modeled fee.", "", "## Three interview talking points", "",
              "1. **Excess return retains dividends.** Official section 11 adds cash dividends and subtracts "
              "Fed Funds financing on the entire equity exposure; unused exposure earns no modeled cash. "
              f"Forfeiting dividends at the same baseline exposures would reduce terminal wealth by "
              f"**{direct['dividend_forfeit_terminal_loss_per_100']:.2f} per initial 100**. This is a "
              "counterfactual, not an official index charge. Financing itself costs "
              f"**{direct['financing_terminal_loss_per_100']:.2f} per initial 100** in the fixed-exposure comparison.", ""]
    if "COVID rebound" in historical.index:
        row = historical.loc["COVID rebound"]
        lines.append("2. **Volatility control can miss the recovery.** During the COVID rebound window "
                     f"({row['Start']} to {row['End']}), the reconstruction returned **{row['Index Return']:.2%}** "
                     f"versus **{row['SPY TR Return']:.2%}** for SPY TR, with mean held exposure "
                     f"**{row['Mean Held Exposure']:.1%}**. Taking the maximum of two EWMAs speeds "
                     "de-risking while retaining the slower measure during normalization. This is "
                     "historical evidence for this interval, not proof of future underperformance.")
    else:
        row = synthetic.loc["Crash then rapid rebound"]
        lines.append("2. **Recovery lag is observable in the seeded crash/rebound stress.** "
                     f"The synthetic index returns {row['Index Return']:.2%}; the price benchmark returns "
                     f"{row['SPY Price Return']:.2%}. Both EWMA lag and slew limits affect re-risking.")
    lines += ["", "3. **Target volatility is not a guarantee, and fees depend on implementation.** "
              f"Sample realized volatility is **{summary.loc[INDEX_NAME, 'Annualized Volatility']:.2%}**, "
              f"against a 10% target. The synthetic -20% overnight gap starts with "
              f"**{synthetic.loc['Overnight -20% gap', 'Exposure Before Shock']:.1%}** exposure and loses "
              f"**{abs(synthetic.loc['Overnight -20% gap', 'First Shock Index Return']):.2%}** on the first shock "
              "day before de-risking. Servicing is notional-weighted and transaction cost uses synthetic "
              f"quantity changes: the three explicit costs reduce terminal wealth by "
              f"**{direct['explicit_fee_terminal_loss_per_100']:.2f} per initial 100** "
              f"(**{direct['explicit_fee_relative_wealth_loss']:.2%}** of no-fee wealth).", "",
              "## Cost reconciliation", "",
              f"The daily nominal cost ledger totals **{diagnostics['explicit_nominal_costs_per_initial_100']:.2f} "
              "per initial 100**. The terminal wealth effect is larger because removed capital "
              f"cannot compound. No-fee CAGR is **{diagnostics['nofee_annualized_return']:.2%}**, "
              f"so explicit costs reduce CAGR by **{diagnostics['fee_cagr_reduction_pp']:.2f} percentage points**. "
              "These three measures have different denominators and should not be conflated.", "",
              "## Audit limits", "",
              "- Yahoo adjusted prices and cash dividends are vendor proxies for official Arca closes/actions; "
              "the explicit-dividend return and adjusted-close benchmark need not coincide exactly.",
              "- XNYS is a holiday-calendar proxy for NYSE Arca. Calendar mismatches fail the run. "
              "Official discretionary disruptions/restatements are not inferred from price data.",
              "- FOMC archives reflect realized dates, not a vendor's historical publication/" 
              "cancellation timestamps. Emergency decisions and notation votes are excluded. "
              "The four-business-day scheduling criterion cannot be independently certified.",
              "- DFF uses final-vintage Fed Funds observations as a transparent rate proxy. DTB3 "
              "sensitivity uses a 91-day money-market-yield conversion from bank discount yield.",
              "- Ten-year history before the June 1, 2021 Index Base Date is a research extension. "
              "Full-history warmup seeds are explicit; official_inception_daily.csv resets published "
              "volatility seeds on May 3, 2021 and index NAV/quantity on June 1, 2021.",
              "- Fixed-exposure attribution measures direct economics. Sequential non-fee "
              "attribution depends on ordering; three-fee Shapley attribution removes ordering bias. "
              "Parameter/funding sensitivity separately recomputes policy feedback.", "",
              "## Resume wording", "",
              '"Built a source-audited Python reconstruction of a calendar-driven, volatility-controlled '
              'equity excess-return index; implemented quantity-based cost accounting, causal signals, '
              'Shapley fee attribution, historical/synthetic stress tests, and reproducible data provenance."', "",
              "Avoid claiming an exact replication without official level reconciliation.", ""]
    (output / "research_report.md").write_text("\n".join(lines))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--start", help="Comparison anchor; default last price minus ten years")
    parser.add_argument("--end", default=datetime.now().date().isoformat(), help="Exclusive data end date")
    parser.add_argument("--download-start", default="2014-01-01", help="Include warmup before comparison")
    parser.add_argument("--data-dir", type=Path, default=ROOT/"data")
    parser.add_argument("--output-dir", type=Path, default=ROOT/"results")
    parser.add_argument("--offline", action="store_true", help="Reuse validated frozen local input CSVs")
    parser.add_argument("--official-levels", type=Path, help="Optional official CSV: date,level")
    parser.add_argument("--vol-target", type=float, default=0.10)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    cfg = Config(vol_target=args.vol_target)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not args.offline:
        download_inputs(args.download_start, args.end, args.data_dir)
    market, metadata = load_inputs(args.data_dir, args.end, cfg)
    events = load_fomc(args.data_dir/"fomc_calendar.csv", args.end)
    # Deliberately demand sufficient calendar coverage, even if few meetings.
    coverage_path = args.data_dir/"fomc_coverage.json"
    coverage = json.loads(coverage_path.read_text())
    if pd.Timestamp(coverage["start"]) > market.index[0] or pd.Timestamp(coverage["end"]) < market.index[-1]:
        raise ValueError("FOMC calendar coverage does not cover the downloaded SPY sample; update it explicitly")
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
    historical, synthetic = stress_analysis(market, events, cfg, anchor)
    sensitivity = sensitivity_analysis(market, events, cfg, anchor)
    simplified = simplified_model(market, feat, cfg)
    simplified_nofee = simplified_model(market, feat, cfg, costs=False)
    simple_path = normalize_path(simplified.net_level, anchor)
    simple_summary = performance(simple_path, market.tbill_cash_return, True)
    simple_summary["Cumulative Fee Drag"] = 1-simple_path.iloc[-1]/normalize_path(simplified_nofee.net_level, anchor).iloc[-1]
    simplified_daily = simplified.join(signals[["tom_requested", "opex_date", "fomc_announcement"]])
    # A separate model actually honors official initialization dates. Its first
    # accounting row is June 1, 2021 NAV=100, with Q and zero costs set at base.
    feat_official = controlled_weights(market, signals, cfg, official_seeds=True)
    mask = market.index >= pd.Timestamp(cfg.index_base_date)
    if mask.sum() > 20 and pd.Timestamp(cfg.index_base_date) in market.index:
        inception = accounting(market.asset_er_return.loc[mask], feat_official.weight_close.loc[mask],
                               market.dcf.loc[mask], cfg)
        inception.join(feat_official[["allocation", "algorithmic_vol"]]).to_csv(output/"official_inception_daily.csv", index_label="date")
    else:
        inception = ledger
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
                   **direct}
    official_audit = {"status": "not performed: official GSEQTMX levels not supplied"}
    if args.official_levels:
        official_audit = audit_official(inception.net_level, args.official_levels, cfg.launch_date, output)
    exports = {"performance_summary.csv": summary, "equity_curves.csv": paths,
               "daily_audit_ledger.csv": market.join(feat).join(ledger.add_prefix("ledger_")),
               "drag_attribution.csv": attribution, "cost_shapley.csv": cost_shapley,
               "counterfactual_curves.csv": counterfactuals, "historical_stress.csv": historical,
               "synthetic_stress.csv": synthetic, "parameter_sensitivity.csv": sensitivity,
               "requested_approximation_daily.csv": simplified_daily,
               "requested_approximation_summary.csv": pd.DataFrame([simple_summary], index=["Requested approximation"])}
    for filename, data in exports.items():
        data.to_csv(output/filename, index_label=data.index.name or "date", float_format="%.12g")
    make_charts(paths, feat, ledger, attribution, cost_shapley, historical, output)
    write_report(summary, direct, historical, synthetic, diagnostics, metadata, output)
    manifest = {"executed_utc": datetime.now(timezone.utc).isoformat(), "config": asdict(cfg),
                "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                "data": metadata, "diagnostics": diagnostics, "official_audit": official_audit,
                "fomc_coverage": coverage, "methodology": METHODOLOGY_URL,
                "script_sha256": sha256(Path(__file__)),
                "input_sha256": {p.name: sha256(p) for p in args.data_dir.glob("*.csv")},
                "versions": {pkg: importlib.metadata.version(pkg) for pkg in
                             ("pandas", "numpy", "yfinance", "matplotlib", "seaborn", "exchange_calendars")},
                "limitations": ["Research extension before index base date", "NYSE calendar proxy for Arca",
                                "Final-vintage Yahoo/FRED data", "FOMC archive not point-in-time vendor history",
                                "Official disruptions, amendments and restatements not modeled"]}
    (output/"run_manifest.json").write_text(json.dumps(manifest, indent=2))
    (output/"diagnostics.json").write_text(json.dumps(diagnostics, indent=2))
    LOG.info("Comparison %s through %s", anchor.date(), market.index[-1].date())
    print(summary.to_string(float_format=lambda x: f"{x:.6f}"))
    LOG.info("Results written to %s", output)
    return summary


if __name__ == "__main__":
    main()
