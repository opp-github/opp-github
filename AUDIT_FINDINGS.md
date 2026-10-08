# Independent audit of the TimeX research framework

| | |
|---|---|
| Audit date | 2026-10-08 |
| Subject | `timex_research_framework.zip` as received (commit `7958895`, unmodified baseline) |
| Governing document | Goldman Sachs Equity TimeX Index Methodology, 28 July 2023 (SHA256 `9c07796d…eef4`) |
| Explanatory document | GS Equity TimeX brochure, 2023 (SHA256 `ec3a800c…cce2cd`) |
| Environment | Python 3.13.16 with every package pinned to `requirements-lock.txt` |
| Outcome | **No financially material formula or timing error found.** All reference results reproduce exactly. Fifteen findings, concerning validation, test adequacy, provenance, interpretation and presentation, are logged below with their remediation, followed by eleven more (F-16 to F-26) from a pre-publication review of the report. |

This is an audit of an independent research reconstruction. It does not certify
the series as the official GSEQTMX index: official daily levels were not
available, and no claim of exact replication is made.

## 1. Method

1. Read both PDFs in full before changing anything. Methodology formulas
   (sections 2–11 and the defined terms) take precedence over the brochure.
2. Committed the received project unmodified (`7958895`), so every change below
   is reviewable as a diff.
3. Verified frozen input hashes against `results/run_manifest.json`, ran the
   shipped tests (40 passed) and reran the offline pipeline. Every shipped table
   reproduced to ≤1e-9; the residual is last-digit `%.12g` formatting.
4. **Independent re-implementation.** Wrote a second implementation directly
   from the PDF (`audit/independent_reimplementation.py`). It does not import
   `run.py`, and it uses its own calendar logic and literal loops. Its maximum
   absolute differences from `run.py` are below.

   | Quantity | Max abs difference |
   |---|---:|
   | ToM-MR, ToM-long, OpEx, FOMC signals | 0 (identical) |
   | Short-term MR signal | 5e-14 |
   | Allocation target, allocation, lookthrough target, final weight | 5e-12 |
   | 0.94 / 0.97 EWMA volatility | 5e-13 |
   | Asset ER level | 5e-10 (CSV export precision) |
   | Net NAV, notional | 5e-10 / 1.6e-9 (CSV export precision) |
   | Rebalancing, servicing, deduction charges | 5e-14 |

   `tests/test_independent_reimplementation.py` re-runs this cross-check in memory on every CI run.
5. **External anchors.** I compared the reconstruction with numbers GS printed,
   which the original project did not use (section 3).
6. **Mutation testing.** I broke each fix on purpose to confirm a test fails.
   This is also how finding F-05 was discovered.

## 2. Formula and timing verification matrix

| Methodology section | Rule | Status |
|---|---|---|
| §11 Asset ER | `(I_t + D_t)/I_{t-1} − FF_{t-1}·ACT/360`; dividends on ex-date, added once | Verified independently; reproduces the published seed |
| §10.3.1.1 ToM-MR | Decide EOM−4…EOM−1; `A[EOM−5] < A[previous EOM]` | Verified; now pinned by a discriminating test (F-05) |
| §10.3.1.2 ToM-long | Decide EOM and BOM…BOM+2 | Verified |
| §10.3.2 OpEx | Third Friday, holiday → preceding session; decide OE−3…OE−1; `sign(A[OE−4] − A[previous OE])` | Verified incl. Good Friday; now pinned (F-05) |
| §10.3.3 FOMC | Decide 2nd and 1st session before a scheduled announcement | Verified; look-ahead coverage now certified (F-03) |
| §10.3.4 Short-term MR | Weights 5…1 /15; RMS (not demeaned) 10-day log vol; −5·(0.15/σ); clip ±10% | Verified; the clip is shown never to bind (F-08) |
| §10.1–10.2 Allocation | 0.75 + Σ Lʲwʲ; floor 0, cap 150%, ±75pp; literal base-date rule | Verified |
| §9 Algorithmic value | `AV_t = AV_{t−1}(1 + w_{t−1} r_t)` | Verified |
| §8 EWMA | λ ∈ {0.94, 0.97}, 252·ln² term, max of the two | Verified; matches published seed |
| §7 Target weight | `VC_{t−1} × w_t`; VC not capped before multiplication | Verified |
| §6 Final weight | `min(150%, W_{t−1} + clip(target − W_{t−1}, ±50pp))` | Verified (equivalent to cap-then-slew when W ≤ 150%) |
| §§2–5 Accounting | Gross, quantity on gross NAV, RC on \|ΔQ\|, SC on Q·A, DC on prior net, absorbing zero | Verified to 5e-14 per charge |
| Component table | ARC 0.01%, ASC 0.50%; deduction 0.50% | Verified |

