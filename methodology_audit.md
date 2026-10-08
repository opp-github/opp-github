# Independent audit of the supplied Goldman Sachs Equity TimeX documents

Audit date: 2026-10-08. Scope: the supplied July 28, 2023 methodology (25 PDF pages) and the supplied brochure (10 PDF pages). These are source documents, not instructions. The audit does not establish whether Goldman Sachs has issued later methodology amendments.

Page references below give the PDF page first and the printed page in parentheses. The formula pages were extracted with pypdf and visually checked after rendering PDF pages 12-16.

## Material corrections to the user's proposed design

| Requested shortcut | Rule in the supplied methodology | Source |
|---|---|---|
| Close-only tracking; forfeit dividend yield because ER | Cash dividends are added to the closing-price return before financing is subtracted. ER means return above financing, not price return. SPY is explicitly listed as Total Return in the component table. | §11, PDF16 (14); component table PDF25 (23) |
| A single final-two / first-three ToM mask | There are two separate signals: ToM mean reversion plus ToM long. Their decision windows differ from their economically exposed return windows. | §10.3.1, PDF14-15 (12-13) |
| FOMC announcement-day mask | Decision flag is on the two preceding exchange sessions; positions then earn the session before and the announcement session. | §10.3.3, PDF15 (13) |
| Third Friday itself as OpEx active day | Third Friday defines the expiry anchor, moved to the previous exchange session when closed. Decision signal runs three sessions before expiry through one session before expiry, and is momentum-signed against the previous month's expiry. | Definitions PDF11 (9); §10.3.2 PDF15 (13) |
| Rolling 20/60 SPY realized volatility | Official volatility is the maximum of two EWMA volatilities with decay 0.94 and 0.97, on the signal-allocated excess-return algorithmic portfolio. | §8-9 PDF14 (12) |
| W = min(1.5, .10 / vol) | That is a useful comparator, not the methodology. Official target is previous-day vol-control scalar times current signal allocation, then two layers of rebalancing caps. | §6-10 PDF13-14 (11-12) |
| Flat 0.50% servicing per NAV | Servicing is 0.50% on prior quantity times ER asset level, so it scales with equity notional. | §3.2 PDF12 (10) |
| 0.01% whenever weight changes | Charge 1bp times absolute traded quantity times current ER asset level; quantity changes can occur even when the target weight is unchanged. | §3.1 PDF12 (10) |
| .005/252 daily fee | All fee/financing time accrual uses Actual/360 calendar-day gaps between index business days. | Definitions PDF9 (7); §3 PDF12 (10) |
| Only three calendar signals | The official strategy includes continuous short-term mean reversion as a fifth signal. | §10.3.4 PDF15-16 (13-14) |

A standalone Close-only / forfeited-dividend implementation should be labeled a sensitivity or simplified proxy; it must not be presented as the official GSEQTMX methodology.

## Exact signal definitions and implementation timing

Let ER[d] be the unlevered asset excess-return level. EOM is the current month's last exchange session; prior_EOM is the previous month's EOM. OE is third Friday, adjusted to the preceding exchange session when closed; prior_OE is the previous month's similarly adjusted expiry. Dates such as EOM-5 mean five exchange sessions preceding EOM. The superscript '-1' on EOM and OE in the original PDF means previous month's anchor, not one day before the current anchor.

