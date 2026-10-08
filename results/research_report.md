# Equity TimeX research results

Executed comparison: **2016-10-07 to 2026-10-07** (2,512 daily returns after 697 warm-up sessions).

> **Status.** Independent research reconstruction of the Goldman Sachs Equity TimeX Index (GSEQTMX) from the supplied methodology dated 28 July 2023. Official daily GSEQTMX levels were not available, so these results are **not** verified official index performance. Before the methodology's 2021 base dates the series is a research extension.

## Performance

| Strategy | Annualized Return | Annualized Volatility | Sharpe Ratio | Maximum Drawdown | Terminal Wealth per 100 | Cumulative Fee Drag |
|---|---:|---:|---:|---:|---:|---:|
| TimeX research reconstruction | 6.75% | 10.04% | 0.703 | -19.32% | 192.08 | 10.80% |
| SPY Price Return | 13.71% | 18.01% | 0.668 | -34.10% | 361.43 | 0.00% |
| SPY Total Return | 15.52% | 17.95% | 0.758 | -33.72% | 423.22 | 0.00% |

Conventions: CAGR uses elapsed calendar years (365.25 days); volatility is the daily sample standard deviation times sqrt(252); maximum drawdown includes the starting value. The ER index return is already an excess return, so its Sharpe ratio uses a zero comparator; SPY price and total returns subtract the same DFF cash accrual. Cumulative fee drag is 1 - net terminal wealth / no-explicit-fee terminal wealth under the same exposures. SPY's embedded fund expense is already in its prices and is not charged again.

The requested simplified approximation is a different model, not the index:

| Model | Annualized Return | Annualized Volatility | Sharpe Ratio | Maximum Drawdown | Terminal Wealth per 100 | Cumulative Fee Drag |
|---|---:|---:|---:|---:|---:|---:|
| Requested approximation | 5.72% | 9.74% | 0.621 | -16.26% | 174.32 | 10.00% |

## Consistency with figures Goldman Sachs published

The methodology prints the EWMA volatilities used to initialize the index on 3 May 2021, and the methodology and brochure print backtested returns and cost rates. These were produced by GS with its own data, so they test the implemented conventions without certifying the series.

| Anchor | Published (%) | Reconstruction (%) | Difference (pp) |
|---|---:|---:|---:|
| 0.94 EWMA vol, 3 May 2021 | 12.0433 | 12.0433 | +0.0000 |
| 0.97 EWMA vol, 3 May 2021 | 14.6234 | 14.6207 | -0.0027 |
| 1Y return to 28 Jul 2023 | 2.15 | 2.5215 | +0.3715 |
| 3Y annualized return | 5.32 | 5.2157 | -0.1043 |
| 5Y annualized return | 6.02 | 6.0694 | +0.0494 |
| Servicing + rebalancing, rolling 1Y average | 0.61 | 0.6373 | +0.0273 |
| Servicing + rebalancing, rolling 1Y high | 0.92 | 0.9264 | +0.0064 |

Convention fingerprint (each row recomputes the full policy):

| Convention | Anchors Contradicting | Verdict |
|---|---|---|
| Methodology baseline | none | methodology |
| Dividends forfeited (Close-only ER) | 0.94 EWMA vol, 3 May 2021; 0.97 EWMA vol, 3 May 2021; 3Y annualized return; 5Y annualized return | contradicted by published anchors |
| Yahoo Adj Close total return in ER | none | not distinguishable at anchor precision |
| DTB3 money-market funding | none | not distinguishable at anchor precision |
| Same-day Fed Funds (look-ahead) | none | not distinguishable at anchor precision |
| ACT/365 financing accrual | none | not distinguishable at anchor precision |
| ToM-MR reference EOM-4 (brochure wording) | 3Y annualized return; 5Y annualized return | contradicted by published anchors |
| Same-day volatility in target (VC_t) | none | not distinguishable at anchor precision |
| EWMA on unallocated SPY ER | 0.94 EWMA vol, 3 May 2021; 0.97 EWMA vol, 3 May 2021; 1Y return to 28 Jul 2023; 3Y annualized return; 5Y annualized return | contradicted by published anchors |
| Servicing on NAV, not notional | 3Y annualized return; 5Y annualized return; Servicing + rebalancing, rolling 1Y average | contradicted by published anchors |