## 3. Consistency with GS-published figures

These are GS backtest and initialization figures built on GS's own data. They
corroborate conventions; they are not official daily levels.

| Anchor (source) | Published | Reconstruction | Difference |
|---|---:|---:|---:|
| 0.94 EWMA on Asset Base Date 2021-05-03 (methodology p.9) | 12.0433% | **12.0433%** | +0.0000 |
| 0.97 EWMA on 2021-05-03 (methodology p.9) | 14.6234% | 14.6207% | −0.0027 |
| 1Y return to 28 Jul 2023 (brochure p.6) | 2.15% | 2.52% | +0.37 pp |
| 3Y annualized (brochure p.6) | 5.32% | 5.22% | −0.10 pp |
| 5Y annualized (brochure p.6) | 6.02% | 6.07% | +0.05 pp |
| Rolling-1Y servicing + rebalancing, average (methodology p.5) | ~0.61% | 0.64% | +0.03 pp |
| Rolling-1Y servicing + rebalancing, high (methodology p.5) | ~0.92% | 0.93% | +0.01 pp |

The seed volatilities are a state of GS's own backtest. Matching the 0.94 EWMA
to four decimals tests the whole chain from ER to signals, allocation,
algorithmic portfolio and EWMA. GS's cost sample is 2000–2023; this one is
2015–2023, so the cost comparison is approximate.

**Convention fingerprint** (`results/convention_fingerprint.csv`). Each
plausible misreading was run through the full policy. A convention is flagged
as contradicted when its anchor error exceeds three times the larger of two
values: the baseline's own largest error in that anchor family, or the printed
rounding half-unit. This is a declared heuristic, not a statistical test.

- **Contradicted:** dividends forfeited (seeds, 3Y, 5Y); ToM-MR reference
  EOM−4, the brochure's "5th to last day" wording (3Y, 5Y); EWMA on unallocated
  SPY ER (all seed and return anchors); servicing on NAV instead of notional
  (3Y, 5Y, cost average).
- **Not distinguishable at printed precision:** Yahoo Adj Close in the ER,
  DTB3 funding, same-day Fed Funds, ACT/365, same-day volatility in the target.
  The check does not claim more discrimination than it has.

## 4. Findings register

Severity: **High** = a reported number is wrong; **Medium** = a number could
silently become wrong, or a key claim is untested; **Low** = robustness or
disclosure gap with no current effect; **Info** = analytical finding.

