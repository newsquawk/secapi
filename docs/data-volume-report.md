# SEC 13F Data Volume Report

**Report date:** 2026-09-28
**Data as of:** 2026-09-25 (queried against the live `sec` database)

Snapshot of holdings and filings volume, for sizing Content Hub backfill/ingestion scope.

## Holdings & filings by filing-date window

| Window | Filings | Holdings | Avg holdings/filing |
|---|---:|---:|---:|
| Last 6 months | 14,387 | 5,881,997 | ~409 |
| Last 12 months | 29,105 | 11,389,198 | ~391 |
| Last 24 months | 59,697 | 22,518,143 | ~377 |
| **All time** | **288,264** | **90,002,484** | ~312 |

## Filing-date range

- **Oldest filing:** 1995-11-13
- **Newest filing:** 2026-09-23
- ~31 years of history.

## Notes

- **Full backfill is ~90M holding docs** across 288k filings. The 24 / 12 / 6-month windows are ~22.5M / 11.4M / 5.9M holdings — limiting the initial ingest to the last 12–24 months cuts explode volume by ~4–8× while still covering recent quarters.
- **Avg holdings/filing rises over time** (312 all-time → ~409 in the last 6 months), consistent with funds growing their books. Recent filings are the fattest, which matters for per-page sizing on the change feed.
- Figures count rows in the `holdings` table joined to `filings` on `filing_id`, bucketed by `filings.filing_date`.