Contradicted: Dividends forfeited (Close-only ER), ToM-MR reference EOM-4 (brochure wording), EWMA on unallocated SPY ER, Servicing on NAV, not notional. Not distinguishable at the printed precision: Yahoo Adj Close total return in ER, DTB3 money-market funding, Same-day Fed Funds (look-ahead), ACT/365 financing accrual, Same-day volatility in target (VC_t). The flag rule (error above three times the larger of the baseline's own family residual and the rounding half-unit) is a declared heuristic, not a statistical test.

## Fee and financing economics

| Measure | Unit | Total | Index deduction | Servicing | Rebalancing |
|---|---|---:|---:|---:|---:|
| Sum of nominal daily charges | per initial 100 | 17.9721 | 8.0182 | 5.2299 | 4.7240 |
| Compounded terminal wealth lost (Shapley) | per initial 100 | 23.2461 | 10.3145 | 6.8675 | 6.0641 |
| Relative terminal-wealth loss | % of no-fee terminal wealth | 10.7959 | n/a | n/a | n/a |
| CAGR reduction | percentage points | 1.2267 | n/a | n/a | n/a |
| Mean annual nominal charge rate | % of prior net NAV per year | 1.1432 | 0.5073 | 0.3377 | 0.2982 |

These measures have different denominators and are not interchangeable. Removed capital cannot compound, so compounded terminal wealth lost exceeds the sum of nominal charges. The Shapley split averages all six orderings of the three costs and sums exactly to the total.

Direct fixed-exposure counterfactuals: Fed Funds financing costs **33.93** per initial 100; a hypothetical forfeiture of dividends would cost a further **22.93**. The sequential bridge reconciles to **-2.8e-14** (floating-point noise).

## Three interview talking points

1. **Excess return keeps dividends; financing and dividend forfeiture are separate mechanisms.** Methodology section 11 adds the cash dividend to the price return and subtracts Fed Funds on the equity notional. At fixed exposures financing costs **33.93** per initial 100, while forfeiting dividends would cost **22.93**. GS's own published numbers support the dividend-inclusive reading. The reconstructed 0.94 EWMA on 3 May 2021 is **12.0433%** against the printed 12.0433%. A Close-only ER gives 12.1009%, and its 5Y annualized return (4.99%) misses the brochure's 6.02% by about a point a year; the methodology path gives 6.07%.

2. **Volatility control lags the recovery.** From 2020-03-23 to 2020-08-31 the reconstruction returned **10.51%** against **57.37%** for SPY TR, with mean held exposure of **27.5%**. Taking the maximum of the 0.94 and 0.97 EWMAs de-risks quickly and re-risks only as the slower estimate decays. This is evidence about this interval, not proof of future underperformance.

3. **A 10% volatility target is not a loss limit, and the timing makes gaps worse.** In the seeded synthetic -20% overnight gap the index held **131.3%** exposure and lost **26.28%** on the shock day. The weight chosen at that close uses volatility through the previous day (VC at t-1), so the shock has not yet entered the estimate. Exposure for the next session *rose* to **138.5%**, helped by the mean-reversion signal. After that the 50pp daily limit allowed only stepwise de-risking: exposure first fell below 50% 2 sessions after the shock. A continuation shock on the next day would have hit at above 100% exposure.

## Stress and sensitivity

Historical windows (close to close):

| Window | Index Return | SPY TR Return | Index Maximum Drawdown | Mean Held Exposure | Minimum Held Exposure |
|---|---:|---:|---:|---:|---:|
| COVID selloff | -9.30% | -33.72% | -9.60% | 37.6% | 0.2% |
| COVID rebound | 10.51% | 57.37% | -1.76% | 27.5% | 9.4% |
| 2022 tightening | -12.43% | -18.18% | -13.76% | 41.8% | 0.0% |
| April 2025 selloff | -7.71% | -12.05% | -7.71% | 44.7% | 16.3% |

Synthetic scenarios (seed 17, 120-session quiet history, real 2019 session dates; synthetic results never replace historical data):

| Scenario | Index Return | SPY Price Return | Maximum Drawdown | Exposure Before Shock | Exposure After Shock Close | First Shock Index Return |
|---|---:|---:|---:|---:|---:|---:|
| Overnight -20% gap | -26.51% | -20.00% | -26.51% | 131.3% | 138.5% | -26.28% |
| Crash then rapid rebound | -14.91% | 2.32% | -18.49% | 131.3% | 140.3% | -3.30% |
| Low-volatility chop | -0.71% | -0.03% | -1.22% | 131.3% | 126.7% | 0.51% |

Parameter and policy sensitivities (full policy recomputed for each row):

| Scenario | Annualized Return | Annualized Volatility | Sharpe Ratio | Maximum Drawdown | Terminal Wealth per 100 | Mean Held Exposure |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | 6.75% | 10.04% | 0.703 | -19.32% | 192.08 | 67.8% |
| Vol target 8% | 5.55% | 8.13% | 0.707 | -15.99% | 171.67 | 55.4% |
| Vol target 12% | 7.91% | 11.76% | 0.708 | -22.51% | 214.04 | 79.1% |
| No leverage above 100% | 7.10% | 9.54% | 0.770 | -16.28% | 198.63 | 67.1% |
| Fast EWMA only | 7.20% | 10.74% | 0.704 | -19.68% | 200.50 | 73.4% |
| Slow EWMA only | 7.10% | 10.46% | 0.711 | -20.37% | 198.58 | 69.2% |
| No calendar / MR signals | 6.96% | 10.00% | 0.725 | -16.73% | 195.91 | 74.7% |
| Funding +200 bp (policy recomputed) | 5.46% | 10.05% | 0.581 | -20.03% | 170.13 | 67.8% |
| Include planned 18 Mar 2020 FOMC | 6.72% | 10.04% | 0.700 | -19.32% | 191.61 | 67.8% |
| DTB3 funding proxy (policy recomputed) | 6.75% | 10.04% | 0.703 | -19.26% | 192.10 | 67.8% |

## Data and convention checks

- **Dividend convention.** Explicit cash-dividend TR wealth is 423.5071 against Yahoo Adj Close 423.2227 (0.0672%). Yahoo's ex-date return equals Close_t / (Close_t-1 - D) - 1 to within 0.007 bp, which is reinvestment at the prior close. That convention explains 99.95% of the gap. The largest daily difference is 2.5221 bp on 2020-03-20.
- **Short-term mean-reversion cap.** The ±10% clip never binds in this sample (maximum |signal| 7.01%). By Cauchy-Schwarz the signal is bounded near 7.39% whenever log(1+r) is close to r.
- **FOMC look-ahead coverage.** The decision on 2026-10-07 needs the scheduled calendar through 2026-10-09; certified by next scheduled announcement 2026-10-28 lies beyond the look-ahead.
- **Inception reconstruction.** Resetting the published seeds on 3 May 2021 and NAV on 1 June 2021 leaves the end level within -0.0002 (per 100 at the index base date) of the extended-history path, so the initialization is forgotten.

## Limitations

- The ten-year study precedes the 1 June 2021 index base date and the 28 July 2023 launch date.
- Yahoo prices/actions and FRED final-vintage DFF and DTB3 are proxies, not the calculation agent's inputs; DTB3 is converted from bank-discount to money-market yield with a 91-day tenor.
- XNYS is a holiday-calendar proxy for NYSE Arca. Calendar mismatches fail the run.
- The FOMC archive gives final calendars, not a vendor's point-in-time schedule. Unscheduled statements and notation votes are excluded, and the four-business-day scheduling test cannot be certified historically. 18 March 2020 is a sensitivity case.
- Discretionary market disruption, adjustment and restatement decisions are not reproduced.
- The methodology is the July 2023 version; later amendments are not presumed absent.
- An index reconstruction does not reproduce a structured note's participation rate, caps, fees, or issuer credit exposure.

## Resume wording

"Built a source-audited Python reconstruction of a calendar-driven, volatility-controlled equity excess-return index (Goldman Sachs Equity TimeX methodology). Implemented quantity-based cost accounting, causal signal timing, Shapley fee attribution, and historical and synthetic stress tests. Corroborated the implementation against the index's published initialization volatilities (0.94 EWMA matched to 4 d.p.) and brochure backtest statistics."

Do not describe this as an exact or official replication without an official-level reconciliation.