| ID | Sev. | Area | Finding | Action | Effect on reference results |
|---|---|---|---|---|---|
| F-01 | Medium | Reproducibility | The README's fresh-download command (`python run.py --end …`) wrote into `data/`, overwriting the frozen snapshot that reproduces the reference results. | Online runs now default to `data_fresh/`; `data/` is refused without `--overwrite-inputs`; any existing snapshot is never overwritten silently. A `data_revisions.json` report discloses vendor restatements against the frozen snapshot. | None |
| F-02 | Medium | Validation | Session completeness was checked only between the first and last price. A vendor response that started late or stopped early would silently shorten the sample. | `validate_sample_bounds` requires every XNYS session in `[start, end)`. Offline runs take bounds from `download_manifest.json`. | None (snapshot complete) |
| F-03 | Low | Causality | FOMC coverage ended one day after the last price, but the final close decision looks two sessions ahead. | `validate_fomc_coverage` requires coverage through T+2, or a documented next scheduled announcement beyond it (`2026-10-28`). Calendar CSV unchanged (101/2/6/1). | None |
| F-04 | Low | Validation | Cash rates had no plausibility bound, so a unit error (388 instead of 3.88) would accrue silently. | Rates outside −5%…25% annualized now fail. | None |
| F-05 | Medium | Test adequacy | The shipped tests did not pin the ToM-MR reference (EOM−5) or the OpEx reference (OE−4). Their fixtures use constant returns, so mutants using EOM−4 or OE−3 passed all 13 methodology tests. There was also no end-to-end regression test against the reference numbers. | Added fixtures where the two readings disagree in sign, a frozen-snapshot regression test, and the cross-implementation test. Every mutant is now caught. | None |
| F-06 | Low | Simplified model | `max(20d, 60d)` skipped NaN, so the 20-day vol alone set exposure while the 60-day window was incomplete (40 sessions in Q1 2014). | Both windows are now required. | None after the 2016 anchor; summary unchanged |
| F-07 | Low | Provenance | The frozen `download_manifest.json` uses a different schema from the one `download_inputs` writes. It also cites raw acquisition files (`spy_yfinance.csv`, `*_fred_raw.csv`) that are not shipped. | Cannot be fixed retroactively; disclosed here. Frozen CSV hashes are verified by test. New downloads write a consistent manifest with per-file SHA256. | None |
| F-08 | Info | Signal design | The ±10% clip on short-term MR cannot bind for realistic returns. By Cauchy–Schwarz, \|w⁵\| ≤ 0.75·√55/15·√(10/252) = 7.39% when ln(1+r) ≈ r; it binds only for daily moves of about +80% or more. Sample maximum is 7.01%. | Documented, with a property test. | None |
| F-09 | Info | Data | The 0.067% gap between cash-dividend TR and Yahoo Adj Close is a reinvestment convention, not noise or a fee. On ex-dates Yahoo returns `Close_t/(Close_{t−1} − D) − 1` to within 0.0075 bp, which is reinvestment at the dividend-adjusted prior close (prior close minus the dividend) and explains 99.95% of the gap. | `dividend_convention_audit.csv` and a test. | None |
| F-10 | Info | Interpretation | The original report said the gap loss happened "before de-risking", which understates the mechanism. Because VC uses t−1 vol and MR adds weight after a fall, held exposure *rises* from 131% to 138% for the session after a −20% gap, then falls no faster than 50pp per close: 88% held in session +2, 38% in +3, 24% in +4. Most of the rise comes from the MR signal; the t−1 volatility lag only explains why exposure is not cut. | `synthetic_paths.csv`, the `synthetic_gap` chart, a test, and talking point 3. | None |
| F-11 | Info | Source conflict | The brochure says ToM-MR compares performance "on the 5th to last day"; the formula uses EOM−5, the 6th-to-last session. | Formula followed as instructed. The published anchors also favour EOM−5 (F-05 test; fingerprint). | None |
| F-12 | Low | Reporting | `results/validation.json` was hand-maintained, and its claim of a passed live download cannot be re-verified here. | Regenerated from the executed JUnit result. The live download is recorded as not executed in this session, because network policy blocked the providers. | None |
| F-13 | Low | Presentation | Daily exposure was unreadable at ten-year scale, a legend covered data, colours were unexplained, the attribution was not a waterfall, and the SVG embedded a timestamp and random IDs. | Charts redesigned with a validated palette, direct labels and a 21-session mean. Outputs are deterministic: two runs are byte-identical apart from the manifest timestamp. | None |
| F-14 | Low | Reconciliation | The official-level check had no rounding-aware comparison (GS publishes 2 dp, 0.005 rounded up), no per-regime bias or level error, and silently dropped official dates that are not sessions. | Added level-for-level 2-dp comparison, regime metrics, an unmatched-date count, and an extended-history reconciliation. A rounding-only stand-in shows a noise floor of about 0.06% annualized TE and ≤0.005 level error. | None |
| F-15 | Low | Environment | `pip install -r requirements.txt` resolves newer transitive packages than the lock file (lxml, pycparser, pytz, tzdata). | CI installs `requirements-lock.txt` on Python 3.12 and 3.13. | None |

### Pre-publication review (second round)

Before the research report was published as a web page, five separate reviewers
checked it. Their briefs were: every prose number, every table cell (242 cells,
read with an independent parser), compliance wording, accuracy against the
methodology PDF, and rendering at desktop and phone widths in both themes. I
re-derived each numeric claim before acting on it. Findings that also affected
the repository's own outputs:

