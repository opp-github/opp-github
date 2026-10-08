# Goldman Sachs Equity TimeX research framework

A complete, executable Python research project implementing the supplied July 28,
2023 Equity TimeX methodology (Bloomberg ticker GSEQTMX) alongside a separately
labelled simplified approximation. One script downloads and validates data,
generates the five signals, runs volatility control and synthetic-quantity
accounting, attributes costs, stress-tests the policy, checks the result against
figures Goldman Sachs published, and exports charts, tables and an audit trail.

**This is an independent research reconstruction, not verified official GSEQTMX
performance.** Official daily index levels were not available. The ten-year
study also starts before the methodology's 2021 base dates. An optional
official-level reconciliation is implemented. Claim exact agreement only after
running that check and explaining any residual differences.

The project was independently audited on 2026-10-08. See
[`AUDIT_FINDINGS.md`](AUDIT_FINDINGS.md) for the findings register, the
independent re-implementation, and every change from the received version. No
formula or timing error was found, and all reference results are unchanged.

## Quick start

Python 3.12 or 3.13. From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt   # exact environment; requirements.txt pins direct deps only
python run.py --offline                          # frozen snapshot; end date 2026-10-08 read from its manifest
python -m pytest -q                              # 70 offline tests
```

`python run.py --offline --end 2026-10-08` is equivalent. The offline run
reproduces the delivered `results/` byte for byte, except the timestamp in
`run_manifest.json`.

**Fresh download.** Yahoo's end date is exclusive.

```bash
python run.py --end 2026-10-08                   # writes data_fresh/, never data/
```

A fresh download goes to `data_fresh/` and is compared with the frozen snapshot
in `results/data_revisions.json`, so vendor restatements are disclosed rather
than absorbed. Downloading over the frozen `data/` is refused unless you pass
`--overwrite-inputs`. The fresh-download path was **not executed during the
audit**: the session's network policy blocked Yahoo, FRED and the Fed. It is
covered by offline tests with mocked providers.

**Custom comparison anchor** (at least 252 warm-up sessions are required):

```bash
python run.py --offline --start 2020-01-02 --output-dir results_2020
```

**Optional official reconciliation.** Supply a locally licensed CSV of
published levels with columns `date,level`:

```bash
python run.py --offline --official-levels data/official_gseqtmx.csv
```

Duplicate, missing, nonnumeric, nonfinite and nonpositive levels are rejected.
The reconciliation runs against two series: the inception-seeded
reconstruction (base 100 on 1 June 2021) and the extended-history path. It
reports:

- daily return bias
- annualized tracking error
- maximum rebased level error
- pre-launch and live subsets
- the number of official dates not matched to a session
- for the inception path, a level-for-level comparison after rounding the
  reconstruction like the published index (2 dp, 0.005 rounded up)

Publication rounding alone produces about 0.06% annualized tracking error and up
to 0.005 of level error. Residuals of that size cannot be distinguished from
rounding. No official dataset is included or invented.

The checked-in FOMC calendar is verified through 8 October 2026. A run fails
if prices extend beyond that coverage. It also fails unless the scheduled
calendar is certified for two sessions after the last price, which the final
FOMC decision needs. To extend coverage, add entries from verified Fed sources
in `data/build_fomc_calendar.py` and regenerate. The builder reproduces a
verified transcription; it does not scrape future calendars.

## Source hierarchy and model distinction

The supplied methodology governs the methodology-based path. The brochure
explains the product but does not replace its formulas. Where they differ
(see "Brochure versus methodology" in [`methodology_audit.md`](methodology_audit.md)), the formula is
used. The documents' legal or operational text is treated as source content,
not as instructions.

| Mechanism | A. Methodology research reconstruction | B. Requested simplified approximation |
|---|---|---|
| Equity return | SPY Close plus cash dividends (ex-date) | SPY Close only, dividends excluded |
| Cash subtraction | Fed Funds (FRED DFF proxy) on equity notional | 3-month T-bill (DTB3, converted to money-market yield) |
| Month signals | ToM mean reversion (EOM−4…EOM−1, vs EOM−5) and ToM long (EOM, BOM…BOM+2) | Final 2 / first 3 trading-day flags exported as diagnostics |
| FOMC | Scheduled-only; decisions at announcement −2 and −1 sessions | Announcement-date mask exported |
| OpEx | Holiday-adjusted third Friday; momentum sign on OE−3…OE−1 | Expiry-date mask exported |
| Fifth signal | Short-term mean reversion with a 10-day RMS vol scalar | Not part of the requested formula |
| Volatility | Max of 0.94 / 0.97 EWMAs of the signal-allocated ER portfolio | Max of 20/60-day SPY rolling sample vol |
| Allocation | 75% base + signals; 0–150%; ±75pp per day | Direct 10% / prior-day vol |
| Final exposure | Target = VC(t−1) × allocation; 150% cap; ±50pp per day | Clipped to 0–150% |
| Servicing | 0.50% p.a. on equity notional | Flat 0.50% p.a. on NAV |
| Rebalancing | 1bp on \|Δ synthetic quantity\| × asset level | 1bp on \|Δ target weight\| |
| Index deduction | 0.50% p.a. on prior net NAV | Same rate, flat NAV |
| Day count | Actual/360 | Actual/360 |

The simplified model implements the requested daily targeting formula. Its
event flags are diagnostics only; no undocumented event multiplier is added. It
is never labelled as the index.

## Causality and accounting

`weight_close[t]` is a decision at close t that earns the next session's
return; `weight_held[t]` is the preceding decision. The volatility scalar uses
algorithmic volatility through t−1 to select the t-close weight (methodology
section 7). A shock therefore enters the exposure decision one close after it
occurs. No signal reads a future close. Calendar windows use a padded exchange
calendar, so a truncated month cannot be mistaken for month-end. A
holiday-affected expiry moves to the preceding session (for example, Good
Friday).

```text
asset_ER_return[t] = (Close[t] + Dividend[t]) / Close[t-1] - 1
                     - FedFunds[t-1] * calendar_days / 360