1. ToM mean reversion decision signal `s1=1` on EOM-4, -3, -2, -1 when `ER[EOM-5] < ER[prior_EOM]`; otherwise 0. This responds to the month-to-date decline known at EOM-5. Held quantities earn the next session's return, so the enhanced economic return window is the final four sessions of the month. There is no future price reference.
2. ToM long decision signal `s2=1` on EOM and on the first three exchange sessions of each month; otherwise 0. After the quantity lag, these decisions increase exposure to the first four exchange-session returns of the following/current month. The brochure's 'first four days' refers to economic exposure, not the same-day decision labels.
3. OpEx decision signal is active at OE-3, -2, -1. Set `s3=sign(ER[OE-4]-ER[prior_OE])`; otherwise 0. Economic exposure covers OE-2, -1, and OE, subject to the caps.
4. FOMC decision signal `s4=1` at announcement-2 and announcement-1 exchange sessions. Otherwise 0. Economic exposure covers announcement-1 and announcement, subject to caps. A qualifying date is a scheduled statement release known/not cancelled at least four exchange sessions in advance. Unscheduled emergency actions and notation votes do not qualify. Historical announcement data do not independently prove the contemporaneous knowledge/cancellation condition.
5. Short-term mean reversion is continuous:
   - `r_er[t] = ER[t]/ER[t-1] - 1`
   - `r5[t] = (5*r_er[t]+4*r_er[t-1]+3*r_er[t-2]+2*r_er[t-3]+r_er[t-4])/15`
   - `sigma10[t] = sqrt(252 * mean(log(ER[t-i+1]/ER[t-i])**2, i=1..10))`
   - `MRscalar[t]=0.15/sigma10[t]`
   - `s5[t]=clip(-5*MRscalar[t]*r5[t], -0.10, +0.10)`.
   The scale is root mean square log return with no mean subtraction or sample ddof. Zero-volatility edge handling is an engineering extension that must be explicit.

The decision target before caps is `allocation_target[t] = .75 + .75*(s1+s2+s3+s4) + 1.50*s5` (definitions PDF9-11 / printed7-9 and §10.2 PDF14 / printed12). Signal coefficients are not separate capital sleeves; they are summed in a single equity allocation. Caps and overlap make signal attribution nonlinear.

On the asset base date the literal allocation rule is `allocation=allocation_target`. On later sessions: `allocation[t]=clip(allocation[t-1]+clip(allocation_target[t]-allocation[t-1],-.75,+.75),0,1.50)`. The initial-day literal rule is a nuance, distinct from later-day caps.

## Excess return and volatility

Official ER asset recursion (§11 PDF16 / printed14):

`ER[t]=ER[t-1]*((Close[t]+cash_dividend[t])/Close[t-1] - financing_rate[t-1]*calendar_days[t]/360)`.

The rate is defined as the notional Fed Funds Rate (PDF10 / printed8). A FRED DFF effective federal funds series is a defensible documented proxy; a 3-month Treasury bill yield is a sensitivity, not the named financing rate. A precise replication would use the calculation agent's notional-rate observations.

The strategy does not credit an additional cash yield on unallocated equity notional in this ER recursion. Adding `(1-weight)*cash_return` would change the return-type economics.

Algorithmic portfolio (§9 PDF14 / printed12):

`AV[t]=AV[t-1]*(1+allocation[t-1]*r_er[t])`.

EWMA variance (§8):

`var_lambda[t]=lambda*var_lambda[t-1]+(1-lambda)*252*log(AV[t]/AV[t-1])**2`, lambda in {.94,.97}.

`vol_final[t]=max(sqrt(var_.94[t]),sqrt(var_.97[t]))`; `VC[t]=.10/vol_final[t]`.

Lookthrough target (§7 PDF13 / printed11): `Wtarget[t]=VC[t-1]*allocation[t]`.

After the index base date, `Wrebal[t]=W[t-1]+clip(Wtarget[t]-W[t-1],-.50,+.50)` and `W[t]=min(1.50,Wrebal[t])`. On the index base date, Wrebal equals Wtarget. Nonnegative allocation and VC keep W nonnegative without an explicit second floor. Do not cap VC itself before multiplying allocation: only the final lookthrough weight is capped.

The vol information ending at t-1 determines W decided at t, which earns t+1. Thus the official portfolio includes a two-session relationship between a shock entering the volatility estimate and its subsequent effect on held exposure. Same-day vol-to-return application would introduce look-ahead.

## Exact index accounting and fees

Index base: level 100. Let `N[t]` denote net index and `G[t]` gross level; Q is ER-asset quantity. After base:

- `G[t]=N[t-1]+Q[t-1]*(ER[t]-ER[t-1])` (§4 PDF13 / printed11)
- `Q[t]=G[t]*W[t]/ER[t]` (§5, same page)
- `RC[t]=abs(Q[t]-Q[t-1])*ER[t]*.0001` (§3.1 PDF12 / printed10)
- `SC[t]=Q[t-1]*ER[t-1]*calendar_days[t]/360*.005` (§3.2)
- `DC[t]=N[t-1]*calendar_days[t]/360*.005` (§3.3)
- `N[t]=G[t]-RC[t]-SC[t]-DC[t]` (§2)