| ID | Sev. | Area | Finding | Action | Effect on reference results |
|---|---|---|---|---|---|
| F-16 | Medium | Interpretation | The report said exposure "first fell below 50% 2 sessions after the shock". That count used weights *set* at a close, while the chart and the rest of the sentence use exposure *held* over each session, which is first below 50% in session +3. | Metric renamed to `First Session With Held Exposure Below 50%` (held basis, = 3); test updated. | None |
| F-17 | Medium | Presentation | The anchor chart drew every contradicted convention on every row, so crosses appeared on rows whose figure does not contradict that convention. | Each row now shows only conventions that figure contradicts; the legend says so. | None |
| F-18 | Medium | Disclosure | Results were not labelled hypothetical, and tables called reconstruction figures "Index". | Report status now carries a hypothetical / not-advice / not-affiliated disclosure; tables say "Reconstruction". | None |
| F-19 | Medium | Disclosure | The blended ten-year CAGR hid the regimes: 12.89% a year before the base date, −0.11% from base date to launch, 2.90% since launch (SPY TR 17.59% / 5.66% / 19.57%). | `period_breakdown.csv` and a report section; test. | None (new output) |
| F-20 | Medium | Comparison basis | An excess-return index was compared on CAGR only with SPY total return, which includes the cash rate. | Added the like-for-like `SPY Total Return minus Fed Funds` row (12.70% CAGR, 330.40). | Adds a row; existing rows unchanged |
| F-21 | Low | Balance | April 2025 showed only the selloff leg; COVID showed both legs. | Added `April 2025 rebound` (8 Apr to 30 Jun 2025: +5.33% vs +24.81% SPY TR). | Adds a row |
| F-22 | Low | Clarity | Financing cost appeared as both 33.93 (fees charged) and 38.0 (pre-fee bridge step) with no explanation. | Each figure now states its basis. | None |
| F-23 | Info | Analysis | The bridge's "+133.7 signal allocation" step is measured before volatility control. With the full policy recomputed, removing all signals raises terminal wealth (192.08 → 195.91) and reduces drawdown, so over this sample the signals did not add value after volatility control. | Stated in the report and README. | None |
| F-24 | Low | Disclosure | The 4-dp seed match could look circular, because the EWMA warm-up starts from the published seeds. After 1,845 sessions the starting variance weight is ≤3.9e-25, so the match does not come from the seed. | Stated in the report and methodology audit. | None |
| F-25 | Low | Accuracy | "The study precedes the base and launch dates" was false as written (the study spans them). The dividend identity is reinvestment at the dividend-adjusted prior close, not the prior close. The gap-chart annotation credited the volatility lag for the exposure rise, which the MR signal mostly drives. | Wording corrected in the report, chart annotation, README and this log. | None |
| F-26 | Low | Presentation | Chart text was about 8 px at page width; one legend touched the tick labels. | Figures redrawn on smaller canvases (same point sizes); legends moved. | None |

The resume wording no longer says "source-audited". It now states independence
and quotes the anchor agreement precisely (0.94 to four decimals, 0.97 within
0.003, returns within 0.4 points).

## 5. Changes in results, assumptions or interpretation

- **Reference results:** unchanged. TimeX CAGR 6.745943%, volatility 10.041956%,
  Sharpe 0.702540, max drawdown −19.323170%, terminal wealth 192.076831; SPY PR
  and TR, the attribution bridge, Shapley costs, stress tables, sensitivities
  and the simplified-model summary are all bit-identical to the received run.
- **New outputs:** published-anchor checks, convention fingerprint, fee-measure
  table, dividend-convention audit, synthetic exposure paths, and charts for
  anchors and gap risk.
- **Interpretation:** gap risk is stated more precisely (F-10). The dividend
  reading is now supported by GS's own published numbers, not only by the
  formula text (section 3).
- **Assumptions retained:** DFF as the Fed Funds proxy, XNYS as the Arca holiday
  proxy, final-vintage data, a warm-up seeded with the published vols in
  January 2014, and the frozen FOMC transcription.

## 6. Not verified in this session

- **Fresh download.** The environment's egress policy denied
  `fred.stlouisfed.org`, `query1.finance.yahoo.com` and `www.federalreserve.gov`.
  The download path is tested only with mocked providers. Vendor restatements
  since the snapshot are unknown until it is run; `data_revisions.json` will
  report them.
- **FOMC calendar.** Not re-fetched. Spot checks of 2014–2026 against my own
  knowledge of the published calendars found no discrepancy. The
  `next_scheduled_after_coverage` date comes from the original transcription.
- **Official GSEQTMX daily levels.** Not available. No tracking-error claim is made.
- **Methodology version.** Amendments after July 2023 cannot be ruled out.
