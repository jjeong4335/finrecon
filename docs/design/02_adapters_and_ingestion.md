# Design notes: adapters and ingestion (`finrecon/adapters/`, `finrecon/http.py`, `finrecon/ingestion/`)

**What it does.** Pulls every source page by page, archives raw responses, normalizes, validates and loads.

**Key decisions**
- *Transport abstraction.* Adapters call a `Transport`. Live mode uses `requests`; offline mode uses fixture transports
  that implement the real request contracts (QBO `STARTPOSITION/MAXRESULTS`, Stripe `starting_after/has_more`, the
  `?payout=` filter). The same pagination code runs in both modes and in the tests.
- *Bounded retry.* 429 and 5xx are retried with exponential backoff (honouring `Retry-After`), 4 attempts max.
  Non-retryable 4xx fail immediately. After the last attempt the pull is a failure, never an empty success.
- *Validate before load.* A location's extract is loaded only if records received = records the source reports, and
  detail totals = the source's summary. Otherwise it is `INCOMPLETE` and nothing is written, so the last validated
  data stays intact.
- *Data status per scope.* `VALIDATED`, `ZERO_TRANSACTIONS`, `NO_DATA`, `INCOMPLETE`, `STALE` plus
  `last_successful_sync`. "Zero" and "unknown" are different facts.
- *Incremental ad-spend loads* from the checkpoint minus a 3-day look-back (late-arriving rows), idempotent by source ID.
- *Raw archive with sha256* in `raw_data_manifest`: any number on screen can be traced to the bytes it came from.
- *Simulated clock offline* (06:00 UTC on the as-of date) so snapshots are byte-for-byte reproducible.

**Alternatives rejected.** Loading partial pulls with a warning (silently corrupts totals); full refresh of ad spend
every run (fine here, wasteful at scale); storing only normalized rows (loses auditability).
