# Design notes: synthetic data (`seed/`) and dashboard (`dashboard/`, `finrecon/dashboard_data.py`)

**Seed.** One plan (`seed/scenarios.py`, fixed seed) acts as a fictional agency's billing system and payment sync.
It renders to offline fixtures shaped like real API objects, to real sandbox objects (`live_seed.py`, idempotent by
`seed_key`) and to the ground truth. Discrepancies are injected on purpose and documented per scenario code.

**Dashboard.** Streamlit reads the database only, through `finrecon/dashboard_data.py`, which the tests cover. In
Docker it connects with a read-only PostgreSQL role. Selecting a row shows the reason, IDs on both sides, allocations,
the ingestion run and the raw pages archived for it; Stripe payments also show a balanced journal-entry preview.
`finrecon/report.py` renders the same content as one static HTML file as a no-server fallback.

**Why Streamlit.** The goal is to make reasoning inspectable in a minute, not to build a front end. The trade-off is
limited layout control.
