# Design notes: real public data module (`finrecon/ingestion/uci_retail.py`, `db/retail_staging.sql`)

**What it does.** Loads UCI Online Retail II (about 1.07M real invoice lines, UK online retailer, 2009-12 to
2011-12, GBP, CC BY 4.0) through raw → validated → invoice layers, and reports what is wrong with it without
losing a single row.

**Why it is separate.** The retail data has no Stripe or QuickBooks identifiers, so matching it to the sandbox
records would be invented. `Country` is the customer's country, not a store location, so it is not mapped to
locations. The module shows data-engineering on real, messy data; the reconciliation shows systems integration.

**Key decisions**
- *Every row kept.* `retail_raw_lines` stores each source row as text keyed `<sheet>#<row>`; `retail_lines` has
  exactly one parsed row per raw row. Control checks prove row counts and amounts are conserved.
- *One class per line:* VALID_SALE, CANCELLATION (`C…`), ADJUSTMENT (`A…`, bad debt), NON_PRODUCT (postage, fees,
  manual lines: stock codes that are not 5 digits + optional letters), STOCK_ADJUSTMENT (negative quantity on a
  sale invoice), INVALID (unparseable). Flags are separate columns: missing customer, zero price, sub-penny amount,
  missing description, possible duplicate, sheet overlap.
- *Duplicates are flagged, not deleted.* An identical line repeated on one invoice may be two genuine units keyed
  separately. Deleting would understate revenue. Only an invoice copied identically into a second sheet (a file
  layout artifact) is excluded, once, from totals; if the copies differ, both are kept and flagged as a conflict.
- *Missing customer IDs stay in revenue*, but are excluded from customer-level analysis.
- *Cancellation tracing.* "C" invoices do not reference the original sale. A cancellation is LINKED when the same
  customer bought the same item earlier in the data for at least that quantity. Unlinked cancellations cluster in
  the first months, when the original sale can predate the dataset.
- *Money.* Unit prices are kept as exact decimal text; line amounts are pence, rounded half-up, with a sub-penny flag.
- *Chunked loading* (50,000 rows per transaction) with timings per stage in `retail_load_runs`.

**Measured.** The real file (1,067,371 rows) loads in 73 s on a MacBook with SQLite: read + parse 40 s, write 26 s,
validate 7 s. 22,523 rows (2.1%) are identical copies of invoices present in both sheets and are excluded once; no
invoice differs between sheets. The full load sequence was also replayed on PostgreSQL 16 with identical results.
