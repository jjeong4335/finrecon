# Design notes: data model (`db/schema.sql`, `finrecon/db.py`, `finrecon/money.py`, `finrecon/ids.py`)

**What it does.** Defines clients, locations, external-ID mappings, the six accounting entities, ingestion bookkeeping
and reconciliation results.

**Key decisions**
- *Internal IDs + `source_id_map`.* External systems disagree about what a "client" is. QBO has customers and
  sub-customers, Stripe has customers, the ad platform has its own IDs. One mapping table makes each relationship
  explicit and auditable, and lets one internal location have several external identities.
- *Location = QBO sub-customer, Stripe `metadata.location_key`.* `DepartmentRef` depends on company settings and is an
  accounting dimension, not an identity, so it is not used as a key.
- *Allocations as their own table.* A payment can pay part of one invoice or several invoices; summing allocations per
  invoice is the only way to avoid double counting.
- *Integer cents.* Exact, engine-independent, and the same representation Stripe uses. QBO decimals are parsed with
  `Decimal`; floats are rejected.
- *UUIDv5 from business keys.* Re-runs produce the same IDs, so `ON CONFLICT` upserts are idempotent.

**Alternatives rejected.** An ORM (hides the SQL the interview asked about); `NUMERIC(14,2)` (fine in PostgreSQL but
not exact in SQLite, which runs the tests); external IDs as primary keys (break when a source re-keys or two sources collide).
