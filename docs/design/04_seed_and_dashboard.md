# Design notes: synthetic data (`seed/`) and web app (`webapp/`, `finrecon/dashboard_data.py`)

**Seed.** One plan (`seed/scenarios.py`, fixed seed) acts as a fictional agency's billing system and payment sync.
It renders to offline fixtures shaped like real API objects, to real sandbox objects (`live_seed.py`, idempotent by
`seed_key`) and to the ground truth. Discrepancies are injected on purpose and documented per scenario code.

**Web app.** Django (`webapp/`) renders every page from `finrecon/dashboard_data.py`, the tested query module the
static report also uses; views only filter and shape. There are no models, sessions, logins or forms that write, so
the ORM, admin, auth and CSRF machinery are not installed, and in Docker the app connects with a read-only PostgreSQL
role behind Caddy (gunicorn, 2 workers). Each reconciliation result has its own URL
(`/record/<type>/<id>/`) with the reason, IDs on both sides, allocations, the ingestion run, archived raw pages and,
for Stripe payments, a balanced journal-entry preview. `finrecon/report.py` renders the same content as one static
HTML file as a no-server fallback.

**Why Django.** It started as Streamlit, which was the fastest way to show results. It moved to Django for
URL-addressable records (a link to one exception can be sent), full control of the HTML, and a natural next step: a
review workflow for NEEDS_REVIEW items (log in, approve or reject a match, keep an audit trail), which needs auth and
writes that a dashboard tool does not offer. Because all queries were already in a separate tested module, the port
replaced only the presentation layer.
