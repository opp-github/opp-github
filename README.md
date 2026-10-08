# Goldman Sachs Equity TimeX research framework

A complete, executable Python research project implementing the supplied July 28,
2023 Equity TimeX methodology, plus the requested simplified approximation.
It downloads data, validates it, generates signals, runs quantity-based accounting,
attributes costs, stress-tests the policy and exports figures and audit tables.

**This is an independent research reconstruction, not verified official GSEQTMX
performance.** Official daily index levels were not available in the supplied
documents. A ten-year study also precedes the methodology's declared 2021 base
dates. An optional official-level reconciliation is implemented; exact agreement
should only be claimed after that check and any residual differences are resolved.

## Quick start

Python 3.12 or later. From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py --offline --end 2026-10-08
python -m pytest -q
```

The offline command reproduces the delivered snapshot. For the complete pinned transitive dependency snapshot, install `requirements-lock.txt` instead of `requirements.txt`. The executed validation suite passes **40 tests**. For a fresh data download:

```bash
python run.py --end 2026-10-08
```

The Yahoo end date is exclusive. The executed comparison is **7 October 2016 to
7 October 2026**, with 2014 onward data for warmup. A custom comparison anchor:

```bash
python run.py --offline --start 2020-01-02 --end 2026-10-08 --output-dir results_2020
```

The checked-in FOMC calendar is verified through 8 October 2026. Future runs fail
if price data exceed calendar coverage: extend the calendar using verified Fed
sources and update the coverage JSON explicitly. The calendar builder reproduces
the verified transcription; it does not scrape a future calendar automatically.

For official verification, supply a locally licensed CSV with columns `date,level`:

```bash
python run.py --offline --end 2026-10-08 --official-levels data/official_gseqtmx.csv
```

The command writes rebased level residuals, daily return bias, annualized tracking
error and separate pre-launch/live-period tracking errors when sufficient data exist.
The input is optional because no official daily series was supplied; there is no
placeholder or invented official index dataset in this project.

## Source hierarchy and model distinction

The supplied methodology is authoritative for the methodology-based path. The
brochure explains the product but does not replace its detailed formulas. The
documents' legal or operational text is treated as source content, not instructions
to the coding agent. The extracted methodology is audited in `methodology_audit.md`.

| Mechanism | Supplied official methodology | Requested approximation |
|---|---|---|
| Equity return | SPY Close plus cash dividends | SPY Close only, forfeiting dividends |
| Cash subtraction | Fed Funds, applied to equity notional | Converted 3-month T-bill yield proxy |
| Month signals | Conditional last-four-return-day MR; first-four-return-day long | Final 2 / first 3 trading-day flags exported |
| FOMC | Scheduled-only; close decisions at announcement minus 2 and minus 1 sessions | Actual announcement mask exported |
| OpEx | Holiday-adjusted third Friday; directional signal before expiry | Expiry-date flag exported |
| Additional signal | Five-day weighted mean reversion with 10-day vol scalar | Not added to the requested simple target formula |
| Volatility | Maximum of .94 and .97 EWMAs on signal-allocated ER portfolio | Maximum of 20/60-day SPY rolling sample vol |
| Allocation | 75% base + five signals, 0–150% range, 75pp daily change cap | Direct 10% / prior-day vol target |
| Final exposure | 150% cap and 50pp daily change cap | 0–150% direct weight cap |
| Servicing | 0.50% p.a. on equity notional | Flat 0.50% p.a. on NAV |
| Rebalancing | 1bp on absolute synthetic quantity turnover | 1bp on absolute weight change |
| Index deduction | 0.50% p.a. on previous net NAV | Same rate, flat NAV |
| Day count | Actual/360 | Actual/360 |

The simple model implements the specified daily targeting formula. Its event
flags are diagnostics; there is no undocumented event multiplier. The exact
methodology path uses all five actual signals.

## Causality and accounting

Every `weight_close[t]` is a decision at close t and earns the next session's
return. `weight_held[t]` is the preceding decision. The official volatility scalar
uses algorithmic volatility at t-1 when selecting the t-close lookthrough weight,
as stated in section 7. Signals using prices never access a future close.
Calendar windows use a padded, complete exchange calendar, so a truncated month
cannot be mistaken for month-end. Holiday OpEx shifts to the preceding session.

Section 11 asset return is:

```text
asset_ER_return[t] = (Close[t] + Dividend[t]) / Close[t-1] - 1
                     - FedFunds[t-1] * elapsed_calendar_days / 360
