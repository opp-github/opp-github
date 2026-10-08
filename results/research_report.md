# Equity TimeX research results

Executed comparison: **2016-10-07 to 2026-10-07**.

Independent research reconstruction using the supplied July 28, 2023 methodology. Official published levels were not supplied unless indicated in run_manifest.json; results must not be presented as verified GSEQTMX performance.

## Performance

| Strategy | CAGR | Volatility | Sharpe | Max drawdown | Fee wealth loss |
|---|---:|---:|---:|---:|---:|
| TimeX research reconstruction | 6.75% | 10.04% | 0.70 | -19.32% | 10.80% |
| SPY Price Return | 13.71% | 18.01% | 0.67 | -34.10% | 0.00% |
| SPY Total Return | 15.52% | 17.95% | 0.76 | -33.72% | 0.00% |

Sharpe convention: an ER-index return is already excess and is compared with zero; SPY price and total returns subtract the same DFF accrual. CAGR uses elapsed calendar years; daily volatility uses 252 observations/year. Fee wealth loss is a percentage of the no-explicit-fee terminal wealth under fixed baseline exposure. SPY's embedded fund expense is already in prices, not an additional modeled fee.

## Three interview talking points

1. **Excess return retains dividends.** Official section 11 adds cash dividends and subtracts Fed Funds financing on the entire equity exposure; unused exposure earns no modeled cash. Forfeiting dividends at the same baseline exposures would reduce terminal wealth by **22.93 per initial 100**. This is a counterfactual, not an official index charge. Financing itself costs **33.93 per initial 100** in the fixed-exposure comparison.

2. **Volatility control can miss the recovery.** During the COVID rebound window (2020-03-23 to 2020-08-31), the reconstruction returned **10.51%** versus **57.37%** for SPY TR, with mean held exposure **27.5%**. Taking the maximum of two EWMAs speeds de-risking while retaining the slower measure during normalization. This is historical evidence for this interval, not proof of future underperformance.

3. **Target volatility is not a guarantee, and fees depend on implementation.** Sample realized volatility is **10.04%**, against a 10% target. The synthetic -20% overnight gap starts with **131.3%** exposure and loses **26.28%** on the first shock day before de-risking. Servicing is notional-weighted and transaction cost uses synthetic quantity changes: the three explicit costs reduce terminal wealth by **23.25 per initial 100** (**10.80%** of no-fee wealth).

## Cost reconciliation

The daily nominal cost ledger totals **17.97 per initial 100**. The terminal wealth effect is larger because removed capital cannot compound. No-fee CAGR is **7.97%**, so explicit costs reduce CAGR by **1.23 percentage points**. These three measures have different denominators and should not be conflated.

## Audit limits

- Yahoo adjusted prices and cash dividends are vendor proxies for official Arca closes/actions; the explicit-dividend return and adjusted-close benchmark need not coincide exactly.
- XNYS is a holiday-calendar proxy for NYSE Arca. Calendar mismatches fail the run. Official discretionary disruptions/restatements are not inferred from price data.
- FOMC archives reflect realized dates, not a vendor's historical publication/cancellation timestamps. Emergency decisions and notation votes are excluded. The four-business-day scheduling criterion cannot be independently certified.
- DFF uses final-vintage Fed Funds observations as a transparent rate proxy. DTB3 sensitivity uses a 91-day money-market-yield conversion from bank discount yield.
- Ten-year history before the June 1, 2021 Index Base Date is a research extension. Full-history warmup seeds are explicit; official_inception_daily.csv resets published volatility seeds on May 3, 2021 and index NAV/quantity on June 1, 2021.
- Fixed-exposure attribution measures direct economics. Sequential non-fee attribution depends on ordering; three-fee Shapley attribution removes ordering bias. Parameter/funding sensitivity separately recomputes policy feedback.

## Resume wording

"Built a source-audited Python reconstruction of a calendar-driven, volatility-controlled equity excess-return index; implemented quantity-based cost accounting, causal signals, Shapley fee attribution, historical/synthetic stress tests, and reproducible data provenance."

Avoid claiming an exact replication without official level reconciliation.