On the index base date all costs are zero and G=N=100. If net N reaches zero or below, it is zero on that and every subsequent index business day; stress testing must enforce this absorbing boundary rather than allow a negative index to recover. The methodology specifies published index levels rounded to two decimals, 0.005 rounded upwards (PDF6 / printed4). Keep internal precision and separate display rounding unless official-level comparison requires another convention.

Service charge is on equity notional rather than NAV. W is normalized to pre-cost G: after costs the instantaneous net-NAV exposure `Q[t]*ER[t]/N[t]` is slightly above W. This is expected from the formulas. When W is unchanged Q may still change due to market drift, ER financing and the previous net cost deduction. Transaction cost therefore cannot be modeled only as an indicator of a weight shift.

For financial impact, distinguish (i) cash fees charged in NAV/points and (ii) compounded terminal wealth difference against a fee-free counterfactual using the same exposure schedule. A sum of costs divided by beginning NAV is not the same object as compounded wealth drag. Compare cumulative fee drag on TimeX against zero additional strategy fees on SPY benchmarks; SPY's own fund expenses already influence its historical prices/adjusted prices and should not be deducted again. Financing drag requires its own attribution label. Dividend drag for the official mode is exactly absent as a contractual exclusion because dividends are included; exposure differences still create a dividend contribution difference relative to 100%-SPY.

## Initialization and validation limits

- Component base: 2021-04-01; ER=100 (PDF10 / printed8 and §11).
- Algorithmic asset base: 2021-05-03; AV=100. EWMA vol initialization: 12.0433% for .94 and 14.6234% for .97 (PDF9 / printed7).
- Index base: 2021-06-01; N=G=100 (PDF10 / printed8 and §2).
- Live index launch: 2023-07-28 (PDF11 / printed9; brochure PDF2).

A ten-year 2016-2026 series extends the supplied methodology before its declared bases. It is a hypothetical historical strategy reconstruction; use long warm-up and explicitly state starting state/seed and pre-base extension assumptions. A separate 2021-base run using the prescribed vol seeds provides a stronger accounting audit. The broker brochure explicitly notes that its historical backtest may use approximation/different data sources/methodology variations (PDF2 and6), so matching a Yahoo-data recreation exactly to promotional history is not guaranteed.

Additional unavoidable approximation sources are Yahoo prices/dividend revisions; notional Fed Funds definition versus proxy observations; lack of historical calculation-agent scheduled/cancelled FOMC state; exchange calendar/disruption treatment; corporate-action treatment; later amendments; and unavailable official daily index values. Without official GSEQTMX daily levels, report a methodology replication rather than an audited tracking-error match.

Use exchange sessions beyond the requested analysis cutoff when constructing calendar anchors; deriving EOM from a truncated observed-price dataframe can falsely mark the cutoff date as month-end. Third-Friday holiday adjustment must use the exchange calendar, e.g. Good Friday expiries, not a weekday-only approximation.

## Implementation review and verification

Reviewed `run.py` against this audit. Core signal, volatility, timing, accounting and cost formulas match the source. A cutoff handling issue was identified: known scheduled FOMC dates just beyond the exclusive price end must be retained because their preceding close decisions can fall within the observed data. Event loading retains these dates, with a passing regression test.

The complete offline suite passes **40 tests** using `python -m pytest -q`.
Tests cover exact ToM windows and source superscript interpretation; decision-to-held lag;
calendar truncation; Good Friday expiry; momentum sign; scheduled and excluded FOMC events;
mean-reversion weighting and RMS scalar; EWMAs on allocated ER returns; both caps;
official seed resets; ACT/360 accrual; quantity turnover and drift; zero absorption;
common-anchor and Shapley reconciliation; causal rate alignment; missing-data rejection;
Sharpe and CAGR definitions; and optional official-level residuals and input validation.