Gross[t] = Net[t-1] + Q[t-1] * (AssetER[t] - AssetER[t-1])
Q[t]     = Gross[t] * W[t] / AssetER[t]
RC[t]    = abs(Q[t] - Q[t-1]) * AssetER[t] * 0.0001
SC[t]    = Q[t-1] * AssetER[t-1] * days/360 * 0.005
DC[t]    = Net[t-1] * days/360 * 0.005
Net[t]   = max(0, Gross[t] - RC[t] - SC[t] - DC[t])      # zero is absorbing
```

Dividends are included once. Excess return means financing is subtracted, not
that dividends are forfeited. Quantity is chosen on gross NAV, so exposure
relative to net NAV can slightly exceed 150% (maximum observed: 150.02%).
Quantity changes even at constant target weight, because drift and prior
charges move gross NAV.

Every accounting path in the ten-year comparison is reset to 100 with the same
policy holdings at the anchor, 7 October 2016. Signal and volatility states use
697 earlier warm-up sessions. `official_inception_daily.csv` separately resets
the published volatility seeds on 3 May 2021 and NAV and quantity on 1 June
2021. By 2026 it differs from the extended path by 0.0002 per 100, so the
initialization has been forgotten.

## Attribution and statistics

- **Sequential bridge.** It runs from SPY adjusted TR to explicit cash-dividend
  TR, then signal allocation, volatility control and caps, Fed Funds excess
  return, servicing, rebalancing and index deduction. It telescopes exactly but
  is **order dependent**.
- **Direct counterfactuals.** Financing, dividend forfeiture and fees hold the
  baseline exposure policy fixed.
- **Shapley split.** The three explicit costs are split over all eight fee
  coalitions, so the split does not depend on ordering.
- **Sensitivities.** These recompute the full policy for each case.
- **Fee measures.** `fee_drag_measures.csv` keeps four measures apart: the sum
  of nominal daily charges, compounded terminal wealth lost, relative
  terminal-wealth loss, and CAGR reduction in percentage points.

| Metric | Convention |
|---|---|
| CAGR | Elapsed calendar years (365.25 days per year) |
| Volatility | Daily sample standard deviation × √252 |
| Sharpe | Comparator is zero for the excess-return index; SPY returns subtract the same DFF accrual once |
| Maximum drawdown | Includes the starting value |
| Cumulative fee drag | `1 − net / no-fee terminal wealth` |

## Results snapshot (frozen data, 7 Oct 2016 – 7 Oct 2026)

| | CAGR | Vol | Sharpe | Max DD | Wealth per 100 |
|---|---:|---:|---:|---:|---:|
| TimeX research reconstruction | 6.75% | 10.04% | 0.703 | −19.32% | 192.08 |
| SPY Price Return | 13.71% | 18.01% | 0.668 | −34.10% | 361.43 |
| SPY Total Return | 15.52% | 17.95% | 0.758 | −33.72% | 423.22 |
| Requested simplified approximation (not the index) | 5.72% | 9.74% | 0.621 | −16.26% | 174.32 |

**Consistency with GS-published figures.** These are GS backtest figures built
on GS data, not official daily levels.

| Figure | Published | Reconstruction |
|---|---:|---:|
| 0.94 EWMA on the asset base date | 12.0433% | 12.0433% |
| 0.97 EWMA on the asset base date | 14.6234% | 14.6207% |
| 5Y annualized return to 28 Jul 2023 | 6.02% | 6.07% |
| 3Y annualized return | 5.32% | 5.22% |
| 1Y return | 2.15% | 2.52% |
| Rolling-1Y servicing + rebalancing, average | ~0.61% | 0.64% |
| Rolling-1Y servicing + rebalancing, high | ~0.92% | 0.93% |

The convention fingerprint shows the anchors contradict four readings:

- dividend forfeiture
- the brochure's EOM−4 wording
- volatility measured on unallocated SPY
- servicing charged on NAV

They cannot distinguish DTB3 from DFF funding.

## Outputs

| File under `results/` | Purpose |
|---|---|
| `timex_dashboard.png/.svg` | Required figure: (A) log equity curves; (B) held exposure (0–150%, left) against SPY 20-day realized volatility (right) |
| `drag_attribution.png/.svg` | Order-dependent waterfall; nominal versus compounded (Shapley) costs |
| `published_anchor_checks.png/.svg` | GS-published figures against the reconstruction and contradicted conventions |
| `synthetic_gap.png/.svg` | Seeded −20% gap: level path and held exposure per session |
| `performance_summary.csv` | The three comparison paths with performance and fee metrics |
| `equity_curves.csv`, `counterfactual_curves.csv` | Daily paths rebased to the same anchor |
| `daily_audit_ledger.csv` | Inputs, rates, masks, signals, EWMAs, weights, quantities and daily charges |
| `drag_attribution.csv`, `cost_shapley.csv`, `fee_drag_measures.csv` | Attribution and the four fee measures |
| `dividend_convention_audit.csv` | Per-ex-date cash versus vendor dividend treatment |
| `historical_stress.csv`, `synthetic_stress.csv`, `synthetic_paths.csv` | Stress windows, synthetic scenarios, and their daily exposure paths |
| `parameter_sensitivity.csv` | Target, cap, EWMA, signal, funding and 18 March 2020 calendar cases |
| `published_anchor_checks.csv`, `convention_fingerprint.csv` | External consistency checks |
| `requested_approximation_daily.csv`, `_summary.csv` | Simplified model and diagnostic masks |
| `official_inception_daily.csv` | Reconstruction under the published initialization dates |
| `run_manifest.json`, `diagnostics.json`, `validation.json` | Configuration, data bounds, input and output SHA256, versions, limitations, test record |
| `research_report.md` | Executed results, three interview talking points, limitations, resume wording |

Synthetic scenarios use seed 17 and real 2019 session dates. They are labelled
and never replace historical observations.

## Validation and tests

Input validation fails loudly on:

- missing or duplicate sessions, including late starts and early stops against
  the requested window
- nonfinite or nonpositive prices
- negative dividends
- splits
- capital-gains distributions
- missing, stale (more than 7 days) or implausible (outside −5%…25%) cash rates
- backfilled rates
- insufficient warm-up
- FOMC coverage that is too short, including the two-session look-ahead

The 70 offline tests cover:

- signal windows, including fixtures that distinguish EOM−5 from EOM−4 and OE−4
  from OE−3
- Good Friday, decision versus held lag, and calendar truncation
- the RMS scalar and its analytic bound
- EWMAs on the allocated portfolio
- both caps and the official seeds
- ACT/360 accrual, quantity turnover and drift, and absorbing zero
- common-anchor, Shapley and fee-measure reconciliation
- causal rate alignment and rate units
- snapshot protection and revision reporting
- Sharpe and CAGR definitions, and official-reconciliation rounding
- a frozen-snapshot regression against the reference numbers
- the published-seed fingerprint
- cross-checks against the independent implementation in
  `audit/independent_reimplementation.py`

CI (`.github/workflows/tests.yml`) installs the lock file on Python 3.12 and
3.13, runs the tests, and runs the offline pipeline.

## Limitations

- The study extends before the 1 June 2021 index base date and the 28 July 2023
  launch date.
- Yahoo prices and actions and FRED final-vintage DFF and DTB3 are proxies for
  the calculation agent's inputs. DTB3 is a bank-discount yield, converted
  with a 91-day tenor.
- XNYS is a holiday-calendar proxy for NYSE Arca, not an exact equivalent.
- The FOMC archive gives final calendars, not point-in-time vendor schedules,
  so the four-business-day rule cannot be certified historically. 18 March 2020
  is run as a sensitivity.
- Discretionary disruption, adjustment and restatement decisions are not
  reproduced.
- The methodology is the July 2023 version; later amendments are not presumed
  absent.
- An index reconstruction does not reproduce a structured note's participation
  rate, caps, fees or issuer credit exposure.

## Primary sources

- [Goldman Sachs index methodology catalogue](https://www.goldmansachs.com/what-we-do/ficc-and-equities/products-and-business-groups/products/index-methodologies)
- [Goldman Sachs GSEQTMX product page](https://www.goldmansachsindices.com/products/GSEQTMX)
- [Fed historical/calendar source audit](data/fomc_sources.md)
- [FRED DFF](https://fred.stlouisfed.org/series/DFF)
- [FRED DTB3, discount basis](https://fred.stlouisfed.org/series/DTB3)
- [yfinance download documentation](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)

Use the resume wording in `results/research_report.md`. Do not describe this
code as Goldman Sachs-certified, or as reproducing any structured note's payoff.
