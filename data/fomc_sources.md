# FOMC decision-date calendar audit

Coverage: 2014-01-01 through 2026-10-08 inclusive. Official sources were browsed
on 2026-10-08. The CSV contains 101 realized scheduled policy decision dates,
2 unscheduled announcement dates, 6 notation-vote announcements, and one
cancelled originally scheduled decision date. No future statement is treated
as historical.

`date` means the public announcement date, usually the second day of a two-day
scheduled meeting. The sole `cancelled` row is the date on which the original
scheduled announcement would have occurred. It is retained only for audit.
Use `event_type == "scheduled"` to form the baseline calendar signal. The
unscheduled and notation-vote rows must not activate a pre-announcement signal.

## Primary sources

- [Fed historical materials, 2014](https://www.federalreserve.gov/monetarypolicy/fomchistorical2014.htm)
- [Fed historical materials, 2015](https://www.federalreserve.gov/monetarypolicy/fomchistorical2015.htm)
- [Fed historical materials, 2016](https://www.federalreserve.gov/monetarypolicy/fomchistorical2016.htm)
- [Fed historical materials, 2017](https://www.federalreserve.gov/monetarypolicy/fomchistorical2017.htm)
- [Fed historical materials, 2018](https://www.federalreserve.gov/monetarypolicy/fomchistorical2018.htm)
- [Fed historical materials, 2019](https://www.federalreserve.gov/monetarypolicy/fomchistorical2019.htm)
- [Fed historical materials, 2020](https://www.federalreserve.gov/monetarypolicy/fomchistorical2020.htm)
- [Fed meeting calendars, statements, and minutes, 2021–2026](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)

Each scheduled CSV row links to the source calendar showing both meeting days
and its policy statement. Exceptional public announcements link directly to
their verified official press release. `build_fomc_calendar.py` reproduces the
CSV deterministically from the sourced transcription and checks its coverage,
uniqueness, scheduled-decision count, and eight-decision-per-complete-year
pattern (seven in 2020 and six realized through September 2026).
`fomc_coverage.json` records exact coverage 2014-01-01 through 2026-10-08,
verification as of 2026-10-08, and `point_in_time: false`. The builder regenerates
both files. Updating the calendar requires extending its sourced date list,
reviewing exceptional events, and updating the explicit coverage constants.

## Exceptional dates and exclusions

| Date | Treatment | Reason |
|---|---|---|
| 2014-03-04 | Excluded, no CSV announcement row | Historical archive lists an unscheduled meeting without a contemporaneous public policy statement. This is a meeting date, not a sourced announcement date. |
| 2019-10-11 | Excluded notation vote | The archive groups it under an unscheduled Oct 4 meeting, but the public statement was issued Oct 11 and explicitly describes a notation vote completed Oct 11. |
| 2020-03-03 | Excluded unscheduled statement | Archive lists a March 2 meeting; public policy statement was released March 3 at 10 a.m. EST. |
| 2020-03-15 | Excluded unscheduled statement | Sunday announcement at 5 p.m. EDT; do not shift it back to Friday or synthesize a tradable pre-event signal. |
| 2020-03-18 | Excluded cancelled scheduled date | March 17–18 scheduled meeting was cancelled. No statement was published on March 18. |
| 2020-03-19 | Excluded notation vote | Temporary dollar liquidity swap arrangements. |
| 2020-03-23 | Excluded notation vote | FOMC statement on asset purchases. |
| 2020-03-31 | Excluded notation vote | FIMA repo facility. |
| 2020-08-27 | Excluded notation vote | Update to longer-run policy strategy. |
| 2025-08-22 | Excluded notation vote | Update to longer-run policy strategy. |
| 2026-10-28 and 2026-12-09 | Not in historical CSV | Future scheduled decisions relative to project as-of date. |

## Point-in-time qualification

This is a **source-backed historical calendar**, not a point-in-time database
of scheduling/cancellation publication timestamps. Scheduled meetings are
generally announced in advance, and the Fed notes that tentative dates are
confirmed at the preceding meeting. That supports their use as deterministic
calendar inputs in an educational approximation; it does not certify that
every date was scheduled or uncancelled at the exact four-business-day cutoff
required by the index methodology.

The cancelled March 18, 2020 meeting is the material audit edge case. Simply
removing it from all earlier pre-event dates uses the final calendar ex post.
The [original 2020 schedule announcement, dated May 17, 2019](https://www.federalreserve.gov/newsevents/pressreleases/monetary20190517a.htm)
included March 17–18. In the [March 15, 2020 press call transcript, page 14](https://www.federalreserve.gov/mediacenter/files/FOMCpresconf20200315.pdf),
Powell says the Committee decided on Thursday to move the meeting forward.
That Thursday was March 12, exactly four ordinary US equity business days
before March 18. This establishes the internal decision date, not when the
change became available to the calculation agent's vendor. The two must not
be conflated.
An exact replication needs dated schedule revisions and the cancellation's
effective publication time, plus the methodology's four-index-business-day
eligibility rule. The framework should disclose this limitation and provide
a calendar override or sensitivity run that retains March 18 as an eligible
originally scheduled date; it should not label the final-calendar selection
fully free of calendar look-ahead.

The checked-in calendar must be explicitly extended using verified primary
sources when running beyond its coverage; absence of future rows is not a
claim that no meetings occur after 2026-10-08.

## Look-ahead certification (added in the 2026-10-08 audit)

The FOMC decision at close T depends on whether T+1 or T+2 is a scheduled
announcement. Coverage ending on the day after the last price therefore does
not by itself certify the final decision. `fomc_coverage.json` now records
`next_scheduled_after_coverage: 2026-10-28` (the second day of the 27–28
October 2026 meeting on the official meeting calendar, as already noted in the
original transcription above). `run.py` accepts a sample only if coverage
reaches the second session after the last price, or if the documented next
scheduled announcement lies beyond it. This field was carried over from the
original verified transcription, not re-fetched: the audit session's network
policy blocked federalreserve.gov. It must be re-verified whenever coverage is
extended. It is not a CSV row and never activates a signal by itself.