```

Dividends are included once. Excess return means financing is subtracted;
it does not mean the dividend yield is discarded. The adjusted-close SPY TR
benchmark remains separate from the cash-dividend convention.

The synthetic quantity ledger implements sections 2–6 literally:

```text
Gross[t] = Net[t-1] + Q[t-1] * (AssetER[t] - AssetER[t-1])
Q[t]     = Gross[t] * LookthroughWeight[t] / AssetER[t]
RC[t]    = abs(Q[t] - Q[t-1]) * AssetER[t] * 0.0001
SC[t]    = Q[t-1] * AssetER[t-1] * day_count_fraction[t] * 0.005
DC[t]    = Net[t-1] * day_count_fraction[t] * 0.005
Net[t]   = max(0, Gross[t] - RC[t] - SC[t] - DC[t])
```

Zero is absorbing. Quantity is selected using gross NAV before cost deductions.
Consequently the official capped weight is gross-normalized; realized notional
divided by net NAV may be slightly greater than 150% after fees. Both conventions
are exposed in the daily ledger. Benchmark incremental fee drag is zero; embedded
SPY expenses remain in the vendor prices and are not subtracted a second time.

The ten-year accounting comparison resets all portfolios to 100 with identical
initial policy weights at the anchor, avoiding contamination from pre-period fees.
Signal/vol states use the longer warmup. `official_inception_daily.csv` separately
resets the published volatility seeds on 3 May 2021 and initializes index NAV and
quantity on 1 June 2021. This dataset is an inception-rule reconstruction too,
not official observations.

## Attribution and statistics

The sequential bridge starts with SPY adjusted TR, isolates its difference from
explicit cash-dividend TR, then adds signal allocation, volatility control,
financing, servicing, transactions and index deduction. Terminal wealth changes
sum to the full benchmark-to-index difference. Non-fee sequential attribution is
order dependent and is labeled as such.

All direct drag counterfactuals keep baseline close weights fixed. This isolates
economics instead of mixing a fee/funding change with a different exposure policy.
The three explicit costs also use all eight fee coalitions to produce
order-independent Shapley attribution, including compounding and gross/net effects.
Sensitivity scenarios recompute the entire policy separately to capture feedback.

`Cumulative Fee Drag` is `1 - net_terminal_wealth / nofee_terminal_wealth`.
It is a percentage of no-fee terminal wealth, not percentage points of CAGR and
not the sum of daily charges. The ledger reports nominal charges separately;
the Shapley table reports lost terminal wealth per initial 100.

CAGR uses elapsed calendar years (365.25 days). Annualized volatility uses
daily sample standard deviation times sqrt(252). Sharpe uses daily mean excess
return / daily excess-return standard deviation times sqrt(252). The index return
is already excess, so its cash comparator is zero; SPY benchmarks subtract the
same DFF cash accrual. Maximum drawdown includes the initial 100 anchor.

## Outputs

| File under `results/` | Purpose |
|---|---|
| `timex_dashboard.png`, `.svg` | Required log equity comparison and dual-axis exposure/market-volatility panel |
| `drag_attribution.png` | Sequential economics and three-cost Shapley attribution |
| `performance_summary.csv` | All three requested paths and performance/fee metrics |
| `equity_curves.csv` | Identically rebased daily comparison paths |
| `daily_audit_ledger.csv` | Input prices/actions/rates, masks, signal weights, volatility states, quantities and daily charges |
| `drag_attribution.csv`, `counterfactual_curves.csv` | Telescoping wealth attribution and daily counterfactual paths |
| `cost_shapley.csv` | Order-independent explicit cost attribution |
| `historical_stress.csv` | COVID selloff/rebound, 2022 tightening, April 2025 selloff |
| `synthetic_stress.csv` | Seeded -20% gap, crash/rebound and low-volatility chop |
| `parameter_sensitivity.csv` | Target/cap/EWMA/signal/funding and March 2020 calendar sensitivity |
| `requested_approximation_daily.csv`, `_summary.csv` | Requested simplified model and masks, separately labeled |
| `official_inception_daily.csv` | Published initialization conventions, from index base date |
| `run_manifest.json`, `diagnostics.json` | Configuration, data ranges, hashes, software versions, limitations and accounting diagnostics |
| `research_report.md` | Executed results, three interview insights and resume wording |

Synthetic scenarios use seed 17 and are explicitly separated from downloaded
historical observations. No synthetic input replaces historical market data.

## Reproducibility and audit limits

The frozen inputs are included under `data/`. The download manifest records
provider links, retrieval timestamps and hashes; the run manifest hashes the actual
input files and executed script. Network downloads retry at most three times and
fail loudly. Missing trading sessions, missing actions, nonpositive/nonfinite
prices, unhandled splits, future backfilling and cash rates stale by more than
seven days all trigger errors. The project has substantive offline tests and a
CI workflow; tests do not require Yahoo or FRED availability.

Remaining limits are material: XNYS is a holiday proxy for NYSE Arca; final-vintage
Yahoo and FRED are not a point-in-time commercial feed; historical FOMC archives
do not certify every publication/cancellation timestamp; official discretionary
disruption, adjustment and restatement decisions are absent. In particular,
cancelled 18 March 2020 is an explicit alternative calendar scenario. The supplied
methodology is a July 2023 version; later amendments are not presumed absent.

Primary sources:

- [Goldman Sachs index methodology catalogue](https://www.goldmansachs.com/what-we-do/ficc-and-equities/products-and-business-groups/products/index-methodologies)
- [Goldman Sachs GSEQTMX product page](https://www.goldmansachsindices.com/products/GSEQTMX)
- [Fed historical/calendar source audit](data/fomc_sources.md)
- [FRED DFF](https://fred.stlouisfed.org/series/DFF)
- [FRED DTB3, discount basis](https://fred.stlouisfed.org/series/DTB3)
- [yfinance download documentation](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)

Use the resume wording in the report. Avoid stating that this code is Goldman
Sachs-certified or that any structured note's payoff is reproduced: product
participation rates, caps, credit risk and contract terms are separate layers.
