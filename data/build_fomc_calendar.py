"""Build the checked-in FOMC event audit from browsed official Fed calendars.

This is a reproducible transcription of the official calendar snapshot, not a
historical snapshot archive of when every meeting was first scheduled.
"""
from pathlib import Path
import csv
import json
from collections import Counter
from datetime import date

# When updating this calendar, extend SCHEDULED from the official sources,
# review exceptional events individually, and update these coverage dates.
# The verification date is the source snapshot date, not the last event date.
COVERAGE_START = "2014-01-01"
COVERAGE_END = "2026-10-08"
VERIFIED_AS_OF = "2026-10-08"

SCHEDULED = {
    2014: "01-29 03-19 04-30 06-18 07-30 09-17 10-29 12-17",
    2015: "01-28 03-18 04-29 06-17 07-29 09-17 10-28 12-16",
    2016: "01-27 03-16 04-27 06-15 07-27 09-21 11-02 12-14",
    2017: "02-01 03-15 05-03 06-14 07-26 09-20 11-01 12-13",
    2018: "01-31 03-21 05-02 06-13 08-01 09-26 11-08 12-19",
    2019: "01-30 03-20 05-01 06-19 07-31 09-18 10-30 12-11",
    2020: "01-29 04-29 06-10 07-29 09-16 11-05 12-16",
    2021: "01-27 03-17 04-28 06-16 07-28 09-22 11-03 12-15",
    2022: "01-26 03-16 05-04 06-15 07-27 09-21 11-02 12-14",
    2023: "02-01 03-22 05-03 06-14 07-26 09-20 11-01 12-13",
    2024: "01-31 03-20 05-01 06-12 07-31 09-18 11-07 12-18",
    2025: "01-29 03-19 05-07 06-18 07-30 09-17 10-29 12-10",
    2026: "01-28 03-18 04-29 06-17 07-29 09-16",
}

ROOT = "https://www.federalreserve.gov"
rows = []
for year, dates in SCHEDULED.items():
    url = (
        f"{ROOT}/monetarypolicy/fomchistorical{year}.htm"
        if year <= 2020 else f"{ROOT}/monetarypolicy/fomccalendars.htm"
    )
    rows.extend((f"{year}-{md}", "scheduled", url) for md in dates.split())

# Excluded event categories retained for audit. A scheduled-only selection is
# required for the TimeX signal. Unscheduled events were not known in advance.
EXCLUDED = [
    ("2019-10-11", "notation_vote", "monetary20191011a.htm"),
    ("2020-03-03", "unscheduled", "monetary20200303a.htm"),
    ("2020-03-15", "unscheduled", "monetary20200315a.htm"),
    ("2020-03-19", "notation_vote", "monetary20200319b.htm"),
    ("2020-03-23", "notation_vote", "monetary20200323a.htm"),
    ("2020-03-31", "notation_vote", "monetary20200331a.htm"),
    ("2020-08-27", "notation_vote", "monetary20200827a.htm"),
    ("2025-08-22", "notation_vote", "monetary20250822a.htm"),
]
rows.extend((d, kind, f"{ROOT}/newsevents/pressreleases/{page}")
            for d, kind, page in EXCLUDED)

# This row is an originally scheduled decision date, not an announcement.
# Keeping it explicitly cancelled makes the COVID calendar treatment visible.
rows.append(("2020-03-18", "cancelled", f"{ROOT}/monetarypolicy/fomchistorical2020.htm"))
rows.sort()

assert len(rows) == len({r[0] for r in rows})
assert all(date.fromisoformat(COVERAGE_START) <= date.fromisoformat(r[0])
           <= date.fromisoformat(COVERAGE_END)
           for r in rows)
assert Counter(r[1] for r in rows)["scheduled"] == 101
assert all(len(SCHEDULED[y].split()) == 8 for y in range(2014, 2026) if y != 2020)
assert len(SCHEDULED[2020].split()) == 7  # March 18 meeting was cancelled.
assert len(SCHEDULED[2026].split()) == 6  # Six realized meetings by Oct 8.
path = Path(__file__).with_name("fomc_calendar.csv")
with path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["date", "event_type", "source_url"])
    writer.writerows(rows)
coverage = {
    "start": COVERAGE_START,
    "end": COVERAGE_END,
    "verified_as_of": VERIFIED_AS_OF,
    "point_in_time": False,
    "notes": [
        "Source-backed final historical Federal Reserve calendars, not a dated vendor schedule archive.",
        "Scheduled policy announcement dates use the second/final meeting day, not the first meeting day.",
        "Baseline event_type scheduled contains 101 realized scheduled decisions.",
        "Two unscheduled statements, six notation votes, and cancelled 2020-03-18 are retained for audit and excluded from scheduled-only signals.",
        "2020-03-18 must be tested as a calendar sensitivity because its eligibility at the four-index-business-day cutoff is not certified.",
        "2026-10-28 and 2026-12-09 are future scheduled dates relative to this verification date and are not in the historical CSV.",
        "Verify and extend the data from official Fed sources before a run outside the stated coverage.",
    ],
}
path.with_name("fomc_coverage.json").write_text(
    json.dumps(coverage, indent=2) + "\n", encoding="utf-8"
)
print(f"Wrote {path}: {dict(Counter(r[1] for r in rows))}")
